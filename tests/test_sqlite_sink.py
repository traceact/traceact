# tests/test_sqlite_sink.py
#
# Tests for SqliteSink.

import json
import sqlite3
import threading
import pytest

from traceact import SqliteSink


def _trace(action="note.create", kind="app", status="completed", **extra):
    t = {
        "trace_id": f"trc_{action.replace('.', '_')}",
        "action": action,
        "started_at": "2026-07-25T10:00:00Z",
        "kind": kind,
        "status": status,
        "duration_ms": 12.4,
    }
    t.update(extra)
    return t


class TestWriteAndRead:
    def test_writes_row_to_database(self, tmp_path):
        db = tmp_path / "traces.db"
        sink = SqliteSink(str(db))
        sink.write(_trace("note.create"))
        sink.close()

        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT trace_id, action, status FROM traces").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0] == ("trc_note_create", "note.create", "completed")

    def test_record_column_contains_full_json(self, tmp_path):
        db = tmp_path / "traces.db"
        sink = SqliteSink(str(db))
        rec = _trace("note.create", correlation_id="corr_abc")
        sink.write(rec)
        sink.close()

        conn = sqlite3.connect(str(db))
        raw = conn.execute("SELECT record FROM traces").fetchone()[0]
        conn.close()
        parsed = json.loads(raw)
        assert parsed["correlation_id"] == "corr_abc"

    def test_multiple_writes(self, tmp_path):
        db = tmp_path / "traces.db"
        sink = SqliteSink(str(db))
        sink.write(_trace("a.create", status="completed"))
        sink.write(_trace("b.update", status="failed"))
        sink.write(_trace("c.delete", status="completed"))
        sink.close()

        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        conn.close()
        assert count == 3

    def test_upsert_on_duplicate_trace_id(self, tmp_path):
        db = tmp_path / "traces.db"
        sink = SqliteSink(str(db))
        rec = _trace("note.create", status="running")
        sink.write(rec)
        rec2 = dict(rec, status="completed")
        sink.write(rec2)
        sink.close()

        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT status FROM traces").fetchall()
        conn.close()
        # INSERT OR REPLACE: only one row, latest status wins.
        assert len(rows) == 1
        assert rows[0][0] == "completed"

    def test_budget_hit_stored_as_integer(self, tmp_path):
        db = tmp_path / "traces.db"
        sink = SqliteSink(str(db))
        sink.write(_trace("a", budget_hit=True))
        sink.close()

        conn = sqlite3.connect(str(db))
        val = conn.execute("SELECT budget_hit FROM traces").fetchone()[0]
        conn.close()
        assert val == 1

    def test_in_memory_database(self):
        sink = SqliteSink(":memory:")
        sink.write(_trace("note.create"))
        # Query through the sink's own connection while it's still open.
        count = sink._conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        sink.close()
        assert count == 1

    def test_creates_parent_directories(self, tmp_path):
        db = tmp_path / "deeply" / "nested" / "dir" / "traces.db"
        sink = SqliteSink(str(db))
        sink.write(_trace("a"))
        sink.close()
        assert db.exists()

    def test_schema_survives_reopen(self, tmp_path):
        """Closing and reopening the database doesn't lose the schema or rows."""
        db = tmp_path / "traces.db"
        sink = SqliteSink(str(db))
        sink.write(_trace("first"))
        sink.close()

        sink2 = SqliteSink(str(db))
        sink2.write(_trace("second"))
        sink2.close()

        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        conn.close()
        assert count == 2


class TestThreadSafety:
    def test_concurrent_writes_all_arrive(self, tmp_path):
        db = tmp_path / "traces.db"
        sink = SqliteSink(str(db))
        errors = []

        def write_batch(n):
            try:
                for i in range(10):
                    sink.write(_trace(f"action_{n}_{i}",
                                      **{"trace_id": f"trc_{n}_{i}"}))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_batch, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        sink.close()
        assert errors == []

        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        conn.close()
        assert count == 50


class TestFaultTolerance:
    def test_write_error_does_not_raise(self, tmp_path, capsys):
        """An error during _get_connection() must print to stderr, not propagate."""
        import unittest.mock as mock
        db = tmp_path / "traces.db"
        sink = SqliteSink(str(db))

        # Patch _get_connection to simulate a DB error (e.g. disk full).
        with mock.patch.object(sink, "_get_connection",
                               side_effect=Exception("simulated db error")):
            sink.write(_trace("first"))

        captured = capsys.readouterr()
        assert "SqliteSink write error" in captured.err


class TestPublicExport:
    def test_importable_from_top_level(self):
        from traceact import SqliteSink as SS
        assert SS is SqliteSink
