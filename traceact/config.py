# config.py
#
# Defines TraceConfig — the settings object that controls how TraceAct behaves —
# and the package-level configure() / reset_config() functions.
#
# Why a separate config module?
# Configuration is shared across the whole package. Every trace, decorator, and
# sink needs to read the same settings. Centralising them here means there is one
# place to look when a setting needs to change, and one place to reset when a
# test has left state behind.
#
# How config inheritance works:
# TraceConfig uses None to mean "not specified". A None field means "inherit from
# the parent trace's config, or fall back to the package default." This allows a
# single decorator to override only the fields it cares about, rather than
# replacing the entire config object.
#
# The resolution order (lowest to highest priority) is:
#   Package defaults → package-level configure() → parent trace config → decorator override
#
# See trace.py for the _resolve_config() function that applies this ordering.

from typing import Any, List, Optional


class TraceConfig:
    """
    Controls the behaviour of TraceAct tracing.

    All fields default to None, which means "not specified — use the package
    default or inherit from the parent trace." Only fields you explicitly set
    will override the inherited value.

    Fields:
        enabled:
            Whether tracing is active. When False, @traced_action decorators
            run their wrapped function with almost no overhead — no trace is
            created, no context is touched, no sink is called. This is the
            global kill switch.

        sink_mode:
            Controls when traces are written to sinks.
            "blocking"  — write immediately when a trace finishes. Best for
                          local development and tests.
            "buffered"  — accumulate finished traces in memory and flush them
                          later (or on program exit). Best default for small apps.
            "disabled"  — never write anything, regardless of sink configuration.
                          Use as a performance kill switch that still leaves
                          decorators in place.

        strict:
            When True, any exception raised inside TraceAct's own tracing logic
            (not inside the function being traced) will propagate into the
            application. When False (default), tracing failures are silently
            swallowed so the app is never broken by its own observability layer.

        redact_by_default:
            When True, field names that match known sensitive patterns (such as
            "password", "token", "secret", "api_key") are replaced with the
            string "[redacted]" before being stored in inputs, events, or outputs.
            This applies to both automatic input capture and manual trace.event()
            calls.

        capture_inputs:
            Controls the automatic input-capture behaviour of @traced_action.

            None / not set  — use the package default, which is False (no capture).
            False           — disable automatic capture globally. When set at the
                              package level via configure(), individual decorators
                              cannot re-enable automatic capture. Manual
                              trace.input() calls still work.
            True            — capture all named function arguments automatically,
                              applying redaction and payload limits.
            list of strings — capture only the named arguments in the list.
                              This is the safest opt-in form: you choose exactly
                              which fields are recorded.

        capture_outputs:
            When True (default), calls to trace.output() are recorded on the
            trace. When False, trace.output() is a no-op.
    """

    def __init__(
        self,
        enabled: Optional[bool] = None,
        sink_mode: Optional[str] = None,
        strict: Optional[bool] = None,
        redact_by_default: Optional[bool] = None,
        capture_inputs: Any = None,
        capture_outputs: Optional[bool] = None,
    ) -> None:
        self.enabled = enabled
        self.sink_mode = sink_mode
        self.strict = strict
        self.redact_by_default = redact_by_default
        self.capture_inputs = capture_inputs
        self.capture_outputs = capture_outputs


# ---------------------------------------------------------------------------
# Package-level state
# ---------------------------------------------------------------------------
#
# These three variables hold the current package-wide configuration, budget,
# and list of sinks. They start as None / empty, which means "use defaults."
# configure() sets them. reset_config() clears them back to None / empty.
#
# Why module-level variables instead of a class?
# A module is itself a singleton in Python — imported once and cached. Module-
# level variables are the simplest, most readable way to hold package state
# without introducing a global object that callers must instantiate.

_package_config: Optional[TraceConfig] = None
_package_budget: Any = None          # Optional[TraceBudget] — typed as Any to avoid circular import
_package_sinks: List[Any] = []       # List[Sink]


def configure(
    config: Optional[TraceConfig] = None,
    budget: Any = None,
    sinks: Optional[List[Any]] = None,
) -> None:
    """
    Set package-wide defaults for all future traces.

    Call this once at application startup. Settings applied here become the
    baseline for every trace unless a specific trace or decorator overrides them.

    Args:
        config:  A TraceConfig instance. Only non-None fields are applied; the
                 rest keep their current values (or package defaults).
        budget:  A TraceBudget instance. Only non-None fields are applied.
        sinks:   A list of sink objects (JsonlSink, ConsoleSink, etc.). Replaces
                 the current sink list entirely.

    Example:
        configure(
            config=TraceConfig(enabled=True, sink_mode="buffered"),
            budget=TraceBudget(max_events=200),
            sinks=[JsonlSink("traces.jsonl")],
        )
    """
    global _package_config, _package_budget, _package_sinks

    if config is not None:
        _package_config = config

    if budget is not None:
        _package_budget = budget

    if sinks is not None:
        _package_sinks = list(sinks)


def reset_config() -> None:
    """
    Restore all package-level state to its initial defaults.

    Use this in test teardown to prevent one test's configure() call from
    leaking into the next test. reset_config() also clears the active trace
    ContextVar so no orphaned trace context carries over between tests.

    What it resets:
        - TraceConfig → package defaults (enabled, buffered, strict=False, etc.)
        - TraceBudget → package defaults
        - Sink list   → empty (no sinks)
        - Active trace ContextVar → None (no active trace)

    What it does NOT reset:
        - Traces that have already been written to a sink. Those are gone.
        - Files on disk. JsonlSink output is not deleted.
    """
    global _package_config, _package_budget, _package_sinks

    _package_config = None
    _package_budget = None
    _package_sinks = []

    # Clear the active trace context so tests start with a clean slate.
    # The deferred import avoids a circular dependency at module load time
    # (context.py does not import config.py).
    from traceact.context import _active_trace
    _active_trace.set(None)


def get_package_config() -> Optional[TraceConfig]:
    """Return the package-level TraceConfig, or None if not set."""
    return _package_config


def get_package_budget() -> Any:
    """Return the package-level TraceBudget, or None if not set."""
    return _package_budget


def get_package_sinks() -> List[Any]:
    """Return the current list of configured sinks."""
    return _package_sinks
