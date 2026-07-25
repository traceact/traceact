# tests/test_otlp_sink.py
#
# Tests for OtlpSink.
#
# We don't spin up a real OTLP collector. We patch urllib.request.urlopen so
# tests run offline with no ports, no threads, and no flakiness.

import json
import unittest.mock as mock
import pytest

from traceact import OtlpSink
from traceact.sinks import (
    _trace_id_hex,
    _span_id_hex,
    _iso_to_nanos,
    _otlp_attr,
    _to_otlp_span,
    _KIND_TO_OTLP,
    _OTLP_KIND_INTERNAL,
    _OTLP_KIND_CLIENT,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _trace(action="order.create", kind="http", status="completed", **extra):
    t = {
        "trace_id":   "trc_order_create",
        "action":     action,
        "kind":       kind,
        "status":     status,
        "started_at": "2026-07-25T10:00:00Z",
        "ended_at":   "2026-07-25T10:00:00.250Z",
        "duration_ms": 250,
    }
    t.update(extra)
    return t


def _mock_response(status=200):
    resp = mock.MagicMock()
    resp.status = status
    resp.__enter__ = lambda s: resp
    resp.__exit__ = mock.MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

class TestIdHelpers:
    def test_trace_id_hex_is_32_chars(self):
        assert len(_trace_id_hex("trc_abc")) == 32

    def test_trace_id_hex_is_hex(self):
        val = _trace_id_hex("trc_abc")
        int(val, 16)  # raises if not valid hex

    def test_span_id_hex_is_16_chars(self):
        assert len(_span_id_hex("trc_abc")) == 16

    def test_span_id_hex_is_hex(self):
        val = _span_id_hex("trc_abc")
        int(val, 16)

    def test_same_input_produces_same_output(self):
        assert _trace_id_hex("trc_x") == _trace_id_hex("trc_x")
        assert _span_id_hex("trc_x")  == _span_id_hex("trc_x")

    def test_different_inputs_produce_different_outputs(self):
        assert _trace_id_hex("trc_x") != _trace_id_hex("trc_y")


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------

class TestIsoToNanos:
    def test_z_suffix(self):
        ns = _iso_to_nanos("2026-07-25T10:00:00Z")
        assert ns > 0

    def test_offset_suffix(self):
        ns = _iso_to_nanos("2026-07-25T10:00:00+00:00")
        assert ns > 0

    def test_z_and_offset_equivalent(self):
        assert _iso_to_nanos("2026-07-25T10:00:00Z") == _iso_to_nanos("2026-07-25T10:00:00+00:00")

    def test_none_returns_zero(self):
        assert _iso_to_nanos(None) == 0

    def test_empty_string_returns_zero(self):
        assert _iso_to_nanos("") == 0

    def test_garbage_returns_zero(self):
        assert _iso_to_nanos("not-a-timestamp") == 0

    def test_millis_precision(self):
        ns = _iso_to_nanos("2026-07-25T10:00:00.250Z")
        assert ns > 0


# ---------------------------------------------------------------------------
# Attribute helper
# ---------------------------------------------------------------------------

class TestOtlpAttr:
    def test_string_value(self):
        a = _otlp_attr("k", "v")
        assert a == {"key": "k", "value": {"stringValue": "v"}}

    def test_int_value(self):
        a = _otlp_attr("k", 42)
        assert a == {"key": "k", "value": {"intValue": "42"}}

    def test_float_value(self):
        a = _otlp_attr("k", 3.14)
        assert a == {"key": "k", "value": {"doubleValue": 3.14}}

    def test_bool_value(self):
        a = _otlp_attr("k", True)
        assert a == {"key": "k", "value": {"boolValue": True}}

    def test_bool_false(self):
        a = _otlp_attr("k", False)
        assert a == {"key": "k", "value": {"boolValue": False}}

    def test_none_becomes_string(self):
        a = _otlp_attr("k", None)
        assert a["value"] == {"stringValue": "None"}


# ---------------------------------------------------------------------------
# _to_otlp_span mapping
# ---------------------------------------------------------------------------

class TestToOtlpSpan:
    def test_trace_id_field(self):
        span = _to_otlp_span(_trace())
        assert span["traceId"] == _trace_id_hex("trc_order_create")

    def test_span_id_field(self):
        span = _to_otlp_span(_trace())
        assert span["spanId"] == _span_id_hex("trc_order_create")

    def test_name_is_action(self):
        span = _to_otlp_span(_trace(action="payment.charge"))
        assert span["name"] == "payment.charge"

    def test_kind_http_maps_to_client(self):
        span = _to_otlp_span(_trace(kind="http"))
        assert span["kind"] == _OTLP_KIND_CLIENT

    def test_kind_unknown_maps_to_internal(self):
        span = _to_otlp_span(_trace(kind="custom_kind"))
        assert span["kind"] == _OTLP_KIND_INTERNAL

    def test_kind_db_maps_to_client(self):
        span = _to_otlp_span(_trace(kind="db"))
        assert span["kind"] == _OTLP_KIND_CLIENT

    def test_timestamps_are_strings(self):
        span = _to_otlp_span(_trace())
        assert isinstance(span["startTimeUnixNano"], str)
        assert isinstance(span["endTimeUnixNano"], str)

    def test_timestamps_nonzero(self):
        span = _to_otlp_span(_trace())
        assert int(span["startTimeUnixNano"]) > 0
        assert int(span["endTimeUnixNano"]) > 0

    def test_status_completed_is_ok(self):
        span = _to_otlp_span(_trace(status="completed"))
        assert span["status"]["code"] == 1

    def test_status_failed_is_error(self):
        span = _to_otlp_span(_trace(status="failed"))
        assert span["status"]["code"] == 2

    def test_status_unknown_is_unset(self):
        span = _to_otlp_span(_trace(status="pending"))
        assert span["status"]["code"] == 0

    def test_parent_span_id_present_when_parent_set(self):
        t = _trace(parent_trace_id="trc_parent")
        span = _to_otlp_span(t)
        assert span["parentSpanId"] == _span_id_hex("trc_parent")

    def test_parent_span_id_absent_when_no_parent(self):
        span = _to_otlp_span(_trace())
        assert "parentSpanId" not in span

    def test_traceact_trace_id_attribute_preserved(self):
        span = _to_otlp_span(_trace())
        keys = [a["key"] for a in span["attributes"]]
        assert "traceact.trace_id" in keys

    def test_correlation_id_attribute(self):
        t = _trace(correlation_id="job_123")
        span = _to_otlp_span(t)
        keys = [a["key"] for a in span["attributes"]]
        assert "traceact.correlation_id" in keys

    def test_duration_ms_attribute(self):
        span = _to_otlp_span(_trace())
        keys = [a["key"] for a in span["attributes"]]
        assert "traceact.duration_ms" in keys

    def test_inputs_become_attributes(self):
        t = _trace(inputs={"user_id": "u_42", "amount": 100})
        span = _to_otlp_span(t)
        keys = [a["key"] for a in span["attributes"]]
        assert "traceact.input.user_id" in keys
        assert "traceact.input.amount" in keys

    def test_outputs_become_attributes(self):
        t = _trace(outputs={"order_id": "ord_9"})
        span = _to_otlp_span(t)
        keys = [a["key"] for a in span["attributes"]]
        assert "traceact.output.order_id" in keys

    def test_touches_become_attributes(self):
        t = _trace(touches=[{"kind": "db", "target": "orders"}])
        span = _to_otlp_span(t)
        keys = [a["key"] for a in span["attributes"]]
        assert "traceact.touch.0.kind" in keys
        assert "traceact.touch.0.target" in keys

    def test_steps_become_span_events(self):
        t = _trace(steps=[{"label": "validated", "recorded_at": "2026-07-25T10:00:00Z"}])
        span = _to_otlp_span(t)
        event_names = [e["name"] for e in span["events"]]
        assert "step" in event_names

    def test_errors_become_exception_events(self):
        t = _trace(
            status="failed",
            errors=[{"type": "ValueError", "message": "bad input"}],
        )
        span = _to_otlp_span(t)
        event_names = [e["name"] for e in span["events"]]
        assert "exception" in event_names

    def test_error_event_has_exception_attributes(self):
        t = _trace(
            status="failed",
            errors=[{"type": "ValueError", "message": "bad input"}],
        )
        span = _to_otlp_span(t)
        exc_event = next(e for e in span["events"] if e["name"] == "exception")
        attr_keys = [a["key"] for a in exc_event["attributes"]]
        assert "exception.type" in attr_keys
        assert "exception.message" in attr_keys

    def test_unmapped_scalar_fields_are_namespaced(self):
        t = _trace(custom_field="hello")
        span = _to_otlp_span(t)
        keys = [a["key"] for a in span["attributes"]]
        assert "traceact.custom_field" in keys

    def test_missing_action_defaults_to_unknown(self):
        t = {"trace_id": "trc_x", "status": "completed"}
        span = _to_otlp_span(t)
        assert span["name"] == "unknown"

    def test_no_crash_on_empty_record(self):
        span = _to_otlp_span({})
        assert span["name"] == "unknown"


# ---------------------------------------------------------------------------
# OtlpSink HTTP delivery
# ---------------------------------------------------------------------------

class TestOtlpSinkDelivery:
    def test_posts_to_v1_traces(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _mock_response(200)

        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace())

        assert captured["url"] == "http://localhost:4318/v1/traces"

    def test_trailing_slash_normalised(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _mock_response(200)

        sink = OtlpSink("http://localhost:4318/")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace())

        assert captured["url"] == "http://localhost:4318/v1/traces"

    def test_content_type_is_json(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["ct"] = req.get_header("Content-type")
            return _mock_response(200)

        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace())

        assert captured["ct"] == "application/json"

    def test_body_is_valid_json(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _mock_response(200)

        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace())

        assert "resourceSpans" in captured["body"]

    def test_custom_headers_forwarded(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["key"] = req.get_header("X-honeycomb-team")
            return _mock_response(200)

        sink = OtlpSink("https://api.honeycomb.io", headers={"x-honeycomb-team": "abc123"})
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace())

        assert captured["key"] == "abc123"

    def test_timeout_forwarded(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            return _mock_response(200)

        sink = OtlpSink("http://localhost:4318", timeout=3.0)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace())

        assert captured["timeout"] == 3.0

    def test_resource_attributes_in_payload(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _mock_response(200)

        sink = OtlpSink(
            "http://localhost:4318",
            resource_attributes={"service.name": "my-app", "deployment.env": "prod"},
        )
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace())

        res_attrs = {
            a["key"]: a
            for a in captured["body"]["resourceSpans"][0]["resource"]["attributes"]
        }
        assert "service.name" in res_attrs
        assert "deployment.env" in res_attrs

    def test_scope_name_is_traceact(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _mock_response(200)

        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace())

        scope = captured["body"]["resourceSpans"][0]["scopeSpans"][0]["scope"]
        assert scope["name"] == "traceact"


# ---------------------------------------------------------------------------
# Observable failures
# ---------------------------------------------------------------------------

class TestOtlpSinkObservableFailures:
    def test_success_does_not_increment_failed(self):
        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen", return_value=_mock_response(200)):
            sink.write(_trace())
        assert sink.failed == 0

    def test_non_2xx_increments_failed(self):
        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen", return_value=_mock_response(500)):
            sink.write(_trace())
        assert sink.failed == 1

    def test_connection_error_increments_failed(self):
        import urllib.error
        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("connection refused")):
            sink.write(_trace())
        assert sink.failed == 1

    def test_timeout_increments_failed(self):
        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen",
                        side_effect=TimeoutError("timed out")):
            sink.write(_trace())
        assert sink.failed == 1

    def test_multiple_failures_accumulate(self):
        import urllib.error
        sink = OtlpSink("http://localhost:4318")
        exc = urllib.error.URLError("refused")
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            sink.write(_trace())
            sink.write(_trace())
            sink.write(_trace())
        assert sink.failed == 3

    def test_failure_does_not_raise(self):
        import urllib.error
        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            sink.write(_trace())  # must return cleanly

    def test_404_is_a_failure(self):
        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen", return_value=_mock_response(404)):
            sink.write(_trace())
        assert sink.failed == 1

    def test_201_is_success(self):
        sink = OtlpSink("http://localhost:4318")
        with mock.patch("urllib.request.urlopen", return_value=_mock_response(201)):
            sink.write(_trace())
        assert sink.failed == 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class TestPublicExport:
    def test_importable_from_top_level(self):
        from traceact import OtlpSink as OS
        assert OS is OtlpSink
