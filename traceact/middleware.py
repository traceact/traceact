# middleware.py
#
# WSGI and ASGI middleware for automatic distributed trace propagation.
#
# Both classes extract the TraceAct propagation headers from every incoming HTTP
# request and apply them for the duration of that request, so any ActionTrace
# started while handling it picks up:
#
#     traceact-trace-id        -> upstream_trace_id  (causal lineage)
#     traceact-correlation-id  -> correlation_id     (workflow grouping)
#
# WSGI (Flask, Django):
#
#     from traceact import TraceActMiddleware
#     app = Flask(__name__)
#     app.wsgi_app = TraceActMiddleware(app.wsgi_app)
#
# ASGI (FastAPI, Starlette):
#
#     from traceact import TraceActASGIMiddleware
#     app = FastAPI()
#     app.add_middleware(TraceActASGIMiddleware)
#
# Streaming responses (WSGI):
#
#   A WSGI application that returns a generator body — Flask's
#   stream_with_context(), Django's StreamingHttpResponse — has not produced any
#   of that body by the time the application callable returns. The server
#   iterates the returned iterable afterwards. Clearing the propagation context
#   in a plain `finally` around the app call would therefore clear it *before*
#   the streamed body runs, and any trace started while yielding chunks would
#   silently lose its context.
#
#   TraceActMiddleware avoids this by wrapping the returned iterable and
#   deferring the context reset to its close(), which PEP 3333 requires the
#   server to call once the request is complete (whether or not it completed
#   normally).
#
#   ASGI has no equivalent problem: `await app(scope, receive, send)` only
#   returns once the full response, streamed chunks included, has been sent.

from typing import Any, Callable, Iterable, List, Optional

from traceact.propagation import (
    HEADER_CORRELATION_ID,
    HEADER_TRACE_ID,
    _INCOMING_CORRELATION_ID,
    _INCOMING_TRACE_ID,
)


def _set_context(trace_id: Optional[str], correlation_id: Optional[str]) -> List[Any]:
    """
    Set BOTH propagation ContextVars for this request, returning reset tokens.

    Both are written on every request, even when the value is None. The reset
    normally happens when the response iterable is closed, but PEP 3333 does
    not guarantee every caller invokes close() (Werkzeug's own test client
    does not). Writing both vars unconditionally at the start of every request
    means whatever an unclosed previous request left behind is overwritten
    before any trace in the current request can be created.

    Returns a list of (contextvar, token) pairs for _reset_context().
    """
    return [
        (_INCOMING_TRACE_ID, _INCOMING_TRACE_ID.set(trace_id)),
        (_INCOMING_CORRELATION_ID, _INCOMING_CORRELATION_ID.set(correlation_id)),
    ]


def _reset_context(tokens: List[Any]) -> None:
    """
    Reset ContextVars from _set_context() tokens, newest first.

    A Token can only be reset in the Context that created it. A WSGI server
    iterates a response body on the same thread that called the application, so
    that holds in practice — but a server that hands the iterable to another
    thread would raise ValueError here, and a failed cleanup must never surface
    as a request error.
    """
    for var, token in reversed(tokens):
        try:
            var.reset(token)
        except ValueError:
            pass


class _ContextClosingIterable:
    """
    Wraps a WSGI response iterable, deferring the propagation-context reset to
    close() so the context stays live while a streamed body is produced.

    PEP 3333: "If the iterable returned by the application has a close() method,
    the server or gateway must call that method upon completion of the current
    request." This wrapper always exposes close(), so the reset always runs.
    """

    __slots__ = ("_inner", "_tokens", "_closed")

    def __init__(self, inner: Iterable[bytes], tokens: List[Any]) -> None:
        self._inner = inner
        self._tokens = tokens
        self._closed = False

    def __iter__(self) -> Any:
        return iter(self._inner)

    def close(self) -> None:
        # Idempotent: a server calling close() twice must not double-reset.
        if self._closed:
            return
        self._closed = True
        try:
            inner_close = getattr(self._inner, "close", None)
            if callable(inner_close):
                inner_close()
        finally:
            _reset_context(self._tokens)


class _ContextClosingIterableWithLen(_ContextClosingIterable):
    """
    Variant used when the wrapped body supports len().

    PEP 3333 lets a server treat a body of len() == 1 specially (send the one
    chunk with a Content-Length instead of chunked encoding). A wrapper that
    always hides __len__ would lose that for every propagated request, so the
    middleware picks this class when the underlying body has __len__ and the
    plain one when it doesn't — defining __len__ unconditionally is not an
    option, because len() raising from the inner body would break servers
    that only check hasattr.
    """

    __slots__ = ()

    def __len__(self) -> int:
        return len(self._inner)


class TraceActMiddleware:
    """
    WSGI middleware that applies incoming TraceAct propagation headers to every
    trace started during the request.

    Wrap your WSGI app once at startup::

        app.wsgi_app = TraceActMiddleware(app.wsgi_app)

    Django, in ``wsgi.py``::

        application = TraceActMiddleware(get_wsgi_application())

    Reads ``traceact-trace-id`` (which WSGI exposes as
    ``HTTP_TRACEACT_TRACE_ID``) and ``traceact-correlation-id`` (as
    ``HTTP_TRACEACT_CORRELATION_ID``) from ``environ``. When neither is present
    the middleware is fully transparent.

    Streaming-safe: the context is held until the response iterable is closed,
    so traces started while yielding a streamed body still see it.
    """

    _TRACE_KEY = "HTTP_" + HEADER_TRACE_ID.upper().replace("-", "_")
    _CORRELATION_KEY = "HTTP_" + HEADER_CORRELATION_ID.upper().replace("-", "_")

    def __init__(self, app: Callable) -> None:
        self._app = app

    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        trace_id = environ.get(self._TRACE_KEY) or None
        correlation_id = environ.get(self._CORRELATION_KEY) or None

        # Set unconditionally — including to None when no headers are present.
        # See _set_context() for why the no-header case must still write.
        tokens = _set_context(trace_id, correlation_id)
        try:
            result = self._app(environ, start_response)
        except BaseException:
            # The app failed before returning an iterable — nothing will be
            # streamed and nothing will call close(), so reset here.
            _reset_context(tokens)
            raise

        # Defer the reset to close(): the body may not have been produced yet.
        # Preserve len() support when the underlying body has it (see
        # _ContextClosingIterableWithLen).
        if hasattr(result, "__len__"):
            return _ContextClosingIterableWithLen(result, tokens)
        return _ContextClosingIterable(result, tokens)


class TraceActASGIMiddleware:
    """
    ASGI middleware that applies incoming TraceAct propagation headers to every
    trace started during the request.

    Add once at startup::

        app.add_middleware(TraceActASGIMiddleware)

    or wrap manually::

        app = TraceActASGIMiddleware(app)

    Reads ``traceact-trace-id`` and ``traceact-correlation-id`` from the ASGI
    ``scope["headers"]`` byte pairs. Header name matching is case-insensitive,
    as the HTTP spec requires. Websocket and lifespan scopes pass through
    untouched.
    """

    def __init__(self, app: Callable) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        trace_id, correlation_id = self._extract(scope.get("headers") or [])

        # Set unconditionally, matching the WSGI side: an ASGI app served from a
        # long-lived event loop must not inherit a previous request's context.
        tokens = _set_context(trace_id, correlation_id)
        try:
            # Awaiting the app covers the whole response, streamed chunks
            # included, so a plain finally is correct here.
            await self._app(scope, receive, send)
        finally:
            _reset_context(tokens)

    @staticmethod
    def _extract(headers: Iterable) -> Any:
        """Pull both propagation headers out of raw ASGI byte pairs."""
        trace_id: Optional[str] = None
        correlation_id: Optional[str] = None
        for pair in headers:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                continue
            key, value = pair
            if isinstance(key, (bytes, bytearray)):
                key = bytes(key).decode("latin-1", "replace")
            if isinstance(value, (bytes, bytearray)):
                value = bytes(value).decode("latin-1", "replace")
            lowered = str(key).lower()
            if lowered == HEADER_TRACE_ID:
                trace_id = str(value) or None
            elif lowered == HEADER_CORRELATION_ID:
                correlation_id = str(value) or None
        return trace_id, correlation_id
