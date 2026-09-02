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
import unittest.mock as mock
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


# ---------------------------------------------------------------------------
# Request body cap
# ---------------------------------------------------------------------------

class TestFocusBodyCap:
    def test_oversized_body_is_rejected_and_nothing_is_forwarded(self):
        # The server answers 413 without ever reading the oversized body
        # into memory — which means it can respond before the client has
        # finished sending several MB over the same socket, and some
        # clients (including urllib, observed here) see a connection reset
        # rather than a fully parsed 413. Either outcome is correct: the
        # point is that nothing this large ever reaches the hook. The payload
        # is otherwise a valid, forwardable trace record (padded with a
        # large field) — a malformed body would 400 regardless of the size
        # cap, which would leave this test unable to tell "the cap worked"
        # from "the cap doesn't exist and the shape check caught it anyway".
        hook_url, received, stop_hook = _start_hook()
        url, _state, shutdown = _serve(focus_hook=hook_url)
        try:
            from traceact.viewer.server import _MAX_BODY_BYTES
            padded = {**_RECORD, "padding": "x" * (_MAX_BODY_BYTES + 1)}
            try:
                status, body = _post(f"{url}/api/focus", padded)
                assert status == 413
                assert "byte" in body["error"]
            except (urllib.error.URLError, ConnectionError):
                pass  # connection reset before the response was read — acceptable
            assert received == []
        finally:
            shutdown()
            stop_hook()

    def test_body_at_the_limit_is_not_rejected_for_size(self):
        # Right at the cap: not a size rejection (may still 400 on shape,
        # since this payload isn't a JSON object — the point is it gets past
        # the size gate to that check at all).
        hook_url, _received, stop_hook = _start_hook()
        url, _state, shutdown = _serve(focus_hook=hook_url)
        try:
            from traceact.viewer.server import _MAX_BODY_BYTES
            at_limit = b'"' + b"x" * (_MAX_BODY_BYTES - 2) + b'"'  # valid JSON string
            status, body = _post(f"{url}/api/focus", None, raw=at_limit)
            assert status == 400  # a JSON string, not an object — shape rejection
            assert "byte" not in body["error"]
        finally:
            shutdown()
            stop_hook()


# ---------------------------------------------------------------------------
# Redirect refusal
# ---------------------------------------------------------------------------

def _start_capturing_server_any_method():
    """
    A local server recording every request it receives, by method — unlike
    _start_hook (POST-only), this also implements do_GET. A refused 302
    after a POST is only provably un-followed if a *converted-to-GET*
    redirect (urllib's default behavior for 301/302/303 after POST) would
    also have been caught here; a POST-only handler would silently 501 an
    incoming GET and this test would pass whether the redirect was refused
    or was simply followed to an endpoint that doesn't speak GET.
    """
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _record(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            hits.append(self.command)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self._record()

        def do_POST(self):
            self._record()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return f"http://127.0.0.1:{port}/elsewhere", hits, shutdown


class TestFocusRedirectRefusal:
    def test_hook_redirect_is_not_followed(self):
        # The redirect *target* — if the hook were followed (as a POST, or
        # as a GET, which is what urllib's default redirect handling does
        # to a 301/302/303 after a POST), this records the hit; it must not.
        target_url, target_hits, stop_target = _start_capturing_server_any_method()

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(302)
                self.send_header("Location", target_url)
                self.send_header("Content-Length", "0")
                self.end_headers()

        redirect_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_port = redirect_server.server_address[1]
        redirect_thread = threading.Thread(target=redirect_server.serve_forever, daemon=True)
        redirect_thread.start()
        hook_url = f"http://127.0.0.1:{redirect_port}/"

        url, _state, shutdown = _serve(focus_hook=hook_url)
        try:
            status, body = _post(f"{url}/api/focus", _RECORD)
            # The refused redirect surfaces as a delivery failure, same as
            # any other hook problem — never followed to the target.
            assert status == 502
            assert body["ok"] is False
            assert "302" in body["error"]
            assert target_hits == []
        finally:
            shutdown()
            redirect_server.shutdown()
            redirect_server.server_close()
            redirect_thread.join(timeout=2)
            stop_target()


# ---------------------------------------------------------------------------
# Auto-enabled token auth for a non-loopback focus hook
# ---------------------------------------------------------------------------

class TestAutoTokenOnNonLoopbackFocusHook:
    def _run_view(self, argv, monkeypatch):
        from traceact.viewer import cli

        captured = {}
        fake_server = mock.MagicMock()
        fake_server.serve_forever.side_effect = lambda: None

        def fake_start(h, p, s, base_path="", token=None, focus_hook=None):
            captured["token"] = token
            captured["focus_hook"] = focus_hook
            return fake_server, p

        monkeypatch.setattr(cli, "_start_server", fake_start)
        monkeypatch.setattr(cli._instance, "write_state", mock.MagicMock())
        monkeypatch.setattr(cli._instance, "clear_state", mock.MagicMock())
        monkeypatch.setattr(cli._instance, "find_running", lambda: None)

        parser = cli._build_parser()
        rc = cli._run_view(parser.parse_args(argv))
        return rc, captured

    def test_loopback_hook_does_not_auto_enable_token(self, monkeypatch):
        rc, captured = self._run_view(
            ["view", "--no-browser", "--focus-hook", "http://127.0.0.1:9999/focus"],
            monkeypatch,
        )
        assert rc == 0
        assert captured["token"] is None

    def test_nonloopback_hook_auto_enables_token(self, monkeypatch, capsys):
        # An IP literal needs no DNS — deterministic without mocking.
        rc, captured = self._run_view(
            ["view", "--no-browser", "--focus-hook", "http://93.184.216.34:9999/focus"],
            monkeypatch,
        )
        assert rc == 0
        token = captured["token"]
        assert token and len(token) >= 24
        assert "token auth is on automatically" in capsys.readouterr().err

    def test_explicit_require_token_silences_the_auto_note(self, monkeypatch, capsys):
        rc, captured = self._run_view(
            ["view", "--no-browser", "--require-token",
             "--focus-hook", "http://93.184.216.34:9999/focus"],
            monkeypatch,
        )
        assert rc == 0
        assert captured["token"] is not None
        assert "automatically" not in capsys.readouterr().err

    def test_no_focus_hook_no_auto_token(self, monkeypatch):
        rc, captured = self._run_view(["view", "--no-browser"], monkeypatch)
        assert rc == 0
        assert captured["token"] is None


class TestLaunchOrConnectAutoToken:
    def test_spawn_command_carries_require_token_for_nonloopback_hook(
        self, monkeypatch
    ):
        # launch_or_connect must mirror the CLI's non-loopback rule and pass
        # the flag explicitly, so its own wait loop knows a token is coming.
        spawned = {}

        def fake_popen(cmd, **kwargs):
            spawned["cmd"] = cmd

        monkeypatch.setattr(instance, "find_running", lambda: None)
        import subprocess as _subprocess
        monkeypatch.setattr(_subprocess, "Popen", fake_popen)
        monkeypatch.setattr(instance, "probe", lambda *a, **k: {"status": "ok"})

        instance.launch_or_connect(
            port=59997, focus_hook="http://93.184.216.34:9/focus", timeout=0.2,
        )
        assert "--require-token" in spawned["cmd"]

    def test_loopback_hook_spawn_stays_untokened(self, monkeypatch):
        spawned = {}

        def fake_popen(cmd, **kwargs):
            spawned["cmd"] = cmd

        monkeypatch.setattr(instance, "find_running", lambda: None)
        import subprocess as _subprocess
        monkeypatch.setattr(_subprocess, "Popen", fake_popen)
        monkeypatch.setattr(instance, "probe", lambda *a, **k: {"status": "ok"})

        instance.launch_or_connect(
            port=59996, focus_hook="http://127.0.0.1:9/focus", timeout=0.2,
        )
        assert "--require-token" not in spawned["cmd"]

    def test_live_nonloopback_hook_returns_usable_tokened_url(
        self, tmp_path, monkeypatch
    ):
        # End to end against a really spawned viewer: the URL must carry the
        # token, and that token must authenticate against the running API.
        # A custom port keeps the spawned CLI out of the shared state file,
        # which is the one case where the token can only arrive via the
        # CLI's own printed URL.
        monkeypatch.setattr(instance, "_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(instance, "_STATE_FILE",
                            str(tmp_path / "viewer.json"))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        try:
            url = instance.launch_or_connect(
                port=port,
                focus_hook="http://93.184.216.34:9/focus",
                timeout=10.0,
            )
            assert f":{port}" in url
            assert "token=" in url
            from urllib.parse import parse_qs, urlparse
            token = parse_qs(urlparse(url).query)["token"][0]
            health = instance.probe("127.0.0.1", port, token=token)
            assert health is not None and health.get("focus_hook") is True
        finally:
            import subprocess as _subprocess
            # No leading dashes in the pattern: pkill would read them as its
            # own options. "port <n>" still matches the spawned command line.
            _subprocess.run(["pkill", "-f", f"port {port}"],
                            capture_output=True)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
