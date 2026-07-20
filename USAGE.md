# TraceAct Usage Reference

Full API documentation for TraceAct v0.1.0.

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
