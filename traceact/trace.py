# trace.py
#
# Defines ActionTrace — the central object in TraceAct.
#
# An ActionTrace is the live record of one action as it executes. It accumulates
# steps, events, touches, inputs, outputs, and errors. When the action finishes,
# it serialises itself to a dict and writes to the configured sinks.
#
# How a trace is born:
# 1. A @traced_action decorator fires, or ActionTrace.start() is called.
# 2. The active trace is read from the ContextVar (context.py).
# 3. If there is an active trace, the new trace becomes a child of it.
# 4. If there is no active trace, the new trace becomes a root trace.
# 5. The new trace is pushed onto the ContextVar as the new active trace.
# 6. The wrapped function runs.
# 7. When the function exits (success or exception), the trace is finished.
# 8. The finished trace is written to sinks.
# 9. A compact child summary is sent to the parent trace (if one exists).
# 10. The ContextVar is restored to whatever it held before.
#
# How deduplication works:
# Touches and errors are stored in two forms:
#   - A public list (_touches, _errors) for readable output.
#   - An internal set (_touch_index, _error_index) for O(1) membership checks.
# When a new touch or error arrives, it is checked against the index first.
# If it is already there, nothing is added to the list. This prevents repeated
# events (e.g. hitting the same DB table 50 times) from filling the trace summary
# with redundant entries.
#
# How config and budget resolution works:
# Both TraceConfig and TraceBudget use None to mean "not specified." The
# _resolve_config() and _resolve_budget() functions apply a three-level merge:
#   Package defaults → package-level configure() → parent trace → local override
# The result is a fully resolved object with no None fields — an "_Effective"
# version that the trace can use directly without further checking.

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from traceact.budget import BUDGET_DEFAULTS, TraceBudget
from traceact.config import (
    TraceConfig,
    get_package_budget,
    get_package_config,
    get_package_sinks,
)
from traceact.context import SKIP, get_active_trace, is_skip, pop_trace, push_trace
from traceact.helpers import TraceHelpersMixin
from traceact.ids import new_event_id, new_step_id, new_trace_id
from traceact.redaction import REDACTION_PRESETS, SENSITIVE_PATTERNS
from traceact.sinks import ConsoleSink, buffer_record, flush_buffer


# ---------------------------------------------------------------------------
# Sensitive field name patterns
# ---------------------------------------------------------------------------
#
# When capture_inputs is enabled and redact_by_default is True, any argument
# whose name matches one of these patterns has its value replaced with the
# string "[redacted]" before being stored. This prevents passwords, tokens, and
# other secrets from appearing in trace output.
#
# Matching is case-insensitive and uses substring matching: "user_password"
# matches because "password" appears in it. SENSITIVE_PATTERNS (the always-on
# baseline) plus any opt-in REDACTION_PRESETS the caller selected are merged
# into one set by _resolve_config() and passed down as `patterns` here — see
# traceact/redaction.py for where both are defined.

def _is_sensitive(field_name: str, patterns: "frozenset" = SENSITIVE_PATTERNS) -> bool:
    """
    Return True if the field name matches a known sensitive pattern.

    Args:
        field_name: The argument or field name to check (e.g. "user_password").
        patterns:   The set of substrings to match against. Defaults to the
                    always-on baseline; callers pass the fully resolved set
                    (baseline + any active presets) from _EffectiveConfig.

    Returns:
        True if any pattern is a substring of the lowercased field name.
    """
    lower = field_name.lower()
    return any(pattern in lower for pattern in patterns)


# ---------------------------------------------------------------------------
# Event-kind to touch-kind mapping
# ---------------------------------------------------------------------------
#
# When an event is recorded, TraceAct automatically derives a touch from it if
# the event has a target. The touch's kind is more specific than the event's
# kind: a "db" event touching "notes" produces a touch with kind "db_table".
#
# This mapping defines those translations. Kinds not in the mapping fall back to
# using the event kind directly as the touch kind.

_EVENT_TO_TOUCH_KIND: Dict[str, str] = {
    "db":    "db_table",
    "http":  "http_endpoint",
    "file":  "file",
    "model": "model",
    "cache": "cache_key",
    "queue": "queue",
    "auth":  "auth_provider",
    "email": "email_service",
}


# ---------------------------------------------------------------------------
# _EffectiveConfig and _EffectiveBudget
# ---------------------------------------------------------------------------
#
# These are simple containers for fully resolved settings — every field is
# guaranteed to be non-None. They are internal and never exposed publicly.
# The resolution functions below produce them from the three-layer merge.

class _EffectiveConfig:
    """Fully resolved TraceConfig with no None fields."""
    __slots__ = (
        "enabled", "sink_mode", "strict",
        "redact_by_default", "capture_inputs", "capture_outputs",
        "redaction_patterns",
    )

    def __init__(
        self,
        enabled: bool,
        sink_mode: str,
        strict: bool,
        redact_by_default: bool,
        capture_inputs: Any,
        capture_outputs: bool,
        redaction_patterns: "frozenset",
    ) -> None:
        self.enabled = enabled
        self.sink_mode = sink_mode
        self.strict = strict
        self.redact_by_default = redact_by_default
        self.capture_inputs = capture_inputs
        self.capture_outputs = capture_outputs
        # Baseline SENSITIVE_PATTERNS plus any opt-in REDACTION_PRESETS named
        # in TraceConfig(redaction_presets=[...]). Only consulted when
        # redact_by_default is True.
        self.redaction_patterns = redaction_patterns


class _EffectiveBudget:
    """Fully resolved TraceBudget with no None fields."""
    __slots__ = (
        "max_events", "max_steps", "max_depth",
        "max_payload_bytes", "sample_rate", "always_trace_errors",
    )

    def __init__(
        self,
        max_events: int,
        max_steps: int,
        max_depth: int,
        max_payload_bytes: int,
        sample_rate: float,
        always_trace_errors: bool,
    ) -> None:
        self.max_events = max_events
        self.max_steps = max_steps
        self.max_depth = max_depth
        self.max_payload_bytes = max_payload_bytes
        self.sample_rate = sample_rate
        self.always_trace_errors = always_trace_errors


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _resolve_config(
    local_override: Optional[TraceConfig],
    parent: Optional["ActionTrace"],
) -> _EffectiveConfig:
    """
    Build a fully resolved _EffectiveConfig by merging (lowest to highest):
        1. Package defaults (hard-coded in this function)
        2. Package-level config from configure()
        3. Local override (from the @traced_action decorator or ActionTrace.start())

    Parent config is not currently inherited (config is package-scoped). Budget
    is the thing that inherits from parent to child. This may change in a future
    version.

    Args:
        local_override: The TraceConfig passed to the decorator or start(), if any.
        parent:         The parent ActionTrace, if this is a child trace.

    Returns:
        An _EffectiveConfig with every field filled in.
    """
    # Start with hard-coded defaults.
    enabled = True
    sink_mode = "buffered"
    strict = False
    redact_by_default = True
    capture_inputs: Any = False   # safe default: no automatic capture
    capture_outputs = True

    redaction_presets: Optional[List[str]] = None

    # Apply package-level config from configure(), if set.
    pkg = get_package_config()
    if pkg is not None:
        if pkg.enabled is not None:          enabled = pkg.enabled
        if pkg.sink_mode is not None:        sink_mode = pkg.sink_mode
        if pkg.strict is not None:           strict = pkg.strict
        if pkg.redact_by_default is not None: redact_by_default = pkg.redact_by_default
        if pkg.capture_inputs is not None:   capture_inputs = pkg.capture_inputs
        if pkg.capture_outputs is not None:  capture_outputs = pkg.capture_outputs
        if pkg.redaction_presets is not None: redaction_presets = pkg.redaction_presets

    # Apply local override from this specific decorator or start() call.
    if local_override is not None:
        if local_override.enabled is not None:          enabled = local_override.enabled
        if local_override.sink_mode is not None:        sink_mode = local_override.sink_mode
        if local_override.strict is not None:           strict = local_override.strict
        if local_override.redact_by_default is not None: redact_by_default = local_override.redact_by_default
        if local_override.capture_inputs is not None:   capture_inputs = local_override.capture_inputs
        if local_override.capture_outputs is not None:  capture_outputs = local_override.capture_outputs
        # Replaces (not merges with) the package-level list, same as every
        # other field here — a decorator that cares enough to override
        # presets is expected to name the full set it wants.
        if local_override.redaction_presets is not None: redaction_presets = local_override.redaction_presets

    # Safety: if the package-level config explicitly set capture_inputs=False,
    # that is the global kill switch and it cannot be overridden by a decorator.
    # We re-apply the package setting last to enforce this.
    if pkg is not None and pkg.capture_inputs is False:
        capture_inputs = False

    # Merge the always-on baseline with whichever presets are active. Preset
    # names are validated at TraceConfig construction time, so no further
    # validation is needed here.
    redaction_patterns = SENSITIVE_PATTERNS
    for preset_name in (redaction_presets or []):
        redaction_patterns = redaction_patterns | REDACTION_PRESETS[preset_name]

    return _EffectiveConfig(
        enabled=enabled,
        sink_mode=sink_mode,
        strict=strict,
        redact_by_default=redact_by_default,
        capture_inputs=capture_inputs,
        capture_outputs=capture_outputs,
        redaction_patterns=redaction_patterns,
    )


def _resolve_budget(
    local_override: Optional[TraceBudget],
    parent: Optional["ActionTrace"],
) -> _EffectiveBudget:
    """
    Build a fully resolved _EffectiveBudget by merging (lowest to highest):
        1. Package defaults (BUDGET_DEFAULTS from budget.py)
        2. Package-level budget from configure()
        3. Parent trace's effective budget (inherited by child traces)
        4. Local override (from the @traced_action decorator or ActionTrace.start())

    Child traces inherit the parent's budget because the parent may have been
    given a more generous budget (e.g. an agent loop with max_events=500).
    Without inheritance, every nested call would silently revert to the default
    100-event limit.

    Only non-None fields in each layer override the layer below. This means
    TraceBudget(max_events=300) overrides only max_events — all other fields
    come from the parent or package default.

    Args:
        local_override: The TraceBudget passed to the decorator or start(), if any.
        parent:         The parent ActionTrace, if this is a child trace.

    Returns:
        An _EffectiveBudget with every field filled in.
    """
    # Start with package defaults.
    values: Dict[str, Any] = dict(BUDGET_DEFAULTS)

    # Apply package-level budget from configure(), if set.
    pkg_budget = get_package_budget()
    if pkg_budget is not None:
        for field in ("max_events", "max_steps", "max_depth",
                      "max_payload_bytes", "sample_rate", "always_trace_errors"):
            v = getattr(pkg_budget, field, None)
            if v is not None:
                values[field] = v

    # Inherit from the parent trace's already-resolved budget.
    # This is why child traces see the same generous limits as their parent
    # without needing to repeat them in every decorator.
    if parent is not None and parent._effective_budget is not None:
        eb = parent._effective_budget
        values["max_events"] = eb.max_events
        values["max_steps"] = eb.max_steps
        values["max_depth"] = eb.max_depth
        values["max_payload_bytes"] = eb.max_payload_bytes
        values["sample_rate"] = eb.sample_rate
        values["always_trace_errors"] = eb.always_trace_errors

    # Apply local override — only the fields the caller explicitly set.
    if local_override is not None:
        for field in ("max_events", "max_steps", "max_depth",
                      "max_payload_bytes", "sample_rate", "always_trace_errors"):
            v = getattr(local_override, field, None)
            if v is not None:
                values[field] = v

    return _EffectiveBudget(**values)


# ---------------------------------------------------------------------------
# Payload safety helpers
# ---------------------------------------------------------------------------

def _safe_value(
    field_name: str,
    value: Any,
    max_bytes: int,
    redact: bool,
    patterns: "frozenset" = SENSITIVE_PATTERNS,
) -> Any:
    """
    Sanitise a single value before storing it in a trace record.

    Applies these safety rules, in order:
        1. Redaction: if the field name matches a sensitive pattern and
           redact=True, replace the value with "[redacted]". This short-
           circuits — a redacted value is never recursed into.
        2. Recursion: if the value is a dict, sanitise its keys the same way
           (so a field like "headers" or "request" doesn't shield a nested
           "authorization" or "password" key from redaction). If it's a list,
           recurse into any dict elements the same way; non-dict elements are
           left for rule 3/4 below.
        3. Size limit: if the JSON representation exceeds max_bytes, replace
           the value with a "[truncated: N chars]" summary.
        4. Serialisability: if the value cannot be JSON-serialised (e.g. a
           complex object), replace it with a "[TypeName]" summary.

    Args:
        field_name: The name of the field (used for redaction pattern matching).
        value:      The raw value to sanitise.
        max_bytes:  Maximum allowed size in bytes after JSON encoding.
        redact:     Whether to apply redaction rules.
        patterns:   The fully resolved set of sensitive-name substrings
                    (baseline + any active presets) to match field names
                    against, at this level and every nested level.

    Returns:
        The original value, "[redacted]", "[truncated: N chars]", or "[TypeName]".
    """
    # Rule 1: Redact sensitive fields.
    if redact and _is_sensitive(field_name, patterns):
        return "[redacted]"

    # Rule 2: Recurse into nested structures so a sensitive key isn't hidden
    # a level or two down inside a request body, config dict, etc.
    if isinstance(value, dict):
        value = _sanitise_dict(value, max_bytes, redact, patterns)
    elif isinstance(value, list):
        value = [
            _sanitise_dict(item, max_bytes, redact, patterns)
            if isinstance(item, dict) else item
            for item in value
        ]

    # Rule 3 & 4: Check serialisability and size. Runs on the already-
    # sanitised structure, so a "[redacted]" placeholder is tiny and never
    # itself triggers the size limit.
    try:
        serialised = json.dumps(value, default=str)
        byte_count = len(serialised.encode("utf-8"))
        if byte_count > max_bytes:
            return f"[truncated: {len(serialised)} chars]"
        # Re-parse so the stored value is always a JSON-native type (not an
        # object that will fail later during sink serialisation).
        return json.loads(serialised)
    except (TypeError, ValueError):
        # Value is not JSON-serialisable at all.
        return f"[{type(value).__name__}]"


def _sanitise_dict(
    data: Dict[str, Any],
    max_bytes: int,
    redact: bool,
    patterns: "frozenset" = SENSITIVE_PATTERNS,
) -> Dict[str, Any]:
    """Apply _safe_value to every key-value pair in a dict, recursively."""
    return {k: _safe_value(k, v, max_bytes, redact, patterns) for k, v in data.items()}


# ---------------------------------------------------------------------------
# _NoOpTrace
# ---------------------------------------------------------------------------
#
# A no-op object returned by ActionTrace.start() when tracing is disabled,
# sampled out, or the depth limit is exceeded. It implements the full public
# ActionTrace API but does nothing. This allows callers to use it safely in a
# with-block without checking for None.

class _NoOpTrace:
    """
    A silent stand-in for ActionTrace when tracing cannot run.

    Why does this exist?
    When ActionTrace.start() is called but tracing is disabled (or sampled out,
    or depth-exceeded), we could return None. But then every caller would need
    to guard:
        trace = ActionTrace.start(...)
        if trace:
            with trace as t:
                ...

    That is ugly and error-prone. Instead, we return a _NoOpTrace that safely
    accepts all the same calls and does nothing. The with-block still works:
        with ActionTrace.start(...) as trace:
            trace.step("...")   # silently ignored
    """

    def __enter__(self) -> "_NoOpTrace":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False  # do not suppress exceptions

    # All public ActionTrace methods are present as no-ops.
    def step(self, *args: Any, **kwargs: Any) -> None: pass
    def event(self, *args: Any, **kwargs: Any) -> None: pass
    def touch(self, *args: Any, **kwargs: Any) -> None: pass
    def input(self, *args: Any, **kwargs: Any) -> None: pass
    def output(self, *args: Any, **kwargs: Any) -> None: pass
    def set_meta(self, *args: Any, **kwargs: Any) -> None: pass
    def db(self, *args: Any, **kwargs: Any) -> None: pass
    def http(self, *args: Any, **kwargs: Any) -> None: pass
    def file(self, *args: Any, **kwargs: Any) -> None: pass
    def model(self, *args: Any, **kwargs: Any) -> None: pass


# ---------------------------------------------------------------------------
# ActionTrace
# ---------------------------------------------------------------------------

class ActionTrace(TraceHelpersMixin):
    """
    The live record of one action as it executes.

    ActionTrace accumulates steps, events, touches, inputs, outputs, and errors
    as the action runs. When the action finishes (successfully or with an error),
    the trace is serialised and written to the configured sinks.

    Two ways to create a trace:

    1. Decorator (preferred for most cases):
        @traced_action(action="note.create", kind="app")
        def create_note(title, body):
            ...

    2. Context manager (for manual, granular control):
        with ActionTrace.start(action="note.create", kind="app") as trace:
            trace.input({"title": title})
            trace.step("Validating input")
            trace.event(kind="db", operation="insert", target="notes")
            trace.output({"note_id": "note_123"})

    Both forms automatically detect and participate in parent-child nesting
    through the ContextVar in context.py.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        action: str,
        kind: str = "app",
        actor: Optional[str] = None,
        project: Optional[str] = None,
        parent: Optional["ActionTrace"] = None,
        correlation_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        depth: int = 0,
        effective_config: Optional[_EffectiveConfig] = None,
        effective_budget: Optional[_EffectiveBudget] = None,
    ) -> None:
        """
        Initialise a new trace. You should not call this directly — use
        ActionTrace.start() or @traced_action instead.

        Args:
            action:           The name of the action being traced (e.g. "note.create").
            kind:             The category of work (e.g. "app", "db", "http").
            actor:            Who or what initiated the action (e.g. "user", "cron").
            project:          The application or project name (for grouping in output).
            parent:           The parent ActionTrace, if this is a child trace.
            correlation_id:   A shared ID linking related traces across a workflow.
            meta:             Arbitrary developer-supplied metadata.
            depth:            Nesting depth (0 = root, 1 = first child, etc.).
            effective_config: Pre-resolved config (from _resolve_config).
            effective_budget: Pre-resolved budget (from _resolve_budget).
        """
        # --- Identity fields ---
        self.trace_id: str = new_trace_id()

        # root_trace_id always points to the first trace in the chain.
        # For a root trace, it points to itself.
        # For a child, it inherits the parent's root_trace_id.
        self.root_trace_id: str = (
            parent.root_trace_id if parent is not None else self.trace_id
        )
        self.parent_trace_id: Optional[str] = (
            parent.trace_id if parent is not None else None
        )
        self.correlation_id: Optional[str] = correlation_id

        # --- Descriptive fields ---
        self.project: Optional[str] = project
        self.action: str = action
        self.kind: str = kind
        self.actor: Optional[str] = actor

        # --- Status fields ---
        self.status: str = "running"
        self.budget_hit: bool = False

        # --- Timing fields ---
        self._started_at: datetime = datetime.now(timezone.utc)
        self.started_at: str = _iso(self._started_at)
        self.ended_at: Optional[str] = None
        self.duration_ms: Optional[float] = None

        # --- Record lists (public output) ---
        self._inputs: Dict[str, Any] = {}
        self._steps: List[Dict[str, Any]] = []
        self._events: List[Dict[str, Any]] = []
        self._touches: List[Dict[str, Any]] = []
        self._outputs: Dict[str, Any] = {}
        self._errors: List[Dict[str, Any]] = []
        self._child_summaries: List[Dict[str, Any]] = []
        self._meta: Dict[str, Any] = dict(meta) if meta else {}

        # --- Internal deduplication sets ---
        # These sets hold canonical string keys (e.g. "db_table:notes") and
        # allow O(1) checks before appending to the public lists. This prevents
        # a hot loop that hits the same DB table 1000 times from producing 1000
        # identical touch entries in the output.
        self._touch_index: set = set()
        self._error_index: set = set()

        # --- Budget counters ---
        # Tracked separately from the list lengths to avoid repeated len() calls.
        self._event_count: int = 0
        self._step_count: int = 0

        # --- Nesting ---
        self._depth: int = depth
        self._parent: Optional["ActionTrace"] = parent

        # --- Resolved settings ---
        self._effective_config: _EffectiveConfig = (
            effective_config or _resolve_config(None, parent)
        )
        self._effective_budget: _EffectiveBudget = (
            effective_budget or _resolve_budget(None, parent)
        )

        # --- Context management token ---
        # Stores the ContextVar token from push_trace() so we can restore the
        # previous active trace when this trace finishes.
        self._context_token: Any = None

    # ------------------------------------------------------------------
    # Factory (context manager entry point)
    # ------------------------------------------------------------------

    @classmethod
    def start(
        cls,
        action: str,
        kind: str = "app",
        actor: Optional[str] = None,
        project: Optional[str] = None,
        correlation_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        config: Optional[TraceConfig] = None,
        budget: Optional[TraceBudget] = None,
    ) -> Union["ActionTrace", _NoOpTrace]:
        """
        Create a trace for use as a context manager.

        ActionTrace.start() is the manual entry point. Use it when you want
        explicit control over what is recorded, rather than relying on the
        decorator to capture everything automatically.

        Usage:
            with ActionTrace.start(action="note.create", kind="app") as trace:
                trace.input({"title": title})
                trace.step("Validating input")
                trace.event(kind="db", operation="insert", target="notes")
                trace.output({"note_id": "note_123"})

        Returns:
            An ActionTrace (if tracing is active and not sampled out) or a
            _NoOpTrace (if tracing is disabled, sampled out, or depth exceeded).
            Both support the with-block interface safely.
        """
        return _create_trace(
            action=action,
            kind=kind,
            actor=actor,
            project=project,
            correlation_id=correlation_id,
            meta=meta,
            config_override=config,
            budget_override=budget,
        )

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "ActionTrace":
        """
        Enter the trace context. Sets this trace as the active trace in the
        ContextVar so that nested @traced_action calls know to create children
        rather than new roots.
        """
        self._context_token = push_trace(self)
        return self

    def __exit__(
        self,
        exc_type: Any,
        exc_val: Any,
        exc_tb: Any,
    ) -> bool:
        """
        Exit the trace context. Finishes the trace (success or failure) and
        restores the ContextVar to its previous value.

        Returns False so exceptions are never suppressed.
        """
        if exc_type is not None:
            # An exception escaped the with-block — the trace failed.
            self._finish(status="failed", error=exc_val)
        else:
            self._finish(status="completed")

        # Restore whatever was active before this trace started.
        if self._context_token is not None:
            pop_trace(self._context_token)
            self._context_token = None

        return False  # do not suppress the exception

    # ------------------------------------------------------------------
    # Public recording methods
    # ------------------------------------------------------------------

    def step(self, label: str) -> None:
        """
        Record a human-readable step marker on the trace timeline.

        Steps are flat markers — they are not structural parents of events.
        Think of them as scene headings in a film script: they say where the
        story is, while events record what technically happened.

        Example:
            trace.step("Validated input")
            trace.event(kind="db", operation="insert", target="notes")
            trace.step("Returned response")

        Args:
            label: A short, readable description of where the trace is
                   (e.g. "Saved note", "Called payment provider").
        """
        # Respect the step budget. Once hit, further steps are silently dropped
        # and budget_hit is flagged. The function continues running normally.
        if self._step_count >= self._effective_budget.max_steps:
            self.budget_hit = True
            return

        self._steps.append({
            "step_id": new_step_id(),
            "label": label,
            "recorded_at": _now_iso(),
        })
        self._step_count += 1

    def event(
        self,
        kind: str,
        operation: Optional[str] = None,
        target: Optional[str] = None,
        status: Optional[str] = None,
        duration_ms: Optional[float] = None,
        result: Optional[Any] = None,
        error: Optional[Any] = None,
        parent_event_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """
        Record a structured event — a specific operation that happened during
        the trace.

        Events are the machine-readable counterpart to steps. A step says
        "we are at the save stage"; an event says "a DB insert ran against
        the notes table and returned 1 row."

        When an event has a target, TraceAct automatically derives and records
        a touch for that resource.

        Args:
            kind:           The category of operation ("db", "http", "file",
                            "model", "cache", etc.).
            operation:      The specific operation ("insert", "post", "write", etc.).
            target:         The resource involved ("notes", "stripe", "data/notes.json").
            status:         "completed" or "failed". Defaults to "completed".
            duration_ms:    How long this specific operation took in milliseconds.
            result:         What the event produced (rows returned, response status, etc.).
            error:          An error dict or exception string if the event failed.
            parent_event_id: ID of a parent event if this event was caused by another.
            **kwargs:       Any additional fields to store with the event (database,
                            rows, tokens_in, tokens_out, etc.).

        Example:
            trace.event(kind="db", operation="insert", target="notes", rows=1)
        """
        # Respect the event budget.
        if self._event_count >= self._effective_budget.max_events:
            self.budget_hit = True
            return

        # Respect the depth budget.
        if self._depth > self._effective_budget.max_depth:
            self.budget_hit = True
            return

        # Apply payload safety to the result field.
        safe_result = None
        if result is not None:
            safe_result = _safe_value(
                "result",
                result,
                self._effective_budget.max_payload_bytes,
                self._effective_config.redact_by_default,
                self._effective_config.redaction_patterns,
            )

        # Build the event record. Extra kwargs (rows, database, tokens_in, etc.)
        # are stored directly on the event dict so callers can attach any fields
        # they need without TraceAct needing to enumerate them all.
        evt: Dict[str, Any] = {
            "event_id": new_event_id(),
            "parent_event_id": parent_event_id,
            "kind": kind,
            "action": self.action,
            "operation": operation,
            "target": target,
            "status": status or "completed",
            "started_at": _now_iso(),
            "ended_at": _now_iso(),
            "duration_ms": duration_ms,
            "result": safe_result,
            "error": error,
            "depth": self._depth,
        }

        # Merge any extra kwargs into the event dict.
        if kwargs:
            evt.update(kwargs)

        self._events.append(evt)
        self._event_count += 1

        # Auto-derive a touch from the event's target. The event kind is mapped
        # to a more specific touch kind (e.g. "db" → "db_table").
        if target:
            touch_kind = _EVENT_TO_TOUCH_KIND.get(kind, kind)
            self._add_touch(touch_kind, target)

        # If the event carries an error, add it to the trace-level error summary.
        if error:
            self._add_error(evt["event_id"], error)

    def touch(self, kind: str, target: str) -> None:
        """
        Record that a resource was involved in this trace.

        Touches record which things the trace made contact with: files, tables,
        endpoints, models, queues. They are deduplicated — touching the same
        resource multiple times produces only one touch entry.

        Most touches are derived automatically from events (see the event()
        method). Use trace.touch() for resources that an event does not cover —
        for example, a module that was loaded, a config file that was read, or
        a third-party service that was contacted without an explicit event.

        Args:
            kind:   The kind of resource ("db_table", "file", "http_endpoint",
                    "module", "config", etc.).
            target: The specific resource identifier ("notes", "data/config.json").

        Example:
            trace.touch(kind="file", target="data/config.json")
        """
        self._add_touch(kind, target)

    def input(self, data: Dict[str, Any]) -> None:
        """
        Record the inputs for this trace.

        Call this when you want to explicitly capture what came into the action.
        This is always available regardless of the capture_inputs config setting
        — the config only controls whether the decorator captures arguments
        automatically. Manual calls to trace.input() are always intentional and
        always recorded.

        Redaction and size limits still apply.

        Args:
            data: A dict of field names to values. For example:
                  {"title": "My note", "user_id": "user_123"}

        Example:
            trace.input({"title": title, "user_id": user_id})
        """
        sanitised = _sanitise_dict(
            data,
            self._effective_budget.max_payload_bytes,
            self._effective_config.redact_by_default,
            self._effective_config.redaction_patterns,
        )
        self._inputs.update(sanitised)

    def output(self, data: Dict[str, Any]) -> None:
        """
        Record the outputs of this trace — what the action produced.

        Args:
            data: A dict of field names to values. For example:
                  {"note_id": "note_123", "created": True}

        Example:
            trace.output({"note_id": note.id})
        """
        # Respect the capture_outputs setting.
        if not self._effective_config.capture_outputs:
            return

        sanitised = _sanitise_dict(
            data,
            self._effective_budget.max_payload_bytes,
            self._effective_config.redact_by_default,
            self._effective_config.redaction_patterns,
        )
        self._outputs.update(sanitised)

    def set_meta(self, key: str, value: Any) -> None:
        """
        Store arbitrary developer-supplied metadata on the trace.

        TraceAct stores meta fields but does not interpret them. Use them for
        anything that helps you understand a trace in context: release versions,
        feature flags, experiment IDs, environment names, etc.

        Args:
            key:   The metadata key (e.g. "release", "env", "experiment_id").
            value: Any JSON-serialisable value.

        Example:
            trace.set_meta("release", "v1.2")
            trace.set_meta("env", "production")
        """
        self._meta[key] = value

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add_touch(self, kind: str, target: str) -> None:
        """
        Add a touch to the deduped touch list.

        The touch index (a set) is checked first. If the touch has already been
        recorded, nothing is added. This keeps the public touches list clean
        even when the same resource is accessed hundreds of times.
        """
        key = f"{kind}:{target}"
        if key not in self._touch_index:
            self._touch_index.add(key)
            self._touches.append({"kind": kind, "target": target})

    def _add_error(self, event_id: str, error: Any) -> None:
        """
        Add an error to the deduped trace-level error summary.

        Errors are kept in two forms:
          - On the event itself (full detail, always present).
          - In the trace's _errors list (deduplicated summary for quick scanning).

        If the same error type and message appears multiple times (e.g. the same
        DB constraint violation fires in a retry loop), the event list captures
        every occurrence while the trace-level summary shows it once.

        The deduplication key is: "{error_type}:{message}".
        """
        # Normalise the error to a dict so the summary is consistent.
        if isinstance(error, Exception):
            error_type = type(error).__name__
            message = str(error)
        elif isinstance(error, dict):
            error_type = error.get("type", "Error")
            message = error.get("message", str(error))
        else:
            error_type = "Error"
            message = str(error)

        key = f"{error_type}:{message}"
        if key not in self._error_index:
            self._error_index.add(key)
            self._errors.append({
                "event_id": event_id,
                "type": error_type,
                "message": message,
            })

    def _make_child_summary(self) -> Dict[str, Any]:
        """
        Build the compact summary that this trace sends to its parent when it
        finishes.

        The summary contains enough information for the parent to:
          1. Understand what happened in this child.
          2. Merge this child's touches and errors into its own deduped sets.

        It deliberately omits the full event list, step list, and inputs/outputs
        to keep the parent trace record manageable.
        """
        return {
            "trace_id": self.trace_id,
            "action": self.action,
            "kind": self.kind,
            "status": self.status,
            "budget_hit": self.budget_hit,
            "duration_ms": self.duration_ms,
            "event_count": self._event_count,
            "step_count": self._step_count,
            "touches": list(self._touches),
            "errors": list(self._errors),
        }

    def _receive_child_summary(self, summary: Dict[str, Any]) -> None:
        """
        Accept a compact summary from a finished child trace and merge its
        touches and errors into this trace's own deduped sets.

        This is the upward propagation step. It runs once when a child trace
        finishes — not on every event — which keeps the propagation cost at
        O(summary size) rather than O(total events).
        """
        self._child_summaries.append(summary)

        # Merge touches from the child.
        for touch in summary.get("touches", []):
            self._add_touch(touch["kind"], touch["target"])

        # Merge errors from the child.
        for error in summary.get("errors", []):
            self._add_error(summary["trace_id"], error)

    def _finish(self, status: str, error: Optional[Any] = None) -> None:
        """
        Finalise the trace: record timing, status, top-level error (if any),
        push a summary to the parent, and write the record to sinks.

        Args:
            status: "completed", "failed", or "cancelled".
            error:  The exception that caused failure, if status is "failed".
        """
        # Record timing.
        ended = datetime.now(timezone.utc)
        self.ended_at = _iso(ended)
        self.duration_ms = round(
            (ended - self._started_at).total_seconds() * 1000, 3
        )
        self.status = status

        # If the trace failed, add the top-level error to the error summary.
        if error is not None:
            self._add_error(self.trace_id, error)

        # Push a compact summary to the direct parent (if this is a child trace).
        # The parent merges our touches and errors into its own sets.
        if self._parent is not None:
            summary = self._make_child_summary()
            self._parent._receive_child_summary(summary)

        # Write to sinks.
        self._write_to_sinks()

    def _write_to_sinks(self) -> None:
        """
        Serialise the trace to a dict and send it to the configured sinks,
        respecting the sink_mode setting.

        Modes:
          "blocking"  — write immediately to each sink.
          "buffered"  — add to the in-memory buffer; flush later.
          "disabled"  — do nothing.

        Sink failures are swallowed (unless strict=True) so that a broken sink
        never crashes the application being traced.
        """
        mode = self._effective_config.sink_mode

        if mode == "disabled":
            return

        sinks = get_package_sinks()

        # If no sinks have been configured, fall back to ConsoleSink so that
        # traces are visible without any setup. This is purely a developer
        # convenience — configure() with an explicit sink list overrides it.
        if not sinks:
            sinks = [ConsoleSink()]

        record = self.to_dict()

        if mode == "blocking":
            for sink in sinks:
                try:
                    sink.write(record)
                except Exception as e:
                    if self._effective_config.strict:
                        raise
                    # Swallow silently in non-strict mode.
                    _ = e

        elif mode == "buffered":
            # buffer_record also registers the atexit flush handler.
            buffer_record(record)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialise the trace to a plain Python dict suitable for JSON output.

        This is the canonical trace record shape. It matches the example in
        PRD section 43.
        """
        return {
            "trace_id": self.trace_id,
            "root_trace_id": self.root_trace_id,
            "parent_trace_id": self.parent_trace_id,
            "correlation_id": self.correlation_id,
            "project": self.project,
            "action": self.action,
            "kind": self.kind,
            "actor": self.actor,
            "status": self.status,
            "budget_hit": self.budget_hit,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "inputs": self._inputs,
            "steps": self._steps,
            "events": self._events,
            "touches": self._touches,
            "outputs": self._outputs,
            "errors": self._errors,
            "child_summaries": self._child_summaries,
            "meta": self._meta,
        }


# ---------------------------------------------------------------------------
# _create_trace — the shared factory used by both the decorator and start()
# ---------------------------------------------------------------------------

def _create_trace(
    action: str,
    kind: str = "app",
    actor: Optional[str] = None,
    project: Optional[str] = None,
    correlation_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    config_override: Optional[TraceConfig] = None,
    budget_override: Optional[TraceBudget] = None,
    operation: Optional[str] = None,
    target: Optional[str] = None,
    database: Optional[str] = None,
) -> Union[ActionTrace, _NoOpTrace]:
    """
    Shared factory for creating an ActionTrace (or returning a _NoOpTrace when
    tracing cannot run).

    Both the @traced_action decorator and ActionTrace.start() call this function.
    It centralises the checks that decide whether to create a real trace or skip:
        1. Is tracing enabled?
        2. Is the current context a skip sentinel (sampled-out parent)?
        3. Should this trace be sampled out?
        4. Would this trace exceed the depth limit?

    If the trace is created, it is not yet pushed onto the ContextVar — that
    happens in ActionTrace.__enter__() (for the context manager API) or directly
    in the decorator wrapper.

    Args:
        action, kind, actor, project, correlation_id, meta:
            Standard trace fields. See ActionTrace.__init__ for descriptions.
        config_override: Optional TraceConfig to override package settings.
        budget_override: Optional TraceBudget to override inherited limits.
        operation, target, database:
            If provided, an initial event is added to the trace using these
            values. This is how @traced_action(kind="db", operation="insert",
            target="notes") works — the operation and target appear on the first
            event, not on the trace root.

    Returns:
        An ActionTrace ready to be entered (via with or the decorator), or a
        _NoOpTrace if tracing cannot run.
    """
    # Auto-populate correlation_id from the propagation context when the caller
    # didn't supply one explicitly. This is how distributed propagation works:
    # the WSGI/ASGI middleware (or a manual propagate() block) sets
    # _INCOMING_TRACE_ID, and all traces started in that context inherit it.
    if correlation_id is None:
        from traceact.propagation import _INCOMING_TRACE_ID
        correlation_id = _INCOMING_TRACE_ID.get()

    # --- Check 1: is tracing enabled? ---
    # Resolve config first so we can check the enabled flag.
    current = get_active_trace()
    parent: Optional[ActionTrace] = current if isinstance(current, ActionTrace) else None

    effective_config = _resolve_config(config_override, parent)
    if not effective_config.enabled:
        return _NoOpTrace()

    # --- Check 2: are we inside a sampled-out parent? ---
    # If the ContextVar holds the SKIP sentinel, a parent was sampled out and
    # we must also skip. Return a _NoOpTrace so the function still runs.
    if is_skip(current):
        return _NoOpTrace()

    # --- Check 3: sampling decision ---
    effective_budget = _resolve_budget(budget_override, parent)
    import random

    # always_trace_errors cannot be applied here because we haven't run yet
    # and don't know if this call will fail. The sampling decision is final.
    if effective_budget.sample_rate < 1.0:
        if random.random() > effective_budget.sample_rate:
            # This trace is sampled out. Return a _NoOpTrace but also push SKIP
            # onto the ContextVar so nested calls inherit the skip decision.
            # The caller (decorator or context manager) is responsible for
            # pushing SKIP and restoring it. We flag it here so the caller knows.
            return _SkippedTrace()

    # --- Check 4: depth limit ---
    depth = (parent._depth + 1) if parent is not None else 0
    if depth > effective_budget.max_depth:
        return _NoOpTrace()

    # --- Create the real trace ---
    trace = ActionTrace(
        action=action,
        kind=kind,
        actor=actor,
        project=project,
        parent=parent,
        correlation_id=correlation_id,
        meta=meta,
        depth=depth,
        effective_config=effective_config,
        effective_budget=effective_budget,
    )

    # If the decorator passed operation and/or target, create an initial event.
    # These fields belong on the event, not the trace root — a trace can span
    # many operations, so putting one at the root would be misleading.
    if operation or target:
        extra: Dict[str, Any] = {}
        if database:
            extra["database"] = database
        trace.event(
            kind=kind,
            operation=operation,
            target=target,
            **extra,
        )

    return trace


class _SkippedTrace(_NoOpTrace):
    """
    A no-op trace marker that tells the decorator the trace was sampled out.

    The decorator needs to push SKIP onto the ContextVar when sampling drops a
    trace, so nested calls also skip. A plain _NoOpTrace is returned for cases
    where we do not need to push SKIP (e.g. tracing disabled, depth exceeded).
    _SkippedTrace is the signal that SKIP propagation is needed.
    """
    pass


# ---------------------------------------------------------------------------
# Timestamp utilities
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    """Format a datetime as an ISO 8601 UTC string with millisecond precision."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return _iso(datetime.now(timezone.utc))
