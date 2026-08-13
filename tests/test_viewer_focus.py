# tests/test_viewer_focus.py
#
# Tests for the viewer's focus hook (`traceact view --focus-hook URL`).
#
# The contract being tested: with a hook configured, POST /api/focus forwards
# the full trace record — every field, including ones traceact has never heard
# of — to the hook URL, answers 2xx-from-hook with {"ok": true}, and turns
# everything else (non-2xx, refused connection, timeout) into a 502 the
# front-end shows as a notice. The hook can never block the viewer beyond its
# ~1s timeout, and a server started without the flag serves no hook at all.
#
# Failure paths first: a hook that is down, slow, or erroring is the case the
# feature has to survive; the happy path is the least interesting test here.

import http.server
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from traceact.viewer import instance
from traceact.viewer.cli import main as cli_main
from traceact.viewer.server import (
    ViewerServer,
    ViewerState,
    _validate_focus_hook,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serve(focus_hook=None, token=None):
    """Start a viewer on a free port; returns (base_url, state, shutdown)."""
    state = ViewerState()
    server = ViewerServer("127.0.0.1", 0, state, token=token,
                          focus_hook=focus_hook)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return f"http://127.0.0.1:{port}", state, shutdown


def _start_hook(status=200, delay=0.0):
    """
    A controllable HTTP endpoint standing in for a hook consumer (the relay
    of a browser extension, say). Records every POST it receives; can answer
    any status and can stall before answering.

    Returns (url, received, shutdown) where received is a list of dicts:
    {"path", "content_type", "body"} per request.
    """
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep test output clean
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            received.append({
                "path": self.path,
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            })
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                # The viewer gave up waiting (its timeout is shorter than
                # `delay`); answering a closed connection is expected here.
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return f"http://127.0.0.1:{port}/focus", received, shutdown


def _dead_port_url():
    """An http URL on a port nothing is listening on (bound, then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/focus"


def _post(url, payload, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    all_headers = {"Content-Type": "application/json"}
    all_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=all_headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


_RECORD = {
    "trace_id": "trc_focus1",
    "action": "checkout",
    "started_at": "2026-08-13T10:00:00Z",
    "status": "completed",
}

# The extra identity fields a browser-tracing producer relies on surviving.
# Deliberately not fields traceact knows about — that's the point.
_EXTRAS = {
    "browser_label": "work-brave",
    "tab_id": 4711,
    "window_id": 2,
    "page_load_id": "pl_9c2f",
    "client_meta": {"viewport": [1440, 900], "agent": "brave/1.66"},
}


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

class TestFocusHookUrlValidation:
    @pytest.mark.parametrize("bad", [
        "ftp://127.0.0.1/focus",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "not a url",
        "127.0.0.1:8899/focus",   # no scheme
        "http://",                # scheme but no host
        "https://",
    ])
    def test_non_http_urls_are_rejected(self, bad):
        with pytest.raises(ValueError):
            _validate_focus_hook(bad)
        with pytest.raises(ValueError):
            ViewerServer("127.0.0.1", 0, ViewerState(), focus_hook=bad)

    @pytest.mark.parametrize("good", [
        "http://127.0.0.1:8899/focus",
        "http://localhost:8899/focus",
        "https://relay.example.com/focus",
        "http://127.0.0.1:8899",     # no path
        "http://[::1]:8899/focus",   # IPv6 loopback
    ])
    def test_http_and_https_urls_are_accepted(self, good):
        assert _validate_focus_hook(good) == good

    def test_empty_and_none_mean_no_hook(self):
        assert _validate_focus_hook(None) is None
        assert _validate_focus_hook("") is None


# ---------------------------------------------------------------------------
# Forwarding failure paths
# ---------------------------------------------------------------------------

class TestFocusForwardingFailures:
    def test_hook_down_reports_failure_and_viewer_survives(self):
        url, _state, shutdown = _serve(focus_hook=_dead_port_url())
        try:
            status, body = _post(f"{url}/api/focus", _RECORD)
            assert status == 502
            assert body["ok"] is False
            assert "focus hook" in body["error"]
            # The viewer itself is unharmed.
            status, health = _get(f"{url}/api/health")
            assert status == 200 and health["status"] == "ok"
        finally:
            shutdown()

    def test_slow_hook_times_out_within_about_a_second(self):
        hook_url, _received, stop_hook = _start_hook(delay=3.0)
        url, _state, shutdown = _serve(focus_hook=hook_url)
        try:
            started = time.monotonic()
            status, body = _post(f"{url}/api/focus", _RECORD)
            elapsed = time.monotonic() - started
            assert status == 502
            assert body["ok"] is False
            # 1s timeout plus overhead — never the hook's full 3s stall.
            assert elapsed < 2.5
        finally:
            shutdown()
            stop_hook()

    def test_non_2xx_from_hook_reports_failure(self):
        hook_url, _received, stop_hook = _start_hook(status=500)
        url, _state, shutdown = _serve(focus_hook=hook_url)
        try:
            status, body = _post(f"{url}/api/focus", _RECORD)
            assert status == 502
            assert body["ok"] is False
            assert "500" in body["error"]
        finally:
            shutdown()
            stop_hook()

    def test_no_hook_configured_is_404(self):
        url, _state, shutdown = _serve()
        try:
            status, body = _post(f"{url}/api/focus", _RECORD)
            assert status == 404
            assert "focus hook" in body["error"]
        finally:
            shutdown()

    def test_malformed_body_is_400_and_nothing_is_forwarded(self):
        hook_url, received, stop_hook = _start_hook()
        url, _state, shutdown = _serve(focus_hook=hook_url)
        try:
            status, _body = _post(f"{url}/api/focus", None,
                                  raw=b"not json at all")
            assert status == 400
            # A JSON body that isn't an object is not a trace record either.
            status, _body = _post(f"{url}/api/focus", [1, 2, 3])
            assert status == 400
            assert received == []
        finally:
            shutdown()
            stop_hook()

    def test_token_gate_covers_focus(self):
        hook_url, received, stop_hook = _start_hook()
        url, _state, shutdown = _serve(focus_hook=hook_url, token="tkn-xyz")
        try:
            status, _body = _post(f"{url}/api/focus", _RECORD)
            assert status == 403
            assert received == []
            status, body = _post(f"{url}/api/focus", _RECORD,
                                 headers={"X-TraceAct-Token": "tkn-xyz"})
            assert status == 200 and body["ok"] is True
        finally:
            shutdown()
            stop_hook()


# ---------------------------------------------------------------------------
# Record fidelity
# ---------------------------------------------------------------------------

class TestFocusRecordFidelity:
    def test_record_with_unknown_extra_fields_arrives_intact(self):
        hook_url, received, stop_hook = _start_hook()
        url, _state, shutdown = _serve(focus_hook=hook_url)
        try:
            record = {**_RECORD, **_EXTRAS}
            status, body = _post(f"{url}/api/focus", record)
            assert status == 200 and body["ok"] is True
            assert len(received) == 1
            assert received[0]["content_type"] == "application/json"
            assert json.loads(received[0]["body"]) == record
        finally:
            shutdown()
            stop_hook()

    def test_record_without_extra_fields_arrives_intact(self):
        hook_url, received, stop_hook = _start_hook()
        url, _state, shutdown = _serve(focus_hook=hook_url)
        try:
            status, body = _post(f"{url}/api/focus", _RECORD)
            assert status == 200 and body["ok"] is True
            assert json.loads(received[0]["body"]) == _RECORD
        finally:
            shutdown()
            stop_hook()

    def test_stream_carries_unknown_extra_fields_to_the_client(self, tmp_path):
        # The other half of the pipeline the hook depends on: a record's
        # extra fields must survive file → SourceReader → SSE stream, or the
        # browser would have nothing to POST back. Reads the stream's first
        # (snapshot) event the same way the page's EventSource would.
        record = {**_RECORD, **_EXTRAS}
        f = tmp_path / "traces.jsonl"
        f.write_text(json.dumps(record) + "\n")

        url, state, shutdown = _serve()
        try:
            name = state.add_source(str(f))
            with urllib.request.urlopen(
                f"{url}/api/stream?source={name}&limit=10", timeout=5
            ) as resp:
                payload = resp.readline().decode("utf-8")
            assert payload.startswith("data: ")
            msg = json.loads(payload[len("data: "):])
            assert msg["kind"] == "snapshot"
            assert msg["traces"] == [record]
        finally:
            shutdown()


# ---------------------------------------------------------------------------
# Health advertisement
# ---------------------------------------------------------------------------

class TestHealthAdvertisesFocusHook:
    def test_health_says_true_with_a_hook(self):
        url, _state, shutdown = _serve(focus_hook="http://127.0.0.1:1/focus")
        try:
            _status, health = _get(f"{url}/api/health")
            assert health["focus_hook"] is True
        finally:
            shutdown()

    def test_health_says_false_without(self):
        url, _state, shutdown = _serve()
        try:
            _status, health = _get(f"{url}/api/health")
            assert health["focus_hook"] is False
        finally:
            shutdown()


# ---------------------------------------------------------------------------
# CLI and launch_or_connect
# ---------------------------------------------------------------------------

class TestCliFocusHook:
    def test_invalid_url_fails_fast_with_a_message(self, capsys):
        code = cli_main(["view", "--focus-hook", "ftp://nope", "--no-browser"])
        assert code == 2
        err = capsys.readouterr().err
        assert "focus hook" in err
        assert "ftp://nope" in err

    def test_parser_accepts_the_flag(self):
        from traceact.viewer.cli import _build_parser
        args = _build_parser().parse_args(
            ["view", "traces.jsonl", "--focus-hook", "http://127.0.0.1:9/f"]
        )
        assert args.focus_hook == "http://127.0.0.1:9/f"

    def test_flag_defaults_to_none(self):
        from traceact.viewer.cli import _build_parser
        args = _build_parser().parse_args(["view", "traces.jsonl"])
        assert args.focus_hook is None

    def test_launch_or_connect_passes_the_flag_to_the_spawned_cli(
        self, monkeypatch
    ):
        spawned = {}

        def fake_popen(cmd, **kwargs):
            spawned["cmd"] = cmd

        monkeypatch.setattr(instance, "find_running", lambda: None)
        # launch_or_connect imports subprocess locally, so the module-level
        # Popen is the one to patch.
        import subprocess as _subprocess
        monkeypatch.setattr(_subprocess, "Popen", fake_popen)
        monkeypatch.setattr(instance, "probe",
                            lambda *a, **k: {"status": "ok"})

        url = instance.launch_or_connect(
            port=59999, focus_hook="http://127.0.0.1:8899/focus", timeout=0.2,
        )
        cmd = spawned["cmd"]
        assert "--focus-hook" in cmd
        assert cmd[cmd.index("--focus-hook") + 1] == "http://127.0.0.1:8899/focus"
        assert url.startswith("http://127.0.0.1:59999")

    def test_launch_or_connect_without_the_flag_omits_it(self, monkeypatch):
        spawned = {}

        def fake_popen(cmd, **kwargs):
            spawned["cmd"] = cmd

        monkeypatch.setattr(instance, "find_running", lambda: None)
        import subprocess as _subprocess
        monkeypatch.setattr(_subprocess, "Popen", fake_popen)
        monkeypatch.setattr(instance, "probe",
                            lambda *a, **k: {"status": "ok"})

        instance.launch_or_connect(port=59998, timeout=0.2)
        assert "--focus-hook" not in spawned["cmd"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
