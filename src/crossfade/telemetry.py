"""Telemetry: capture and normalization.

Python apps run under `python -m crossfade.instrument`, which installs an OpenTelemetry TracerProvider that
writes finished spans as JSON lines to $CROSSFADE_TRACE_FILE. Apps instrument themselves with the standard
OpenTelemetry API; the harness instruments model and tool calls automatically.

Apps in other languages can export OTLP/HTTP JSON to the world server's /v1/traces endpoint, and existing traces
can be imported from OTLP JSON files. Everything is normalized into `models.Span`. Missing data stays missing:
an absent duration or token count is not recorded as zero.

Going out, `to_otlp_json` turns a run's spans back into OTLP with the run, revision, experiment and decision as resource
attributes, so any OpenTelemetry backend can filter traces by the code version that produced them and what was decided
about it. `diff` compares what two versions did, call path by call path.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
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


def is_model_span(s: Span) -> bool:
    a = s.attributes
    return a.get("gen_ai.operation.name") in ("chat", "text_completion", "generate_content") or "gen_ai.system" in a or s.name.startswith("model")


def coverage(spans: Iterable[Span]) -> dict[str, Any]:  # noqa: D401
    """What the trace does and doesn't contain. Used to report instrumentation gaps honestly."""
    spans = list(spans)
    app = [s for s in spans if s.source != "world"]
    app_ids = {x.span_id for x in app}
    deps = [s for s in spans if s.source == "world"]
    model = [s for s in app if is_model_span(s)]
    return {
        "spans": len(spans),
        "app_spans": len(app),
        "dependency_spans": len(deps),
        # dependency calls Crossfade measured that it could attribute to the app span that made them (via traceparent)
        "dependency_spans_linked": sum(1 for s in deps if s.parent_id in app_ids),
        "model_spans": len(model),
        "model_spans_with_tokens": sum(1 for s in model if "gen_ai.usage.input_tokens" in s.attributes),
        "orphans": sum(1 for s in app if s.parent_id and s.parent_id not in app_ids),
    }


# ---------- comparing two versions ----------

_ID = re.compile(r"\b(?:[A-Z]{1,4}-\d{3,}|[0-9a-f]{8,}|\d{4,})\b")


def _label(s: Span) -> str:
    return _ID.sub("*", s.name)  # "GET /orders/O-1042" and "GET /orders/O-2001" are the same step


def paths(spans: Iterable[Span]) -> dict[str, dict[str, float]]:
    """Aggregate spans by call path (names from the root down): count, total ms, errors, tokens."""
    spans = list(spans)
    by_id = {s.span_id: s for s in spans}
    cache: dict[str, str] = {}

    def path(s: Span, depth: int = 0) -> str:
        if s.span_id in cache:
            return cache[s.span_id]
        parent = by_id.get(s.parent_id or "")
        p = _label(s) if parent is None or depth > 64 else path(parent, depth + 1) + " > " + _label(s)
        cache[s.span_id] = p
        return p

    out: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0.0, "ms": 0.0, "errors": 0.0, "input_tokens": 0.0, "output_tokens": 0.0})
    for s in spans:
        row = out[path(s)]
        row["count"] += 1
        row["ms"] += s.duration_ms
        row["errors"] += s.status == "error"
        for k in ("input_tokens", "output_tokens"):
            v = s.attributes.get(f"gen_ai.usage.{k}")
            if isinstance(v, (int, float)):
                row[k] += v
    return dict(out)


def diff(a_runs: list[list[Span]], b_runs: list[list[Span]]) -> dict[str, Any]:
    """What changed between two versions, per call path, averaged per run.

    Rows are sorted with structural changes first (a path that appears or disappears, or runs a different number of times),
    then by the size of the time change. Durations of app-reported spans are evidence, not measurements; decisions use the
    runner's own metrics.
    """
    def mean(runs: list[list[Span]]) -> dict[str, dict[str, float]]:
        total: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))  # type: ignore[arg-type]
        for spans in runs:
            for p, row in paths(spans).items():
                for k, v in row.items():
                    total[p][k] += v
        n = max(1, len(runs))
        return {p: {k: v / n for k, v in row.items()} for p, row in total.items()}

    a, b = mean(a_runs), mean(b_runs)
    zero = {"count": 0.0, "ms": 0.0, "errors": 0.0, "input_tokens": 0.0, "output_tokens": 0.0}
    rows: list[dict[str, Any]] = []
    for p in sorted(set(a) | set(b)):
        ra, rb = a.get(p, zero), b.get(p, zero)
        change = "added" if p not in a else "removed" if p not in b else "count" if abs(ra["count"] - rb["count"]) >= 0.5 else ""
        if not change and abs(ra["ms"] - rb["ms"]) < 0.5 and ra["errors"] == rb["errors"] and ra["input_tokens"] == rb["input_tokens"]:
            continue
        rows.append({"path": p, "change": change or "timing", **{f"{k}_a": round(ra[k], 2) for k in zero}, **{f"{k}_b": round(rb[k], 2) for k in zero}})
    rows.sort(key=lambda r: (r["change"] == "timing", -abs(r["ms_b"] - r["ms_a"])))
    tot = {f"{k}_{side}": round(sum(r[k] for r in rows_.values()), 2) for side, rows_ in (("a", a), ("b", b)) for k in ("count", "errors", "input_tokens")}
    return {"runs_a": len(a_runs), "runs_b": len(b_runs), "totals": tot, "rows": rows}


# ---------- export ----------


def _hex(value: str, n: int) -> str:
    return value if re.fullmatch(f"[0-9a-f]{{{n}}}", value or "") else hashlib.sha256(value.encode()).hexdigest()[:n]


def _otlp_value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_otlp_value(x) for x in v]}}
    return {"stringValue": v if isinstance(v, str) else json.dumps(v, default=str)}


def _attrs(d: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _otlp_value(v)} for k, v in d.items() if v is not None]


_KIND = {"world": 2}  # SERVER: the world answered the call; everything else is left unspecified
_CODE = {"unset": 0, "ok": 1, "error": 2}


def to_otlp_json(spans: Iterable[Span], resource: dict[str, Any]) -> dict[str, Any]:
    """An OTLP/JSON ExportTraceServiceRequest. `resource` is added to every service's resource attributes."""
    by_service: dict[str, list[Span]] = defaultdict(list)
    for s in spans:
        by_service[s.service or resource.get("service.name", "app")].append(s)
    out = []
    for service, group in by_service.items():
        out.append({"resource": {"attributes": _attrs({**resource, "service.name": service})},
                    "scopeSpans": [{"scope": {"name": "crossfade"}, "spans": [{
                        "traceId": _hex(s.trace_id, 32), "spanId": _hex(s.span_id, 16),
                        **({"parentSpanId": _hex(s.parent_id, 16)} if s.parent_id else {}),
                        "name": s.name, "kind": _KIND.get(s.source, 0),
                        "startTimeUnixNano": str(int(s.start * 1e9)), "endTimeUnixNano": str(int(s.end * 1e9)),
                        "attributes": _attrs({**s.attributes, "crossfade.source": s.source}),
                        "status": {"code": _CODE[s.status]}} for s in group]}]})
    return {"resourceSpans": out}
