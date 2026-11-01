"""Canonical object model.

Every object has an id, a kind, a schema version and a creation time. Objects are written once into the
ledger and never updated; state changes are recorded as new objects or events (see store/ledger.py).
"""

from __future__ import annotations

import os
import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


def new_id(prefix: str) -> str:
    """Sortable, unique id: prefix, millisecond timestamp, random suffix."""
    return f"{prefix}_{int(time.time() * 1000):013x}{os.urandom(5).hex()}"


def now() -> float:
    return time.time()


class Obj(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    schema_version: int = SCHEMA_VERSION
    created_at: float = Field(default_factory=now)

    kind: str = "object"


# ---------- provenance and evidence ----------


class Evidence(StrEnum):
    """Where a fact came from. Discovery and world fidelity must say which."""

    OBSERVED = "observed"  # seen directly (file present, call made at runtime)
    STATIC = "static_inference"  # inferred from source code
    RUNTIME = "runtime"  # derived from an execution trace
    DECLARED = "declared"  # stated by a person in configuration
    ASSUMED = "assumed"  # unresolved assumption


class Fact(BaseModel):
    key: str
    value: Any
    evidence: Evidence
    confidence: float = Field(ge=0, le=1)
    source: str = ""


# ---------- project, revision ----------


class Project(Obj):
    kind: Literal["project"] = "project"
    name: str
    root: str
    default_branch: str = "main"
    manifest_artifact: str | None = None


class Revision(Obj):
    kind: Literal["revision"] = "revision"
    project_id: str
    commit: str
    tree: str
    label: str = ""
    parent: str | None = None
    dirty: bool = False
    patch_artifact: str | None = None


# ---------- traces ----------


class Span(BaseModel):
    trace_id: str
    span_id: str
    parent_id: str | None = None
    name: str
    service: str = ""
    start: float
    end: float
    status: Literal["ok", "error", "unset"] = "unset"
    attributes: dict[str, Any] = Field(default_factory=dict)
    source: str = "otel"  # otel | world (trusted, measured by Crossfade) | import

    @property
    def duration_ms(self) -> float:
        return (self.end - self.start) * 1000


# ---------- scenarios, worlds ----------


class Expectation(BaseModel):
    """A deterministic assertion. `target` addresses a value in the run outcome, `op` compares it."""

    target: str  # e.g. "response[0].status", "world.inventory.sku-1.stock", "outputs.reply_count"
    op: Literal["eq", "ne", "gt", "ge", "lt", "le", "in", "contains", "exists"] = "eq"
    value: Any = None
    description: str = ""


class Step(BaseModel):
    """One action against the application. `kind` selects how the runner invokes it."""

    kind: Literal["http", "conversation", "clock"] = "http"
    method: str = "GET"
    path: str = "/"
    body: Any = None
    message: str | None = None  # for conversation steps
    advance_seconds: float = 0  # for clock steps


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    world_snapshot: str = "default"  # named fixture of the world plugin
    seed: int = 0
    steps: list[Step]
    expect: list[Expectation] = Field(default_factory=list)
    holdout: bool = False
    tags: list[str] = Field(default_factory=list)


class Fidelity(BaseModel):
    implements: list[str]
    derived_from_traces: list[str] = Field(default_factory=list)
    from_contracts: list[str] = Field(default_factory=list)
    inferred: list[str] = Field(default_factory=list)
    simplified: list[str] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
    divergence_risks: list[str] = Field(default_factory=list)


# ---------- runs, evaluations, experiments, decisions ----------


class RunStatus(StrEnum):
    COMPLETED = "completed"
    APP_FAILED = "app_failed"  # the application misbehaved (crash, bad exit)
    INFRA_FAILED = "infra_failed"  # setup failed: not the application's fault
    CANCELLED = "cancelled"


class Run(Obj):
    kind: Literal["run"] = "run"
    project_id: str
    revision_id: str
    scenario: str
    scenario_digest: str
    world: str
    world_snapshot_digest: str
    seed: int
    trial: int = 0
    status: RunStatus
    started_at: float
    ended_at: float
    error: str | None = None
    outcome_artifact: str | None = None  # responses, outputs, final world state
    trace_artifact: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    log_artifact: str | None = None


class Check(BaseModel):
    name: str
    kind: Literal["gate", "objective", "info"]
    passed: bool | None = None  # None for objectives that are measured, not pass/fail
    detail: str = ""
    baseline: float | None = None
    candidate: float | None = None
    evidence: list[str] = Field(default_factory=list)  # run ids, artifact digests


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    NEEDS_REVIEW = "needs_review"


class Evaluation(Obj):
    kind: Literal["evaluation"] = "evaluation"
    experiment_id: str
    evaluator: str
    evaluator_version: str
    policy_digest: str
    checks: list[Check]
    baseline_runs: list[str]
    candidate_runs: list[str]


class ExperimentState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PREPARING = "preparing"
    BASELINE_RUNNING = "baseline_running"
    CANDIDATE_RUNNING = "candidate_running"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Experiment(Obj):
    kind: Literal["experiment"] = "experiment"
    project_id: str
    name: str
    hypothesis_id: str | None = None
    baseline_revision: str
    candidate_revision: str
    scenarios: list[str]
    policy_digest: str
    trials: int = 3
    seed: int = 0


class Decision(Obj):
    kind: Literal["decision"] = "decision"
    experiment_id: str
    outcome: Outcome
    reasons: list[str]
    evaluation_id: str
    authority: str = "crossfade.evaluator"  # never the proposing agent
    # Acceptance is not authorization to merge or deploy. That is a separate, human decision.
    merge_authorized: bool = False


class Finding(Obj):
    kind: Literal["finding"] = "finding"
    project_id: str
    description: str
    severity: Literal["info", "low", "medium", "high"] = "medium"
    source_traces: list[str] = Field(default_factory=list)
    supporting_runs: list[str] = Field(default_factory=list)
    revisions: list[str] = Field(default_factory=list)  # revisions whose runs showed it
    confidence: float = 0.5
    detector: str = ""


class Hypothesis(Obj):
    kind: Literal["hypothesis"] = "hypothesis"
    project_id: str
    findings: list[str] = Field(default_factory=list)
    mechanism: str
    expected_impact: str = ""
    risks: str = ""
    author: str = "human"


class Candidate(Obj):
    kind: Literal["candidate"] = "candidate"
    project_id: str
    hypothesis_id: str | None = None
    base_revision: str
    candidate_revision: str
    originator: str  # "human" | "agent:<adapter>"
    patch_artifact: str | None = None
    touched_paths: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    """Versioned rules for executing and evaluating experiments. Locked into the artifact store."""

    model_config = ConfigDict(extra="forbid")
    version: str = "1"
    owner: str = "maintainers"
    protected_paths: list[str] = Field(default_factory=lambda: ["crossfade/", ".crossfade/"])
    gates: list[str] = Field(default_factory=list)  # names of check functions that must pass
    objectives: list[dict[str, Any]] = Field(default_factory=list)  # {metric, direction, min_improvement}
    confidence: float = 0.9
    min_trials: int = 3
    max_cv: float = 0.5  # coefficient of variation above which results are too noisy to decide
    allow_network: bool = False
    timeout_seconds: float = 60
    memory_mb: int = 1024
    require_review_for: list[str] = Field(default_factory=list)


class Event(BaseModel):
    seq: int | None = None
    id: str
    type: str
    actor: str
    at: float
    project_id: str | None = None
    subject: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = ""
    hash: str = ""
