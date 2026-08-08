# tests/test_sqlite_source.py
#
# Tests for reading SqliteSink databases as sources — in the viewer
# (SourceReader snapshot + tail, export, doctor) and in TraceLog.
#
# Databases are produced by the real SqliteSink, not hand-built schemas, so
# these tests break if the sink's schema and the readers ever drift apart.
# Detection is by magic bytes, not extension: SqliteSink outputs get named
# .db, .sqlite, or anything else.

import json
import sqlite3
import threading
import urllib.request

import pytest

from traceact import SqliteSink, TraceLog
from traceact.viewer import doctor
from traceact.viewer.reader import SourceReader, is_sqlite_file
from traceact.viewer.server import ViewerServer, ViewerState


def _trace(trace_id, started_at, action="job.run", project="sqltest",
           status="completed", **extra):
    rec = {
        "trace_id": trace_id, "action": action, "started_at": started_at,
        "kind": "app", "status": status, "project": project,
    }
    rec.update(extra)
    return rec


def _make_db(path, records):
    sink = SqliteSink(str(path))
    for rec in records:
        sink.write(rec)
    sink.close()
    return str(path)


class TestDetection:
    def test_sqlite_file_is_detected_by_magic_bytes(self, tmp_path):
        db = _make_db(tmp_path / "weird_extension.jsonl",
                      [_trace("t1", "2026-08-08T10:00:00Z")])
        # Extension lies; the header doesn't.
        assert is_sqlite_file(db)

    def test_jsonl_file_is_not_detected(self, tmp_path):
        f = tmp_path / "traces.db"  # extension lies the other way
        f.write_text(json.dumps(_trace("t1", "2026-08-08T10:00:00Z")) + "\n")
        assert not is_sqlite_file(str(f))

    def test_empty_and_missing_files_are_not_sqlite(self, tmp_path):
        empty = tmp_path / "empty"
        empty.write_bytes(b"")
        assert not is_sqlite_file(str(empty))
        assert not is_sqlite_file(str(tmp_path / "does-not-exist"))


class TestSourceReaderSnapshot:
    def test_snapshot_returns_newest_first(self, tmp_path):
        db = _make_db(tmp_path / "traces.db", [
            _trace("old", "2026-08-08T10:00:00Z"),
            _trace("new", "2026-08-08T11:00:00Z"),
        ])
        traces = SourceReader(db).snapshot(limit=50)
        assert [t["trace_id"] for t in traces] == ["new", "old"]

    def test_snapshot_respects_limit(self, tmp_path):
        db = _make_db(tmp_path / "traces.db", [
            _trace(f"t{i}", f"2026-08-08T10:00:{i:02d}Z") for i in range(10)
        ])
        assert len(SourceReader(db).snapshot(limit=3)) == 3

    def test_garbage_record_rows_are_skipped(self, tmp_path):
        db = _make_db(tmp_path / "traces.db",
                      [_trace("good", "2026-08-08T10:00:00Z")])
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO traces (trace_id, action, record) "
            "VALUES ('bad', 'x', '{ not json')")
        conn.commit()
        conn.close()
        traces = SourceReader(db).snapshot(limit=50)
        assert [t["trace_id"] for t in traces] == ["good"]

    def test_database_without_traces_table_is_empty_not_fatal(self, tmp_path):
        path = tmp_path / "other.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        assert SourceReader(str(path)).snapshot(limit=50) == []

    def test_wal_database_reads_fine_under_an_exclusive_writer(self, tmp_path):
        # SqliteSink enables WAL precisely so readers never block on the
        # writing application. An exclusive write transaction must not stop
        # the viewer from reading.
        db = _make_db(tmp_path / "traces.db",
                      [_trace("t1", "2026-08-08T10:00:00Z")])
        blocker = sqlite3.connect(db)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            traces = SourceReader(db).snapshot(limit=50)
            assert [t["trace_id"] for t in traces] == ["t1"]
        finally:
            blocker.rollback()
            blocker.close()

    def test_non_wal_locked_database_fails_fast_and_empty(self, tmp_path):
        # A rollback-journal database (not SqliteSink output, but sniffs as
        # SQLite) does block readers during an exclusive write. The 0.5s
        # read timeout turns that into a fast empty snapshot, not a hung
        # poll loop.
        import time
        path = tmp_path / "plain.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY, "
                     "trace_id TEXT, action TEXT, started_at TEXT, "
                     "record TEXT)")
        conn.execute(
            "INSERT INTO traces (trace_id, action, started_at, record) "
            "VALUES ('t1', 'a', '2026-08-08T10:00:00Z', ?)",
            (json.dumps(_trace("t1", "2026-08-08T10:00:00Z")),))
        conn.commit()
        conn.execute("BEGIN EXCLUSIVE")
        try:
            start = time.monotonic()
            traces = SourceReader(str(path)).snapshot(limit=50)
            elapsed = time.monotonic() - start
            assert traces == []
            assert elapsed < 3.0  # 0.5s timeout, not a hang
        finally:
            conn.rollback()
            conn.close()


class TestSourceReaderTail:
    def test_poll_returns_only_new_rows(self, tmp_path):
        path = tmp_path / "traces.db"
        sink = SqliteSink(str(path))
        sink.write(_trace("t1", "2026-08-08T10:00:00Z"))

        reader = SourceReader(str(path))
        assert len(reader.snapshot(limit=50)) == 1

        sink.write(_trace("t2", "2026-08-08T10:01:00Z"))
        sink.write(_trace("t3", "2026-08-08T10:02:00Z"))
        result = reader.poll(limit=50)
        sink.close()
        assert result["kind"] == "append"
        assert [t["trace_id"] for t in result["traces"]] == ["t2", "t3"]

    def test_replaced_trace_resurfaces_through_the_tail(self, tmp_path):
        # INSERT OR REPLACE gives the row a fresh id; the update must arrive
        # as an append so the client can replace the displayed row. This is
        # how in-flight stubs flip to final records on a SQLite source.
        path = tmp_path / "traces.db"
        sink = SqliteSink(str(path))
        sink.write(_trace("t1", "2026-08-08T10:00:00Z", status="running",
                          in_flight=True))

        reader = SourceReader(str(path))
        snap = reader.snapshot(limit=50)
        assert snap[0]["status"] == "running"

        sink.write(_trace("t1", "2026-08-08T10:00:00Z", status="completed"))
        result = reader.poll(limit=50)
        sink.close()
        assert result["kind"] == "append"
        assert len(result["traces"]) == 1
        assert result["traces"][0]["trace_id"] == "t1"
        assert result["traces"][0]["status"] == "completed"

    def test_recreated_database_forces_snapshot(self, tmp_path):
        path = tmp_path / "traces.db"
        _make_db(path, [_trace(f"t{i}", f"2026-08-08T10:00:{i:02d}Z")
                        for i in range(5)])
        reader = SourceReader(str(path))
        reader.snapshot(limit=50)

        path.unlink()
        for suffix in ("-wal", "-shm"):
            side = tmp_path / f"traces.db{suffix}"
            if side.exists():
                side.unlink()
        _make_db(path, [_trace("fresh", "2026-08-08T11:00:00Z")])

        result = reader.poll(limit=50)
        assert result["kind"] == "snapshot"
        assert [t["trace_id"] for t in result["traces"]] == ["fresh"]

    def test_quiet_database_polls_empty(self, tmp_path):
        db = _make_db(tmp_path / "traces.db",
                      [_trace("t1", "2026-08-08T10:00:00Z")])
        reader = SourceReader(db)
        reader.snapshot(limit=50)
        assert reader.poll(limit=50) == {"kind": "append", "traces": []}


class TestViewerIntegration:
    @pytest.fixture
    def running_server(self):
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

    def test_source_name_comes_from_project_field(self, tmp_path):
        db = _make_db(tmp_path / "traces.db",
                      [_trace("t1", "2026-08-08T10:00:00Z",
                              project="quantum-app")])
        state = ViewerState()
        assert state.add_source(db) == "quantum-app"

    def test_export_streams_ndjson(self, running_server, tmp_path):
        url, state = running_server
        db = _make_db(tmp_path / "traces.db", [
            _trace("t1", "2026-08-08T10:00:00Z"),
            _trace("t2", "2026-08-08T10:01:00Z"),
        ])
        name = state.add_source(db)
        with urllib.request.urlopen(
            f"{url}/api/export?source={name}", timeout=5
        ) as resp:
            assert resp.headers["Content-Type"] == "application/x-ndjson"
            body = resp.read()
        lines = [json.loads(l) for l in body.decode().splitlines()]
        assert [r["trace_id"] for r in lines] == ["t1", "t2"]


class TestDoctor:
    def test_valid_database_passes(self, tmp_path):
        db = _make_db(tmp_path / "traces.db",
                      [_trace("t1", "2026-08-08T10:00:00Z")])
        result = doctor.run_checks(db)
        source_check = [c for c in result["checks"]
                        if c["label"] == "source"][0]
        assert source_check["status"] == "pass"
        assert "SQLite" in source_check["message"]

    def test_database_without_table_fails_with_hint(self, tmp_path):
        path = tmp_path / "other.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        result = doctor.run_checks(str(path))
        source_check = [c for c in result["checks"]
                        if c["label"] == "source"][0]
        assert source_check["status"] == "fail"
        assert "traces" in source_check["hint"]

    def test_empty_database_is_info_not_failure(self, tmp_path):
        path = tmp_path / "traces.db"
        sink = SqliteSink(str(path))
        sink.write(_trace("t1", "2026-08-08T10:00:00Z"))
        sink.close()
        conn = sqlite3.connect(str(path))
        conn.execute("DELETE FROM traces")
        conn.commit()
        conn.close()
        result = doctor.run_checks(str(path))
        source_check = [c for c in result["checks"]
                        if c["label"] == "source"][0]
        assert source_check["status"] == "info"

    def test_scan_finds_planted_credential_with_row_id(self, tmp_path):
        fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
        db = _make_db(tmp_path / "traces.db", [
            _trace("clean", "2026-08-08T10:00:00Z"),
            _trace("dirty", "2026-08-08T10:01:00Z",
                   inputs={"location": fake_key}),
        ])
        result = doctor.scan_source(db)
        assert result["ok"] is False
        assert result["hits"][0]["pattern"] == "aws-key"
        assert result["hits"][0]["line"] == 2  # the table row id

    def test_scan_of_clean_database_passes(self, tmp_path):
        db = _make_db(tmp_path / "traces.db",
                      [_trace("t1", "2026-08-08T10:00:00Z")])
        result = doctor.scan_source(db)
        assert result["ok"] is True
        assert result["lines"] == 1


class TestTraceLog:
    def _db(self, tmp_path):
        return _make_db(tmp_path / "traces.db", [
            _trace("a", "2026-08-08T10:00:00Z", action="note.create"),
            _trace("b", "2026-08-08T10:01:00Z", action="note.create",
                   status="failed"),
            _trace("c", "2026-08-08T10:02:00Z", action="job.run"),
        ])

    def test_all_returns_every_record(self, tmp_path):
        assert len(TraceLog(self._db(tmp_path)).all()) == 3

    def test_filters_apply_identically(self, tmp_path):
        db = self._db(tmp_path)
        assert TraceLog(db).filter(status="failed").count() == 1
        assert TraceLog(db).filter(action__contains="note").count() == 2
        assert TraceLog(db).filter(action__re=r"^job\.").count() == 1

    def test_last_and_first_order_by_started_at(self, tmp_path):
        db = self._db(tmp_path)
        assert [t["trace_id"] for t in TraceLog(db).last(2)] == ["c", "b"]
        assert [t["trace_id"] for t in TraceLog(db).first(2)] == ["a", "b"]

    def test_query_reports_limit_reached(self, tmp_path):
        db = self._db(tmp_path)
        result = TraceLog(db).query(2)
        assert len(result["traces"]) == 2
        assert result["limit_reached"] is True
        assert result["scan_capped"] is False

    def test_max_lines_scanned_caps_rows(self, tmp_path):
        db = self._db(tmp_path)
        result = TraceLog(db, max_lines_scanned=1).query(10)
        assert result["scan_capped"] is True

    def test_orphaned_stub_surfaces_as_running(self, tmp_path):
        # A crashed streaming trace: the stub row was never replaced by a
        # final record. INSERT OR REPLACE means it is the row — no dedupe
        # bookkeeping — and it must be visible as evidence.
        db = _make_db(tmp_path / "traces.db", [
            _trace("done", "2026-08-08T10:00:00Z"),
            _trace("crashed", "2026-08-08T10:01:00Z", status="running",
                   in_flight=True, progress_seq=3),
        ])
        records = TraceLog(db).all()
        by_id = {r["trace_id"]: r for r in records}
        assert len(records) == 2
        assert by_id["crashed"]["status"] == "running"

    def test_missing_database_reads_empty(self, tmp_path):
        assert TraceLog(str(tmp_path / "nope.db")).all() == []
