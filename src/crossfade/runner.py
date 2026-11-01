"""The local runner: executes a real revision of the application against a world, for one scenario.

Lifecycle: prepare (worktree, world, temp dir) -> start app -> health check -> invoke steps -> collect
(responses, world state, meters, traces, resource usage) -> stop -> cleanup.

Isolation, honestly stated: this runner uses separate processes, a private temp directory, CPU/file limits,
a scrubbed environment and a dead-end HTTP proxy. It is not a security boundary against hostile code; use a
container or VM runner for untrusted candidates (see docs/architecture/security.md). Setup failures are
recorded as INFRA_FAILED and never counted against the application.
"""

from __future__ import annotations

import json
import os
import resource
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import telemetry
from .models import Revision, Run, RunStatus, Scenario, Span, new_id, now
from .project import Bundle, Workspace
from .revisions import worktree
from .worlds.base import FailureRule, WorldServer


@dataclass
class RunResult:
    run: Run
    outcome: dict[str, Any]
    spans: list[Span] = field(default_factory=list)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _limited(cpu_seconds: int, cmd: list[str]) -> list[str]:
    """Apply CPU and open-file limits with a shell wrapper that execs the app.

    The limits are set after the fork by `sh`, not by a Python preexec_fn: running Python in a forked child while the
    parent has threads (the world server is one) can deadlock, which on macOS left apps alive but never started.
    """
    return ["/bin/sh", "-c", 'ulimit -t "$0" 2>/dev/null; ulimit -n 512 2>/dev/null; exec "$@"', str(cpu_seconds), *cmd]


def _cmd(start: str) -> list[str]:
    parts = shlex.split(start)
    if parts and parts[0] in ("python", "python3"):
        return [sys.executable, "-m", "crossfade.instrument", *parts[1:]]
    return parts


def lookup(obj: Any, path: str) -> Any:
    """Resolve "responses[2].body.order_id" or "world.products.sku-1.stock" against an outcome dict."""
    cur = obj
    for tok in path.replace("]", "").replace("[", ".").split("."):
        if tok == "":
            continue
        if isinstance(cur, list):
            cur = cur[int(tok)]
        elif isinstance(cur, dict):
            if tok not in cur:
                raise KeyError(path)
            cur = cur[tok]
        else:
            raise KeyError(path)
    return cur


def run_scenario(ws: Workspace, bundle: Bundle, rev: Revision, scenario: Scenario, trial: int = 0, seed: int | None = None,
                 experiment_id: str | None = None) -> RunResult:
    cfg, policy = bundle.config, bundle.policy
    seed = scenario.seed + trial if seed is None else seed
    run_id = new_id("run")
    started = now()
    tmp = Path(tempfile.mkdtemp(prefix="crossfade-run-"))
    trace_file = tmp / "trace.jsonl"
    log_file = tmp / "app.log"
    responses: list[dict[str, Any]] = []
    status, error = RunStatus.COMPLETED, None
    proc: subprocess.Popen[bytes] | None = None
    world = bundle.world_cls(fixture=scenario.world_snapshot or cfg.world.fixture, seed=seed,
                             failures=[FailureRule(**f) for f in cfg.world.failures], latency_scale=cfg.world.latency_scale)
    snapshot_digest = world.digest(world.snapshot())
    usage0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t_steps = 0.0
    try:
        wt = worktree(ws, rev)
    except Exception as e:  # noqa: BLE001
        return _record(ws, bundle, rev, scenario, run_id, started, seed, trial, RunStatus.INFRA_FAILED, f"worktree: {e}", snapshot_digest,
                       {}, [], {}, None, tmp, experiment_id)
    with WorldServer(world) as srv:
        port = _free_port()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp), "TMPDIR": str(tmp), "LANG": "C.UTF-8",
            "PORT": str(port), "WORLD_URL": srv.url, "APP_DATA_DIR": str(tmp / "data"),
            "CROSSFADE_TRACE_FILE": str(trace_file), "CROSSFADE_RUN_ID": run_id, "OTEL_SERVICE_NAME": cfg.name,
            "PYTHONHASHSEED": str(seed), "PYTHONDONTWRITEBYTECODE": "1",
            **({} if policy.allow_network else {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                                                 "NO_PROXY": "127.0.0.1,localhost"}),
            **cfg.app.env,
        }
        (tmp / "data").mkdir()
        try:
            with open(log_file, "wb") as log:
                cmd = _cmd(cfg.app.start)
                exe = cmd[0] if "/" in cmd[0] else shutil.which(cmd[0], path=env["PATH"])
                if not exe or not os.access(Path(wt) / exe if not os.path.isabs(exe) else exe, os.X_OK):
                    raise FileNotFoundError(f"command not found: {cmd[0]}")  # a setup problem, not the app's fault
                proc = subprocess.Popen(_limited(int(policy.timeout_seconds), cmd), cwd=wt, env=env, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            base = f"http://127.0.0.1:{port}"
            deadline = time.time() + cfg.app.startup_timeout
            with httpx.Client(timeout=policy.timeout_seconds, trust_env=False) as client:
                while True:
                    if proc.poll() is not None:
                        raise RuntimeError(f"app exited during startup with code {proc.returncode}")
                    try:
                        if client.get(base + cfg.app.health, timeout=1).status_code < 500:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.time() > deadline:
                        try:  # ask a stuck app for its stacks (crossfade.instrument registers SIGUSR1) before giving up
                            os.kill(proc.pid, signal.SIGUSR1)
                            time.sleep(0.5)
                        except ProcessLookupError:
                            pass
                        tail = log_file.read_text(errors="replace")[-2000:] if log_file.exists() else ""
                        raise TimeoutError(f"app did not become healthy within {cfg.app.startup_timeout}s; log tail: {tail!r}")
                    time.sleep(0.05)
                run_deadline = time.time() + policy.timeout_seconds
                for i, step in enumerate(scenario.steps):
                    if time.time() > run_deadline:
                        raise TimeoutError(f"scenario exceeded {policy.timeout_seconds}s")
                    if step.kind == "clock":
                        world.advance(step.advance_seconds)
                        responses.append({"step": i, "kind": "clock", "clock": world.clock})
                        continue
                    if step.kind == "conversation":
                        method, path, body = "POST", step.path if step.path != "/" else "/chat", {"message": step.message, **(step.body or {})}
                    else:
                        method, path, body = step.method, step.path, step.body
                    t0 = time.perf_counter()
                    try:
                        r = client.request(method, base + path, json=body)
                        elapsed = (time.perf_counter() - t0) * 1000
                        try:
                            rbody = r.json()
                        except ValueError:
                            rbody = r.text
                        responses.append({"step": i, "status": r.status_code, "body": rbody, "ms": round(elapsed, 3)})
                    except httpx.HTTPError as e:
                        elapsed = (time.perf_counter() - t0) * 1000
                        responses.append({"step": i, "status": 0, "error": type(e).__name__, "ms": round(elapsed, 3)})
                    t_steps += elapsed
                    if proc.poll() is not None:
                        status, error = RunStatus.APP_FAILED, f"app exited with code {proc.returncode} during step {i}"
                        break
        except (RuntimeError, TimeoutError) as e:
            status = RunStatus.APP_FAILED if proc and proc.poll() is not None else RunStatus.INFRA_FAILED
            if isinstance(e, TimeoutError) and "scenario" in str(e):
                status = RunStatus.APP_FAILED
            error = str(e)
        except OSError as e:
            status, error = RunStatus.INFRA_FAILED, f"could not start app: {e}"
        finally:
            if proc and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        usage1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        world_state = world.snapshot()
        meters = dict(world.meters)
        spans = telemetry.from_jsonl(trace_file) + world.spans(run_id)
        for payload in srv.otlp_payloads:
            if isinstance(payload, dict):
                spans += telemetry.from_otlp_json(payload)
        world_violations = world.invariants()
    log_tail = log_file.read_text(errors="replace")[-4000:] if log_file.exists() else ""
    metrics = {
        "latency_ms": round(t_steps, 3),
        "cpu_ms": round(((usage1.ru_utime - usage0.ru_utime) + (usage1.ru_stime - usage0.ru_stime)) * 1000, 3),
        "dependency_calls": meters.get("calls_total", 0),
        "unsupported_calls": meters.get("calls_unsupported", 0),
        "errors": sum(1 for r in responses if r.get("status", 200) >= 500 or r.get("status") == 0),
        **{k: v for k, v in meters.items() if k.startswith("model_") or k.startswith("tool_")},
    }
    outcome = {"responses": responses, "world": world_state["state"], "clock": world_state["clock"], "meters": meters,
               "world_violations": world_violations, "log_tail": log_tail}
    return _record(ws, bundle, rev, scenario, run_id, started, seed, trial, status, error, snapshot_digest, outcome, spans, metrics,
                   log_file, tmp, experiment_id)


def _record(ws: Workspace, bundle: Bundle, rev: Revision, scenario: Scenario, run_id: str, started: float, seed: int, trial: int,
            status: RunStatus, error: str | None, snapshot_digest: str, outcome: dict[str, Any], spans: list[Span], metrics: dict[str, float],
            log_file: Path | None, tmp: Path, experiment_id: str | None) -> RunResult:
    outcome_digest = ws.artifacts.put_json(outcome) if outcome else None
    trace_digest = ws.artifacts.put_bytes("\n".join(s.model_dump_json() for s in spans).encode()) if spans else None
    log_digest = ws.artifacts.put_bytes(log_file.read_bytes()) if log_file and log_file.exists() else None
    run = Run(id=run_id, project_id=ws.project.id, revision_id=rev.id, scenario=scenario.name, scenario_digest=bundle.scenario_digest(scenario.name),
              world=f"{bundle.world_cls.name}@{bundle.world_cls.version}", world_snapshot_digest=snapshot_digest, seed=seed, trial=trial,
              status=status, started_at=started, ended_at=now(), error=error, outcome_artifact=outcome_digest, trace_artifact=trace_digest,
              metrics=metrics, log_artifact=log_digest)
    ws.ledger.put(run, actor="crossfade.runner", event=f"run.{status.value}")
    if experiment_id:
        ws.ledger.append("run.attached", actor="crossfade.runner", project_id=ws.project.id, subject=experiment_id, run_id=run_id)
    if spans:
        ws.ledger.index_spans(run_id, spans)
    shutil.rmtree(tmp, ignore_errors=True)
    return RunResult(run, outcome, spans)


def load_outcome(ws: Workspace, run: Run) -> dict[str, Any]:
    return ws.artifacts.get_json(run.outcome_artifact) if run.outcome_artifact else {}


def load_spans(ws: Workspace, run: Run) -> list[Span]:
    if not run.trace_artifact:
        return []
    return [Span.model_validate_json(line) for line in ws.artifacts.get_bytes(run.trace_artifact).decode().splitlines() if line]


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)
