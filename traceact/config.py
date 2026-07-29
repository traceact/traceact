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

from traceact.redaction import REDACTION_PRESETS


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
            "blocking"  — write immediately when a trace finishes. The
                          package default: traces appear in the sink (and the
                          viewer) the moment they complete.
            "buffered"  — accumulate finished traces in memory and flush them
                          later (or on program exit). Opt-in for hot paths
                          where per-trace write latency is unwelcome; note a
                          hard crash loses whatever is still buffered.
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

        redaction_presets:
            Named groups of extra field-name patterns to redact, layered on
            top of the always-on baseline (password, token, secret, api_key,
            auth, credential, credit_card, ssn, etc). Available presets:

                "api_keys"          — jwt, bearer, signing_key, encryption_key,
                                       hmac_key, master_key
                "http"              — cookie, session_id, csrf_token,
                                       x_forwarded_for, remote_addr, client_ip
                "filesystem_paths"  — path, filepath, dir, workdir, cwd, and
                                       similar fields that can leak a machine's
                                       username or directory layout
                "env_vars"          — env, environ, environment, dotenv

            See traceact.redaction.REDACTION_PRESETS for the exact set of
            patterns in each. Like every other field on TraceConfig, setting
            this at the decorator/trace level REPLACES the package-level list
            rather than merging with it.

            Matching is by field name (substring, case-insensitive), the same
            mechanism as the baseline set — not by scanning value content. A
            secret stored under a field name not covered by any active preset
            will not be caught.
    """

    def __init__(
        self,
        enabled: Optional[bool] = None,
        sink_mode: Optional[str] = None,
        strict: Optional[bool] = None,
        redact_by_default: Optional[bool] = None,
        capture_inputs: Any = None,
        capture_outputs: Optional[bool] = None,
        redaction_presets: Optional[List[str]] = None,
    ) -> None:
        if redaction_presets is not None:
            unknown = set(redaction_presets) - set(REDACTION_PRESETS)
            if unknown:
                raise ValueError(
                    f"Unknown redaction preset(s): {sorted(unknown)}. "
                    f"Available: {sorted(REDACTION_PRESETS)}"
                )

        self.enabled = enabled
        self.sink_mode = sink_mode
        self.strict = strict
        self.redact_by_default = redact_by_default
        self.capture_inputs = capture_inputs
        self.capture_outputs = capture_outputs
        self.redaction_presets = redaction_presets


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
_package_project: Optional[str] = None  # set via configure(project=...); stamped onto every trace


def configure(
    config: Optional[TraceConfig] = None,
    budget: Any = None,
    sinks: Optional[List[Any]] = None,
    project: Optional[str] = None,
) -> None:
    """
    Set package-wide defaults for all future traces.

    Call this once at application startup. Settings applied here become the
    baseline for every trace unless a specific trace or decorator overrides them.

    Args:
        config:   A TraceConfig instance. Only non-None fields are applied; the
                  rest keep their current values (or package defaults).
        budget:   A TraceBudget instance. Only non-None fields are applied.
        sinks:    A list of sink objects (JsonlSink, ConsoleSink, etc.). Replaces
                  the current sink list entirely.
        project:  The application or project name. Stamped onto every trace so
                  the viewer can label sources by their project rather than by
                  the sink's file path. Required in practice — traces written
                  without a project name emit a warning.

    Example:
        configure(
            project="my-app",
            config=TraceConfig(enabled=True, sink_mode="buffered"),
            budget=TraceBudget(max_events=200),
            sinks=[JsonlSink("traces.jsonl")],
        )
    """
    global _package_config, _package_budget, _package_sinks, _package_project

    if config is not None:
        _package_config = config

    if budget is not None:
        _package_budget = budget

    if sinks is not None:
        _package_sinks = list(sinks)

    if project is not None:
        _package_project = project


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
        - Distributed propagation ContextVars → None (no inbound context)

    What it does NOT reset:
        - Traces that have already been written to a sink. Those are gone.
        - Files on disk. JsonlSink output is not deleted.
    """
    global _package_config, _package_budget, _package_sinks, _package_project

    _package_config = None
    _package_budget = None
    _package_sinks = []
    _package_project = None

    # Discard any buffered records so one test's buffered traces cannot leak
    # into the next test's buffer state. Imported here to avoid a circular
    # import at module load time (sinks.py does not import config.py).
    from traceact.sinks import reset_buffer
    reset_buffer()

    # Clear the active trace context so tests start with a clean slate.
    # The deferred import avoids a circular dependency at module load time
    # (context.py does not import config.py).
    from traceact.context import _active_trace
    _active_trace.set(None)

    # Clear inbound propagation context too. A test that drives a WSGI app
    # through a caller that skips the PEP 3333 close() contract (Werkzeug's
    # test client does exactly this) would otherwise leave an upstream trace ID
    # set on the thread, silently attaching it to every trace in later tests.
    from traceact.propagation import (
        _INCOMING_CORRELATION_ID,
        _INCOMING_TRACE_ID,
    )
    _INCOMING_TRACE_ID.set(None)
    _INCOMING_CORRELATION_ID.set(None)


def get_package_config() -> Optional[TraceConfig]:
    """Return the package-level TraceConfig, or None if not set."""
    return _package_config


def get_package_budget() -> Any:
    """Return the package-level TraceBudget, or None if not set."""
    return _package_budget


def get_package_sinks() -> List[Any]:
    """Return the current list of configured sinks."""
    return _package_sinks


def get_package_project() -> Optional[str]:
    """Return the package-level project name, or None if not set."""
    return _package_project
