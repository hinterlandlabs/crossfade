"""Materialize a bundled example as a git repository: a baseline commit on main and one branch per candidate."""

from __future__ import annotations

import shutil
from pathlib import Path

from .project import STATE_DIR, git

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
IDENT = ["-c", "user.name=crossfade-example", "-c", "user.email=example@crossfade.local"]


def available() -> list[str]:
    return sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir()) if EXAMPLES.exists() else []


def materialize(name: str, dest: Path) -> list[str]:
    src = EXAMPLES / name
    if not src.exists():
        raise ValueError(f"no example {name!r}. Available: {', '.join(available())}")
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"{dest} exists and is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    for p in src.iterdir():
        if p.name in ("candidates", "__pycache__", "README.md") or p.name.startswith("."):
            continue
        (shutil.copytree if p.is_dir() else shutil.copy2)(p, dest / p.name)
    (dest / ".gitignore").write_text(f"{STATE_DIR}/\n__pycache__/\n")
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, *IDENT, "commit", "-q", "-m", "Baseline")
    for cand in sorted((src / "candidates").iterdir()) if (src / "candidates").exists() else []:
        git(dest, "checkout", "-q", "-b", cand.name, "main")
        for f in cand.rglob("*"):
            if f.is_file() and f.name != "NOTES.md":
                target = dest / f.relative_to(cand)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
        msg = (cand / "NOTES.md").read_text() if (cand / "NOTES.md").exists() else cand.name
        git(dest, "add", "-A")
        git(dest, *IDENT, "commit", "-q", "-m", msg)
    git(dest, "checkout", "-q", "main")
    return git(dest, "branch", "--format=%(refname:short)").split()
