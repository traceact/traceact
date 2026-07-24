# TraceAct Usage Reference

Full API and CLI documentation for TraceAct.

## Package layout

```
traceact/
  __init__.py     — public exports
  trace.py        — ActionTrace class, core lifecycle
  decorators.py   — @traced_action decorator (sync + async)
  config.py       — TraceConfig, configure(), reset_config()
  budget.py       — TraceBudget, TraceBudget.production() preset
  context.py      — ContextVar for active trace, SKIP sentinel
  sinks.py        — JsonlSink (thread-safe), ConsoleSink, AsyncSink (not yet public)
  helpers.py      — TraceHelpersMixin (trace.db, trace.http, trace.file, trace.model)
  ids.py          — ID generation (trc_, evt_, stp_, corr_ prefixes)

  viewer/
    cli.py        — `traceact view` / `traceact show` CLI entry point; --new flag
    server.py     — stdlib ThreadingHTTPServer; SPA + REST + SSE
    reader.py     — SourceReader: JSONL snapshot + live byte-offset tail
    instance.py   — single-instance coordination (state file + HTTP probe);
                    launch_or_connect() for embedding in app backends
    static/
      index.html  — single-page app shell
      styles.css  — design system (dark theme, CSS custom properties)
      app.js      — all frontend logic (no framework, no build step)
```

## Contents

1. [Installation](#installation)
2. [Configuration](#configuration)
3. [Decorator API](#decorator-api)
4. [Manual API](#manual-api)
5. [Recording steps](#recording-steps)
6. [Recording events](#recording-events)
7. [Helper methods](#helper-methods)
8. [Inputs and outputs](#inputs-and-outputs)
9. [Touches](#touches)
10. [Errors](#errors)
11. [Parent and child traces](#parent-and-child-traces)
12. [Input capture](#input-capture)
13. [Sinks](#sinks)
14. [Budget configuration](#budget-configuration)
15. [TraceConfig fields](#traceconfig-fields)
16. [Test isolation](#test-isolation)
17. [Trace record schema](#trace-record-schema)
18. [Viewing traces](#viewing-traces)
19. [Integrating the viewer into your app](#integrating-the-viewer-into-your-app)

---

## Installation

```bash
pip install traceact
```

From source (editable):

```bash
pip install -e /path/to/traceact
```

From a sibling directory (common in monorepo or demo setups):

```bash
pip install -e ../traceact
```

---

## Configuration

Call `configure()` once at application startup before any traces run.

```python
from traceact import configure, TraceConfig, TraceBudget, JsonlSink, ConsoleSink

configure(
    config=TraceConfig(
        enabled=True,
        sink_mode="blocking",   # "blocking" | "buffered" | "disabled"
        strict=False,
        redact_by_default=True,
        capture_inputs=False,   # global kill switch for automatic input capture
        capture_outputs=True,
    ),
    budget=TraceBudget(
        max_events=100,
        max_steps=50,
        max_depth=10,
        max_payload_bytes=8192,
        sample_rate=1.0,
        always_trace_errors=True,
    ),
    sinks=[
        JsonlSink("data/traces/traces.jsonl"),
        # ConsoleSink(pretty=True),  # also print to stdout
    ],
)
```

All fields are optional. Omitted fields use package defaults. `configure()` can be called multiple times; later calls replace earlier ones.

**Sink modes:**
- `"blocking"` — write immediately when a trace finishes. Best for development.
- `"buffered"` — hold traces in memory, flush on exit or on explicit `flush_buffer()`. Best for production.
- `"disabled"` — decorators stay in place but nothing is recorded or written.

---

## Decorator API

`@traced_action` wraps any sync or async function. The async/sync decision is made at decoration time, not call time.

```python
from traceact import traced_action, TraceConfig, TraceBudget

@traced_action(
    action="note.create",           # required: dot-notation action name
    kind="app",                     # default: "app"
    actor="user",                   # who triggered this ("user", "cron", "agent")
    project="my-app",               # group traces by service or app
    operation="insert",             # creates an initial event if provided with target
    target="notes",                 # the resource (table, endpoint, file, model)
    database="sqlite",              # for kind="db" traces
    capture_inputs=False,           # False | True | ["field1", "field2"]
    meta={"release": "v1.2"},       # arbitrary key-value data
    config=TraceConfig(strict=True),# override package config for this trace only
    budget=TraceBudget(max_events=50), # override budget for this trace only
    correlation_id="corr_abc123",   # link this trace to related traces
)
def create_note(title, body, user_id):
    ...
```

**Async functions** work identically:

```python
@traced_action(action="payment.authorise", kind="payment")
async def authorise_payment(amount, currency):
    ...
```

**What the decorator does automatically:**
1. Creates a trace when the function is called.
2. Detects whether a parent trace is active and makes this a child trace if so.
3. Captures function arguments if `capture_inputs` is set.
4. Records timing.
5. Sets status to `"completed"` on success or `"failed"` on exception.
6. Captures the exception as an error on failure.
7. Re-raises exceptions — TraceAct never suppresses them.
8. Writes the trace to the configured sinks.

---

## Manual API

Use `ActionTrace.start()` when you want explicit control over what's recorded.

```python
from traceact import ActionTrace

with ActionTrace.start(
    action="note.create",
    kind="app",
    actor="user",
    project="my-app",
    correlation_id="corr_abc123",
    meta={"release": "v1.2"},
) as trace:
    trace.input({"title": "Hello", "user_id": 42})
    trace.step("Validated input")
    trace.event(kind="db", operation="insert", target="notes", rows=1)
    trace.output({"note_id": "note_123"})
```

The `with` block:
- Sets the trace as the active trace in the ContextVar on `__enter__`.
- Finishes with `status="completed"` on clean exit.
- Finishes with `status="failed"` and captures the exception if one is raised.
- Restores the ContextVar to its previous value on `__exit__`.

Any `@traced_action` calls made inside the `with` block automatically become child traces.

---

## Recording steps

Steps are human-readable timeline markers. They don't own events — both live on the same flat timeline, ordered by time.

```python
trace.step("Validated input")
trace.step("Built note object")
trace.step("Saved to database")
trace.step("Returned response")
```

Each step is recorded with a `step_id`, `label`, and `recorded_at` timestamp.

---

## Recording events

Events are structured operations. Use them for any interaction with an external system.

```python
trace.event(
    kind="db",                  # required: the event kind
    operation="insert",         # what was done
    target="notes",             # what was acted on
    status="completed",         # "completed" | "failed" | "pending" | "running" | "cancelled"
    duration_ms=12.4,           # how long it took
    result={"rows": 1},         # what the event produced
    error=None,                 # exception or error dict if it failed
    parent_event_id=None,       # for nested events
    # any extra kwargs are stored on the event:
    database="sqlite",
    rows=1,
    safe_query="INSERT INTO notes ...",
    params_shape={"title": "str"},
)
```

**Standard `kind` values:**

| kind | Use for |
|---|---|
| `"app"` | General application logic |
| `"db"` | Database operations |
| `"http"` | HTTP calls to external services |
| `"file"` | File reads, writes, deletes |
| `"model"` | LLM / AI model calls |
| `"cache"` | Cache reads and writes |
| `"queue"` | Queue publishes and consumes |
| `"auth"` | Authentication and authorisation |
| `"payment"` | Payment operations |
| `"email"` | Email sending |
| `"export"` | File exports |
| `"job"` | Background jobs |

Events automatically derive touches. A `kind="db"` event with `target="notes"` creates a `db_table:notes` touch on the trace.

---

## Helper methods

Helpers are shorthand wrappers around `trace.event()`. They accept the same keyword arguments.

```python
# Database
trace.db(operation="insert", target="notes", rows=1, database="sqlite")
trace.db(operation="select", target="users", rows=5)

# HTTP
trace.http(operation="post", target="stripe", status_code=200, duration_ms=120)
trace.http(operation="get", target="github-api")

# File
trace.file(operation="write", target="data/output.json", bytes_written=4096)
trace.file(operation="read", target="config/settings.yaml")

# Model
trace.model(operation="completion", target="claude-sonnet-5", tokens_in=800, tokens_out=200)
trace.model(operation="embedding", target="text-embedding-3-small")
```

All helpers use `target` as the resource field name. Aliases like `table`, `url`, or `path` aren't accepted.

---

## Inputs and outputs

### `trace.input(data)`

Records what came into the trace. Always works regardless of the `capture_inputs` setting. Applies redaction and size limits.

```python
trace.input({"title": "Hello", "user_id": 42})
trace.input({"password": "secret"})  # → {"password": "[REDACTED]"}
```

### `trace.output(data)`

Records what the trace produced. Respects `capture_outputs` setting.

```python
trace.output({"note_id": "note_123", "created": True})
```

### `trace.set_meta(key, value)`

Attach arbitrary metadata to the trace.

```python
trace.set_meta("release", "v1.2")
trace.set_meta("region", "eu-west-1")
```

---

## Touches

A touch records that a specific resource was involved in the trace. TraceAct deduplicates touches — the same resource is only recorded once regardless of how many times it's touched.

**Auto-derived from events** (preferred):

```python
trace.event(kind="db", operation="insert", target="notes")
# automatically adds: {"kind": "db_table", "target": "notes"}
```

**Manual:**

```python
trace.touch(kind="file", target="data/notes.json")
trace.touch(kind="db_table", target="users")
trace.touch(kind="http_endpoint", target="stripe")
```

**Touch kind mapping** (auto-derived from event kind):

| Event kind | Touch kind |
|---|---|
| `"db"` | `"db_table"` |
| `"http"` | `"http_endpoint"` |
| `"file"` | `"file"` |
| `"model"` | `"model"` |
| `"cache"` | `"cache_key"` |
| `"queue"` | `"queue"` |
| `"auth"` | `"auth_provider"` |
| `"email"` | `"email_service"` |

---

## Errors

Errors are captured automatically when a decorated function raises an exception. They can also be attached to events manually.

**On an event:**

```python
trace.event(
    kind="db",
    operation="insert",
    target="notes",
    status="failed",
    error={"type": "IntegrityError", "message": "Unique constraint failed"},
)
```

TraceAct also deduplicates errors at the trace level — if the same error type and message occurs multiple times, the trace-level error summary shows it once.

---

## Parent and child traces

Parent/child relationships are detected automatically via a `contextvars.ContextVar`. No manual wiring is needed.

```python
@traced_action(action="order.process", kind="app")
def process_order(order_id):
    # This becomes a child trace of process_order automatically:
    save_order(order_id)

@traced_action(action="order.save", kind="db", operation="insert", target="orders")
def save_order(order_id):
    ...
```

When `save_order` runs inside `process_order`:
- `save_order`'s trace gets `parent_trace_id` = `process_order`'s trace ID
- Both share the same `root_trace_id`
- When `save_order` finishes, it pushes a compact summary up to `process_order`
- `process_order`'s trace includes that summary in `child_summaries`

**Manual API inside a decorator trace:**

```python
@traced_action(action="report.export", kind="export")
def export_report():
    # Manual trace becomes a child automatically:
    with ActionTrace.start(action="report.render", kind="app") as child:
        child.step("Rendering PDF")
```

**Sampling and nested traces:**

When `sample_rate < 1.0` and a trace is sampled out, a skip sentinel is pushed onto the ContextVar. All nested `@traced_action` calls inside that function also produce nothing. A child trace can't appear in the sink without its parent.

---

## Input capture

By default, `@traced_action` doesn't capture function arguments.

**Capture selected fields (recommended):**

```python
@traced_action(
    action="note.create",
    kind="app",
    capture_inputs=["title", "user_id"],  # only these two fields
)
def create_note(title, body, user_id):
    ...
```

**Capture all arguments:**

```python
@traced_action(action="note.create", kind="app", capture_inputs=True)
def create_note(title, body, user_id):
    ...
```

**Global kill switch** (overrides all decorator-level settings):

```python
configure(config=TraceConfig(capture_inputs=False))
# Now capture_inputs=True on any @traced_action is ignored.
```

**What TraceAct does when capture is enabled:**
1. Maps positional args to parameter names using `inspect.signature()`.
2. Skips `self` and `cls`.
3. Redacts arguments whose names match sensitive patterns: `password`, `passwd`, `secret`, `token`, `api_key`, `apikey`, `private_key`, `privatekey`, `access_key`, `accesskey`, `auth`, `credential`, `credentials`, `credit_card`, `card_number`, `cvv`, `ssn`.
4. Truncates values larger than `max_payload_bytes`.
5. Converts non-serialisable types to `[TypeName]`.

**`trace.input()` always works** regardless of the `capture_inputs` setting.

---

## Sinks

### JsonlSink

Appends one JSON object per line to a file. Creates parent directories automatically.

```python
from traceact import JsonlSink

JsonlSink("data/traces/traces.jsonl")
JsonlSink("/absolute/path/to/traces.jsonl")
```

### ConsoleSink

Prints traces to stdout.

```python
from traceact import ConsoleSink

ConsoleSink(pretty=True)   # indented JSON (default)
ConsoleSink(pretty=False)  # compact single-line JSON
```

### Multiple sinks

```python
configure(sinks=[
    JsonlSink("data/traces/traces.jsonl"),
    ConsoleSink(pretty=True),
])
```

### Fallback

If no sinks are configured when a trace finishes, TraceAct falls back to `ConsoleSink()` so traces aren't silently dropped.

---

## Budget configuration

`TraceBudget` controls how much TraceAct records. When a limit is reached, `budget_hit` is set to `True` on the trace and recording stops. The wrapped function continues running normally.

```python
from traceact import TraceBudget

TraceBudget(
    max_events=100,          # stop recording events after this many
    max_steps=50,            # stop recording steps after this many
    max_depth=10,            # don't create child traces beyond this depth
    max_payload_bytes=8192,  # truncate field values larger than this
    sample_rate=1.0,         # 0.0–1.0: share of successful traces to record
    always_trace_errors=True # always record failed traces regardless of sample_rate
)
```

**Defaults:**

| Field | Default |
|---|---|
| `max_events` | 100 |
| `max_steps` | 50 |
| `max_depth` | 10 |
| `max_payload_bytes` | 8192 |
| `sample_rate` | 1.0 |
| `always_trace_errors` | True |

**Per-trace budget override** (merges with inherited values, doesn't replace the whole budget):

```python
@traced_action(
    action="agent.run",
    kind="app",
    budget=TraceBudget(max_events=500),  # only max_events is overridden
)
def run_agent():
    ...
```

### Production preset

The package default records every trace (`sample_rate=1.0`), which is right for development and first-run — you trace a function, run it once, and the trace is there. For high-volume production, `TraceBudget.production()` opts into a lighter footprint:

```python
from traceact import configure, TraceBudget

configure(budget=TraceBudget.production())
# equivalent to TraceBudget(sample_rate=0.1, always_trace_errors=True)
```

It records roughly 10% of successful traces while never dropping a failure. Only `sample_rate` and `always_trace_errors` are set; every other field inherits from the package default or a parent trace. Override a single field on top of the preset if needed:

```python
budget = TraceBudget.production()
budget.sample_rate = 0.25   # record 25% instead of 10%
```

---

## TraceConfig fields

```python
from traceact import TraceConfig

TraceConfig(
    enabled=True,              # False disables all tracing globally
    sink_mode="blocking",      # "blocking" | "buffered" | "disabled"
    strict=False,              # True: tracing failures raise exceptions
    redact_by_default=True,    # True: apply redaction to captured inputs/outputs
    capture_inputs=False,      # global default; False = global kill switch
    capture_outputs=True,      # whether trace.output() records anything
)
```

**Precedence (closest wins):**

```
Package defaults → package configure() → parent trace settings → local decorator override
```

---

## Test isolation

`configure()` mutates package-level state. Use `reset_config()` in teardown to prevent one test from affecting another.

```python
from traceact import reset_config

def teardown_function():
    reset_config()
```

`reset_config()` restores:
- `TraceConfig` to package defaults
- `TraceBudget` to package defaults
- The active trace ContextVar to `None`

It doesn't affect traces already written to a sink.

**Pattern for tests that need a specific config:**

```python
from traceact import configure, reset_config, TraceConfig, ConsoleSink

def test_create_note():
    configure(
        config=TraceConfig(enabled=True, sink_mode="blocking"),
        sinks=[ConsoleSink(pretty=False)],
    )
    try:
        result = create_note("Hello", "World")
        assert result["note_id"] is not None
    finally:
        reset_config()
```

---

## Trace record schema

The full JSON object written to the JSONL sink:

```json
{
  "trace_id": "trc_9f3a1c7b2d44",
  "root_trace_id": "trc_9f3a1c7b2d44",
  "parent_trace_id": null,
  "correlation_id": "corr_71ac4e19aaf0",
  "project": "my-app",
  "action": "note.create",
  "kind": "app",
  "actor": "user",
  "status": "completed",
  "budget_hit": false,
  "started_at": "2026-07-20T08:30:00.000Z",
  "ended_at": "2026-07-20T08:30:00.015Z",
  "duration_ms": 15.2,
  "inputs": {
    "title": "Hello"
  },
  "steps": [
    {
      "step_id": "stp_62e1aa49c103",
      "label": "Validated input",
      "recorded_at": "2026-07-20T08:30:00.002Z"
    }
  ],
  "events": [
    {
      "event_id": "evt_184c90aa22af",
      "parent_event_id": null,
      "kind": "db",
      "operation": "insert",
      "target": "notes",
      "status": "completed",
      "started_at": "2026-07-20T08:30:00.008Z",
      "ended_at": "2026-07-20T08:30:00.013Z",
      "duration_ms": 5.1,
      "result": {"rows": 1},
      "error": null,
      "depth": 1
    }
  ],
  "touches": [
    {"kind": "db_table", "target": "notes"}
  ],
  "outputs": {
    "note_id": "note_123"
  },
  "errors": [],
  "child_summaries": [],
  "meta": {}
}
```

**Status values:**

| Value | Meaning |
|---|---|
| `"pending"` | Created but not yet executing |
| `"running"` | Actively executing |
| `"completed"` | Finished successfully |
| `"failed"` | Ended with an unhandled exception |
| `"cancelled"` | Explicitly stopped before finishing |

**`budget_hit`** is a separate boolean field, not a status. A trace can be `"completed"` with `budget_hit: true`, meaning the function ran to completion but TraceAct stopped recording events partway through.

---

## Viewing traces

TraceAct ships with a local, dependency-free web viewer. Installing the package gives you both the SDK and the `traceact` command — there is no separate viewer package to install.

```bash
pip install traceact
traceact view data/traces/traces.jsonl
```

This opens a browser at `http://127.0.0.1:8765` showing a live trace log, a trace map, and an inspector. The viewer tails the source, so traces appear as your app writes them.

### Command

```bash
traceact view [SOURCE]     # open the viewer
traceact show [SOURCE]     # alias of view (identical)
```

`view` and `show` are interchangeable aliases of the same command.

`SOURCE` is optional and may be:

- a `.jsonl` file — `traceact view data/traces.jsonl`
- a folder of `.jsonl` files — `traceact view data/traces/` (merges every file inside, e.g. per-process shards or several apps' files)
- omitted — `traceact view` opens empty and prompts you to add a source

The viewer reads any line that parses as JSON and looks like a trace; malformed entries are skipped, so files being appended to concurrently are safe to read.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--port N` | `8765` | Port to serve on. Auto-increments if the port is taken. |
| `--host HOST` | `127.0.0.1` | Interface to bind. Localhost only by default. |
| `--no-browser` | off | Start the server without opening a browser tab. |
| `--new` | off | Force a new viewer instance even if one is already running. |

You can also run it as a module: `python -m traceact.viewer.cli view SOURCE`.

### Single-instance behaviour

Running `traceact view` a second time — even for a different source — reuses a viewer that is already running rather than spawning a second server. The new source is added to the running viewer and a browser tab is opened on it. This avoids accumulating background processes across repeated launches during a dev session.

The coordination mechanism is a state file at `~/.traceact/viewer.json` that records the host and port of the running viewer. On each launch, TraceAct probes that address with a health check before deciding whether to reuse or start fresh. A stale state file (crashed or stopped viewer) is ignored and a new server starts normally.

Pass `--new` to bypass this and force a second instance — useful when you want two viewers side-by-side with different sources.

### Adding sources in the viewer

The "Add source" modal supports three ways to load a source:

- **Choose file / Choose folder buttons** — opens a native macOS file or folder picker. The picker runs on the server side via AppleScript (`osascript`), so it returns the real filesystem path and the viewer can tail it live. (Falls back to a `tkinter` dialog on non-macOS platforms.)
- **Drag and drop** — drop a `.jsonl` file onto the drop zone. The browser reads the file contents and posts them to the server, which saves a copy to `~/.traceact/imports/` and adds it as a source. Because the server holds a copy (not the original), this is a **static snapshot** — new writes to the original file won't appear. The viewer labels imported sources accordingly.
- **Type a path** — a collapsible text input for pasting or typing an absolute path. This gives live tailing just like the command-line argument.

### macOS launcher (`launch.command`)

`launch.command` is a double-clickable shell script in the repo root. Opening it from Finder:

1. Probes port 8765 — if a viewer is already running, opens it immediately and exits.
2. Finds Python 3.9+ on the machine (checks pyenv shims, Homebrew, system Python).
3. Creates (or reuses) a `.venv/` virtual environment in the same directory.
4. Installs or upgrades `traceact` inside that venv.
5. Runs `traceact view`, waits for the server to be ready, then opens the browser.

The Terminal window stays open so Ctrl+C stops the viewer cleanly. To pass a source file at launch from a script: `open launch.command path/to/traces.jsonl`.

### What the viewer shows

- **Trace log** — a live, newest-first table of traces (time, action, status, duration, and touch/error/budget counts). A search box filters by action, kind, status, or touched target. The row count is capped (25 / 50 / 100 / 250, default 100) and paired with live tailing, so the newest traces are always in view.
- **Trace inspector** — selecting a trace shows its own ID, its parent and root trace IDs (when it is a child trace), kind, duration, and touch/error counts. "Copy JSON" copies the full record.
- **Trace map** — a visual of one trace: the action as origin, its events and resources as connected nodes, with per-node status and a red marker on failures. Plays as a sequential step-through replay, with a speed slider (1×–10×, live, persisted) and pause/play.
- **Settings** — accent colour, display density, default trace view, row count, and default replay speed — all persisted to `localStorage`.

### Viewer server API

These endpoints are available while a viewer is running. Apps and scripts can call them directly.

| Method | Path | Request | Response |
|---|---|---|---|
| `GET` | `/api/health` | — | `{"status":"ok","version":"0.2.1","sources":N}` |
| `GET` | `/api/sources` | — | `[{"name":"...","path":"..."}]` |
| `POST` | `/api/sources` | `{"path":"..."}` | `{"name":"...","path":"..."}` |
| `GET` | `/api/pick?type=file\|folder` | — | `{"path":"...","cancelled":bool}` |
| `POST` | `/api/import` | `{"name":"file.jsonl","content":"..."}` | `{"name":"...","path":"...","imported":true}` |
| `GET` | `/api/stream?source=NAME&limit=N` | — | SSE stream: `snapshot` then `append` events |

The SSE stream delivers one `snapshot` message (the last N traces as a JSON array) then `append` messages for each newly-written trace. A `": keepalive"` comment is sent every 0.5 s when nothing new arrives, to keep the connection alive through proxies.

### Notes

- The viewer is a **local, single-node development tool**. It reads files on the machine it runs on. Exposing one machine's traces to another over the network (`traceact serve`) is planned for a later version.
- The viewer server binds to localhost by default. Only pass `--host 0.0.0.0` if you understand that it exposes trace data (which may contain sensitive payloads) to your network.

---

## Integrating the viewer into your app

If your app has its own web UI, you can add a "traceact viewer" button that opens the viewer in a new tab — starting it automatically if it isn't already running.

### Backend route

Add a route that calls `launch_or_connect()`. It checks for a running viewer (via `~/.traceact/viewer.json` + health probe), adds your trace source to it, and returns the URL. If no viewer is running it spawns one as a background subprocess and waits up to 3 seconds for it to be ready.

**FastAPI** (`launch_or_connect` does blocking I/O, so run it in a thread pool):
```python
import asyncio
from traceact.viewer.instance import launch_or_connect

@router.get("/api/launch-viewer")
async def launch_viewer():
    loop = asyncio.get_event_loop()
    url = await loop.run_in_executor(None, launch_or_connect,
                                     "data/traces/traces.jsonl")
    return {"url": url}
```

**Flask:**
```python
from traceact.viewer.instance import launch_or_connect
from flask import jsonify

@app.get("/api/launch-viewer")
def launch_viewer():
    url = launch_or_connect(source="data/traces/traces.jsonl")
    return jsonify({"url": url})
```

`launch_or_connect` signature:

```python
launch_or_connect(
    source=None,      # path to .jsonl file or folder; added to running viewer if given
    host="127.0.0.1", # host to bind if starting a new viewer
    port=8765,        # port to try if starting a new viewer
    open_browser=False,
    timeout=3.0,      # seconds to wait for a freshly started server to be ready
) -> str              # returns the viewer URL, e.g. "http://127.0.0.1:8765/"
```

### Frontend button

The button should be a `<button>` (not an `<a href>`), so you can disable it and show a loading state while the backend starts the viewer:

```javascript
document.getElementById("btn-traceact-viewer").addEventListener("click", async () => {
    const btn = document.getElementById("btn-traceact-viewer");
    btn.disabled = true;
    btn.textContent = "opening…";
    try {
        const { url } = await fetch("/api/launch-viewer").then(r => r.json());
        window.open(url, "_blank", "noopener");
    } catch {
        window.open("http://127.0.0.1:8765/", "_blank", "noopener");
    } finally {
        btn.disabled = false;
        btn.textContent = "traceact viewer";
    }
});
```

The fallback `window.open` in the `catch` block handles the case where the backend route doesn't exist yet (e.g. during development), so the button always does something useful.

### Direct API access (no backend route)

If adding a backend route isn't practical, the frontend can probe the viewer directly:

```javascript
async function openViewer() {
    try {
        await fetch("http://127.0.0.1:8765/api/health", { mode: "no-cors" });
    } catch { /* not running — tell the user to start it */ }
    window.open("http://127.0.0.1:8765/", "_blank", "noopener");
}
```

Note: cross-origin `fetch` to the viewer will always be blocked by CORS unless the viewer adds an `Access-Control-Allow-Origin` header (it doesn't, by design). Use `mode: "no-cors"` only to warm up the connection; don't try to read the response. The backend-route approach above is cleaner because it doesn't depend on the client being on the same machine as the viewer.

---

## Quick reference

```python
from traceact import (
    ActionTrace,    # manual tracing context manager
    TraceConfig,    # behaviour settings
    TraceBudget,    # recording limits
    configure,      # set package-level config, budget, and sinks
    reset_config,   # restore defaults (use in test teardown)
    traced_action,  # decorator
    JsonlSink,      # write traces to a .jsonl file
    ConsoleSink,    # print traces to stdout
)
```
