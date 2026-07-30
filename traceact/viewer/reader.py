# viewer/reader.py
#
# Reads TraceAct JSONL sources for the viewer.
#
# A "source" is either:
#   - a single .jsonl file, or
#   - a folder containing one or more .jsonl files (each written by an app,
#     a worker, or a per-process shard such as traces.<pid>.jsonl).
#
# The reader does two jobs:
#   1. snapshot()  — read the most recent N valid traces for the initial load.
#   2. poll()      — read only newly-appended traces since the last read, so the
#                    viewer can stream live updates without re-reading the file.
#
# Tolerant by design:
# The viewer renders any line that parses as JSON and looks like a trace (has a
# trace id, an action, and a start timestamp). Malformed or partial lines are
# skipped, not fatal. This means apps can name their files however they like
# (traces.jsonl, <pid>.traced.jsonl, ...) as long as the extension is .jsonl and
# the shape is right.
#
# Efficiency note:
# Live tailing tracks a byte offset per file and reads only the bytes appended
# since last time, so the cost scales with new data, not total file size. A
# 2 GB file tails as cheaply as a 2 KB one. The initial snapshot does read the
# whole file once, keeping only the last N parsed records in a ring buffer; for
# very large files a future version may seek from the end instead.

import glob
import json
import os
from collections import deque
from typing import Any, Deque, Dict, List, Optional


# The keys a JSON object must have to be treated as a trace. Kept deliberately
# small so the viewer is tolerant of schema growth: anything with an id, an
# action, and a start time is renderable.
_REQUIRED_KEYS = ("trace_id", "action", "started_at")


def is_valid_trace(obj: Any) -> bool:
    """
    Return True if a parsed JSON object looks like a trace record.

    We require only a minimal core so that the viewer keeps working as the
    trace schema grows. Objects missing any required key are skipped.
    """
    if not isinstance(obj, dict):
        return False
    return all(key in obj for key in _REQUIRED_KEYS)


def _jsonl_files(path: str) -> List[str]:
    """
    Resolve a source path to the list of .jsonl files it refers to.

    - A file path returns just that file.
    - A directory returns every *.jsonl inside it, plus *.jsonl.* — segments
      rotated by JsonlSink versions that appended the rotation timestamp
      after the extension (current versions keep the extension last, so the
      first pattern already matches them). Sorted, deduplicated.
    - A missing path returns an empty list (the source simply has no data yet).
    """
    if os.path.isdir(path):
        matches = set(glob.glob(os.path.join(path, "*.jsonl")))
        matches.update(glob.glob(os.path.join(path, "*.jsonl.*")))
        return sorted(matches)
    if os.path.isfile(path):
        return [path]
    return []


class SourceReader:
    """
    Reads and tails one source (a file or a folder of .jsonl files).

    The reader remembers a byte offset for every file it has seen. snapshot()
    reads everything currently present and moves each offset to end-of-file;
    poll() then returns only what has been appended since.

    A single SourceReader instance is tied to one source and is used by the
    server for both the initial load and the live stream of that source.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        # Per-file read position: {absolute_file_path: byte_offset_already_read}.
        # New files that appear later (e.g. a new shard) start at offset 0 and
        # so are read from their beginning on the next poll.
        self._offsets: Dict[str, int] = {}
        # Per-file inode number. When a file is deleted and recreated at the same
        # path the inode changes even if the new file is larger than the old one,
        # so a size-only check would seek into the wrong position. Tracking the
        # inode lets us detect replacement (not just truncation) reliably.
        # st_ino is 0 on Windows (inodes unavailable); the check is a safe no-op
        # there and falls back to the existing truncation-only detection.
        self._inodes: Dict[str, int] = {}

    # -- initial load ------------------------------------------------------

    def snapshot(self, limit: int) -> List[Dict[str, Any]]:
        """
        Read the most recent `limit` valid traces across all files in the source.

        Every file is read from the beginning, valid traces are collected into a
        ring buffer of size `limit` (so memory stays bounded even for large
        files), and each file's offset is advanced to end-of-file so that a
        subsequent poll() returns only newer appends.

        Returns traces ordered newest-first (by started_at), matching the log's
        top-is-newest layout.
        """
        ring: Deque[Dict[str, Any]] = deque(maxlen=limit)

        for filepath in _jsonl_files(self.path):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        obj = _parse_line(line)
                        if obj is not None:
                            ring.append(obj)
                    # Record where we stopped so poll() reads only new bytes.
                    self._offsets[filepath] = _file_size(filepath)
                    # Record which physical file we just read, so poll() can
                    # tell a delete+recreate apart from a plain append.
                    self._inodes[filepath] = _file_inode(filepath)
            except OSError:
                # A file that vanished or can't be opened is simply skipped.
                continue

        # In-flight streaming writes "running" stub lines that the final
        # record supersedes. JSONL append order makes last-wins per trace_id
        # correct: within a file the final always follows its stubs.
        traces = _dedupe_in_flight(list(ring))
        traces.sort(key=_started_at_key, reverse=True)
        return traces

    # -- live tail ---------------------------------------------------------

    def poll(self, limit: int) -> Dict[str, Any]:
        """
        Return traces that arrived since the last snapshot() or poll().

        Handles four cases per file:
          - new bytes appended   → read and parse them from the stored offset.
          - a brand-new file     → offset defaults to 0, so it's read in full.
          - a truncated file     → size < stored offset; reset to 0 and re-read.
          - a deleted+recreated file at the same path → its inode changes even
            though the new file may be larger than the old offset, so a
            size-only check can't detect it: it would silently seek into the
            middle of unrelated new content and drop everything before that
            byte position. Detected here by comparing `st_ino` across calls.

        On a plain append, returns {"kind": "append", "traces": [...]}
        (newest-last, arrival order — the caller prepends these to the top of
        the log). If any file was replaced, the reader instead rebuilds a full
        snapshot from scratch (so no earlier trace is lost) and returns
        {"kind": "snapshot", "traces": [...]} (newest-first) — the caller
        should replace its trace list rather than prepend onto it.
        """
        fresh: List[Dict[str, Any]] = []
        replaced = False

        for filepath in _jsonl_files(self.path):
            inode = _file_inode(filepath)
            prior_inode = self._inodes.get(filepath)
            if prior_inode is not None and inode is not None and inode != prior_inode:
                replaced = True
            self._inodes[filepath] = inode

            size = _file_size(filepath)
            offset = self._offsets.get(filepath, 0)

            # File shrank (rotated or truncated): start over from the top.
            if size < offset:
                offset = 0

            if size == offset:
                # Nothing new in this file.
                self._offsets[filepath] = size
                continue

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    f.seek(offset)
                    for line in f:
                        obj = _parse_line(line)
                        if obj is not None:
                            fresh.append(obj)
                    self._offsets[filepath] = f.tell()
            except OSError:
                continue

        if replaced:
            # Discard whatever `fresh` picked up from the wrong byte offset
            # and rebuild cleanly. snapshot() re-reads every file from the
            # start and refreshes both _offsets and _inodes.
            return {"kind": "snapshot", "traces": self.snapshot(limit)}

        # One poll window can contain several snapshots of the same open
        # trace (or a stub plus its final record); only the latest is sent.
        # Cross-poll supersession is the client's job — it replaces a row it
        # has already rendered when a newer record for that trace_id arrives.
        return {"kind": "append", "traces": _dedupe_in_flight(fresh)}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _dedupe_in_flight(traces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Collapse in-flight snapshots to one record per trace_id, keeping the
    latest occurrence (which, given JSONL append order, is either the newest
    stub or the final record that supersedes them all). Records without a
    trace_id — nothing TraceAct writes, but tolerated input — pass through
    untouched. Order of the surviving records is preserved.
    """
    # Fast path: no in-flight lines, nothing to collapse. This keeps the
    # overwhelmingly common non-streaming source at zero extra allocations.
    if not any(t.get("in_flight") for t in traces):
        return traces

    latest_index: Dict[str, int] = {}
    for i, trace in enumerate(traces):
        tid = trace.get("trace_id")
        if tid is not None:
            latest_index[tid] = i
    return [
        t for i, t in enumerate(traces)
        if t.get("trace_id") is None or latest_index[t["trace_id"]] == i
    ]


def _parse_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse one line into a trace dict, or return None if it isn't a valid trace.

    Blank lines, partial lines (an append caught mid-write), and non-trace JSON
    all return None and are silently skipped.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if is_valid_trace(obj) else None


def _file_size(filepath: str) -> int:
    """Current size of a file in bytes, or 0 if it can't be stat'd."""
    try:
        return os.path.getsize(filepath)
    except OSError:
        return 0


def _file_inode(filepath: str) -> Optional[int]:
    """
    Inode number for a file, or None if it can't be determined.

    st_ino is 0 on some Windows filesystems where Python can't populate it;
    treated as unknown there so replacement detection simply doesn't trigger
    (falling back to the existing truncation-only check) rather than firing
    false positives on every poll.
    """
    try:
        ino = os.stat(filepath).st_ino
    except OSError:
        return None
    return ino or None


def _started_at_key(trace: Dict[str, Any]) -> str:
    """
    Sort key for ordering traces by start time. started_at is an ISO 8601
    string, which sorts correctly lexicographically. Missing values sort first.
    """
    return trace.get("started_at") or ""
