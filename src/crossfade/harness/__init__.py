"""The Crossfade agent harness.

A harness is the environment agent-driven software runs in. This one provides:

- an agent loop (model -> tools -> model ... -> reply) with explicit constraints: step limits, an allow-list of tools,
  token budgets, argument validation, and a gate on irreversible tools;
- tools as plain Python functions with declared parameters and side-effect metadata; a model may call several in one reply;
- conversation state and per-conversation memory;
- hooks around every model call and tool call, for guardrails, redaction or caching without forking the loop;
- replaceable model providers (Anthropic, any OpenAI-compatible endpoint, an HTTP provider for local or simulated models,
  and a fallback chain), with retries on transient failures;
- tracing with the OpenTelemetry GenAI semantic conventions: an `invoke_agent` span per turn, a `chat` span per model call
  and an `execute_tool` span per tool call, plus the gen_ai.client token and duration metrics. Message content is recorded
  only when OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true or `capture_content=True`.

Because execution happens inside the harness, it is also where Crossfade observes behavior and controls conditions. Under
`crossfade.instrument` the trace context travels on outgoing HTTP calls, so the dependency calls Crossfade measures on its
side land under the tool call that made them.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics, trace

from .providers import (
    AnthropicProvider,
    FallbackProvider,
    HTTPModelProvider,
    ModelProvider,
    ModelReply,
    OpenAIChatProvider,
    ProviderError,
    Retry,
    ToolCall,
)

tracer = trace.get_tracer("crossfade.harness", "0.2.0")
meter = metrics.get_meter("crossfade.harness", "0.2.0")
token_usage = meter.create_histogram("gen_ai.client.token.usage", unit="{token}", description="Tokens used per model call")
op_duration = meter.create_histogram("gen_ai.client.operation.duration", unit="s", description="Duration of model calls")
CONTENT_LIMIT = 16000


class ConstraintViolation(Exception):
    pass


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    parameters: dict[str, str] = field(default_factory=dict)  # name -> type ("string", "number", "boolean")
    required: list[str] = field(default_factory=list)
    irreversible: bool = False  # e.g. issuing a refund, sending an email

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {k: {"type": v} for k, v in self.parameters.items()}, "required": self.required},
                "irreversible": self.irreversible}


def tool(name: str | None = None, description: str = "", irreversible: bool = False, **parameters: str) -> Callable[[Callable[..., Any]], Tool]:
    def deco(fn: Callable[..., Any]) -> Tool:
        return Tool(name or fn.__name__, description or (fn.__doc__ or "").strip(), fn, parameters, list(parameters), irreversible)

    return deco


@dataclass
class Constraints:
    max_steps: int = 8
    allowed_tools: set[str] | None = None  # None means every registered tool
    max_input_tokens: int | None = None  # per conversation turn
    allow_irreversible: bool = True
    max_irreversible_per_turn: int = 1


@dataclass
class ModelCall:
    """What is about to be sent to the model. Hooks may change `system` and `messages` (a copy; the conversation is untouched)."""

    conversation_id: str
    step: int
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]


class Hook:
    """Override any of these. Raising ConstraintViolation from a hook ends the turn the same way a constraint does."""

    def before_model(self, call: ModelCall) -> None:
        pass

    def after_model(self, call: ModelCall, reply: ModelReply) -> ModelReply | None:
        """Return a reply to replace the model's."""
        return None

    def before_tool(self, conversation_id: str, call: ToolCall) -> Any:
        """Return anything but None to skip the tool and use that as its result (a cache hit, a refusal)."""
        return None

    def after_tool(self, conversation_id: str, call: ToolCall, result: Any) -> Any:
        """Return anything but None to replace the result the model sees."""
        return None


@dataclass
class TurnResult:
    text: str
    steps: int
    tool_calls: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    error: str | None = None


def _capture_default() -> bool:
    return os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "").lower() in ("1", "true", "yes")


def _parts(m: dict[str, Any]) -> dict[str, Any]:
    """A harness message in the GenAI semantic conventions' message shape (role + typed parts)."""
    if m["role"] == "tool":
        return {"role": "tool", "parts": [{"type": "tool_call_response", "id": m.get("tool_call_id", ""), "response": m.get("content")}]}
    parts: list[dict[str, Any]] = [{"type": "text", "content": m["content"]}] if m.get("content") else []
    parts += [{"type": "tool_call", "id": c["id"], "name": c["name"], "arguments": c["args"]} for c in m.get("tool_calls", [])]
    return {"role": m["role"], "parts": parts}


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)[:CONTENT_LIMIT]


class Agent:
    def __init__(self, provider: ModelProvider, system: str, tools: list[Tool], constraints: Constraints | None = None,
                 name: str = "agent", context: Callable[[str, str, dict[str, Any]], str] | None = None,
                 hooks: list[Hook] | None = None, capture_content: bool | None = None):
        self.provider, self.system, self.name = provider, system, name
        self.tools = {t.name: t for t in tools}
        self.constraints = constraints or Constraints()
        self.context = context  # optional hook that adds context to the system prompt for a turn
        self.hooks = list(hooks or [])
        self.capture_content = _capture_default() if capture_content is None else capture_content
        self.conversations: dict[str, list[dict[str, Any]]] = {}
        self.memory: dict[str, dict[str, Any]] = {}

    def _visible_tools(self) -> list[Tool]:
        allowed = self.constraints.allowed_tools
        return [t for t in self.tools.values() if allowed is None or t.name in allowed]

    def _complete(self, call: ModelCall) -> ModelReply:
        provider_name = getattr(self.provider, "name", "unknown")
        requested = getattr(self.provider, "model", "")
        with tracer.start_as_current_span(f"chat {requested}".strip(), kind=trace.SpanKind.CLIENT) as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.conversation.id", call.conversation_id)
            span.set_attribute("crossfade.turn.step", call.step)
            if requested:
                span.set_attribute("gen_ai.request.model", requested)
            for h in self.hooks:
                h.before_model(call)
            if self.capture_content:
                span.set_attribute("gen_ai.system_instructions", _json([{"type": "text", "content": call.system}]))
                span.set_attribute("gen_ai.input.messages", _json([_parts(m) for m in call.messages]))
            metric_attrs: dict[str, Any] = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": provider_name}
            t0 = time.perf_counter()
            try:
                reply = self.provider.complete(call.system, call.messages, call.tools)
            except ProviderError as e:
                span.set_attribute("gen_ai.provider.name", provider_name)
                span.set_attribute("error.type", e.error_type)
                span.set_status(trace.StatusCode.ERROR, str(e))
                op_duration.record(time.perf_counter() - t0, {**metric_attrs, "error.type": e.error_type})
                raise
            elapsed = time.perf_counter() - t0
            for h in self.hooks:
                reply = h.after_model(call, reply) or reply
            system_name = reply.system_name or provider_name
            span.set_attribute("gen_ai.provider.name", system_name)
            span.set_attribute("gen_ai.system", system_name)  # older name, still read by many backends
            if reply.model:
                span.set_attribute("gen_ai.response.model", reply.model)
                span.set_attribute("gen_ai.request.model", requested or reply.model)
                span.update_name(f"chat {requested or reply.model}")
            if reply.id:
                span.set_attribute("gen_ai.response.id", reply.id)
            if reply.finish_reason:
                span.set_attribute("gen_ai.response.finish_reasons", [reply.finish_reason])
            span.set_attribute("crossfade.model.latency_ms", elapsed * 1000)
            metric_attrs.update({"gen_ai.provider.name": system_name, "gen_ai.request.model": requested or reply.model,
                                 "gen_ai.response.model": reply.model})
            op_duration.record(elapsed, metric_attrs)
            for kind, n in (("input", reply.input_tokens), ("output", reply.output_tokens)):
                if n is not None:
                    span.set_attribute(f"gen_ai.usage.{kind}_tokens", n)
                    token_usage.record(n, {**metric_attrs, "gen_ai.token.type": kind})
            if self.capture_content:
                out = _parts({"role": "assistant", "content": reply.text,
                              "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in reply.calls]})
                span.set_attribute("gen_ai.output.messages", _json([{**out, "finish_reason": reply.finish_reason or reply.type}]))
            return reply

    def _call_tool(self, conversation_id: str, call: ToolCall, turn_state: dict[str, int]) -> Any:
        name, args = call.name, call.args
        with tracer.start_as_current_span(f"execute_tool {name}") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", name)
            span.set_attribute("gen_ai.tool.type", "function")
            span.set_attribute("gen_ai.tool.call.id", call.id)
            span.set_attribute("crossfade.tool.args", json.dumps(args, default=str)[:2000])
            t = self.tools.get(name)
            if t is None or (self.constraints.allowed_tools is not None and name not in self.constraints.allowed_tools):
                span.set_status(trace.StatusCode.ERROR, "tool not allowed")
                span.set_attribute("error.type", "tool_not_allowed")
                raise ConstraintViolation(f"tool {name!r} is not available to this agent")
            missing = [p for p in t.required if p not in args]
            if missing:
                span.set_status(trace.StatusCode.ERROR, "missing arguments")
                span.set_attribute("error.type", "missing_arguments")
                return {"error": f"missing arguments: {missing}"}
            if t.irreversible:
                if not self.constraints.allow_irreversible:
                    raise ConstraintViolation(f"irreversible tool {name!r} is disabled")
                turn_state["irreversible"] += 1
                if turn_state["irreversible"] > self.constraints.max_irreversible_per_turn:
                    raise ConstraintViolation(f"more than {self.constraints.max_irreversible_per_turn} irreversible actions in one turn")
                span.set_attribute("crossfade.tool.irreversible", True)
            result = None
            for h in self.hooks:
                result = h.before_tool(conversation_id, call)
                if result is not None:
                    span.set_attribute("crossfade.tool.short_circuited_by", type(h).__name__)
                    break
            if result is None:
                try:
                    result = t.fn(**{k: v for k, v in args.items() if k in t.parameters})
                except ConstraintViolation:
                    raise
                except Exception as e:  # noqa: BLE001 - a failing tool is the model's problem to handle, not the turn's
                    span.record_exception(e)
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    span.set_attribute("error.type", type(e).__name__)
                    result = {"error": f"{type(e).__name__}: {e}"}
            for h in self.hooks:
                replaced = h.after_tool(conversation_id, call, result)
                result = result if replaced is None else replaced
            span.set_attribute("crossfade.tool.result", json.dumps(result, default=str)[:2000])
            return result

    def respond(self, conversation_id: str, message: str) -> TurnResult:
        msgs = self.conversations.setdefault(conversation_id, [])
        msgs.append({"role": "user", "content": message})
        mem = self.memory.setdefault(conversation_id, {})
        turn = {"irreversible": 0}
        tin = tout = 0
        calls: list[dict[str, Any]] = []
        with tracer.start_as_current_span(f"invoke_agent {self.name}") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", self.name)
            span.set_attribute("gen_ai.conversation.id", conversation_id)
            span.set_attribute("gen_ai.provider.name", getattr(self.provider, "name", "unknown"))
            system = self.system + (("\n\n" + self.context(conversation_id, message, mem)) if self.context else "")
            try:
                for step in range(1, self.constraints.max_steps + 1):
                    call = ModelCall(conversation_id, step, system, [dict(m) for m in msgs], [t.spec() for t in self._visible_tools()])
                    reply = self._complete(call)
                    tin += reply.input_tokens or 0
                    tout += reply.output_tokens or 0
                    span.set_attribute("crossfade.turn.input_tokens", tin)
                    span.set_attribute("crossfade.turn.output_tokens", tout)
                    if self.constraints.max_input_tokens and tin > self.constraints.max_input_tokens:
                        raise ConstraintViolation(f"turn exceeded the input token budget ({tin} > {self.constraints.max_input_tokens})")
                    if reply.type == "text" or not reply.calls:
                        msgs.append({"role": "assistant", "content": reply.text})
                        span.set_attribute("crossfade.turn.steps", step)
                        return TurnResult(reply.text, step, calls, tin, tout)
                    for c in reply.calls:
                        c.id = c.id or f"call_{uuid.uuid4().hex[:12]}"
                    msgs.append({"role": "assistant", "content": reply.text,
                                 "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in reply.calls]})
                    for c in reply.calls:
                        result = self._call_tool(conversation_id, c, turn)
                        calls.append({"tool": c.name, "args": c.args, "result": result, "id": c.id})
                        msgs.append({"role": "tool", "tool_call_id": c.id, "name": c.name, "content": result})
                raise ConstraintViolation(f"no reply within {self.constraints.max_steps} steps")
            except (ConstraintViolation, ProviderError) as e:
                kind = "constraint_violation" if isinstance(e, ConstraintViolation) else "provider_error"
                span.set_status(trace.StatusCode.ERROR, str(e))
                span.set_attribute("error.type", kind)
                span.add_event(kind, {"message": str(e)})
                last = next((m for m in reversed(msgs) if m["role"] == "assistant"), None)
                if last and last.get("tool_calls"):
                    answered = {m.get("tool_call_id") for m in msgs if m["role"] == "tool"}
                    for c in last["tool_calls"]:  # every tool call needs a result, or the next turn is malformed for real APIs
                        if c["id"] not in answered:
                            msgs.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": {"error": "not run"}})
                msgs.append({"role": "assistant", "content": "I can't complete that request."})
                return TurnResult("I can't complete that request.", len(calls), calls, tin, tout, error=str(e))


__all__ = ["Agent", "Tool", "tool", "Constraints", "ConstraintViolation", "Hook", "ModelCall", "ModelReply", "ModelProvider", "ToolCall",
           "HTTPModelProvider", "AnthropicProvider", "OpenAIChatProvider", "FallbackProvider", "ProviderError", "Retry", "TurnResult"]
