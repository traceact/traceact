# test_sinks.py — JsonlSink writes, size-based rotation, and the shared
# buffered-mode record buffer.

import json
import os
import threading

from traceact import JsonlSink, TraceLog

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


def test_rotated_segments_keep_jsonl_extension(tmp_path):
    # The rotation timestamp goes before the extension, not after it. A name
    # ending in anything other than .jsonl would be invisible to the *.jsonl
    # folder-source pattern the viewer and TraceLog read.
    path = tmp_path / "traces.jsonl"
    sink = JsonlSink(str(path), max_bytes=300)

    for i in range(10):
        sink.write({
            "trace_id": f"trc_{i}", "action": "a", "started_at": f"t{i}",
            "padding": "x" * 20,
        })

    rotated = [f for f in os.listdir(tmp_path) if f != "traces.jsonl"]
    assert len(rotated) > 0
    for name in rotated:
        assert name.endswith(".jsonl")
        assert name.startswith("traces.")


def test_folder_source_reads_active_plus_rotated_segments(tmp_path):
    # End to end: every record written through a rotating sink is readable
    # back through a folder source, active file and rotated segments together.
    path = tmp_path / "traces.jsonl"
    sink = JsonlSink(str(path), max_bytes=300)

    for i in range(15):
        sink.write({
            "trace_id": f"trc_{i}", "action": "a",
            "started_at": f"2026-07-26T00:{i:02d}:00Z",
            "padding": "x" * 20,
        })

    assert len(os.listdir(tmp_path)) > 1  # rotation happened
    assert TraceLog(str(tmp_path)).count() == 15


def test_folder_source_reads_legacy_rotated_names(tmp_path):
    # Segments rotated by versions that appended the timestamp AFTER the
    # extension (traces.jsonl.<ts>) must stay reachable too.
    legacy = tmp_path / "traces.jsonl.20260726T120000000000Z"
    with open(legacy, "w", encoding="utf-8") as f:
        f.write(json.dumps({"trace_id": "trc_old", "action": "a",
                            "started_at": "2026-07-26T00:00:00Z"}) + "\n")
    active = tmp_path / "traces.jsonl"
    with open(active, "w", encoding="utf-8") as f:
        f.write(json.dumps({"trace_id": "trc_new", "action": "a",
                            "started_at": "2026-07-26T01:00:00Z"}) + "\n")

    log = TraceLog(str(tmp_path))
    assert log.count() == 2

    from traceact.viewer.reader import SourceReader
    assert len(SourceReader(str(tmp_path)).snapshot(10)) == 2


def test_buffer_records_appended_during_flush_are_not_lost():
    # flush_buffer snapshots and clears atomically; concurrent buffer_record
    # calls during repeated flushes must never lose a record between the
    # iterate and the clear.
    from traceact.sinks import buffer_record, flush_buffer, _buffer

    written = []

    class Capture:
        def write(self, record):
            written.append(record)

    total = 2000
    stop = threading.Event()

    def producer():
        for i in range(total):
            buffer_record({"n": i})

    def flusher():
        while not stop.is_set():
            flush_buffer([Capture()])

    p = threading.Thread(target=producer)
    f = threading.Thread(target=flusher)
    f.start()
    p.start()
    p.join()
    stop.set()
    f.join()
    flush_buffer([Capture()])  # drain whatever the last cycle left behind

    leftover = len(_buffer)
    assert len(written) + leftover == total
    assert leftover == 0
