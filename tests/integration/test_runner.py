from crossfade import revisions, telemetry
from crossfade.models import RunStatus
from crossfade.project import WorkspaceError
from crossfade.runner import run_scenario


def test_runner_executes_real_code_and_captures_evidence(example_ws):
    ws = example_ws("order-service")
    b = ws.bundle()
    rev = revisions.resolve(ws, "main")
    res = run_scenario(ws, b, rev, b.scenarios["checkout"])
    assert res.run.status == RunStatus.COMPLETED, res.run.error
    assert [r["status"] for r in res.outcome["responses"]] == [201, 201, 201, 201, 409, 201, 201, 422]
    assert res.outcome["world"]["products"]["sku-1"]["stock"] == 46
    assert res.run.metrics["dependency_calls"] > 0 and res.run.metrics["latency_ms"] > 0
    app_spans = [s for s in res.spans if s.source == "otel"]
    assert any(s.name == "create_order" for s in app_spans)
    assert any(s.parent_id for s in app_spans)  # nested operations are captured
    assert ws.ledger.spans(res.run.id)
    assert ws.artifacts.exists(res.run.outcome_artifact)


def test_worktrees_isolate_revisions(example_ws):
    ws = example_ws("order-service")
    a = revisions.worktree(ws, revisions.resolve(ws, "main"))
    b = revisions.worktree(ws, revisions.resolve(ws, "batch-lookups"))
    assert a != b and "fetch_products" in (b / "app.py").read_text() and "fetch_products" not in (a / "app.py").read_text()


def test_setup_failure_is_infra_not_app(example_ws, tmp_path):
    ws = example_ws("order-service")
    b = ws.bundle()
    b.config.app.start = "definitely-not-a-command"
    res = run_scenario(ws, b, revisions.resolve(ws, "main"), b.scenarios["checkout"])
    assert res.run.status == RunStatus.INFRA_FAILED


def test_unknown_ref_is_rejected(example_ws):
    ws = example_ws("order-service")
    try:
        revisions.resolve(ws, "no-such-branch")
    except WorkspaceError:
        return
    raise AssertionError("expected WorkspaceError")


def test_dirty_working_tree_is_captured_without_touching_it(example_ws):
    ws = example_ws("order-service")
    app = ws.root / "app.py"
    app.write_text(app.read_text() + "\n# local edit\n")
    rev = revisions.resolve(ws, "HEAD", capture_dirty=True)
    assert rev.dirty and rev.patch_artifact and b"local edit" in ws.artifacts.get_bytes(rev.patch_artifact)
    assert "# local edit" in app.read_text()


def test_app_limits_apply_without_python_in_the_forked_child():
    """Limits come from a shell wrapper, so starting an app while the parent has threads can't deadlock the fork."""
    import subprocess
    import sys
    import threading

    from crossfade.runner import _limited

    stop = threading.Event()
    busy = threading.Thread(target=stop.wait, daemon=True)
    busy.start()  # like the world server: the parent has another thread when the app starts
    try:
        probe = "import resource as r; print(r.getrlimit(r.RLIMIT_CPU)[0], r.getrlimit(r.RLIMIT_NOFILE)[0])"
        out = subprocess.run(_limited(7, [sys.executable, "-c", probe]), capture_output=True, text=True, timeout=30, start_new_session=True)
    finally:
        stop.set()
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["7", "512"]


def test_a_stuck_app_reports_where_it_hung(example_ws):
    from crossfade.project import git

    ws = example_ws("order-service")
    (ws.root / "stuck.py").write_text("import time\n\ndef wait_forever():\n    time.sleep(3600)\n\nwait_forever()\n")
    git(ws.root, "add", "stuck.py")
    git(ws.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "an app that never starts")
    b = ws.bundle()
    b.config.app.start, b.config.app.startup_timeout = "python stuck.py", 2
    res = run_scenario(ws, b, revisions.resolve(ws, "HEAD"), b.scenarios["checkout"])
    assert res.run.status == RunStatus.INFRA_FAILED
    assert "did not become healthy" in res.run.error and "wait_forever" in res.run.error  # the stack says where


def test_every_dependency_call_is_traced_to_the_code_that_made_it(example_ws):
    for name, scenario in (("order-service", "checkout"), ("support-agent", None)):
        ws = example_ws(name)
        b = ws.bundle()
        sc = b.scenarios[scenario] if scenario else next(s for s in b.scenarios.values() if not s.holdout)
        res = run_scenario(ws, b, revisions.resolve(ws, "main"), sc)
        assert res.run.status == RunStatus.COMPLETED, res.run.error
        cov = telemetry.coverage(res.spans)
        assert cov["dependency_spans"] > 0 and cov["dependency_spans_linked"] == cov["dependency_spans"], (name, cov)
        by_id = {s.span_id: s for s in res.spans}
        parents = {by_id[s.parent_id].name for s in res.spans if s.source == "world"}
        if name == "support-agent":  # the model call sits under the chat span, the order lookup under the tool span
            assert any(p.startswith("chat") for p in parents) and any(p.startswith("execute_tool") or p == "invoke_agent support" for p in parents), parents
