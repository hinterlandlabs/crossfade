"""Findings: evidence-backed observations from traces. Each finding links to the runs and spans behind it."""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from .models import Finding, Run, RunStatus, Span, new_id
from .project import Workspace
from .runner import load_spans


def detect(spans: list[Span]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    # 1. identical dependency calls repeated within one request (span tree under the same root)
    parent = {s.span_id: s.parent_id for s in spans}

    def root(sid: str) -> str:
        seen = 0
        while parent.get(sid) and seen < 64:
            sid, seen = parent[sid] or sid, seen + 1
        return sid

    app = [s for s in spans if s.source != "world"]
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for s in app:
        if not s.parent_id:
            continue
        is_tool = s.name.startswith(("tool ", "execute_tool "))
        is_dep = s.name.lower().startswith(("supplier", "http", "get ", "post ")) or "http.status_code" in s.attributes
        # writes with different bodies are different calls; only reads (or tool calls with identical args) can be redundant
        is_write = any(f" {verb} " in f" {s.name} " for verb in ("POST", "PUT", "PATCH", "DELETE")) or s.attributes.get("http.method") in ("POST", "PUT", "PATCH", "DELETE")
        if is_tool or (is_dep and not is_write):
            key = s.name + (" " + s.attributes.get("crossfade.tool.args", "") if is_tool else "")
            groups[root(s.span_id)][key] += 1
    repeats: Counter[str] = Counter()
    for counter in groups.values():
        for name, n in counter.items():
            if n > 1:
                repeats[name] += n - 1
    for name, extra in repeats.most_common(5):
        out.append({"description": f"Repeated identical call within one request: {name} ({extra} redundant calls across the run)",
                    "severity": "medium", "confidence": 0.8, "detector": "redundant_calls"})
    # 2. dependency call volume
    world = [s for s in spans if s.source == "world"]
    if world and app:
        roots = {root(s.span_id) for s in app if s.parent_id is None}
        if roots and len(world) / len(roots) > 5:
            out.append({"description": f"{len(world)} dependency calls for {len(roots)} requests ({len(world) / len(roots):.1f} per request)",
                        "severity": "low", "confidence": 0.6, "detector": "call_volume"})
    # 3. model token share
    toks = [(s, s.attributes.get("gen_ai.usage.input_tokens")) for s in app if "gen_ai.usage.input_tokens" in s.attributes]
    if toks:
        total = sum(t for _, t in toks if isinstance(t, int))
        out.append({"description": f"{len(toks)} model calls used {total} input tokens ({total // max(1, len(toks))} per call on average)",
                    "severity": "info", "confidence": 0.9, "detector": "model_tokens"})
    errors = [s for s in spans if s.status == "error"]
    if errors:
        top = Counter(s.name for s in errors).most_common(3)
        out.append({"description": "Spans ended in error (this can include expected rejections such as out-of-stock): " + ", ".join(f"{n} x{c}" for n, c in top),
                    "severity": "medium", "confidence": 0.7, "detector": "errors"})
    return out


def record(ws: Workspace, runs: list[Run]) -> list[Finding]:
    spans: list[Span] = []
    ids = []
    revs: set[str] = set()
    for r in runs:
        if r.status == RunStatus.COMPLETED:
            spans += load_spans(ws, r)
            ids.append(r.id)
            revs.add(r.revision_id)
    found = []
    # the same observation on a different revision is a new finding: it tells an agent the problem is still there
    existing = {(f.description, tuple(f.revisions)) for f in ws.ledger.list("finding", Finding, limit=10000)}
    for d in detect(spans):
        if (d["description"], tuple(sorted(revs))) in existing:
            continue
        f = Finding(id=new_id("fnd"), project_id=ws.project.id, description=str(d["description"]), severity=d["severity"],  # type: ignore[arg-type]
                    supporting_runs=ids, revisions=sorted(revs), confidence=float(d["confidence"]), detector=str(d["detector"]),  # type: ignore[arg-type]
                    source_traces=sorted({s.trace_id for s in spans if s.source != "world"})[:20])
        ws.ledger.put(f, actor="crossfade.findings")
        found.append(f)
    return found


def to_json(fs: list[Finding]) -> str:
    return json.dumps([f.model_dump(mode="json") for f in fs], indent=2)
