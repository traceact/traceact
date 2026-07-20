# budget.py
#
# Defines TraceBudget — the limits object that controls how much TraceAct
# records before stopping and flagging budget_hit on the trace.
#
# Why a separate budget from config?
# Config controls *behaviour* (is tracing on? what mode? strict or not?).
# Budget controls *volume* (how many events? how deep? what size payloads?).
# Keeping them separate makes it easy to tune one without touching the other,
# and makes the inherited-vs-overridden distinction clear at each trace boundary.
#
# How budget inheritance works:
# Like TraceConfig, TraceBudget uses None to mean "not specified." When a child
# trace specifies TraceBudget(max_events=300), only max_events is overridden —
# all other fields are inherited from the parent trace or the package default.
#
# This is key because a specific trace (say, an agent loop) might need more
# events than the default, but still wants the global max_depth and
# always_trace_errors settings to apply.

from typing import Optional


class TraceBudget:
    """
    Controls the recording limits for a trace.

    All fields default to None, which means "not specified — inherit from the
    parent trace or use the package default." See _resolve_budget() in trace.py
    for how inheritance is applied.

    Fields:
        max_events:
            Maximum number of events to record in a single trace. Once this
            limit is reached, further calls to trace.event() are silently
            dropped and budget_hit is set to True on the trace. The wrapped
            function continues to run normally.

        max_steps:
            Maximum number of steps to record. Same behaviour as max_events
            once the limit is hit.

        max_depth:
            Maximum nesting depth for child traces. If a @traced_action
            decorator would create a trace at depth > max_depth, the decorator
            behaves as if tracing is disabled for that call. The function still
            runs normally; it just produces no trace record.

        max_payload_bytes:
            Maximum size (in bytes, after JSON serialisation) of any single
            captured value — an input field, an event result field, or an
            output field. Values that exceed this are replaced with a
            "[truncated: N chars]" summary.

        sample_rate:
            The fraction of successful traces to record. 1.0 means record
            everything. 0.1 means record approximately 10% of traces. Failed
            traces (status="failed") are always recorded when always_trace_errors
            is True, regardless of sample_rate.

            Sampling happens before a trace object is created. If a trace is
            sampled out, the ContextVar is set to a skip sentinel so that any
            nested @traced_action calls are also skipped. Nothing is written.

        always_trace_errors:
            When True (default), failed traces are always recorded even if
            sample_rate would otherwise drop them. This ensures that errors are
            never silently lost because of sampling.
    """

    def __init__(
        self,
        max_events: Optional[int] = None,
        max_steps: Optional[int] = None,
        max_depth: Optional[int] = None,
        max_payload_bytes: Optional[int] = None,
        sample_rate: Optional[float] = None,
        always_trace_errors: Optional[bool] = None,
    ) -> None:
        self.max_events = max_events
        self.max_steps = max_steps
        self.max_depth = max_depth
        self.max_payload_bytes = max_payload_bytes
        self.sample_rate = sample_rate
        self.always_trace_errors = always_trace_errors


# ---------------------------------------------------------------------------
# Package-level defaults
# ---------------------------------------------------------------------------
#
# These are the values used when no package config and no trace-level override
# specifies a budget field. They are intentionally generous so that TraceAct
# works well in small local tools without any configuration at all.

BUDGET_DEFAULTS = {
    "max_events": 100,
    "max_steps": 50,
    "max_depth": 10,
    "max_payload_bytes": 8192,
    "sample_rate": 1.0,
    "always_trace_errors": True,
}
