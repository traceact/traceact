# sinks.py
#
# Defines the sink system — where finished traces are written.
#
# A sink is any object that accepts a trace record (a plain Python dict) and
# stores or displays it. TraceAct ships two sinks for v1:
#   JsonlSink    — appends records to a .jsonl file, one JSON object per line.
#   ConsoleSink  — prints records to stdout, formatted for readability.
#
# How sink_mode is handled:
# The sink objects themselves are simple: they just implement write(record).
# The sink_mode setting (from TraceConfig) controls *when* write() is called:
#   "blocking"  — write() is called immediately when a trace finishes.
#   "buffered"  — the record is held in memory; write() is called on flush().
#   "disabled"  — write() is never called.
#
# The actual sink_mode logic lives in trace.py (_write_to_sinks). Sinks do not
# need to know which mode is active — they just write when asked.
#
# Future sinks (SqliteSink, HttpSink, OpenTelemetrySink) will follow the same
# interface: implement write(record: dict) and that is all TraceAct requires.

import atexit
import json
import os
import queue
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Global buffer for buffered sink mode
# ---------------------------------------------------------------------------
#
# When sink_mode="buffered", finished trace records are appended here instead
# of being written to sinks immediately. They are flushed either by an explicit
# flush_buffer() call or automatically when the Python interpreter exits.
#
# Why a module-level list?
# The buffer needs to be shared across all traces in the same process, regardless
# of which sink or configuration object is in scope at the time of the flush.
# A module-level list is the simplest way to achieve this.

_buffer: List[Dict[str, Any]] = []
_flush_registered: bool = False   # True once we have registered the atexit handler

# Guards _buffer against concurrent append/flush. Without it, a record
# appended while flush_buffer() iterates and then clears the list can vanish
# without any signal — flush takes an atomic snapshot under this lock instead,
# and anything appended during the writes stays buffered for the next flush.
_buffer_lock = threading.Lock()


def _flush_on_exit() -> None:
    """
    Called automatically by the Python interpreter on normal program exit.
    Flushes any buffered traces to the configured sinks.

    This ensures that short-lived scripts (which never call flush_buffer()
    explicitly) still produce complete trace output.
    """
    from traceact.config import get_package_sinks
    flush_buffer(get_package_sinks())


def _ensure_flush_registered() -> None:
    """
    Register the atexit flush handler the first time a buffered trace is
    recorded. We register lazily so the handler is only installed when
    actually needed.
    """
    global _flush_registered
    if not _flush_registered:
        atexit.register(_flush_on_exit)
        _flush_registered = True


def reset_buffer() -> None:
    """
    Discard all buffered records and reset the atexit flush registration.

    Call this in test teardown (via reset_config()) to prevent one test's
    buffered traces from leaking into the next test's buffer state. Not
    intended for production use — buffered records not yet flushed to a sink
    are silently discarded.
    """
    global _flush_registered
    with _buffer_lock:
        _buffer.clear()
    _flush_registered = False


def buffer_record(record: Dict[str, Any]) -> None:
    """
    Add a finished trace record to the in-memory buffer.
    Also ensures the atexit flush handler is registered.
    """
    _ensure_flush_registered()
    with _buffer_lock:
        _buffer.append(record)


def flush_buffer(sinks: List[Any]) -> None:
    """
    Write all buffered records to each sink, then clear the buffer.

    Args:
        sinks: The list of sink objects to write to. Note this is the sink
               list at flush time — a record buffered under one configuration
               and flushed after configure() replaced the sinks goes to the
               new sinks.

    This is safe to call multiple times, and safe against concurrent
    buffer_record() calls: the buffer is snapshotted and cleared atomically,
    so a record appended mid-flush is kept for the next flush rather than
    lost. Sink writes happen outside the lock so a slow sink never blocks
    the traced application from buffering.

    With no sinks configured, buffered records fall back to ConsoleSink —
    the same fallback the blocking path applies at write time. Without it,
    a developer who buffers traces and never configures a sink would have
    every record discarded here with nothing to show it ever existed.
    """
    with _buffer_lock:
        if not _buffer:
            return
        snapshot = list(_buffer)
        _buffer.clear()
    if not sinks:
        sinks = [ConsoleSink()]
    for record in snapshot:
        for sink in sinks:
            try:
                sink.write(record)
            except Exception as e:
                # Sink failures never crash the application, but they are
                # reported: a record dropped here has no other trace left.
                print(
                    f"TraceAct: {type(sink).__name__}.write() failed "
                    f"during buffer flush: {e}",
                    file=sys.stderr,
                )


# ---------------------------------------------------------------------------
# JsonlSink
# ---------------------------------------------------------------------------

class JsonlSink:
    """
    Writes trace records to a newline-delimited JSON (JSONL) file.

    Each finished trace is appended as a single line of JSON, followed by a
    newline. This format is easy to append to, easy to read line by line, and
    compatible with tools like jq, grep, and most log aggregators.

    Args:
        path: Path to the output file. The file is created if it does not exist
              and appended to if it does. Parent directories must exist.
        max_bytes: If set, rotate the file once it would exceed this size. The
              current file is renamed to "<stem>.<UTC timestamp><extension>"
              (e.g. traces.20260726T120000000000Z.jsonl) and a fresh file is
              started at `path`. The extension is kept at the end of the name
              so rotated segments still match the *.jsonl pattern that folder
              sources (the viewer and TraceLog) read. Default None: never
              rotate.

    Example:
        sinks=[JsonlSink("data/traces/traces.jsonl")]
        sinks=[JsonlSink("data/traces/traces.jsonl", max_bytes=50_000_000)]

    Rotation and the viewer:
        A rotated-away file keeps its trace history but is no longer the file
        at `path`, so a viewer tailing that single file won't show it. Point
        the viewer at the containing folder instead (`traceact view data/traces/`)
        to see the active file plus every rotated segment merged together.
    """

    def __init__(self, path: str, max_bytes: Optional[int] = None) -> None:
        self.path = path
        self.max_bytes = max_bytes
        # Ensure the parent directory exists so writes don't fail silently.
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        # A per-sink lock that serialises appends from concurrent threads or
        # async tasks within the SAME process.
        #
        # Why this is needed:
        # When many traced actions finish at once (for example, an app running
        # hundreds of concurrent async debates), they may all call write() at
        # nearly the same moment. On POSIX, an append is only guaranteed atomic
        # for writes under PIPE_BUF (typically 4KB). A rich trace record with
        # events and payloads can exceed that, and two overlapping appends could
        # then interleave and corrupt a line. Holding this lock around the write
        # guarantees one complete line lands before the next begins.
        #
        # Scope and limits:
        # This lock only coordinates writers inside one process. It does NOT
        # coordinate separate processes writing to the same file — a threading
        # lock is not shared across process boundaries. For multi-process
        # concurrency, have each process write its own file (for example
        # traces.<pid>.jsonl) and point the viewer at the containing folder,
        # which merges them. See the async sink and the docs for more.
        self._lock = threading.Lock()

    def write(self, record: Dict[str, Any]) -> None:
        """
        Append one trace record to the JSONL file.

        Args:
            record: A plain dict representing the finished trace. Must be
                    JSON-serialisable; non-serialisable values should have
                    been sanitised before reaching the sink.

        The write is guarded by a per-sink lock so that concurrent threads or
        async tasks in the same process cannot interleave partial lines. The
        JSON is serialised once, then written as a single line, so the time
        spent holding the lock is as short as possible.
        """
        line = json.dumps(record, default=str) + "\n"
        with self._lock:
            self._rotate_if_needed(len(line.encode("utf-8")))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        """
        Rename the current file out of the way if writing the next line would
        push it past max_bytes, so a fresh file starts at `path`.

        Called with `self._lock` already held. A rename (not a delete) so the
        rotated segment's history is preserved on disk; only the *active* file
        at `path` is capped in size.
        """
        if self.max_bytes is None:
            return
        try:
            current_size = os.path.getsize(self.path)
        except OSError:
            return  # file doesn't exist yet — nothing to rotate.
        if current_size + incoming_bytes <= self.max_bytes:
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        # The timestamp goes before the extension, not after it, so a rotated
        # segment keeps its .jsonl suffix and stays visible to the *.jsonl
        # folder-source globs in the viewer and TraceLog. (Appending after the
        # extension would hide every rotated segment from those readers.)
        root, ext = os.path.splitext(self.path)
        rotated_path = f"{root}.{timestamp}{ext}"
        try:
            os.rename(self.path, rotated_path)
        except OSError:
            # Best-effort: if the rename fails, keep appending to the same
            # file rather than losing the record.
            pass


# ---------------------------------------------------------------------------
# ConsoleSink
# ---------------------------------------------------------------------------

class ConsoleSink:
    """
    Prints trace records to stdout.

    In pretty mode (the default), the record is printed as indented JSON so it
    is readable in a terminal. In compact mode, it is printed as a single line
    — useful when traces are mixed with other log output and you want each
    record to occupy one line.

    Args:
        pretty: When True (default), print with 2-space indentation. When
                False, print as a compact single-line JSON string.

    Example:
        sinks=[ConsoleSink()]                  # pretty output
        sinks=[ConsoleSink(pretty=False)]      # compact output
    """

    def __init__(self, pretty: bool = True) -> None:
        self.pretty = pretty

    def write(self, record: Dict[str, Any]) -> None:
        """
        Print one trace record to stdout.

        Args:
            record: A plain dict representing the finished trace.
        """
        if self.pretty:
            print(json.dumps(record, indent=2, default=str))
        else:
            print(json.dumps(record, default=str))


# ---------------------------------------------------------------------------
# SqliteSink
# ---------------------------------------------------------------------------
#
# Writes each finished trace to a local SQLite database using the sqlite3
# module from the standard library — no third-party dependencies.
#
# Schema design:
# The full trace record is stored as JSON in the `record` column so no trace
# data is lost. A set of scalar columns (action, kind, status, started_at, …)
# are extracted alongside it and indexed so common queries ("all failed db
# traces", "all traces for correlation id X") run without scanning JSON text.
#
# Thread safety:
# A single sqlite3 connection is kept open per SqliteSink. SQLite connections
# are not safe to share across threads by default; we pass check_same_thread=
# False and guard every write with a threading.Lock. For high-concurrency
# workloads, wrap this sink in AsyncSink — that serialises all writes to a
# single background thread, making the lock a no-op.
#
# WAL mode (Write-Ahead Logging):
# Enabled on every database we open. WAL allows concurrent readers without
# blocking the writer and significantly reduces write latency under load.


import sqlite3 as _sqlite3


class SqliteSink:
    """
    Writes finished traces to a SQLite database, one row per trace.

    The database and table are created automatically on first write if they
    do not already exist. Existing databases opened by a previous process are
    safe to reuse: the CREATE TABLE and CREATE INDEX statements use
    IF NOT EXISTS, so they are no-ops when the schema is already in place.

    The full trace record is stored as JSON so no detail is lost. Common
    fields (action, kind, status, started_at, etc.) are also stored as
    indexed scalar columns for fast filtering without scanning JSON.

    Usage::

        from traceact import SqliteSink, configure

        configure(sinks=[SqliteSink("data/traces/traces.db")])

    For high-concurrency workloads, wrap in AsyncSink so database writes
    happen off the application's hot path::

        configure(sinks=[AsyncSink([SqliteSink("data/traces/traces.db")])])

    Args:
        path:
            Path to the SQLite database file. Created (including any missing
            parent directories) on first write. Use ":memory:" for an
            in-memory database (useful in tests).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        # The connection is opened lazily on first write so that simply
        # constructing a SqliteSink has no side effects.
        self._conn: Optional["_sqlite3.Connection"] = None
        self._lock = threading.Lock()

    # -- public API ----------------------------------------------------------

    def write(self, record: Dict[str, Any]) -> None:
        """
        Insert a finished trace record into the database.

        On the first call, opens (or creates) the database, enables WAL mode,
        and creates the table and indexes if they don't already exist.

        Errors are swallowed — a failing sink must never crash the application
        or the surrounding AsyncSink worker. The exception is logged to stderr
        so the failure is observable without being fatal.
        """
        with self._lock:
            try:
                conn = self._get_connection()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO traces (
                        trace_id, root_trace_id, parent_trace_id,
                        upstream_trace_id, correlation_id, action, kind, status,
                        started_at, ended_at, duration_ms, budget_hit,
                        record
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("trace_id"),
                        record.get("root_trace_id"),
                        record.get("parent_trace_id"),
                        record.get("upstream_trace_id"),
                        record.get("correlation_id"),
                        record.get("action"),
                        record.get("kind"),
                        record.get("status"),
                        record.get("started_at"),
                        record.get("ended_at"),
                        record.get("duration_ms"),
                        int(bool(record.get("budget_hit", False))),
                        json.dumps(record, default=str),
                    ),
                )
                conn.commit()
            except Exception as exc:
                # Write errors must never propagate up to the application or
                # kill an AsyncSink worker thread. Print to stderr so the
                # failure is observable without being fatal.
                import sys
                print(f"traceact SqliteSink write error: {exc}", file=sys.stderr)

    def close(self) -> None:
        """Close the database connection. Safe to call when never written to."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- internals -----------------------------------------------------------

    def _get_connection(self) -> "_sqlite3.Connection":
        """
        Return the open connection, creating it (and the schema) on first call.

        Must be called with self._lock held.
        """
        if self._conn is not None:
            return self._conn

        # Create parent directories if needed (same behaviour as JsonlSink).
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)

        conn = _sqlite3.connect(self.path, check_same_thread=False)

        # WAL mode: readers don't block the writer and writes don't block
        # readers. This is the recommended mode for any application that reads
        # and writes concurrently.
        conn.execute("PRAGMA journal_mode=WAL")

        # Create the schema. IF NOT EXISTS means this is safe to run against
        # a database that already has the table (e.g. from a previous run).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS traces (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id          TEXT NOT NULL UNIQUE,
                root_trace_id     TEXT,
                parent_trace_id   TEXT,
                upstream_trace_id TEXT,
                correlation_id    TEXT,
                action            TEXT NOT NULL,
                kind              TEXT,
                status            TEXT,
                started_at        TEXT,
                ended_at          TEXT,
                duration_ms       REAL,
                budget_hit        INTEGER DEFAULT 0,
                record            TEXT NOT NULL
            );
        """)

        # Migrate databases created by an older TraceAct before running the
        # index statements. CREATE TABLE IF NOT EXISTS is a no-op against an
        # existing table, so a column added in a later version would otherwise
        # be missing and every INSERT would fail. Adding it here means an
        # existing traces.db keeps working across an upgrade with no manual step.
        self._add_missing_columns(conn)

        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_traces_action
                ON traces (action);
            CREATE INDEX IF NOT EXISTS idx_traces_status
                ON traces (status);
            CREATE INDEX IF NOT EXISTS idx_traces_kind
                ON traces (kind);
            CREATE INDEX IF NOT EXISTS idx_traces_started_at
                ON traces (started_at);
            CREATE INDEX IF NOT EXISTS idx_traces_root
                ON traces (root_trace_id);
            CREATE INDEX IF NOT EXISTS idx_traces_correlation
                ON traces (correlation_id);
            CREATE INDEX IF NOT EXISTS idx_traces_upstream
                ON traces (upstream_trace_id);
        """)
        conn.commit()

        self._conn = conn
        return conn

    @staticmethod
    def _add_missing_columns(conn: "_sqlite3.Connection") -> None:
        """
        Add any scalar columns this version expects that an older database
        doesn't have yet.

        SQLite's ALTER TABLE ADD COLUMN is cheap (metadata-only) and existing
        rows get NULL for the new column, which is exactly right — those traces
        genuinely had no value for it.
        """
        expected = {
            "upstream_trace_id": "TEXT",
        }
        existing = {row[1] for row in conn.execute("PRAGMA table_info(traces)")}
        for column, decl in expected.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE traces ADD COLUMN {column} {decl}")


# ---------------------------------------------------------------------------
# HttpSink
# ---------------------------------------------------------------------------
#
# POSTs each finished trace as a JSON body to an HTTP/HTTPS endpoint.
# Uses urllib.request from the standard library — no requests dependency.
#
# Observable failures:
# Network errors, timeouts, and non-2xx responses are caught so the sink
# never crashes the application. Every failure increments HttpSink.failed
# so the caller can observe and act on errors rather than being silently
# misled into thinking all traces reached the collector.
#
# Hot-path note:
# Each write() makes a synchronous HTTP request. For any real endpoint
# this adds meaningful latency to the traced function's return path. Always
# wrap HttpSink in AsyncSink for production use:
#
#     AsyncSink([HttpSink("https://collector.example.com/traces")])
#
# The bundled USAGE.md documents this pattern.


import urllib.request as _urllib_request


class HttpSink:
    """
    POSTs finished traces as JSON to an HTTP or HTTPS endpoint.

    Uses urllib from the standard library — no third-party dependencies.

    **Always wrap in AsyncSink for production use.** Each write() makes a
    synchronous HTTP request; without AsyncSink that latency hits your
    application's hot path on every traced function call.

    Failed writes (connection error, timeout, non-2xx response) are counted
    in HttpSink.failed so they are observable without being fatal. Check it
    in a health endpoint or periodic log::

        sink = HttpSink("https://collector.example.com/traces")
        configure(sinks=[AsyncSink([sink])])

        # later, in a health route:
        if sink.failed > 0:
            logger.warning("HttpSink: %d trace deliveries failed", sink.failed)

    Args:
        url:
            The endpoint URL. Must accept POST requests with a JSON body
            (Content-Type: application/json). Receives one trace record per
            request.

        headers:
            Optional dict of additional request headers. Use this for
            authentication (e.g. {"Authorization": "Bearer <token>",
            "X-Api-Key": "<key>"}).

        timeout:
            Request timeout in seconds. Requests that exceed this are
            abandoned and counted as failures. Default: 5.0.
    """

    def __init__(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 5.0,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout

        # Count of write() calls that failed due to a network error, timeout,
        # or non-2xx response. Observable by choice, never silently hidden.
        self._failed = 0
        self._failed_lock = threading.Lock()

    @property
    def failed(self) -> int:
        """Number of trace deliveries that have failed so far."""
        with self._failed_lock:
            return self._failed

    def write(self, record: Dict[str, Any]) -> None:
        """
        POST a single trace record to the configured URL.

        The body is a UTF-8 encoded JSON object. All errors (network failure,
        timeout, non-2xx status) are caught; failures increment self.failed
        rather than propagating to the caller or crashing an AsyncSink worker.
        """
        body = json.dumps(record, default=str).encode("utf-8")
        req = _urllib_request.Request(
            self.url,
            data=body,
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(body)))
        for name, value in self.headers.items():
            req.add_header(name, value)

        try:
            with _urllib_request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
            if status < 200 or status >= 300:
                self._record_failure()
        except Exception:
            # Broad on purpose: connection refused, DNS failure, timeout, SSL
            # errors, and anything unexpected from urlopen are all treated as
            # delivery failures — counted, never re-raised.
            self._record_failure()

    def _record_failure(self) -> None:
        """Increment the failure counter under its lock."""
        with self._failed_lock:
            self._failed += 1


# ---------------------------------------------------------------------------
# OtlpSink
# ---------------------------------------------------------------------------
#
# Exports finished TraceAct records to any OTLP-compatible collector via the
# OTLP/HTTP+JSON transport. Zero dependencies — uses only stdlib urllib.
#
# Design decision (locked):
#
#   Three approaches were evaluated before this implementation:
#
#   1. Wrap the opentelemetry-sdk — rejected. Takes on OTel's release cycle and
#      contradicts TraceAct's zero-dependency principle. Any version pinning or
#      lag would be TraceAct's problem, not the user's.
#
#   2. OTLP/JSON via stdlib urllib — CHOSEN. Speaks the standard wire protocol
#      directly. Works with any OTLP collector (Jaeger, Grafana Tempo, Honeycomb,
#      Datadog agent, OpenTelemetry Collector) without additional SDKs. The user
#      who already has OTel running just points OtlpSink at their existing
#      collector endpoint — nothing else changes in their setup.
#
#   3. TraceAct becomes an OTel exporter/source — rejected. OTel is TraceAct's
#      export target; TraceAct is the source of truth, not the other way around.
#
# Mapping principle — observable by choice, never lost:
#
#   Fields with a direct OTel span equivalent are mapped explicitly (trace ID,
#   span ID, name, timestamps, parent span, status, steps → events, errors →
#   exception events).
#
#   Fields without a direct OTel equivalent are emitted as `traceact.*` span
#   attributes. This means:
#     - New TraceAct fields added in future versions automatically appear in OTel
#       output as attributes, even before an explicit mapping is decided.
#     - Nothing is ever silently lost.
#     - New explicit mappings are a one-line change inside OtlpSink; core and
#       other sinks never change for this.
#
# ID format:
#
#   OTel requires 128-bit trace IDs (32 hex chars) and 64-bit span IDs (16 hex
#   chars). TraceAct uses prefixed string IDs ("trc_abc123"). We hash each ID
#   with hashlib.md5 to produce a stable, deterministic hex representation. The
#   original TraceAct ID is always preserved as a "traceact.trace_id" span
#   attribute so the two systems remain cross-referenceable.
#
# Observable failures:
#
#   Network errors, timeouts, and non-2xx responses from the collector are caught
#   and counted in OtlpSink.failed — never silently swallowed. Wrap in AsyncSink
#   for production so delivery latency stays off the application's hot path.

import hashlib as _hashlib

# ---------------------------------------------------------------------------
# OTLP helper constants and functions (module-level, used only by OtlpSink)
# ---------------------------------------------------------------------------

# OTel SpanKind codes. INTERNAL is the safe default for anything that doesn't
# map to a more specific category.
_OTLP_KIND_INTERNAL  = 1
_OTLP_KIND_SERVER    = 2
_OTLP_KIND_CLIENT    = 3
_OTLP_KIND_PRODUCER  = 4
_OTLP_KIND_CONSUMER  = 5

# Map TraceAct `kind` values to OTel SpanKind codes.
# Client = outbound call (db query, model API, cache, external HTTP).
# Producer = work enqueued (queue, email, export).
# Consumer = work dequeued (background job).
# Anything not listed defaults to INTERNAL.
_KIND_TO_OTLP: Dict[str, int] = {
    "db":      _OTLP_KIND_CLIENT,
    "http":    _OTLP_KIND_CLIENT,    # treated as outbound by default
    "cache":   _OTLP_KIND_CLIENT,
    "model":   _OTLP_KIND_CLIENT,    # calling a model API
    "auth":    _OTLP_KIND_CLIENT,
    "payment": _OTLP_KIND_CLIENT,
    "queue":   _OTLP_KIND_PRODUCER,
    "email":   _OTLP_KIND_PRODUCER,
    "export":  _OTLP_KIND_PRODUCER,
    "job":     _OTLP_KIND_CONSUMER,
}

# Map TraceAct `status` to OTel StatusCode.
# STATUS_CODE_UNSET=0, STATUS_CODE_OK=1, STATUS_CODE_ERROR=2.
_STATUS_TO_OTLP: Dict[str, Dict[str, Any]] = {
    "completed": {"code": 1},
    "failed":    {"code": 2, "message": "trace failed"},
    # pending / running / cancelled → UNSET (0); OTel has no partial-state codes
}


def _trace_id_hex(traceact_id: str) -> str:
    """
    Convert a TraceAct trace ID to a 32-char hex string (128-bit OTel trace ID).

    We hash the TraceAct ID with MD5 so the output is always exactly 32 hex
    chars and is deterministic: the same TraceAct ID always produces the same
    OTel trace ID, making correlation reliable without any state.
    """
    return _hashlib.md5(traceact_id.encode()).hexdigest()  # 32 chars


def _span_id_hex(traceact_id: str) -> str:
    """
    Convert a TraceAct trace ID to a 16-char hex string (64-bit OTel span ID).

    Uses the first 16 chars of the MD5 hash — the low-order 64 bits. The same
    ID used for the trace and span is intentional: each TraceAct record is both
    a trace root and a span, so there's no separate span object to derive from.
    """
    return _hashlib.md5(traceact_id.encode()).hexdigest()[:16]  # 16 chars


def _iso_to_nanos(ts: Optional[str]) -> int:
    """
    Convert an ISO 8601 timestamp to Unix nanoseconds (OTel's time unit).

    Returns 0 for a missing or unparseable timestamp rather than raising, so
    a bad timestamp in one trace record doesn't drop the whole export.

    Python's datetime.fromisoformat handles "Z" only from 3.11+; we normalise
    the "Z" suffix to "+00:00" first so this works on Python 3.9/3.10 too.
    """
    if not ts:
        return 0
    try:
        from datetime import datetime, timezone
        normalised = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return int((dt - epoch).total_seconds() * 1_000_000_000)
    except (ValueError, OverflowError):
        return 0


def _otlp_attr(key: str, value: Any) -> Dict[str, Any]:
    """
    Encode a key-value pair as an OTLP AnyValue attribute dict.

    OTel's attribute value is a typed union. We map Python types to their
    OTel equivalents; everything else becomes a string so nothing is lost.
    """
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        # OTel encodes int64 as a string in JSON to avoid precision loss.
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


# Fields that are handled by explicit OTel mappings. Any top-level record field
# NOT in this set is emitted as a "traceact.<field>" span attribute — the safety
# net that ensures future TraceAct schema additions are never silently lost.
_EXPLICITLY_MAPPED = frozenset({
    "trace_id", "root_trace_id", "parent_trace_id", "upstream_trace_id",
    "correlation_id",
    "action", "kind", "status", "started_at", "ended_at", "duration_ms",
    "budget_hit", "sampled_out", "actor", "project",
    "inputs", "outputs", "touches",
    "steps", "events", "errors",
    "child_summaries", "meta",
})


def _to_otlp_span(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Translate one TraceAct record into an OTLP span dict.

    The span dict is the innermost element of the OTLP ExportTraceServiceRequest
    JSON payload. OtlpSink wraps it in the required resourceSpans → scopeSpans
    envelope before POSTing.

    Mapping summary:
      trace_id / parent_trace_id  → traceId / parentSpanId (MD5-hashed hex)
      action                      → span name
      kind                        → SpanKind (see _KIND_TO_OTLP)
      started_at / ended_at       → startTimeUnixNano / endTimeUnixNano
      status                      → OTel StatusCode (see _STATUS_TO_OTLP)
      steps                       → OTel span events (name="step")
      events                      → OTel span events (name=event.kind)
      errors                      → OTel span events (name="exception")
      inputs / outputs / touches  → span attributes (traceact.input.*, etc.)
      everything else             → span attributes (traceact.*)
    """
    trace_id = record.get("trace_id") or ""
    attrs: List[Dict[str, Any]] = []

    # -- Always-present TraceAct identity attributes -------------------------
    # Preserve original IDs so the two systems stay cross-referenceable even
    # when the hashed OTel IDs are the only thing visible in the collector UI.
    attrs.append(_otlp_attr("traceact.trace_id", trace_id))
    if record.get("root_trace_id"):
        attrs.append(_otlp_attr("traceact.root_trace_id", record["root_trace_id"]))
    if record.get("correlation_id"):
        attrs.append(_otlp_attr("traceact.correlation_id", record["correlation_id"]))
    # Cross-service lineage. Emitted as an attribute rather than an OTel
    # parentSpanId because the upstream span belongs to a different trace tree
    # in the collector; a span link would be the richer mapping, but an
    # attribute keeps the payload valid for every OTLP backend.
    if record.get("upstream_trace_id"):
        attrs.append(
            _otlp_attr("traceact.upstream_trace_id", record["upstream_trace_id"])
        )
    if record.get("actor"):
        attrs.append(_otlp_attr("traceact.actor", record["actor"]))
    if record.get("project"):
        attrs.append(_otlp_attr("traceact.project", record["project"]))
    if record.get("budget_hit"):
        attrs.append(_otlp_attr("traceact.budget_hit", True))
    # Emitted only when true, like budget_hit: a promoted failure record from
    # a sampled-out trace (empty steps/events by design).
    if record.get("sampled_out"):
        attrs.append(_otlp_attr("traceact.sampled_out", True))
    if record.get("duration_ms") is not None:
        attrs.append(_otlp_attr("traceact.duration_ms", record["duration_ms"]))

    # -- Inputs → traceact.input.* attributes --------------------------------
    for k, v in (record.get("inputs") or {}).items():
        if not isinstance(v, (dict, list)):
            attrs.append(_otlp_attr(f"traceact.input.{k}", v))

    # -- Outputs → traceact.output.* attributes ------------------------------
    for k, v in (record.get("outputs") or {}).items():
        if not isinstance(v, (dict, list)):
            attrs.append(_otlp_attr(f"traceact.output.{k}", v))

    # -- Touches → traceact.touch.N.* attributes -----------------------------
    for i, touch in enumerate(record.get("touches") or []):
        attrs.append(_otlp_attr(f"traceact.touch.{i}.kind",   touch.get("kind", "")))
        attrs.append(_otlp_attr(f"traceact.touch.{i}.target", touch.get("target", "")))

    # -- Safety net: unmapped top-level scalar fields → traceact.* ----------
    # Any scalar field added to the TraceAct schema in a future version
    # automatically appears here as a span attribute, so it's never silently
    # dropped while we decide whether it deserves an explicit mapping.
    for k, v in record.items():
        if k not in _EXPLICITLY_MAPPED and not isinstance(v, (dict, list)):
            attrs.append(_otlp_attr(f"traceact.{k}", v))

    # -- Steps → OTel span events (name="step") ------------------------------
    otlp_events: List[Dict[str, Any]] = []
    for step in (record.get("steps") or []):
        otlp_events.append({
            "name": "step",
            "timeUnixNano": str(_iso_to_nanos(
                step.get("recorded_at") or record.get("started_at")
            )),
            "attributes": [
                _otlp_attr("traceact.step.label", step.get("label", "")),
            ],
        })

    # -- TraceAct events → OTel span events ----------------------------------
    for ev in (record.get("events") or []):
        ev_attrs: List[Dict[str, Any]] = []
        for k, v in ev.items():
            # Skip already-captured structural fields; flatten scalars.
            if k in ("event_id", "kind", "started_at", "parent_event_id"):
                continue
            if isinstance(v, dict):
                # Flatten one level (e.g. result: {rows: 1} → traceact.event.result.rows)
                for sub_k, sub_v in v.items():
                    if not isinstance(sub_v, (dict, list)):
                        ev_attrs.append(_otlp_attr(f"traceact.event.result.{sub_k}", sub_v))
            elif not isinstance(v, list):
                ev_attrs.append(_otlp_attr(f"traceact.event.{k}", v))
        otlp_events.append({
            "name": ev.get("kind") or "event",
            "timeUnixNano": str(_iso_to_nanos(
                ev.get("started_at") or record.get("started_at")
            )),
            "attributes": ev_attrs,
        })

    # -- Errors → OTel exception events (OTel semantic conventions) ----------
    for err in (record.get("errors") or []):
        otlp_events.append({
            "name": "exception",
            "timeUnixNano": str(_iso_to_nanos(record.get("started_at"))),
            "attributes": [
                _otlp_attr("exception.type",    err.get("type", "Error")),
                _otlp_attr("exception.message", err.get("message", "")),
            ],
        })

    # -- Assemble the span ---------------------------------------------------
    span: Dict[str, Any] = {
        "traceId":          _trace_id_hex(trace_id),
        "spanId":           _span_id_hex(trace_id),
        "name":             record.get("action") or "unknown",
        "kind":             _KIND_TO_OTLP.get(record.get("kind") or "", _OTLP_KIND_INTERNAL),
        "startTimeUnixNano": str(_iso_to_nanos(record.get("started_at"))),
        "endTimeUnixNano":   str(_iso_to_nanos(
            record.get("ended_at") or record.get("started_at")
        )),
        "attributes": attrs,
        "events":     otlp_events,
        "status":     _STATUS_TO_OTLP.get(record.get("status") or "", {"code": 0}),
    }

    parent = record.get("parent_trace_id")
    if parent:
        span["parentSpanId"] = _span_id_hex(parent)

    return span


def _get_traceact_version() -> str:
    """Return the installed traceact version without a circular import."""
    try:
        from importlib.metadata import version
        return version("traceact")
    except Exception:
        return "0.0.0"


class OtlpSink:
    """
    Exports finished traces to any OTLP-compatible collector via HTTP/JSON.

    Uses stdlib urllib — zero additional dependencies.

    Point it at your collector's OTLP HTTP receiver (default port 4318):

        from traceact import OtlpSink, AsyncSink, configure

        configure(sinks=[AsyncSink([OtlpSink("http://localhost:4318")])])

    Works with any OTLP-compatible backend: Jaeger, Grafana Tempo, Honeycomb,
    Datadog agent, OpenTelemetry Collector, and others.

    For SaaS collectors that require authentication, pass your API key in
    headers:

        OtlpSink(
            "https://api.honeycomb.io",
            headers={"x-honeycomb-team": "<your-api-key>"},
        )

    **Always wrap in AsyncSink for production.** Each write() makes a
    synchronous HTTP request; without AsyncSink that latency hits every
    traced function call.

    Failed deliveries are counted in OtlpSink.failed — observable by choice,
    never silently dropped:

        if sink.failed > 0:
            logger.warning("OtlpSink: %d trace deliveries failed", sink.failed)

    Args:
        endpoint:
            Base URL of the OTLP HTTP receiver, without a path. Traces are
            posted to ``{endpoint}/v1/traces``.

        headers:
            Optional dict of extra request headers (API keys, auth tokens).

        timeout:
            Per-request timeout in seconds. Default: 5.0.

        resource_attributes:
            Optional dict of OTel resource attributes added to every exported
            span (e.g. {"service.name": "my-app", "deployment.env": "prod"}).
            If omitted, ``service.name`` defaults to ``"traceact"``.
    """

    def __init__(
        self,
        endpoint: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 5.0,
        resource_attributes: Optional[Dict[str, str]] = None,
    ) -> None:
        # Normalise: strip trailing slash so we can always append /v1/traces.
        self.endpoint = endpoint.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout

        # Build the OTel resource attribute list once — it's the same for
        # every span this sink exports.
        res_attrs = {"service.name": "traceact"}
        res_attrs.update(resource_attributes or {})
        self._resource = {
            "attributes": [_otlp_attr(k, v) for k, v in res_attrs.items()]
        }

        # Count of write() calls that resulted in a delivery failure.
        # Observable by choice — never silently hidden.
        self._failed = 0
        self._failed_lock = threading.Lock()

    @property
    def failed(self) -> int:
        """Number of trace deliveries that have failed so far."""
        with self._failed_lock:
            return self._failed

    def write(self, record: Dict[str, Any]) -> None:
        """
        Export one trace record to the collector.

        Translates the TraceAct record to an OTLP span, wraps it in the
        ExportTraceServiceRequest envelope, and POSTs to {endpoint}/v1/traces.

        Delivery failures are caught, counted in self.failed, and never
        propagated — a failing collector must not crash the application or
        kill an AsyncSink worker.
        """
        try:
            span = _to_otlp_span(record)
            payload = {
                "resourceSpans": [{
                    "resource": self._resource,
                    "scopeSpans": [{
                        "scope": {
                            "name":    "traceact",
                            "version": _get_traceact_version(),
                        },
                        "spans": [span],
                    }],
                }]
            }
            body = json.dumps(payload, default=str).encode("utf-8")
            url = f"{self.endpoint}/v1/traces"
            req = _urllib_request.Request(url, data=body, method="POST")
            req.add_header("Content-Type",   "application/json")
            req.add_header("Content-Length", str(len(body)))
            for name, value in self.headers.items():
                req.add_header(name, value)

            with _urllib_request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
            if status < 200 or status >= 300:
                self._record_failure()
        except Exception:
            # Catches network errors, timeouts, JSON serialisation failures,
            # and anything unexpected. All counted; none re-raised.
            self._record_failure()

    def _record_failure(self) -> None:
        """Increment the failure counter under its lock."""
        with self._failed_lock:
            self._failed += 1


# ---------------------------------------------------------------------------
# AsyncSink
# ---------------------------------------------------------------------------
#
# STATUS: Publicly exported from traceact/__init__.py as of v0.4.
#
# Why an async sink exists:
# Every other sink writes on the caller's thread. When a traced action finishes,
# it calls sink.write(record), and the application waits for that write to
# return before continuing. For a local file that is usually fast (the OS
# buffers the write), but for a slow or flaky sink — most importantly a network
# sink that posts traces to a remote collector — a blocking write would add the
# sink's latency directly onto the application's hot path. Tracing must never
# slow the code it observes, so this sink removes I/O from the caller entirely.
#
# How it works:
# AsyncSink wraps one or more inner sinks. When the application calls write(),
# the record is placed on an in-memory queue and write() returns immediately.
# A single background worker thread drains the queue and forwards each record
# to the inner sinks. The application's only cost is an enqueue; all real I/O
# happens off the hot path.
#
#     app thread ──write(record)──▶ [ queue ] ──▶ worker thread ──▶ inner sinks
#
# The three hard problems an async sink must answer, and the choices made here:
#
#   1. Backpressure — what happens when the queue is full (the app is producing
#      records faster than the worker can write them)? See the on_full policy.
#   2. Shutdown — buffered records must reach the sinks before the process
#      exits, or traces are silently lost. Handled via close() and an atexit
#      hook that flushes and stops the worker.
#   3. Fork safety — a background thread does not survive os.fork(). An app that
#      forks worker processes would end up with a dead worker in each child.
#      Handled with os.register_at_fork where available.

# A private sentinel object used to tell the worker thread to stop. It is placed
# on the queue by close(); when the worker pulls it, it drains anything left and
# exits. Using a unique object (rather than None) means it can never be confused
# with a real record.
_SHUTDOWN = object()


class AsyncSink:
    """
    A sink that performs all writes on a background thread, so the traced
    application never blocks on sink I/O.

    Wrap any other sink (or several) in an AsyncSink:

        AsyncSink([JsonlSink("data/traces.jsonl")])
        AsyncSink([JsonlSink(...), ConsoleSink()], max_queue=50000)

    Args:
        sinks:
            The inner sinks to forward records to. Each must implement
            write(record). The worker thread calls them one after another for
            every record, in the order given.

        max_queue:
            The maximum number of records held in memory waiting to be written.
            When this many records are already queued, the on_full policy
            decides what happens to the next one. A larger queue absorbs bigger
            bursts at the cost of more memory. Default: 10000.

        on_full:
            The backpressure policy when the queue is full. One of:

                "drop_newest" (default):
                    Discard the incoming record and count it as dropped. The
                    application never blocks and never loses already-queued
                    data. This honours the "tracing must not slow the app"
                    principle; the cost is that some traces are lost under
                    sustained overload. The dropped count is exposed via the
                    dropped property so this loss is observable, not silent.

                "drop_oldest":
                    Discard the oldest queued record to make room for the new
                    one. Favours recent data over old. Also never blocks.

                "block":
                    Block the calling thread until the worker frees space. This
                    guarantees zero loss but reintroduces latency on the hot
                    path, which is the very thing this sink exists to avoid. Use
                    only when losing a trace is worse than a brief stall.

    Notes:
        - The worker is a daemon thread, so it will not keep the interpreter
          alive on its own. Always rely on close() / atexit to flush cleanly.
        - Inner-sink exceptions are swallowed (like all sink writes) so a
          failing sink never crashes the worker or the application.
    """

    def __init__(
        self,
        sinks: List[Any],
        max_queue: int = 10000,
        on_full: str = "drop_newest",
    ) -> None:
        if on_full not in ("drop_newest", "drop_oldest", "block"):
            raise ValueError(
                "on_full must be 'drop_newest', 'drop_oldest', or 'block', "
                f"got {on_full!r}"
            )

        self.sinks = sinks
        self.max_queue = max_queue
        self.on_full = on_full

        # A bounded queue is the buffer between the app and the worker. Bounding
        # it is what makes backpressure possible — an unbounded queue would grow
        # without limit under overload and eventually exhaust memory.
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max_queue)

        # Number of records dropped due to the on_full policy.
        #
        # TraceAct's core principle is "X-ray vision for your code" — a silent
        # drop is blindness. This counter makes every dropped record observable:
        # callers can check it, log it, or expose it in a health endpoint. The
        # developer chooses whether to act on drops; the library never hides them.
        #
        # Guarded by its own lock because it is written by app threads (or the
        # worker for drop_oldest) and read by callers concurrently.
        self._dropped = 0
        self._dropped_lock = threading.Lock()

        # The worker thread and a flag marking whether we have started it. We
        # start lazily on the first write so that simply constructing an
        # AsyncSink (for example in a config that is never used) costs nothing.
        self._worker: Optional[threading.Thread] = None
        self._started = False
        self._start_lock = threading.Lock()
        # True once close() has run. A closed sink never restarts: a write()
        # after close() counts the record in `dropped` instead of silently
        # spawning a fresh worker (and re-registering the atexit hook) as an
        # earlier implementation did.
        self._closed = False

        # Register a fork handler so that a child process created via os.fork()
        # gets a fresh worker thread instead of inheriting a dead one. Not all
        # platforms provide register_at_fork (Windows does not), so guard it.
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._reinit_after_fork)

    # -- public API --------------------------------------------------------

    @property
    def dropped(self) -> int:
        """The number of records dropped so far under the on_full policy."""
        with self._dropped_lock:
            return self._dropped

    def write(self, record: Dict[str, Any]) -> None:
        """
        Enqueue a record for the worker thread to write. Returns immediately
        (unless on_full="block" and the queue is full).

        The worker is started on the first call, so an AsyncSink that is never
        written to never spawns a thread.

        A write after close() is counted in `dropped` and not enqueued — the
        sink stays closed rather than restarting its worker.
        """
        if self._closed:
            self._record_drop()
            return
        self._ensure_started()

        if self.on_full == "block":
            # Block until there is space. This is the only policy that can stall
            # the caller; it trades hot-path latency for zero loss.
            self._queue.put(record)
            return

        try:
            # Non-blocking enqueue. Raises queue.Full immediately if full.
            self._queue.put_nowait(record)
        except queue.Full:
            if self.on_full == "drop_oldest":
                # Make room by discarding the oldest queued record, then retry.
                # Both operations are best-effort: under heavy contention the
                # queue state can change between them, so we tolerate races
                # rather than lock the whole queue.
                try:
                    self._queue.get_nowait()
                    self._record_drop()
                    self._queue.put_nowait(record)
                    return
                except (queue.Empty, queue.Full):
                    self._record_drop()
                    return
            else:
                # on_full == "drop_newest": discard the incoming record.
                self._record_drop()

    def flush(self) -> None:
        """
        Block until every record queued so far has been handed to the inner
        sinks. Useful in tests and before a known shutdown point.

        This does not stop the worker; more records can be written afterward.
        """
        if self._started:
            self._queue.join()

    def close(self) -> None:
        """
        Flush all remaining records and stop the worker thread.

        Safe to call more than once. After close(), the sink is permanently
        closed: further write() calls are counted in `dropped` rather than
        restarting the worker. Registered with atexit on first start so that
        short-lived scripts flush automatically on exit.
        """
        self._closed = True
        if not self._started:
            return
        # Signal the worker to finish. It will drain any remaining real records
        # before it sees the sentinel and exits.
        self._queue.put(_SHUTDOWN)
        if self._worker is not None:
            self._worker.join()
        self._started = False

    # -- internals ---------------------------------------------------------

    def _ensure_started(self) -> None:
        """Start the worker thread once, on first write. Thread-safe."""
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            self._worker = threading.Thread(
                target=self._run,
                name="traceact-async-sink",
                daemon=True,
            )
            self._worker.start()
            self._started = True
            # Flush on interpreter exit so buffered records are not lost by a
            # script that never calls close() itself.
            atexit.register(self.close)

    def _run(self) -> None:
        """
        The worker loop. Pulls records off the queue and forwards each to every
        inner sink until it receives the shutdown sentinel.
        """
        while True:
            item = self._queue.get()
            try:
                if item is _SHUTDOWN:
                    # Stop looping. task_done in finally keeps join() correct.
                    return
                for sink in self.sinks:
                    try:
                        sink.write(item)
                    except Exception:
                        # A failing inner sink must never crash the worker or,
                        # by extension, the application. Tracing is observability
                        # tooling; it must not become a point of failure.
                        pass
            finally:
                # Mark the item done so flush()'s queue.join() can return once
                # the backlog is fully processed.
                self._queue.task_done()

    def _record_drop(self) -> None:
        """Increment the dropped-record counter under its lock."""
        with self._dropped_lock:
            self._dropped += 1

    def _reinit_after_fork(self) -> None:
        """
        Reset state in a freshly forked child process.

        The parent's worker thread does not exist in the child (fork copies only
        the calling thread), and the inherited queue may hold half-written state.
        We start clean: a new empty queue and no worker. The next write() in the
        child starts a fresh worker. Records that were queued in the parent at
        fork time stay with the parent to be written there.
        """
        self._queue = queue.Queue(maxsize=self.max_queue)
        self._worker = None
        self._started = False
        self._start_lock = threading.Lock()
        self._dropped_lock = threading.Lock()
