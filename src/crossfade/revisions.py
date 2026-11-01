"""Revisions are exact, immutable source states. Worktrees isolate them from each other and from the user's tree."""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path

from .models import Revision, new_id
from .project import Workspace, WorkspaceError, git


def resolve(ws: Workspace, ref: str, label: str = "", capture_dirty: bool = False) -> Revision:
    """Register a git ref as a revision. With capture_dirty, uncommitted changes are captured as their own commit
    (via `git stash create`, which doesn't touch the working tree) and the patch is stored as an artifact."""
    known = ws.ledger.list("revision", Revision, limit=10000)
    for r in known:
        if r.id == ref:
            return r
    dirty = False
    patch = None
    if capture_dirty and git(ws.root, "status", "--porcelain", check=False):
        commit = git(ws.root, "stash", "create") or git(ws.root, "rev-parse", "HEAD")
        dirty = True
        patch = ws.artifacts.put_bytes(git(ws.root, "diff", "HEAD").encode())
    else:
        # a branch name means wherever the branch points now, not where it pointed when it was first registered
        commit = git(ws.root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
        if not commit:
            for r in known:
                if r.label and r.label == ref:
                    return r
            raise WorkspaceError(f"unknown revision or git ref: {ref}")
    for r in ws.ledger.list("revision", Revision, limit=10000):
        if r.commit == commit:
            return r
    tree = git(ws.root, "rev-parse", f"{commit}^{{tree}}")
    parent = git(ws.root, "rev-parse", f"{commit}^", check=False) or None
    rev = Revision(id=new_id("rev"), project_id=ws.project.id, commit=commit, tree=tree, label=label or ref, parent=parent,
                   dirty=dirty, patch_artifact=patch)
    ws.ledger.put(rev, actor="human", event="revision.registered")
    return rev


def worktree(ws: Workspace, rev: Revision) -> Path:
    """A detached worktree at exactly this commit, under .crossfade/worktrees. Reused across runs; verified clean."""
    path = ws.state / "worktrees" / rev.commit[:12]
    if path.exists():
        if git(path, "rev-parse", "HEAD", check=False) == rev.commit:
            # reset tracked files and remove untracked and ignored ones, so nothing survives from a previous run
            git(path, "checkout", "-q", "-f", rev.commit, check=False)
            git(path, "clean", "-q", "-ffdx", check=False)
            if not git(path, "status", "--porcelain", "--untracked-files=all", "--ignored", check=False):
                return path
        git(ws.root, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    git(ws.root, "worktree", "prune", check=False)
    git(ws.root, "worktree", "add", "--detach", str(path), rev.commit)
    return path


def changed_paths(ws: Workspace, base: Revision, cand: Revision) -> list[str]:
    out = git(ws.root, "diff", "--name-only", base.commit, cand.commit)
    return [p for p in out.splitlines() if p]


def protected_violations(paths: list[str], protected: list[str]) -> list[str]:
    bad = []
    for p in paths:
        for pat in protected:
            if p.startswith(pat.rstrip("*")) if pat.endswith("/") else fnmatch.fnmatch(p, pat):
                bad.append(p)
                break
    return bad


def patch(ws: Workspace, base: Revision, cand: Revision) -> str:
    return git(ws.root, "diff", base.commit, cand.commit)


def require(ws: Workspace, rev_id: str) -> Revision:
    try:
        return ws.ledger.get(rev_id, Revision)
    except KeyError as e:
        raise WorkspaceError(f"unknown revision {rev_id}") from e
