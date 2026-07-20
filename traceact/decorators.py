# decorators.py
#
# Defines @traced_action — the primary developer-facing API for TraceAct.
#
# Why a decorator?
# The decorator form is the most natural way to attach tracing to existing
# functions. It requires no changes to the function body and wraps the entire
# call lifecycle (start, input capture, success, error, finish) automatically.
# The developer focuses on what the function does; TraceAct handles recording
# the story.
#
# How the decorator works:
# 1. @traced_action(action="...", kind="...") returns a decorator.
# 2. That decorator wraps the target function in a wrapper.
# 3. The wrapper calls _create_trace() to build (or skip) a trace.
# 4. If the trace is a _SkippedTrace, SKIP is pushed onto the ContextVar and
#    the function runs without recording anything.
# 5. If the trace is a real ActionTrace, it is pushed onto the ContextVar as
#    the active trace, inputs are optionally captured, the function runs, and
#    the trace is finished on success or failure.
# 6. The ContextVar is always restored in a finally block.
#
# Async support:
# The decorator detects whether the wrapped function is a coroutine function
# (async def) and returns either a sync or async wrapper accordingly. The logic
# in both wrappers is identical — the only difference is the use of "await".
#
# Input capture:
# When capture_inputs is specified, the decorator calls _capture_args() to map
# the positional and keyword arguments to their parameter names via
# inspect.signature(), applies redaction and size limits, and stores the result
# on the trace's inputs dict. This never breaks the function call — if capture
# fails for any reason, it is silently skipped (unless strict=True).

import functools
import inspect
from typing import Any, Dict, List, Optional, Union

from traceact.budget import TraceBudget
from traceact.config import TraceConfig
from traceact.context import SKIP, pop_trace, push_trace
from traceact.trace import (
    ActionTrace,
    _NoOpTrace,
    _SkippedTrace,
    _create_trace,
    _is_sensitive,
    _safe_value,
)


def traced_action(
    action: str,
    kind: str = "app",
    actor: Optional[str] = None,
    project: Optional[str] = None,
    operation: Optional[str] = None,
    target: Optional[str] = None,
    database: Optional[str] = None,
    capture_inputs: Any = False,
    meta: Optional[Dict[str, Any]] = None,
    config: Optional[TraceConfig] = None,
    budget: Optional[TraceBudget] = None,
    correlation_id: Optional[str] = None,
) -> Any:
    """
    Decorator that traces the execution of a function.

    Attach this to any function — sync or async — to automatically record a
    trace whenever the function is called. The trace captures timing, status,
    errors, and optionally inputs.

    Args:
        action:
            The name of the action being traced. Use dot-notation to describe
            the action clearly. Examples: "note.create", "payment.authorise",
            "agent.run", "report.export".

        kind:
            The category of work. This tells readers what kind of system the
            function interacts with. Standard values: "app", "db", "http",
            "file", "model", "cache", "queue", "job", "auth", "payment",
            "email", "export". Default: "app".

        actor:
            Who or what triggered the action. Examples: "user", "cron",
            "webhook", "agent". Optional.

        project:
            The application or service this trace belongs to. Useful for
            grouping traces from multiple services in the same output.

        operation:
            For kind="db", "http", "file" etc. — the specific operation being
            performed (e.g. "insert", "post", "write"). When provided alongside
            target, an initial event is automatically created inside the trace.

        target:
            The resource the operation acts on (e.g. "notes", "stripe",
            "data/output.json"). Works alongside operation.

        database:
            For kind="db" traces — the database name or driver (e.g. "sqlite",
            "postgres"). Stored on the initial event alongside operation/target.

        capture_inputs:
            Controls whether function arguments are automatically recorded.
            False (default): no automatic capture.
            True: capture all named arguments (excluding self/cls), with
                  redaction and size limits applied.
            list of strings: capture only the named arguments in the list.
                             This is the safest and most explicit form.

        meta:
            Arbitrary key-value data to attach to the trace. TraceAct stores
            it but does not interpret it. Examples: {"release": "v1.2"}.

        config:
            A TraceConfig that overrides package-level settings for this trace
            only. Only non-None fields in the config are applied.

        budget:
            A TraceBudget that overrides inherited limits for this trace only.
            Only non-None fields in the budget are applied. Other fields are
            inherited from the parent trace or the package default.

        correlation_id:
            A shared ID to link this trace with other traces in the same
            logical workflow (e.g. the same API request, the same job, the
            same order). Useful for connecting traces across functions or
            services.

    Returns:
        A decorator that wraps the target function.

    Examples:

        Basic usage:
            @traced_action(action="note.create", kind="app")
            def create_note(title, body):
                ...

        With selective input capture:
            @traced_action(
                action="note.create",
                kind="app",
                capture_inputs=["title", "user_id"],
            )
            def create_note(title, body, user_id):
                ...

        Database function:
            @traced_action(
                action="note.save",
                kind="db",
                operation="insert",
                target="notes",
                database="sqlite",
            )
            def save_note(note):
                ...

        Async function:
            @traced_action(action="payment.authorise", kind="payment")
            async def authorise_payment(amount, currency):
                ...
    """
    def decorator(func: Any) -> Any:
        # Decide now (at decoration time, not call time) which wrapper to use.
        # This avoids an inspect.iscoroutinefunction() check on every call.
        if inspect.iscoroutinefunction(func):
            return _async_wrapper(
                func, action, kind, actor, project, operation, target,
                database, capture_inputs, meta, config, budget, correlation_id,
            )
        else:
            return _sync_wrapper(
                func, action, kind, actor, project, operation, target,
                database, capture_inputs, meta, config, budget, correlation_id,
            )

    return decorator


# ---------------------------------------------------------------------------
# Sync wrapper
# ---------------------------------------------------------------------------

def _sync_wrapper(
    func: Any,
    action: str,
    kind: str,
    actor: Optional[str],
    project: Optional[str],
    operation: Optional[str],
    target: Optional[str],
    database: Optional[str],
    capture_inputs: Any,
    meta: Optional[Dict[str, Any]],
    config: Optional[TraceConfig],
    budget: Optional[TraceBudget],
    correlation_id: Optional[str],
) -> Any:
    """
    Build and return a sync wrapper for the given function.

    The wrapper follows this flow on every call:
        1. Ask _create_trace() whether to create a real trace or skip.
        2. If skipped (sampled out): push SKIP onto ContextVar, run function, restore.
        3. If no-op (disabled / depth exceeded): run function with no context changes.
        4. If real trace: push trace, capture inputs, run function, finish, restore.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Build (or decide to skip) the trace. _create_trace handles all the
        # checks: enabled?, skip sentinel?, sampling?, depth limit?.
        trace_or_noop = _create_trace(
            action=action,
            kind=kind,
            actor=actor,
            project=project,
            correlation_id=correlation_id,
            meta=meta,
            config_override=config,
            budget_override=budget,
            operation=operation,
            target=target,
            database=database,
        )

        # Case 1: The trace was sampled out. We must push SKIP onto the
        # ContextVar so that nested @traced_action calls also skip.
        if isinstance(trace_or_noop, _SkippedTrace):
            token = push_trace(SKIP)
            try:
                return func(*args, **kwargs)
            finally:
                pop_trace(token)

        # Case 2: Tracing is disabled or depth exceeded. No context changes
        # needed — just run the function directly.
        if isinstance(trace_or_noop, _NoOpTrace):
            return func(*args, **kwargs)

        # Case 3: We have a real trace. Run the full tracing lifecycle.
        trace: ActionTrace = trace_or_noop

        # Push the trace onto the ContextVar. Any @traced_action calls made
        # inside func() will find this trace as their parent.
        token = push_trace(trace)

        try:
            # Optionally capture function arguments as trace inputs.
            # This happens before the function runs so inputs are recorded even
            # if the function raises immediately.
            if capture_inputs is not False:
                _capture_inputs(trace, func, args, kwargs, capture_inputs)

            # Run the actual function.
            result = func(*args, **kwargs)

            # Success path.
            trace._finish(status="completed")
            return result

        except Exception as exc:
            # Failure path. The exception is re-raised after the trace is
            # finished — TraceAct never suppresses exceptions.
            trace._finish(status="failed", error=exc)
            raise

        finally:
            # Always restore the ContextVar, regardless of success or failure.
            pop_trace(token)

    return wrapper


# ---------------------------------------------------------------------------
# Async wrapper
# ---------------------------------------------------------------------------

def _async_wrapper(
    func: Any,
    action: str,
    kind: str,
    actor: Optional[str],
    project: Optional[str],
    operation: Optional[str],
    target: Optional[str],
    database: Optional[str],
    capture_inputs: Any,
    meta: Optional[Dict[str, Any]],
    config: Optional[TraceConfig],
    budget: Optional[TraceBudget],
    correlation_id: Optional[str],
) -> Any:
    """
    Build and return an async wrapper for the given coroutine function.

    The logic is identical to _sync_wrapper except that the function is awaited.
    ContextVar is async-safe in Python 3.7+: each asyncio Task inherits a copy
    of the current context from the task that created it. Within a single Task,
    the ContextVar behaves like a call-stack variable.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        trace_or_noop = _create_trace(
            action=action,
            kind=kind,
            actor=actor,
            project=project,
            correlation_id=correlation_id,
            meta=meta,
            config_override=config,
            budget_override=budget,
            operation=operation,
            target=target,
            database=database,
        )

        # Sampled out — push SKIP, await, restore.
        if isinstance(trace_or_noop, _SkippedTrace):
            token = push_trace(SKIP)
            try:
                return await func(*args, **kwargs)
            finally:
                pop_trace(token)

        # Disabled / depth exceeded — just await.
        if isinstance(trace_or_noop, _NoOpTrace):
            return await func(*args, **kwargs)

        # Real trace.
        trace: ActionTrace = trace_or_noop
        token = push_trace(trace)

        try:
            if capture_inputs is not False:
                _capture_inputs(trace, func, args, kwargs, capture_inputs)

            result = await func(*args, **kwargs)
            trace._finish(status="completed")
            return result

        except Exception as exc:
            trace._finish(status="failed", error=exc)
            raise

        finally:
            pop_trace(token)

    return wrapper


# ---------------------------------------------------------------------------
# Input capture
# ---------------------------------------------------------------------------

def _capture_inputs(
    trace: ActionTrace,
    func: Any,
    args: tuple,
    kwargs: Dict[str, Any],
    capture_spec: Any,
) -> None:
    """
    Capture function arguments and record them on the trace as inputs.

    This is called before the function body runs, so inputs are always recorded
    even if the function raises immediately.

    Args:
        trace:        The ActionTrace to record inputs on.
        func:         The original (unwrapped) function.
        args:         Positional arguments passed to the function.
        kwargs:       Keyword arguments passed to the function.
        capture_spec: False (no capture), True (all args), or a list of field names.

    How it works:
        1. inspect.signature() gives us the function's parameter names.
        2. We map positional args to their names using that signature.
        3. We skip "self" and "cls" (the first param of methods).
        4. If capture_spec is a list, we filter to only those names.
        5. We apply redaction and size limits via the trace's config/budget.
        6. We call trace.input() with the sanitised dict.

    Failures are swallowed (in non-strict mode) so that a problem with
    input capture never breaks the function call.
    """
    try:
        # Get the parameter list from the function signature.
        sig = inspect.signature(func)
        param_names = list(sig.parameters.keys())

        # Remove "self" and "cls" — instance method receivers are not inputs.
        if param_names and param_names[0] in ("self", "cls"):
            param_names = param_names[1:]
            args = args[1:]

        # Build a dict mapping parameter names to their values.
        # Positional args are mapped by position; kwargs are merged in.
        bound: Dict[str, Any] = {}
        for i, val in enumerate(args):
            if i < len(param_names):
                bound[param_names[i]] = val
        bound.update(kwargs)

        # If capture_spec is a list, keep only the explicitly requested fields.
        # This is the safest form: the developer chose exactly what to record.
        if isinstance(capture_spec, list):
            bound = {k: v for k, v in bound.items() if k in capture_spec}

        # If capture_spec is True, keep everything (already in bound).
        # We do not need an elif here — the list branch has already filtered.

        if not bound:
            return

        # Delegate to trace.input(), which applies redaction and size limits.
        trace.input(bound)

    except Exception as exc:
        # Input capture should never crash the application.
        if trace._effective_config.strict:
            raise
        # In non-strict mode, silently skip the capture.
        _ = exc
