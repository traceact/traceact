# tests/test_http_sink.py
#
# Tests for HttpSink.
#
# We don't spin up a real HTTP server. Instead we patch urllib.request.urlopen
# so tests run offline with no ports, no threads, and no flakiness.

import json
import unittest.mock as mock
import pytest

from traceact import HttpSink


def _trace(action="note.create", **extra):
    t = {
        "trace_id": "trc_note_create",
        "action": action,
        "started_at": "2026-07-25T10:00:00Z",
        "status": "completed",
    }
    t.update(extra)
    return t


def _mock_response(status=200):
    """Return a mock context-manager response with the given status."""
    resp = mock.MagicMock()
    resp.status = status
    resp.__enter__ = lambda s: resp
    resp.__exit__ = mock.MagicMock(return_value=False)
    return resp


class TestSuccessfulDelivery:
    def test_posts_json_body(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.method
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["content_type"] = req.get_header("Content-type")
            return _mock_response(200)

        sink = HttpSink("http://collector.example.com/traces")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace("note.create"))

        assert captured["url"] == "http://collector.example.com/traces"
        assert captured["method"] == "POST"
        assert captured["body"]["action"] == "note.create"
        assert captured["content_type"] == "application/json"

    def test_success_does_not_increment_failed(self):
        sink = HttpSink("http://example.com/traces")
        with mock.patch("urllib.request.urlopen", return_value=_mock_response(200)):
            sink.write(_trace())
        assert sink.failed == 0

    def test_custom_headers_sent(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return _mock_response(200)

        sink = HttpSink(
            "http://example.com/traces",
            headers={"Authorization": "Bearer secret-token"},
        )
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace())

        assert captured["auth"] == "Bearer secret-token"

    def test_timeout_passed_to_urlopen(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            return _mock_response(200)

        sink = HttpSink("http://example.com/traces", timeout=2.5)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sink.write(_trace())

        assert captured["timeout"] == 2.5


class TestObservableFailures:
    def test_non_2xx_increments_failed(self):
        sink = HttpSink("http://example.com/traces")
        with mock.patch("urllib.request.urlopen", return_value=_mock_response(500)):
            sink.write(_trace())
        assert sink.failed == 1

    def test_connection_error_increments_failed(self):
        import urllib.error
        sink = HttpSink("http://example.com/traces")
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("connection refused")):
            sink.write(_trace())
        assert sink.failed == 1

    def test_timeout_increments_failed(self):
        import socket
        sink = HttpSink("http://example.com/traces")
        with mock.patch("urllib.request.urlopen",
                        side_effect=TimeoutError("timed out")):
            sink.write(_trace())
        assert sink.failed == 1

    def test_multiple_failures_counted(self):
        import urllib.error
        sink = HttpSink("http://example.com/traces")
        exc = urllib.error.URLError("refused")
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            sink.write(_trace())
            sink.write(_trace())
            sink.write(_trace())
        assert sink.failed == 3

    def test_failure_does_not_raise(self):
        import urllib.error
        sink = HttpSink("http://example.com/traces")
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            # Must return cleanly, not propagate.
            sink.write(_trace())

    def test_404_is_a_failure(self):
        sink = HttpSink("http://example.com/traces")
        with mock.patch("urllib.request.urlopen", return_value=_mock_response(404)):
            sink.write(_trace())
        assert sink.failed == 1

    def test_201_is_not_a_failure(self):
        sink = HttpSink("http://example.com/traces")
        with mock.patch("urllib.request.urlopen", return_value=_mock_response(201)):
            sink.write(_trace())
        assert sink.failed == 0


class TestPublicExport:
    def test_importable_from_top_level(self):
        from traceact import HttpSink as HS
        assert HS is HttpSink
