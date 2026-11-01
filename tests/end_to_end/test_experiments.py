"""The MVP's definition of done, as tests: real execution, a valid optimization accepted, a faster-but-wrong one rejected,
for two unrelated applications, with history, integrity and reproducible evaluation."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from crossfade import agents, experiments
from crossfade.api import create_app
from crossfade.models import Outcome, Revision, Run


def test_order_service(example_ws):
    ws = example_ws("order-service")
    good = experiments.run(ws, experiments.create(ws, "batch", "main", "batch-lookups"))
    assert good.decision.outcome == Outcome.ACCEPTED, (good.decision.reasons, [c.detail for c in good.evaluation.checks if c.name.startswith("objective")])
    lat = next(c for c in good.evaluation.checks if c.name == "objective:latency_ms")
    assert lat.candidate < lat.baseline
    bad = experiments.run(ws, experiments.create(ws, "skip", "main", "skip-reservation"))
    assert bad.decision.outcome == Outcome.REJECTED
    assert any("every_confirmed_order_is_reserved" in r for r in bad.decision.reasons)
    bad_lat = next(c for c in bad.evaluation.checks if c.name == "objective:latency_ms")
    assert bad_lat.candidate < bad_lat.baseline  # it really was faster, and it was still rejected
    # holdout details never appear in feedback
    assert not any("last-units" in c.detail for c in bad.evaluation.checks)
    # repeated trials are recorded per run
    assert len(good.evaluation.baseline_runs) == ws.bundle().policy.min_trials * len(ws.bundle().scenarios)
    h = experiments.history(ws)
    assert {e["outcome"] for e in h} == {"accepted", "rejected"}
    n, bad_events = ws.ledger.verify_chain()
    assert n > 50 and bad_events == []
    assert ws.artifacts.verify_all()[1] == []


def test_support_agent_harness(example_ws):
    ws = example_ws("support-agent")
    good = experiments.run(ws, experiments.create(ws, "prefetch", "main", "drop-prefetch"))
    assert good.decision.outcome == Outcome.ACCEPTED, good.decision.reasons
    bad = experiments.run(ws, experiments.create(ws, "trim", "main", "trim-prompt"))
    assert bad.decision.outcome == Outcome.REJECTED
    cost = next(c for c in bad.evaluation.checks if c.name == "objective:model_cost_usd")
    assert cost.candidate < cost.baseline * 0.85  # much cheaper
    assert any("no_refund_outside_return_window" in r for r in bad.decision.reasons)
    run = ws.ledger.get(bad.evaluation.candidate_runs[0], Run)
    assert run.metrics["model_input_tokens"] > 0  # cost is metered by the world, not reported by the app


def test_agent_candidates_and_protected_paths(example_ws, tmp_path):
    ws = example_ws("order-service")
    src = Path(__file__).resolve().parents[2] / "examples" / "order-service" / "candidates" / "batch-lookups" / "app.py"
    rev, cand = agents.propose(ws, "main", f"{sys.executable} -c \"import shutil; shutil.copy('{src}', 'app.py')\"", name="agent-batch")
    assert cand.originator.startswith("agent:") and cand.touched_paths == ["app.py"]
    # a branch that edits the evaluation bundle (however it was made) is rejected outright, whatever its code does
    from crossfade.project import git

    git(ws.root, "checkout", "-q", "-b", "cheat", "batch-lookups")
    checks = ws.root / "crossfade" / "checks.py"
    checks.write_text(checks.read_text().replace("return not problems", "return True"))
    git(ws.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "loosen the gates")
    git(ws.root, "checkout", "-q", "main")
    res = experiments.run(ws, experiments.create(ws, "cheat", "main", "cheat", trials=5))
    assert res.decision.outcome == Outcome.REJECTED and "protected" in res.decision.reasons[0]


def test_api_serves_ledger_data_and_guards_writes(example_ws):
    ws = example_ws("order-service")
    experiments.run(ws, experiments.create(ws, "batch", "main", "batch-lookups"))
    c = TestClient(create_app(ws))
    exps = c.get("/v1/experiments").json()
    assert exps["total"] == 1 and exps["items"][0]["outcome"] == "accepted"
    detail = c.get(f"/v1/experiments/{exps['items'][0]['id']}").json()
    assert detail["patch"] and detail["evaluation"]["checks"]
    run_id = detail["runs"][0]["id"]
    assert c.get(f"/v1/runs/{run_id}/trace").json()["tree"]
    assert c.post("/v1/experiments", json={"baseline": "main", "candidate": "main"}).status_code == 401
    token = (ws.state / "api-token").read_text()
    assert c.post("/v1/hypotheses", json={"mechanism": "x"}, headers={"Authorization": f"Bearer {token}"}).status_code == 201
    scen = {s["name"]: s for s in c.get("/v1/scenarios").json()}
    assert scen["last-units"]["detail"] is None and scen["checkout"]["detail"]
    assert c.get("/v1/verify").json()["ok"]


def test_mcp_exposes_tools_without_holdouts(example_ws):
    import asyncio

    from crossfade.mcp_server import build

    ws = example_ws("order-service")
    server = build(ws)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"inspect_project", "list_scenarios", "request_experiment", "get_trace"} <= names
    res = asyncio.run(server.call_tool("list_scenarios", {}))
    assert "last-units" not in str(res) and "checkout" in str(res)


def test_behaviour_diff_and_export_tie_traces_to_the_decision(example_ws, tmp_path):
    import json

    from typer.testing import CliRunner

    from crossfade import export, telemetry
    from crossfade.cli import app

    ws = example_ws("order-service")
    res = experiments.run(ws, experiments.create(ws, "batch", "main", "batch-lookups", trials=1))
    d = export.behaviour_diff(ws, res.experiment.id)
    added = [r["path"] for r in d["rows"] if r["change"] == "added"]
    removed = [r["path"] for r in d["rows"] if r["change"] == "removed"]
    assert any(p.endswith("supplier GET /products") for p in added), added  # the batch call appears
    assert any("/products/{sku}" in p for p in removed), removed  # the per-item lookups are gone
    out = tmp_path / "otlp.json"
    r = CliRunner().invoke(app, ["traces", "export", "--experiment", res.experiment.id, "--out", str(out), "--json"])
    assert r.exit_code == 0, r.output
    summary = json.loads(r.output)
    holdout_runs = [i for i in summary["skipped"] if ws.bundle().scenarios[ws.ledger.get(i, Run).scenario].holdout]
    assert holdout_runs and len(holdout_runs) == len(summary["skipped"])  # holdouts stay home by default
    payload = json.loads(out.read_text())
    resources = [{a["key"]: list(a["value"].values())[0] for a in rs["resource"]["attributes"]} for rs in payload["resourceSpans"]]
    assert {x["crossfade.decision"] for x in resources} == {res.decision.outcome.value}
    assert {x["crossfade.side"] for x in resources} == {"original", "change"}
    assert {x["vcs.ref.head.revision"] for x in resources} == {ws.ledger.get(res.experiment.baseline_revision, Revision).commit,
                                                              ws.ledger.get(res.experiment.candidate_revision, Revision).commit}
    assert len(telemetry.from_otlp_json(payload)) == summary["spans"]
    r = CliRunner().invoke(app, ["traces", "diff", "--experiment", res.experiment.id])
    assert r.exit_code == 0 and "added" in r.output, r.output
