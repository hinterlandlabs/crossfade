"""Experiment lifecycle.

draft -> validated -> preparing -> baseline/candidate running (interleaved per trial) -> evaluating -> completed
(or failed / cancelled). Lifecycle state is recorded as events; the terminal Decision is a separate object, so a
completed experiment can still end in a rejected candidate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import revisions
from .evaluation import EVALUATOR, EVALUATOR_VERSION, VersionView, judge
from .models import (
    Candidate,
    Decision,
    Evaluation,
    Experiment,
    ExperimentState,
    Hypothesis,
    Outcome,
    Revision,
    Run,
    new_id,
)
from .project import Workspace, WorkspaceError
from .runner import load_outcome, run_scenario


@dataclass
class Result:
    experiment: Experiment
    decision: Decision
    evaluation: Evaluation


def state(ws: Workspace, exp_id: str) -> ExperimentState:
    evs = [e for e in ws.ledger.events(subject=exp_id) if e.type == "experiment.state"]
    return ExperimentState(evs[-1].payload["state"]) if evs else ExperimentState.DRAFT


def _set(ws: Workspace, exp: Experiment, st: ExperimentState, **info: object) -> None:
    ws.ledger.append("experiment.state", actor="crossfade.experiments", project_id=exp.project_id, subject=exp.id, state=st.value, **info)


def create(ws: Workspace, name: str, baseline: str, candidate: str, scenarios: list[str] | None = None, trials: int | None = None,
           hypothesis: str | None = None, seed: int = 0, actor: str = "human") -> Experiment:
    bundle = ws.bundle()
    base = revisions.resolve(ws, baseline)
    cand = revisions.resolve(ws, candidate)
    names = scenarios or sorted(bundle.scenarios)
    exp = Experiment(id=new_id("exp"), project_id=ws.project.id, name=name, hypothesis_id=hypothesis, baseline_revision=base.id,
                     candidate_revision=cand.id, scenarios=names, policy_digest=bundle.digest,
                     trials=max(trials or bundle.policy.min_trials, bundle.policy.min_trials), seed=seed)
    ws.ledger.put(exp, actor=actor, event="experiment.created")
    if not any(c.candidate_revision == cand.id for c in ws.ledger.list("candidate", Candidate, limit=10000)):
        touched = revisions.changed_paths(ws, base, cand)
        patch = ws.artifacts.put_bytes(revisions.patch(ws, base, cand).encode())
        ws.ledger.put(Candidate(id=new_id("cand"), project_id=ws.project.id, hypothesis_id=hypothesis, base_revision=base.id,
                                candidate_revision=cand.id, originator=actor, patch_artifact=patch, touched_paths=touched), actor=actor)
    return exp


def validate(ws: Workspace, exp: Experiment) -> list[str]:
    problems = []
    try:
        bundle = ws.bundle(exp.policy_digest)
    except Exception as e:  # noqa: BLE001
        return [f"evaluation bundle {exp.policy_digest} unavailable: {e}"]
    if not ws.artifacts.exists(exp.policy_digest):
        problems.append("locked bundle missing from the artifact store")
    for s in exp.scenarios:
        if s not in bundle.scenarios:
            problems.append(f"unknown scenario {s}")
    for rid in (exp.baseline_revision, exp.candidate_revision):
        try:
            rev = revisions.require(ws, rid)
            from .project import git

            git(ws.root, "cat-file", "-e", rev.commit)
        except WorkspaceError as e:
            problems.append(str(e))
    if bundle.policy.allow_network:
        problems.append("policy allows network access; experiments require deny-by-default network")
    return problems


def run(ws: Workspace, exp: Experiment, progress: Callable[[str], None] = lambda s: None) -> Result:
    problems = validate(ws, exp)
    if problems:
        _set(ws, exp, ExperimentState.FAILED, problems=problems)
        raise WorkspaceError("experiment failed validation: " + "; ".join(problems))
    _set(ws, exp, ExperimentState.VALIDATED)
    bundle = ws.bundle(exp.policy_digest)
    base = ws.ledger.get(exp.baseline_revision, Revision)
    cand = ws.ledger.get(exp.candidate_revision, Revision)
    _set(ws, exp, ExperimentState.PREPARING)
    revisions.worktree(ws, base)
    revisions.worktree(ws, cand)
    base_runs: list[Run] = []
    cand_runs: list[Run] = []
    try:
        for trial in range(exp.trials):
            # interleave baseline and candidate so slow drift on the machine affects both equally
            order = [("baseline", base, base_runs), ("candidate", cand, cand_runs)]
            if trial % 2:
                order.reverse()
            for side, rev, sink in order:
                _set(ws, exp, ExperimentState.BASELINE_RUNNING if side == "baseline" else ExperimentState.CANDIDATE_RUNNING, trial=trial)
                for sname in exp.scenarios:
                    sc = bundle.scenarios[sname]
                    res = run_scenario(ws, bundle, rev, sc, trial=trial, seed=exp.seed + sc.seed + trial, experiment_id=exp.id)
                    sink.append(res.run)
                    progress(f"{side:9} trial {trial + 1}/{exp.trials}  {sname:28} {res.run.status.value:13} "
                             f"{res.run.metrics.get('latency_ms', 0):8.1f} ms")
    except KeyboardInterrupt:
        _set(ws, exp, ExperimentState.CANCELLED)
        raise
    _set(ws, exp, ExperimentState.EVALUATING)
    touched = revisions.changed_paths(ws, base, cand)
    hits = revisions.protected_violations(touched, bundle.policy.protected_paths)
    review = revisions.protected_violations(touched, bundle.policy.require_review_for) if bundle.policy.require_review_for else []
    views = [VersionView(label, runs, {r.id: load_outcome(ws, r) for r in runs}, bundle.scenarios) for label, runs in (("baseline", base_runs), ("candidate", cand_runs))]
    j = judge(bundle, bundle.policy, views[0], views[1], touched, hits, review)
    ev = Evaluation(id=new_id("eval"), experiment_id=exp.id, evaluator=EVALUATOR, evaluator_version=EVALUATOR_VERSION, policy_digest=exp.policy_digest,
                    checks=j.checks, baseline_runs=[r.id for r in base_runs], candidate_runs=[r.id for r in cand_runs])
    ws.ledger.put(ev, actor=EVALUATOR, event="evaluation.completed")
    dec = Decision(id=new_id("dec"), experiment_id=exp.id, outcome=j.outcome, reasons=j.reasons, evaluation_id=ev.id)
    ws.ledger.put(dec, actor=EVALUATOR, event=f"decision.{j.outcome.value}")
    _set(ws, exp, ExperimentState.COMPLETED, outcome=j.outcome.value)
    return Result(exp, dec, ev)


def decision_for(ws: Workspace, exp_id: str) -> Decision | None:
    for d in ws.ledger.list("decision", Decision, limit=10000):
        if d.experiment_id == exp_id:
            return d
    return None


def evaluation_for(ws: Workspace, exp_id: str) -> Evaluation | None:
    for e in ws.ledger.list("evaluation", Evaluation, limit=10000):
        if e.experiment_id == exp_id:
            return e
    return None


def history(ws: Workspace) -> list[dict[str, Any]]:
    out = []
    for exp in ws.ledger.list("experiment", Experiment, limit=1000):
        d = decision_for(ws, exp.id)
        hyp = None
        if exp.hypothesis_id:
            try:
                hyp = ws.ledger.get(exp.hypothesis_id, Hypothesis).mechanism
            except KeyError:
                hyp = None
        out.append({"id": exp.id, "name": exp.name, "hypothesis": hyp, "state": state(ws, exp.id).value,
                    "outcome": d.outcome.value if d else None, "reasons": d.reasons if d else [], "created_at": exp.created_at,
                    "baseline": ws.ledger.get(exp.baseline_revision, Revision).label, "candidate": ws.ledger.get(exp.candidate_revision, Revision).label})
    return out


__all__ = ["create", "run", "validate", "history", "decision_for", "evaluation_for", "state", "Outcome"]
