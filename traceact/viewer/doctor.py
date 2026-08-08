# viewer/doctor.py
#
# Shared health-check logic behind `traceact doctor` (CLI) and the viewer's
# Settings > "Run diagnostics" button (GET /api/doctor). Both surfaces call
# run_checks() and get back the same structured result; the CLI renders it as
# text, the API returns it as JSON for the Settings page to render.
#
# Checks performed:
#   - Python version meets the 3.10 minimum
#   - the ~/.traceact state directory exists and is writable (single-instance
#     coordination and drag-drop imports depend on this)
#   - whether a viewer is currently running (informational only — never a
#     failure; a viewer isn't required for tracing to work)
#   - if a source path is given: that it exists and its lines parse as valid
#     trace records

import json
import os
import sys
from typing import Any, Dict, List, Optional

from traceact import __version__
from traceact.viewer.reader import is_valid_trace, _jsonl_files
import traceact.viewer.instance as _instance


def run_checks(source: Optional[str] = None) -> Dict[str, Any]:
    """
    Run every check and return a structured result:

        {
            "ok": bool,             # True if every "pass"/"fail" check passed
            "version": "0.3.0",
            "checks": [
                {"label": str, "status": "pass"|"fail"|"info", "message": str,
                 "hint": str},  # "hint" is present only on "fail" checks —
                                 # what the failure means and what to do about it
                ...
            ],
        }

    "info" checks (traceact's version, whether a viewer is running) never
    affect `ok` — only "pass"/"fail" checks do. A missing running viewer is
    always "info", never a failure.
    """
    checks: List[Dict[str, str]] = []

    py_ok = sys.version_info >= (3, 10)
    py_check = {
        "label": "python_version",
        "status": "pass" if py_ok else "fail",
        "message": (
            f"Python {sys.version_info.major}.{sys.version_info.minor} "
            f"({'meets the 3.10+ requirement' if py_ok else 'is below the 3.10 minimum'})"
        ),
    }
    if not py_ok:
        py_check["hint"] = (
            "TraceAct needs Python 3.10 or later. Install a newer Python and "
            "reinstall traceact into that environment."
        )
    checks.append(py_check)

    checks.append({
        "label": "traceact_version",
        "status": "info",
        "message": f"traceact {__version__}",
    })

    state_dir = os.path.expanduser("~/.traceact")
    try:
        os.makedirs(state_dir, exist_ok=True)
        writable = os.access(state_dir, os.W_OK)
    except OSError:
        writable = False
    state_check = {
        "label": "state_dir",
        "status": "pass" if writable else "fail",
        "message": (
            f"State directory ({state_dir}) is writable" if writable
            else f"State directory ({state_dir}) is not writable"
        ),
    }
    if not writable:
        state_check["hint"] = (
            f"Check the permissions on {state_dir} (or your home directory). "
            "TraceAct uses this folder to coordinate single-instance reuse and "
            "store drag-and-dropped files — it can't do either without write access."
        )
    checks.append(state_check)

    existing = _instance.find_running()
    if existing is not None:
        health = existing["health"]
        checks.append({
            "label": "viewer_running",
            "status": "info",
            "message": (
                f"Viewer running at http://{existing['host']}:{existing['port']}/ "
                f"(v{health.get('version', '?')}, {health.get('sources', 0)} source(s))"
            ),
        })
    else:
        checks.append({
            "label": "viewer_running",
            "status": "info",
            "message": "No viewer currently running (not required).",
        })

    if source is not None:
        checks.append(_check_source(source))

    ok = all(c["status"] != "fail" for c in checks)
    return {"ok": ok, "version": __version__, "checks": checks}


def scan_source(path: str) -> Dict[str, Any]:
    """
    Scan a source's files for known credential formats (the same
    redaction.VALUE_PATTERNS registry that redacts captured values at trace
    time) and report what is sitting in them.

    Capture-time scanning protects records written from now on; this audits
    the ones already on disk — traces written before value scanning existed,
    with redact_values off, or by an older TraceAct. A hit means a secret is
    in the file right now, whatever the viewer or exporter does about it.

    Returns:
        {
            "ok": bool,          # True when nothing was found
            "files": int,        # files scanned
            "lines": int,        # lines scanned
            "hits": [            # one entry per finding, capped
                {"pattern": str, "file": str, "line": int},
                ...
            ],
            "hits_capped": bool, # True if more findings exist than listed
        }
    """
    from traceact.redaction import find_value_patterns
    from traceact.viewer.reader import is_sqlite_file

    max_hits = 100
    hits: List[Dict[str, Any]] = []

    if os.path.isfile(path) and is_sqlite_file(path):
        return _scan_sqlite_source(path, max_hits)

    files = _jsonl_files(path) if os.path.exists(path) else []
    lines_scanned = 0
    capped = False

    for filepath in files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    lines_scanned += 1
                    for pattern_name in find_value_patterns(line):
                        if len(hits) >= max_hits:
                            capped = True
                            break
                        hits.append({
                            "pattern": pattern_name,
                            "file": filepath,
                            "line": lineno,
                        })
                    if capped:
                        break
        except OSError:
            continue
        if capped:
            break

    return {
        "ok": not hits,
        "files": len(files),
        "lines": lines_scanned,
        "hits": hits,
        "hits_capped": capped,
    }


def _check_source(path: str) -> Dict[str, str]:
    """
    Validate a source path: it must exist and contain at least one .jsonl
    file whose lines parse as JSON and look like trace records. An empty
    (freshly created) source is reported as "info", not a failure.
    """
    if not os.path.exists(path):
        return {
            "label": "source", "status": "fail",
            "message": f"Source '{path}' does not exist",
            "hint": "Double-check the path — the file or folder may have "
                    "been moved, renamed, or deleted since it was added.",
        }

    from traceact.viewer.reader import is_sqlite_file
    if os.path.isfile(path) and is_sqlite_file(path):
        return _check_sqlite_source(path)

    files = _jsonl_files(path)
    if not files:
        return {
            "label": "source", "status": "fail",
            "message": f"Source '{path}' has no .jsonl files",
            "hint": "Point to a .jsonl file directly, or a folder that "
                    "contains at least one.",
        }

    total_lines = 0
    valid_lines = 0
    for filepath in files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    total_lines += 1
                    try:
                        if is_valid_trace(json.loads(line)):
                            valid_lines += 1
                    except (json.JSONDecodeError, ValueError):
                        pass
        except OSError as e:
            return {
                "label": "source", "status": "fail",
                "message": f"Could not read {filepath}: {e}",
                "hint": "Check file permissions, or that the file isn't "
                        "locked by another process.",
            }

    if total_lines == 0:
        return {
            "label": "source", "status": "info",
            "message": f"{path}: {len(files)} file(s), no trace lines yet.",
        }

    passed = valid_lines > 0
    result = {
        "label": "source",
        "status": "pass" if passed else "fail",
        "message": (
            f"{path}: {valid_lines}/{total_lines} line(s) look like valid "
            f"traces across {len(files)} file(s)"
        ),
    }
    if not passed:
        result["hint"] = (
            "None of the lines matched the expected trace shape (a trace "
            "needs trace_id, action, and started_at). Confirm this file is "
            "TraceAct output and not something else."
        )
    return result


def _scan_sqlite_source(path: str, max_hits: int) -> Dict[str, Any]:
    """
    The credential scan over a SQLite source's record column. "line" in each
    hit is the table row id, which is what a follow-up
    `SELECT record FROM traces WHERE id = ?` needs to inspect the finding.
    """
    from traceact.redaction import find_value_patterns
    from traceact.viewer.reader import _sqlite_connect

    hits: List[Dict[str, Any]] = []
    rows_scanned = 0
    capped = False
    try:
        conn = _sqlite_connect(path)
        try:
            for row_id, raw in conn.execute(
                "SELECT id, record FROM traces ORDER BY id ASC"
            ):
                rows_scanned += 1
                for pattern_name in find_value_patterns(raw):
                    if len(hits) >= max_hits:
                        capped = True
                        break
                    hits.append({
                        "pattern": pattern_name,
                        "file": path,
                        "line": row_id,
                    })
                if capped:
                    break
        finally:
            conn.close()
    except Exception:
        pass

    return {
        "ok": not hits,
        "files": 1 if rows_scanned or os.path.exists(path) else 0,
        "lines": rows_scanned,
        "hits": hits,
        "hits_capped": capped,
    }


def _check_sqlite_source(path: str) -> Dict[str, str]:
    """
    Validate a SQLite source: the sink's `traces` table must exist, and its
    record column must hold parseable trace documents. This is the loud
    counterpart to the reader's tolerance — the viewer shows an unreadable
    database as simply empty, and this check says why.
    """
    from traceact.viewer.reader import _sqlite_connect

    try:
        conn = _sqlite_connect(path)
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='traces'"
            ).fetchone()
            if table is None:
                return {
                    "label": "source", "status": "fail",
                    "message": f"{path}: SQLite database with no 'traces' "
                               "table",
                    "hint": "The viewer reads SqliteSink output, which "
                            "writes a 'traces' table. A custom table= name "
                            "isn't supported yet; this database may not be "
                            "TraceAct output at all.",
                }
            total = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
            sample = conn.execute(
                "SELECT record FROM traces ORDER BY id ASC LIMIT 50"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return {
            "label": "source", "status": "fail",
            "message": f"Could not read {path}: {exc}",
            "hint": "Check file permissions, or whether another process "
                    "holds a long write lock on the database.",
        }

    if total == 0:
        return {
            "label": "source", "status": "info",
            "message": f"{path}: SQLite source, no trace rows yet.",
        }

    valid = 0
    for (raw,) in sample:
        try:
            if is_valid_trace(json.loads(raw)):
                valid += 1
        except (json.JSONDecodeError, ValueError):
            pass
    passed = valid > 0
    result = {
        "label": "source",
        "status": "pass" if passed else "fail",
        "message": (
            f"{path}: SQLite source, {total} row(s); {valid}/{len(sample)} "
            "sampled record(s) look like valid traces"
        ),
    }
    if not passed:
        result["hint"] = (
            "The traces table exists but its record column doesn't hold "
            "TraceAct trace documents. Confirm this database was written "
            "by SqliteSink."
        )
    return result
