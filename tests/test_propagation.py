# tests/test_propagation.py
#
# Tests for distributed trace propagation.
#
# Header collections are tested in the form real callers actually pass them:
# Title-Case keys (what Werkzeug/Flask and Django reconstruct from the wire),
# and the actual header classes from Werkzeug, Starlette, Django, and requests
# rather than hand-built dicts shaped to match the implementation.

import asyncio
import threading
import unittest

from traceact import (
    ActionTrace,
    TraceActASGIMiddleware,
    TraceActMiddleware,
    TraceConfig,
    configure,
    extract_correlation_id,
    extract_trace_id,
    inject_headers,
    propagate,
    reset_config,
)
from traceact.propagation import (
    HEADER_CORRELATION_ID,
    HEADER_TRACE_ID,
    _INCOMING_CORRELATION_ID,
    _INCOMING_TRACE_ID,
    _normalise_headers,
)

TRACE_HEADER_TITLE = "Traceact-Trace-Id"
CORR_HEADER_TITLE = "Traceact-Correlation-Id"


def _make_sink():
    """Return an in-memory sink plus the list it records into."""
    records = []

    class _MemSink:
        def write(self, record):
            records.append(record)

    return _MemSink(), records


class _SinkTestCase(unittest.TestCase):
    """Base: blocking in-memory sink, config reset between tests."""

    def setUp(self):
        sink, self.records = _make_sink()
        configure(config=TraceConfig(sink_mode="blocking"), sinks=[sink])

    def tearDown(self):
        reset_config()

    @property
    def last(self):
        return self.records[-1]


# ---------------------------------------------------------------------------
# Header normalisation — the layer the original bug lived in
# ---------------------------------------------------------------------------

class TestNormaliseHeaders(unittest.TestCase):

    def test_lowercase_dict(self):
        out = _normalise_headers({"traceact-trace-id": "trc_a"})
        self.assertEqual(out["traceact-trace-id"], "trc_a")

    def test_title_case_dict(self):
        out = _normalise_headers({TRACE_HEADER_TITLE: "trc_a"})
        self.assertEqual(out["traceact-trace-id"], "trc_a")

    def test_upper_case_dict(self):
        out = _normalise_headers({"TRACEACT-TRACE-ID": "trc_a"})
        self.assertEqual(out["traceact-trace-id"], "trc_a")

    def test_list_of_pairs(self):
        out = _normalise_headers([(TRACE_HEADER_TITLE, "trc_a")])
        self.assertEqual(out["traceact-trace-id"], "trc_a")

    def test_bytes_pairs_asgi_style(self):
        out = _normalise_headers([(b"Traceact-Trace-Id", b"trc_a")])
        self.assertEqual(out["traceact-trace-id"], "trc_a")

    def test_none(self):
        self.assertEqual(_normalise_headers(None), {})

    def test_empty(self):
        self.assertEqual(_normalise_headers({}), {})

    def test_string_input_is_not_iterated_as_chars(self):
        self.assertEqual(_normalise_headers("not-headers"), {})

    def test_malformed_pairs_skipped(self):
        out = _normalise_headers([("a", "b"), ("bad",), "x", ("c", "d")])
        self.assertEqual(out, {"a": "b", "c": "d"})

    def test_unparseable_object_returns_empty(self):
        self.assertEqual(_normalise_headers(object()), {})


# ---------------------------------------------------------------------------
# extract_* against REAL framework header objects
# ---------------------------------------------------------------------------

class TestExtractAgainstRealFrameworks(unittest.TestCase):
    """
    Each framework below reconstructs header names its own way and none of the
    header classes subclass dict. These are the inputs the documented recipe
    actually produces.
    """

    def test_werkzeug_headers_object(self):
        from werkzeug.datastructures import Headers
        h = Headers([(TRACE_HEADER_TITLE, "trc_wz"), (CORR_HEADER_TITLE, "corr_wz")])
        self.assertFalse(isinstance(h, dict))
        self.assertEqual(extract_trace_id(h), "trc_wz")
        self.assertEqual(extract_correlation_id(h), "corr_wz")

    def test_werkzeug_headers_via_dict_call(self):
        # dict(request.headers) — what the original USAGE.md recipe wrote.
        from werkzeug.datastructures import Headers
        h = Headers([(TRACE_HEADER_TITLE, "trc_wz")])
        self.assertEqual(extract_trace_id(dict(h)), "trc_wz")

    def test_starlette_headers_object(self):
        from starlette.datastructures import Headers
        h = Headers(raw=[(b"traceact-trace-id", b"trc_st")])
        self.assertEqual(extract_trace_id(h), "trc_st")

    def test_django_http_headers(self):
        from django.http.request import HttpHeaders
        h = HttpHeaders({"HTTP_TRACEACT_TRACE_ID": "trc_dj"})
        self.assertEqual(extract_trace_id(h), "trc_dj")

    def test_requests_case_insensitive_dict(self):
        from requests.structures import CaseInsensitiveDict
        h = CaseInsensitiveDict({TRACE_HEADER_TITLE: "trc_rq"})
        self.assertEqual(extract_trace_id(h), "trc_rq")

    def test_flask_request_headers_end_to_end(self):
        """A real Flask request object, not a simulated one."""
        from flask import Flask, request
        app = Flask(__name__)
        captured = {}

        @app.route("/", methods=["GET"])
        def index():
            captured["direct"] = extract_trace_id(request.headers)
            captured["dict"] = extract_trace_id(dict(request.headers))
            captured["corr"] = extract_correlation_id(request.headers)
            return "ok"

        client = app.test_client()
        client.get("/", headers={
            TRACE_HEADER_TITLE: "trc_flask",
            CORR_HEADER_TITLE: "corr_flask",
        })
        self.assertEqual(captured["direct"], "trc_flask")
        self.assertEqual(captured["dict"], "trc_flask")
        self.assertEqual(captured["corr"], "corr_flask")


class TestExtractBasics(unittest.TestCase):

    def test_absent_returns_none(self):
        self.assertIsNone(extract_trace_id({"Content-Type": "application/json"}))

    def test_empty_value_returns_none(self):
        self.assertIsNone(extract_trace_id({TRACE_HEADER_TITLE: ""}))

    def test_none_returns_none(self):
        self.assertIsNone(extract_trace_id(None))

    def test_correlation_independent_of_trace(self):
        self.assertIsNone(extract_trace_id({CORR_HEADER_TITLE: "corr_only"}))
        self.assertEqual(
            extract_correlation_id({CORR_HEADER_TITLE: "corr_only"}), "corr_only"
        )


# ---------------------------------------------------------------------------
# inject_headers
# ---------------------------------------------------------------------------

class TestInjectHeaders(_SinkTestCase):

    def test_no_context_returns_unchanged(self):
        result = inject_headers({"Content-Type": "application/json"})
        self.assertNotIn(HEADER_TRACE_ID, result)
        self.assertEqual(result["Content-Type"], "application/json")

    def test_active_trace_injects_trace_id(self):
        with ActionTrace.start(action="svc.call") as trace:
            result = inject_headers()
        self.assertEqual(result[HEADER_TRACE_ID], trace.trace_id)

    def test_injects_correlation_when_set(self):
        with ActionTrace.start(action="svc.call", correlation_id="corr_wf") as trace:
            result = inject_headers()
        self.assertEqual(result[HEADER_TRACE_ID], trace.trace_id)
        self.assertEqual(result[HEADER_CORRELATION_ID], "corr_wf")

    def test_omits_correlation_when_unset(self):
        with ActionTrace.start(action="svc.call"):
            result = inject_headers()
        self.assertNotIn(HEADER_CORRELATION_ID, result)

    def test_original_headers_preserved_and_unmutated(self):
        original = {"Authorization": "Bearer tok"}
        with ActionTrace.start(action="svc.call"):
            result = inject_headers(original)
        self.assertEqual(result["Authorization"], "Bearer tok")
        self.assertIn(HEADER_TRACE_ID, result)
        self.assertNotIn(HEADER_TRACE_ID, original)

    def test_forwards_context_when_no_active_trace(self):
        """An untraced hop must not break the chain."""
        headers = {TRACE_HEADER_TITLE: "trc_up", CORR_HEADER_TITLE: "corr_wf"}
        with propagate(headers):
            result = inject_headers()
        self.assertEqual(result[HEADER_TRACE_ID], "trc_up")
        self.assertEqual(result[HEADER_CORRELATION_ID], "corr_wf")

    def test_active_trace_overrides_forwarded_trace_id(self):
        with propagate({TRACE_HEADER_TITLE: "trc_up"}):
            with ActionTrace.start(action="svc.call") as trace:
                result = inject_headers()
        self.assertEqual(result[HEADER_TRACE_ID], trace.trace_id)
        self.assertNotEqual(result[HEADER_TRACE_ID], "trc_up")


# ---------------------------------------------------------------------------
# propagate — and the upstream_trace_id / correlation_id separation
# ---------------------------------------------------------------------------

class TestPropagate(_SinkTestCase):

    def test_upstream_trace_id_set_not_correlation(self):
        """The core semantic fix: a trace id must not land in correlation_id."""
        with propagate({TRACE_HEADER_TITLE: "trc_up"}):
            with ActionTrace.start(action="downstream.op"):
                pass
        self.assertEqual(self.last["upstream_trace_id"], "trc_up")
        self.assertIsNone(self.last["correlation_id"])

    def test_correlation_passes_through_untouched(self):
        with propagate({CORR_HEADER_TITLE: "corr_wf"}):
            with ActionTrace.start(action="downstream.op"):
                pass
        self.assertEqual(self.last["correlation_id"], "corr_wf")
        self.assertIsNone(self.last["upstream_trace_id"])

    def test_both_headers_populate_both_fields(self):
        headers = {TRACE_HEADER_TITLE: "trc_up", CORR_HEADER_TITLE: "corr_wf"}
        with propagate(headers):
            with ActionTrace.start(action="downstream.op"):
                pass
        self.assertEqual(self.last["upstream_trace_id"], "trc_up")
        self.assertEqual(self.last["correlation_id"], "corr_wf")

    def test_upstream_correlation_is_not_lost(self):
        """upstream_trace_id and correlation_id must not overwrite each other."""
        headers = {TRACE_HEADER_TITLE: "trc_up", CORR_HEADER_TITLE: "corr_original"}
        with propagate(headers):
            with ActionTrace.start(action="downstream.op"):
                pass
        self.assertEqual(self.last["correlation_id"], "corr_original")

    def test_works_with_title_case_headers(self):
        with propagate({TRACE_HEADER_TITLE: "trc_title"}):
            with ActionTrace.start(action="op"):
                pass
        self.assertEqual(self.last["upstream_trace_id"], "trc_title")

    def test_works_with_werkzeug_headers(self):
        from werkzeug.datastructures import Headers
        with propagate(Headers([(TRACE_HEADER_TITLE, "trc_wz")])):
            with ActionTrace.start(action="op"):
                pass
        self.assertEqual(self.last["upstream_trace_id"], "trc_wz")

    def test_no_headers_no_fields(self):
        with propagate({}):
            with ActionTrace.start(action="solo.op"):
                pass
        self.assertIsNone(self.last["upstream_trace_id"])
        self.assertIsNone(self.last["correlation_id"])

    def test_explicit_correlation_id_wins(self):
        with propagate({CORR_HEADER_TITLE: "corr_incoming"}):
            with ActionTrace.start(action="op", correlation_id="corr_explicit"):
                pass
        self.assertEqual(self.last["correlation_id"], "corr_explicit")

    def test_explicit_upstream_id_wins(self):
        with propagate({TRACE_HEADER_TITLE: "trc_incoming"}):
            with ActionTrace.start(action="op", upstream_trace_id="trc_explicit"):
                pass
        self.assertEqual(self.last["upstream_trace_id"], "trc_explicit")

    def test_context_cleared_after_exit(self):
        with propagate({TRACE_HEADER_TITLE: "t", CORR_HEADER_TITLE: "c"}):
            pass
        self.assertIsNone(_INCOMING_TRACE_ID.get())
        self.assertIsNone(_INCOMING_CORRELATION_ID.get())

    def test_context_cleared_on_exception(self):
        try:
            with propagate({TRACE_HEADER_TITLE: "t", CORR_HEADER_TITLE: "c"}):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertIsNone(_INCOMING_TRACE_ID.get())
        self.assertIsNone(_INCOMING_CORRELATION_ID.get())

    def test_nested_propagate_restores_outer(self):
        with propagate({TRACE_HEADER_TITLE: "trc_outer"}):
            with propagate({TRACE_HEADER_TITLE: "trc_inner"}):
                self.assertEqual(_INCOMING_TRACE_ID.get(), "trc_inner")
            self.assertEqual(_INCOMING_TRACE_ID.get(), "trc_outer")

    def test_child_traces_inherit_context(self):
        with propagate({TRACE_HEADER_TITLE: "trc_up"}):
            with ActionTrace.start(action="parent.op"):
                with ActionTrace.start(action="child.op"):
                    pass
        for rec in self.records:
            self.assertEqual(rec["upstream_trace_id"], "trc_up")

    def test_properties_expose_both(self):
        ctx = propagate({TRACE_HEADER_TITLE: "trc_p", CORR_HEADER_TITLE: "corr_p"})
        self.assertEqual(ctx.incoming_trace_id, "trc_p")
        self.assertEqual(ctx.incoming_correlation_id, "corr_p")

    def test_thread_isolation(self):
        results = {}

        def thread_a():
            with propagate({TRACE_HEADER_TITLE: "trc_thread_a"}):
                import time
                time.sleep(0.02)
                results["a"] = _INCOMING_TRACE_ID.get()

        def thread_b():
            import time
            time.sleep(0.005)
            results["b"] = _INCOMING_TRACE_ID.get()

        ta, tb = threading.Thread(target=thread_a), threading.Thread(target=thread_b)
        ta.start(); tb.start()
        ta.join(); tb.join()

        self.assertEqual(results["a"], "trc_thread_a")
        self.assertIsNone(results["b"])


# ---------------------------------------------------------------------------
# Two-service round trip
# ---------------------------------------------------------------------------

class TestRoundTrip(_SinkTestCase):

    def test_inject_then_propagate_links_services(self):
        """Service A injects; Service B extracts. The link must survive."""
        with ActionTrace.start(action="a.submit", correlation_id="corr_wf") as a:
            wire_headers = inject_headers({"Content-Type": "application/json"})
            service_a_trace_id = a.trace_id

        # Wire transit: a real client/server would Title-Case these names.
        received = {k.title(): v for k, v in wire_headers.items()}

        with propagate(received):
            with ActionTrace.start(action="b.process"):
                pass

        b = self.last
        self.assertEqual(b["upstream_trace_id"], service_a_trace_id)
        self.assertEqual(b["correlation_id"], "corr_wf")
        self.assertNotEqual(b["trace_id"], service_a_trace_id)

    def test_three_hop_chain_preserves_correlation(self):
        with ActionTrace.start(action="a.op", correlation_id="corr_wf") as a:
            hop1 = inject_headers()
        with propagate({k.title(): v for k, v in hop1.items()}):
            with ActionTrace.start(action="b.op") as b:
                hop2 = inject_headers()
                b_id = b.trace_id
        with propagate({k.title(): v for k, v in hop2.items()}):
            with ActionTrace.start(action="c.op"):
                pass

        c = self.last
        self.assertEqual(c["upstream_trace_id"], b_id)
        self.assertEqual(c["correlation_id"], "corr_wf")


# ---------------------------------------------------------------------------
# WSGI middleware
# ---------------------------------------------------------------------------

class TestTraceActMiddleware(_SinkTestCase):

    def _environ(self, trace_id=None, correlation_id=None):
        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/"}
        if trace_id:
            environ["HTTP_TRACEACT_TRACE_ID"] = trace_id
        if correlation_id:
            environ["HTTP_TRACEACT_CORRELATION_ID"] = correlation_id
        return environ

    def _app(self):
        def inner(environ, start_response):
            with ActionTrace.start(action="wsgi.handle"):
                pass
            start_response("200 OK", [])
            return [b"ok"]
        return inner

    def _drive(self, app, environ):
        """Iterate and close, exactly as a conforming WSGI server does."""
        statuses = []
        body = app(environ, lambda s, h: statuses.append(s))
        chunks = list(body)
        if hasattr(body, "close"):
            body.close()
        return statuses, chunks

    def test_sets_upstream_not_correlation(self):
        app = TraceActMiddleware(self._app())
        self._drive(app, self._environ(trace_id="trc_wsgi"))
        self.assertEqual(self.last["upstream_trace_id"], "trc_wsgi")
        self.assertIsNone(self.last["correlation_id"])

    def test_sets_correlation_header(self):
        app = TraceActMiddleware(self._app())
        self._drive(app, self._environ(trace_id="trc_w", correlation_id="corr_w"))
        self.assertEqual(self.last["upstream_trace_id"], "trc_w")
        self.assertEqual(self.last["correlation_id"], "corr_w")

    def test_no_headers_is_transparent(self):
        app = TraceActMiddleware(self._app())
        statuses, chunks = self._drive(app, self._environ())
        self.assertIsNone(self.last["upstream_trace_id"])
        self.assertEqual(chunks, [b"ok"])
        self.assertEqual(statuses[0], "200 OK")

    def test_clears_context_after_close(self):
        app = TraceActMiddleware(self._app())
        self._drive(app, self._environ(trace_id="trc_tmp"))
        self.assertIsNone(_INCOMING_TRACE_ID.get())
        self.assertIsNone(_INCOMING_CORRELATION_ID.get())

    def test_clears_context_on_app_exception(self):
        def failing(environ, start_response):
            raise RuntimeError("app exploded")

        app = TraceActMiddleware(failing)
        with self.assertRaises(RuntimeError):
            app(self._environ(trace_id="trc_err"), lambda s, h: None)
        self.assertIsNone(_INCOMING_TRACE_ID.get())

    def test_double_close_is_safe(self):
        app = TraceActMiddleware(self._app())
        body = app(self._environ(trace_id="trc_dbl"), lambda s, h: None)
        list(body)
        body.close()
        body.close()  # must not raise
        self.assertIsNone(_INCOMING_TRACE_ID.get())

    def test_inner_close_is_forwarded(self):
        closed = []

        class ClosableBody:
            def __iter__(self):
                return iter([b"chunk"])

            def close(self):
                closed.append(True)

        def app_returning_closable(environ, start_response):
            start_response("200 OK", [])
            return ClosableBody()

        app = TraceActMiddleware(app_returning_closable)
        self._drive(app, self._environ(trace_id="trc_close"))
        self.assertEqual(closed, [True])

    def test_streaming_body_keeps_context(self):
        """A generator body is produced after the app callable returns; traces
        created during that iteration must still see the propagated context."""
        def streaming_app(environ, start_response):
            start_response("200 OK", [])

            def generate():
                for i in range(3):
                    with ActionTrace.start(action=f"stream.chunk.{i}"):
                        pass
                    yield b"chunk"

            return generate()

        app = TraceActMiddleware(streaming_app)
        self._drive(app, self._environ(trace_id="trc_stream", correlation_id="corr_s"))

        streamed = [r for r in self.records if r["action"].startswith("stream.chunk")]
        self.assertEqual(len(streamed), 3)
        for rec in streamed:
            self.assertEqual(rec["upstream_trace_id"], "trc_stream")
            self.assertEqual(rec["correlation_id"], "corr_s")

    def test_flask_end_to_end(self):
        """Real Flask app behind the middleware, driven by its test client."""
        from flask import Flask

        app = Flask(__name__)

        @app.route("/")
        def index():
            with ActionTrace.start(action="flask.handle"):
                pass
            return "ok"

        app.wsgi_app = TraceActMiddleware(app.wsgi_app)
        client = app.test_client()
        resp = client.get("/", headers={
            TRACE_HEADER_TITLE: "trc_flask_mw",
            CORR_HEADER_TITLE: "corr_flask_mw",
        })

        self.assertEqual(resp.status_code, 200)
        handled = [r for r in self.records if r["action"] == "flask.handle"]
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["upstream_trace_id"], "trc_flask_mw")
        self.assertEqual(handled[0]["correlation_id"], "corr_flask_mw")


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------

class TestTraceActASGIMiddleware(_SinkTestCase):

    def _scope(self, trace_id=None, correlation_id=None, scope_type="http"):
        headers = []
        if trace_id:
            headers.append((b"traceact-trace-id", trace_id.encode()))
        if correlation_id:
            headers.append((b"traceact-correlation-id", correlation_id.encode()))
        return {"type": scope_type, "headers": headers}

    def _app(self):
        async def inner(scope, receive, send):
            if scope["type"] == "http":
                with ActionTrace.start(action="asgi.handle"):
                    pass
        return inner

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_sets_upstream_not_correlation(self):
        app = TraceActASGIMiddleware(self._app())
        self._run(app(self._scope(trace_id="trc_asgi"), None, None))
        self.assertEqual(self.last["upstream_trace_id"], "trc_asgi")
        self.assertIsNone(self.last["correlation_id"])

    def test_sets_both_headers(self):
        app = TraceActASGIMiddleware(self._app())
        self._run(app(self._scope("trc_a", "corr_a"), None, None))
        self.assertEqual(self.last["upstream_trace_id"], "trc_a")
        self.assertEqual(self.last["correlation_id"], "corr_a")

    def test_no_headers_transparent(self):
        app = TraceActASGIMiddleware(self._app())
        self._run(app(self._scope(), None, None))
        self.assertIsNone(self.last["upstream_trace_id"])

    def test_clears_context_after_request(self):
        app = TraceActASGIMiddleware(self._app())
        self._run(app(self._scope(trace_id="trc_tmp"), None, None))
        self.assertIsNone(_INCOMING_TRACE_ID.get())

    def test_clears_context_on_exception(self):
        async def failing(scope, receive, send):
            raise RuntimeError("boom")

        app = TraceActASGIMiddleware(failing)
        with self.assertRaises(RuntimeError):
            self._run(app(self._scope(trace_id="trc_e"), None, None))
        self.assertIsNone(_INCOMING_TRACE_ID.get())

    def test_header_name_case_insensitive(self):
        app = TraceActASGIMiddleware(self._app())
        scope = {"type": "http", "headers": [(b"Traceact-Trace-Id", b"trc_upper")]}
        self._run(app(scope, None, None))
        self.assertEqual(self.last["upstream_trace_id"], "trc_upper")

    def test_non_http_scopes_pass_through(self):
        seen = []

        async def inner(scope, receive, send):
            seen.append(scope["type"])

        app = TraceActASGIMiddleware(inner)

        async def go():
            await app({"type": "websocket", "headers": []}, None, None)
            await app({"type": "lifespan", "headers": []}, None, None)

        self._run(go())
        self.assertEqual(seen, ["websocket", "lifespan"])

    def test_starlette_end_to_end(self):
        """Real Starlette app + TestClient through the middleware."""
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        def homepage(request):
            with ActionTrace.start(action="starlette.handle"):
                pass
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/", homepage)])
        app.add_middleware(TraceActASGIMiddleware)

        client = TestClient(app)
        resp = client.get("/", headers={
            TRACE_HEADER_TITLE: "trc_star",
            CORR_HEADER_TITLE: "corr_star",
        })

        self.assertEqual(resp.status_code, 200)
        handled = [r for r in self.records if r["action"] == "starlette.handle"]
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["upstream_trace_id"], "trc_star")
        self.assertEqual(handled[0]["correlation_id"], "corr_star")


# ---------------------------------------------------------------------------
# Schema + sink integration for the new field
# ---------------------------------------------------------------------------

class TestUpstreamFieldSchema(_SinkTestCase):

    def test_field_always_present_in_record(self):
        with ActionTrace.start(action="plain.op"):
            pass
        self.assertIn("upstream_trace_id", self.last)
        self.assertIsNone(self.last["upstream_trace_id"])

    def test_distinct_from_parent_trace_id(self):
        with propagate({TRACE_HEADER_TITLE: "trc_up"}):
            with ActionTrace.start(action="parent.op") as parent:
                with ActionTrace.start(action="child.op"):
                    pass
        child = self.records[0]
        self.assertEqual(child["parent_trace_id"], parent.trace_id)
        self.assertEqual(child["upstream_trace_id"], "trc_up")
        self.assertNotEqual(child["parent_trace_id"], child["upstream_trace_id"])


class TestSqliteSinkUpstreamColumn(unittest.TestCase):

    def tearDown(self):
        reset_config()

    def test_new_database_stores_upstream(self):
        import os
        import sqlite3
        import tempfile

        from traceact import SqliteSink

        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "traces.db")
        sink = SqliteSink(path)
        configure(config=TraceConfig(sink_mode="blocking"), sinks=[sink])

        with propagate({TRACE_HEADER_TITLE: "trc_up"}):
            with ActionTrace.start(action="db.op"):
                pass
        sink.close()

        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT upstream_trace_id FROM traces WHERE action = 'db.op'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "trc_up")

    def test_migrates_pre_existing_database(self):
        """A database without the upstream_trace_id column must gain it via
        ALTER TABLE rather than failing every insert."""
        import os
        import sqlite3
        import tempfile

        from traceact import SqliteSink

        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "legacy.db")

        # Schema without upstream_trace_id, as an older TraceAct version wrote it.
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE traces (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id        TEXT NOT NULL UNIQUE,
                root_trace_id   TEXT,
                parent_trace_id TEXT,
                correlation_id  TEXT,
                action          TEXT NOT NULL,
                kind            TEXT,
                status          TEXT,
                started_at      TEXT,
                ended_at        TEXT,
                duration_ms     REAL,
                budget_hit      INTEGER DEFAULT 0,
                record          TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO traces (trace_id, action, record) VALUES (?, ?, ?)",
            ("trc_legacy", "legacy.op", "{}"),
        )
        conn.commit()
        conn.close()

        sink = SqliteSink(path)
        configure(config=TraceConfig(sink_mode="blocking"), sinks=[sink])
        with propagate({TRACE_HEADER_TITLE: "trc_up"}):
            with ActionTrace.start(action="new.op"):
                pass
        sink.close()

        conn = sqlite3.connect(path)
        new_row = conn.execute(
            "SELECT upstream_trace_id FROM traces WHERE action = 'new.op'"
        ).fetchone()
        legacy_row = conn.execute(
            "SELECT upstream_trace_id FROM traces WHERE action = 'legacy.op'"
        ).fetchone()
        conn.close()

        self.assertEqual(new_row[0], "trc_up")   # new write works
        self.assertIsNone(legacy_row[0])         # old row preserved, NULL column


class TestOtlpUpstreamAttribute(unittest.TestCase):

    def test_upstream_emitted_as_span_attribute(self):
        from traceact.sinks import _to_otlp_span

        span = _to_otlp_span({
            "trace_id": "trc_self",
            "action": "svc.op",
            "upstream_trace_id": "trc_up",
            "started_at": "2026-07-26T10:00:00.000Z",
        })
        keys = {a["key"]: a["value"] for a in span["attributes"]}
        self.assertIn("traceact.upstream_trace_id", keys)
        self.assertEqual(
            keys["traceact.upstream_trace_id"]["stringValue"], "trc_up"
        )

    def test_absent_upstream_emits_no_attribute(self):
        from traceact.sinks import _to_otlp_span

        span = _to_otlp_span({
            "trace_id": "trc_self",
            "action": "svc.op",
            "started_at": "2026-07-26T10:00:00.000Z",
        })
        keys = {a["key"] for a in span["attributes"]}
        self.assertNotIn("traceact.upstream_trace_id", keys)


# ---------------------------------------------------------------------------
# ai_prompts redaction preset
# ---------------------------------------------------------------------------

class TestAiPromptsPreset(unittest.TestCase):

    def setUp(self):
        sink, self.records = _make_sink()
        configure(
            config=TraceConfig(
                sink_mode="blocking",
                capture_inputs=True,
                redaction_presets=["ai_prompts"],
            ),
            sinks=[sink],
        )

    def tearDown(self):
        reset_config()

    def _inputs(self):
        return self.records[-1]["inputs"]

    def test_raw_prompt_redacted(self):
        with ActionTrace.start(action="llm.call") as t:
            t.input({"raw_prompt": "Tell me about nuclear reactors"})
        self.assertEqual(self._inputs()["raw_prompt"], "[redacted]")

    def test_system_prompt_redacted(self):
        with ActionTrace.start(action="llm.call") as t:
            t.input({"system_prompt": "You are a helpful assistant."})
        self.assertEqual(self._inputs()["system_prompt"], "[redacted]")

    def test_response_content_redacted(self):
        with ActionTrace.start(action="llm.call") as t:
            t.input({"response_content": "Here is my answer..."})
        self.assertEqual(self._inputs()["response_content"], "[redacted]")

    def test_nested_prompt_redacted(self):
        with ActionTrace.start(action="llm.call") as t:
            t.input({"payload": {"raw_prompt": "secret prompt"}})
        self.assertEqual(self._inputs()["payload"]["raw_prompt"], "[redacted]")

    def test_safe_metadata_not_redacted(self):
        # Neither name contains a baseline or preset substring.
        with ActionTrace.start(action="llm.call") as t:
            t.input({"model": "claude-opus-5", "latency_ms": 312})
        inputs = self._inputs()
        self.assertEqual(inputs["model"], "claude-opus-5")
        self.assertEqual(inputs["latency_ms"], 312)

    def test_registry_contains_preset(self):
        from traceact.redaction import REDACTION_PRESETS
        self.assertIn("ai_prompts", REDACTION_PRESETS)
        self.assertIn("raw_prompt", REDACTION_PRESETS["ai_prompts"])

    def test_invalid_preset_raises(self):
        with self.assertRaises(ValueError):
            TraceConfig(redaction_presets=["nonexistent_preset"])


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

class TestPublicExports(unittest.TestCase):

    def test_all_names_exported(self):
        import traceact
        for name in (
            "propagate", "inject_headers",
            "extract_trace_id", "extract_correlation_id",
            "TraceActMiddleware", "TraceActASGIMiddleware",
        ):
            self.assertIn(name, traceact.__all__)
            self.assertTrue(hasattr(traceact, name))

    def test_legacy_header_name_alias_preserved(self):
        from traceact.propagation import HEADER_NAME, HEADER_TRACE_ID
        self.assertEqual(HEADER_NAME, HEADER_TRACE_ID)


if __name__ == "__main__":
    unittest.main()
