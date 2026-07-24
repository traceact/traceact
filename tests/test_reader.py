# test_reader.py — SourceReader snapshot/poll, including the delete+recreate
# (inode replacement) fix for the viewer's SSE tail.
#
# Background: poll() used to track only a byte offset per file. If a file was
# deleted and rewritten with more bytes than the old offset, poll() would seek
# into the new file at the stale offset and silently skip everything before
# it — an already-open viewer tab would stop seeing new traces, while a fresh
# tab (which always calls snapshot() from byte 0) worked fine. The fix tracks
# each file's inode too, so a changed inode at the same path triggers a full
# snapshot rebuild instead of a broken incremental read.

import json
import os

from traceact.viewer.reader import SourceReader


def _trace_line(i: int) -> str:
    return json.dumps({
        "trace_id": f"trc_{i}", "action": "a",
        "started_at": f"2026-01-01T00:00:{i:02d}Z",
    })


def test_snapshot_reads_existing_traces_newest_first(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(_trace_line(1) + "\n" + _trace_line(2) + "\n")

    reader = SourceReader(str(path))
    traces = reader.snapshot(limit=100)

    assert [t["trace_id"] for t in traces] == ["trc_2", "trc_1"]


def test_poll_with_no_new_data_returns_empty_append(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(_trace_line(1) + "\n")

    reader = SourceReader(str(path))
    reader.snapshot(limit=100)

    result = reader.poll(limit=100)
    assert result == {"kind": "append", "traces": []}


def test_poll_with_appended_data_returns_only_the_new_lines(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(_trace_line(1) + "\n")

    reader = SourceReader(str(path))
    reader.snapshot(limit=100)

    with open(path, "a", encoding="utf-8") as f:
        f.write(_trace_line(2) + "\n")

    result = reader.poll(limit=100)
    assert result["kind"] == "append"
    assert [t["trace_id"] for t in result["traces"]] == ["trc_2"]


def test_poll_detects_delete_and_recreate_and_rebuilds_full_snapshot(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(_trace_line(1) + "\n" + _trace_line(2) + "\n")

    reader = SourceReader(str(path))
    reader.snapshot(limit=100)

    # Delete and recreate with more content than the old file had. Without
    # inode tracking, poll() would seek to the old (now stale) byte offset in
    # this brand-new file and silently drop everything before that point.
    os.remove(path)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(3, 20):
            f.write(_trace_line(i) + "\n")

    result = reader.poll(limit=100)

    assert result["kind"] == "snapshot"
    assert len(result["traces"]) == 17
    ids = {t["trace_id"] for t in result["traces"]}
    assert ids == {f"trc_{i}" for i in range(3, 20)}


def test_poll_handles_truncation_without_inode_change(tmp_path):
    # A file shrinking in place (not deleted/recreated, e.g. an app truncating
    # and rewriting the same inode) is a distinct case from replacement and
    # was already handled before this fix — confirm it still works.
    path = tmp_path / "traces.jsonl"
    path.write_text(_trace_line(1) + "\n" + _trace_line(2) + "\n" + _trace_line(3) + "\n")

    reader = SourceReader(str(path))
    reader.snapshot(limit=100)

    # Truncate in place (same inode, smaller size) and write one new line.
    with open(path, "w", encoding="utf-8") as f:
        f.write(_trace_line(9) + "\n")

    result = reader.poll(limit=100)
    assert [t["trace_id"] for t in result["traces"]] == ["trc_9"]


def test_folder_source_merges_multiple_files(tmp_path):
    (tmp_path / "a.jsonl").write_text(_trace_line(1) + "\n")
    (tmp_path / "b.jsonl").write_text(_trace_line(2) + "\n")

    reader = SourceReader(str(tmp_path))
    traces = reader.snapshot(limit=100)

    assert {t["trace_id"] for t in traces} == {"trc_1", "trc_2"}
