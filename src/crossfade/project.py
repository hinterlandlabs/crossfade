"""Workspaces, project configuration and the locked evaluation bundle.

A project keeps everything that defines *how it is judged* in one directory, `crossfade/` at the repository root:

    crossfade/project.yaml   how to start and reach the app, which world to use
    crossfade/policy.yaml    gates, objectives, limits, protected paths
    crossfade/checks.py      gate functions (independent evaluation)
    crossfade/world.py       the world plugin (stateful dependencies)
    crossfade/scenarios/     scenario definitions, including holdouts

`crossfade lock` snapshots that directory into the content-addressed store. Experiments always load the bundle
from that snapshot, never from a candidate's worktree, so a candidate cannot change its own evaluator, world,
scenarios or thresholds. Candidates that touch protected paths are rejected outright.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .models import Policy, Project, Scenario, new_id
from .store.artifacts import ArtifactStore
from .store.ledger import Ledger
from .worlds.base import World

BUNDLE_DIR = "crossfade"
STATE_DIR = ".crossfade"


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str  # e.g. "python app.py"
    health: str = "/health"
    env: dict[str, str] = Field(default_factory=dict)
    startup_timeout: float = 20


class WorldConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin: str  # "world.py:ClassName", relative to the bundle
    fixture: str = "default"
    latency_scale: float = 1.0
    failures: list[dict[str, Any]] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    app: AppConfig
    world: WorldConfig
    checks: str = "checks.py"
    policy: str = "policy.yaml"
    scenarios: str = "scenarios"


class WorkspaceError(Exception):
    pass


def git(root: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def find_root(start: Path) -> Path:
    p = Path(start).resolve()
    for cand in [p, *p.parents]:
        if (cand / BUNDLE_DIR / "project.yaml").exists():
            return cand
    raise WorkspaceError(f"no {BUNDLE_DIR}/project.yaml found at or above {start}. Run `crossfade init` first.")


@dataclass
class Bundle:
    """A loaded, locked evaluation bundle."""

    digest: str
    path: Path
    config: ProjectConfig
    policy: Policy
    scenarios: dict[str, Scenario]
    world_cls: type[World]
    checks: ModuleType

    def scenario_digest(self, name: str) -> str:
        import hashlib

        return "sha256:" + hashlib.sha256(self.scenarios[name].model_dump_json().encode()).hexdigest()


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise WorkspaceError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_bundle_dir(path: Path, digest: str = "unlocked") -> Bundle:
    cfg = ProjectConfig.model_validate(yaml.safe_load((path / "project.yaml").read_text()))
    policy = Policy.model_validate(yaml.safe_load((path / cfg.policy).read_text()) or {})
    scenarios: dict[str, Scenario] = {}
    sdir = path / cfg.scenarios
    for f in sorted(sdir.glob("*.yaml")) if sdir.exists() else []:
        sc = Scenario.model_validate(yaml.safe_load(f.read_text()))
        if sc.name in scenarios:
            raise WorkspaceError(f"duplicate scenario name {sc.name}")
        scenarios[sc.name] = sc
    tag = digest.split(":")[-1][:12]
    file_, _, cls_name = cfg.world.plugin.partition(":")
    world_mod = _load_module(f"crossfade_bundle_{tag}_world", path / file_)
    world_cls = getattr(world_mod, cls_name)
    checks = _load_module(f"crossfade_bundle_{tag}_checks", path / cfg.checks)
    return Bundle(digest, path, cfg, policy, scenarios, world_cls, checks)


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.state = self.root / STATE_DIR
        self.state.mkdir(exist_ok=True)
        self.ledger = Ledger(self.state / "ledger.db")
        self.artifacts = ArtifactStore(self.state / "artifacts")
        self._bundles: dict[str, Bundle] = {}

    @classmethod
    def open(cls, start: Path | str = ".") -> Workspace:
        return cls(find_root(Path(start)))

    # ----- project -----

    @property
    def project(self) -> Project:
        ps = self.ledger.list("project", Project, limit=1)
        if not ps:
            raise WorkspaceError("project is not registered. Run `crossfade init`.")
        return ps[0]

    def register(self, name: str) -> Project:
        existing = self.ledger.list("project", Project, limit=1)
        if existing:
            return existing[0]
        branch = git(self.root, "rev-parse", "--abbrev-ref", "HEAD", check=False) or "main"
        p = Project(id=new_id("prj"), name=name, root=str(self.root), default_branch=branch)
        self.ledger.put(p, actor="human", event="project.registered")
        return p

    # ----- bundle -----

    def lock(self, actor: str = "human") -> str:
        src = self.root / BUNDLE_DIR
        load_bundle_dir(src)  # validate before locking
        digest = self.artifacts.put_dir(src)
        current = self.current_bundle_digest()
        if digest != current:
            self.ledger.append("bundle.locked", actor=actor, project_id=self.project.id, subject=digest, digest=digest)
        return digest

    def current_bundle_digest(self) -> str | None:
        evs = [e for e in self.ledger.events(limit=100000) if e.type == "bundle.locked"]
        return evs[-1].payload["digest"] if evs else None

    def bundle(self, digest: str | None = None) -> Bundle:
        digest = digest or self.current_bundle_digest()
        if not digest:
            raise WorkspaceError("no locked evaluation bundle. Run `crossfade lock`.")
        if digest not in self._bundles:
            dest = self.state / "bundles" / digest.split(":")[1][:16]
            if not dest.exists():
                self.artifacts.extract_dir(digest, dest)
            self._bundles[digest] = load_bundle_dir(dest, digest)
        return self._bundles[digest]
