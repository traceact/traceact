# viewer/doctor.py
#
# Shared health-check logic behind `traceact doctor` (CLI) and the viewer's
# Settings > "Run diagnostics" button (GET /api/doctor). Both surfaces call
# run_checks() and get back the same structured result; the CLI renders it as
# text, the API returns it as JSON for the Settings page to render.
#
# Checks performed:
#   - Python version meets the 3.9 minimum
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
            "version": "0.2.1",
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

    py_ok = sys.version_info >= (3, 9)
    py_check = {
        "label": "python_version",
        "status": "pass" if py_ok else "fail",
        "message": (
            f"Python {sys.version_info.major}.{sys.version_info.minor} "
            f"({'meets the 3.9+ requirement' if py_ok else 'is below the 3.9 minimum'})"
        ),
    }
    if not py_ok:
        py_check["hint"] = (
            "TraceAct needs Python 3.9 or later. Install a newer Python and "
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
