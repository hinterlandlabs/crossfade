"""The improvement loop: Crossfade watches the app run, a coding agent decides what to change and changes it, and an
experiment decides whether the change stays.

Before the first round the base revision runs each visible scenario once, and its traces become findings (repeated
calls, dependency calls per request, tokens per model call, errors). Each round runs the agent on a staged copy of the
current best revision (see agents.propose) with that revision's findings and traces, then a full experiment against the
locked evaluation bundle. When a change is accepted, findings are refreshed from its runs. An accepted candidate becomes the base for the next round; anything else is
pruned and the base stays. The loop stops at a round limit, a time budget, or after `patience` rounds in a row
without an accepted change. Every round is recorded in the ledger, and agents see earlier outcomes in their context,
so they don't repeat what already failed. By default the loop ends with the best revision's commit for a person to
review. With `into`, it also fast-forwards that branch to the best revision, and only when the branch hasn't moved
since the loop started and has no uncommitted work.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import agents, experiments, findings, revisions
from .models import Outcome, Revision, Run, RunStatus, new_id
from .project import Workspace, WorkspaceError, git
from .runner import run_scenario


@dataclass
class Round:
    number: int
    outcome: str                 # accepted, rejected, inconclusive, no_change, agent_failed
    detail: str = ""
    candidate: str | None = None  # commit
    experiment: str | None = None


@dataclass
class Optimization:
    id: str
    start: Revision
    best: Revision
    rounds: list[Round] = field(default_factory=list)
    stopped: str = ""
    applied: str = ""  # what happened to the `into` branch

    @property
    def kept(self) -> int:
        return sum(r.outcome == "accepted" for r in self.rounds)


def optimize(ws: Workspace, base_ref: str, agent: str, rounds: int = 5, patience: int = 3, max_minutes: float = 60,
             trials: int | None = None, pass_env: list[str] | None = None, allow_write: list[Path] | None = None,
             require_sandbox: bool = False, into: str | None = None,
             progress: Callable[[str], None] = lambda s: None) -> Optimization:
    if rounds < 1 or patience < 1:
        raise WorkspaceError("rounds and patience must be at least 1")
    start = revisions.resolve(ws, base_ref)
    if into:
        git(ws.root, "check-ref-format", "--branch", into)
    run = Optimization(id=new_id("opt"), start=start, best=start)
    deadline = time.monotonic() + max_minutes * 60
    ws.ledger.append("optimize.started", actor="crossfade.optimize", project_id=ws.project.id, subject=run.id, base=start.id,
                     agent=agent, rounds=rounds, patience=patience, max_minutes=max_minutes)
    progress(f"observing {start.commit[:12]}")
    _observe(ws, start, progress)
    misses = 0
    for n in range(1, rounds + 1):
        if time.monotonic() > deadline:
            run.stopped = f"time budget of {max_minutes:g} minutes used"
            break
        progress(f"round {n}/{rounds}: agent proposing a change to {run.best.commit[:12]}")
        try:
            cand_rev, _ = agents.propose(ws, run.best.commit, agent, name=f"{run.id[-8:]}-r{n}", pass_env=pass_env,
                                         allow_write=allow_write, require_sandbox=require_sandbox)
        except WorkspaceError as e:
            msg = str(e)
            rnd = Round(n, "no_change" if "no changes" in msg else "agent_failed", msg)
        else:
            exp = experiments.create(ws, f"{run.id} round {n}", run.best.commit, cand_rev.commit, trials=trials, actor="crossfade.optimize")
            res = experiments.run(ws, exp, progress=lambda s: progress("  " + s))
            rnd = Round(n, res.decision.outcome.value, "; ".join(res.decision.reasons)[:500], cand_rev.commit, exp.id)
            if res.decision.outcome == Outcome.ACCEPTED:
                run.best = cand_rev
                _learn(ws, cand_rev, progress)
        run.rounds.append(rnd)
        ws.ledger.append("optimize.round", actor="crossfade.optimize", project_id=ws.project.id, subject=run.id, round=n,
                         outcome=rnd.outcome, detail=rnd.detail, candidate=rnd.candidate, experiment=rnd.experiment, best=run.best.commit)
        progress(f"round {n}: {rnd.outcome}" + (f" ({rnd.detail[:120]})" if rnd.detail and rnd.outcome != "accepted" else ""))
        misses = 0 if rnd.outcome == "accepted" else misses + 1
        if misses >= patience:
            run.stopped = f"{patience} rounds in a row without an accepted change"
            break
    else:
        run.stopped = f"round limit of {rounds} reached"
    ws.ledger.append("optimize.finished", actor="crossfade.optimize", project_id=ws.project.id, subject=run.id,
                     best=run.best.commit, kept=run.kept, rounds=len(run.rounds), stopped=run.stopped)
    if into and run.kept:
        run.applied = _fast_forward(ws, into, run.start.commit, run.best.commit)
        ws.ledger.append("optimize.applied", actor="crossfade.optimize", project_id=ws.project.id, subject=run.id,
                         branch=into, best=run.best.commit, result=run.applied)
    return run


def _visible_runs(ws: Workspace, rev: Revision) -> list[Run]:
    scenarios = ws.bundle().scenarios
    return [r for r in ws.ledger.list("run", Run, limit=10000) if r.revision_id == rev.id and r.status == RunStatus.COMPLETED
            and r.scenario in scenarios and not scenarios[r.scenario].holdout]


def _learn(ws: Workspace, rev: Revision, progress: Callable[[str], None]) -> None:
    for f in findings.record(ws, _visible_runs(ws, rev)):
        progress(f"  finding: {f.description}")


def _observe(ws: Workspace, rev: Revision, progress: Callable[[str], None]) -> None:
    """Run the visible scenarios once on `rev` if it has never run, then record what its traces show."""
    if not _visible_runs(ws, rev):
        bundle = ws.bundle()
        for sc in bundle.scenarios.values():
            if not sc.holdout:
                run_scenario(ws, bundle, rev, sc)
    _learn(ws, rev, progress)


def _fast_forward(ws: Workspace, branch: str, start: str, best: str) -> str:
    """Move `branch` to `best` if it still points at `start` (or doesn't exist). Never rewrites anyone's work."""
    ref = f"refs/heads/{branch}"
    tip = git(ws.root, "rev-parse", "--verify", "-q", ref, check=False)
    if tip and tip != start:
        return f"left {branch} alone: it moved to {tip[:12]} during the loop"
    worktrees = git(ws.root, "worktree", "list", "--porcelain").split("\n\n")
    holder = next((w for w in worktrees if f"branch {ref}" in w.splitlines()), None)
    if holder is None:
        git(ws.root, "update-ref", ref, best, *([tip] if tip else []))
    else:
        path = Path(holder.splitlines()[0].removeprefix("worktree "))
        if git(path, "status", "--porcelain", "--untracked-files=no"):
            return f"left {branch} alone: it has uncommitted changes"
        git(path, "merge", "--ff-only", "-q", best)
    return f"fast-forwarded {branch} to {best[:12]}"


__all__ = ["optimize", "Optimization", "Round"]
