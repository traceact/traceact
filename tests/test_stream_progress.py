# tests/test_stream_progress.py
#
# Tests for in-flight trace streaming: open traces append "running" stub
# lines so long-running work is visible before it finishes, and so a crash
# leaves evidence instead of losing the trace entirely.
#
# The contract under test, from the design in the PRD:
#   - off by default; nothing changes for anyone who didn't opt in
#   - grace: traces shorter than the throttle interval write no stubs at all
#   - throttle: at most one stub per interval
#   - errors bypass the throttle and carry the full record
#   - heartbeat: a quiet open trace still reports in
#   - readers collapse stubs last-wins per trace_id; a stub with no final
#     record (the crash case) survives as evidence
#
# Tests use tiny intervals (0.05s) so the timing rules are exercised
# honestly without slow tests.

import json
import time

import pytest

from traceact import (
    ActionTrace,
    JsonlSink,
    TraceConfig,
    TraceLog,
    configure,
    reset_config,
    traced_action,
)
from traceact.viewer.reader import SourceReader

INTERVAL = 0.05


@pytest.fixture
def stream_sink(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(project="streamtest",
              config=TraceConfig(sink_mode="blocking",
                                 stream_progress=INTERVAL),
              sinks=[JsonlSink(str(path))])
    yield path
    reset_config()


def _lines(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines()]


def _stubs(path):
    return [r for r in _lines(path) if r.get("in_flight")]


def _finals(path):
    return [r for r in _lines(path) if not r.get("in_flight")]


class TestOffByDefault:
    def test_no_stubs_without_opt_in(self, tmp_path):
        path = tmp_path / "traces.jsonl"
        configure(project="streamtest",
                  config=TraceConfig(sink_mode="blocking"),
                  sinks=[JsonlSink(str(path))])
        try:
            with ActionTrace.start(action="t") as t:
                time.sleep(0.15)
                t.step("worked a while")
            assert _stubs(path) == []
            assert len(_finals(path)) == 1
        finally:
            reset_config()

    def test_validation_rejects_bad_spellings(self):
        for bad in ("bogus", -1, 0, [1]):
            with pytest.raises(ValueError, match="stream_progress"):
                TraceConfig(stream_progress=bad)

    def test_valid_spellings_accepted(self):
        for good in (True, False, None, 1, 0.5, "full"):
            TraceConfig(stream_progress=good)


class TestGraceAndThrottle:
    def test_fast_trace_writes_no_stubs(self, stream_sink):
        # Finishes well inside the grace window: streaming must cost nothing.
        with ActionTrace.start(action="fast") as t:
            t.step("one")
            t.step("two")
        assert _stubs(stream_sink) == []
        assert len(_finals(stream_sink)) == 1

    def test_slow_trace_writes_stubs(self, stream_sink):
        with ActionTrace.start(action="slow") as t:
            time.sleep(INTERVAL * 1.5)
            t.step("past the grace threshold")
        stubs = _stubs(stream_sink)
        assert len(stubs) == 1
        stub = stubs[0]
        assert stub["status"] == "running"
        assert stub["progress_seq"] == 1
        assert stub["steps_count"] == 1
        assert stub["last_step"] == "past the grace threshold"
        assert stub["action"] == "slow"
        assert "snapshot_at" in stub
        # The stub is the slim shape — no accumulated lists.
        assert "steps" not in stub
        assert len(json.dumps(stub)) < 600

    def test_burst_is_throttled_to_one_per_interval(self, stream_sink):
        with ActionTrace.start(action="burst") as t:
            time.sleep(INTERVAL * 1.5)
            for i in range(50):
                t.step(f"step {i}")  # 50 recordings, ~no elapsed time
        # One stub when the burst starts; the other 49 fall inside the
        # throttle window.
        assert len(_stubs(stream_sink)) == 1

    def test_final_record_still_written_normally(self, stream_sink):
        with ActionTrace.start(action="slow") as t:
            time.sleep(INTERVAL * 1.5)
            t.step("progress")
        finals = _finals(stream_sink)
        assert len(finals) == 1
        assert finals[0]["status"] == "completed"
        assert "progress_seq" not in finals[0]


class TestErrorEscalation:
    def test_error_snapshot_bypasses_throttle_and_carries_the_record(
            self, stream_sink):
        with ActionTrace.start(action="failing") as t:
            time.sleep(INTERVAL * 1.5)
            t.step("about to fail")           # stub 1 (throttle window opens)
            t.event(kind="tool", operation="call", target="broken",
                    status="failed",
                    error={"type": "ToolError", "message": "exploded"})
        stubs = _stubs(stream_sink)
        # The error snapshot fired inside the same throttle window as stub 1.
        assert len(stubs) == 2
        error_snap = stubs[-1]
        # Full record, not the slim shape: the whole story so far is on disk
        # even if the process dies right after.
        assert error_snap["steps"][0]["label"] == "about to fail"
        assert error_snap["errors"]
        assert error_snap["status"] == "running"


class TestFullMode:
    def test_full_mode_snapshots_carry_the_whole_record(self, tmp_path):
        path = tmp_path / "traces.jsonl"
        configure(project="streamtest",
                  config=TraceConfig(sink_mode="blocking",
                                     stream_progress="full"),
                  sinks=[JsonlSink(str(path))])
        try:
            with ActionTrace.start(action="verbose") as t:
                time.sleep(1.1)  # "full" uses the 1s default interval
                t.step("visible in the snapshot")
            stubs = _stubs(path)
            assert len(stubs) >= 1
            assert stubs[0]["steps"][0]["label"] == "visible in the snapshot"
        finally:
            reset_config()

    def test_per_decorator_override(self, tmp_path):
        # One function opts into streaming while the package stays off —
        # the standard config-precedence chain, nothing bespoke.
        path = tmp_path / "traces.jsonl"
        configure(project="streamtest",
                  config=TraceConfig(sink_mode="blocking"),
                  sinks=[JsonlSink(str(path))])
        try:
            from traceact.context import get_active_trace

            @traced_action(action="streamer",
                           config=TraceConfig(stream_progress=INTERVAL))
            def slow_one():
                time.sleep(INTERVAL * 1.5)
                get_active_trace().step("progress")

            @traced_action(action="quiet")
            def quiet_one():
                time.sleep(INTERVAL * 1.5)
                get_active_trace().step("progress")

            slow_one()
            quiet_one()
            stub_actions = {s["action"] for s in _stubs(path)}
            assert stub_actions == {"streamer"}
        finally:
            reset_config()


class TestHeartbeat:
    def test_quiet_open_trace_still_reports(self, stream_sink):
        # Heartbeat fires at 5× the interval (0.25s here). A trace that
        # records once and then hangs must keep appearing on disk.
        cm = ActionTrace.start(action="hanging")
        trace = cm.__enter__()
        try:
            time.sleep(INTERVAL * 1.5)
            trace.step("last sign of life")   # stub 1
            time.sleep(INTERVAL * 12)         # quiet for ~2 heartbeat periods
        finally:
            cm.__exit__(None, None, None)
        stubs = _stubs(stream_sink)
        assert len(stubs) >= 2, "a hung trace must heartbeat, not go silent"
        assert stubs[-1]["steps_count"] == 1  # nothing new, still reporting

    def test_no_stubs_after_finish(self, stream_sink):
        with ActionTrace.start(action="done") as t:
            time.sleep(INTERVAL * 1.5)
            t.step("only stub")
        count_after_finish = len(_stubs(stream_sink))
        time.sleep(INTERVAL * 12)  # if deregistration failed, more arrive
        assert len(_stubs(stream_sink)) == count_after_finish


class TestReaderDedupe:
    def _write(self, path, records):
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def _stub(self, tid, seq, action="job.run", started="2026-07-30T10:00:00Z"):
        return {"trace_id": tid, "action": action, "status": "running",
                "started_at": started, "in_flight": True, "progress_seq": seq}

    def _final(self, tid, action="job.run", started="2026-07-30T10:00:00Z"):
        return {"trace_id": tid, "action": action, "status": "completed",
                "started_at": started}

    def test_viewer_snapshot_collapses_to_final(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        self._write(f, [self._stub("t1", 1), self._stub("t1", 2),
                        self._final("t1")])
        traces = SourceReader(str(f)).snapshot(limit=50)
        assert len(traces) == 1
        assert traces[0]["status"] == "completed"

    def test_viewer_snapshot_keeps_orphaned_stub(self, tmp_path):
        # The crash case: stubs with no final record are the only evidence
        # the trace ever ran. They must surface, marked running.
        f = tmp_path / "traces.jsonl"
        self._write(f, [self._stub("crashed", 1), self._stub("crashed", 2),
                        self._final("other", started="2026-07-30T10:01:00Z")])
        traces = SourceReader(str(f)).snapshot(limit=50)
        assert len(traces) == 2
        crashed = [t for t in traces if t["trace_id"] == "crashed"][0]
        assert crashed["status"] == "running"
        assert crashed["progress_seq"] == 2  # the latest stub, not the first

    def test_viewer_poll_dedupes_within_one_window(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        self._write(f, [self._final("old")])
        reader = SourceReader(str(f))
        reader.snapshot(limit=50)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(self._stub("t2", 1)) + "\n")
            fh.write(json.dumps(self._stub("t2", 2)) + "\n")
        result = reader.poll(limit=50)
        assert result["kind"] == "append"
        assert len(result["traces"]) == 1
        assert result["traces"][0]["progress_seq"] == 2

    def test_tracelog_all_collapses_to_final(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        self._write(f, [self._stub("t1", 1), self._final("t1"),
                        self._stub("open", 1,
                                   started="2026-07-30T10:02:00Z")])
        records = TraceLog(str(f)).all()
        by_id = {r["trace_id"]: r for r in records}
        assert len(records) == 2
        assert by_id["t1"]["status"] == "completed"
        assert by_id["open"]["status"] == "running"

    def test_tracelog_stub_hidden_when_final_filtered_out(self, tmp_path):
        # The final record fails the predicate; its stubs must not sneak in
        # as if the trace were still running.
        f = tmp_path / "traces.jsonl"
        self._write(f, [self._stub("t1", 1), self._final("t1")])
        records = TraceLog(str(f)).filter(status="running").all()
        assert records == []

    def test_tracelog_last_collapses_too(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        self._write(f, [self._stub("t1", 1), self._stub("t1", 2),
                        self._final("t1")])
        records = TraceLog(str(f)).last(10)
        assert len(records) == 1
        assert records[0]["status"] == "completed"

    def test_live_end_to_end_single_row(self, stream_sink):
        # A streamed trace leaves stubs plus a final on disk; every reader
        # view of it must be exactly one record, the final one.
        with ActionTrace.start(action="e2e") as t:
            time.sleep(INTERVAL * 1.5)
            t.step("streaming")
        assert len(_stubs(stream_sink)) >= 1     # stubs were written
        log_view = TraceLog(str(stream_sink)).all()
        viewer_view = SourceReader(str(stream_sink)).snapshot(limit=50)
        assert len(log_view) == 1
        assert len(viewer_view) == 1
        assert log_view[0]["status"] == "completed"
