"""Trust-boundary regressions from the 2026-10-30 implementation audit. Each test tries to cross a boundary."""

import json
import os
import shlex
import sys

import pytest
from fastapi.testclient import TestClient

from crossfade import agents, revisions
from crossfade.api import create_app
from crossfade.project import WorkspaceError

PROBE = """
import json, os, subprocess
from pathlib import Path
root = Path({root!r})
def can(f):
    try:
        f(); return True
    except Exception:
        return False
facts = {{
    "bundle_in_stage": Path("crossfade").exists(),
    "bundle_in_history": subprocess.run(["git", "log", "--all", "--name-only", "--format="], capture_output=True, text=True).stdout.count("crossfade/") > 0,
    "real_repo_readable": can(lambda: (root / "crossfade" / "checks.py").read_text()),
    "ledger_readable": can(lambda: (root / ".crossfade" / "ledger.db").read_bytes()),
    "external_write": can(lambda: Path({marker!r}).write_text("x")),
    "env_marker": os.environ.get("HLL_PROBE_MARKER"),
    "passed_env": os.environ.get("HLL_PASSED"),
}}
Path("probe.json").write_text(json.dumps(facts))
"""


def _probe(ws, tmp_path, **kw):
    marker = tmp_path / "outside.txt"
    code = PROBE.format(root=str(ws.root), marker=str(marker))
    os.environ["HLL_PROBE_MARKER"] = "inherited"
    os.environ["HLL_PASSED"] = "allowed"
    try:
        rev, cand = agents.propose(ws, "main", shlex.join([sys.executable, "-c", code]), name="probe", **kw)
    finally:
        del os.environ["HLL_PROBE_MARKER"], os.environ["HLL_PASSED"]
    facts = json.loads(revisions.git(ws.root, "show", f"{rev.commit}:probe.json"))
    return facts, marker, cand


def test_agent_cannot_see_bundle_or_inherit_environment(example_ws, tmp_path):
    ws = example_ws("order-service")
    facts, marker, cand = _probe(ws, tmp_path, pass_env=["HLL_PASSED"])
    assert not facts["bundle_in_stage"], "the staged copy must not contain the evaluation bundle"
    assert not facts["bundle_in_history"], "the bundle must not be recoverable from the agent's git history"
    assert facts["env_marker"] is None, "the agent must not inherit arbitrary environment variables"
    assert facts["passed_env"] == "allowed"
    assert cand.touched_paths == ["probe.json"]


@pytest.mark.skipif(agents.sandbox_kind() is None, reason="no OS sandbox on this machine")
def test_sandboxed_agent_cannot_read_repo_or_write_outside(example_ws, tmp_path):
    ws = example_ws("order-service")
    facts, marker, _ = _probe(ws, tmp_path)
    assert not facts["real_repo_readable"] and not facts["ledger_readable"]
    assert not facts["external_write"] and not marker.exists()


def test_agent_writing_protected_paths_is_refused(example_ws):
    ws = example_ws("order-service")
    cheat = f"{sys.executable} -c \"import pathlib; p=pathlib.Path('crossfade'); p.mkdir(); (p/'checks.py').write_text('def x(v): return True')\""
    with pytest.raises(WorkspaceError, match="protected"):
        agents.propose(ws, "main", cheat, name="cheat")
    assert any(e.type == "agent.protected_write" for e in ws.ledger.events(limit=10000))


def test_empty_capabilities_mean_no_capabilities(example_ws):
    ws = example_ws("order-service")
    with pytest.raises(PermissionError):
        agents.propose(ws, "main", "true", capabilities=set())


def test_reused_worktrees_start_clean(example_ws):
    ws = example_ws("order-service")
    rev = revisions.resolve(ws, "main")
    wt = revisions.worktree(ws, rev)
    (wt / "leftover.txt").write_text("from a previous trial")
    (wt / "__pycache__").mkdir(exist_ok=True)
    (wt / "__pycache__" / "ignored.txt").write_text("ignored residue")
    (wt / "app.py").write_text("tampered")
    again = revisions.worktree(ws, rev)
    assert again == wt
    assert not (wt / "leftover.txt").exists() and not (wt / "__pycache__" / "ignored.txt").exists()
    assert "def create_order" in (wt / "app.py").read_text()


def test_api_never_serves_the_locked_bundle(example_ws):
    ws = example_ws("order-service")
    digest = ws.current_bundle_digest()
    c = TestClient(create_app(ws))
    r = c.get(f"/v1/artifacts/{digest}")
    assert r.status_code == 404
    assert "last-units" not in r.text and "min_improvement" not in r.text
