# TraceAct

[![PyPI version](https://img.shields.io/pypi/v/traceact.svg)](https://pypi.org/project/traceact/)
[![Python versions](https://img.shields.io/pypi/pyversions/traceact.svg)](https://pypi.org/project/traceact/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

X-ray vision for Python code.

TraceAct is a lightweight Python package for action-level tracing. It records the full story of what happens when a function runs, including every step taken, resource touched, event recorded, and failure encountered, so you or your agent can understand what actually happened.

## Install

```bash
pip install traceact
```

Or from source:

```bash
pip install -e .
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

## Manual tracing

```python
from traceact import ActionTrace

with ActionTrace.start(action="note.create", kind="app") as trace:
    trace.input({"title": "Hello"})
    trace.step("Validated input")
    trace.event(kind="db", operation="insert", target="notes")
    trace.output({"note_id": "note_123"})
```

## Concepts

| Concept | Meaning |
|---|---|
| `Trace` | The full story of one action |
| `Step` | A human-readable timeline marker |
| `Event` | A structured operation (db, http, file, model, etc.) |
| `Touch` | A resource involved in the trace |
| `Sink` | Where the trace is written |

## Requirements

Python 3.9+. No runtime dependencies.

## License

MIT

---

Built by [Mo Shehu](https://mohammedshehu.com).
