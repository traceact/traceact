# TraceAct

[![PyPI version](https://img.shields.io/pypi/v/traceact.svg)](https://pypi.org/project/traceact/)
[![Python versions](https://img.shields.io/pypi/pyversions/traceact.svg)](https://pypi.org/project/traceact/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

X-ray vision for Python code.

TraceAct is a lightweight Python package for action-level tracing. It records the full story of what happens when a function runs — every step taken, resource touched, event recorded, and failure encountered — so you or your agent can understand what actually happened.

## Install

```bash
pip install traceact
```

## Quick start

```python
from traceact import traced_action, configure, TraceConfig, JsonlSink

configure(
    config=TraceConfig(sink_mode="blocking"),
    sinks=[JsonlSink("data/traces.jsonl")],
)

@traced_action(action="note.create", kind="app", actor="user")
def create_note(title, body):
    ...
```

Each traced function call produces one JSON object appended to the JSONL file. Open the viewer to explore it live.

## Manual tracing

```python
from traceact import ActionTrace

with ActionTrace.start(action="note.create", kind="app") as trace:
    trace.input({"title": "Hello"})
    trace.step("Validated input")
    trace.event(kind="db", operation="insert", target="notes")
    trace.output({"note_id": "note_123"})
```

## The viewer

TraceAct includes a local web viewer. No extra install — it ships with the package.

```bash
traceact view data/traces.jsonl
```

This starts a server at `http://127.0.0.1:8765` and opens your browser. The viewer tails the file live: traces appear as your app writes them.

### Source types

| What you pass | What happens |
|---|---|
| A `.jsonl` file | Tails that file live |
| A folder | Merges all `.jsonl` files inside (e.g. per-process shards) |
| Nothing | Opens empty; use the in-app modal to add a source |

### CLI flags

```bash
traceact view [SOURCE] [--port N] [--host HOST] [--no-browser] [--new]
traceact show [SOURCE] ...   # identical alias of view
```

| Flag | Default | Effect |
|---|---|---|
| `--port N` | `8765` | Port to serve on |
| `--host HOST` | `127.0.0.1` | Interface to bind (localhost only by default) |
| `--no-browser` | off | Start the server without opening a browser tab |
| `--new` | off | Force a fresh instance even if one is already running |

### Port selection

The viewer auto-increments the port if the requested one is taken. If you ask for `8765` and it's in use, it tries `8766`, `8767`, and so on up to 20 times before giving up. Pass `--port` to start from a different base.

### Single-instance behaviour

Running `traceact view` a second time reuses an already-running viewer rather than starting a second server. The new source (if given) is added to the running viewer and a browser tab is opened on it. This means you can call `traceact view path/to/new-file.jsonl` from multiple terminal tabs during a session and they all feed into one viewer.

Pass `--new` to bypass this and force a second independent instance.

### macOS launcher

Double-click `launch.command` in the repo root to open TraceAct from Finder without a terminal. It checks for a running instance first, then creates a `.venv/`, installs or upgrades `traceact`, and opens the browser.

### Adding sources in the app

The source modal (click the source name in the header) lets you:

- **Choose file / Choose folder** — opens a native macOS picker; returns the filesystem path for live tailing
- **Drag and drop** a `.jsonl` file — saved as a static snapshot in `~/.traceact/imports/`
- **Type a path** — collapsible fallback for pasting an absolute path

### Health checks

```bash
traceact doctor [SOURCE]
```

Checks Python version, that `~/.traceact` is writable, whether a viewer is already running, and (if `SOURCE` is given) that the file or folder parses as valid trace data. Useful for ruling out setup problems before debugging your own code. The same checks are also available from the viewer itself — Settings > **Run diagnostics**. See [USAGE.md](USAGE.md#viewing-traces) for full output and exit-code details.

## Concepts

| Concept | Meaning |
|---|---|
| `Trace` | The full record of one action (function call) |
| `Step` | A human-readable timeline marker within a trace |
| `Event` | A structured operation: db, http, file, model, job, etc. |
| `Touch` | A resource involved in the trace (auto-derived from events) |
| `Sink` | Where completed traces are written (`JsonlSink`, `ConsoleSink`, `AsyncSink`) |

### Design principle: observable by choice, never forced blind

TraceAct exists to give you X-ray vision for your code. That means nothing TraceAct does itself should take that vision away.

Wherever TraceAct might skip, drop, or truncate data — a trace sampled out by `sample_rate`, events truncated by a budget limit, records dropped by `AsyncSink` under backpressure — there is always an observable signal. The `budget_hit` flag marks truncated traces. The `AsyncSink.dropped` counter counts every dropped record. Sampling decisions are made before a trace object is created, so nothing is half-recorded.

The design choice is always: **silent by default, observable by choice**. You decide whether to log, alert on, or ignore those signals. TraceAct never makes that decision for you.

## Wiring into a web app

If your app has its own UI, add a backend route to launch or connect to the viewer, then call it from a button:

```python
# FastAPI — launch_or_connect is blocking, so use run_in_executor
import asyncio
from traceact.viewer.instance import launch_or_connect

@router.get("/api/launch-viewer")
async def launch_viewer():
    loop = asyncio.get_event_loop()
    url = await loop.run_in_executor(None, launch_or_connect,
                                     "data/traces/traces.jsonl")
    return {"url": url}
```

```javascript
// Frontend button
document.getElementById("btn-viewer").addEventListener("click", async () => {
    const btn = document.getElementById("btn-viewer");
    btn.disabled = true;
    try {
        const { url } = await fetch("/api/launch-viewer").then(r => r.json());
        window.open(url, "_blank", "noopener");
    } finally {
        btn.disabled = false;
    }
});
```

`launch_or_connect` checks for a running viewer first (via `~/.traceact/viewer.json` + a health probe). If one is found, it adds your source to it and returns the URL immediately — no new process. If nothing is running, it spawns the viewer as a background subprocess and waits up to 3 s for it to be ready.

## Requirements

Python 3.9+. No runtime dependencies.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Full reference

See [USAGE.md](USAGE.md) for complete API documentation: all decorator and context manager parameters, helper methods (`trace.db`, `trace.http`, `trace.file`, `trace.model`), input capture, parent/child traces, sinks, budget configuration, the trace record schema, test isolation, and the full viewer server API.

## License

MIT

---

Built by [Mo Shehu](https://mohammedshehu.com).
