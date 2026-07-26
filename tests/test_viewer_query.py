# tests/test_viewer_query.py
#
# Tests for GET /api/query — the viewer's server-side search endpoint.
#
# This is the first pytest coverage viewer/server.py has had at all, so the
# fixture below starts a real ViewerServer on an OS-assigned port and makes
# real HTTP requests against it (urllib, stdlib only) rather than calling
# handler methods directly — an HTTP-level test is what actually exercises
# request parsing, status codes, and the JSON response shape a real client
# depends on.

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from traceact.viewer.server import ViewerServer, ViewerState


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _trace(action="note.create", kind="app", status="completed",
           started_at="2026-07-25T10:00:00Z", **extra):
    t = {
        "trace_id": f"trc_{action.replace('.', '_')}",
        "action": action,
        "started_at": started_at,
        "kind": kind,
        "status": status,
    }
    t.update(extra)
    return t


@pytest.fixture
def running_server():
    """
    Start a real ViewerServer on an OS-assigned free port, yield (base_url,
    state) so tests can register sources, and shut it down cleanly afterward.
    """
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


def _get(base_url, path):
    """GET path, returning (status_code, parsed_json)."""
    try:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# Basic query behaviour
# ---------------------------------------------------------------------------

class TestQueryBasics:
    def test_unknown_source_returns_404(self, running_server):
        base_url, _state = running_server
        status, body = _get(base_url, "/api/query?source=nope")
        assert status == 404
        assert "error" in body

    def test_missing_source_param_returns_404(self, running_server):
        base_url, _state = running_server
        status, body = _get(base_url, "/api/query")
        assert status == 404

    def test_no_filters_returns_everything_up_to_limit(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("a"), _trace("b"), _trace("c")])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}")
        assert status == 200
        assert body["count"] == 3
        assert body["scan_capped"] is False

    def test_exact_match_filter(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", status="completed"),
            _trace("b", status="failed"),
        ])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&status=failed")
        assert status == 200
        assert body["count"] == 1
        assert body["traces"][0]["action"] == "b"

    def test_contains_operator(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("note.create"), _trace("note.delete"), _trace("user.update"),
        ])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&action__contains=note")
        assert status == 200
        assert body["count"] == 2

    def test_startswith_operator(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("note.create"), _trace("user.update")])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&action__startswith=note")
        assert status == 200
        assert body["count"] == 1

    def test_endswith_operator(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("note.create"), _trace("user.create"), _trace("note.delete")])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&action__endswith=create")
        assert status == 200
        assert body["count"] == 2

    def test_multiple_filters_anded(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", kind="db", status="failed"),
            _trace("b", kind="db", status="completed"),
            _trace("c", kind="app", status="failed"),
        ])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&kind=db&status=failed")
        assert status == 200
        assert body["count"] == 1
        assert body["traces"][0]["action"] == "a"

    def test_no_matches_returns_empty_not_error(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("a", status="completed")])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&status=failed")
        assert status == 200
        assert body["count"] == 0
        assert body["traces"] == []

    def test_newest_first_ordering(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("old", started_at="2026-07-25T08:00:00Z"),
            _trace("new", started_at="2026-07-25T09:00:00Z"),
        ])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}")
        assert [t["action"] for t in body["traces"]] == ["new", "old"]

    def test_folder_source(self, running_server, tmp_path):
        base_url, state = running_server
        _write_jsonl(tmp_path / "a.jsonl", [_trace("a1")])
        _write_jsonl(tmp_path / "b.jsonl", [_trace("b1"), _trace("b2")])
        name = state.add_source(str(tmp_path))

        status, body = _get(base_url, f"/api/query?source={name}")
        assert status == 200
        assert body["count"] == 3

    def test_malformed_lines_tolerated(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        f.write_text(
            json.dumps(_trace("good")) + "\n"
            "not json\n"
            '{"incomplete": true}\n'
        )
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}")
        assert status == 200
        assert body["count"] == 1


# ---------------------------------------------------------------------------
# __re rejection — the ReDoS guard
# ---------------------------------------------------------------------------

class TestReOperatorRejected:
    def test_re_operator_returns_400(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("note.create")])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&action__re=.*")
        assert status == 400
        assert "error" in body

    def test_re_rejected_even_alongside_valid_filters(self, running_server, tmp_path):
        # A request mixing one allowed filter with one __re filter must still
        # be rejected outright, not partially applied.
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("a", status="failed")])
        name = state.add_source(str(f))

        status, body = _get(
            base_url, f"/api/query?source={name}&status=failed&action__re=.*"
        )
        assert status == 400

    def test_unknown_operator_also_rejected(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("a")])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&action__bogus=x")
        assert status == 400


# ---------------------------------------------------------------------------
# limit capping
# ---------------------------------------------------------------------------

class TestLimitCapping:
    def test_huge_requested_limit_is_capped(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace(f"t{i}") for i in range(5)])
        name = state.add_source(str(f))

        # Requesting far more than _QUERY_MAX_LIMIT must not error or hang;
        # it should just be silently capped.
        status, body = _get(base_url, f"/api/query?source={name}&limit=999999999")
        assert status == 200
        assert body["count"] == 5  # only 5 traces exist; cap just ceilings it

    def test_limit_below_matches_truncates(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace(f"t{i}", started_at=f"2026-07-25T{i:02d}:00:00Z")
            for i in range(10)
        ])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&limit=3")
        assert status == 200
        assert body["count"] == 3

    def test_zero_or_negative_limit_does_not_crash(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("a")])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&limit=0")
        assert status == 200  # clamped to at least 1, not a 500

        status, body = _get(base_url, f"/api/query?source={name}&limit=-5")
        assert status == 200

    def test_non_numeric_limit_falls_back_to_default(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("a")])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&limit=not-a-number")
        assert status == 200
        assert body["count"] == 1


# ---------------------------------------------------------------------------
# scan_capped surfaces through the HTTP layer
# ---------------------------------------------------------------------------

class TestScanCappedSurfaces:
    def test_scan_capped_true_when_source_scanner_capped(self, running_server, tmp_path, monkeypatch):
        # Force the endpoint's internal scan cap down to something tiny so
        # the test doesn't need to write hundreds of thousands of lines to
        # exercise the same code path _QUERY_MAX_LINES_SCANNED guards.
        import traceact.viewer.server as server_mod
        monkeypatch.setattr(server_mod, "_QUERY_MAX_LINES_SCANNED", 3)

        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace(f"t{i}") for i in range(20)])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}")
        assert status == 200
        assert body["scan_capped"] is True


# ---------------------------------------------------------------------------
# limit_reached surfaces through the HTTP layer — the observable signal for
# the silent server-side limit clamp.
# ---------------------------------------------------------------------------

class TestLimitReachedSurfaces:
    def test_false_when_fewer_matches_than_limit(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("a"), _trace("b")])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&limit=10")
        assert status == 200
        assert body["limit_reached"] is False

    def test_true_when_more_matches_than_requested_limit(self, running_server, tmp_path):
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace(f"t{i}", started_at=f"2026-07-25T{i:02d}:00:00Z")
            for i in range(15)
        ])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&limit=10")
        assert status == 200
        assert body["limit_reached"] is True
        assert body["count"] == 10

    def test_true_when_server_side_cap_clamps_a_huge_requested_limit(
        self, running_server, tmp_path, monkeypatch
    ):
        # The specific scenario this signal exists for: caller asks for far
        # more than _QUERY_MAX_LIMIT, gets silently clamped, and needs a way
        # to know the response isn't exhaustive.
        import traceact.viewer.server as server_mod
        monkeypatch.setattr(server_mod, "_QUERY_MAX_LIMIT", 5)

        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace(f"t{i}", started_at=f"2026-07-25T{i:02d}:00:00Z")
            for i in range(50)
        ])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&limit=999999")
        assert status == 200
        assert body["count"] == 5  # clamped to the patched _QUERY_MAX_LIMIT
        assert body["limit_reached"] is True

    def test_false_when_exactly_n_matches_exist(self, running_server, tmp_path):
        # The case that originally broke a naive "len(result) >= n" check:
        # deque(maxlen=n) always ends at length <= n regardless of whether n
        # or ten thousand matches were found, so this has to come from an
        # actual count, not an inference from the returned list's size.
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace(f"t{i}", started_at=f"2026-07-25T{i:02d}:00:00Z")
            for i in range(10)
        ])
        name = state.add_source(str(f))

        status, body = _get(base_url, f"/api/query?source={name}&limit=10")
        assert status == 200
        assert body["count"] == 10
        assert body["limit_reached"] is False


# ---------------------------------------------------------------------------
# Concurrency: a query running alongside an active SSE tail on the same source
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_query_alongside_active_stream_does_not_corrupt_either(
        self, running_server, tmp_path
    ):
        # Closing the stream connection early (below) makes the stdlib server
        # print a ConnectionResetError traceback to stderr from its own
        # keep-alive connection handling — not from _serve_stream, which
        # already catches this at its own layer, and not from anything this
        # test asserts on. Any real browser tab closed mid-stream produces the
        # exact same stderr noise against the real server; it's expected here
        # too, not a sign this test is failing or hiding a real error.
        base_url, state = running_server
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace(f"t{i}") for i in range(5)])
        name = state.add_source(str(f))

        stream_error = []

        def tail_stream():
            try:
                req = urllib.request.urlopen(
                    f"{base_url}/api/stream?source={name}&limit=100", timeout=3
                )
                req.read(200)  # read the snapshot event, then stop
                req.close()
            except Exception as e:
                stream_error.append(e)

        stream_thread = threading.Thread(target=tail_stream)
        stream_thread.start()
        time.sleep(0.1)  # let the stream connection open first

        status, body = _get(base_url, f"/api/query?source={name}")
        stream_thread.join(timeout=5)

        assert status == 200
        assert body["count"] == 5
        assert stream_error == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
