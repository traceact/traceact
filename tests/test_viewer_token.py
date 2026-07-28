# tests/test_viewer_token.py
#
# Tests for opt-in token auth on the viewer server.
#
# The boundary being tested: a localhost server runs with its owner's file
# permissions, so on a shared machine a *different OS user* could read traces
# through it by hitting 127.0.0.1 directly. A token in the 0600 state file is
# what shuts that out — same-user tools read the file and authenticate
# transparently; other accounts can't read it and get 403.
#
# The default (no token) must stay byte-for-byte as open as it always was:
# the feature is additive, and every pre-existing launcher must keep working.

import json
import os
import stat
import threading
import unittest.mock as mock
import urllib.error
import urllib.request

import pytest

from traceact.viewer import instance
from traceact.viewer.server import ViewerServer, ViewerState

TOKEN = "test-token-abc123"


def _serve(token=None, base_path=""):
    """Start a server on a free port; returns (base_url, state, shutdown)."""
    state = ViewerState()
    server = ViewerServer("127.0.0.1", 0, state, base_path=base_path,
                          token=token)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return f"http://127.0.0.1:{port}", state, shutdown


@pytest.fixture
def tokened():
    url, state, shutdown = _serve(token=TOKEN)
    try:
        yield url, state
    finally:
        shutdown()


@pytest.fixture
def open_server():
    url, state, shutdown = _serve()
    try:
        yield url, state
    finally:
        shutdown()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url, payload, headers=None):
    all_headers = {"Content-Type": "application/json"}
    all_headers.update(headers or {})
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=all_headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TestDefaultStaysOpen:
    """No token configured — the server behaves exactly as before."""

    def test_api_needs_no_token(self, open_server):
        url, _ = open_server
        status, body = _get(f"{url}/api/health")
        assert status == 200
        assert json.loads(body)["status"] == "ok"

    def test_a_stray_token_param_is_ignored(self, open_server):
        # A bookmarked tokened URL pasted at an untokened server must not 403.
        url, _ = open_server
        status, _body = _get(f"{url}/api/health?token=whatever")
        assert status == 200

    def test_empty_token_means_no_token(self):
        # ViewerServer(token="") must not create a server that requires the
        # empty string — "" normalises to None, i.e. open.
        url, _state, shutdown = _serve(token="")
        try:
            status, _body = _get(f"{url}/api/health")
            assert status == 200
        finally:
            shutdown()


class TestTokenGate:
    def test_api_refused_without_token(self, tokened):
        url, _ = tokened
        status, body = _get(f"{url}/api/health")
        assert status == 403
        assert "error" in json.loads(body)

    def test_query_param_token_accepted(self, tokened):
        url, _ = tokened
        status, body = _get(f"{url}/api/health?token={TOKEN}")
        assert status == 200
        assert json.loads(body)["status"] == "ok"

    def test_header_token_accepted(self, tokened):
        url, _ = tokened
        status, _body = _get(f"{url}/api/health",
                             headers={"X-TraceAct-Token": TOKEN})
        assert status == 200

    def test_wrong_query_token_refused(self, tokened):
        url, _ = tokened
        status, _body = _get(f"{url}/api/health?token=wrong")
        assert status == 403

    def test_wrong_header_token_refused(self, tokened):
        url, _ = tokened
        status, _body = _get(f"{url}/api/health",
                             headers={"X-TraceAct-Token": "wrong"})
        assert status == 403

    def test_token_prefix_is_not_enough(self, tokened):
        # A comparison that stopped at the shorter string's end would pass
        # a truncated token.
        url, _ = tokened
        status, _body = _get(f"{url}/api/health?token={TOKEN[:-1]}")
        assert status == 403

    def test_every_get_endpoint_is_gated(self, tokened, tmp_path):
        url, state = tokened
        src = tmp_path / "traces.jsonl"
        src.write_text('{"trace_id":"t1","action":"a"}\n')
        name = state.add_source(str(src))
        for path in ("/api/sources", "/api/doctor",
                     f"/api/stream?source={name}",
                     f"/api/query?source={name}",
                     f"/api/export?source={name}"):
            status, _body = _get(f"{url}{path}")
            assert status == 403, f"{path} answered {status} without a token"

    def test_post_refused_without_token(self, tokened, tmp_path):
        url, state = tokened
        src = tmp_path / "traces.jsonl"
        src.write_text("")
        status, _body = _post(f"{url}/api/sources", {"path": str(src)})
        assert status == 403
        assert len(state.sources) == 0  # the add must not have happened

    def test_post_with_header_token_works(self, tokened, tmp_path):
        url, state = tokened
        src = tmp_path / "traces.jsonl"
        src.write_text("")
        status, body = _post(f"{url}/api/sources", {"path": str(src)},
                             headers={"X-TraceAct-Token": TOKEN})
        assert status == 200
        assert json.loads(body)["name"] in state.sources

    def test_query_token_composes_with_other_params(self, tokened, tmp_path):
        # The browser appends token to URLs that already carry a query.
        url, state = tokened
        src = tmp_path / "traces.jsonl"
        src.write_text('{"trace_id":"t1","action":"a","started_at":"2026-01-01T00:00:00Z"}\n')
        name = state.add_source(str(src))
        status, body = _get(f"{url}/api/export?source={name}&token={TOKEN}")
        assert status == 200
        assert b"t1" in body

    def test_page_shell_and_assets_stay_open(self, tokened):
        # The HTML/CSS/JS is package content anyone can pip install; trace
        # data flows only through the API, so the gate lives there. Asset
        # requests made by the browser carry no token, and must not need one.
        url, _ = tokened
        for path in ("/", "/static/app.js", "/static/styles.css"):
            status, _body = _get(f"{url}{path}")
            assert status == 200, f"{path} should be served without a token"

    def test_gate_applies_under_a_base_path(self, tmp_path):
        url, state, shutdown = _serve(token=TOKEN, base_path="/audit")
        try:
            status, _body = _get(f"{url}/audit/api/health")
            assert status == 403
            status, _body = _get(f"{url}/audit/api/health?token={TOKEN}")
            assert status == 200
        finally:
            shutdown()


class TestStateFile:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(instance, "_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(instance, "_STATE_FILE",
                            str(tmp_path / "viewer.json"))

    def test_token_is_stored(self):
        instance.write_state("127.0.0.1", 8765, token=TOKEN)
        with open(instance._STATE_FILE) as f:
            assert json.load(f)["token"] == TOKEN

    def test_no_token_key_when_untokened(self):
        instance.write_state("127.0.0.1", 8765)
        with open(instance._STATE_FILE) as f:
            assert "token" not in json.load(f)

    def test_state_file_is_owner_only(self):
        # 0600 is what keeps the token out of other users' hands; the whole
        # scheme rests on this one permission bit.
        instance.write_state("127.0.0.1", 8765, token=TOKEN)
        mode = stat.S_IMODE(os.stat(instance._STATE_FILE).st_mode)
        assert mode == 0o600

    def test_find_running_returns_token_for_live_tokened_viewer(self):
        url, _state, shutdown = _serve(token=TOKEN)
        try:
            port = int(url.rsplit(":", 1)[1])
            instance.write_state("127.0.0.1", port, token=TOKEN)
            found = instance.find_running()
            assert found is not None
            assert found["token"] == TOKEN
        finally:
            shutdown()

    def test_stateless_probe_reads_tokened_viewer_as_absent(self):
        # A state file lacking the token (say, clobbered by an old traceact
        # version) must fail closed: the probe is refused, so the caller
        # starts its own viewer rather than silently talking to one it can't
        # authenticate against.
        url, _state, shutdown = _serve(token=TOKEN)
        try:
            port = int(url.rsplit(":", 1)[1])
            instance.write_state("127.0.0.1", port)  # no token recorded
            assert instance.find_running() is None
        finally:
            shutdown()


class TestInstanceHelpersCarryTheToken:
    def test_add_source_to_with_token(self, tmp_path):
        url, state, shutdown = _serve(token=TOKEN)
        try:
            port = int(url.rsplit(":", 1)[1])
            src = tmp_path / "traces.jsonl"
            src.write_text("")
            added = instance.add_source_to("127.0.0.1", port, str(src),
                                           token=TOKEN)
            assert added is not None
            assert added["name"] in state.sources
        finally:
            shutdown()

    def test_add_source_to_without_token_fails_observably(self, tmp_path):
        url, state, shutdown = _serve(token=TOKEN)
        try:
            port = int(url.rsplit(":", 1)[1])
            src = tmp_path / "traces.jsonl"
            src.write_text("")
            assert instance.add_source_to("127.0.0.1", port, str(src)) is None
            assert len(state.sources) == 0
        finally:
            shutdown()

    def test_list_source_names_with_token(self, tmp_path):
        url, state, shutdown = _serve(token=TOKEN)
        try:
            port = int(url.rsplit(":", 1)[1])
            src = tmp_path / "traces.jsonl"
            src.write_text("")
            name = state.add_source(str(src))
            assert instance.list_source_names("127.0.0.1", port,
                                              token=TOKEN) == [name]
            assert instance.list_source_names("127.0.0.1", port) == []
        finally:
            shutdown()


class TestViewerUrl:
    def test_bare(self):
        assert instance._viewer_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/"

    def test_source_only(self):
        url = instance._viewer_url("127.0.0.1", 8765, source="agora")
        assert url == "http://127.0.0.1:8765/?source=agora"

    def test_token_only(self):
        url = instance._viewer_url("127.0.0.1", 8765, token="abc")
        assert url == "http://127.0.0.1:8765/?token=abc"

    def test_source_and_token(self):
        url = instance._viewer_url("127.0.0.1", 8765, source="agora",
                                   token="abc")
        assert url == "http://127.0.0.1:8765/?source=agora&token=abc"

    def test_base_path_included(self):
        url = instance._viewer_url("127.0.0.1", 8765, base_path="/audit",
                                   source="agora")
        assert url == "http://127.0.0.1:8765/audit/?source=agora"

    def test_values_are_url_quoted(self):
        url = instance._viewer_url("127.0.0.1", 8765, source="my log/1")
        assert " " not in url
        assert "my%20log%2F1" in url


class TestLaunchOrConnectReuse:
    def test_running_viewers_token_is_used_and_returned(self):
        with mock.patch.object(
            instance, "find_running",
            return_value={"host": "127.0.0.1", "port": 8765,
                          "base_path": "", "token": "abc"},
        ), mock.patch.object(
            instance, "add_source_to",
            return_value={"name": "mylog", "path": "/x/mylog.jsonl"},
        ) as add:
            url = instance.launch_or_connect(source="/x/mylog.jsonl")
        assert add.call_args.kwargs["token"] == "abc"
        assert url == "http://127.0.0.1:8765/?source=mylog&token=abc"

    def test_untokened_running_viewer_wins_over_require_token(self):
        # Same policy as base_path: the running instance's settings hold. A
        # caller asking for a token doesn't get one bolted onto a server that
        # started without it — that can't be done after bind.
        with mock.patch.object(
            instance, "find_running",
            return_value={"host": "127.0.0.1", "port": 8765},
        ):
            url = instance.launch_or_connect(require_token=True)
        assert url == "http://127.0.0.1:8765/"


class TestCliRequireToken:
    def _run_view(self, argv, monkeypatch):
        from traceact.viewer import cli

        captured = {}
        fake_server = mock.MagicMock()
        fake_server.serve_forever.side_effect = lambda: None

        def fake_start(h, p, s, base_path="", token=None):
            captured["token"] = token
            return fake_server, p

        write_state = mock.MagicMock()
        monkeypatch.setattr(cli, "_start_server", fake_start)
        monkeypatch.setattr(cli._instance, "write_state", write_state)
        monkeypatch.setattr(cli._instance, "clear_state", mock.MagicMock())
        monkeypatch.setattr(cli._instance, "find_running", lambda: None)

        parser = cli._build_parser()
        rc = cli._run_view(parser.parse_args(argv))
        return rc, captured, write_state

    def test_flag_generates_a_token(self, monkeypatch, capsys):
        rc, captured, write_state = self._run_view(
            ["view", "--no-browser", "--require-token"], monkeypatch)
        assert rc == 0
        token = captured["token"]
        assert token and len(token) >= 24
        # The token reaches the user via the printed URL and the state file —
        # the two same-user channels — and nowhere else.
        assert f"token={token}" in capsys.readouterr().out
        assert write_state.call_args.kwargs["token"] == token

    def test_without_flag_no_token(self, monkeypatch, capsys):
        rc, captured, write_state = self._run_view(
            ["view", "--no-browser"], monkeypatch)
        assert rc == 0
        assert captured["token"] is None
        assert write_state.call_args.kwargs["token"] is None
        assert "token=" not in capsys.readouterr().out

    def test_each_launch_gets_a_fresh_token(self, monkeypatch):
        _rc, first, _ = self._run_view(
            ["view", "--no-browser", "--require-token"], monkeypatch)
        _rc, second, _ = self._run_view(
            ["view", "--no-browser", "--require-token"], monkeypatch)
        assert first["token"] != second["token"]


class TestCliSourcePin:
    """
    The front-end no longer auto-attaches to the first source, so every CLI
    path that knows which source it loaded must pin it with ?source= — or a
    plain `traceact view mylog.jsonl` would open on the picker instead of
    the file it was just given.
    """

    def _run_view(self, argv, monkeypatch, tmp_path):
        from traceact.viewer import cli

        fake_server = mock.MagicMock()
        fake_server.serve_forever.side_effect = lambda: None
        monkeypatch.setattr(
            cli, "_start_server",
            lambda h, p, s, base_path="", token=None: (fake_server, p),
        )
        monkeypatch.setattr(cli._instance, "write_state", mock.MagicMock())
        monkeypatch.setattr(cli._instance, "clear_state", mock.MagicMock())
        monkeypatch.setattr(cli._instance, "find_running", lambda: None)

        parser = cli._build_parser()
        return cli._run_view(parser.parse_args(argv))

    def test_fresh_start_with_source_pins_it(self, monkeypatch, tmp_path,
                                             capsys):
        src = tmp_path / "traces.jsonl"
        src.write_text('{"project": "agora", "action": "x"}\n')
        rc = self._run_view(["view", "--no-browser", str(src)],
                            monkeypatch, tmp_path)
        assert rc == 0
        assert "?source=agora" in capsys.readouterr().out

    def test_fresh_start_without_source_has_no_pin(self, monkeypatch,
                                                   tmp_path, capsys):
        rc = self._run_view(["view", "--no-browser"], monkeypatch, tmp_path)
        assert rc == 0
        assert "source=" not in capsys.readouterr().out

    def test_reuse_pins_the_added_source(self, monkeypatch, capsys):
        from traceact.viewer import cli

        monkeypatch.setattr(
            cli._instance, "find_running",
            lambda: {"host": "127.0.0.1", "port": 8765, "base_path": "",
                     "token": None},
        )
        monkeypatch.setattr(
            cli._instance, "add_source_to",
            lambda *a, **k: {"name": "agora", "path": "/x"},
        )
        parser = cli._build_parser()
        rc = cli._run_view(parser.parse_args(
            ["view", "--no-browser", "/x/traces.jsonl"]))
        assert rc == 0
        assert "?source=agora" in capsys.readouterr().out
