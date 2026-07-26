# tests/test_propagation.py
#
# Tests for distributed trace propagation: extract_trace_id, inject_headers,
# propagate context manager, TraceActMiddleware, TraceActASGIMiddleware,
# and the ai_prompts redaction preset.

import asyncio
import threading
import unittest

from traceact import (
    ActionTrace,
    TraceActASGIMiddleware,
    TraceActMiddleware,
    TraceConfig,
    configure,
    inject_headers,
    propagate,
    reset_config,
)
from traceact.propagation import HEADER_NAME, _INCOMING_TRACE_ID, extract_trace_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sink():
    """Return an in-memory sink that records written dicts."""
    records = []

    class _MemSink:
        def write(self, record):
            records.append(record)

    return _MemSink(), records


# ---------------------------------------------------------------------------
# TestExtractTraceId
# ---------------------------------------------------------------------------

class TestExtractTraceId(unittest.TestCase):

    def test_exact_header_name(self):
        self.assertEqual(extract_trace_id({"traceact-trace-id": "trc_abc"}), "trc_abc")

    def test_lowercased_header(self):
        self.assertEqual(extract_trace_id({"traceact-trace-id": "trc_xyz"}), "trc_xyz")

    def test_absent_returns_none(self):
        self.assertIsNone(extract_trace_id({"content-type": "application/json"}))

    def test_empty_dict(self):
        self.assertIsNone(extract_trace_id({}))

    def test_non_dict_returns_none(self):
        self.assertIsNone(extract_trace_id([("traceact-trace-id", "trc_abc")]))

    def test_none_returns_none(self):
        self.assertIsNone(extract_trace_id(None))

    def test_empty_string_value(self):
        # An empty string is falsy — treated as absent.
        self.assertIsNone(extract_trace_id({"traceact-trace-id": ""}))


# ---------------------------------------------------------------------------
# TestInjectHeaders
# ---------------------------------------------------------------------------

class TestInjectHeaders(unittest.TestCase):

    def setUp(self):
        sink, self.records = _make_sink()
        configure(config=TraceConfig(sink_mode="blocking"), sinks=[sink])

    def tearDown(self):
        reset_config()

    def test_no_active_trace_returns_unchanged(self):
        result = inject_headers({"Content-Type": "application/json"})
        self.assertNotIn(HEADER_NAME, result)
        self.assertEqual(result["Content-Type"], "application/json")

    def test_active_trace_injects_trace_id(self):
        with ActionTrace.start(action="svc.call") as trace:
            result = inject_headers()
        self.assertEqual(result[HEADER_NAME], trace.trace_id)

    def test_original_headers_preserved(self):
        with ActionTrace.start(action="svc.call"):
            result = inject_headers({"Authorization": "Bearer tok"})
        self.assertIn("Authorization", result)
        self.assertIn(HEADER_NAME, result)

    def test_does_not_mutate_input(self):
        original = {"X-Request-Id": "req_1"}
        with ActionTrace.start(action="svc.call"):
            inject_headers(original)
        self.assertNotIn(HEADER_NAME, original)

    def test_none_input_returns_dict(self):
        with ActionTrace.start(action="svc.call") as trace:
            result = inject_headers(None)
        self.assertEqual(result[HEADER_NAME], trace.trace_id)

    def test_empty_input_returns_dict(self):
        with ActionTrace.start(action="svc.call") as trace:
            result = inject_headers({})
        self.assertEqual(result[HEADER_NAME], trace.trace_id)


# ---------------------------------------------------------------------------
# TestPropagate
# ---------------------------------------------------------------------------

class TestPropagate(unittest.TestCase):

    def setUp(self):
        sink, self.records = _make_sink()
        configure(config=TraceConfig(sink_mode="blocking"), sinks=[sink])

    def tearDown(self):
        reset_config()

    def test_correlation_id_set_when_header_present(self):
        incoming_id = "trc_upstream_123"
        with propagate({HEADER_NAME: incoming_id}):
            with ActionTrace.start(action="downstream.op") as trace:
                pass
        self.assertEqual(self.records[-1]["correlation_id"], incoming_id)

    def test_no_correlation_when_header_absent(self):
        with propagate({}):
            with ActionTrace.start(action="solo.op") as trace:
                pass
        self.assertIsNone(self.records[-1]["correlation_id"])

    def test_explicit_correlation_id_wins(self):
        incoming_id = "trc_upstream"
        explicit_id = "trc_explicit"
        with propagate({HEADER_NAME: incoming_id}):
            with ActionTrace.start(action="op", correlation_id=explicit_id) as trace:
                pass
        self.assertEqual(self.records[-1]["correlation_id"], explicit_id)

    def test_context_var_cleared_after_exit(self):
        with propagate({HEADER_NAME: "trc_temp"}):
            pass
        self.assertIsNone(_INCOMING_TRACE_ID.get())

    def test_context_var_cleared_on_exception(self):
        try:
            with propagate({HEADER_NAME: "trc_temp"}):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertIsNone(_INCOMING_TRACE_ID.get())

    def test_incoming_trace_id_property(self):
        ctx = propagate({HEADER_NAME: "trc_prop_test"})
        self.assertEqual(ctx.incoming_trace_id, "trc_prop_test")

    def test_incoming_trace_id_none_when_absent(self):
        ctx = propagate({})
        self.assertIsNone(ctx.incoming_trace_id)

    def test_nested_propagate_restores_outer(self):
        outer_id = "trc_outer"
        inner_id = "trc_inner"
        with propagate({HEADER_NAME: outer_id}):
            with propagate({HEADER_NAME: inner_id}):
                self.assertEqual(_INCOMING_TRACE_ID.get(), inner_id)
            self.assertEqual(_INCOMING_TRACE_ID.get(), outer_id)

    def test_multiple_traces_in_one_propagate_block(self):
        incoming_id = "trc_shared"
        with propagate({HEADER_NAME: incoming_id}):
            with ActionTrace.start(action="op.one"):
                pass
            with ActionTrace.start(action="op.two"):
                pass
        corr_ids = [r["correlation_id"] for r in self.records]
        self.assertTrue(all(c == incoming_id for c in corr_ids))

    def test_thread_isolation(self):
        # Propagation set in one thread must not leak into another.
        results = {}

        def thread_a():
            with propagate({HEADER_NAME: "trc_thread_a"}):
                import time; time.sleep(0.02)
                results["a"] = _INCOMING_TRACE_ID.get()

        def thread_b():
            import time; time.sleep(0.005)
            results["b"] = _INCOMING_TRACE_ID.get()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start(); tb.start()
        ta.join(); tb.join()

        self.assertEqual(results["a"], "trc_thread_a")
        self.assertIsNone(results["b"])


# ---------------------------------------------------------------------------
# TestTraceActMiddleware (WSGI)
# ---------------------------------------------------------------------------

class TestTraceActMiddleware(unittest.TestCase):

    def setUp(self):
        sink, self.records = _make_sink()
        configure(config=TraceConfig(sink_mode="blocking"), sinks=[sink])

    def tearDown(self):
        reset_config()

    def _make_environ(self, trace_id=None, extra=None):
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "5000",
            "wsgi.input": None,
        }
        if trace_id:
            key = "HTTP_" + HEADER_NAME.upper().replace("-", "_")
            environ[key] = trace_id
        if extra:
            environ.update(extra)
        return environ

    def _make_app(self):
        """A minimal WSGI app that starts a trace."""
        def inner(environ, start_response):
            with ActionTrace.start(action="wsgi.handle"):
                pass
            start_response("200 OK", [])
            return [b"ok"]
        return inner

    def test_sets_correlation_id_when_header_present(self):
        app = TraceActMiddleware(self._make_app())
        environ = self._make_environ(trace_id="trc_wsgi_upstream")
        responses = []
        app(environ, lambda s, h: responses.append(s))
        self.assertEqual(self.records[-1]["correlation_id"], "trc_wsgi_upstream")

    def test_no_correlation_when_header_absent(self):
        app = TraceActMiddleware(self._make_app())
        environ = self._make_environ()
        app(environ, lambda s, h: None)
        self.assertIsNone(self.records[-1]["correlation_id"])

    def test_clears_context_after_request(self):
        app = TraceActMiddleware(self._make_app())
        environ = self._make_environ(trace_id="trc_wsgi_temp")
        app(environ, lambda s, h: None)
        self.assertIsNone(_INCOMING_TRACE_ID.get())

    def test_passes_through_response(self):
        app = TraceActMiddleware(self._make_app())
        environ = self._make_environ()
        statuses = []
        result = app(environ, lambda s, h: statuses.append(s))
        self.assertEqual(statuses[0], "200 OK")
        self.assertEqual(result, [b"ok"])

    def test_clears_context_on_app_exception(self):
        def failing_app(environ, start_response):
            raise RuntimeError("app exploded")

        app = TraceActMiddleware(failing_app)
        try:
            app(self._make_environ(trace_id="trc_wsgi_err"), lambda s, h: None)
        except RuntimeError:
            pass
        self.assertIsNone(_INCOMING_TRACE_ID.get())


# ---------------------------------------------------------------------------
# TestTraceActASGIMiddleware
# ---------------------------------------------------------------------------

class TestTraceActASGIMiddleware(unittest.TestCase):

    def setUp(self):
        sink, self.records = _make_sink()
        configure(config=TraceConfig(sink_mode="blocking"), sinks=[sink])

    def tearDown(self):
        reset_config()

    def _make_scope(self, trace_id=None, scope_type="http"):
        headers = []
        if trace_id:
            headers.append((HEADER_NAME.encode(), trace_id.encode()))
        return {"type": scope_type, "headers": headers}

    def _make_app(self):
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

    def test_sets_correlation_id_when_header_present(self):
        app = TraceActASGIMiddleware(self._make_app())

        async def go():
            scope = self._make_scope(trace_id="trc_asgi_upstream")
            await app(scope, None, None)

        self._run(go())
        self.assertEqual(self.records[-1]["correlation_id"], "trc_asgi_upstream")

    def test_no_correlation_when_header_absent(self):
        app = TraceActASGIMiddleware(self._make_app())

        async def go():
            await app(self._make_scope(), None, None)

        self._run(go())
        self.assertIsNone(self.records[-1]["correlation_id"])

    def test_clears_context_after_request(self):
        app = TraceActASGIMiddleware(self._make_app())

        async def go():
            await app(self._make_scope(trace_id="trc_asgi_temp"), None, None)

        self._run(go())
        self.assertIsNone(_INCOMING_TRACE_ID.get())

    def test_non_http_scope_passes_through(self):
        passed = []

        async def inner(scope, receive, send):
            passed.append(scope["type"])

        app = TraceActASGIMiddleware(inner)

        async def go():
            await app({"type": "websocket", "headers": []}, None, None)
            await app({"type": "lifespan", "headers": []}, None, None)

        self._run(go())
        self.assertEqual(passed, ["websocket", "lifespan"])

    def test_header_case_insensitive(self):
        app = TraceActASGIMiddleware(self._make_app())

        async def go():
            # Uppercase bytes header key
            scope = {
                "type": "http",
                "headers": [(b"Traceact-Trace-Id", b"trc_upper")],
            }
            await app(scope, None, None)

        self._run(go())
        self.assertEqual(self.records[-1]["correlation_id"], "trc_upper")


# ---------------------------------------------------------------------------
# TestAiPromptsPreset
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

    def _last_inputs(self):
        return self.records[-1]["inputs"]

    def test_raw_prompt_redacted(self):
        with ActionTrace.start(action="llm.call") as t:
            t.input({"raw_prompt": "Tell me everything about nuclear reactors"})
        self.assertEqual(self._last_inputs()["raw_prompt"], "[redacted]")

    def test_system_prompt_redacted(self):
        with ActionTrace.start(action="llm.call") as t:
            t.input({"system_prompt": "You are a helpful assistant."})
        self.assertEqual(self._last_inputs()["system_prompt"], "[redacted]")

    def test_response_content_redacted(self):
        with ActionTrace.start(action="llm.call") as t:
            t.input({"response_content": "Here is my answer..."})
        self.assertEqual(self._last_inputs()["response_content"], "[redacted]")

    def test_safe_metadata_not_redacted(self):
        # "model" and "latency_ms" contain no sensitive substrings.
        with ActionTrace.start(action="llm.call") as t:
            t.input({"model": "claude-opus-5", "latency_ms": 312})
        inputs = self._last_inputs()
        self.assertEqual(inputs["model"], "claude-opus-5")
        self.assertEqual(inputs["latency_ms"], 312)

    def test_preset_in_redaction_presets_registry(self):
        from traceact.redaction import REDACTION_PRESETS
        self.assertIn("ai_prompts", REDACTION_PRESETS)
        self.assertIn("raw_prompt", REDACTION_PRESETS["ai_prompts"])
        self.assertIn("system_prompt", REDACTION_PRESETS["ai_prompts"])

    def test_invalid_preset_raises(self):
        with self.assertRaises(ValueError):
            TraceConfig(redaction_presets=["nonexistent_preset"])


# ---------------------------------------------------------------------------
# TestPublicExports
# ---------------------------------------------------------------------------

class TestPublicExports(unittest.TestCase):

    def test_propagate_importable(self):
        from traceact import propagate
        self.assertTrue(callable(propagate))

    def test_inject_headers_importable(self):
        from traceact import inject_headers
        self.assertTrue(callable(inject_headers))

    def test_middleware_importable(self):
        from traceact import TraceActMiddleware, TraceActASGIMiddleware
        self.assertTrue(callable(TraceActMiddleware))
        self.assertTrue(callable(TraceActASGIMiddleware))

    def test_in_all(self):
        import traceact
        for name in ("propagate", "inject_headers",
                     "TraceActMiddleware", "TraceActASGIMiddleware"):
            self.assertIn(name, traceact.__all__)


if __name__ == "__main__":
    unittest.main()
