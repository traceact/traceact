# TraceAct — AI Agent Context

TraceAct is a Python package for action-level tracing. It records the full story of what happens when a function runs: every step, every resource touched, every event, every failure. The output is a JSONL file, one JSON object per trace.

## Package layout

```
traceact/
  __init__.py     — public exports
  trace.py        — ActionTrace class, core lifecycle, _NoOpTrace, _create_trace
  decorators.py   — @traced_action decorator (sync + async)
  config.py       — TraceConfig, configure(), reset_config()
  budget.py       — TraceBudget
  context.py      — ContextVar for active trace, SKIP sentinel
  sinks.py        — JsonlSink, ConsoleSink, buffer system
  helpers.py      — TraceHelpersMixin (trace.db, trace.http, trace.file, trace.model)
  ids.py          — ID generation (trc_, evt_, stp_, corr_ prefixes)
```

## The two APIs

**Decorator (preferred):**
```python
from traceact import traced_action

@traced_action(action="note.create", kind="app", actor="user")
def create_note(title, body):
    ...
```

**Manual context manager:**
```python
from traceact import ActionTrace

with ActionTrace.start(action="note.create", kind="app") as trace:
    trace.step("Validated input")
    trace.event(kind="db", operation="insert", target="notes")
    trace.output({"note_id": "note_123"})
```

## Minimal working setup

```python
from traceact import configure, TraceConfig, JsonlSink, traced_action

configure(
    config=TraceConfig(enabled=True, sink_mode="blocking"),
    sinks=[JsonlSink("data/traces/traces.jsonl")],
)

@traced_action(action="note.create", kind="app")
def create_note(title, body):
    ...
```

## Full reference

See `USAGE.md` for complete API documentation including all parameters, the trace record schema, budget configuration, input capture, parent/child traces, and test isolation.
