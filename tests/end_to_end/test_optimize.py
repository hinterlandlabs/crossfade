"""The improvement loop end to end: an agent that learns from earlier attempts, a rejected change that is pruned,
an accepted one that becomes the new base, and a loop that stops when the agent has nothing left to try."""

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from crossfade import agents, revisions
from crossfade.models import Finding, Outcome
from crossfade.optimize import _fast_forward, optimize

CANDIDATES = Path(__file__).resolve().parents[2] / "examples" / "order-service" / "candidates"

# A scripted coding agent. It reads the context Crossfade gives it: with no history it tries the fast-but-wrong change;
# once that has been rejected it tries the batching change; once something has been accepted it has no more ideas.
AGENT = f"""
import json, os, shutil
ctx = json.load(open(os.environ["CROSSFADE_CONTEXT"]))
assert ctx["traces"], "the agent should see what the base revision does"
seen = {{a["outcome"] for a in ctx["previous_attempts"]}}
if "accepted" in seen:
    raise SystemExit(0)
src = "batch-lookups" if "rejected" in seen else "skip-reservation"
shutil.copy(os.path.join({str(CANDIDATES)!r}, src, "app.py"), "app.py")
"""


def test_optimize_keeps_only_what_holds_up(example_ws):
    ws = example_ws("order-service")
    log: list[str] = []
    res = optimize(ws, "main", shlex.join([sys.executable, "-c", AGENT]), rounds=6, patience=2, into="main", progress=log.append)
    assert [r.outcome for r in res.rounds] == ["rejected", "accepted", "no_change", "no_change"], [(r.outcome, r.detail) for r in res.rounds]
    assert res.kept == 1 and res.best.commit == res.rounds[1].candidate != res.start.commit
    assert "2 rounds in a row" in res.stopped
    # the rejected change was pruned; the accepted one became the base
    assert "every_confirmed_order_is_reserved" in res.rounds[0].detail
    assert res.rounds[1].experiment and res.rounds[2].candidate is None
    events = [e.type for e in ws.ledger.events(subject=res.id)]
    assert events[0] == "optimize.started" and events.count("optimize.round") == 4 and events[-2:] == ["optimize.finished", "optimize.applied"]
    # main now carries the kept change, and the checkout is on it
    git = lambda *a: subprocess.run(["git", *a], cwd=ws.root, capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    assert res.applied.startswith("fast-forwarded main") and git("rev-parse", "main") == git("rev-parse", "HEAD") == res.best.commit
    # the loop looked at the start and at the kept change; batching shows up as fewer dependency calls per request
    found = ws.ledger.list("finding", Finding, limit=1000)
    per_request = {f.revisions[0] for f in found if "per request" in f.description}
    assert res.start.id in per_request and res.best.id not in per_request
    ctx = json.loads((agents.export_context(ws, ws.root / ".ctx", None, res.best.id) / "context.json").read_text())
    assert ctx["traces"] and not any("per request" in f["description"] for f in ctx["findings"])
    # "main" now means the kept commit
    assert revisions.resolve(ws, "main").commit == res.best.commit
    assert ws.ledger.verify_chain()[1] == []


def test_agent_context_lists_previous_attempts(example_ws, tmp_path):
    ws = example_ws("order-service")
    from crossfade import experiments

    experiments.run(ws, experiments.create(ws, "skip", "main", "skip-reservation"))
    ctx = json.loads((agents.export_context(ws, tmp_path / "ctx", None) / "context.json").read_text())
    assert ctx["previous_attempts"] == [{"name": "skip", "outcome": Outcome.REJECTED.value,
                                         "reasons": ctx["previous_attempts"][0]["reasons"], "hypothesis": None}]
    assert not any("last-units" in r for r in ctx["previous_attempts"][0]["reasons"])  # holdout details stay hidden


def test_into_never_moves_a_branch_that_moved(tmp_path):
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "start")
    start = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "best")
    best = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "other", start)
    git("commit", "-q", "--allow-empty", "-m", "someone else")
    ws = SimpleNamespace(root=tmp_path)
    assert "moved" in _fast_forward(ws, "other", start, best)  # type: ignore[arg-type]
    assert _fast_forward(ws, "fresh", start, best).startswith("fast-forwarded") and git("rev-parse", "fresh") == best  # type: ignore[arg-type]
    (tmp_path / "f").write_text("x")
    git("add", "f")
    git("checkout", "-q", "-b", "dirty", start)
    assert "uncommitted" in _fast_forward(ws, "dirty", start, best)  # type: ignore[arg-type]
