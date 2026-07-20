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
from typing import Any, Dict, List


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


def buffer_record(record: Dict[str, Any]) -> None:
    """
    Add a finished trace record to the in-memory buffer.
    Also ensures the atexit flush handler is registered.
    """
    _ensure_flush_registered()
    _buffer.append(record)


def flush_buffer(sinks: List[Any]) -> None:
    """
    Write all buffered records to each sink, then clear the buffer.

    Args:
        sinks: The list of sink objects to write to.

    This is safe to call multiple times. After flushing, the buffer is empty.
    Calling flush when the buffer is already empty does nothing.
    """
    if not _buffer:
        return
    for record in _buffer:
        for sink in sinks:
            try:
                sink.write(record)
            except Exception:
                # Sink failures never crash the application. Tracing is
                # observability tooling; it must never become a point of
                # failure for the code it is observing.
                pass
    _buffer.clear()


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

    Example:
        sinks=[JsonlSink("data/traces/traces.jsonl")]
    """

    def __init__(self, path: str) -> None:
        self.path = path
        # Ensure the parent directory exists so writes don't fail silently.
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)

    def write(self, record: Dict[str, Any]) -> None:
        """
        Append one trace record to the JSONL file.

        Args:
            record: A plain dict representing the finished trace. Must be
                    JSON-serialisable; non-serialisable values should have
                    been sanitised before reaching the sink.
        """
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


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
