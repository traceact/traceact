# test_sinks.py — JsonlSink writes and size-based rotation.

import os

from traceact import JsonlSink

from conftest import read_traces


def test_jsonl_sink_appends_one_line_per_record(tmp_path):
    path = tmp_path / "traces.jsonl"
    sink = JsonlSink(str(path))

    sink.write({"trace_id": "trc_1", "action": "a", "started_at": "t1"})
    sink.write({"trace_id": "trc_2", "action": "a", "started_at": "t2"})

    records = read_traces(path)
    assert [r["trace_id"] for r in records] == ["trc_1", "trc_2"]


def test_jsonl_sink_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "traces.jsonl"
    JsonlSink(str(path))  # should not raise

    assert path.parent.is_dir()


def test_max_bytes_rotates_active_file_by_renaming(tmp_path):
    path = tmp_path / "traces.jsonl"
    sink = JsonlSink(str(path), max_bytes=500)

    for i in range(20):
        sink.write({
            "trace_id": f"trc_{i}", "action": "a", "started_at": f"t{i}",
            "padding": "x" * 20,
        })

    siblings = sorted(os.listdir(tmp_path))
    rotated = [f for f in siblings if f != "traces.jsonl"]

    # More than one file exists — rotation happened at least once.
    assert len(rotated) > 0
    # The active file never exceeds the cap.
    assert os.path.getsize(path) <= 500
    # Every rotated segment is itself under the cap too (each is a snapshot
    # of what was the active file at the moment it got rotated away).
    for name in rotated:
        assert os.path.getsize(tmp_path / name) <= 500

    # No record was lost: total lines across all files == number written.
    total_lines = 0
    for name in siblings:
        with open(tmp_path / name, encoding="utf-8") as f:
            total_lines += sum(1 for line in f if line.strip())
    assert total_lines == 20


def test_no_max_bytes_never_rotates(tmp_path):
    path = tmp_path / "traces.jsonl"
    sink = JsonlSink(str(path))  # max_bytes=None (default)

    for i in range(50):
        sink.write({
            "trace_id": f"trc_{i}", "action": "a", "started_at": f"t{i}",
            "padding": "x" * 50,
        })

    assert sorted(os.listdir(tmp_path)) == ["traces.jsonl"]
    assert len(read_traces(path)) == 50
