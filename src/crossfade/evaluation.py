"""Independent evaluation.

Order of judgment:
  1. Validity: protected paths untouched, enough runs completed, no infrastructure failures hiding results.
  2. Correctness gates: scenario expectations, world invariants, and the bundle's gate functions (checks.py).
     Any failed gate rejects the candidate, whatever its performance.
  3. Objectives: compared across repeated trials with a bootstrap confidence interval. Improvement must clear
     the policy threshold with the required confidence; noisy or ambiguous results are inconclusive.

The evaluator only reads locked bundle code and Crossfade-measured evidence. Nothing the candidate reports
about itself decides the outcome.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .models import Check, Outcome, Policy, Run, RunStatus, Scenario
from .project import Bundle
from .runner import lookup

EVALUATOR = "crossfade.evaluator.default"
EVALUATOR_VERSION = "1.0"

OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b, "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "le": lambda a, b: a <= b, "in": lambda a, b: a in b, "contains": lambda a, b: b in a,
    "exists": lambda a, b: a is not None,
}


@dataclass
class VersionView:
    """Everything the gate functions may look at for one implementation: runs, outcomes and the scenarios."""

    label: str
    runs: list[Run]
    outcomes: dict[str, dict[str, Any]]  # run id -> outcome
    scenarios: dict[str, Scenario]
    initial_worlds: dict[str, dict[str, Any]] = field(default_factory=dict)  # run id -> initial world state

    def completed(self) -> list[tuple[Run, dict[str, Any]]]:
        return [(r, self.outcomes[r.id]) for r in self.runs if r.status == RunStatus.COMPLETED]


def expectation_failures(scenario: Scenario, outcome: dict[str, Any]) -> list[str]:
    fails = []
    for e in scenario.expect:
        try:
            actual = lookup(outcome, e.target)
        except (KeyError, IndexError, ValueError):
            actual = None
            if e.op != "exists":
                fails.append(f"{e.target}: missing (expected {e.op} {e.value!r})")
                continue
        try:
            ok = OPS[e.op](actual, e.value)
        except TypeError:
            ok = False
        if not ok:
            fails.append(f"{e.description or e.target}: got {actual!r}, expected {e.op} {e.value!r}")
    return fails


def _gate_checks(bundle: Bundle, v: VersionView, side: str) -> list[Check]:
    checks: list[Check] = []
    # the same failure in every trial is one finding, reported once with how often it happened
    counts: dict[str, set[int]] = {}
    for r, o in v.completed():
        sc = v.scenarios[r.scenario]
        for f in expectation_failures(sc, o):
            key = "[holdout scenario] an expectation failed (details withheld)" if sc.holdout else f"[{r.scenario}] {f}"
            counts.setdefault(key, set()).add(r.trial)
    trials = len({r.trial for r in v.runs}) or 1
    exp_fail = [f"{k} (in {len(t)} of {trials} trials)" for k, t in counts.items()]
    checks.append(Check(name="scenario_expectations", kind="gate", passed=not exp_fail, detail="; ".join(exp_fail)[:2000],
                        evidence=[r.id for r in v.runs]))
    wv = [f"[{'holdout scenario' if v.scenarios[r.scenario].holdout else r.scenario}] {x if not v.scenarios[r.scenario].holdout else 'world invariant violated'}"
          for r, o in v.completed() for x in o.get("world_violations", [])]
    checks.append(Check(name="world_invariants", kind="gate", passed=not wv, detail="; ".join(dict.fromkeys(wv))[:2000]))
    # Gate functions run separately over visible and holdout runs. Holdout failures are reported without
    # details, so evaluation feedback can't teach a proposing agent what the protected cases contain.
    visible = VersionView(v.label, [r for r in v.runs if not v.scenarios[r.scenario].holdout], v.outcomes, v.scenarios)
    hidden = VersionView(v.label, [r for r in v.runs if v.scenarios[r.scenario].holdout], v.outcomes, v.scenarios)
    for name in bundle.policy.gates:
        fn = getattr(bundle.checks, name, None)
        if fn is None:
            checks.append(Check(name=name, kind="gate", passed=False, detail="gate function missing from locked checks.py"))
            continue
        results = []
        for part in (visible, hidden):
            if not part.runs:
                results.append((True, ""))
                continue
            try:
                res = fn(part)
                results.append((bool(res), "") if isinstance(res, bool) else (bool(res[0]), str(res[1])))
            except Exception as e:  # noqa: BLE001 - a crashing gate is a failed gate, recorded
                results.append((False, f"gate raised {type(e).__name__}: {e}"))
        (ok_v, det_v), (ok_h, _) = results
        detail = det_v if not ok_v else ""
        if not ok_h:
            detail = (detail + "; " if detail else "") + "also failed on holdout scenarios (details withheld)"
        checks.append(Check(name=name, kind="gate", passed=ok_v and ok_h, detail=detail[:2000]))
    for c in checks:
        c.name = f"{side}:{c.name}"
    return checks


def _paired_trials(base: list[Run], cand: list[Run], metric: str) -> tuple[list[float], list[float], str]:
    """Per-trial totals of a metric for both sides, or a reason they can't be compared.

    Totals are only comparable when both sides have a completed run reporting the metric for exactly the same
    (trial, scenario) cells. Dropping a run silently would shrink one side's total and fake an improvement."""
    cells = {(r.trial, r.scenario) for r in base + cand}
    sides: list[dict[int, float]] = []
    for label, runs in (("baseline", base), ("candidate", cand)):
        have = {(r.trial, r.scenario): r for r in runs}
        missing = sorted(c for c in cells if c not in have or have[c].status != RunStatus.COMPLETED or metric not in have[c].metrics)
        if missing:
            t, sc = missing[0]
            return [], [], f"{label} has no {metric} for {len(missing)} of {len(cells)} trial/scenario cells (e.g. trial {t + 1}, {sc})"
        totals: dict[int, float] = {}
        for (trial, _), r in have.items():
            totals[trial] = totals.get(trial, 0.0) + r.metrics[metric]
        sides.append(totals)
    trials = sorted(sides[0])
    return [sides[0][t] for t in trials], [sides[1][t] for t in trials], ""


def bootstrap_improvement(base: list[float], cand: list[float], direction: str, confidence: float, seed: int = 7, n: int = 4000) -> tuple[float, float, float]:
    """Relative improvement of the candidate mean over the baseline mean, with a percentile bootstrap interval.
    Positive means better in the chosen direction."""

    def rel(b: list[float], c: list[float]) -> float:
        mb, mc = statistics.fmean(b), statistics.fmean(c)
        if mb == 0:
            return 0.0 if mc == 0 else (-1.0 if direction == "lower" else 1.0)
        return (mb - mc) / abs(mb) if direction == "lower" else (mc - mb) / abs(mb)

    point = rel(base, cand)
    rng = random.Random(seed)
    samples = sorted(rel([rng.choice(base) for _ in base], [rng.choice(cand) for _ in cand]) for _ in range(n))
    a = (1 - confidence) / 2
    return point, samples[int(a * n)], samples[min(n - 1, int((1 - a) * n))]


def cv(xs: list[float]) -> float:
    if len(xs) < 2 or statistics.fmean(xs) == 0:
        return 0.0
    return statistics.stdev(xs) / abs(statistics.fmean(xs))


@dataclass
class Judgment:
    outcome: Outcome
    reasons: list[str]
    checks: list[Check]


def judge(bundle: Bundle, policy: Policy, base: VersionView, cand: VersionView, touched: list[str], protected_hits: list[str],
          review_hits: list[str]) -> Judgment:
    checks: list[Check] = []
    reasons: list[str] = []

    # 1. validity
    checks.append(Check(name="protected_paths_untouched", kind="gate", passed=not protected_hits,
                        detail=("candidate modified protected paths: " + ", ".join(protected_hits)) if protected_hits else f"{len(touched)} paths changed"))
    if protected_hits:
        return Judgment(Outcome.REJECTED, ["The candidate modified protected evaluation files: " + ", ".join(protected_hits)], checks)
    infra = [r for r in base.runs + cand.runs if r.status == RunStatus.INFRA_FAILED]
    if infra:
        checks.append(Check(name="infrastructure", kind="info", passed=False, detail=f"{len(infra)} runs failed during setup: {infra[0].error}"))
        return Judgment(Outcome.INCONCLUSIVE, [f"{len(infra)} runs failed for infrastructure reasons, so the comparison is incomplete."], checks)
    cand_crash = [r for r in cand.runs if r.status == RunStatus.APP_FAILED]
    base_crash = [r for r in base.runs if r.status == RunStatus.APP_FAILED]
    checks.append(Check(name="candidate:runs_completed", kind="gate", passed=not cand_crash,
                        detail=f"{len(cand_crash)} candidate runs failed: {cand_crash[0].error}" if cand_crash else "all runs completed"))
    checks.append(Check(name="baseline:runs_completed", kind="gate", passed=not base_crash,
                        detail=f"{len(base_crash)} baseline runs failed: {base_crash[0].error}" if base_crash else "all runs completed"))

    # 2. correctness gates
    base_checks = _gate_checks(bundle, base, "baseline")
    cand_checks = _gate_checks(bundle, cand, "candidate")
    checks += base_checks + cand_checks
    failed = [c for c in cand_checks if c.passed is False]
    if cand_crash:
        reasons.append(f"{len(cand_crash)} candidate runs crashed or timed out.")
    for c in failed:
        reasons.append(f"Required check failed: {c.name.split(':', 1)[1]}" + (f" ({c.detail})" if c.detail else ""))
    base_failed = [c for c in base_checks if c.passed is False]

    # 3. objectives (still measured when gates fail, so the record shows what the change would have bought)
    primary_ok, primary_any, noisy, regressed = [], False, [], []
    incomplete: list[str] = []
    for obj in policy.objectives:
        metric, direction = obj["metric"], obj.get("direction", "lower")
        bs, cs, problem = _paired_trials(base.runs, cand.runs, metric)
        if problem:
            checks.append(Check(name=f"objective:{metric}", kind="objective", detail=f"not comparable: {problem}"))
            incomplete.append(f"{metric} ({problem})")
            continue
        if len(bs) < policy.min_trials:
            checks.append(Check(name=f"objective:{metric}", kind="objective", detail=f"not enough trials ({len(bs)}, need {policy.min_trials})"))
            incomplete.append(f"{metric} (only {len(bs)} trials, policy needs {policy.min_trials})")
            continue
        point, lo, hi = bootstrap_improvement(bs, cs, direction, policy.confidence)
        mb, mc = statistics.fmean(bs), statistics.fmean(cs)
        detail = (f"{metric}: baseline {mb:.4g}, candidate {mc:.4g}, improvement {point:+.1%} "
                  f"({policy.confidence:.0%} CI {lo:+.1%} to {hi:+.1%}); CV {cv(bs):.2f}/{cv(cs):.2f}")
        role = obj.get("role", "primary")
        if role == "primary":
            primary_any = True
            if max(cv(bs), cv(cs)) > policy.max_cv:
                noisy.append(metric)
                detail += " (too noisy to decide)"
            elif lo >= obj.get("min_improvement", 0.05):
                primary_ok.append(metric)
            elif hi < 0:
                regressed.append(f"{metric} got worse")
        else:  # guard: must not regress beyond tolerance
            if lo < -obj.get("max_regression", 0.1) and point < -obj.get("max_regression", 0.1):
                regressed.append(f"{metric} regressed beyond the {obj.get('max_regression', 0.1):.0%} tolerance")
        checks.append(Check(name=f"objective:{metric}", kind="objective", detail=detail, baseline=mb, candidate=mc))

    if failed or cand_crash:
        if base_failed:
            reasons.append("Note: the baseline also failed " + ", ".join(c.name.split(":", 1)[1] for c in base_failed) + ".")
        return Judgment(Outcome.REJECTED, reasons, checks)
    if base_crash:
        return Judgment(Outcome.INCONCLUSIVE, [f"{len(base_crash)} baseline runs crashed or timed out ({base_crash[0].error}), so there is no valid "
                                               "comparison point. Re-run the experiment or fix the baseline."], checks)
    if incomplete:
        return Judgment(Outcome.INCONCLUSIVE, ["Measurements are incomplete, so the versions can't be compared: " + "; ".join(incomplete) + "."], checks)
    if base_failed:
        return Judgment(Outcome.NEEDS_REVIEW, ["The baseline itself fails required checks: " + ", ".join(c.name.split(":", 1)[1] for c in base_failed)
                                               + ". The comparison point is not trustworthy."], checks)
    if regressed:
        return Judgment(Outcome.REJECTED, ["Correctness held, but " + "; ".join(regressed) + "."], checks)
    if review_hits:
        return Judgment(Outcome.NEEDS_REVIEW, ["Correctness held, but the change touches paths that require human review: " + ", ".join(review_hits)], checks)
    if primary_any and primary_ok and not noisy:
        return Judgment(Outcome.ACCEPTED, ["All required checks passed.", "Improved " + ", ".join(primary_ok) + " beyond the policy threshold with the required confidence."], checks)
    if noisy:
        return Judgment(Outcome.INCONCLUSIVE, ["All required checks passed, but " + ", ".join(noisy) + " varied too much or had too few trials to decide."], checks)
    if not primary_any:
        return Judgment(Outcome.NEEDS_REVIEW, ["All required checks passed. The policy defines no primary objective, so a person decides."], checks)
    return Judgment(Outcome.INCONCLUSIVE, ["All required checks passed, but the improvement did not clear the policy threshold with the required confidence."], checks)
