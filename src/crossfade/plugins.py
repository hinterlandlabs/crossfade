"""Plugin registry.

Every replaceable part of Crossfade is a plugin with declared metadata: name, version, interface version,
capabilities, permissions it needs and a health check. The core resolves capabilities, not concrete plugins,
and tolerates plugins that are missing, incompatible or unhealthy. Plugins never receive the ledger, the
evaluator internals or secrets; the core passes them what they need.

In-process plugins are Python classes. Less-trusted or other-language plugins should run out of process and
talk JSON over stdin/stdout (see docs/reference/plugins.md for the protocol).
"""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field

INTERFACE_VERSION = 1


@dataclass
class PluginInfo:
    name: str
    version: str
    kind: str  # discoverer | instrumentation | trace-store | exporter | world-runtime | runner | evaluator | harness | agent-adapter | reporter
    provides: list[str]
    permissions: list[str] = field(default_factory=list)
    health: Callable[[], tuple[bool, str]] = lambda: (True, "ok")
    description: str = ""
    interface_version: int = INTERFACE_VERSION


def _has(mod: str) -> Callable[[], tuple[bool, str]]:
    return lambda: (importlib.util.find_spec(mod) is not None, "available" if importlib.util.find_spec(mod) else f"install {mod}")


def _cmd(name: str) -> Callable[[], tuple[bool, str]]:
    return lambda: (shutil.which(name) is not None, "on PATH" if shutil.which(name) else f"{name} not found")


BUILTIN = [
    PluginInfo("discoverer.python-static", "1.0", "discoverer", ["discovery.inspect"], ["read_repository"], description="AST-based repository inspection"),
    PluginInfo("instrumentation.otel-jsonl", "1.0", "instrumentation", ["telemetry.capture"], [], _has("opentelemetry.sdk"), "OpenTelemetry SDK, spans to JSON lines"),
    PluginInfo("instrumentation.otlp-json", "1.0", "instrumentation", ["telemetry.receive", "telemetry.import"], [], description="OTLP/HTTP JSON receiver and file import"),
    PluginInfo("instrumentation.w3c-propagation", "1.0", "instrumentation", ["telemetry.link"], [],
               description="traceparent on outgoing HTTP, so measured dependency calls join the app's trace"),
    PluginInfo("exporter.otlp-http", "1.0", "exporter", ["telemetry.export"], ["network:operator_endpoint"],
               description="OTLP/HTTP JSON export tagged with commit, experiment and decision; holdouts withheld by default"),
    PluginInfo("trace-store.sqlite", "1.0", "trace-store", ["traces.query"], ["write_ledger"], description="span index in the ledger, spans in the artifact store"),
    PluginInfo("world-runtime.http", "1.0", "world-runtime", ["world.serve"], ["bind_localhost"], description="serves world plugins on 127.0.0.1"),
    PluginInfo("runner.local-process", "1.0", "runner", ["execution.run"], ["spawn_process", "bind_localhost"],
               description="git worktree + subprocess with CPU/file limits; not a hostile-code boundary"),
    PluginInfo("runner.docker", "0.1", "runner", ["execution.run"], ["docker_socket"], _cmd("docker"),
               "container runner for untrusted candidates (requires Docker; interface only in this release)"),
    PluginInfo("evaluator.default", "1.0", "evaluator", ["evaluation.run"], ["read_bundle"], description="gates, expectations, bootstrap objectives"),
    PluginInfo("harness.native", "0.2", "harness", ["agent.run"], ["network:model_endpoint"],
               description="agent loop with hooks, multi-call tool use and GenAI semantic-convention tracing"),
    PluginInfo("harness.anthropic-provider", "0.1", "harness", ["model.complete"], ["network:api.anthropic.com", "secret:ANTHROPIC_API_KEY"],
               lambda: (bool(__import__("os").environ.get("ANTHROPIC_API_KEY")), "ANTHROPIC_API_KEY set" if __import__("os").environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY not set")),
    PluginInfo("harness.openai-compatible-provider", "0.1", "harness", ["model.complete"], ["network:model_endpoint"],
               description="OpenAI-compatible chat completions: OpenAI, Ollama, vLLM, LM Studio"),
    PluginInfo("agent-adapter.command", "1.0", "agent-adapter", ["candidate.propose"], ["spawn_process"], description="any coding agent run as a command"),
    PluginInfo("agent-adapter.claude-code", "1.0", "agent-adapter", ["candidate.propose"], ["spawn_process"], _cmd("claude"), "Claude Code via `claude -p`"),
    PluginInfo("agent-adapter.codex", "1.0", "agent-adapter", ["candidate.propose"], ["spawn_process"], _cmd("codex"), "Codex CLI via `codex exec`"),
    PluginInfo("reporter.api", "1.0", "reporter", ["report.http"], ["bind_localhost"], _has("fastapi"), "local HTTP API and UI"),
    PluginInfo("reporter.mcp", "1.0", "reporter", ["report.mcp"], [], _has("mcp"), "MCP server for coding agents"),
]


def registry() -> list[PluginInfo]:
    return list(BUILTIN)


def resolve(capability: str) -> PluginInfo | None:
    """First healthy plugin providing a capability, or None."""
    for p in BUILTIN:
        if capability in p.provides and p.interface_version == INTERFACE_VERSION:
            try:
                ok, _ = p.health()
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                return p
    return None


def status() -> list[dict[str, object]]:
    out = []
    for p in BUILTIN:
        try:
            ok, msg = p.health()
        except Exception as e:  # noqa: BLE001
            ok, msg = False, f"health check failed: {e}"
        out.append({"name": p.name, "version": p.version, "kind": p.kind, "provides": p.provides, "permissions": p.permissions,
                    "healthy": ok, "status": msg, "description": p.description})
    return out
