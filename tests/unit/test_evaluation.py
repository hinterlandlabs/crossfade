from types import SimpleNamespace

from crossfade.evaluation import VersionView, bootstrap_improvement, judge
from crossfade.models import Expectation, Outcome, Policy, Run, RunStatus, Scenario, Step


def runs(label, values, status=RunStatus.COMPLETED, scenario="s"):
    out = []
    for i, v in enumerate(values):
        out.append(Run(id=f"{label}{i}", project_id="p", revision_id=label, scenario=scenario, scenario_digest="d", world="w", world_snapshot_digest="d",
                       seed=0, trial=i, status=status, started_at=0, ended_at=1, metrics={"latency_ms": v}))
    return out


SC = {"s": Scenario(name="s", steps=[Step()], expect=[Expectation(target="responses[0].status", value=200)])}


def view(label, values, ok=True, status=RunStatus.COMPLETED):
    rs = runs(label, values, status)
    outcome = {"responses": [{"step": 0, "status": 200 if ok else 500}], "world": {}, "world_violations": []}
    return VersionView(label, rs, {r.id: outcome for r in rs}, SC)


def bundle(gates=()):
    checks = SimpleNamespace(always_true=lambda v: (True, ""), always_false=lambda v: (False, "nope"))
    return SimpleNamespace(policy=Policy(gates=list(gates), min_trials=5), checks=checks)


POLICY = Policy(objectives=[{"metric": "latency_ms", "direction": "lower", "min_improvement": 0.1}], min_trials=5, max_cv=0.3)


def test_faster_and_correct_is_accepted():
    j = judge(bundle(), POLICY, view("b", [100, 102, 98, 101, 99]), view("c", [70, 71, 69, 70, 72]), ["app.py"], [], [])
    assert j.outcome == Outcome.ACCEPTED


def test_faster_but_wrong_is_rejected():
    j = judge(bundle(), POLICY, view("b", [100] * 5), view("c", [10] * 5, ok=False), ["app.py"], [], [])
    assert j.outcome == Outcome.REJECTED
    assert any("scenario_expectations" in r for r in j.reasons)


def test_failed_gate_rejects_even_with_huge_improvement():
    j = judge(bundle(["always_false"]), POLICY, view("b", [100] * 5), view("c", [1] * 5), ["app.py"], [], [])
    assert j.outcome == Outcome.REJECTED


def test_touching_protected_paths_rejects_immediately():
    j = judge(bundle(), POLICY, view("b", [100] * 5), view("c", [50] * 5), ["crossfade/checks.py"], ["crossfade/checks.py"], [])
    assert j.outcome == Outcome.REJECTED and "protected" in j.reasons[0]


def test_noisy_results_are_inconclusive():
    j = judge(bundle(), POLICY, view("b", [100, 30, 180, 60, 140]), view("c", [80, 20, 170, 40, 100]), ["app.py"], [], [])
    assert j.outcome == Outcome.INCONCLUSIVE


def test_small_improvement_is_inconclusive():
    j = judge(bundle(), POLICY, view("b", [100, 101, 99, 100, 100]), view("c", [97, 98, 96, 97, 97]), ["app.py"], [], [])
    assert j.outcome == Outcome.INCONCLUSIVE


def test_infrastructure_failures_are_not_blamed_on_the_app():
    j = judge(bundle(), POLICY, view("b", [100] * 5), view("c", [100] * 5, status=RunStatus.INFRA_FAILED), ["app.py"], [], [])
    assert j.outcome == Outcome.INCONCLUSIVE


def test_regression_is_rejected():
    j = judge(bundle(), POLICY, view("b", [100, 101, 99, 100, 100]), view("c", [150, 151, 149, 150, 150]), ["app.py"], [], [])
    assert j.outcome == Outcome.REJECTED


def test_review_paths_need_a_person():
    j = judge(bundle(), POLICY, view("b", [100] * 5), view("c", [50] * 5), ["billing/x.py"], [], ["billing/x.py"])
    assert j.outcome == Outcome.NEEDS_REVIEW


def test_bootstrap_interval_contains_point_estimate():
    point, lo, hi = bootstrap_improvement([100, 102, 98], [80, 81, 79], "lower", 0.9)
    assert lo <= point <= hi and point > 0.15


def _two_scenario_view(label, values, drop=None, crash=None):
    """Five trials x two scenarios. `drop` removes the metric from one run; `crash` marks one run APP_FAILED."""
    sc = {"s": SC["s"], "t": Scenario(name="t", steps=[Step()])}
    rs = []
    for trial, v in enumerate(values):
        for name in ("s", "t"):
            r = Run(id=f"{label}{trial}{name}", project_id="p", revision_id=label, scenario=name, scenario_digest="d", world="w",
                    world_snapshot_digest="d", seed=0, trial=trial, status=RunStatus.COMPLETED, started_at=0, ended_at=1, metrics={"latency_ms": v})
            if drop == (trial, name):
                r.metrics = {}
            if crash == (trial, name):
                r.status, r.error = RunStatus.APP_FAILED, "exit 1"
            rs.append(r)
    outcome = {"responses": [{"step": 0, "status": 200}], "world": {}, "world_violations": []}
    return VersionView(label, rs, {r.id: outcome for r in rs}, sc)


def test_missing_candidate_metric_cannot_fake_an_improvement():
    # audit 2026-10-30 finding 1: the candidate lacks latency for one scenario in one trial; its total must not shrink
    base = _two_scenario_view("b", [100] * 5)
    cand = _two_scenario_view("c", [100] * 5, drop=(2, "t"))
    j = judge(bundle(), POLICY, base, cand, ["app.py"], [], [])
    assert j.outcome == Outcome.INCONCLUSIVE and "incomplete" in j.reasons[0]


def test_failed_baseline_blocks_automatic_acceptance():
    # audit 2026-10-30 finding 2: one baseline crash, a candidate twice as fast; that is not a valid comparison
    base = _two_scenario_view("b", [100] * 5, crash=(1, "s"))
    cand = _two_scenario_view("c", [50] * 5)
    j = judge(bundle(), POLICY, base, cand, ["app.py"], [], [])
    assert j.outcome == Outcome.INCONCLUSIVE and "baseline" in j.reasons[0]
    assert any(c.name == "baseline:runs_completed" and c.passed is False for c in j.checks)


def test_complete_paired_measurements_still_accept():
    j = judge(bundle(), POLICY, _two_scenario_view("b", [100, 101, 99, 100, 102]), _two_scenario_view("c", [60, 61, 59, 60, 62]), ["app.py"], [], [])
    assert j.outcome == Outcome.ACCEPTED
