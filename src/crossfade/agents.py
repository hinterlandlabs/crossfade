"""Coding-agent integration.

`propose` gives an external coding agent (Claude Code, Codex, or any command) a staged copy of the base revision with
the protected paths removed, plus a context file with findings, non-holdout scenario names and trace summaries. Where the
OS allows it, the agent runs in a sandbox that can't read the real repository or write outside its stage. Its changes
come back as a patch and are registered as a Candidate revision. The decision is made by the evaluator.

Agent capabilities are explicit. By default an agent may read the code and findings and create a candidate. It may
not edit the bundle, write decisions, change policy, or reach the network beyond what the operator allows.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from enum import StrEnum
from pathlib import Path

from . import experiments, revisions
from .models import Candidate, Finding, Hypothesis, Revision, Run, new_id
from .project import Workspace, WorkspaceError, git


class Capability(StrEnum):
    READ_CODE = "read_code"
    READ_TRACES = "read_traces"
    WRITE_SCENARIOS = "write_scenarios"
    WRITE_WORLDS = "write_worlds"
    CREATE_CANDIDATE = "create_candidate"
    RUN_EXPERIMENT = "run_experiment"
    REVIEW_EVALUATION = "review_evaluation"
    ADMIN_POLICY = "admin_policy"
    EXTERNAL_WRITES = "external_writes"


DEFAULT_AGENT_CAPABILITIES = {Capability.READ_CODE, Capability.READ_TRACES, Capability.CREATE_CANDIDATE, Capability.RUN_EXPERIMENT}


def require(caps: set[Capability], cap: Capability) -> None:
    if cap not in caps:
        raise PermissionError(f"agent lacks capability {cap.value}")


def export_context(ws: Workspace, dest: Path, hypothesis: Hypothesis | None, revision_id: str | None = None) -> Path:
    """What the agent may see. Holdout scenarios and the bundle's internals are excluded.

    With `revision_id`, findings and traces come from that revision when it has any, so an agent working on the current
    best revision reads what that revision does, not what an older one did.
    """
    bundle = ws.bundle()
    dest.mkdir(parents=True, exist_ok=True)
    found = ws.ledger.list("finding", Finding, limit=1000)
    runs = [r for r in ws.ledger.list("run", Run, limit=1000) if r.scenario in bundle.scenarios and not bundle.scenarios[r.scenario].holdout]
    if revision_id and any(r.revision_id == revision_id for r in runs):
        # this revision has been run: show only what it does, even if that's nothing (a fixed problem stays fixed)
        runs = [r for r in runs if r.revision_id == revision_id]
        found = [f for f in found if revision_id in f.revisions]
    findings = [f.model_dump(mode="json") for f in found[-200:]]
    trace_summary = []
    for r in runs[:10]:
        spans = [s for s in ws.ledger.spans(r.id) if s.source != "world"]
        trace_summary.append({"run": r.id, "scenario": r.scenario, "metrics": r.metrics,
                              "spans": [{"name": s.name, "ms": round(s.duration_ms, 2), "status": s.status} for s in spans[:200]]})
    (dest / "context.json").write_text(json.dumps({
        "project": ws.project.name,
        "hypothesis": hypothesis.model_dump(mode="json") if hypothesis else None,
        "findings": findings,
        "scenarios": [{"name": s.name, "description": s.description} for s in bundle.scenarios.values() if not s.holdout],
        "objectives": [o["metric"] for o in bundle.policy.objectives],
        "rules": ["Do not modify files under the protected paths: " + ", ".join(bundle.policy.protected_paths),
                  "Your change will be judged by checks you cannot see or edit. Correctness gates come before any speed or cost gain."],
        "traces": trace_summary,
        # earlier attempts and why they were decided that way, so an agent doesn't repeat a change that already failed
        "previous_attempts": [{"name": e["name"], "outcome": e["outcome"], "reasons": e["reasons"], "hypothesis": e["hypothesis"]}
                              for e in experiments.history(ws)[-20:] if e["outcome"]],
    }, indent=2, default=str))
    return dest


SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "SHELL", "TZ")


def sandbox_kind() -> str | None:
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if shutil.which("bwrap"):
        return "bwrap"
    return None


def _sandboxed(cmd: str, stage: Path, scratch: Path, ctx: Path, hidden: list[Path], writable: list[Path] | None = None) -> list[str]:
    """Wrap the agent command so it can write only to its stage and scratch dirs and can't read the real repository."""
    kind = sandbox_kind()
    stage, scratch, ctx = stage.resolve(), scratch.resolve(), ctx.resolve()
    if kind == "sandbox-exec":
        allowed = " ".join(f'(subpath "{p}")' for p in (stage, scratch, *[w.resolve() for w in writable or []])) + ' (subpath "/dev")'
        unreadable = " ".join(f'(subpath "{p.resolve()}")' for p in hidden)
        profile = (f"(version 1)(allow default)"
                   f"(deny file-write* (require-not (require-any {allowed})))"
                   + (f"(deny file-read* {unreadable})" if hidden else ""))
        return ["sandbox-exec", "-p", profile, "/bin/sh", "-c", cmd]
    if kind == "bwrap":
        args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                "--bind", str(stage), str(stage), "--bind", str(scratch), str(scratch), "--ro-bind", str(ctx), str(ctx)]
        for w in writable or []:
            args += ["--bind", str(w.resolve()), str(w.resolve())]
        for h in hidden:
            if h.exists():
                args += ["--tmpfs", str(h.resolve())]
        return [*args, "--chdir", str(stage), "/bin/sh", "-c", cmd]
    return ["/bin/sh", "-c", cmd]


def propose(ws: Workspace, base_ref: str, command: str, hypothesis_id: str | None = None, name: str | None = None,
            capabilities: set[Capability] | None = None, timeout: float = 1800, pass_env: list[str] | None = None,
            require_sandbox: bool = False, allow_write: list[Path] | None = None) -> tuple[Revision, Candidate]:
    """Run a coding agent on a staged copy of the base revision and turn its changes into a candidate.

    The agent never gets the repository itself. It works in a fresh git repo made from `git archive` of the base
    commit with the protected paths left out, so the evaluation bundle isn't in its files or its history. Where the
    OS supports it, the agent is sandboxed: it can write only to its stage and a scratch dir, and can't read the real
    repository. Its changes come back as a patch; a patch that writes to protected paths is refused.
    """
    caps = DEFAULT_AGENT_CAPABILITIES if capabilities is None else capabilities
    require(caps, Capability.CREATE_CANDIDATE)
    kind = sandbox_kind()
    if require_sandbox and kind is None:
        raise WorkspaceError("no OS sandbox available (sandbox-exec on macOS, bwrap on Linux); refusing to run the agent")
    bundle = ws.bundle()
    protected = bundle.policy.protected_paths
    base = revisions.resolve(ws, base_ref)
    hyp = ws.ledger.get(hypothesis_id, Hypothesis) if hypothesis_id else None
    label = name or new_id("c")[-10:]
    branch = f"crossfade/candidate-{label}"
    tmp = Path(tempfile.mkdtemp(prefix="crossfade-agent-"))
    stage, scratch, ctx = tmp / "repo", tmp / "scratch", tmp / "context"
    stage.mkdir()
    scratch.mkdir()
    try:
        # 1. stage: the base commit's files minus protected paths, in a repo with no history of them
        excludes = [f":(exclude){p.rstrip('/')}" for p in protected]
        archive = subprocess.run(["git", "archive", "--format=tar", base.commit, "--", ".", *excludes], cwd=ws.root, capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(stage)], input=archive.stdout, check=True)
        ident = ["-c", "user.name=crossfade", "-c", "user.email=crossfade@local"]
        git(stage, "init", "-q", "-b", "work")
        git(stage, "add", "-A")
        git(stage, *ident, "commit", "-q", "--allow-empty", "-m", "base")
        export_context(ws, ctx, hyp, base.id)
        # 2. run the agent with a minimal environment
        env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
        for k in pass_env or []:
            if k in os.environ:
                env[k] = os.environ[k]
        env.update({"CROSSFADE_CONTEXT": str(ctx / "context.json"), "CROSSFADE_WORKTREE": str(stage), "TMPDIR": str(scratch)})
        hidden = [ws.root]
        ws.ledger.append("agent.started", actor=f"agent:{shlex.split(command)[0]}", project_id=ws.project.id, subject=base.id, command=command,
                         branch=branch, capabilities=sorted(c.value for c in caps), sandbox=kind or "none", env=sorted(env))
        if kind is None:
            ws.ledger.append("agent.unsandboxed", actor="crossfade.agents", project_id=ws.project.id, subject=base.id,
                             note="no OS sandbox available; the agent ran with the user's file access")
        r = subprocess.run(_sandboxed(command, stage, scratch, ctx, hidden, allow_write), cwd=stage, env=env, capture_output=True, text=True, timeout=timeout)
        log = ws.artifacts.put_bytes((r.stdout + "\n" + r.stderr).encode())
        if r.returncode != 0:
            ws.ledger.append("agent.failed", actor="crossfade.agents", project_id=ws.project.id, subject=base.id, code=r.returncode, log=log)
            raise WorkspaceError(f"agent exited with {r.returncode}; log artifact {log}")
        # 3. collect the change as a patch and refuse writes to protected paths
        git(stage, "add", "-A")
        touched = [p for p in git(stage, "diff", "--cached", "--name-only", "HEAD").splitlines() if p]
        if not touched:
            raise WorkspaceError("agent made no changes")
        hits = revisions.protected_violations(touched, protected)
        if hits:
            ws.ledger.append("agent.protected_write", actor="crossfade.agents", project_id=ws.project.id, subject=base.id, paths=hits, log=log)
            raise WorkspaceError("agent wrote to protected paths: " + ", ".join(hits))
        patch = subprocess.run(["git", "diff", "--cached", "--binary", "HEAD"], cwd=stage, capture_output=True, check=True).stdout
        # 4. apply it to a real branch from the base commit
        path = ws.state / "agent-worktrees" / label
        if path.exists():
            git(ws.root, "worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
        git(ws.root, "worktree", "add", "-q", "-b", branch, str(path), base.commit)
        subprocess.run(["git", "apply", "--index", "-"], cwd=path, input=patch, check=True, capture_output=True)
        git(path, "-c", "user.name=crossfade-agent", "-c", "user.email=agent@crossfade.local", "commit", "-q", "-m",
            f"Candidate from agent: {hyp.mechanism if hyp else command}")
        commit = git(path, "rev-parse", "HEAD")
        git(ws.root, "worktree", "remove", "--force", str(path), check=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    cand_rev = revisions.resolve(ws, commit, label=branch)
    cand = Candidate(id=new_id("cand"), project_id=ws.project.id, hypothesis_id=hypothesis_id, base_revision=base.id, candidate_revision=cand_rev.id,
                     originator=f"agent:{shlex.split(command)[0]}", patch_artifact=ws.artifacts.put_bytes(patch),
                     touched_paths=revisions.changed_paths(ws, base, cand_rev))
    ws.ledger.put(cand, actor=cand.originator, event="candidate.created")
    return cand_rev, cand
