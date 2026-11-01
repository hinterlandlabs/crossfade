"""Providers, multiple tool calls per reply, retries, fallback, hooks and content capture."""

import json

import httpx
import pytest
from opentelemetry import trace

from crossfade.harness import (
    Agent,
    AnthropicProvider,
    ConstraintViolation,
    FallbackProvider,
    Hook,
    ModelReply,
    OpenAIChatProvider,
    ProviderError,
    Retry,
    ToolCall,
    tool,
)
from crossfade.harness.providers import post_json

from .test_harness_and_telemetry import Scripted, exporter


@tool(description="look up", order_id="string")
def lookup(order_id):
    return {"order_id": order_id, "total": 10}


@tool(description="explodes")
def broken():
    raise RuntimeError("database is down")


def test_several_tool_calls_in_one_reply_are_all_run_and_answered():
    a = Agent(Scripted([ModelReply("tool_call", calls=[ToolCall("lookup", {"order_id": "A"}, "t1"), ToolCall("lookup", {"order_id": "B"}, "t2")]),
                        ModelReply("text", text="both found")]), "sys", [lookup])
    r = a.respond("c", "two orders")
    assert r.text == "both found" and [c["args"]["order_id"] for c in r.tool_calls] == ["A", "B"]
    msgs = a.conversations["c"]
    assert [c["id"] for c in msgs[1]["tool_calls"]] == ["t1", "t2"]
    assert [m["tool_call_id"] for m in msgs if m["role"] == "tool"] == ["t1", "t2"]


def test_provider_message_translation_keeps_ids_and_groups_results():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "checking", "tool_calls": [{"id": "t1", "name": "lookup", "args": {"order_id": "A"}},
                                                                         {"id": "t2", "name": "lookup", "args": {"order_id": "B"}}]},
            {"role": "tool", "tool_call_id": "t1", "name": "lookup", "content": {"ok": 1}},
            {"role": "tool", "tool_call_id": "t2", "name": "lookup", "content": {"ok": 2}}]
    ant = AnthropicProvider.to_messages(msgs)
    assert [m["role"] for m in ant] == ["user", "assistant", "user"]  # both results in one user message
    assert [b["tool_use_id"] for b in ant[2]["content"]] == ["t1", "t2"] and ant[1]["content"][0] == {"type": "text", "text": "checking"}
    oai = OpenAIChatProvider.to_messages("sys", msgs)
    assert oai[0] == {"role": "system", "content": "sys"} and json.loads(oai[2]["tool_calls"][1]["function"]["arguments"]) == {"order_id": "B"}
    assert [m.get("tool_call_id") for m in oai[3:]] == ["t1", "t2"]


def _client(responses):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        status, body, headers = responses.pop(0)
        return httpx.Response(status, json=body, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_openai_provider_parses_tool_calls_and_usage():
    p = OpenAIChatProvider("local-model", base_url="http://localhost:11434/v1", api_key="")
    p.client, seen = _client([(200, {"id": "cmpl-1", "model": "local-model", "choices": [{"finish_reason": "tool_calls", "message": {
        "content": None, "tool_calls": [{"id": "x1", "type": "function", "function": {"name": "lookup", "arguments": '{"order_id": "A"}'}}]}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 7}}, {})])
    r = p.complete("sys", [{"role": "user", "content": "hi"}], [lookup.spec()])
    assert r.type == "tool_call" and r.calls[0].id == "x1" and r.args == {"order_id": "A"} and r.input_tokens == 40
    assert r.finish_reason == "tool_calls" and seen[0]["tools"][0]["function"]["name"] == "lookup"


def test_retries_honor_status_and_record_events():
    exporter.clear()
    client, _ = _client([(429, {}, {"retry-after": "0"}), (503, {}, {}), (200, {"ok": True}, {})])
    with trace.get_tracer("t").start_as_current_span("chat"):
        assert post_json(client, "http://m/v1", {}, retry=Retry(attempts=3, base_delay=0)) == {"ok": True}
    span = exporter.get_finished_spans()[-1]
    assert [e.attributes["error.type"] for e in span.events if e.name == "gen_ai.retry"] == ["429", "503"]
    client, _ = _client([(400, {"error": "bad"}, {})])
    with pytest.raises(ProviderError):  # a client error is not retried
        post_json(client, "http://m/v1", {}, retry=Retry(attempts=3, base_delay=0))


class Down:
    name = "down"

    def complete(self, *a):
        raise ProviderError("overloaded", "529")


def test_fallback_and_total_provider_failure():
    exporter.clear()
    a = Agent(FallbackProvider(Down(), Scripted([ModelReply("text", text="from backup")])), "sys", [])
    assert a.respond("c", "hi").text == "from backup"
    chat = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat"))
    assert chat.attributes["crossfade.model.fallback_index"] == 1 and any(e.name == "gen_ai.fallback" for e in chat.events)
    r = Agent(Down(), "sys", []).respond("c", "hi")
    assert r.error and "overloaded" in r.error  # the turn ends cleanly instead of raising into the app


def test_hooks_can_block_short_circuit_and_rewrite():
    class Guard(Hook):
        def before_tool(self, conversation_id, call):
            if call.args.get("order_id") == "cached":
                return {"order_id": "cached", "from": "cache"}
            if call.args.get("order_id") == "forbidden":
                raise ConstraintViolation("order is locked")
            return None

        def after_tool(self, conversation_id, call, result):
            return {**result, "redacted": True}

    def agent(order_id):
        return Agent(Scripted([ModelReply("tool_call", tool="lookup", args={"order_id": order_id}), ModelReply("text", text="ok")]), "sys",
                     [lookup], hooks=[Guard()])

    r = agent("cached").respond("c", "x")
    assert r.tool_calls[0]["result"] == {"order_id": "cached", "from": "cache", "redacted": True}
    a = agent("forbidden")
    r = a.respond("c", "x")
    assert "locked" in r.error
    assert a.conversations["c"][2]["role"] == "tool"  # the unanswered call still gets a result, so the history stays valid


def test_a_failing_tool_becomes_an_error_result():
    exporter.clear()
    r = Agent(Scripted([ModelReply("tool_call", tool="broken"), ModelReply("text", text="sorry")]), "sys", [broken]).respond("c", "x")
    assert r.text == "sorry" and "database is down" in r.tool_calls[0]["result"]["error"]
    span = next(s for s in exporter.get_finished_spans() if s.name == "execute_tool broken")
    assert span.attributes["error.type"] == "RuntimeError"


def test_content_capture_is_opt_in():
    exporter.clear()
    Agent(Scripted([ModelReply("text", text="hello there", model="m1")]), "be nice", [], capture_content=True).respond("c", "hi")
    chat = next(s for s in exporter.get_finished_spans() if s.name == "chat m1")
    assert json.loads(chat.attributes["gen_ai.input.messages"]) == [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}]
    assert json.loads(chat.attributes["gen_ai.output.messages"])[0]["parts"][0]["content"] == "hello there"
    assert json.loads(chat.attributes["gen_ai.system_instructions"])[0]["content"] == "be nice"
