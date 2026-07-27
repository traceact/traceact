# log.py
#
# TraceLog — programmatic query interface for TraceAct JSONL files.
#
# Solves the "code needs to read traces" problem. The viewer solves the
# human-eyes problem; TraceLog solves the AI-agent / script / test problem.
# An agent, test suite, or background script that wants to inspect what happened
# during a run calls TraceLog rather than parsing JSONL itself.
#
# Usage:
#
#   from traceact import TraceLog
#
#   log = TraceLog("data/traces/traces.jsonl")    # file or folder
#
#   # Exact-match filter
#   db = log.filter(kind="db")
#   failures = log.filter(status="failed")
#
#   # Double-underscore lookup operators (like Django ORM)
#   note_traces = log.filter(action__contains="note")
#
#   # Multiple filters in one call (AND logic)
#   recent_failures = log.filter(kind="db", status="failed").last(10)
#
#   # Terminal methods
#   log.filter(status="failed").all()          # List[dict], oldest first
#   log.filter(status="failed").last(10)       # 10 most recent
#   log.filter(status="failed").first(10)      # 10 oldest
#   log.filter(status="failed").count()        # int
#   log.filter(status="failed").render_table() # print to stdout
#
# filter() returns a new TraceLog with the added predicate, leaving the
# original unchanged. This lets you branch from a base query without
# interference:
#
#   log = TraceLog("traces.jsonl")
#   db_traces  = log.filter(kind="db").all()
#   app_traces = log.filter(kind="app").all()   # independent; no db filter
#
# No dependencies beyond the standard library.

import glob
import json
import os
import re as _re
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class TraceLog:
    """
    Programmatic query interface over one TraceAct JSONL source.

    A "source" is the same as a viewer source: a .jsonl file, or a folder
    containing one or more .jsonl files (e.g. per-process shards). All files
    in a folder are read and merged on every terminal call.

    Instances are immutable from the caller's perspective: filter() always
    returns a new TraceLog with the added predicate; the original is unchanged.
    """

    def __init__(self, path: str, max_lines_scanned: Optional[int] = None) -> None:
        # Absolute path to the source file or folder. Relative paths work but
        # are resolved at read time, not at construction, so they're sensitive
        # to cwd changes between construction and the first terminal call. Use
        # absolute paths when in doubt.
        self._path = path
        # Ordered list of predicate functions. Every predicate must return True
        # for a trace to be included in results (AND semantics).
        self._predicates: List[Callable[[Dict[str, Any]], bool]] = []
        # Raw filter specs stored alongside the compiled predicates so view()
        # can serialise them as URL params for the human viewer.
        # Each entry is (field, op, value) — e.g. ("status", "eq", "failed").
        self._specs: List[Tuple[str, str, Any]] = []
        # Optional ceiling on how many lines .last()/.first()/.query() will
        # read before giving up and returning whatever was found so far. None
        # (the default) means no cap — every existing caller that doesn't pass
        # this is completely unaffected. Set by callers reading untrusted or
        # unbounded-size sources (the viewer's HTTP query endpoint sets one) so
        # a single request has a predictable worst-case cost.
        self._max_lines_scanned = max_lines_scanned

    # -- filtering API -------------------------------------------------------

    def filter(self, **kwargs: Any) -> "TraceLog":
        """
        Return a new TraceLog with additional filter predicates.

        Keyword arguments accept two forms:

          field=value             Exact equality: trace["field"] == value.
                                  Works for str, int, bool, and None.

          field__contains=value   Case-insensitive substring: value appears
                                  somewhere in str(trace["field"]).

          field__startswith=val   Case-insensitive prefix match.

          field__endswith=val     Case-insensitive suffix match.

          field__re=pattern       regex search against str(trace["field"]).
                                  Uses re.search (partial match).

        Multiple keyword arguments in one call are ANDed together. Chaining
        .filter().filter() also ANDs across calls.

        A filter on a field that doesn't exist in a trace evaluates to False
        for that trace (i.e. the trace is excluded).
        """
        clone = self._copy()
        for key, value in kwargs.items():
            field, op = _parse_filter_key(key)
            clone._predicates.append(_build_predicate(key, value))
            clone._specs.append((field, op, value))
        return clone

    # -- terminal methods ----------------------------------------------------

    def all(self) -> List[Dict[str, Any]]:
        """
        Return all matching traces, sorted oldest-first by started_at.

        Reads every .jsonl file in the source on each call. For continuous
        polling from a live source, consider calling this inside a loop rather
        than holding a TraceLog open (there is no caching — each call re-reads
        the file).
        """
        traces = self._read_matching()
        traces.sort(key=_started_at_key)
        return traces

    def last(self, n: int = 10) -> List[Dict[str, Any]]:
        """
        Return the n most recent matching traces (by started_at), newest first.

        If fewer than n traces match, all matching traces are returned.

        Bounded: holds at most n matches per file in memory at once (see
        _read_bounded), not the full matching set. This matters for a broad
        filter (or no filter at all) over a large source — memory stays
        proportional to n, not to how much of the source actually matches.
        """
        traces, _capped, _limit_reached = self._read_bounded(n, newest=True)
        return traces

    def first(self, n: int = 10) -> List[Dict[str, Any]]:
        """
        Return the n oldest matching traces (by started_at), oldest first.

        If fewer than n traces match, all matching traces are returned.

        Bounded the same way as last() — see _read_bounded. Oldest-n additionally
        stops reading each file as soon as n matches are found in it, since a
        JSONL file's lines already appear in the order they were written.
        """
        traces, _capped, _limit_reached = self._read_bounded(n, newest=False)
        return traces

    def query(self, n: int = 500) -> Dict[str, Any]:
        """
        Bounded query returning the n most recent matching traces plus two
        separate reasons the result might not be every match that exists:

        - scan_capped: the scan gave up early (max_lines_scanned was hit)
          before it finished reading the source.
        - limit_reached: the scan finished (or at least got far enough to find
          n matches), but n matches is exactly what was asked for — there may
          be more beyond it that were never counted because counting stopped
          at n. This is true of any bounded query, not specific to an HTTP
          caller's limit= parameter or to max_lines_scanned; last(n) has the
          exact same property, it just has nowhere to report it.

        Use this instead of last() when the caller needs to distinguish "this
        is everything that matches" from "this is only what fit" — for example
        an HTTP endpoint telling the browser "results may be incomplete" rather
        than silently presenting a truncated result as exhaustive. last()/
        first() intentionally keep returning a plain list; this is a separate
        method rather than a change to their return type, so existing callers
        are unaffected.

        Returns:
            {"traces": [...], "scan_capped": bool, "limit_reached": bool} —
            traces are newest-first.
        """
        traces, capped, limit_reached = self._read_bounded(n, newest=True)
        return {
            "traces": traces,
            "scan_capped": capped,
            "limit_reached": limit_reached,
        }

    def count(self) -> int:
        """Return the total number of matching traces."""
        return len(self._read_matching())

    def render_table(self, n: Optional[int] = None) -> None:
        """
        Print matching traces as a plain-text table to stdout.

        Columns shown: started_at, status, kind, action, duration_ms, trace_id.
        Rows are sorted newest-first. Pass n to cap the number of rows shown.

        Designed for quick terminal inspection — not for programmatic use.
        Use .all() or .last() when you need the data as Python objects.
        """
        traces = self._read_matching()
        traces.sort(key=_started_at_key, reverse=True)
        if n is not None:
            traces = traces[:n]

        if not traces:
            print("(no traces matched)")
            return

        # Column definitions: (field_name, header_label, max_width).
        # Fields wider than max_width are truncated with "…".
        COLS = [
            ("started_at",   "STARTED AT",   23),
            ("status",       "STATUS",        9),
            ("kind",         "KIND",          8),
            ("action",       "ACTION",        34),
            ("duration_ms",  "DURATION",     10),
            ("trace_id",     "TRACE ID",     16),
        ]

        def _cell(trace: Dict[str, Any], field: str, width: int) -> str:
            val = trace.get(field)
            if val is None:
                return "".ljust(width)
            if field == "duration_ms" and isinstance(val, (int, float)):
                raw = f"{val:.1f} ms"
            else:
                raw = str(val)
            # Truncate long values with an ellipsis so columns stay fixed-width.
            if len(raw) > width:
                raw = raw[: width - 1] + "…"
            return raw.ljust(width)

        sep = "  "
        header = sep.join(label.ljust(w) for _, label, w in COLS)
        divider = sep.join("-" * w for _, _, w in COLS)

        print(header)
        print(divider)
        for t in traces:
            print(sep.join(_cell(t, f, w) for f, _, w in COLS))

        shown = len(traces)
        noun = "trace" if shown == 1 else "traces"
        print(f"\n{shown} {noun} shown.")

    # -- internal helpers ----------------------------------------------------

    def view(self, open_browser: bool = True) -> str:
        """
        Open the viewer in a browser, pre-filtered to match this TraceLog's filters.

        Launches a viewer (or reuses a running one) pointed at this source, then
        opens the browser at a URL that encodes the current filter specs as URL
        params. The viewer renders the pre-filters as dismissable badges above the
        trace list — the human can remove any badge to widen the view, and the
        search box still works on top of the pre-filters as normal.

        Returns the viewer URL (with filter params) so callers can log or display
        it rather than opening a browser automatically (pass open_browser=False).

        Requires the traceact viewer package (installed by default). Raises
        RuntimeError if the viewer sub-package is unavailable.
        """
        try:
            from traceact.viewer.instance import launch_or_connect
        except ImportError:
            raise RuntimeError(
                "TraceLog.view() requires the traceact viewer package. "
                "Install it with: pip install traceact"
            )
        import webbrowser
        from urllib.parse import urlencode

        # Ensure a viewer is running and get its base URL.
        base_url = launch_or_connect(source=self._path)

        # Build ?pf_* URL params from stored filter specs.
        # Exact-match: pf_status=failed
        # Operator form: pf_action__contains=note
        params: List[Tuple[str, str]] = []
        for field, op, value in self._specs:
            key = f"pf_{field}" if op == "eq" else f"pf_{field}__{op}"
            params.append((key, str(value)))

        base = base_url if base_url.endswith("/") else base_url + "/"
        url = base + ("?" + urlencode(params) if params else "")

        if open_browser:
            import threading
            # Small delay lets the server finish starting before the browser hits it.
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()

        return url

    def _copy(self) -> "TraceLog":
        """
        Return a shallow copy with the same path and a copy of the predicate list.

        New predicates added to the copy don't affect the original, and vice versa.
        """
        c = TraceLog(self._path, max_lines_scanned=self._max_lines_scanned)
        c._predicates = list(self._predicates)
        c._specs = list(self._specs)
        return c

    def _read_bounded(
        self, n: int, newest: bool
    ) -> Tuple[List[Dict[str, Any]], bool, bool]:
        """
        Return up to n matching traces without holding more than n matches per
        file in memory at once — the bounded counterpart to _read_matching(),
        which collects every match before the caller truncates to n.

        Correctness: if a trace X is among the true top-n most recent matches
        across the whole source, X must also be among its own file's top-n most
        recent matches. If it weren't, n other traces in that same file alone
        would already be newer than X, which on its own would place X outside
        the global top-n. So collecting each file's own top-n candidates and
        re-selecting the global top-n from that combined (much smaller) set is
        exact, not an approximation. The symmetric argument holds for oldest-n.

        Newest-n must scan every line of every file — the most recent match
        could be the last line — so each file's candidates are held in a
        maxlen deque that evicts its oldest entry as new matches arrive.

        Oldest-n does not need to scan a whole file: since a JSONL file's lines
        already appear in write order, the first n matches encountered in a
        file are already that file's oldest n, so reading stops there.

        max_lines_scanned, if set, caps the total lines read (matched or not)
        across the whole call. Hitting it stops the scan early and returns
        whatever was collected so far rather than reading the rest of the
        source — this bounds worst-case time (and, for an HTTP-exposed query,
        worst-case exposure to a slow request) the same way n bounds memory.

        Detecting "there were more than n matches" (limit_reached) needs its
        own counter, not an inference from the final result's length: a
        maxlen-n deque always ends up at length <= n whether it received
        exactly n items or a hundred thousand, so len(result) alone can't
        distinguish "exactly n matches, nothing more" from "far more than n,
        most were evicted." total_matches counts every match found — cheap (an
        int increment), unlike holding every match would be — regardless of
        whether that match survives eviction into the final bounded result.

        Returns (traces, scan_capped, limit_reached):
          - traces: sorted, truncated to n.
          - scan_capped: True only if max_lines_scanned was reached before
            every file was fully read.
          - limit_reached: True if more than n matches were found (newest-n
            only — oldest-n's early exit per file makes an accurate total
            uneconomical to track, and no current caller needs it there).
        """
        if n <= 0:
            return [], False, False

        candidates: List[Dict[str, Any]] = []
        lines_scanned = 0
        total_matches = 0
        capped = False

        for filepath in _jsonl_files(self._path):
            newest_buf: Deque[Dict[str, Any]] = deque(maxlen=n)
            oldest_matches: List[Dict[str, Any]] = []

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        lines_scanned += 1
                        if (
                            self._max_lines_scanned is not None
                            and lines_scanned > self._max_lines_scanned
                        ):
                            capped = True
                            break

                        obj = _parse_line(line)
                        if obj is None or not _passes(obj, self._predicates):
                            continue

                        total_matches += 1
                        if newest:
                            newest_buf.append(obj)
                        else:
                            oldest_matches.append(obj)
                            if len(oldest_matches) >= n:
                                break
            except OSError:
                # A file that vanished or can't be read is silently skipped.
                continue

            candidates.extend(newest_buf if newest else oldest_matches)
            if capped:
                # A source-wide cap was hit inside this file; later files in
                # the source are not read at all, matching the "stop as soon
                # as the cap is reached" contract rather than reading further
                # files while under-reporting the ones already skipped.
                break

        candidates.sort(key=_started_at_key, reverse=newest)
        return candidates[:n], capped, total_matches > n

    def _read_matching(self) -> List[Dict[str, Any]]:
        """
        Read every valid trace from the source and return those that pass all
        current predicates.

        Reads each .jsonl file sequentially, skipping malformed lines and files
        that can't be opened. Order within a file is preserved; folder sources
        are read in sorted filename order.

        Unbounded by design: .all()/.count()/.render_table() need to see every
        matching trace, so there is no smaller candidate set to bound memory
        against the way _read_bounded() does for .last()/.first()/.query().
        """
        results: List[Dict[str, Any]] = []
        for filepath in _jsonl_files(self._path):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        obj = _parse_line(line)
                        if obj is not None and _passes(obj, self._predicates):
                            results.append(obj)
            except OSError:
                # A file that vanished or can't be read is silently skipped.
                continue
        return results


# ---------------------------------------------------------------------------
# Predicate builder
# ---------------------------------------------------------------------------

def _parse_filter_key(key: str) -> Tuple[str, str]:
    """
    Parse a filter keyword argument key into (field, op).

    "status"           → ("status", "eq")
    "action__contains" → ("action", "contains")
    """
    if "__" in key:
        field, op = key.rsplit("__", 1)
        return field, op
    return key, "eq"


def _build_predicate(
    key: str, value: Any
) -> Callable[[Dict[str, Any]], bool]:
    """
    Parse a filter keyword argument into a predicate function.

    key syntax:  "field"           → exact equality
                 "field__contains" → case-insensitive substring
                 "field__startswith"
                 "field__endswith"
                 "field__re"       → regex search (re.search)

    The predicate returns False (exclude) when the field is absent from the
    trace dict — there is no separate "field exists" check needed in callers.
    """
    if "__" in key:
        field, op = key.rsplit("__", 1)
    else:
        field, op = key, "eq"

    if op not in ("eq", "contains", "startswith", "endswith", "re"):
        raise ValueError(
            f"Unknown TraceLog filter operator {op!r} in key {key!r}. "
            "Supported: contains, startswith, endswith, re (or no suffix for exact match)."
        )

    def pred(trace: Dict[str, Any]) -> bool:
        raw = trace.get(field)
        # A missing field never matches any filter, regardless of operator.
        if raw is None:
            return value is None and op == "eq"
        if op == "eq":
            return raw == value
        # String operators: convert both sides to lowercase for case-insensitive
        # matching. The value comes from the caller; raw comes from the trace.
        s = str(raw).lower()
        v = str(value).lower()
        if op == "contains":
            return v in s
        if op == "startswith":
            return s.startswith(v)
        if op == "endswith":
            return s.endswith(v)
        if op == "re":
            # re.search allows the pattern to match anywhere in the string,
            # which is more useful than a full-string re.fullmatch here.
            return bool(_re.search(str(value), str(raw)))
        return False  # unreachable; op validated above

    return pred


def _passes(
    trace: Dict[str, Any],
    predicates: List[Callable[[Dict[str, Any]], bool]],
) -> bool:
    """Return True if the trace passes every predicate (AND logic)."""
    return all(p(trace) for p in predicates)


# ---------------------------------------------------------------------------
# File I/O helpers (duplicated from viewer/reader.py intentionally)
# ---------------------------------------------------------------------------
#
# TraceLog is a core module and must not import from traceact.viewer.*,
# which ships as a separate optional sub-package. The three functions below
# are deliberately self-contained copies of the equivalent viewer helpers.

_REQUIRED_KEYS = ("trace_id", "action", "started_at")


def _jsonl_files(path: str) -> List[str]:
    """
    Resolve a source path to the ordered list of .jsonl files it contains.

    A file path → [that file].  A directory → sorted, deduplicated union of
    *.jsonl and *.jsonl.* — the second pattern covers segments rotated by
    JsonlSink versions that appended the rotation timestamp after the
    extension (current versions keep the extension last).
    A missing path → [] (no data yet — not an error).
    """
    if os.path.isdir(path):
        matches = set(glob.glob(os.path.join(path, "*.jsonl")))
        matches.update(glob.glob(os.path.join(path, "*.jsonl.*")))
        return sorted(matches)
    if os.path.isfile(path):
        return [path]
    return []


def _parse_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse one JSONL line into a trace dict, or None if it isn't a valid trace.

    Blank lines, truncated lines caught mid-write, and objects missing the
    required trace keys are all silently skipped.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if not all(k in obj for k in _REQUIRED_KEYS):
        return None
    return obj


def _started_at_key(trace: Dict[str, Any]) -> str:
    """
    Sort key for ordering by start time.

    started_at is an ISO 8601 string, which sorts correctly lexicographically.
    Traces missing the field sort first (empty string < any timestamp).
    """
    return trace.get("started_at") or ""
