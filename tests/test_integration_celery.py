# tests/test_integration_celery.py
#
# Queue tracing exercised through REAL Celery dispatch — not a simulated
# boundary. The test broker is Celery's in-memory transport, but the message
# still takes the full production path: kombu JSON-serialises the payload,
# the task executes on Celery's testing worker in a separate thread, and a
# thread starts with its own empty ContextVar context. That means the
# producer's ambient trace context is genuinely unreachable from the task —
# if the worker's trace comes out linked, the linkage travelled as data
# through inject_context() / traceact_context, which is the claim under test.
#
# Same testing philosophy as test_integration_langchain.py: drive the real
# framework's own dispatch, not a hand-built imitation of it.

import json

import pytest

celery = pytest.importorskip("celery")

from celery import Celery
from celery.contrib.testing.worker import start_worker

from traceact import (
    ActionTrace,
    JsonlSink,
    TraceConfig,
    configure,
    inject_context,
    reset_config,
    traced_action,
)
from traceact.context import get_active_trace


@pytest.fixture
def sink_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(project="celerytest",
              config=TraceConfig(sink_mode="blocking"),
              sinks=[JsonlSink(str(path))])
    yield path
    reset_config()


@pytest.fixture
def celery_app():
    app = Celery(
        "celerytest",
        broker="memory://",
        backend="cache+memory://",
    )
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
    )
    return app


def _records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_celery_round_trip_links_worker_trace(sink_file, celery_app):
    @celery_app.task(name="export_report")
    @traced_action(action="report.export", kind="job", actor="worker")
    def export_report(user_id):
        trace = get_active_trace()
        trace.queue(operation="consume", target="exports")
        return user_id * 2

    with start_worker(celery_app, perform_ping_check=False):
        # Producer: trace the enqueue, stamp context into the task kwargs.
        with ActionTrace.start(action="export.request", kind="app",
                               correlation_id="corr_celery") as trace:
            producer_trace_id = trace.trace_id
            trace.queue(operation="publish", target="exports")
            result = export_report.apply_async(kwargs={
                "user_id": 21,
                "traceact_context": inject_context(),
            })

        # The task result proves the worker ran the real function body.
        assert result.get(timeout=10) == 42

    records = _records(sink_file)
    producer = [r for r in records if r["action"] == "export.request"][0]
    worker = [r for r in records if r["action"] == "report.export"][0]

    # Linked across the boundary — upstream lineage plus shared workflow ID.
    assert worker["upstream_trace_id"] == producer_trace_id
    assert worker["correlation_id"] == "corr_celery"
    # Cross-process linkage is upstream, not parent.
    assert worker["parent_trace_id"] is None
    # The reserved kwarg never reached the task function (it returned 42,
    # so it wasn't passed an unexpected argument) and never hit the record.
    assert "traceact_context" not in json.dumps(worker)
    # Both sides recorded their half of the queue event.
    assert [e["operation"] for e in producer["events"]] == ["publish"]
    assert [e["operation"] for e in worker["events"]] == ["consume"]


def test_celery_without_context_is_standalone(sink_file, celery_app):
    @celery_app.task(name="plain_job")
    @traced_action(action="job.plain", kind="job", actor="worker")
    def plain_job(x):
        return x + 1

    with start_worker(celery_app, perform_ping_check=False):
        result = plain_job.apply_async(kwargs={"x": 1})
        assert result.get(timeout=10) == 2

    worker = [r for r in _records(sink_file) if r["action"] == "job.plain"][0]
    assert worker["upstream_trace_id"] is None
    assert worker["correlation_id"] is None
