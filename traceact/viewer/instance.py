# viewer/instance.py
#
# Single-instance coordination for the viewer.
#
# The problem: TraceAct can now read any JSONL source, so a user rarely needs
# more than one viewer running. Launching `traceact view` a second time (for a
# different file, say) should ideally just add that source to the viewer that is
# already open, rather than spinning up a second server on another port.
#
# Why a state file, not a PID scan:
# We could enumerate running processes and grep their command lines for a
# viewer, but that is fragile and platform-specific (ps parsing, permissions,
# psutil dependency). Instead, a running viewer writes a tiny state file naming
# its host and port. A new launch reads that file and *probes* the port with an
# HTTP health check. If a live viewer answers, we reuse it; if not (stale file,
# crashed process), we start fresh and overwrite the file. This is how Jupyter
# and similar local tools coordinate, and it needs nothing beyond the stdlib.
#
# When a NEW instance is still wanted (handled by the CLI, not here):
#   - the user passed --new to force a fresh instance
#   - the user asked for a specific --port (they want that exact server)
#   - the recorded instance is unreachable (we then replace it)

import json
import os
import urllib.request
from typing import Any, Dict, Optional

_STATE_DIR = os.path.expanduser("~/.traceact")
_STATE_FILE = os.path.join(_STATE_DIR, "viewer.json")


def write_state(host: str, port: int) -> None:
    """Record the running viewer's location so later launches can find it."""
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"host": host, "port": port, "pid": os.getpid()}, f)
    except OSError:
        # Not being able to write the state file is non-fatal; it just means
        # single-instance reuse won't kick in. The viewer still runs.
        pass


def clear_state() -> None:
    """Remove the state file on clean shutdown."""
    try:
        os.remove(_STATE_FILE)
    except OSError:
        pass


def _read_state() -> Optional[Dict[str, Any]]:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def probe(host: str, port: int, timeout: float = 0.5) -> Optional[dict]:
    """
    Ask a candidate viewer whether it is alive. Returns its health payload
    (which includes the version and source count) or None if nothing answers.
    """
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return None


def find_running() -> Optional[Dict[str, Any]]:
    """
    Return {host, port, health} for a live viewer recorded in the state file,
    or None if there is no record or the recorded viewer is not responding.
    """
    state = _read_state()
    if not state:
        return None
    host, port = state.get("host"), state.get("port")
    if not host or not port:
        return None
    health = probe(host, port)
    if health is None:
        return None
    return {"host": host, "port": port, "health": health}


def launch_or_connect(
    source: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    timeout: float = 3.0,
) -> str:
    """
    Ensure a viewer is running and return its URL.  Designed to be called from
    a web-app backend (e.g. a FastAPI route) so a "traceact viewer" button can
    smart-launch: reuse an existing viewer if one is running, otherwise start
    one in the background.

    Usage in a FastAPI / Flask app::

        from traceact.viewer.instance import launch_or_connect

        @app.get("/api/launch-viewer")
        async def launch_viewer():
            url = launch_or_connect(source="data/traces/traces.jsonl")
            return {"url": url}

    The front-end then does::

        const { url } = await fetch("/api/launch-viewer").then(r => r.json());
        window.open(url, "_blank");

    If an existing viewer is found it gets the source added (if given) and the
    URL is returned immediately — no new process is started.  If nothing is
    running a background subprocess is spawned, waited on for `timeout` seconds,
    and then the URL is returned (the server is usually ready in < 0.5 s).

    The returned URL carries ``?source=<name>`` when a source was given, so the
    viewer opens on *that* source. Without it, the front-end selects whichever
    source is first in the list — so an app adding its source to a viewer
    another app had already started would open showing the other app's traces.
    """
    import subprocess
    import sys
    import time
    from urllib.parse import quote

    existing = find_running()
    if existing is not None:
        h, p = existing["host"], existing["port"]
        if source is not None:
            added = add_source_to(h, p, source)
            if added and added.get("name"):
                return f"http://{h}:{p}/?source={quote(added['name'], safe='')}"
        return f"http://{h}:{p}/"

    # Not running — start one in the background.
    cmd = [sys.executable, "-m", "traceact.viewer.cli", "view", "--no-browser",
           "--host", host, "--port", str(port)]
    if source is not None:
        cmd.append(source)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait for it to be ready (polls health endpoint).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(host, port) is not None:
            break
        time.sleep(0.1)

    # A freshly spawned viewer was seeded with this source on the command
    # line, so it's the only one registered — but name it explicitly anyway,
    # so the URL stays correct if another app adds a source before the tab
    # is opened.
    if source is not None:
        names = list_source_names(host, port)
        if names:
            return f"http://{host}:{port}/?source={quote(names[0], safe='')}"
    return f"http://{host}:{port}/"


def add_source_to(host: str, port: int, path: str,
                  timeout: float = 1.0) -> Optional[dict]:
    """
    Ask an already-running viewer to add a source. Returns the created source
    ({name, path}) or None on failure.
    """
    url = f"http://{host}:{port}/api/sources"
    data = json.dumps({"path": path}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def list_source_names(host: str, port: int,
                      timeout: float = 1.0) -> list:
    """
    Return the names of the sources registered with a running viewer, in the
    order the viewer reports them. Empty list on any failure.
    """
    url = f"http://{host}:{port}/api/sources"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            sources = json.loads(resp.read().decode("utf-8"))
        return [s["name"] for s in sources if isinstance(s, dict) and "name" in s]
    except Exception:
        return []
