# __init__.py
#
# Public API for the traceact package.
#
# Everything a developer needs to use TraceAct is exported from here. Importing
# from the submodules directly (e.g. from traceact.trace import ActionTrace) is
# supported but not the intended pattern — this file is the stable public surface.
#
# Public API (v1):
#
#   ActionTrace   — the live trace object. Use with ActionTrace.start() for manual
#                   tracing inside a with-block.
#
#   TraceConfig   — settings object. Pass to configure() or to @traced_action.
#
#   TraceBudget   — limits object. Pass to configure() or to @traced_action.
#
#   configure()   — set package-wide defaults for all future traces.
#
#   reset_config()— restore package defaults (use in test teardown).
#
#   traced_action — decorator. The primary way to add tracing to a function.
#
#   JsonlSink     — writes traces to a .jsonl file.
#
#   ConsoleSink   — prints traces to stdout.
#
#   AsyncSink     — wraps any other sink(s) and performs all writes on a
#                   background thread, keeping I/O off the application's hot
#                   path. Handles backpressure, graceful shutdown, and fork
#                   safety. Use when the inner sink is slow or remote.
#
#   SqliteSink    — writes traces to a local SQLite database (stdlib sqlite3).
#                   Scalar columns for fast queries; full JSON in a record
#                   column so no detail is lost. WAL mode enabled by default.
#
#   HttpSink      — POSTs each trace as JSON to an HTTP/HTTPS endpoint (stdlib
#                   urllib). Failed deliveries counted in HttpSink.failed.
#                   Always wrap in AsyncSink for production use.
#
#   OtlpSink      — exports traces to any OTLP-compatible collector via
#                   HTTP/JSON (stdlib urllib, zero extra dependencies).
#                   Works with Jaeger, Grafana Tempo, Honeycomb, Datadog
#                   agent, OTel Collector, and others. Failed deliveries
#                   counted in OtlpSink.failed. Always wrap in AsyncSink
#                   for production use.
#
#   REDACTION_PRESETS — named groups of extra redaction patterns; pass their
#                   names to TraceConfig(redaction_presets=[...]).
#
#   TraceLog      — programmatic query interface for JSONL trace files.
#                   Use instead of the viewer when code (an agent, test, or
#                   script) needs to read traces. TraceLog("traces.jsonl")
#                   .filter(status="failed").last(10) returns plain dicts.
#
#   propagate     — context manager for manual distributed propagation. Reads
#                   traceact-trace-id and traceact-correlation-id from an
#                   incoming request and applies them to every trace started
#                   inside the block (as upstream_trace_id and correlation_id
#                   respectively). Accepts any framework header object.
#
#   inject_headers — stamps the active trace's ID (and correlation id, when set)
#                   into an outbound headers dict so the receiving service can
#                   link its traces back to the caller. Returns a new dict;
#                   the original is not modified.
#
#   inject_context — the queue-boundary counterpart of inject_headers. Stamps
#                   the same context into a job payload dict, which travels
#                   through the queue as ordinary data (a worker process has
#                   no shared ContextVar to inherit). On the worker side pass
#                   it as the reserved traceact_context kwarg to a
#                   @traced_action function, or to propagate().
#
#   TraceActMiddleware     — WSGI middleware (Flask, Django). Wraps the app and
#                   propagates automatically on every request.
#
#   TraceActASGIMiddleware — ASGI middleware (FastAPI, Starlette). Same as
#                   above for async frameworks.

__version__ = "0.14.0"

from traceact.config import configure, reset_config, TraceConfig
from traceact.budget import TraceBudget
from traceact.trace import ActionTrace
from traceact.decorators import traced_action
from traceact.redaction import REDACTION_PRESETS
from traceact.sinks import JsonlSink, ConsoleSink, AsyncSink, SqliteSink, HttpSink, OtlpSink
from traceact.log import TraceLog
from traceact.propagation import (
    propagate,
    inject_headers,
    inject_context,
    extract_trace_id,
    extract_correlation_id,
)
from traceact.middleware import TraceActMiddleware, TraceActASGIMiddleware

__all__ = [
    "__version__",
    "ActionTrace",
    "TraceConfig",
    "TraceBudget",
    "configure",
    "reset_config",
    "traced_action",
    "JsonlSink",
    "ConsoleSink",
    "AsyncSink",
    "SqliteSink",
    "HttpSink",
    "OtlpSink",
    "REDACTION_PRESETS",
    "TraceLog",
    "propagate",
    "inject_headers",
    "inject_context",
    "extract_trace_id",
    "extract_correlation_id",
    "TraceActMiddleware",
    "TraceActASGIMiddleware",
]
