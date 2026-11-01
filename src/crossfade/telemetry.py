"""Telemetry: capture and normalization.

Python apps run under `python -m crossfade.instrument`, which installs an OpenTelemetry TracerProvider that
writes finished spans as JSON lines to $CROSSFADE_TRACE_FILE. Apps instrument themselves with the standard
OpenTelemetry API; the harness instruments model and tool calls automatically.

Apps in other languages can export OTLP/HTTP JSON to the world server's /v1/traces endpoint, and existing traces
can be imported from OTLP JSON files. Everything is normalized into `models.Span`. Missing data stays missing:
an absent duration or token count is not recorded as zero.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import Span

STATUS = {0: "unset", 1: "ok", 2: "error", "STATUS_CODE_UNSET": "unset", "STATUS_CODE_OK": "ok", "STATUS_CODE_ERROR": "error",
          "UNSET": "unset", "OK": "ok", "ERROR": "error"}


def _attr_value(v: dict[str, Any]) -> Any:
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return int(v[k]) if k == "intValue" else v[k]
    if "arrayValue" in v:
        return [_attr_value(x) for x in v["arrayValue"].get("values", [])]
    return None


def from_jsonl(path: Path) -> list[Span]:
    """Read spans written by crossfade.instrument."""
    out: list[Span] = []
    if not Path(path).exists():
        return out
    for line in Path(path).read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            out.append(Span(**d))
    return out


def from_otlp_json(payload: dict[str, Any], source: str = "otel") -> list[Span]:
    """Normalize an OTLP/JSON ExportTraceServiceRequest."""
    out: list[Span] = []
    for rs in payload.get("resourceSpans", []):
        res = {a["key"]: _attr_value(a["value"]) for a in rs.get("resource", {}).get("attributes", [])}
        service = res.get("service.name", "")
        for ss in rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", [])):
            for s in ss.get("spans", []):
                out.append(Span(
                    trace_id=s["traceId"], span_id=s["spanId"], parent_id=s.get("parentSpanId") or None, name=s["name"], service=service,
                    start=int(s["startTimeUnixNano"]) / 1e9, end=int(s["endTimeUnixNano"]) / 1e9,
                    status=STATUS.get(s.get("status", {}).get("code", 0), "unset"),  # type: ignore[arg-type]
                    attributes={a["key"]: _attr_value(a["value"]) for a in s.get("attributes", [])}, source=source))
    return out


def tree(spans: Iterable[Span]) -> list[dict[str, Any]]:
    """Nest spans by parent for display. Orphans (parent not captured) become roots and are marked."""
    spans = list(spans)
    ids = {s.span_id for s in spans}
    nodes: dict[str, dict[str, Any]] = {s.span_id: {"span": s.model_dump(), "children": []} for s in spans}
    roots = []
    for s in sorted(spans, key=lambda x: x.start):
        n = nodes[s.span_id]
        if s.parent_id and s.parent_id in ids:
            nodes[s.parent_id]["children"].append(n)
        else:
            if s.parent_id:
                n["orphan"] = True
            roots.append(n)
    return roots


def coverage(spans: Iterable[Span]) -> dict[str, Any]:  # noqa: D401
    """What the trace does and doesn't contain. Used to report instrumentation gaps honestly."""
    spans = list(spans)
    app = [s for s in spans if s.source != "world"]
    model = [s for s in app if "gen_ai.system" in s.attributes or s.name.startswith("model")]
    return {
        "spans": len(spans),
        "app_spans": len(app),
        "dependency_spans": len(spans) - len(app),
        "model_spans": len(model),
        "model_spans_with_tokens": sum(1 for s in model if "gen_ai.usage.input_tokens" in s.attributes),
        "orphans": sum(1 for s in app if s.parent_id and s.parent_id not in {x.span_id for x in app}),
    }
