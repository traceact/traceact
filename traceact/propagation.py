# propagation.py
#
# Distributed trace propagation: injecting and extracting trace context across
# service boundaries via HTTP headers.
#
# Two distinct things travel across the wire and must stay separate fields:
#
#   traceact-trace-id        The calling service's trace_id. On the receiving
#                            side this becomes `upstream_trace_id` — causal
#                            lineage, "that trace triggered me".
#
#   traceact-correlation-id  A business-level group ID shared by every trace in
#                            one logical workflow (one API request, one order,
#                            one job). Developer-assigned, passed through
#                            untouched. On the receiving side it stays
#                            `correlation_id`.
#
# They are separate fields because they answer separate questions. A trace can
# have an upstream parent in another service AND belong to a wider correlation
# group whose ID was assigned three hops earlier.
#
# How it works:
#
#   Service A has an active trace. Before an outbound request it calls
#   inject_headers() to stamp both values onto the request headers:
#
#       headers = inject_headers({"Content-Type": "application/json"})
#       requests.post(url, json=payload, headers=headers)
#
#   Service B receives the request. Its WSGI/ASGI middleware (or a manual
#   propagate() block) extracts both headers and sets them as ContextVars for
#   the duration of the request:
#
#       with propagate(request.headers):
#           with ActionTrace.start(action="order.process") as trace:
#               trace.upstream_trace_id   # Service A's trace_id
#               trace.correlation_id      # the shared workflow ID
#
# Header parsing:
#   HTTP header names are case-insensitive, and every framework reconstructs
#   them differently — Flask/Werkzeug and Django hand back Title-Case
#   ("Traceact-Trace-Id"), Starlette lowercases, ASGI delivers raw bytes.
#   _normalise_headers() flattens all of those to a lowercase str->str dict
#   before lookup, and accepts any mapping-like or pair-iterable object rather
#   than requiring a real dict. Passing `request.headers` straight through
#   works on every framework; so does dict(request.headers), a list of tuples,
#   or the raw ASGI bytes list.

import contextvars
from typing import Any, Dict, Optional

# ContextVars carrying incoming propagated values through the current thread or
# async task. Set by propagate() / middleware; read by _create_trace() in
# trace.py when the caller didn't supply the corresponding field explicitly.
_INCOMING_TRACE_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "traceact_incoming_trace_id", default=None
)
_INCOMING_CORRELATION_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "traceact_incoming_correlation_id", default=None
)

# Canonical (lowercase) header names. HTTP header names are case-insensitive on
# the wire; these are the forms we send, and the forms we compare against after
# normalising an incoming header collection.
HEADER_TRACE_ID = "traceact-trace-id"
HEADER_CORRELATION_ID = "traceact-correlation-id"

# Backwards-compatible alias. HEADER_NAME referred to the trace-id header before
# the correlation header existed; keep it working for anyone who imported it.
HEADER_NAME = HEADER_TRACE_ID


def _normalise_headers(headers: Any) -> Dict[str, str]:
    """
    Flatten any header collection into a lowercase-keyed ``str -> str`` dict.

    Accepts, and is tested against, all of these:

      * a plain ``dict`` in any casing (``{"Traceact-Trace-Id": "..."}``)
      * Werkzeug / Flask ``Headers``
      * Django ``HttpHeaders``
      * Starlette / FastAPI ``Headers``
      * ``requests`` ``CaseInsensitiveDict``
      * a list of ``(name, value)`` pairs
      * a raw ASGI ``[(b"name", b"value")]`` bytes list

    None of the framework header classes above subclass ``dict``, so an
    ``isinstance(headers, dict)`` gate would reject every one of them. We duck-
    type on ``.items()`` and fall back to iterating pairs.

    Bytes keys and values are decoded as latin-1, which is the encoding the HTTP
    spec (and ASGI) uses for header octets. Duplicate names collapse to the last
    occurrence. Anything unparseable yields an empty dict rather than raising —
    a malformed header collection must never break the traced request.
    """
    if headers is None:
        return {}

    items = None
    # Mapping-like (dict, Werkzeug/Django/Starlette Headers, CaseInsensitiveDict).
    if hasattr(headers, "items"):
        try:
            items = list(headers.items())
        except Exception:
            items = None
    # Pair-iterable (list of tuples, raw ASGI headers).
    if items is None:
        if isinstance(headers, (str, bytes)):
            return {}
        try:
            items = list(headers)
        except TypeError:
            return {}

    out: Dict[str, str] = {}
    for pair in items:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            continue
        key, value = pair
        if isinstance(key, (bytes, bytearray)):
            key = bytes(key).decode("latin-1", "replace")
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("latin-1", "replace")
        out[str(key).lower()] = str(value)
    return out


def extract_trace_id(headers: Any) -> Optional[str]:
    """
    Extract the upstream trace ID from an incoming header collection.

    Args:
        headers: Any header collection accepted by :func:`_normalise_headers` —
                 a framework header object, a dict in any casing, a list of
                 pairs, or raw ASGI bytes pairs.

    Returns:
        The upstream service's trace ID, or None when the header is absent or
        empty.
    """
    return _normalise_headers(headers).get(HEADER_TRACE_ID) or None


def extract_correlation_id(headers: Any) -> Optional[str]:
    """
    Extract the workflow correlation ID from an incoming header collection.

    Returns None when the header is absent or empty. See
    :func:`extract_trace_id` for the accepted header collection types.
    """
    return _normalise_headers(headers).get(HEADER_CORRELATION_ID) or None


def inject_headers(headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Return a headers dict with the TraceAct propagation headers stamped in.

    Adds two headers, both optional depending on what context exists:

      ``traceact-trace-id``        the active trace's ``trace_id``
      ``traceact-correlation-id``  the active trace's ``correlation_id``

    When there is no active trace, the current propagation context is forwarded
    instead, so a service that receives a traced request but doesn't trace a
    particular code path still passes the chain along rather than breaking it.

    Args:
        headers: An existing headers dict to extend. A copy is made — the
                 original is never modified. Defaults to an empty dict.

    Returns:
        A new dict with all original headers plus whichever propagation headers
        apply.

    Example::

        headers = inject_headers({"Content-Type": "application/json"})
        requests.post(url, json=payload, headers=headers)
    """
    from traceact.context import get_active_trace
    from traceact.trace import ActionTrace

    result: Dict[str, str] = dict(headers) if headers else {}

    active = get_active_trace()
    if isinstance(active, ActionTrace):
        result[HEADER_TRACE_ID] = active.trace_id
        if active.correlation_id:
            result[HEADER_CORRELATION_ID] = active.correlation_id
        return result

    # No active trace: forward whatever context this request arrived with so the
    # chain survives an untraced hop.
    incoming_trace = _INCOMING_TRACE_ID.get()
    if incoming_trace:
        result[HEADER_TRACE_ID] = incoming_trace
    incoming_corr = _INCOMING_CORRELATION_ID.get()
    if incoming_corr:
        result[HEADER_CORRELATION_ID] = incoming_corr
    return result


def inject_context(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Return a payload dict with the TraceAct propagation context stamped in —
    the queue-boundary counterpart of :func:`inject_headers`.

    A queue's message is its propagation mechanism: the worker that picks up
    a job runs in a different process with a fresh, empty context, so trace
    context has to travel as ordinary job data. Call this on the enqueue side
    and ship the result with the job:

        job = inject_context({"user_id": 42})
        queue.enqueue("export_report", **job)          # RQ-style
        task.delay(user_id=42, traceact_context=inject_context())  # Celery-style

    The stamped keys are the same names the HTTP headers use
    (``traceact-trace-id`` / ``traceact-correlation-id``), and the values are
    plain strings — the dict is JSON-safe and round-trips through any queue
    serialiser. On the worker side, either:

      * pass the dict as the reserved ``traceact_context`` keyword to a
        function decorated with ``@traced_action`` — the decorator consumes
        it (the function never sees the kwarg) and links the job's trace to
        the enqueuing trace via ``upstream_trace_id`` / ``correlation_id``; or
      * apply it manually: ``with propagate(job_context): ...`` — the same
        context manager that handles incoming HTTP headers accepts this dict
        unchanged.

    Like :func:`inject_headers`, when there is no active trace the current
    incoming propagation context is forwarded instead, so an untraced hop
    (an HTTP handler that only enqueues) still passes the chain along.

    Args:
        payload: An existing job payload dict to extend. A copy is made — the
                 original is never modified. Defaults to an empty dict.

    Returns:
        A new dict with all original payload keys plus whichever propagation
        keys apply. When no trace context exists at all, the payload is
        returned (as a copy) without extra keys — safe to call
        unconditionally.
    """
    # The header carrier and the job carrier are the same wire format: a
    # lowercase-keyed str->str mapping. Delegating keeps one source of truth
    # for what travels and how fallbacks work.
    return inject_headers(payload)


class propagate:
    """
    Context manager that applies incoming trace context to new traces.

    Any ``ActionTrace`` started inside the ``with`` block gets:

      * ``upstream_trace_id`` set to the calling service's ``trace_id``
      * ``correlation_id``    set to the incoming workflow correlation ID

    Either is applied only when the corresponding header was present, and an
    explicit ``correlation_id=`` passed to the trace always wins.

    Usage (manual)::

        with propagate(request.headers):
            with ActionTrace.start(action="order.process") as trace:
                ...

    Pass the framework's header object directly — ``request.headers`` works on
    Flask, Django, FastAPI and Starlette, as does a plain dict in any casing.

    Usage (automatic): see :class:`~traceact.middleware.TraceActMiddleware` and
    :class:`~traceact.middleware.TraceActASGIMiddleware`, which call this for
    every request so you don't have to.

    Args:
        headers: Any header collection accepted by :func:`_normalise_headers`.
                 When neither propagation header is present, the context
                 manager is a no-op.
    """

    def __init__(self, headers: Any) -> None:
        normalised = _normalise_headers(headers)
        self._trace_id: Optional[str] = normalised.get(HEADER_TRACE_ID) or None
        self._correlation_id: Optional[str] = (
            normalised.get(HEADER_CORRELATION_ID) or None
        )
        self._trace_token: Optional[contextvars.Token] = None
        self._corr_token: Optional[contextvars.Token] = None

    def __enter__(self) -> "propagate":
        if self._trace_id is not None:
            self._trace_token = _INCOMING_TRACE_ID.set(self._trace_id)
        if self._correlation_id is not None:
            self._corr_token = _INCOMING_CORRELATION_ID.set(self._correlation_id)
        return self

    def __exit__(self, *args: object) -> bool:
        if self._trace_token is not None:
            _INCOMING_TRACE_ID.reset(self._trace_token)
            self._trace_token = None
        if self._corr_token is not None:
            _INCOMING_CORRELATION_ID.reset(self._corr_token)
            self._corr_token = None
        return False

    @property
    def incoming_trace_id(self) -> Optional[str]:
        """The upstream trace ID from the incoming headers, or None."""
        return self._trace_id

    @property
    def incoming_correlation_id(self) -> Optional[str]:
        """The workflow correlation ID from the incoming headers, or None."""
        return self._correlation_id
