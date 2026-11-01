"""Run a Python application with OpenTelemetry configured to write spans for Crossfade.

    python -m crossfade.instrument app.py [args...]

Spans go to $CROSSFADE_TRACE_FILE as JSON lines. SIGTERM is turned into a clean exit so spans are flushed.
"""

from __future__ import annotations

import atexit
import faulthandler
import json
import os
import runpy
import signal
import sys
import threading
from collections.abc import Sequence

# Registered before anything heavy is imported: if the app hangs while starting, the runner sends SIGUSR1 and gets every
# thread's stack in the app log, so the startup error says where it was stuck.
faulthandler.register(signal.SIGUSR1, all_threads=True)

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult  # noqa: E402

_STATUS = {0: "unset", 1: "ok", 2: "error"}


class JsonlExporter(SpanExporter):
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock, open(self.path, "a") as fh:
            for s in spans:
                ctx = s.get_span_context()
                if ctx is None:
                    continue
                fh.write(json.dumps({
                    "trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x"),
                    "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
                    "name": s.name, "service": s.resource.attributes.get("service.name", ""),
                    "start": (s.start_time or 0) / 1e9, "end": (s.end_time or 0) / 1e9,
                    "status": _STATUS.get(s.status.status_code.value, "unset"),
                    "attributes": dict(s.attributes or {}), "source": "otel",
                }, default=str) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def setup(service: str | None = None) -> TracerProvider:
    path = os.environ.get("CROSSFADE_TRACE_FILE")
    provider = TracerProvider(resource=Resource.create({"service.name": service or os.environ.get("OTEL_SERVICE_NAME", "app")}))
    if path:
        provider.add_span_processor(SimpleSpanProcessor(JsonlExporter(path)))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)
    return provider


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python -m crossfade.instrument script.py [args...]")
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    setup()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
