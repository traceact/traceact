# propagation.py
#
# Distributed trace propagation: injecting and extracting trace context across
# service boundaries via HTTP headers.
#
# How it works:
#
#   Service A has an active trace. Before making an outbound request it calls
#   inject_headers() to stamp the current trace_id onto the request headers:
#
#       headers = inject_headers({"Content-Type": "application/json"})
#       requests.post(url, json=payload, headers=headers)
#
#   Service B receives the request. Its WSGI/ASGI middleware (or a manual call
#   to propagate()) extracts the header and sets a ContextVar for the duration
#   of the request:
#
#       with propagate(request.headers):
#           # All traces started here inherit correlation_id = Service A's trace_id
#           with ActionTrace.start(action="order.process") as trace:
#               ...  # trace.correlation_id == Service A's trace_id
#
#   That correlation_id is stored in every trace record and shown in the viewer
#   inspector. TraceLog.filter(correlation_id=...) queries it programmatically.
#
# Header name:
#   "traceact-trace-id" — lowercase, hyphenated. WSGI environ maps this to
#   HTTP_TRACEACT_TRACE_ID; ASGI scope["headers"] carries it as bytes.

import contextvars
from typing import Dict, Optional

# The ContextVar that carries an incoming propagated trace ID through the
# current async task or thread. Set by propagate() / middleware; read by
# _create_trace() in trace.py when no correlation_id was explicitly supplied.
_INCOMING_TRACE_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "traceact_incoming_trace_id", default=None
)

HEADER_NAME = "traceact-trace-id"


def extract_trace_id(headers: Dict[str, str]) -> Optional[str]:
    """
    Extract a TraceAct trace ID from an incoming HTTP headers dict.

    Checks both the canonical header name and its lowercased form to handle
    frameworks that normalise header names differently.

    Args:
        headers: A dict of header names to values (e.g. from Flask request.headers
                 or a plain dict). Keys are matched case-insensitively.

    Returns:
        The extracted trace ID string, or None if the header is absent.
    """
    if not isinstance(headers, dict):
        return None
    return headers.get(HEADER_NAME) or headers.get(HEADER_NAME.lower()) or None


def inject_headers(headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Return a headers dict with the TraceAct propagation header stamped in.

    Reads the currently active trace from the ContextVar and adds
    ``traceact-trace-id: <trace_id>`` to the returned dict. If no trace is
    active, the dict is returned unchanged.

    Args:
        headers: An existing headers dict to extend. A copy is made — the
                 original is not modified. Defaults to an empty dict.

    Returns:
        A new dict containing all original headers plus the propagation header
        (when a trace is active).

    Example::

        headers = inject_headers({"Content-Type": "application/json"})
        requests.post(url, json=payload, headers=headers)
    """
    from traceact.context import get_active_trace
    from traceact.trace import ActionTrace

    result = dict(headers) if headers else {}
    active = get_active_trace()
    if isinstance(active, ActionTrace):
        result[HEADER_NAME] = active.trace_id
    return result


class propagate:
    """
    Context manager that propagates an incoming trace ID to new traces.

    Any ``ActionTrace`` started inside the ``with`` block will have its
    ``correlation_id`` automatically set to the incoming trace ID, linking
    the two services' traces together.

    Usage (manual)::

        with propagate(request.headers):
            with ActionTrace.start(action="order.process") as trace:
                # trace.correlation_id == incoming service's trace_id
                ...

    Usage (automatic — see TraceActMiddleware / TraceActASGIMiddleware):
        The middleware calls ``propagate`` for every request, so you don't need
        to call it yourself in Flask / Django / FastAPI / Starlette apps.

    Args:
        headers: A headers dict from the incoming HTTP request. Both plain
                 dicts and framework header objects that behave like dicts are
                 supported. If the propagation header is absent, the context
                 manager is a no-op.
    """

    def __init__(self, headers: Dict[str, str]) -> None:
        self._trace_id: Optional[str] = extract_trace_id(headers)
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> "propagate":
        if self._trace_id is not None:
            self._token = _INCOMING_TRACE_ID.set(self._trace_id)
        return self

    def __exit__(self, *args: object) -> bool:
        if self._token is not None:
            _INCOMING_TRACE_ID.reset(self._token)
            self._token = None
        return False

    @property
    def incoming_trace_id(self) -> Optional[str]:
        """The trace ID extracted from the incoming headers, or None."""
        return self._trace_id
