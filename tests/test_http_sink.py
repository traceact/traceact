# tests/test_http_sink.py
#
# Tests for HttpSink.
#
# We don't spin up a live HTTP server. Instead we patch
# urllib.request.OpenerDirector.open (the method both urlopen() and
# traceact._netguard's guarded opener ultimately call) so tests run offline
# with no ports, no threads, and no flakiness. Tests unrelated to network
# policy pass network_policy="off" so the outbound guard's own DNS
# resolution never runs here either — see TestNetworkPolicy below for that.

import json
import unittest.mock as mock
import pytest

from traceact import HttpSink
from traceact import _netguard


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

        sink = HttpSink("http://collector.example.com/traces", network_policy="off")
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            sink.write(_trace("note.create"))

        assert captured["url"] == "http://collector.example.com/traces"
        assert captured["method"] == "POST"
        assert captured["body"]["action"] == "note.create"
        assert captured["content_type"] == "application/json"

    def test_success_does_not_increment_failed(self):
        sink = HttpSink("http://example.com/traces", network_policy="off")
        with mock.patch("urllib.request.OpenerDirector.open", return_value=_mock_response(200)):
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
            network_policy="off",
        )
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            sink.write(_trace())

        assert captured["auth"] == "Bearer secret-token"

    def test_timeout_passed_to_urlopen(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            return _mock_response(200)

        sink = HttpSink("http://example.com/traces", timeout=2.5, network_policy="off")
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            sink.write(_trace())

        assert captured["timeout"] == 2.5


class TestObservableFailures:
    def test_non_2xx_increments_failed(self):
        sink = HttpSink("http://example.com/traces", network_policy="off")
        with mock.patch("urllib.request.OpenerDirector.open", return_value=_mock_response(500)):
            sink.write(_trace())
        assert sink.failed == 1

    def test_connection_error_increments_failed(self):
        import urllib.error
        sink = HttpSink("http://example.com/traces", network_policy="off")
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=urllib.error.URLError("connection refused")):
            sink.write(_trace())
        assert sink.failed == 1

    def test_timeout_increments_failed(self):
        sink = HttpSink("http://example.com/traces", network_policy="off")
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=TimeoutError("timed out")):
            sink.write(_trace())
        assert sink.failed == 1

    def test_multiple_failures_counted(self):
        import urllib.error
        sink = HttpSink("http://example.com/traces", network_policy="off")
        exc = urllib.error.URLError("refused")
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=exc):
            sink.write(_trace())
            sink.write(_trace())
            sink.write(_trace())
        assert sink.failed == 3

    def test_failure_does_not_raise(self):
        import urllib.error
        sink = HttpSink("http://example.com/traces", network_policy="off")
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=urllib.error.URLError("refused")):
            # Must return without raising, not propagate.
            sink.write(_trace())

    def test_404_is_a_failure(self):
        sink = HttpSink("http://example.com/traces", network_policy="off")
        with mock.patch("urllib.request.OpenerDirector.open", return_value=_mock_response(404)):
            sink.write(_trace())
        assert sink.failed == 1

    def test_201_is_not_a_failure(self):
        sink = HttpSink("http://example.com/traces", network_policy="off")
        with mock.patch("urllib.request.OpenerDirector.open", return_value=_mock_response(201)):
            sink.write(_trace())
        assert sink.failed == 0


class TestNetworkPolicy:
    """
    network_policy's three modes, plus the allow_private_network /
    allow_insecure_http escape hatches. DNS is mocked (traceact._netguard's
    own resolve_host wraps socket.getaddrinfo for this one call) so these
    stay offline and deterministic regardless of live network access.
    """

    def _mock_private_dns(self):
        import socket as _socket
        return mock.patch.object(
            _netguard.socket, "getaddrinfo",
            lambda host, port, **kw: [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))],
        )

    def _mock_public_dns(self):
        import socket as _socket
        return mock.patch.object(
            _netguard.socket, "getaddrinfo",
            lambda host, port, **kw: [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
        )

    def test_default_policy_is_warn(self):
        with self._mock_public_dns():
            sink = HttpSink("https://collector.example.com/traces")
        assert sink.network_policy == "warn"

    def test_warn_mode_warns_once_at_construction_for_unsafe_destination(self):
        with self._mock_private_dns():
            with pytest.warns(_netguard.NetworkGuardWarning):
                HttpSink("https://internal.example.com/traces")

    def test_warn_mode_does_not_warn_for_safe_destination(self):
        with self._mock_public_dns():
            with warnings_none():
                HttpSink("https://collector.example.com/traces")

    def test_warn_mode_still_delivers_to_unsafe_destination(self):
        # Warn never blocks — the whole point is "nothing that works today
        # stops working," only now with a warning attached.
        with self._mock_private_dns():
            with pytest.warns(_netguard.NetworkGuardWarning):
                sink = HttpSink("https://internal.example.com/traces")
        with mock.patch("urllib.request.OpenerDirector.open", return_value=_mock_response(200)):
            sink.write(_trace())
        assert sink.failed == 0

    def test_off_mode_never_warns(self):
        with self._mock_private_dns():
            with warnings_none():
                HttpSink("https://internal.example.com/traces", network_policy="off")

    def test_enforce_mode_blocks_unsafe_destination_without_connecting(self):
        opened = []
        sink = HttpSink("https://internal.example.com/traces", network_policy="enforce")
        with self._mock_private_dns():
            with mock.patch("urllib.request.OpenerDirector.open",
                            side_effect=lambda *a, **k: opened.append(1)):
                sink.write(_trace())
        assert sink.failed == 1
        assert opened == []  # never attempted a connection

    def test_enforce_mode_does_not_raise_out_of_write(self):
        sink = HttpSink("https://internal.example.com/traces", network_policy="enforce")
        with self._mock_private_dns():
            sink.write(_trace())  # must return without raising, matching every other failure mode

    def test_enforce_mode_allows_safe_destination(self):
        # enforce mode re-checks at every write(), not just construction —
        # the DNS mock has to stay active for the write() call too.
        sink = HttpSink("https://collector.example.com/traces", network_policy="enforce")
        with self._mock_public_dns():
            with mock.patch("urllib.request.OpenerDirector.open", return_value=_mock_response(200)):
                sink.write(_trace())
        assert sink.failed == 0

    def test_allow_private_network_permits_enforce_mode_delivery(self):
        sink = HttpSink(
            "https://internal.example.com/traces",
            network_policy="enforce",
            allow_private_network=True,
        )
        with self._mock_private_dns():
            with mock.patch("urllib.request.OpenerDirector.open", return_value=_mock_response(200)):
                sink.write(_trace())
        assert sink.failed == 0

    def test_loopback_http_allowed_by_default_even_under_enforce(self):
        sink = HttpSink("http://127.0.0.1:4318/traces", network_policy="enforce")
        with mock.patch("urllib.request.OpenerDirector.open", return_value=_mock_response(200)):
            sink.write(_trace())
        assert sink.failed == 0

    def test_allow_insecure_http_false_blocks_loopback_under_enforce(self):
        sink = HttpSink(
            "http://127.0.0.1:4318/traces",
            network_policy="enforce",
            allow_insecure_http=False,
        )
        with mock.patch("urllib.request.OpenerDirector.open", return_value=_mock_response(200)):
            sink.write(_trace())
        assert sink.failed == 1

    def test_invalid_network_policy_rejected(self):
        with pytest.raises(ValueError, match="network_policy"):
            HttpSink("https://example.com/traces", network_policy="bogus")


class warnings_none:
    """Context manager asserting no warning of any kind was raised."""

    def __enter__(self):
        import warnings
        self._catch = warnings.catch_warnings(record=True)
        self._record = self._catch.__enter__()
        warnings.simplefilter("always")
        return self

    def __exit__(self, exc_type, exc, tb):
        self._catch.__exit__(exc_type, exc, tb)
        assert self._record == [], f"unexpected warnings: {self._record}"
        return False


class TestPublicExport:
    def test_importable_from_top_level(self):
        from traceact import HttpSink as HS
        assert HS is HttpSink
