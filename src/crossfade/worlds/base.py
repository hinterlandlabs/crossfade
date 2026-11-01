"""World runtime: coherent, stateful stand-ins for an application's external dependencies.

A world is not a folder of canned responses. It holds entities in one state model, so a write changes later
reads. It has a controlled clock, seeded failure injection, snapshot/restore, and a fidelity statement that says
what it models and what it doesn't. Every interaction is metered here, on the trusted side, so cost and call
counts don't depend on numbers the application under test reports about itself.

World plugins subclass `World` and register routes with the `route` decorator.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import secrets
import socketserver
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..models import Fidelity, Span

Handler = Callable[["World", "Request"], "Response"]


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, str]
    query: dict[str, list[str]]
    body: Any


@dataclass
class Response:
    status: int = 200
    body: Any = None


class WorldError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def route(method: str, pattern: str, latency_ms: float | None = None):
    """Register a handler. `pattern` uses {name} placeholders, e.g. "/orders/{order_id}"."""

    def deco(fn: Handler) -> Handler:
        fn.__world_route__ = (method.upper(), pattern, latency_ms)  # type: ignore[attr-defined]
        return fn

    return deco


@dataclass
class FailureRule:
    """Inject a failure into matching calls. Applied with the world's seeded RNG so runs are reproducible."""

    method: str
    pattern: str
    status: int = 503
    rate: float = 1.0
    after_calls: int = 0
    kind: str = "error"  # error | timeout


@dataclass
class Call:
    method: str
    path: str
    route: str
    status: int
    start: float
    end: float
    attrs: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None  # from the caller's traceparent, when it sent one
    parent_id: str | None = None


_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def parse_traceparent(value: str | None) -> tuple[str | None, str | None]:
    m = _TRACEPARENT.match((value or "").strip().lower())
    if not m or set(m.group(1)) == {"0"} or set(m.group(2)) == {"0"}:
        return None, None
    return m.group(1), m.group(2)


class World:
    name = "world"
    version = "0"
    default_latency_ms = 0.0
    fidelity = Fidelity(implements=[])

    def __init__(self, fixture: str = "default", seed: int = 0, failures: list[FailureRule] | None = None, latency_scale: float = 1.0):
        self.seed = seed
        self.rng = random.Random(seed)
        self.failures = failures or []
        self.latency_scale = latency_scale
        self.clock: float = 1_767_225_600.0  # 2026-01-01T00:00:00Z, simulated time (not wall clock)
        self.state: dict[str, dict[str, Any]] = {}
        self.calls: list[Call] = []
        self.meters: dict[str, float] = {"calls_total": 0}
        self._lock = threading.RLock()
        self._routes: list[tuple[str, re.Pattern[str], str, Handler, float | None]] = []
        for attr in dir(type(self)):
            fn = getattr(type(self), attr)
            spec = getattr(fn, "__world_route__", None)
            if spec:
                method, pattern, lat = spec
                rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
                self._routes.append((method, rx, pattern, fn, lat))
        fixtures = self.fixtures()
        if fixture not in fixtures:
            raise KeyError(f"world {self.name} has no fixture {fixture!r} (has {sorted(fixtures)})")
        fixtures[fixture](self)
        self._initial = self.snapshot()

    # ----- to implement in plugins -----

    def fixtures(self) -> dict[str, Callable[[World], None]]:
        return {"default": lambda w: None}

    def invariants(self) -> list[str]:
        """Self-consistency checks on world state (referential integrity etc). Returns violations."""
        return []

    # ----- state helpers -----

    def table(self, name: str) -> dict[str, Any]:
        return self.state.setdefault(name, {})

    def meter(self, key: str, amount: float = 1) -> None:
        self.meters[key] = self.meters.get(key, 0) + amount

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"clock": self.clock, "state": copy.deepcopy(self.state)}

    def restore(self, snap: dict[str, Any]) -> None:
        with self._lock:
            self.clock = snap["clock"]
            self.state = copy.deepcopy(snap["state"])

    def reset(self) -> None:
        self.restore(self._initial)
        self.calls.clear()
        self.meters = {"calls_total": 0}
        self.rng = random.Random(self.seed)

    @staticmethod
    def digest(snap: dict[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.clock += seconds

    # ----- dispatch -----

    def handle(self, method: str, raw_path: str, body: Any, traceparent: str | None = None) -> Response:
        url = urlparse(raw_path)
        start = time.time()
        trace_id, parent_id = parse_traceparent(traceparent)
        with self._lock:
            for m, rx, pattern, fn, lat in self._routes:
                if m != method:
                    continue
                match = rx.match(url.path)
                if not match:
                    continue
                self.meter("calls_total")
                self.meter(f"calls:{method} {pattern}")
                resp = self._maybe_fail(method, pattern)
                if resp is None:
                    try:
                        resp = fn(self, Request(method, url.path, match.groupdict(), parse_qs(url.query), body))
                    except WorldError as e:
                        resp = Response(e.status, {"error": e.message})
                latency = (lat if lat is not None else self.default_latency_ms) * self.latency_scale
                call = Call(method, url.path, pattern, resp.status, start, 0, trace_id=trace_id, parent_id=parent_id)
                self.calls.append(call)
                break
            else:
                self.meter("calls_unsupported")
                self.calls.append(Call(method, url.path, "<unsupported>", 501, start, time.time(), {"coverage_gap": True}, trace_id, parent_id))
                return Response(501, {"error": f"world {self.name} does not implement {method} {url.path}", "coverage_gap": True})
        if latency:
            time.sleep(latency / 1000)  # outside the lock: concurrent callers wait in parallel, like a real service
        call.end = time.time()
        return resp

    def _maybe_fail(self, method: str, pattern: str) -> Response | None:
        for rule in self.failures:
            if rule.method not in (method, "*") or rule.pattern not in (pattern, "*"):
                continue
            if self.meters.get(f"calls:{method} {pattern}", 0) <= rule.after_calls:
                continue
            if self.rng.random() < rule.rate:
                self.meter("failures_injected")
                if rule.kind == "timeout":
                    time.sleep(5)
                return Response(rule.status, {"error": "injected failure", "injected": True})
        return None

    def spans(self, run_id: str) -> list[Span]:
        """Dependency calls as trusted spans (measured by Crossfade, not reported by the app).

        A call that arrived with a traceparent becomes a child of the app span that made it, in the app's trace. Calls without
        one share a per-run trace of their own. Ids are hex, so the spans export as valid OTLP.
        """
        out = []
        own_trace = hashlib.sha256(f"world:{run_id}".encode()).hexdigest()[:32]
        for i, c in enumerate(self.calls):
            attrs = {"http.method": c.method, "http.target": c.path, "http.status_code": c.status,
                     "http.request.method": c.method, "url.path": c.path, "http.route": c.route, "http.response.status_code": c.status,
                     "crossfade.measured": True, **c.attrs}
            out.append(Span(trace_id=c.trace_id or own_trace, span_id=hashlib.sha256(f"{run_id}:{i}".encode()).hexdigest()[:16],
                            parent_id=c.parent_id, name=f"{self.name} {c.method} {c.route}", service=f"world:{self.name}",
                            start=c.start, end=c.end, status="error" if c.status >= 500 else "ok", attributes=attrs, source="world"))
        return out


class _QuietServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind looks up the host's full name, a reverse DNS call that can hang (it did on macOS CI)
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)

class WorldServer:
    """Serves a world over HTTP on 127.0.0.1. Control endpoints under /_world require a per-run token."""

    def __init__(self, world: World, port: int = 0):
        self.world = world
        self.token = secrets.token_hex(16)
        self.otlp_payloads: list[Any] = []
        server = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:  # quiet
                pass

            def _send(self, status: int, body: Any) -> None:
                data = json.dumps(body, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> Any:
                n = int(self.headers.get("Content-Length") or 0)
                if not n:
                    return None
                raw = self.rfile.read(n)
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw.decode(errors="replace")

            def _dispatch(self, method: str) -> None:
                body = self._body()
                if self.path.startswith("/_world"):
                    if self.headers.get("X-World-Token") != server.token:
                        return self._send(403, {"error": "control endpoints are for Crossfade only"})
                    return self._send(*server.control(method, self.path, body))
                if self.path == "/v1/traces" and method == "POST":  # OTLP/HTTP JSON receiver for non-Python apps
                    server.otlp_payloads.append(body)
                    return self._send(200, {})
                resp = server.world.handle(method, self.path, body, self.headers.get("traceparent"))
                self._send(resp.status, resp.body)

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_PUT(self) -> None:
                self._dispatch("PUT")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

        self.httpd = _QuietServer(("127.0.0.1", port), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def control(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        w = self.world
        if path == "/_world/state":
            return 200, w.snapshot()
        if path == "/_world/meters":
            return 200, w.meters
        if path == "/_world/advance" and method == "POST":
            w.advance(float((body or {}).get("seconds", 0)))
            return 200, {"clock": w.clock}
        if path == "/_world/reset" and method == "POST":
            w.reset()
            return 200, {"ok": True}
        return 404, {"error": "unknown control endpoint"}

    def __enter__(self) -> WorldServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
