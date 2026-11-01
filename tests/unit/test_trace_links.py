"""Trace context crosses into the world, diffs compare call paths, and exports carry what the trace is evidence for."""

import urllib.request

import httpx
from opentelemetry import trace

from crossfade import instrument, telemetry
from crossfade.models import Span
from crossfade.worlds.base import WorldServer, parse_traceparent

from .test_harness_and_telemetry import exporter  # noqa: F401 - installs the test tracer provider
from .test_worlds import Supplier


def test_dependency_calls_land_under_the_span_that_made_them():
    instrument.propagate()
    instrument.propagate()  # idempotent
    world = Supplier(latency_scale=0)
    tracer = trace.get_tracer("t")
    with WorldServer(world) as srv:
        with tracer.start_as_current_span("reserve") as span:
            req = urllib.request.Request(srv.url + "/products/sku-1", headers={"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"})
            urllib.request.urlopen(req).read()  # the app's own header is not sent twice; ours wins
            with httpx.Client(trust_env=False) as c:
                c.get(srv.url + "/products/sku-2")
        urllib.request.urlopen(srv.url + "/products/sku-3").read()  # no current span: nothing to link
    ctx = span.get_span_context()
    spans = world.spans("run_x")
    assert [s.parent_id for s in spans[:2]] == [format(ctx.span_id, "016x")] * 2
    assert {s.trace_id for s in spans[:2]} == {format(ctx.trace_id, "032x")}
    assert spans[2].parent_id is None and len(spans[2].trace_id) == 32
    assert all(s.attributes["crossfade.measured"] for s in spans)


def test_traceparent_parsing_rejects_garbage():
    assert parse_traceparent("00-" + "a" * 32 + "-" + "b" * 16 + "-01") == ("a" * 32, "b" * 16)
    for bad in (None, "", "00-xyz-abc-01", "00-" + "0" * 32 + "-" + "b" * 16 + "-01"):
        assert parse_traceparent(bad) == (None, None)


def _span(sid, name, parent=None, ms=1.0, **attrs):
    return Span(trace_id="t" * 32, span_id=sid, parent_id=parent, name=name, start=0, end=ms / 1000, attributes=attrs)


def test_diff_finds_removed_paths_and_normalizes_ids():
    before = [_span("r", "create_order"), *[_span(f"g{i}", f"supplier GET /products/sku-{i}", "r") for i in range(4)],
              _span("o", "GET /orders/O-1042", "r", ms=5)]
    after = [_span("r", "create_order"), _span("b", "supplier GET /products", "r"), _span("o", "GET /orders/O-2001", "r", ms=5)]
    d = telemetry.diff([before], [after])
    changes = {r["path"]: r["change"] for r in d["rows"]}
    assert changes["create_order > supplier GET /products"] == "added"
    assert changes["create_order > supplier GET /products/sku-1"] == "removed"
    assert not any("O-" in p or "/orders/*" in p for p in changes)  # same step with different ids is not a difference
    assert d["totals"]["count_a"] == 6 and d["totals"]["count_b"] == 3


def test_otlp_export_round_trips_with_resource_attributes():
    spans = [_span("a" * 16, "root", tokens=1, **{"gen_ai.usage.input_tokens": 12}),
             Span(trace_id="world-x", span_id="w00001", parent_id="a" * 16, name="dep", start=0, end=0.001, source="world", service="world:s")]
    payload = telemetry.to_otlp_json(spans, {"vcs.ref.head.revision": "abc123", "crossfade.decision": "rejected", "service.name": "app"})
    res = {a["key"]: a["value"] for rs in payload["resourceSpans"] for a in rs["resource"]["attributes"]}
    assert res["vcs.ref.head.revision"] == {"stringValue": "abc123"} and res["crossfade.decision"] == {"stringValue": "rejected"}
    back = telemetry.from_otlp_json(payload)
    assert {len(s.trace_id) for s in back} == {32} and {len(s.span_id) for s in back} == {16}  # non-hex ids are made valid
    assert next(s for s in back if s.name == "root").attributes["gen_ai.usage.input_tokens"] == 12
    assert next(s for s in back if s.name == "dep").parent_id == "a" * 16
