# context.py
#
# Manages the active trace using Python's contextvars.ContextVar.
#
# Why ContextVar?
# Python's contextvars module (introduced in 3.7) provides context-local
# storage that is automatically isolated across:
#   - Threads (each thread gets its own copy)
#   - asyncio Tasks (each task gets its own copy, inherited from its parent)
#   - Nested function calls (the ContextVar token mechanism allows exact restore)
#
# This makes it the right tool for tracking "which trace is currently running"
# without requiring the developer to pass a trace object through every function
# call manually.
#
# The public API never exposes the ContextVar directly. Developers interact with
# traces through @traced_action and ActionTrace.start(), not through this module.
#
# How the skip sentinel works:
# When a trace is sampled out (sample_rate < 1.0 decides to skip it), we still
# set the ContextVar — but we set it to the SKIP sentinel rather than to a real
# ActionTrace. Any nested @traced_action call checks the ContextVar first: if it
# sees SKIP, it also skips. This ensures that a sampled-out parent silently
# suppresses all its children without requiring any coordination between traces.
#
# Without the skip sentinel, a sampled-out parent would leave the ContextVar
# empty, and a nested @traced_action would see "no active parent" and create a
# new root trace — which would then appear in the sink without its parent, making
# the output confusing and incomplete.

from contextvars import ContextVar
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Skip sentinel
# ---------------------------------------------------------------------------
#
# A sentinel is a unique object used as a special signal value. We use a class
# rather than a string like "SKIP" so that nothing can accidentally match it.

class _SkipSentinel:
    """
    Marker placed in the ContextVar when a trace has been sampled out.

    Any code that reads the ContextVar and finds this object should treat the
    current execution context as "tracing suppressed" and do nothing.
    """
    __slots__ = ()

    def __repr__(self) -> str:
        return "<TraceAct: sampled out>"


# The single instance of the skip sentinel. This is what gets stored in the
# ContextVar when a trace is sampled out.
SKIP: _SkipSentinel = _SkipSentinel()


def is_skip(value: Any) -> bool:
    """Return True if the given value is the skip sentinel."""
    return isinstance(value, _SkipSentinel)


# ---------------------------------------------------------------------------
# The ContextVar
# ---------------------------------------------------------------------------
#
# This variable holds one of three things at any point in time:
#   None          — no active trace (we are outside any traced function)
#   ActionTrace   — a live trace that @traced_action or ActionTrace.start() created
#   SKIP          — we are inside a sampled-out trace; everything is suppressed
#
# The variable is module-level so it is truly global (one per interpreter), but
# ContextVar ensures each asyncio Task and each thread sees its own independent
# value. Two concurrent requests will never interfere with each other's active
# trace.

_active_trace: ContextVar[Optional[Any]] = ContextVar(
    "traceact_active_trace",
    default=None,
)


def get_active_trace() -> Optional[Any]:
    """
    Return whatever is currently stored in the active trace ContextVar.

    Returns:
        None           — no active trace
        ActionTrace    — the currently running trace
        SKIP sentinel  — we are inside a sampled-out parent
    """
    return _active_trace.get()


def push_trace(trace_or_skip: Any) -> Any:
    """
    Set a new value as the active trace and return a token for restoring the
    previous value later.

    Args:
        trace_or_skip: Either an ActionTrace or the SKIP sentinel.

    Returns:
        A ContextVar Token. Pass this to pop_trace() when the trace finishes
        to restore whatever was active before.

    How the token mechanism works:
        Python's ContextVar.set() returns a Token object that remembers the
        previous value. Calling ContextVar.reset(token) restores that exact
        previous value — even if something else has changed the ContextVar in
        between. This is what makes nested traces work correctly: each level
        saves its own token and restores independently.
    """
    return _active_trace.set(trace_or_skip)


def pop_trace(token: Any) -> None:
    """
    Restore the active trace to whatever it was before the matching push_trace()
    call.

    Args:
        token: The Token returned by the corresponding push_trace() call.

    This should always be called in a finally block so that the context is
    restored even if the traced function raises an exception.
    """
    _active_trace.reset(token)
