# middleware.py
#
# WSGI and ASGI middleware for automatic distributed trace propagation.
#
# Both classes extract the "traceact-trace-id" header from every incoming HTTP
# request and set _INCOMING_TRACE_ID for the duration of the request, so any
# ActionTrace started while handling that request automatically inherits the
# caller's trace ID as its correlation_id.
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

from traceact.propagation import HEADER_NAME, _INCOMING_TRACE_ID


class TraceActMiddleware:
    """
    WSGI middleware that extracts the TraceAct propagation header and sets
    ``correlation_id`` on all traces started during the request.

    Wrap your WSGI app once at startup::

        app.wsgi_app = TraceActMiddleware(app.wsgi_app)

    The middleware reads the ``traceact-trace-id`` header (which WSGI exposes
    as ``HTTP_TRACEACT_TRACE_ID`` in ``environ``) and sets the module-level
    ``_INCOMING_TRACE_ID`` ContextVar for the lifetime of the request. If the
    header is absent, the middleware is transparent.
    """

    # WSGI environ key for "traceact-trace-id":
    # HTTP headers are stored as HTTP_<UPPERCASE, hyphens→underscores>.
    _ENVIRON_KEY = "HTTP_" + HEADER_NAME.upper().replace("-", "_")

    def __init__(self, app: object) -> None:
        self._app = app

    def __call__(self, environ: dict, start_response: object) -> object:
        trace_id: str = environ.get(self._ENVIRON_KEY, "")
        if trace_id:
            token = _INCOMING_TRACE_ID.set(trace_id)
            try:
                return self._app(environ, start_response)
            finally:
                _INCOMING_TRACE_ID.reset(token)
        return self._app(environ, start_response)


class TraceActASGIMiddleware:
    """
    ASGI middleware that extracts the TraceAct propagation header and sets
    ``correlation_id`` on all traces started during the request.

    Add to your ASGI app once at startup::

        app.add_middleware(TraceActASGIMiddleware)

    or wrap manually::

        app = TraceActASGIMiddleware(app)

    The middleware reads the ``traceact-trace-id`` header from the ASGI
    ``scope["headers"]`` list (bytes key/value pairs) and sets the module-level
    ``_INCOMING_TRACE_ID`` ContextVar for the lifetime of the request. Websocket
    and lifespan scopes are passed through unchanged.
    """

    _HEADER_KEY = HEADER_NAME.encode()

    def __init__(self, app: object) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope.get("type") == "http":
            trace_id = self._extract(scope.get("headers", []))
            if trace_id:
                token = _INCOMING_TRACE_ID.set(trace_id)
                try:
                    await self._app(scope, receive, send)
                finally:
                    _INCOMING_TRACE_ID.reset(token)
                return
        await self._app(scope, receive, send)

    def _extract(self, headers: list) -> str:
        for key, value in headers:
            if key.lower() == self._HEADER_KEY:
                return value.decode("latin-1")
        return ""
