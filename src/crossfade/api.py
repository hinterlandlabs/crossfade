"""Local HTTP API (versioned under /v1) and the UI.

Binds to localhost by default. Read endpoints are open on that interface; mutating endpoints need the per-workspace
token in .crossfade/api-token (Authorization: Bearer <token>). Everything served comes from the ledger and the
artifact store: no numbers are computed for display that the ledger doesn't hold.
"""

from __future__ import annotations

import secrets
import threading
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import discovery, experiments, export, telemetry
from .models import Candidate, Experiment, Finding, Hypothesis, Revision, Run, new_id
from .plugins import status as plugin_status
from .project import Workspace
from .runner import load_outcome, load_spans

UI = Path(__file__).parent / "ui"


class ExperimentIn(BaseModel):
    baseline: str
    candidate: str
    name: str = ""
    trials: int | None = None
    hypothesis: str | None = None


class HypothesisIn(BaseModel):
    mechanism: str
    findings: list[str] = []
    expected_impact: str = ""
    risks: str = ""
    author: str = "api"


def create_app(ws: Workspace) -> FastAPI:
    token_file = ws.state / "api-token"
    if not token_file.exists():
        token_file.write_text(secrets.token_hex(24))
        token_file.chmod(0o600)
    token = token_file.read_text().strip()
    app = FastAPI(title="Crossfade", version="0.1.0", docs_url="/v1/docs", openapi_url="/v1/openapi.json")
    jobs: dict[str, dict[str, Any]] = {}

    def auth(authorization: str = Header("")) -> None:
        if not secrets.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "mutating endpoints need Authorization: Bearer <token from .crossfade/api-token>")

    def page(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
        return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}

    @app.get("/v1/project")
    def project() -> dict[str, Any]:
        p = ws.project
        evs = [e for e in ws.ledger.events(subject=p.id) if e.type == "project.inspected"]
        manifest = ws.artifacts.get_json(evs[-1].payload["manifest"]) if evs else None
        return {"project": p.model_dump(), "bundle": ws.current_bundle_digest(), "manifest": manifest,
                "counts": {k: ws.ledger.count(k) for k in ("revision", "run", "experiment", "decision", "finding")}}

    @app.get("/v1/manifest")
    def manifest() -> dict[str, Any]:
        return discovery.inspect(ws.root)

    @app.get("/v1/revisions")
    def revisions(limit: int = Query(50, le=500), offset: int = 0) -> dict[str, Any]:
        return page([r.model_dump() for r in ws.ledger.list("revision", Revision, limit=10000)], limit, offset)

    @app.get("/v1/experiments")
    def list_experiments(limit: int = Query(50, le=500), offset: int = 0) -> dict[str, Any]:
        return page(experiments.history(ws), limit, offset)

    @app.get("/v1/experiments/{exp_id}")
    def get_experiment(exp_id: str) -> dict[str, Any]:
        try:
            exp = ws.ledger.get(exp_id, Experiment)
        except KeyError as e:
            raise HTTPException(404, "no such experiment") from e
        d, ev = experiments.decision_for(ws, exp_id), experiments.evaluation_for(ws, exp_id)
        base, cand = ws.ledger.get(exp.baseline_revision, Revision), ws.ledger.get(exp.candidate_revision, Revision)
        c = next((x for x in ws.ledger.list("candidate", Candidate, limit=10000) if x.candidate_revision == cand.id), None)
        hyp = ws.ledger.get(exp.hypothesis_id, Hypothesis).model_dump() if exp.hypothesis_id else None
        run_ids = (ev.baseline_runs + ev.candidate_runs) if ev else [e.payload["run_id"] for e in ws.ledger.events(subject=exp_id) if e.type == "run.attached"]
        runs = [ws.ledger.get(i, Run) for i in run_ids]
        side = {i: "baseline" for i in (ev.baseline_runs if ev else [])} | {i: "candidate" for i in (ev.candidate_runs if ev else [])}
        return {"experiment": exp.model_dump(), "state": experiments.state(ws, exp_id).value, "hypothesis": hyp,
                "baseline": base.model_dump(), "candidate": cand.model_dump(),
                "patch": ws.artifacts.get_bytes(c.patch_artifact).decode(errors="replace") if c and c.patch_artifact else None,
                "candidate_meta": c.model_dump() if c else None, "decision": d.model_dump() if d else None, "evaluation": ev.model_dump() if ev else None,
                "runs": [{**r.model_dump(), "side": side.get(r.id, "baseline" if r.revision_id == base.id else "candidate")} for r in runs],
                "events": [e.model_dump() for e in ws.ledger.events(subject=exp_id)], "job": jobs.get(exp_id)}

    @app.post("/v1/experiments", dependencies=[Depends(auth)], status_code=201)
    def create_experiment(body: ExperimentIn) -> dict[str, Any]:
        exp = experiments.create(ws, body.name or f"{body.baseline} vs {body.candidate}", body.baseline, body.candidate, None, body.trials, body.hypothesis, actor="api")
        return {"experiment": exp.model_dump(), "problems": experiments.validate(ws, exp)}

    @app.post("/v1/experiments/{exp_id}/run", dependencies=[Depends(auth)], status_code=202)
    def run_experiment(exp_id: str) -> dict[str, Any]:
        exp = ws.ledger.get(exp_id, Experiment)
        if exp_id in jobs and jobs[exp_id]["status"] == "running":
            raise HTTPException(409, "already running")
        jobs[exp_id] = {"status": "running", "log": []}

        def work() -> None:
            try:
                res = experiments.run(ws, exp, progress=jobs[exp_id]["log"].append)
                jobs[exp_id].update(status="done", outcome=res.decision.outcome.value)
            except Exception as e:  # noqa: BLE001
                jobs[exp_id].update(status="failed", error=str(e))

        threading.Thread(target=work, daemon=True).start()
        return {"job": exp_id, "status": "running", "poll": f"/v1/experiments/{exp_id}"}

    @app.get("/v1/runs")
    def list_runs(limit: int = Query(50, le=500), offset: int = 0) -> dict[str, Any]:
        return page([r.model_dump() for r in ws.ledger.list("run", Run, limit=10000)], limit, offset)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        r = ws.ledger.get(run_id, Run)
        if is_holdout(r):
            return {"run": r.model_dump(), "outcome": {"withheld": "holdout scenario"}, "log_tail": ""}
        o = load_outcome(ws, r)
        return {"run": r.model_dump(), "outcome": {k: v for k, v in o.items() if k != "log_tail"}, "log_tail": o.get("log_tail", "")}

    @app.get("/v1/runs/{run_id}/trace")
    def run_trace(run_id: str) -> dict[str, Any]:
        r = ws.ledger.get(run_id, Run)
        if is_holdout(r):
            return {"tree": [], "dependencies": [], "coverage": {}, "withheld": "holdout scenario"}
        spans = load_spans(ws, r)
        # measured dependency calls sit in the tree under the span that made them; `dependencies` lists them flat
        return {"tree": telemetry.tree(spans), "dependencies": [s.model_dump() for s in spans if s.source == "world"],
                "coverage": telemetry.coverage(spans)}

    @app.get("/v1/experiments/{exp_id}/diff")
    def experiment_diff(exp_id: str, scenario: str | None = None) -> dict[str, Any]:
        try:
            return export.behaviour_diff(ws, exp_id, scenario)
        except KeyError as e:
            raise HTTPException(404, "no such experiment") from e

    @app.get("/v1/traces/diff")
    def runs_diff(a: str, b: str) -> dict[str, Any]:
        try:
            ra, rb = ws.ledger.get(a, Run), ws.ledger.get(b, Run)
        except KeyError as e:
            raise HTTPException(404, "no such run") from e
        if is_holdout(ra) or is_holdout(rb):
            raise HTTPException(403, "holdout runs are not compared")
        return telemetry.diff([load_spans(ws, ra)], [load_spans(ws, rb)])

    @app.get("/v1/traces/search")
    def search_spans(name: str = "%", status: str | None = None, limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
        return ws.ledger.span_search(name, status, limit)

    @app.post("/v1/traces", dependencies=[Depends(auth)])
    def import_traces(payload: dict[str, Any]) -> dict[str, Any]:
        spans = telemetry.from_otlp_json(payload, source="import")
        digest = ws.artifacts.put_bytes("\n".join(s.model_dump_json() for s in spans).encode())
        ws.ledger.index_spans(f"import-{digest[7:19]}", spans)
        ws.ledger.append("trace.ingested", actor="api", project_id=ws.project.id, subject=digest, spans=len(spans))
        return {"artifact": digest, "spans": len(spans)}

    @app.get("/v1/scenarios")
    def scenarios() -> list[dict[str, Any]]:
        b = ws.bundle()
        return [{"name": s.name, "description": s.description, "steps": len(s.steps), "expectations": len(s.expect), "holdout": s.holdout,
                 "detail": None if s.holdout else s.model_dump()} for s in b.scenarios.values()]

    @app.get("/v1/worlds")
    def worlds() -> dict[str, Any]:
        b = ws.bundle()
        w = b.world_cls(fixture=b.config.world.fixture)
        return {"name": w.name, "version": w.version, "fixtures": sorted(w.fixtures()), "routes": [p for _, _, p, _, _ in w._routes],
                "entities": {k: (len(v) if isinstance(v, dict) else 1) for k, v in w.state.items()}, "fidelity": w.fidelity.model_dump(),
                "snapshot_digest": w.digest(w.snapshot())}

    @app.get("/v1/findings")
    def findings(limit: int = Query(50, le=500), offset: int = 0) -> dict[str, Any]:
        return page([f.model_dump() for f in ws.ledger.list("finding", Finding, limit=10000)], limit, offset)

    @app.post("/v1/hypotheses", dependencies=[Depends(auth)], status_code=201)
    def add_hypothesis(body: HypothesisIn) -> dict[str, Any]:
        h = Hypothesis(id=new_id("hyp"), project_id=ws.project.id, findings=body.findings, mechanism=body.mechanism,
                       expected_impact=body.expected_impact, risks=body.risks, author=body.author)
        ws.ledger.put(h, actor=body.author)
        return h.model_dump()

    def protected_digests() -> set[str]:
        """Locked bundles and the evidence of holdout runs are never served: they would reveal gates, thresholds and holdouts."""
        out = {e.payload["digest"] for e in ws.ledger.events(limit=100000) if e.type == "bundle.locked"}
        for r in ws.ledger.list("run", Run, limit=100000):
            if is_holdout(r):
                out |= {d for d in (r.outcome_artifact, r.trace_artifact, r.log_artifact) if d}
        return out

    def is_holdout(r: Run) -> bool:
        try:
            sc = ws.bundle().scenarios.get(r.scenario)
        except Exception:  # noqa: BLE001
            return False
        return bool(sc and sc.holdout)

    @app.get("/v1/artifacts/{digest}")
    def artifact(digest: str) -> PlainTextResponse:
        if not ws.artifacts.exists(digest) or digest in protected_digests():
            raise HTTPException(404, "no such artifact")
        return PlainTextResponse(ws.artifacts.get_bytes(digest).decode(errors="replace"))

    @app.get("/v1/events")
    def events(subject: str | None = None, limit: int = Query(200, le=5000)) -> list[dict[str, Any]]:
        return [e.model_dump() for e in ws.ledger.events(subject=subject, limit=limit)]

    @app.get("/v1/plugins")
    def plugins() -> list[dict[str, Any]]:
        return plugin_status()

    @app.get("/v1/verify")
    def verify() -> dict[str, Any]:
        n, bad = ws.ledger.verify_chain()
        na, bada = ws.artifacts.verify_all()
        return {"events": n, "bad_events": bad, "artifacts": na, "bad_artifacts": bada, "ok": not bad and not bada}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(UI / "index.html")

    app.mount("/ui", StaticFiles(directory=UI), name="ui")
    return app
