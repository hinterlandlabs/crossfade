from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from crossfade import discovery, telemetry
from crossfade.harness import Agent, Constraints, ModelReply, tool

exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(_provider)
ROOT = Path(__file__).resolve().parents[2]


class Scripted:
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, system, messages, tools):
        return self.replies.pop(0)


@tool(description="echo", text="string")
def echo(text):
    return {"echo": text}


@tool(description="refund", irreversible=True, order_id="string")
def refund(order_id):
    return {"ok": True}


def test_agent_loop_runs_tools_and_traces_them():
    exporter.clear()
    a = Agent(Scripted([ModelReply("tool_call", tool="echo", args={"text": "hi"}, input_tokens=10, output_tokens=2),
                        ModelReply("text", text="done", input_tokens=12, output_tokens=3)]), "sys", [echo])
    r = a.respond("c1", "hello")
    assert r.text == "done" and r.steps == 2 and r.input_tokens == 22
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert names.count("chat") == 2 and "execute_tool echo" in names and "invoke_agent agent" in names
    chat = next(s for s in spans if s.name == "chat")
    assert chat.attributes["gen_ai.usage.input_tokens"] == 10 and chat.attributes["gen_ai.operation.name"] == "chat"
    assert chat.attributes["gen_ai.conversation.id"] == "c1" and chat.kind == trace.SpanKind.CLIENT
    tool_span = next(s for s in spans if s.name == "execute_tool echo")
    assert tool_span.attributes["gen_ai.tool.call.id"].startswith("call_")
    assert tool_span.parent.span_id == next(s for s in spans if s.name == "invoke_agent agent").context.span_id
    # content is off unless asked for
    assert "gen_ai.input.messages" not in chat.attributes


def test_step_limit_is_enforced():
    a = Agent(Scripted([ModelReply("tool_call", tool="echo", args={"text": "x"})] * 5), "sys", [echo], Constraints(max_steps=3))
    r = a.respond("c", "loop")
    assert r.error and "3 steps" in r.error


def test_disallowed_and_irreversible_tools_are_constrained():
    a = Agent(Scripted([ModelReply("tool_call", tool="refund", args={"order_id": "1"})]), "sys", [echo, refund], Constraints(allowed_tools={"echo"}))
    assert "not available" in a.respond("c", "x").error
    b = Agent(Scripted([ModelReply("tool_call", tool="refund", args={"order_id": "1"}), ModelReply("tool_call", tool="refund", args={"order_id": "2"})]),
              "sys", [refund], Constraints(max_irreversible_per_turn=1))
    assert "irreversible" in b.respond("c", "x").error


def test_token_budget():
    a = Agent(Scripted([ModelReply("text", text="hi", input_tokens=5000)]), "sys", [], Constraints(max_input_tokens=1000))
    assert "budget" in a.respond("c", "x").error


def test_otlp_json_normalization_keeps_missing_data_missing():
    payload = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]},
                                  "scopeSpans": [{"spans": [{"traceId": "t", "spanId": "a", "name": "root", "startTimeUnixNano": "1000000000",
                                                             "endTimeUnixNano": "2000000000", "status": {"code": 1},
                                                             "attributes": [{"key": "n", "value": {"intValue": "3"}}]},
                                                            {"traceId": "t", "spanId": "b", "parentSpanId": "zz", "name": "child",
                                                             "startTimeUnixNano": "1100000000", "endTimeUnixNano": "1200000000"}]}]}]}
    spans = telemetry.from_otlp_json(payload)
    assert spans[0].service == "svc" and spans[0].attributes["n"] == 3 and spans[0].status == "ok"
    assert "gen_ai.usage.input_tokens" not in spans[1].attributes
    roots = telemetry.tree(spans)
    assert any(n.get("orphan") for n in roots)


def test_discovery_labels_evidence():
    m = discovery.inspect(ROOT / "examples" / "order-service")
    s = m["summary"]
    assert "Python" in s["languages"] and "SQLite" in s["storage"] and s["instrumented"]
    assert "python app.py" in s["entrypoints"] and "WORLD_URL" in s["env_vars"]
    unknown = [f for f in m["facts"] if f["key"] == "business_semantics"]
    assert unknown and unknown[0]["evidence"] == "assumed"
