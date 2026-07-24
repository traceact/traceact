# viewer/server.py
#
# The viewer's local web server. Built entirely on the Python standard library
# (http.server) so the viewer adds no dependencies to TraceAct.
#
# What it serves:
#   GET  /                        the single-page app (static/index.html)
#   GET  /static/<file>           the app's CSS and JS
#   GET  /api/health              {"status":"ok","version":"<pkg version>","sources":N}
#   GET  /api/sources             list configured sources (JSON)
#   POST /api/sources             add a source by path (JSON body)
#   GET  /api/pick?type=file|folder  open a native OS dialog; returns {"path":"..."}
#   POST /api/import              save dropped-file content, add as snapshot source
#   GET  /api/stream?source=&limit=   Server-Sent Events: snapshot then live tail
#
# Why SSE and not WebSockets:
# The data flow is one-directional — the server pushes traces to the browser and
# the browser never sends trace data back. SSE is exactly that shape, runs over
# plain HTTP, needs no library, and reconnects automatically. WebSockets would
# be more machinery for no benefit.
#
# The stream design (snapshot + tail in one connection):
# When the browser opens /api/stream for a source, the server creates one
# SourceReader, sends the most recent N traces as an initial "snapshot" message,
# then polls that same reader on an interval and pushes any newly-appended
# traces as "append" messages. Because one reader handles both phases, the tail
# begins exactly where the snapshot ended — no trace is sent twice and none is
# missed in a gap between two separate requests.
#
# Concurrency:
# ThreadingHTTPServer handles each request on its own thread, so a long-lived
# SSE connection never blocks other requests (static files, adding a source,
# a second stream). Threads are daemons so the process can exit cleanly.

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

from traceact import __version__
from traceact.viewer.reader import SourceReader

# Directory where drag-dropped files are saved so they can be tailed.
_IMPORTS_DIR = os.path.expanduser("~/.traceact/imports")

# Only one native OS picker dialog may be open at once.
_picker_lock = threading.Lock()

# Directory holding index.html, styles.css, app.js (shipped inside the package).
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# How often the live tail checks each source for newly-appended traces.
_POLL_INTERVAL_SECONDS = 0.5

# Content types for the handful of static files the viewer serves.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


class ViewerState:
    """
    Shared, in-memory state for the running viewer: the set of sources the user
    has added. A source is a friendly name mapped to a filesystem path (a .jsonl
    file or a folder of them).

    Sources live only for the lifetime of the process. Persisting them to a
    config file (so the sidebar remembers them across runs) is a v0.3 addition;
    for now each `traceact view` starts fresh, optionally seeded with the source
    given on the command line.
    """

    def __init__(self) -> None:
        self.sources: Dict[str, str] = {}

    def add_source(self, path: str, name: Optional[str] = None) -> str:
        """
        Register a source and return the name it was stored under.

        If no name is given, one is derived from the path: the folder name for a
        directory, or the file's stem for a file. Collisions get a numeric
        suffix so two files with the same name stay distinct.
        """
        path = os.path.abspath(os.path.expanduser(path))
        if name is None:
            name = _derive_name(path)
        name = self._unique_name(name)
        self.sources[name] = path
        return name

    def _unique_name(self, name: str) -> str:
        if name not in self.sources:
            return name
        i = 2
        while f"{name}-{i}" in self.sources:
            i += 1
        return f"{name}-{i}"


class _Handler(BaseHTTPRequestHandler):
    """
    Handles every request. The shared ViewerState is reached via
    self.server.state (attached when the server is created).
    """

    # Quieter default logging: the stdlib handler logs every request to stderr,
    # which is noisy for a local tool. Override to stay silent.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/":
            self._serve_static("index.html")
        elif route.startswith("/static/"):
            self._serve_static(route[len("/static/"):])
        elif route == "/api/health":
            self._serve_health()
        elif route == "/api/sources":
            self._serve_sources()
        elif route == "/api/pick":
            self._serve_pick(parse_qs(parsed.query))
        elif route == "/api/stream":
            self._serve_stream(parse_qs(parsed.query))
        else:
            self._send_error(404, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/sources":
            self._add_source()
        elif parsed.path == "/api/import":
            self._import_file()
        else:
            self._send_error(404, "Not found")

    # -- static files ------------------------------------------------------

    def _serve_static(self, filename: str) -> None:
        # Reduce to a bare filename to prevent path traversal (../../etc).
        filename = os.path.basename(filename)
        filepath = os.path.join(_STATIC_DIR, filename)
        if not os.path.isfile(filepath):
            self._send_error(404, "Not found")
            return
        ext = os.path.splitext(filename)[1]
        content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(filepath, "rb") as f:
                body = f.read()
        except OSError:
            self._send_error(500, "Could not read file")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- health ------------------------------------------------------------

    def _serve_health(self) -> None:
        self._send_json(200, {
            "status": "ok",
            "version": __version__,
            "sources": len(self.server.state.sources),  # type: ignore[attr-defined]
        })

    # -- native OS file/folder picker --------------------------------------

    def _serve_pick(self, query: Dict[str, list]) -> None:
        pick_type = _first(query.get("type")) or "file"
        if not _picker_lock.acquire(blocking=False):
            self._send_json(409, {"error": "A picker dialog is already open."})
            return
        try:
            path = _open_native_picker(pick_type)
        finally:
            _picker_lock.release()
        if path is None:
            self._send_json(200, {"path": None, "cancelled": True})
        else:
            self._send_json(200, {"path": path, "cancelled": False})

    # -- drag-drop import --------------------------------------------------

    def _import_file(self) -> None:
        body = self._read_json_body()
        if body is None or "content" not in body or "name" not in body:
            self._send_json(400, {"error": "expected {name, content}"})
            return
        # Sanitise the filename: strip any directory component, keep the stem.
        raw_name = os.path.basename(body["name"]) or "import"
        if not raw_name.endswith(".jsonl"):
            raw_name += ".jsonl"
        try:
            os.makedirs(_IMPORTS_DIR, exist_ok=True)
            dest = os.path.join(_IMPORTS_DIR, raw_name)
            # Avoid clobbering an existing import with a numeric suffix.
            if os.path.exists(dest):
                stem = raw_name[:-6]  # strip .jsonl
                i = 2
                while os.path.exists(os.path.join(_IMPORTS_DIR, f"{stem}-{i}.jsonl")):
                    i += 1
                dest = os.path.join(_IMPORTS_DIR, f"{stem}-{i}.jsonl")
            with open(dest, "w", encoding="utf-8") as f:
                f.write(body["content"])
        except OSError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        name = self.server.state.add_source(dest, body.get("label"))  # type: ignore[attr-defined]
        self._send_json(200, {
            "name": name,
            "path": dest,
            "imported": True,
        })

    # -- sources API -------------------------------------------------------

    def _serve_sources(self) -> None:
        payload = [
            {"name": name, "path": path}
            for name, path in self.server.state.sources.items()  # type: ignore[attr-defined]
        ]
        self._send_json(200, payload)

    def _add_source(self) -> None:
        body = self._read_json_body()
        if body is None or "path" not in body:
            self._send_json(400, {"error": "expected JSON body with a 'path'"})
            return
        name = self.server.state.add_source(  # type: ignore[attr-defined]
            body["path"], body.get("name")
        )
        self._send_json(
            200, {"name": name, "path": self.server.state.sources[name]}  # type: ignore[attr-defined]
        )

    # -- SSE stream --------------------------------------------------------

    def _serve_stream(self, query: Dict[str, list]) -> None:
        source_name = _first(query.get("source"))
        limit = _to_int(_first(query.get("limit")), default=100)

        sources = self.server.state.sources  # type: ignore[attr-defined]
        if source_name is None or source_name not in sources:
            self._send_json(404, {"error": "unknown source"})
            return

        reader = SourceReader(sources[source_name])

        # Open the event stream. No Content-Length: the connection stays open.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            # Phase 1: the snapshot — the most recent N traces, newest-first.
            snapshot = reader.snapshot(limit)
            self._send_event({"kind": "snapshot", "traces": snapshot})

            # Phase 2: the live tail — poll for appended traces forever, until
            # the browser disconnects (which surfaces as a write error).
            # poll() returns kind="snapshot" instead of "append" when a source
            # file was deleted and recreated mid-stream, so the client replaces
            # its trace list instead of prepending onto stale data.
            while True:
                time.sleep(_POLL_INTERVAL_SECONDS)
                result = reader.poll(limit)
                if result["traces"]:
                    self._send_event(result)
                else:
                    # A comment line acts as a heartbeat: it keeps the
                    # connection alive and lets us notice a dropped client.
                    self._write_raw(": keepalive\n\n")
        except (BrokenPipeError, ConnectionResetError):
            # The browser closed the tab or navigated away. Normal; just stop.
            return

    def _send_event(self, obj: dict) -> None:
        """Write one SSE message carrying a JSON payload."""
        self._write_raw(f"data: {json.dumps(obj, default=str)}\n\n")

    def _write_raw(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    # -- small response helpers -------------------------------------------

    def _read_json_body(self) -> Optional[dict]:
        length = _to_int(self.headers.get("Content-Length"), default=0)
        if length <= 0:
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, OSError):
            return None

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", _CONTENT_TYPES[".json"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})


class ViewerServer(ThreadingHTTPServer):
    """A threading HTTP server that carries the shared ViewerState."""

    # Daemon threads so a long-lived SSE connection can't keep the process alive.
    daemon_threads = True
    # Let the port be reused immediately on restart (avoids "address in use").
    allow_reuse_address = True

    def __init__(self, host: str, port: int, state: ViewerState) -> None:
        super().__init__((host, port), _Handler)
        self.state = state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_native_picker(pick_type: str) -> Optional[str]:
    """
    Open a native OS file or folder picker and return the selected path, or
    None if the user cancelled.

    macOS: uses osascript (AppleScript), which ships with every Mac and needs no
    extra dependencies.  Falls back to tkinter (stdlib) on other platforms.
    """
    import platform
    if platform.system() == "Darwin":
        return _pick_via_osascript(pick_type)
    return _pick_via_tkinter(pick_type)


def _pick_via_osascript(pick_type: str) -> Optional[str]:
    if pick_type == "folder":
        script = 'POSIX path of (choose folder with prompt "Select a folder of JSONL traces")'
    else:
        script = (
            'POSIX path of (choose file of type {"jsonl", "public.plain-text"} '
            'with prompt "Select a JSONL trace file")'
        )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return None  # user cancelled
        return result.stdout.strip().rstrip("/") or None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _pick_via_tkinter(pick_type: str) -> Optional[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if pick_type == "folder":
            path = filedialog.askdirectory(title="Select a folder of JSONL traces")
        else:
            path = filedialog.askopenfilename(
                title="Select a JSONL trace file",
                filetypes=[("JSONL files", "*.jsonl"), ("All files", "*.*")],
            )
        root.destroy()
        return path or None
    except Exception:
        return None


def _derive_name(path: str) -> str:
    """Human-friendly source name from a path: folder name, or file stem."""
    if os.path.isdir(path):
        return os.path.basename(os.path.normpath(path)) or "source"
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem or "source"


def _first(values: Optional[list]) -> Optional[str]:
    """First value from a parse_qs list, or None."""
    return values[0] if values else None


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
