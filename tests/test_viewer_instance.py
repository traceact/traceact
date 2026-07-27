# tests/test_viewer_instance.py
#
# Tests for viewer instance coordination: source deduplication, the
# ?source=NAME pin that makes an app open on its own source, and the
# state-file gating that keeps a deliberately-separate viewer private.
#
# These three behaviours are interlocking — the failure they prevent is one
# app's viewer showing another app's traces — so they are tested together.

import json
import unittest.mock as mock

import pytest

from traceact.viewer.server import ViewerState, _derive_name


# ---------------------------------------------------------------------------
# Source naming
# ---------------------------------------------------------------------------
#
# Nearly every app writes to some variant of <project>/data/traces/traces.jsonl,
# so a name taken from the filename alone reads "traces" for every project on
# the machine. These assert the name identifies the project instead.

class TestDeriveName:
    def test_skips_generic_dirs_to_project_name(self):
        assert _derive_name("/Users/mo/Dev/agora/data/traces/traces.jsonl") == "agora"

    def test_skips_logs_dir(self):
        assert _derive_name("/Users/mo/Dev/casewright/logs/traces.jsonl") == "casewright"

    def test_two_projects_get_different_names(self):
        # The whole point: same filename, same layout, different projects.
        a = _derive_name("/Users/mo/Dev/agora/data/traces/traces.jsonl")
        b = _derive_name("/Users/mo/Dev/casewright/data/traces/traces.jsonl")
        assert a != b

    def test_specific_filename_is_honoured(self):
        # An app that already names its file per project keeps that name —
        # the directory walk is only a fallback for generic filenames.
        assert _derive_name("/Users/mo/Dev/agora/agora_traces.jsonl") == "agora_traces"

    def test_non_generic_filename_wins_over_dir(self):
        assert _derive_name("/Users/mo/Dev/agora/data/traces/worker.jsonl") == "worker"

    def test_hidden_project_dir_loses_leading_dot(self):
        assert _derive_name("/Users/mo/.tree/traces/traces.jsonl") == "tree"

    def test_pid_shard_derives_project_not_shard(self):
        # All shards of one project must derive the same name, or a sharded
        # app fills the picker with near-identical entries.
        a = _derive_name("/Users/mo/Dev/agora/data/traces/traces.1234.jsonl")
        b = _derive_name("/Users/mo/Dev/agora/data/traces/traces.5678.jsonl")
        assert a == b == "agora"

    def test_rotated_segment_derives_project(self):
        path = "/Users/mo/Dev/agora/data/traces/traces.20260726T120000000000Z.jsonl"
        assert _derive_name(path) == "agora"

    def test_folder_source_skips_generic_components(self):
        assert _derive_name("/Users/mo/Dev/agora/data/traces") == "agora"

    def test_all_generic_falls_back(self):
        assert _derive_name("/data/traces/traces.jsonl") == "source"

    def test_generic_matching_is_case_insensitive(self):
        assert _derive_name("/Users/mo/Dev/agora/Data/Traces/Traces.jsonl") == "agora"

    def test_traceact_state_dir_is_generic(self):
        assert _derive_name("/Users/mo/.traceact/imports/dropped.jsonl") == "dropped"

    def test_bare_filename_does_not_crash(self):
        assert _derive_name("traces.jsonl") == "source"

    def test_explicit_name_overrides_derivation(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        state = ViewerState()
        assert state.add_source(str(f), name="agora") == "agora"


# ---------------------------------------------------------------------------
# add_source deduplication
# ---------------------------------------------------------------------------

class TestAddSourceDedupe:
    def test_same_path_twice_returns_same_name(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        state = ViewerState()
        first = state.add_source(str(f))
        second = state.add_source(str(f))
        assert first == second
        assert len(state.sources) == 1

    def test_repeated_adds_do_not_accumulate(self, tmp_path):
        # The reported symptom: an app calling launch_or_connect() on every
        # run produced traces-2, traces-3, traces-4 ... for one unchanging file.
        # Asserts the dedupe property, not the derived name — the name comes
        # from the tmp directory here and is covered by TestDeriveName.
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        state = ViewerState()
        for _ in range(5):
            state.add_source(str(f))
        assert len(state.sources) == 1
        assert not any(n.endswith("-2") for n in state.sources)

    def test_relative_and_absolute_path_dedupe(self, tmp_path, monkeypatch):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        monkeypatch.chdir(tmp_path)
        state = ViewerState()
        a = state.add_source("traces.jsonl")
        b = state.add_source(str(f))
        assert a == b
        assert len(state.sources) == 1

    def test_path_with_dotdot_dedupes(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        f = sub / "traces.jsonl"
        f.write_text("")
        state = ViewerState()
        a = state.add_source(str(f))
        b = state.add_source(str(sub / ".." / "sub" / "traces.jsonl"))
        assert a == b
        assert len(state.sources) == 1

    def test_distinct_paths_still_get_distinct_names(self, tmp_path):
        # Dedupe must not collapse genuinely different files that happen to
        # share a basename.
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        (a_dir / "traces.jsonl").write_text("")
        (b_dir / "traces.jsonl").write_text("")
        state = ViewerState()
        n1 = state.add_source(str(a_dir / "traces.jsonl"))
        n2 = state.add_source(str(b_dir / "traces.jsonl"))
        assert n1 != n2
        assert len(state.sources) == 2

    def test_explicit_name_still_dedupes_by_path(self, tmp_path):
        # The path is the identity; a different label for the same file must
        # not create a second entry pointing at the same data.
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        state = ViewerState()
        first = state.add_source(str(f), name="alpha")
        second = state.add_source(str(f), name="beta")
        assert first == second == "alpha"
        assert len(state.sources) == 1


# ---------------------------------------------------------------------------
# launch_or_connect returns a ?source= pin
# ---------------------------------------------------------------------------

class TestLaunchOrConnectSourcePin:
    def test_existing_viewer_url_pins_added_source(self):
        from traceact.viewer import instance

        with mock.patch.object(
            instance, "find_running",
            return_value={"host": "127.0.0.1", "port": 8765},
        ), mock.patch.object(
            instance, "add_source_to",
            return_value={"name": "mylog", "path": "/x/mylog.jsonl"},
        ):
            url = instance.launch_or_connect(source="/x/mylog.jsonl")
        assert url == "http://127.0.0.1:8765/?source=mylog"

    def test_name_is_url_quoted(self):
        from traceact.viewer import instance

        with mock.patch.object(
            instance, "find_running",
            return_value={"host": "127.0.0.1", "port": 8765},
        ), mock.patch.object(
            instance, "add_source_to",
            return_value={"name": "my log/1", "path": "/x"},
        ):
            url = instance.launch_or_connect(source="/x")
        assert " " not in url
        assert "my%20log%2F1" in url

    def test_no_source_returns_bare_url(self):
        from traceact.viewer import instance

        with mock.patch.object(
            instance, "find_running",
            return_value={"host": "127.0.0.1", "port": 8765},
        ):
            url = instance.launch_or_connect()
        assert url == "http://127.0.0.1:8765/"

    def test_explicit_name_forwarded_to_add_source(self):
        from traceact.viewer import instance

        with mock.patch.object(
            instance, "find_running",
            return_value={"host": "127.0.0.1", "port": 8765},
        ), mock.patch.object(
            instance, "add_source_to",
            return_value={"name": "agora", "path": "/x/traces.jsonl"},
        ) as add:
            url = instance.launch_or_connect(source="/x/traces.jsonl", name="agora")
        assert add.call_args.kwargs["name"] == "agora"
        assert url == "http://127.0.0.1:8765/?source=agora"

    def test_add_source_to_omits_name_when_not_given(self):
        # The payload must not carry name=None; the server treats a missing
        # name as "derive one" and an explicit null would be a different case.
        from traceact.viewer import instance

        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"name":"x","path":"/x"}'

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            instance.add_source_to("127.0.0.1", 8765, "/x/traces.jsonl")
        assert captured["body"] == {"path": "/x/traces.jsonl"}

    def test_add_source_to_includes_name_when_given(self):
        from traceact.viewer import instance

        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"name":"agora","path":"/x"}'

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            instance.add_source_to("127.0.0.1", 8765, "/x/traces.jsonl", name="agora")
        assert captured["body"]["name"] == "agora"

    def test_failed_add_falls_back_to_bare_url(self):
        # add_source_to returns None when the running viewer can't be reached;
        # a URL pinning a source that was never registered would be worse than
        # no pin at all.
        from traceact.viewer import instance

        with mock.patch.object(
            instance, "find_running",
            return_value={"host": "127.0.0.1", "port": 8765},
        ), mock.patch.object(instance, "add_source_to", return_value=None):
            url = instance.launch_or_connect(source="/x/mylog.jsonl")
        assert url == "http://127.0.0.1:8765/"


# ---------------------------------------------------------------------------
# TraceLog.view() preserves the ?source= pin
# ---------------------------------------------------------------------------

class TestViewPreservesSourcePin:
    def test_pin_survives_with_no_filters(self, tmp_path):
        from traceact import TraceLog

        f = tmp_path / "traces.jsonl"
        f.write_text("")
        with mock.patch(
            "traceact.viewer.instance.launch_or_connect",
            return_value="http://127.0.0.1:8765/?source=mylog",
        ), mock.patch("webbrowser.open"):
            url = TraceLog(str(f)).view(open_browser=False)
        assert "source=mylog" in url

    def test_pin_and_prefilters_both_present(self, tmp_path):
        from traceact import TraceLog

        f = tmp_path / "traces.jsonl"
        f.write_text("")
        with mock.patch(
            "traceact.viewer.instance.launch_or_connect",
            return_value="http://127.0.0.1:8765/?source=mylog",
        ), mock.patch("webbrowser.open"):
            url = TraceLog(str(f)).filter(status="failed").view(open_browser=False)
        assert "source=mylog" in url
        assert "pf_status=failed" in url

    def test_bare_base_url_still_works(self, tmp_path):
        # Backward compatibility: a launch_or_connect that returns no query
        # (no source given) must still produce a valid pre-filter URL.
        from traceact import TraceLog

        f = tmp_path / "traces.jsonl"
        f.write_text("")
        with mock.patch(
            "traceact.viewer.instance.launch_or_connect",
            return_value="http://127.0.0.1:8765/",
        ), mock.patch("webbrowser.open"):
            url = TraceLog(str(f)).filter(status="failed").view(open_browser=False)
        assert url.startswith("http://127.0.0.1:8765/?")
        assert "pf_status=failed" in url


# ---------------------------------------------------------------------------
# State-file gating: a private instance must not claim the shared slot
# ---------------------------------------------------------------------------

class TestPrivateInstanceStateGating:
    """
    `traceact view --port N` / `--new` starts a deliberately separate viewer.
    Writing the shared state file from one would point the next
    launch_or_connect() caller — a different app entirely — at this instance.
    """

    def _run_view(self, argv, monkeypatch):
        """Run cli._run_view with the server loop stubbed out; return mocks."""
        from traceact.viewer import cli

        write_state = mock.MagicMock()
        clear_state = mock.MagicMock()
        fake_server = mock.MagicMock()
        # serve_forever returns immediately so _run_view falls through to the
        # finally block without blocking the test.
        fake_server.serve_forever.side_effect = lambda: None

        monkeypatch.setattr(cli._instance, "write_state", write_state)
        monkeypatch.setattr(cli._instance, "clear_state", clear_state)
        monkeypatch.setattr(cli, "_start_server", lambda h, p, s: (fake_server, p))
        monkeypatch.setattr(
            cli._instance, "find_running", lambda: None
        )

        parser = cli._build_parser()
        args = parser.parse_args(argv)
        rc = cli._run_view(args)
        return rc, write_state, clear_state

    def test_default_port_writes_state(self, monkeypatch):
        rc, write_state, _ = self._run_view(["view", "--no-browser"], monkeypatch)
        assert rc == 0
        assert write_state.called

    def test_explicit_port_does_not_write_state(self, monkeypatch):
        rc, write_state, _ = self._run_view(
            ["view", "--no-browser", "--port", "8951"], monkeypatch
        )
        assert rc == 0
        assert not write_state.called

    def test_new_flag_does_not_write_state(self, monkeypatch):
        rc, write_state, _ = self._run_view(
            ["view", "--no-browser", "--new"], monkeypatch
        )
        assert rc == 0
        assert not write_state.called

    def test_private_instance_does_not_clear_shared_state_on_exit(self, monkeypatch):
        # A private instance clearing the state file on shutdown would evict a
        # still-running shared viewer's entry.
        rc, _, clear_state = self._run_view(
            ["view", "--no-browser", "--port", "8951"], monkeypatch
        )
        assert rc == 0
        assert not clear_state.called

    def test_shared_instance_clears_state_on_exit(self, monkeypatch):
        rc, _, clear_state = self._run_view(["view", "--no-browser"], monkeypatch)
        assert rc == 0
        assert clear_state.called
