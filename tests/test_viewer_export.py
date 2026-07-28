# tests/test_viewer_export.py
#
# Tests for GET /api/export — the whole of one source as a .jsonl download.
#
# The endpoint exists for deployments where the viewer's UI can't be reached
# from the user's browser: the host app proxies this one route, the user
# downloads their traces, and runs `traceact view` on them locally. That makes
# fidelity the thing to test hardest. An export that silently drops the
# malformed lines, or reorders records, produces a file that disagrees with
# what the app actually wrote.

import json
import threading
import urllib.error
import urllib.request

import pytest

from traceact.viewer.server import ViewerServer, ViewerState, _export_filename


@pytest.fixture
def running_server():
    state = ViewerState()
    server = ViewerServer("127.0.0.1", 0, state)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _trace(trace_id, started_at, action="note.create"):
    return {
        "trace_id": trace_id,
        "action": action,
        "started_at": started_at,
        "kind": "app",
        "status": "completed",
    }


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _records(body):
    """Parse an exported body back into trace dicts."""
    return [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]


class TestExportSingleFile:
    def test_returns_the_file_verbatim(self, running_server, tmp_path):
        # A single-file source is streamed without parsing, so the download
        # must be byte-identical to what the app wrote.
        url, state = running_server
        src = tmp_path / "traces.jsonl"
        raw = (
            json.dumps(_trace("t1", "2026-01-01T00:00:00Z")) + "\n"
            + json.dumps(_trace("t2", "2026-01-02T00:00:00Z")) + "\n"
        )
        src.write_text(raw)
        name = state.add_source(str(src))

        status, _headers, body = _get(f"{url}/api/export?source={name}")
        assert status == 200
        assert body == raw.encode("utf-8")

    def test_download_headers(self, running_server, tmp_path):
        url, state = running_server
        src = tmp_path / "traces.jsonl"
        src.write_text(json.dumps(_trace("t1", "2026-01-01T00:00:00Z")) + "\n")
        name = state.add_source(str(src))

        _status, headers, body = _get(f"{url}/api/export?source={name}")
        assert headers["Content-Type"] == "application/x-ndjson"
        assert headers["Content-Disposition"] == f'attachment; filename="{name}.jsonl"'
        assert headers["Content-Length"] == str(len(body))

    def test_order_is_not_disturbed(self, running_server, tmp_path):
        # Even out-of-order records in one file pass through as written: the
        # single-file path does no sorting, so nothing can be rearranged.
        url, state = running_server
        src = tmp_path / "traces.jsonl"
        src.write_text(
            json.dumps(_trace("late", "2026-06-01T00:00:00Z")) + "\n"
            + json.dumps(_trace("early", "2026-01-01T00:00:00Z")) + "\n"
        )
        name = state.add_source(str(src))

        _status, _headers, body = _get(f"{url}/api/export?source={name}")
        assert [r["trace_id"] for r in _records(body)] == ["late", "early"]

    def test_empty_file_exports_empty(self, running_server, tmp_path):
        url, state = running_server
        src = tmp_path / "traces.jsonl"
        src.write_text("")
        name = state.add_source(str(src))

        status, _headers, body = _get(f"{url}/api/export?source={name}")
        assert status == 200
        assert body == b""


class TestExportFolderMerge:
    def _folder_source(self, state, tmp_path):
        folder = tmp_path / "shards"
        folder.mkdir()
        (folder / "a.jsonl").write_text(
            json.dumps(_trace("a1", "2026-01-01T00:00:00Z")) + "\n"
            + json.dumps(_trace("a2", "2026-01-03T00:00:00Z")) + "\n"
        )
        (folder / "b.jsonl").write_text(
            json.dumps(_trace("b1", "2026-01-02T00:00:00Z")) + "\n"
            + json.dumps(_trace("b2", "2026-01-04T00:00:00Z")) + "\n"
        )
        return state.add_source(str(folder))

    def test_segments_merge_in_chronological_order(self, running_server, tmp_path):
        url, state = running_server
        name = self._folder_source(state, tmp_path)

        status, _headers, body = _get(f"{url}/api/export?source={name}")
        assert status == 200
        ids = [r["trace_id"] for r in _records(body)]
        assert ids == ["a1", "b1", "a2", "b2"]

    def test_no_record_is_lost_in_the_merge(self, running_server, tmp_path):
        url, state = running_server
        name = self._folder_source(state, tmp_path)

        _status, _headers, body = _get(f"{url}/api/export?source={name}")
        assert len(_records(body)) == 4

    def test_output_is_valid_jsonl(self, running_server, tmp_path):
        # Every line must parse on its own; a merge that concatenated a file
        # lacking a trailing newline would glue two records together.
        url, state = running_server
        folder = tmp_path / "shards"
        folder.mkdir()
        (folder / "a.jsonl").write_text(
            json.dumps(_trace("a1", "2026-01-01T00:00:00Z"))  # no trailing \n
        )
        (folder / "b.jsonl").write_text(
            json.dumps(_trace("b1", "2026-01-02T00:00:00Z"))  # no trailing \n
        )
        name = state.add_source(str(folder))

        _status, _headers, body = _get(f"{url}/api/export?source={name}")
        lines = [ln for ln in body.decode("utf-8").split("\n") if ln.strip()]
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # raises if two records were glued together

    def test_equal_timestamps_keep_segment_order(self, running_server, tmp_path):
        url, state = running_server
        folder = tmp_path / "shards"
        folder.mkdir()
        same = "2026-01-01T00:00:00Z"
        (folder / "a.jsonl").write_text(json.dumps(_trace("from-a", same)) + "\n")
        (folder / "b.jsonl").write_text(json.dumps(_trace("from-b", same)) + "\n")
        name = state.add_source(str(folder))

        _status, _headers, body = _get(f"{url}/api/export?source={name}")
        # Files are read in sorted order, and the merge is stable, so a ties
        # ordering is deterministic rather than luck of the scheduler.
        assert [r["trace_id"] for r in _records(body)] == ["from-a", "from-b"]

    def test_empty_folder_exports_empty(self, running_server, tmp_path):
        url, state = running_server
        folder = tmp_path / "empty"
        folder.mkdir()
        name = state.add_source(str(folder))

        status, _headers, body = _get(f"{url}/api/export?source={name}")
        assert status == 200
        assert body == b""


class TestExportFidelity:
    """Nothing may be dropped in silence — an export is meant to be a copy."""

    def test_malformed_lines_are_preserved(self, running_server, tmp_path):
        url, state = running_server
        folder = tmp_path / "shards"
        folder.mkdir()
        (folder / "a.jsonl").write_text(
            json.dumps(_trace("good", "2026-01-02T00:00:00Z")) + "\n"
        )
        (folder / "b.jsonl").write_text("{ this is not json\n")
        name = state.add_source(str(folder))

        _status, _headers, body = _get(f"{url}/api/export?source={name}")
        text = body.decode("utf-8")
        assert "{ this is not json" in text
        assert "good" in text

    def test_unparseable_lines_sort_first(self, running_server, tmp_path):
        # They carry no timestamp to place them by, and the reader's own
        # convention is that a missing started_at sorts first.
        url, state = running_server
        folder = tmp_path / "shards"
        folder.mkdir()
        (folder / "a.jsonl").write_text(
            json.dumps(_trace("good", "2026-01-02T00:00:00Z")) + "\n"
        )
        (folder / "b.jsonl").write_text("not json at all\n")
        name = state.add_source(str(folder))

        _status, _headers, body = _get(f"{url}/api/export?source={name}")
        lines = [ln for ln in body.decode("utf-8").split("\n") if ln.strip()]
        assert lines[0] == "not json at all"

    def test_records_without_started_at_are_kept(self, running_server, tmp_path):
        url, state = running_server
        folder = tmp_path / "shards"
        folder.mkdir()
        (folder / "a.jsonl").write_text(
            json.dumps(_trace("timed", "2026-01-02T00:00:00Z")) + "\n"
        )
        (folder / "b.jsonl").write_text(
            json.dumps({"trace_id": "untimed", "action": "x"}) + "\n"
        )
        name = state.add_source(str(folder))

        _status, _headers, body = _get(f"{url}/api/export?source={name}")
        ids = [r["trace_id"] for r in _records(body)]
        assert ids == ["untimed", "timed"]

    def test_non_trace_json_is_kept(self, running_server, tmp_path):
        # /api/export copies a source; it is not the reader's job to decide
        # which lines look enough like traces to survive the trip.
        url, state = running_server
        folder = tmp_path / "shards"
        folder.mkdir()
        (folder / "a.jsonl").write_text(json.dumps({"unrelated": "object"}) + "\n")
        (folder / "b.jsonl").write_text(
            json.dumps(_trace("t1", "2026-01-01T00:00:00Z")) + "\n"
        )
        name = state.add_source(str(folder))

        _status, _headers, body = _get(f"{url}/api/export?source={name}")
        assert len(_records(body)) == 2

    def test_blank_lines_are_dropped(self, running_server, tmp_path):
        # A blank line is not a record, and keeping it would only make the
        # merged stream messier. Every actual record still survives.
        url, state = running_server
        folder = tmp_path / "shards"
        folder.mkdir()
        (folder / "a.jsonl").write_text(
            json.dumps(_trace("t1", "2026-01-01T00:00:00Z")) + "\n\n\n"
        )
        (folder / "b.jsonl").write_text(
            "\n" + json.dumps(_trace("t2", "2026-01-02T00:00:00Z")) + "\n"
        )
        name = state.add_source(str(folder))

        _status, _headers, body = _get(f"{url}/api/export?source={name}")
        assert body.decode("utf-8").count("\n\n") == 0
        assert len(_records(body)) == 2

    def test_unicode_survives_the_round_trip(self, running_server, tmp_path):
        url, state = running_server
        folder = tmp_path / "shards"
        folder.mkdir()
        rec = _trace("t1", "2026-01-01T00:00:00Z", action="note.créate")
        rec["note"] = "日本語 — emoji 🎯"
        (folder / "a.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (folder / "b.jsonl").write_text(
            json.dumps(_trace("t2", "2026-01-02T00:00:00Z")) + "\n"
        )
        name = state.add_source(str(folder))

        _status, _headers, body = _get(f"{url}/api/export?source={name}")
        out = _records(body)[0]
        assert out["action"] == "note.créate"
        assert out["note"] == "日本語 — emoji 🎯"


class TestExportErrors:
    def test_missing_source_param_is_400(self, running_server):
        url, _state = running_server
        status, _headers, body = _get(f"{url}/api/export")
        assert status == 400
        assert "error" in json.loads(body)

    def test_unknown_source_is_404(self, running_server):
        url, _state = running_server
        status, _headers, body = _get(f"{url}/api/export?source=nope")
        assert status == 404
        assert json.loads(body)["source"] == "nope"

    def test_a_path_cannot_be_passed_as_a_source(self, running_server):
        # Sources are addressed by registered name only. A path in the query
        # string must not reach the filesystem.
        url, _state = running_server
        status, _headers, _body = _get(f"{url}/api/export?source=/etc/passwd")
        assert status == 404

    def test_registered_source_that_vanished_is_not_a_crash(
        self, running_server, tmp_path
    ):
        url, state = running_server
        src = tmp_path / "traces.jsonl"
        src.write_text(json.dumps(_trace("t1", "2026-01-01T00:00:00Z")) + "\n")
        name = state.add_source(str(src))
        src.unlink()

        status, _headers, body = _get(f"{url}/api/export?source={name}")
        # The source resolves to no files at all, so this is an empty export
        # rather than an error: the name is still registered and valid.
        assert status == 200
        assert body == b""


class TestExportFilename:
    @pytest.mark.parametrize("name,expected", [
        ("agora", "agora.jsonl"),
        ("my app", "my-app.jsonl"),
        ("a/b", "a-b.jsonl"),
        ('quote"name', "quote-name.jsonl"),
        ("../../etc/passwd", "etc-passwd.jsonl"),
        ("trailing.", "trailing.jsonl"),
        ("", "traces.jsonl"),
        ("///", "traces.jsonl"),
        ("日本語", "traces.jsonl"),
    ])
    def test_names_are_reduced_to_safe_filenames(self, name, expected):
        assert _export_filename(name) == expected

    def test_header_stays_well_formed_for_an_awkward_name(
        self, running_server, tmp_path
    ):
        # A quote in the source name would break out of the header's quoting
        # if it were interpolated raw.
        url, state = running_server
        src = tmp_path / "traces.jsonl"
        src.write_text(json.dumps(_trace("t1", "2026-01-01T00:00:00Z")) + "\n")
        state.add_source(str(src), name='we"ird name')

        _status, headers, _body = _get(f"{url}/api/export?source=we%22ird%20name")
        disposition = headers["Content-Disposition"]
        assert disposition == 'attachment; filename="we-ird-name.jsonl"'
        assert disposition.count('"') == 2


class TestExportUnderBasePath:
    """Both features have to work together, not just each on its own."""

    def test_export_is_reachable_under_a_prefix(self, tmp_path):
        state = ViewerState()
        server = ViewerServer("127.0.0.1", 0, state, base_path="/audit-viewer")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            src = tmp_path / "traces.jsonl"
            src.write_text(json.dumps(_trace("t1", "2026-01-01T00:00:00Z")) + "\n")
            name = state.add_source(str(src))

            base = f"http://127.0.0.1:{port}"
            status, headers, body = _get(
                f"{base}/audit-viewer/api/export?source={name}"
            )
            assert status == 200
            assert headers["Content-Type"] == "application/x-ndjson"
            assert len(_records(body)) == 1

            # And not at the root.
            status, _headers, _body = _get(f"{base}/api/export?source={name}")
            assert status == 404
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
