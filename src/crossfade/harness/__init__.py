"""The Crossfade agent harness.

A harness is the environment agent-driven software runs in. This one provides:

- an agent loop (model -> tool -> model ... -> reply) with explicit constraints: step limits, an allow-list of tools,
  token budgets, argument validation, and a gate on irreversible tools;
- tools as plain Python functions with declared parameters and side-effect metadata;
- conversation state and per-conversation memory;
- tracing of every model call and tool call with OpenTelemetry, using the GenAI semantic conventions
  (gen_ai.system, gen_ai.request.model, gen_ai.usage.input_tokens, ...), so execution is observable by default;
- replaceable model providers (an HTTP provider for local or simulated models, and an Anthropic Messages API provider).

Because execution happens inside the harness, it is also where Crossfade observes behavior and controls conditions.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from opentelemetry import trace

tracer = trace.get_tracer("crossfade.harness", "0.1.0")


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
class ModelReply:
    type: str  # "tool_call" | "text"
    text: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str = ""
    system_name: str = ""


class ModelProvider(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...


class HTTPModelProvider:
    """Talks to a model endpoint that accepts {system, messages, tools} and returns a ModelReply as JSON.
    Used for local models and for simulated models served by a Crossfade world."""

    name = "http"

    def __init__(self, url: str, model: str = "default", timeout: float = 30):
        self.url, self.model = url.rstrip("/"), model
        self.client = httpx.Client(timeout=timeout, trust_env=False)

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        r = self.client.post(f"{self.url}/v1/complete", json={"model": self.model, "system": system, "messages": messages, "tools": tools})
        r.raise_for_status()
        d = r.json()
        return ModelReply(type=d["type"], text=d.get("text", ""), tool=d.get("tool", ""), args=d.get("args", {}),
                          input_tokens=d.get("usage", {}).get("input_tokens"), output_tokens=d.get("usage", {}).get("output_tokens"),
                          model=d.get("model", self.model), system_name=d.get("system_name", "simulated"))


class AnthropicProvider:
    """Anthropic Messages API with tool use. Needs ANTHROPIC_API_KEY and network access (denied by default in experiments)."""

    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None, max_tokens: int = 1024):
        self.model = model or os.environ.get("CROSSFADE_MODEL", "claude-sonnet-5-5")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_tokens = max_tokens
        self.client = httpx.Client(timeout=60)

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        msgs: list[dict[str, Any]] = []
        for i, m in enumerate(messages):
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant" and "tool_call" in m:
                msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": m["tool_call"]["name"], "input": m["tool_call"]["args"]}]})
            elif m["role"] == "tool":
                msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i - 1}", "content": json.dumps(m["content"])}]})
            else:
                msgs.append({"role": "assistant", "content": m.get("content", "")})
        r = self.client.post("https://api.anthropic.com/v1/messages",
                             headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                             json={"model": self.model, "max_tokens": self.max_tokens, "system": system, "messages": msgs,
                                   "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]})
        r.raise_for_status()
        d = r.json()
        usage = d.get("usage", {})
        for block in d.get("content", []):
            if block["type"] == "tool_use":
                return ModelReply("tool_call", tool=block["name"], args=block["input"], input_tokens=usage.get("input_tokens"),
                                  output_tokens=usage.get("output_tokens"), model=self.model, system_name="anthropic")
        text = "".join(b.get("text", "") for b in d.get("content", []) if b["type"] == "text")
        return ModelReply("text", text=text, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                          model=self.model, system_name="anthropic")


@dataclass
class Constraints:
    max_steps: int = 8
    allowed_tools: set[str] | None = None  # None means every registered tool
    max_input_tokens: int | None = None  # per conversation turn
    allow_irreversible: bool = True
    max_irreversible_per_turn: int = 1


@dataclass
class TurnResult:
    text: str
    steps: int
    tool_calls: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    error: str | None = None


class Agent:
    def __init__(self, provider: ModelProvider, system: str, tools: list[Tool], constraints: Constraints | None = None,
                 name: str = "agent", context: Callable[[str, str, dict[str, Any]], str] | None = None):
        self.provider, self.system, self.name = provider, system, name
        self.tools = {t.name: t for t in tools}
        self.constraints = constraints or Constraints()
        self.context = context  # optional hook that adds context to the system prompt for a turn
        self.conversations: dict[str, list[dict[str, Any]]] = {}
        self.memory: dict[str, dict[str, Any]] = {}

    def _visible_tools(self) -> list[Tool]:
        allowed = self.constraints.allowed_tools
        return [t for t in self.tools.values() if allowed is None or t.name in allowed]

    def _call_tool(self, name: str, args: dict[str, Any], turn_state: dict[str, int]) -> Any:
        with tracer.start_as_current_span(f"tool {name}") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", name)
            span.set_attribute("crossfade.tool.args", json.dumps(args, default=str)[:2000])
            t = self.tools.get(name)
            if t is None or (self.constraints.allowed_tools is not None and name not in self.constraints.allowed_tools):
                span.set_status(trace.StatusCode.ERROR, "tool not allowed")
                raise ConstraintViolation(f"tool {name!r} is not available to this agent")
            missing = [p for p in t.required if p not in args]
            if missing:
                span.set_status(trace.StatusCode.ERROR, "missing arguments")
                return {"error": f"missing arguments: {missing}"}
            if t.irreversible:
                if not self.constraints.allow_irreversible:
                    raise ConstraintViolation(f"irreversible tool {name!r} is disabled")
                turn_state["irreversible"] += 1
                if turn_state["irreversible"] > self.constraints.max_irreversible_per_turn:
                    raise ConstraintViolation(f"more than {self.constraints.max_irreversible_per_turn} irreversible actions in one turn")
                span.set_attribute("crossfade.tool.irreversible", True)
            result = t.fn(**{k: v for k, v in args.items() if k in t.parameters})
            span.set_attribute("crossfade.tool.result", json.dumps(result, default=str)[:2000])
            return result

    def respond(self, conversation_id: str, message: str) -> TurnResult:
        msgs = self.conversations.setdefault(conversation_id, [])
        msgs.append({"role": "user", "content": message})
        mem = self.memory.setdefault(conversation_id, {})
        turn = {"irreversible": 0}
        tin = tout = 0
        calls: list[dict[str, Any]] = []
        with tracer.start_as_current_span(f"agent {self.name} turn") as span:
            span.set_attribute("crossfade.conversation.id", conversation_id)
            system = self.system + (("\n\n" + self.context(conversation_id, message, mem)) if self.context else "")
            try:
                for step in range(1, self.constraints.max_steps + 1):
                    with tracer.start_as_current_span("model complete") as ms:
                        t0 = time.time()
                        reply = self.provider.complete(system, msgs, [t.spec() for t in self._visible_tools()])
                        ms.set_attribute("gen_ai.operation.name", "chat")
                        ms.set_attribute("gen_ai.system", reply.system_name or self.provider.name)
                        ms.set_attribute("gen_ai.request.model", reply.model)
                        if reply.input_tokens is not None:
                            ms.set_attribute("gen_ai.usage.input_tokens", reply.input_tokens)
                            tin += reply.input_tokens
                        if reply.output_tokens is not None:
                            ms.set_attribute("gen_ai.usage.output_tokens", reply.output_tokens)
                            tout += reply.output_tokens
                        ms.set_attribute("crossfade.model.latency_ms", (time.time() - t0) * 1000)
                    if self.constraints.max_input_tokens and tin > self.constraints.max_input_tokens:
                        raise ConstraintViolation(f"turn exceeded the input token budget ({tin} > {self.constraints.max_input_tokens})")
                    if reply.type == "text":
                        msgs.append({"role": "assistant", "content": reply.text})
                        span.set_attribute("crossfade.turn.steps", step)
                        return TurnResult(reply.text, step, calls, tin, tout)
                    msgs.append({"role": "assistant", "tool_call": {"name": reply.tool, "args": reply.args}})
                    result = self._call_tool(reply.tool, reply.args, turn)
                    calls.append({"tool": reply.tool, "args": reply.args, "result": result})
                    msgs.append({"role": "tool", "name": reply.tool, "content": result})
                raise ConstraintViolation(f"no reply within {self.constraints.max_steps} steps")
            except ConstraintViolation as e:
                span.set_status(trace.StatusCode.ERROR, str(e))
                span.add_event("constraint_violation", {"message": str(e)})
                msgs.append({"role": "assistant", "content": "I can't complete that request."})
                return TurnResult("I can't complete that request.", len(calls), calls, tin, tout, error=str(e))


__all__ = ["Agent", "Tool", "tool", "Constraints", "ConstraintViolation", "ModelReply", "ModelProvider", "HTTPModelProvider",
           "AnthropicProvider", "TurnResult"]
