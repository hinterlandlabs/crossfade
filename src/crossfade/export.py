"""Traces tied to what they are evidence for.

A span from a Crossfade run always knows its run, scenario, trial and revision (the exact commit), and, when the run was
part of an experiment, which side it was on and what the evaluator decided. `otlp` puts all of that on the resource, so
a trace sent to Jaeger, Langfuse, Phoenix or Honeycomb can be filtered by commit or by `crossfade.decision=rejected`.
`behaviour_diff` compares an experiment's two sides, call path by call path.

Holdout runs are left out unless asked for: their contents never reach agents, and exporting them by default would make
that easy to undo by accident.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import experiments, telemetry
from .models import Experiment, Revision, Run, RunStatus
from .project import Workspace
from .runner import load_spans


def holdouts(ws: Workspace) -> set[str]:
    try:
        return {name for name, sc in ws.bundle().scenarios.items() if sc.holdout}
    except Exception:  # noqa: BLE001 - no bundle means no holdouts to protect
        return set()


def runs_by_side(ws: Workspace, exp_id: str) -> tuple[Experiment, list[Run], list[Run]]:
    exp = ws.ledger.get(exp_id, Experiment)
    ev = experiments.evaluation_for(ws, exp_id)
    ids = (ev.baseline_runs + ev.candidate_runs) if ev else [e.payload["run_id"] for e in ws.ledger.events(subject=exp_id) if e.type == "run.attached"]
    runs = [ws.ledger.get(i, Run) for i in dict.fromkeys(ids)]
    return (exp, [r for r in runs if r.revision_id == exp.baseline_revision], [r for r in runs if r.revision_id == exp.candidate_revision])


def behaviour_diff(ws: Workspace, exp_id: str, scenario: str | None = None) -> dict[str, Any]:
    """What the change does differently from the original, per call path, averaged over completed non-holdout runs."""
    exp, base, cand = runs_by_side(ws, exp_id)
    hidden = holdouts(ws)

    def keep(r: Run) -> bool:
        return r.status == RunStatus.COMPLETED and r.scenario not in hidden and (scenario is None or r.scenario == scenario)

    d = telemetry.diff([load_spans(ws, r) for r in base if keep(r)], [load_spans(ws, r) for r in cand if keep(r)])
    return {"experiment": exp.id, "scenario": scenario, "baseline_revision": exp.baseline_revision, "candidate_revision": exp.candidate_revision, **d}


def _resource(ws: Workspace, run: Run, revs: dict[str, Revision], context: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rev = revs.get(run.revision_id) or ws.ledger.get(run.revision_id, Revision)
    revs[rev.id] = rev
    return {"crossfade.project": ws.project.name, "crossfade.run.id": run.id, "crossfade.run.status": run.status.value,
            "crossfade.scenario": run.scenario, "crossfade.trial": run.trial, "crossfade.seed": run.seed, "crossfade.world": run.world,
            "crossfade.revision.id": rev.id, "crossfade.revision.label": rev.label or None, "vcs.ref.head.revision": rev.commit,
            **context.get(run.id, {})}


def otlp(ws: Workspace, runs: list[Run], include_holdouts: bool = False) -> tuple[dict[str, Any], list[str]]:
    """One OTLP/JSON payload for these runs, and the ids of runs left out (holdouts, or runs without traces)."""
    hidden = set() if include_holdouts else holdouts(ws)
    context: dict[str, dict[str, Any]] = {}
    wanted = {r.id for r in runs}
    attached = {e.subject for e in ws.ledger.events(limit=1_000_000) if e.type == "run.attached" and e.subject and e.payload.get("run_id") in wanted}
    for exp_id in attached:
        exp = ws.ledger.get(exp_id, Experiment)
        dec = experiments.decision_for(ws, exp_id)
        for r in runs:
            if r.revision_id in (exp.baseline_revision, exp.candidate_revision):
                context[r.id] = {"crossfade.experiment.id": exp.id, "crossfade.experiment.name": exp.name,
                                 "crossfade.side": "original" if r.revision_id == exp.baseline_revision else "change",
                                 "crossfade.decision": dec.outcome.value if dec else "undecided"}
    payload: dict[str, Any] = {"resourceSpans": []}
    skipped, revs = [], {}  # type: ignore[var-annotated]
    for r in runs:
        spans = load_spans(ws, r) if r.scenario not in hidden else []
        if not spans:
            skipped.append(r.id)
            continue
        payload["resourceSpans"] += telemetry.to_otlp_json(spans, _resource(ws, r, revs, context))["resourceSpans"]
    return payload, skipped


def send(endpoint: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: float = 30) -> int:
    """POST to an OTLP/HTTP traces endpoint. `endpoint` may be the collector base URL or the full /v1/traces URL."""
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    r = httpx.post(url, json=payload, headers={"Content-Type": "application/json", **(headers or {})}, timeout=timeout)
    r.raise_for_status()
    return r.status_code
