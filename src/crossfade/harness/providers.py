"""Model providers for the harness.

Every provider takes the harness's own message format and returns a ModelReply. The format is small on purpose:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [{"id": "...", "name": "...", "args": {...}}]}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": <result>}

Providers translate it to their API, retry transient failures (connection errors, 408, 409, 429, 5xx, honoring
Retry-After) and record each retry as a span event, so a slow turn shows why it was slow.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from opentelemetry import trace


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str = ""


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
    calls: list[ToolCall] = field(default_factory=list)  # every tool call in the reply; `tool`/`args` mirror the first
    id: str = ""
    finish_reason: str = ""

    def __post_init__(self) -> None:
        if self.type == "tool_call" and not self.calls and self.tool:
            self.calls = [ToolCall(self.tool, self.args)]
        if self.calls and not self.tool:
            self.tool, self.args = self.calls[0].name, self.calls[0].args


class ModelProvider(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...


class ProviderError(Exception):
    def __init__(self, message: str, error_type: str = "provider_error"):
        super().__init__(message)
        self.error_type = error_type


@dataclass
class Retry:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    statuses: frozenset[int] = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


def post_json(client: httpx.Client, url: str, body: dict[str, Any], headers: dict[str, str] | None = None,
              retry: Retry | None = None) -> dict[str, Any]:
    retry = retry or Retry()
    span = trace.get_current_span()
    last = ""
    for attempt in range(1, retry.attempts + 1):
        wait = min(retry.max_delay, retry.base_delay * 2 ** (attempt - 1)) * (0.5 + random.random() / 2)
        try:
            r = client.post(url, json=body, headers=headers or {})
        except httpx.TransportError as e:
            last, kind = f"{type(e).__name__}: {e}", type(e).__name__
        else:
            if r.status_code < 400:
                data: dict[str, Any] = r.json()
                return data
            last, kind = f"HTTP {r.status_code}: {r.text[:300]}", str(r.status_code)
            if r.status_code not in retry.statuses:
                raise ProviderError(last, kind)
            after = r.headers.get("retry-after")
            if after and after.replace(".", "", 1).isdigit():
                wait = min(retry.max_delay, float(after))
        if attempt < retry.attempts:
            span.add_event("gen_ai.retry", {"attempt": attempt, "error.type": kind, "wait_s": round(wait, 3)})
            time.sleep(wait)
    raise ProviderError(f"gave up after {retry.attempts} attempts: {last}", kind)


class HTTPModelProvider:
    """Talks to a model endpoint that accepts {model, system, messages, tools} and returns a ModelReply as JSON.
    Used for local models and for simulated models served by a Crossfade world."""

    name = "http"

    def __init__(self, url: str, model: str = "", timeout: float = 30, retry: Retry | None = None):
        self.url, self.model, self.retry = url.rstrip("/"), model, retry
        self.client = httpx.Client(timeout=timeout, trust_env=False)

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        d = post_json(self.client, f"{self.url}/v1/complete", {"model": self.model or "default", "system": system, "messages": messages,
                                                               "tools": tools}, retry=self.retry)
        usage = d.get("usage", {})
        calls = [ToolCall(c["name"], c.get("args", {}), c.get("id", "")) for c in d.get("calls", [])]
        return ModelReply(type=d["type"], text=d.get("text", ""), tool=d.get("tool", ""), args=d.get("args", {}), calls=calls,
                          input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), model=d.get("model") or self.model,
                          system_name=d.get("system_name", "simulated"), id=d.get("id", ""), finish_reason=d.get("finish_reason", ""))


class AnthropicProvider:
    """Anthropic Messages API with tool use. Needs ANTHROPIC_API_KEY and network access (denied by default in experiments)."""

    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None, max_tokens: int = 1024, retry: Retry | None = None,
                 base_url: str = "https://api.anthropic.com"):
        self.model = model or os.environ.get("CROSSFADE_MODEL", "claude-sonnet-5-5")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_tokens, self.retry, self.base_url = max_tokens, retry, base_url.rstrip("/")
        self.client = httpx.Client(timeout=60)

    @staticmethod
    def to_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": json.dumps(m["content"], default=str)}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)  # results of one assistant turn go back in one user message
                else:
                    out.append({"role": "user", "content": [block]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                blocks: list[dict[str, Any]] = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]} for c in m["tool_calls"]]
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": m["role"], "content": m.get("content", "")})
        return out

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        d = post_json(self.client, f"{self.base_url}/v1/messages",
                      {"model": self.model, "max_tokens": self.max_tokens, "system": system, "messages": self.to_messages(messages),
                       "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]},
                      {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, self.retry)
        usage = d.get("usage", {})
        calls = [ToolCall(b["name"], b["input"], b["id"]) for b in d.get("content", []) if b["type"] == "tool_use"]
        text = "".join(b.get("text", "") for b in d.get("content", []) if b["type"] == "text")
        return ModelReply("tool_call" if calls else "text", text=text, calls=calls, input_tokens=usage.get("input_tokens"),
                          output_tokens=usage.get("output_tokens"), model=d.get("model", self.model), system_name="anthropic",
                          id=d.get("id", ""), finish_reason=d.get("stop_reason") or "")


class OpenAIChatProvider:
    """Any OpenAI-compatible chat completions endpoint: OpenAI, and local servers such as Ollama, vLLM or LM Studio."""

    name = "openai"

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1", api_key: str | None = None, max_tokens: int = 1024,
                 retry: Retry | None = None, provider_name: str = "openai"):
        self.model, self.base_url, self.max_tokens, self.retry = model, base_url.rstrip("/"), max_tokens, retry
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self.name = provider_name
        self.client = httpx.Client(timeout=60)

    @staticmethod
    def to_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": json.dumps(m["content"], default=str)})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                out.append({"role": "assistant", "content": m.get("content") or None,
                            "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                           for c in m["tool_calls"]]})
            else:
                out.append({"role": m["role"], "content": m.get("content", "")})
        return out

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        body: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": self.to_messages(system, messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
                             for t in tools]
        d = post_json(self.client, f"{self.base_url}/chat/completions", body,
                      {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}, self.retry)
        choice = d["choices"][0]
        msg, usage = choice.get("message", {}), d.get("usage") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparsed": c["function"].get("arguments")}
            calls.append(ToolCall(c["function"]["name"], args, c.get("id", "")))
        return ModelReply("tool_call" if calls else "text", text=msg.get("content") or "", calls=calls, input_tokens=usage.get("prompt_tokens"),
                          output_tokens=usage.get("completion_tokens"), model=d.get("model", self.model), system_name=self.name,
                          id=d.get("id", ""), finish_reason=choice.get("finish_reason") or "")


class FallbackProvider:
    """Tries each provider in order and uses the first that answers. The chat span records which one did."""

    def __init__(self, *providers: ModelProvider):
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self.providers = providers
        self.name = providers[0].name
        self.model = getattr(providers[0], "model", "")

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        span = trace.get_current_span()
        errors = []
        for i, p in enumerate(self.providers):
            try:
                reply = p.complete(system, messages, tools)
            except ProviderError as e:
                errors.append(f"{p.name}: {e}")
                span.add_event("gen_ai.fallback", {"from": p.name, "error.type": e.error_type})
                continue
            if i:
                span.set_attribute("crossfade.model.fallback_index", i)
            return reply
        raise ProviderError("every provider failed: " + "; ".join(errors), "all_providers_failed")
