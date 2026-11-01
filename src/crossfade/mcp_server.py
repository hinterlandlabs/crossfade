"""MCP adapter for coding agents.

A thin wrapper: authorization and evaluation policy stay in the Crossfade core. Agents can inspect the project,
read findings and non-holdout traces, record hypotheses and request experiments. They cannot read holdout scenarios,
gate code or thresholds, write decisions, or change the bundle. Responses reference ids instead of embedding
whole traces.
"""

from __future__ import annotations

from typing import Any

from . import discovery, experiments, export, findings, telemetry
from .models import Finding, Hypothesis, Run, new_id
from .project import Workspace
from .runner import load_spans


def build(ws: Workspace) -> Any:
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server  # type: ignore[no-redef,attr-defined]

    mcp = Server("crossfade")

    def _visible(run: Run) -> bool:
        b = ws.bundle()
        return run.scenario in b.scenarios and not b.scenarios[run.scenario].holdout

    @mcp.tool()
    def inspect_project() -> dict[str, Any]:
        """Summary of the repository: languages, frameworks, storage, entrypoints, instrumentation."""
        return discovery.inspect(ws.root)["summary"]

    @mcp.tool()
    def list_findings() -> list[dict[str, Any]]:
        """Evidence-backed findings recorded from traces."""
        return [{"id": f.id, "description": f.description, "severity": f.severity, "confidence": f.confidence}
                for f in ws.ledger.list("finding", Finding, limit=100)]

    @mcp.tool()
    def list_scenarios() -> list[dict[str, Any]]:
        """Scenarios you may look at. Protected holdout scenarios are not listed."""
        return [{"name": s.name, "description": s.description} for s in ws.bundle().scenarios.values() if not s.holdout]

    @mcp.tool()
    def get_trace(run_id: str, max_spans: int = 80) -> dict[str, Any]:
        """Span summary for a run (name, duration, status). Spans marked measured are dependency calls Crossfade timed itself,
        under the app span that made them. Holdout runs are not available."""
        run = ws.ledger.get(run_id, Run)
        if not _visible(run):
            return {"error": "this run belongs to a protected scenario"}
        all_spans = load_spans(ws, run)
        spans = sorted(all_spans, key=lambda s: s.start)[:max_spans]
        return {"run": run_id, "metrics": run.metrics, "coverage": telemetry.coverage(all_spans),
                "spans": [{"name": s.name, "ms": round(s.duration_ms, 2), "status": s.status, "parent": s.parent_id, "id": s.span_id,
                           **({"measured": True} if s.source == "world" else {})} for s in spans]}

    @mcp.tool()
    def compare_behaviour(experiment_id: str, scenario: str | None = None, max_rows: int = 30) -> dict[str, Any]:
        """What the candidate did differently from the baseline in an experiment: call paths that appeared, disappeared, ran a
        different number of times or took longer, averaged per run. Holdout runs are not included."""
        d = export.behaviour_diff(ws, experiment_id, scenario)
        return {**d, "rows": d["rows"][:max_rows], "rows_total": len(d["rows"])}

    @mcp.tool()
    def record_findings(run_ids: list[str]) -> list[str]:
        """Analyse runs and record new findings. Returns finding descriptions."""
        runs = [r for r in (ws.ledger.get(i, Run) for i in run_ids) if _visible(r)]
        return [f.description for f in findings.record(ws, runs)]

    @mcp.tool()
    def propose_hypothesis(mechanism: str, expected_impact: str = "", finding_ids: list[str] | None = None) -> str:
        """Record a hypothesis before you change code. Returns its id."""
        h = Hypothesis(id=new_id("hyp"), project_id=ws.project.id, findings=finding_ids or [], mechanism=mechanism,
                       expected_impact=expected_impact, author="agent:mcp")
        ws.ledger.put(h, actor="agent:mcp")
        return h.id

    @mcp.tool()
    def request_experiment(baseline: str, candidate: str, hypothesis_id: str | None = None) -> dict[str, Any]:
        """Run baseline vs candidate under controlled conditions. The evaluator decides; holdout details are withheld."""
        exp = experiments.create(ws, f"agent: {baseline} vs {candidate}", baseline, candidate, None, None, hypothesis_id, actor="agent:mcp")
        res = experiments.run(ws, exp)
        behaviour = export.behaviour_diff(ws, exp.id)["rows"]
        return {"experiment": exp.id, "outcome": res.decision.outcome.value, "reasons": res.decision.reasons,
                "behaviour": [r for r in behaviour if r["change"] != "timing"][:15],
                "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in res.evaluation.checks if c.name.startswith(("candidate:", "objective:"))]}

    @mcp.tool()
    def experiment_history() -> list[dict[str, Any]]:
        """Past experiments and decisions, so you don't repeat what already failed."""
        return experiments.history(ws)

    @mcp.tool()
    def world_coverage() -> dict[str, Any]:
        """What the world reproduces and what it doesn't."""
        b = ws.bundle()
        return {"world": b.world_cls.name, "fidelity": b.world_cls.fidelity.model_dump()}

    return mcp


def main(ws: Workspace) -> None:
    build(ws).run()
