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
  redaction.py    — SENSITIVE_PATTERNS baseline + REDACTION_PRESETS registry
  sinks.py        — JsonlSink (thread-safe, rotation via max_bytes), ConsoleSink,
                    AsyncSink (background-thread wrapper; public as of v0.4)
  helpers.py      — TraceHelpersMixin (trace.db, trace.http, trace.file, trace.model)
  ids.py          — ID generation (trc_, evt_, stp_, corr_ prefixes)
  propagation.py  — extract_trace_id, inject_headers, propagate context manager,
                    _INCOMING_TRACE_ID ContextVar for cross-service correlation
  middleware.py   — TraceActMiddleware (WSGI), TraceActASGIMiddleware (ASGI)

  viewer/
    cli.py        — `traceact view` / `traceact show` / `traceact doctor` CLI entry point
    server.py     — stdlib ThreadingHTTPServer; SPA + REST + SSE
    reader.py     — SourceReader: JSONL snapshot + live byte-offset tail,
                    with inode-based delete+recreate detection
    doctor.py     — run_checks(): shared health-check logic behind both
                    `traceact doctor` and GET /api/doctor (Settings > Run diagnostics)
    instance.py   — single-instance coordination (state file + HTTP probe);
                    launch_or_connect() for embedding in app backends
    static/
      index.html  — single-page app shell
      styles.css  — design system (dark theme, CSS custom properties)
      app.js      — all frontend logic (no framework, no build step)

tests/            — pytest suite (pip install -e ".[dev]" && pytest)
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
12. [Framework recipes](#framework-recipes)
13. [Background jobs and correlation IDs](#background-jobs-and-correlation-ids)
14. [Input capture](#input-capture)
15. [Sinks](#sinks)
16. [TraceLog](#tracelog)
17. [Budget configuration](#budget-configuration)
18. [TraceConfig fields](#traceconfig-fields)
19. [Test isolation](#test-isolation)
20. [Trace record schema](#trace-record-schema)
21. [Viewing traces](#viewing-traces)
22. [Integrating the viewer into your app](#integrating-the-viewer-into-your-app)
23. [Distributed propagation](#distributed-propagation)

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
    project="my-app",           # stamps every trace; viewer uses this as the source name
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

**`project`** is the recommended way to name your traces. It is stamped onto every trace produced by this process and used by the viewer to label the source. Traces written without a `project` emit a `UserWarning` at runtime. A per-trace `project=` argument on `@traced_action` or `ActionTrace.start()` overrides the package-level value for that trace only.

**Sink modes:**
- `"blocking"` *(default)* — write immediately when a trace finishes. Traces appear in the sink, and the viewer, the moment they complete.
- `"buffered"` — hold traces in memory, flush on exit or on explicit `flush_buffer()`. Opt in for hot paths where per-trace write latency is unwelcome. Two costs to know: nothing appears in the sink until a flush, and a hard crash loses whatever is still buffered (the exit flush only runs on normal interpreter shutdown).
- `"disabled"` — decorators stay in place but nothing is recorded or written.

If no sinks are configured, traces fall back to `ConsoleSink` — in both modes. A trace is never dropped just because setup is incomplete.

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
    capture_inputs=True,             # None (defer to package config) | False | True | ["field1", "field2"]
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

When `sample_rate < 1.0` and a trace is sampled out, a skip sentinel is pushed onto the ContextVar. All nested `@traced_action` calls inside that function are suppressed with it — a successful child trace can't appear in the sink without its parent.

One exception, on by default: with `always_trace_errors=True`, a failure inside a sampled-out trace still produces a record. Nothing was recording while the action ran, so the record carries the action's identity, true timing, and the error, but empty steps/events/inputs — marked `sampled_out: true` to explain the reduced detail. Each suppressed frame the exception passes through records its own failure, the same shape as an unsampled run. Set `always_trace_errors=False` to make suppression absolute.

---

## Framework recipes

`@traced_action` and `ActionTrace` work on any callable — there's no framework integration to install. The only decision per framework is *where* to call `configure()` and *which* functions to wrap. These are patterns, not new API surface.

### FastAPI

Call `configure()` once, at import time or in a startup event, then wrap your route handlers or (more usefully) the service functions they call:

```python
# app/tracing.py — imported once, before routes are registered
from traceact import configure, TraceConfig, JsonlSink

configure(
    config=TraceConfig(sink_mode="blocking"),
    sinks=[JsonlSink("data/traces/traces.jsonl")],
)
```

```python
# app/routers/notes.py
from fastapi import APIRouter
from traceact import traced_action

router = APIRouter()

@traced_action(action="note.create", kind="app", actor="user")
async def _create_note(title: str, body: str) -> dict:
    ...
    return {"note_id": "note_123"}

@router.post("/notes")
async def create_note(payload: dict):
    return await _create_note(payload["title"], payload["body"])
```

Wrapping the inner service function (not the route handler directly) keeps the trace's `action` name stable even if you later rename the route or move it behind a different path. `correlation_id` is a good place to pass FastAPI's request ID if you have request-ID middleware:

```python
@traced_action(action="note.create", kind="app", correlation_id=request.state.request_id)
```

### Django

Django's request/response cycle is a natural boundary for `correlation_id`, and views or service functions are the natural place for `@traced_action`:

```python
# myapp/apps.py — configure once when the app is ready
from django.apps import AppConfig
from traceact import configure, TraceConfig, JsonlSink

class MyAppConfig(AppConfig):
    name = "myapp"

    def ready(self):
        configure(
            config=TraceConfig(sink_mode="blocking"),
            sinks=[JsonlSink("data/traces/traces.jsonl")],
        )
```

```python
# myapp/services.py
from traceact import traced_action

@traced_action(action="order.process", kind="app", actor="user")
def process_order(order_id: int) -> None:
    ...
```

```python
# myapp/views.py
from django.http import JsonResponse
from myapp.services import process_order

def process_order_view(request, order_id):
    process_order(order_id)
    return JsonResponse({"status": "ok"})
```

If you want every request to carry a correlation ID automatically (so all traces from one request share it), set it from middleware using Python's `contextvars` and read it in the view before calling the traced function — TraceAct doesn't read Django's request object itself, so the ID has to be passed explicitly into `correlation_id=`.

---

## Background jobs and correlation IDs

`correlation_id` links traces that belong to the same logical unit of work — a request, a job, a batch run — even when they happen across different function calls or processes. TraceAct doesn't generate or propagate it automatically across a queue boundary; the queue's message *is* the propagation mechanism, so you pass the ID through it like any other job argument.

**Enqueuing** — generate (or reuse) a correlation ID before the job goes on the queue:

```python
import uuid
from myqueue import enqueue

def start_export(user_id: int):
    correlation_id = f"corr_{uuid.uuid4().hex[:12]}"
    enqueue("export_report", user_id=user_id, correlation_id=correlation_id)
    return correlation_id  # e.g. return to the client so it can poll status
```

**Celery:**

```python
from celery import shared_task
from traceact import traced_action

@shared_task(name="export_report")
@traced_action(action="report.export", kind="job", actor="worker")
def export_report(user_id: int, correlation_id: str = None):
    # correlation_id is picked up automatically by @traced_action's
    # correlation_id kwarg only if passed through explicitly:
    ...
```

`@traced_action`'s `correlation_id` parameter isn't populated from task kwargs automatically — pass it explicitly so the decorator sees it:

```python
@shared_task(name="export_report")
def export_report(user_id: int, correlation_id: str = None):
    _export_report(user_id, correlation_id=correlation_id)

@traced_action(action="report.export", kind="job", actor="worker")
def _export_report(user_id: int, correlation_id: str = None):
    ...
```

**RQ:**

```python
from rq import Queue
from traceact import traced_action

@traced_action(action="report.export", kind="job", actor="worker")
def export_report(user_id: int, correlation_id: str = None):
    ...

queue.enqueue(export_report, user_id=42, correlation_id="corr_abc123")
```

**Why this can't be automatic:** `contextvars.ContextVar` (which powers automatic parent/child linking within one process) does not cross a queue boundary — the worker that picks up the job runs in a different process with a fresh, empty context. The correlation ID has to travel as ordinary job data, the same way you'd pass any other argument the job needs.

Traces from the enqueue side and the worker side won't share a `parent_trace_id` (they're different processes, so there's no shared ContextVar to link them), but they will share `correlation_id`. The viewer's search box doesn't currently filter by `correlation_id` (it matches on action, kind, status, and touched targets); to pull together one job's traces today, use "Copy JSON" on a trace to get its `correlation_id`, then `grep` or `jq` the JSONL file for it: `jq 'select(.correlation_id == "corr_abc123")' data/traces/traces.jsonl`.

---

## Input capture

By default, `@traced_action` doesn't capture function arguments — `capture_inputs` defaults to `None`, which means "defer to the package-level setting from `configure()`, or no capture if that isn't set either."

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

**Capture all arguments (per decorator):**

```python
@traced_action(action="note.create", kind="app", capture_inputs=True)
def create_note(title, body, user_id):
    ...
```

**Capture all arguments (package-wide default):**

```python
configure(config=TraceConfig(capture_inputs=True))

@traced_action(action="note.create", kind="app")  # no capture_inputs needed here
def create_note(title, body, user_id):
    ...
```

`capture_inputs=` on the decorator is shorthand for `config=TraceConfig(capture_inputs=...)` — both resolve through the same package-default → `configure()` → decorator-override chain, so a package-level default set via `configure()` is honoured by any decorator that doesn't explicitly override it.

**Global kill switch** (cannot be re-enabled by any decorator, not even one that explicitly passes `capture_inputs=True`):

```python
configure(config=TraceConfig(capture_inputs=False))
# Now capture_inputs=True on any @traced_action is ignored.
```

**What TraceAct does when capture is enabled:**
1. Maps positional args to parameter names using `inspect.signature()`.
2. Skips `self` and `cls`.
3. Redacts arguments whose names match sensitive patterns: `password`, `passwd`, `pwd`, `secret`, `token`, `api_key`, `apikey`, `private_key`, `privatekey`, `access_key`, `accesskey`, `auth`, `credential`, `credentials`, `credit_card`, `card_number`, `cvv`, `ssn`.
4. Recurses into nested dicts and lists (including lists inside lists), so a sensitive field buried inside a request body or config object is also redacted — not just top-level keys (see below).
5. Truncates values larger than `max_payload_bytes` to `[truncated: N chars]`.
6. Converts non-serialisable types to `[TypeName]` — including objects whose own `__str__` raises.
7. Replaces self-referencing structures with `[circular reference]` and cuts branches nested more than 100 levels deep with `[nested too deep]`, so a hostile or accidental payload can never blow the recursion stack inside your function call.

These rules apply identically to `trace.input()`, `trace.output()`, and event `result` values. None of them can raise into your application: a payload the sanitiser can't represent degrades to a placeholder, never to an exception.

**`trace.input()` always works** regardless of the `capture_inputs` setting.

### Redaction presets

The pattern list above is always on. Layer additional field-name patterns on top of it with `redaction_presets`:

```python
from traceact import configure, TraceConfig, REDACTION_PRESETS

print(sorted(REDACTION_PRESETS))
# ['api_keys', 'env_vars', 'filesystem_paths', 'http']

configure(config=TraceConfig(redaction_presets=["filesystem_paths", "env_vars"]))
```

| Preset | Extra patterns |
|---|---|
| `"api_keys"` | `jwt`, `bearer`, `signing_key`, `encryption_key`, `hmac_key`, `master_key` |
| `"http"` | `cookie`, `set_cookie`, `session_id`, `csrf_token`, `x_forwarded_for`, `remote_addr`, `client_ip` |
| `"filesystem_paths"` | `path`, `filepath`, `file_path`, `dir`, `directory`, `workdir`, `cwd`, `home_dir`, `homedir` |
| `"env_vars"` | `env`, `environ`, `environment`, `envvar`, `env_var`, `dotenv` |

An unknown preset name raises `ValueError` immediately at `TraceConfig(...)` construction, not later at trace time.

Also settable per-decorator, same as any other `TraceConfig` field — but note that a decorator-level `redaction_presets` **replaces** the package-level list rather than adding to it, the same way `capture_inputs` does:

```python
@traced_action(action="report.export", kind="app",
               config=TraceConfig(redaction_presets=["api_keys"]))
def export_report(...):
    ...
```

**These are field-name patterns, not content scanning.** A value is redacted because of what its *key* is called, not what it contains. `trace.input({"path": "/Users/mo/secret"})` is redacted by the `filesystem_paths` preset; `trace.input({"location": "/Users/mo/secret"})` is not, because `"location"` doesn't match any active pattern. This mirrors the baseline mechanism (same substring, case-insensitive matching) — it's simple and has no false-positive risk from scanning arbitrary string content, at the cost of missing secrets stored under an unexpected field name.

**Nested redaction example:**

```python
trace.input({
    "request": {
        "headers": {"authorization": "Bearer abc123"},
        "body": {"user_id": 42},
    },
})
# stored as:
# {"request": {"headers": {"authorization": "[redacted]"}, "body": {"user_id": 42}}}
```

---

## Sinks

### JsonlSink

Appends one JSON object per line to a file. Creates parent directories automatically.

```python
from traceact import JsonlSink

JsonlSink("data/traces/traces.jsonl")
JsonlSink("/absolute/path/to/traces.jsonl")
```

**Rotation (`max_bytes`):** by default the file grows without limit. Pass `max_bytes` to cap the active file's size — once the next write would exceed it, the current file is renamed to `<stem>.<UTC timestamp><extension>` (e.g. `traces.20260726T120000000000Z.jsonl` — the extension stays last so rotated segments keep matching the `*.jsonl` pattern folder sources read) and a fresh file starts at `path`:

```python
JsonlSink("data/traces/traces.jsonl", max_bytes=50_000_000)  # cap ~50 MB per file
```

Rotation renames rather than deletes, so history isn't lost — it's just no longer at `path`. Point the viewer at the containing **folder** rather than the single file to see the active file plus every rotated segment merged together: `traceact view data/traces/`. TraceAct doesn't currently delete old rotated segments on a schedule; clean them up yourself (e.g. a cron job or a retention script) to keep disk usage down.

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

### AsyncSink

Wraps any other sink(s) and performs all writes on a background thread, keeping I/O completely off the application's hot path. The traced function drops the record into an in-memory queue and returns immediately; a single worker thread drains the queue at its own pace.

```python
from traceact import AsyncSink, JsonlSink

configure(sinks=[
    AsyncSink([JsonlSink("data/traces/traces.jsonl")])
])
```

**When to use it:** any time the inner sink is slow or remote — an HTTP collector, a database, or a high-latency filesystem. For local files on fast hardware, `JsonlSink` alone rarely needs the extra wrapping.

**Backpressure — and why drops are always observable:**

If your application produces traces faster than the worker can write them, the queue fills up. TraceAct will never crash or block your app to protect a trace record — but it will never drop records *silently* either. Every record dropped under a backpressure policy is counted in `AsyncSink.dropped`. Check it, log it, or expose it in a health endpoint:

```python
sink = AsyncSink([JsonlSink("traces.jsonl")])
configure(sinks=[sink])

# later, in a health check or periodic log:
if sink.dropped > 0:
    logger.warning("AsyncSink dropped %d trace records (queue full)", sink.dropped)
```

The whole point of TraceAct is X-ray vision for your code. A silent drop is blindness. The `dropped` counter means you can *choose* to ignore dropped records — but the choice is yours, not the library's.

Three policies for when the queue is full:

| Policy | Behaviour | Use when |
|---|---|---|
| `"drop_newest"` (default) | Discard the incoming record; count it | Older in-flight records are more diagnostically useful |
| `"drop_oldest"` | Evict the oldest queued record to make room | Recent traces matter more than historical ones |
| `"block"` | Stall the calling thread until there is space | Zero loss is required and brief latency is acceptable |

```python
AsyncSink([JsonlSink("traces.jsonl")], max_queue=50_000, on_full="drop_oldest")
```

**Graceful shutdown:** `AsyncSink` registers an `atexit` hook on first write. When the process exits normally, the worker flushes all buffered records before stopping — short-lived scripts don't need to call `close()` explicitly, but you can call it yourself at a known shutdown point to flush sooner.

**Fork safety:** `os.fork()` doesn't copy background threads into child processes. `AsyncSink` registers a post-fork handler to reset the worker in the child so it starts fresh on the next write.

**Wrapping multiple sinks:**

```python
AsyncSink([
    JsonlSink("data/traces/traces.jsonl"),
    ConsoleSink(pretty=False),
])
```

Both inner sinks receive every record on the background thread. A failing inner sink is caught and skipped so one bad sink can't kill the worker or lose records destined for the others.

### SqliteSink

Writes finished traces to a local SQLite database using stdlib `sqlite3` — no extra dependencies. Common fields (`action`, `kind`, `status`, `started_at`, `correlation_id`, etc.) are stored as indexed scalar columns for fast filtering; the full trace record is also stored as JSON in a `record` column so no detail is ever lost. The schema is created automatically on first write.

```python
from traceact import SqliteSink, configure

configure(sinks=[SqliteSink("data/traces.db")])
```

**Custom table name:**

```python
SqliteSink("data/traces.db", table="my_traces")
```

**Querying traces directly from the database:**

```python
import sqlite3, json

conn = sqlite3.connect("data/traces.db")

# All failures in the last hour:
rows = conn.execute("""
    SELECT action, duration_ms, record
    FROM traces
    WHERE status = 'failed'
      AND started_at > datetime('now', '-1 hour')
    ORDER BY started_at DESC
""").fetchall()

for action, ms, raw in rows:
    record = json.loads(raw)
    print(f"{action}  {ms}ms  errors={record.get('errors')}")
```

**Concurrent writes:** SQLite is opened in WAL mode so reads and writes can proceed concurrently. For high-concurrency workloads, wrap in `AsyncSink` so write latency stays off the application's hot path:

```python
from traceact import AsyncSink, SqliteSink, configure

configure(sinks=[AsyncSink([SqliteSink("data/traces.db")])])
```

**Write errors** are printed to stderr and never propagated to the caller — a database hiccup doesn't interrupt the traced function.

### HttpSink

POSTs each finished trace as a JSON body to an HTTP or HTTPS endpoint. Uses stdlib `urllib` only — zero extra dependencies.

```python
from traceact import HttpSink, AsyncSink, configure

configure(sinks=[
    AsyncSink([HttpSink("https://collector.example.com/traces")])
])
```

**Always wrap in `AsyncSink` for production use.** Each write makes a synchronous HTTP request; without `AsyncSink` that latency hits every traced function call on the return path.

**Custom headers** (API keys, auth tokens):

```python
HttpSink(
    "https://collector.example.com/traces",
    headers={"Authorization": "Bearer <your-token>"},
)
```

**Custom timeout** (default: 5 seconds):

```python
HttpSink("https://collector.example.com/traces", timeout=2.0)
```

**Observable failures:** network errors, timeouts, and non-2xx responses are counted in `HttpSink.failed` — never raised, never silently swallowed. Check it in a health endpoint or periodic log:

```python
sink = HttpSink("https://collector.example.com/traces")
configure(sinks=[AsyncSink([sink])])

# in a health check or periodic log:
if sink.failed > 0:
    logger.warning("HttpSink: %d trace deliveries failed", sink.failed)
```

### OtlpSink

Exports finished traces to any OTLP-compatible collector — Jaeger, Grafana Tempo, Honeycomb, Datadog agent, the OpenTelemetry Collector, and others. Uses OTLP/HTTP+JSON over stdlib `urllib`. Zero extra dependencies; no `opentelemetry-sdk` required.

```python
from traceact import OtlpSink, AsyncSink, configure

configure(sinks=[
    AsyncSink([OtlpSink("http://localhost:4318")])
])
```

Point `endpoint` at your collector's OTLP HTTP receiver base URL (the standard port is 4318). TraceAct appends `/v1/traces` automatically.

**Always wrap in `AsyncSink` for production use** — each write makes a synchronous HTTP request.

**SaaS collectors (Honeycomb, Datadog, etc.):**

```python
# Honeycomb
OtlpSink(
    "https://api.honeycomb.io",
    headers={"x-honeycomb-team": "<your-api-key>"},
)

# Datadog (OTLP agent receiver, default port 4318)
OtlpSink("http://localhost:4318")

# Grafana Cloud
OtlpSink(
    "https://<your-instance>.grafana.net/otlp",
    headers={"Authorization": "Basic <base64-encoded-credentials>"},
)
```

**Service name and resource attributes:**

```python
OtlpSink(
    "http://localhost:4318",
    resource_attributes={
        "service.name":    "orders-api",
        "deployment.env":  "production",
        "service.version": "2.1.0",
    },
)
```

These appear as resource-level attributes on every span exported by this sink. `service.name` defaults to `"traceact"` if you don't set it.

**How TraceAct records map to OTel spans:**

| TraceAct field | OTel span field |
|---|---|
| `action` | Span name |
| `kind` (`db`, `http`, `cache`, `model`, `auth`, `payment`) | SpanKind CLIENT |
| `kind` (`queue`, `email`, `export`) | SpanKind PRODUCER |
| `kind` (`job`) | SpanKind CONSUMER |
| Everything else | SpanKind INTERNAL |
| `started_at` / `ended_at` | `startTimeUnixNano` / `endTimeUnixNano` |
| `status = "completed"` | StatusCode OK |
| `status = "failed"` | StatusCode ERROR |
| `parent_trace_id` | `parentSpanId` |
| `steps` | Span events (`name="step"`) |
| `errors` | Span events (`name="exception"`) |
| `inputs.*` | Span attributes `traceact.input.*` |
| `outputs.*` | Span attributes `traceact.output.*` |
| `touches` | Span attributes `traceact.touch.N.kind/target` |
| `trace_id`, `correlation_id`, `actor`, etc. | Span attributes `traceact.*` |
| Any unlisted scalar field | Span attribute `traceact.<field>` |

TraceAct IDs (`trc_...`) are hashed with MD5 to produce the 128-bit trace ID and 64-bit span ID that OTel requires. The original TraceAct IDs are always preserved as `traceact.trace_id` (and `traceact.root_trace_id`, `traceact.correlation_id`) span attributes so you can cross-reference them.

**Observable failures:** network errors, timeouts, and non-2xx responses from the collector are counted in `OtlpSink.failed`:

```python
sink = OtlpSink("http://localhost:4318")
configure(sinks=[AsyncSink([sink])])

if sink.failed > 0:
    logger.warning("OtlpSink: %d trace deliveries failed", sink.failed)
```

### Fallback

If no sinks are configured when a trace finishes, TraceAct falls back to `ConsoleSink()` so traces aren't silently dropped.

---

## TraceLog

`TraceLog` is the programmatic query interface for TraceAct JSONL files. Use it when code — an AI agent, a test suite, or a background script — needs to read trace data without opening a browser.

```python
from traceact import TraceLog

log = TraceLog("data/traces/traces.jsonl")   # file or folder
```

A folder source behaves the same as in the viewer: all `.jsonl` files inside it are merged on every read.

### Filtering

`filter()` returns a **new** `TraceLog` — the original is never mutated.

```python
failures  = log.filter(status="failed")
db_traces = log.filter(kind="db")

# AND logic: both conditions must hold
recent_db_failures = log.filter(kind="db", status="failed")

# Chained calls are equivalent
same_thing = log.filter(kind="db").filter(status="failed")
```

**Supported operators:**

| Syntax | Behaviour |
|---|---|
| `field=value` | Exact equality (case-sensitive) |
| `field__contains=value` | Case-insensitive substring |
| `field__startswith=value` | Case-insensitive prefix |
| `field__endswith=value` | Case-insensitive suffix |
| `field__re=pattern` | `re.search` — partial regex match |

```python
log.filter(action__contains="order")          # any action with "order"
log.filter(action__startswith="payment")      # actions starting with "payment"
log.filter(action__re=r"^order\.(create|update)$")
log.filter(correlation_id="job_abc123")       # find one background job's traces
```

### Terminal methods

```python
log.filter(status="failed").all()       # List[dict], oldest-first
log.filter(status="failed").last(10)    # 10 most recent
log.filter(status="failed").first(10)   # 10 oldest
log.filter(status="failed").count()     # int

log.filter(status="failed").render_table()      # pretty-print to stdout
log.filter(status="failed").render_table(n=25)  # cap rows shown
```

Each terminal call re-reads the JSONL file(s) — there is no caching, so you always get the current state of a live source.

`last()`/`first()` are memory-bounded: they hold at most `n` matching records per file at once rather than collecting every match before truncating. A broad filter (or no filter at all) over a large source costs memory proportional to `n`, not to how much of the source matches.

### query() — bounded result plus two completeness flags

```python
result = log.filter(status="failed").query(500)
result["traces"]         # up to 500 matches, newest-first
result["scan_capped"]    # True if max_lines_scanned stopped the scan early
result["limit_reached"]  # True if more than 500 traces matched
```

Two separate reasons a result might not be every match that exists:

- `scan_capped` — the scan itself gave up early, per `max_lines_scanned`.
- `limit_reached` — the scan finished (or found enough to stop), but `n` (or more) results matched — there may be more beyond what's returned. This is true of `last(n)` too; `last()` just has nowhere to report it, since it returns a plain list. Note that `len(result["traces"]) == n` alone can't tell you this — a bounded scan returns at most `n` regardless of whether `n` or a hundred thousand traces matched, so `limit_reached` is tracked from a count taken during the scan, not inferred from the result's length afterward.

Use `query()` instead of `last()` when the caller needs to distinguish those two situations — this is what the viewer's `/api/query` endpoint uses under the hood (see [Server-side search](#server-side-search-apiquery) above). `last()`/`first()` keep returning a plain list; `query()` is a separate method rather than a change to their return shape, so nothing about them breaks for existing callers.

### max_lines_scanned — bounding scan time

```python
log = TraceLog("data/traces.jsonl", max_lines_scanned=200_000)
```

Caps how many lines a scan reads (matched or not) before giving up and returning whatever was found, setting `scan_capped=True` on the next `query()` call. Defaults to `None` — unbounded, the same behaviour as before this existed. Set it when reading a source you don't control the size of, or when a single call needs a predictable worst-case cost (an HTTP handler, for instance — see below).

### Using TraceLog in tests

```python
import pytest
from traceact import TraceLog, configure, reset_config, JsonlSink

def test_checkout_records_payment_touch(tmp_path):
    sink_path = tmp_path / "traces.jsonl"
    configure(sinks=[JsonlSink(str(sink_path))])

    # run the function under test
    checkout(user_id="u_42", amount=99.00)

    log = TraceLog(str(sink_path))
    traces = log.filter(action="checkout").all()

    assert len(traces) == 1
    touch_kinds = [t["kind"] for t in traces[0].get("touches", [])]
    assert "payment" in touch_kinds

    reset_config()
```

### TraceLog.view() — shared lens

`view()` opens the viewer pre-filtered to match the `TraceLog`'s current filters. The viewer shows each active filter as a dismissable badge above the trace list — a human can remove any badge to widen the view, and the search box still works on top.

```python
# Open the viewer showing only failed traces
TraceLog("data/traces/traces.jsonl").filter(status="failed").view()

# Open with multiple filters
TraceLog("data/traces/traces.jsonl") \
    .filter(kind="db", status="failed") \
    .view()

# Get the URL without opening a browser (useful in CI or a script)
url = log.filter(status="failed").view(open_browser=False)
print(f"Open the viewer at: {url}")
```

The viewer is launched (or an existing instance is reused) and pointed at the same source file or folder that the `TraceLog` reads from. The viewer's normal behaviour when opened without `view()` is completely unchanged.

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

**TraceAct's own test suite** lives in `tests/` at the repo root and follows this exact pattern — a `_clean_config` autouse fixture in `tests/conftest.py` calls `reset_config()` before and after every test. Run it with:

```bash
pip install -e ".[dev]"
pytest
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
  "sampled_out": false,
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

**`sampled_out`** is `true` only on a failure record promoted from a sampled-out trace (`always_trace_errors`, on by default — see [Sampling and nested traces](#parent-and-child-traces)). Such a record has `status: "failed"` and the error, but empty `steps`/`events`/`inputs`, because nothing was recording while the action ran. It is `false` on every normally recorded trace.

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
| `--base-path PATH` | *(none)* | Mount the viewer at a subpath (e.g. `/audit-viewer`) for reverse-proxy deployments. |
| `--require-token` | off | Require a random token on every API request. See [Token auth](#token-auth). |

You can also run it as a module: `python -m traceact.viewer.cli view SOURCE`.

### Token auth

By default the viewer accepts any request from the local machine. That is fine on a single-user dev box: anything running as you can read the trace files directly anyway, so the viewer grants nothing extra. The one exposure is a **shared machine** — the server binds `127.0.0.1`, a different OS user can reach that port, and the server reads files with *your* permissions.

`--require-token` closes that:

```bash
traceact view data/traces.jsonl --require-token
```

```
TraceAct viewer running at http://127.0.0.1:8765/?source=agora&token=Xsklccm...
Token auth is on: API requests need the token from the URL above
(?token= or an X-TraceAct-Token header).
```

- Every `/api/*` request must carry the token — `X-TraceAct-Token` header for API clients, `?token=` query param for the browser (whose EventSource and download links can't set headers). Requests without it get `403`.
- The page shell and static assets stay open; they're the same bytes anyone gets from `pip install traceact`. All trace data flows through the gated API.
- The token is generated in-process and reaches clients through exactly two channels: the printed URL, and `~/.traceact/viewer.json` (written with mode `0600`). It is never accepted as a command-line value — a token in `traceact view --token abc123` would be readable by every user on the machine via the process list.
- Same-user tools need no wiring: `launch_or_connect()`, `traceact view` reuse, and `traceact doctor` read the token from the state file and authenticate automatically. Other OS users can't read that file, and that asymmetry is the entire mechanism.
- On single-instance reuse the running viewer's setting wins, same as `--base-path`: token auth is fixed when a server starts. Asking for a token while an untokened viewer is running prints a notice to stderr and reuses it as it stands.
- A browser page opened without the token shows "this viewer requires a token" rather than an empty trace log, and learns nothing — not even source names.

Each launch generates a fresh token; restarting the viewer invalidates old URLs. The token appears in the browser URL and therefore in browser history — the same trade Jupyter makes, acceptable because history is readable only by the same OS user the token exists to serve.

### Health checks (`traceact doctor`)

```bash
traceact doctor [SOURCE]
```

Runs a handful of local checks and prints a pass/fail report — useful when tracing "isn't working" and you want to rule out setup problems before debugging your own code:

- Python version meets the 3.9 minimum
- the `~/.traceact` state directory exists and is writable (single-instance coordination and drag-drop imports depend on this)
- whether a viewer is currently running (informational only — `doctor` doesn't require one)
- if `SOURCE` is given: that the path exists and its lines parse as valid trace records

```
$ traceact doctor data/traces/traces.jsonl
traceact doctor

  ✓  Python 3.11 (OK, 3.9+ required)
  ·  traceact 0.3.0
  ✓  State directory (/Users/you/.traceact) is writable
  ·  No viewer currently running (not required).
  ✓  data/traces/traces.jsonl: 42/42 line(s) look like valid traces across 1 file(s)

All checks passed.
```

Exits `0` if every check that can fail passed, `1` otherwise. A missing running viewer is never treated as a failure.

### Single-instance behaviour

Running `traceact view` a second time — even for a different source — reuses a viewer that is already running rather than spawning a second server. The new source is added to the running viewer and a browser tab is opened on it. This avoids accumulating background processes across repeated launches during a dev session.

The coordination mechanism is a state file at `~/.traceact/viewer.json` that records the host and port of the running viewer. On each launch, TraceAct probes that address with a health check before deciding whether to reuse or start fresh. A stale state file (crashed or stopped viewer) is ignored and a new server starts normally.

Pass `--new` to bypass this and force a second instance — useful when you want two viewers side-by-side with different sources.

### Adding sources in the viewer

A tab opened without `?source=` in its URL — a bare `traceact view`, or a browser pointed at a running viewer's address by hand — starts on this picker rather than auto-attaching to whichever source happens to be first. Attaching to a stream is always a deliberate click; launch paths that know their source (the CLI with a path, `launch_or_connect(source=...)`) pin it in the URL so they still open attached.

The "Add source" modal supports three ways to load a source:

- **Choose file / Choose folder buttons** — opens a native macOS file or folder picker. The picker runs on the server side via AppleScript (`osascript`), so it returns the filesystem path and the viewer can tail it live. (Falls back to a `tkinter` dialog on non-macOS platforms.)
- **Drag and drop** — drop a `.jsonl` file onto the drop zone. The browser reads the file contents and posts them to the server, which saves a copy to `~/.traceact/imports/` and adds it as a source. Because the server holds a copy (not the original), this is a **static snapshot** — new writes to the original file won't appear. The viewer labels imported sources accordingly.
- **Type a path** — a collapsible text input for pasting or typing an absolute path. This gives live tailing just like the command-line argument.

### macOS launcher (`launch.command`)

`launch.command` is a double-clickable shell script in the repo root. Opening it from Finder:

1. Probes port 8765 — if a viewer is already running, opens it immediately and exits.
2. Finds Python 3.9+ on the machine (checks pyenv shims, Homebrew, system Python).
3. Creates (or reuses) a `.venv/` virtual environment in the same directory.
4. Installs or upgrades `traceact` inside that venv.
5. Runs `traceact view`, waits for the server to be ready, then opens the browser.

The Terminal window stays open so Ctrl+C stops the viewer. To pass a source file at launch from a script: `open launch.command path/to/traces.jsonl`.

### What the viewer shows

- **Trace log** — a live, newest-first table of traces (time, action, status, duration, and touch/error/budget counts). A search box filters by action, kind, status, correlation ID, or touched target, against the currently tailed rows. The row count is capped (25 / 50 / 100 / 250, default 100) and paired with live tailing, so the newest traces are always in view. A pre-filtered view opened via `TraceLog.view()` instead searches the full source on disk — see [Server-side search](#server-side-search-apiquery) below.
- **Trace inspector** — selecting a trace shows its own ID, its parent and root trace IDs (when it is a child trace), correlation ID (when present, shown in full), kind, duration, and touch/error counts. "Copy JSON" copies the full record.
- **Trace map** — a visual of one trace: the action as origin, its events and resources as connected nodes, with per-node status and a red marker on failures. Plays as a sequential step-through replay, with a speed slider (1×–10×, live, persisted) and pause/play. The map zooms and pans: the mouse wheel zooms about the cursor, left-drag pans, and the `+` / `−` / `⟲` buttons zoom about the centre and reset to 1×. Zoom is clamped to 0.2×–5× and resets when you select a different trace.
- **Source export** — each source row in the source picker shows a `⤓` button on hover. Clicking it downloads the full source as a `.jsonl` file via `/api/export`. The download is a snapshot as of the moment the request is made; traces written after it are not included.
- **Settings** — accent colour, display density, default trace view, row count, default replay speed, and a **Run diagnostics** button — all persisted to `localStorage` except diagnostics, which runs fresh each time.

### Run diagnostics (Settings)

The Settings page has a "Run diagnostics" button that runs the exact same checks as `traceact doctor` on the command line, via `GET /api/doctor` — Python version, state directory writability, whether a viewer is running, and (if a source is loaded) whether its trace data looks valid. Results appear as a checklist with a short progress indicator, and each failing check shows a one-line explanation of what it means and what to do about it. Useful when something isn't showing up in the log and you want to rule out a setup problem without opening a terminal.

### Viewer server API

These endpoints are available while a viewer is running. Apps and scripts can call them directly.

| Method | Path | Request | Response |
|---|---|---|---|
| `GET` | `/api/health` | — | `{"status":"ok","version":"0.3.0","sources":N}` |
| `GET` | `/api/doctor?source=` | — | `{"ok":bool,"version":"...","checks":[{"label","status","message","hint"?}]}` |
| `GET` | `/api/sources` | — | `[{"name":"...","path":"..."}]` |
| `POST` | `/api/sources` | `{"path":"..."}` | `{"name":"...","path":"..."}` |
| `GET` | `/api/pick?type=file\|folder` | — | `{"path":"...","cancelled":bool}` |
| `POST` | `/api/import` | `{"name":"file.jsonl","content":"..."}` | `{"name":"...","path":"...","imported":true}` |
| `GET` | `/api/stream?source=NAME&limit=N` | — | SSE stream: `snapshot` then `append` events |
| `GET` | `/api/query?source=NAME&field[__op]=value&limit=N` | — | `{"traces":[...],"scan_capped":bool,"limit_reached":bool,"count":N}` |
| `GET` | `/api/export?source=NAME` | — | `.jsonl` file download (`application/x-ndjson`) |

`/api/export` returns all records for the named source as an NDJSON download. Sources addressed by registered name only — a path cannot be passed as `source`. Single-file sources are streamed byte-identical with a `Content-Length` header; folder sources merge segments chronologically (`Content-Length` omitted). Malformed lines are preserved verbatim; blank lines are the only thing stripped. Missing `source` param → 400; unknown name → 404; registered source whose file has since been deleted → 200 with an empty body.

When the viewer is mounted at a `base_path`, all endpoints above are served under that prefix (e.g. `/audit-viewer/api/export`). Requests at the unprefixed paths return 404.

When the viewer was started with `--require-token`, every endpoint above requires the token (`X-TraceAct-Token` header or `?token=` query param) and answers `403` without it. See [Token auth](#token-auth).

`/api/doctor`'s `status` is `"pass"`, `"fail"`, or `"info"`; `hint` is present only on `"fail"` checks. `ok` is `true` only if every `"pass"`/`"fail"` check passed — `"info"` checks (traceact's version, whether a viewer is running) never affect it.

The SSE stream delivers one `snapshot` message (the last N traces as a JSON array) then `append` messages for each newly-written trace. A `": keepalive"` comment is sent every 0.5 s when nothing new arrives, to keep the connection alive through proxies.

### Server-side search (`/api/query`)

The trace log's search box and the row-limit setting both operate on the live-tailed buffer — whatever the last N traces happen to be. That's fine for the search box (a quick, instant, client-side filter over what's currently in view), but a `TraceLog.view()` pre-filter is a precise, deliberately-specified query — it needs to find its match regardless of whether that match is still inside the tail window. `/api/query` answers filters against the whole source on disk, via `TraceLog`, and the viewer routes pre-filters through it automatically. You don't need to call it directly for that to happen.

```
GET /api/query?source=traces&status=failed&action__contains=order&limit=200
```

- Every query param except `source` and `limit` is a filter field, in the same `field` / `field__contains` / `field__startswith` / `field__endswith` form as `TraceLog.filter()`. Multiple params are ANDed, same as chaining `.filter()` calls. Because `source` and `limit` are reserved for the endpoint itself, trace fields with those two names can't be filtered over HTTP — use `TraceLog.filter()` directly for that.
- `__re` is not accepted here — it's rejected with `400`. `TraceLog.filter(field__re=...)` only makes sense when the pattern comes from trusted code; over HTTP it's arbitrary caller-supplied input, and a catastrophic-backtracking pattern could hang the request. Use `__re` directly against `TraceLog` in Python instead.
- **`limit` is hard-capped at 1000 server-side.** Requesting `limit=5000` against a source with 3000 matches returns only the newest 1000 — but not silently: `count` in the response is the count *returned*, not the count that matched, and `limit_reached` (below) tells you whether more may exist.
- The response carries two separate completeness flags, both `false` when the result is everything that matched:
  - **`scan_capped`** — `true` if the scan hit its internal line-read ceiling before finishing reading the source.
  - **`limit_reached`** — `true` if more traces matched than `limit` allowed back (including when a too-large requested `limit` was clamped to 1000). `count == limit` is not itself a safe signal that nothing more exists — a bounded scan always returns at most `limit` regardless of whether `limit` or a hundred thousand traces matched, so this comes from a count taken during the scan, not from inspecting the returned list's length afterward.

  Either flag `true` means the same thing to a caller: this may not be every match. The viewer shows "results may be incomplete" next to the pre-filter badges when either is set, rather than presenting a partial result as if it were exhaustive.

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
    name=None,        # label for this source in the picker; derived from the path if unset
    base_path="",     # subpath to mount under, e.g. "/audit-viewer" (default: root)
    require_token=False,  # start the viewer with token auth (see Token auth)
) -> str              # returns the viewer URL, e.g. "http://127.0.0.1:8765/?source=agora"
```

The returned URL carries `?source=<name>` so the tab opens attached to *this* app's source — without the param the viewer opens on its source picker rather than auto-attaching to whichever source is first (see [Token auth](#token-auth) and the 0.10.0 changelog entry for why). With `require_token=True` the URL also carries `?token=`; the token itself is generated by the spawned viewer and picked up from the state file, and all of this function's own API calls authenticate with it automatically. Like `base_path`, the flag only takes effect on the launch that actually spawns a server — a viewer already running is reused with whatever token setting it started with.

Pass `base_path` when the viewer sits behind a reverse proxy at a subpath instead of at the server root. The same value must be passed on every call for a given viewer instance — when reusing a running viewer, `launch_or_connect` reads the stored `base_path` from the state file and uses it for all subsequent API calls.

### How sources are named

The viewer labels each source in its picker. Left to itself it derives the name from the path, skipping components that describe storage rather than a project (`traces`, `data`, `logs`, `output`, `var`, `tmp`, and similar):

| Path | Name |
|---|---|
| `~/Dev/agora/data/traces/traces.jsonl` | `agora` |
| `~/Dev/casewright/logs/traces.jsonl` | `casewright` |
| `~/Dev/agora/agora_traces.jsonl` | `agora_traces` |
| `~/Dev/agora/data/traces/worker.jsonl` | `worker` |
| `~/Dev/agora/data/traces/traces.1234.jsonl` | `agora` |

A filename that already identifies the project is used as-is, so an app that writes `agora_traces.jsonl` keeps that name. Only a generic filename falls through to the directory walk. Per-process shards and rotated segments reduce to the same project name, so they don't fill the picker with near-identical entries.

Pass `name` when the app knows its own identity and shouldn't depend on where its files sit:

```python
launch_or_connect(source="data/traces/traces.jsonl", name="agora")
```

Sources are deduplicated by resolved path, so calling `launch_or_connect()` on every run reuses the existing entry rather than adding `agora-2`, `agora-3`, … Two different paths that derive the same name still get a numeric suffix to stay distinguishable.

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

### Admin-safe viewer pattern

**The viewer has no login of its own** — no accounts, no sessions, no credential storage. This is intentional: it's a local dev tool. What it does have is [opt-in token auth](#token-auth) (`--require-token`), which keeps other OS users on a shared machine out of the API. That is the full extent of its access control; anything beyond it — user accounts, roles, network exposure — is your application's responsibility.

**Rule of thumb:** never bind the viewer to a non-localhost host (`--host 0.0.0.0` or similar) unless you put your own authentication in front of it — the token gates other *local* users, not the open network, and it travels in URLs that plain HTTP shows to every hop. Binding `127.0.0.1` (the default) means only processes on the same machine can reach it; add `--require-token` on shared machines so "same machine" also means "same user".

If you want a "traceact viewer" button inside an internal team tool (like the FastAPI/Flask examples above), gate the *button and the route*, not the viewer itself:

```python
# The route that launches/connects to the viewer sits behind your app's
# own admin-only auth dependency — the viewer itself stays on localhost
# and is never exposed directly.
from fastapi import Depends
from traceact.viewer.instance import launch_or_connect

@router.get("/api/launch-viewer")
async def launch_viewer(user=Depends(require_admin)):
    loop = asyncio.get_event_loop()
    url = await loop.run_in_executor(None, launch_or_connect,
                                     "data/traces/traces.jsonl")
    return {"url": url}
```

Because `launch_or_connect` starts the viewer on `127.0.0.1` by default, the returned URL only works for someone on the same machine as the server process — appropriate for a local dev box, not for handing a link to a remote teammate. If your team needs to view traces from a shared/remote environment, don't punch the viewer through to the network; instead have each person run `traceact view` against a synced or shared copy of the trace file on their own machine, or wait for a future TraceAct release aimed at that use case (see Notes above — network-exposed viewing is explicitly out of scope for the current local-only design).

**Trace data itself may be sensitive.** Traces can contain request bodies, resource identifiers, and (if `capture_inputs` is misconfigured) unredacted arguments. Treat a running viewer, and the JSONL files it reads, with the same access discipline as application logs.

---

## Distributed propagation

When a traced action in Service A calls Service B, TraceAct links the two
services' traces together with two separate HTTP headers — separate because
they answer separate questions:

| Header | Sets on the receiving trace | Answers |
|---|---|---|
| `traceact-trace-id` | `upstream_trace_id` | "which trace in another service triggered me?" (causal lineage) |
| `traceact-correlation-id` | `correlation_id` | "which wider workflow do I belong to?" (business grouping, passed through untouched) |

Keeping them separate is deliberate: a trace can have an upstream parent in one
service *and* belong to a correlation group that was assigned several hops
earlier. Folding one into the other silently discards whichever one loses.

### Outbound: stamp both headers

`inject_headers()` reads the active trace and stamps its `trace_id` (always)
and its `correlation_id` (if set) onto an outbound headers dict. It never
mutates the dict you pass in.

```python
import requests
from traceact import inject_headers

with ActionTrace.start(action="order.submit", correlation_id="corr_wf_9f2a") as trace:
    headers = inject_headers({"Content-Type": "application/json"})
    requests.post("https://payments.internal/charge", json=payload, headers=headers)
```

Works the same with `httpx`, `urllib`, or any other HTTP client — `inject_headers`
just returns a plain dict.

### Inbound: extract the headers (manual)

Use the `propagate` context manager when you want explicit control, or when
your framework isn't covered by the automatic middleware. Pass the framework's
header object **directly** — `request.headers` works as-is on Flask, Django,
FastAPI, and Starlette, and so does a plain dict in any casing:

```python
from traceact import propagate, ActionTrace

def handle_charge(request):
    with propagate(request.headers):
        with ActionTrace.start(action="charge.process") as trace:
            # trace.upstream_trace_id == Service A's trace_id
            # trace.correlation_id   == whatever correlation Service A had
            ...
```

HTTP header names are case-insensitive and every framework reconstructs them
differently (Flask/Werkzeug and Django hand back `Traceact-Trace-Id`,
Title-Case; ASGI delivers raw lowercase bytes). `propagate()` and
`extract_trace_id()`/`extract_correlation_id()` normalise all of that
internally — pass the header object however your framework gives it to you,
including `dict(request.headers)` if you already have that.

### Inbound: automatic via middleware (Flask / Django)

```python
from traceact import TraceActMiddleware

# Flask
app.wsgi_app = TraceActMiddleware(app.wsgi_app)

# Django (in wsgi.py)
from django.core.wsgi import get_wsgi_application
application = TraceActMiddleware(get_wsgi_application())
```

Reads both headers from the WSGI environ and applies them to every trace
started during that request — including traces started while a streamed
response body is generated (the context is held until the response is fully
closed, not just until the view function returns). If neither header is
present, the middleware is fully transparent.

### Inbound: automatic via middleware (FastAPI / Starlette)

```python
from traceact import TraceActASGIMiddleware
from fastapi import FastAPI

app = FastAPI()
app.add_middleware(TraceActASGIMiddleware)
```

Or wrap manually:

```python
app = TraceActASGIMiddleware(app)
```

### How the chain looks in the viewer and TraceLog

Both services write their own trace records. Query by whichever link answers
your question — `upstream_trace_id` for "what did this specific call trigger",
`correlation_id` for "show me the whole workflow":

```python
from traceact import TraceLog

log = TraceLog("traces.jsonl")
log.filter(upstream_trace_id="trc_abc123").all()   # traces this call triggered
log.filter(correlation_id="corr_wf_9f2a").all()    # the entire workflow
log.filter(correlation_id="corr_wf_9f2a").view()   # same, in the browser viewer
```

The inspector's summary card shows both when present, each in full (not
shortened) so they can be copied and searched against another service's logs.

### `ai_prompts` redaction preset

For AI pipelines where trace payloads must not store raw prompt text, model
responses, or conversation history, enable the `ai_prompts` preset:

```python
configure(
    config=TraceConfig(redaction_presets=["ai_prompts"]),
)
```

Redacted fields include: `raw_prompt`, `prompt_content`, `system_prompt`,
`raw_response`, `response_content`, `conversation`, `message_content`,
`file_content`, `source_excerpt`, `context_window`, `completion`,
`generation`, `output_text`. Safe fields like `model`, `latency_ms`,
`prompt_id`, and token counts are unaffected.

| What gets redacted | What stays |
|---|---|
| Raw prompts and responses | Model name, version |
| System prompts | Token counts (as long as field name has no "token" substring) |
| Conversation history | Latency, cost |
| File and source excerpts | Trace IDs, correlation IDs |

Combine with other presets:

```python
TraceConfig(redaction_presets=["ai_prompts", "api_keys"])
```

### Reference: headers

| Header | Outbound (inject) | Inbound (extract) |
|---|---|---|
| `traceact-trace-id` | Active `ActionTrace.trace_id` | Sets `upstream_trace_id` |
| `traceact-correlation-id` | Active `ActionTrace.correlation_id`, if set | Sets `correlation_id` |

Accepted header collection types on the inbound side: any object with
`.items()` (dict in any casing, Werkzeug/Django/Starlette header objects,
`requests.CaseInsensitiveDict`), a list of `(name, value)` pairs, or raw ASGI
`[(b"name", b"value")]` byte pairs. Matching is case-insensitive regardless of
which form you pass.

WSGI: the header arrives as `HTTP_TRACEACT_TRACE_ID` in the environ.
ASGI: the header arrives as bytes in `scope["headers"]`.

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
    propagate,      # context manager for inbound propagation
    inject_headers, # stamp outbound request headers
    TraceActMiddleware,     # WSGI auto-propagation (Flask, Django)
    TraceActASGIMiddleware, # ASGI auto-propagation (FastAPI, Starlette)
)
```
