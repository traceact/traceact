# tests/test_queue_tracing.py
#
# Tests for queue / background job tracing:
#
#   - inject_context() — stamping trace context into a job payload on the
#     enqueue side (the queue-boundary counterpart of inject_headers()).
#   - The reserved traceact_context kwarg on @traced_action — the worker-side
#     half: the decorator consumes the kwarg (the function never sees it) and
#     links the job's trace to the enqueuing trace.
#   - trace.queue() — the helper for recording publish/consume events.
#
# The queue boundary is simulated the way a real one behaves: the payload is
# JSON round-tripped (queues serialise), and the "worker" runs after the
# producer's trace has closed and its context is gone — in a real deployment
# it's a different process with a fresh, empty ContextVar context.

import asyncio
import json

import pytest

from traceact import (
    ActionTrace,
    JsonlSink,
    TraceConfig,
    configure,
    inject_context,
    propagate,
    reset_config,
    traced_action,
)
from traceact.context import get_active_trace


@pytest.fixture
def sink_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(project="queuetest",
              config=TraceConfig(sink_mode="blocking"),
              sinks=[JsonlSink(str(path))])
    yield path
    reset_config()


def _records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _round_trip(payload):
    """Simulate the queue: serialise on enqueue, deserialise on consume."""
    return json.loads(json.dumps(payload))


class TestInjectContext:
    def test_stamps_active_trace_context(self, sink_file):
        with ActionTrace.start(action="export.start", kind="app",
                               correlation_id="corr_job1") as trace:
            payload = inject_context({"user_id": 42})
            assert payload["user_id"] == 42
            assert payload["traceact-trace-id"] == trace.trace_id
            assert payload["traceact-correlation-id"] == "corr_job1"

    def test_original_payload_not_modified(self, sink_file):
        original = {"user_id": 42}
        with ActionTrace.start(action="t", kind="app"):
            inject_context(original)
        assert original == {"user_id": 42}

    def test_no_trace_no_extra_keys(self, sink_file):
        payload = inject_context({"user_id": 42})
        assert payload == {"user_id": 42}

    def test_defaults_to_empty_dict(self, sink_file):
        with ActionTrace.start(action="t", kind="app") as trace:
            payload = inject_context()
            assert payload["traceact-trace-id"] == trace.trace_id

    def test_payload_is_json_safe(self, sink_file):
        with ActionTrace.start(action="t", kind="app",
                               correlation_id="corr_x"):
            payload = inject_context({"n": 1})
        assert _round_trip(payload) == payload

    def test_forwards_incoming_context_without_active_trace(self, sink_file):
        # An untraced hop (e.g. an HTTP handler that only enqueues) still
        # passes the chain along — same contract as inject_headers().
        with propagate({"traceact-trace-id": "trc_upstream",
                        "traceact-correlation-id": "corr_up"}):
            payload = inject_context({"user_id": 1})
        assert payload["traceact-trace-id"] == "trc_upstream"
        assert payload["traceact-correlation-id"] == "corr_up"


class TestWorkerSideDecorator:
    def test_links_job_trace_to_producer(self, sink_file):
        # Producer side.
        with ActionTrace.start(action="export.start", kind="app",
                               correlation_id="corr_e2e") as trace:
            producer_trace_id = trace.trace_id
            job = _round_trip(inject_context({"user_id": 42}))

        # Queue boundary: producer's trace is closed, its context gone.
        assert get_active_trace() is None

        # Worker side.
        @traced_action(action="export.run", kind="job", actor="worker")
        def export_report(user_id):
            return user_id * 2

        ctx = {k: v for k, v in job.items() if k.startswith("traceact-")}
        result = export_report(42, traceact_context=ctx)
        assert result == 84

        records = _records(sink_file)
        worker = [r for r in records if r["action"] == "export.run"][0]
        assert worker["upstream_trace_id"] == producer_trace_id
        assert worker["correlation_id"] == "corr_e2e"
        # Cross-process linkage is upstream, not parent — different processes
        # share no ContextVar, so there is no parent_trace_id.
        assert worker["parent_trace_id"] is None

    def test_function_never_sees_the_kwarg(self, sink_file):
        seen_kwargs = {}

        @traced_action(action="job.run", kind="job")
        def job(**kwargs):
            seen_kwargs.update(kwargs)

        job(a=1, traceact_context={"traceact-trace-id": "trc_x"})
        assert seen_kwargs == {"a": 1}

    def test_kwarg_not_captured_as_input(self, sink_file):
        @traced_action(action="job.run", kind="job", capture_inputs=True)
        def job(user_id):
            pass

        job(user_id=7, traceact_context={"traceact-trace-id": "trc_x"})
        record = _records(sink_file)[0]
        assert record["inputs"] == {"user_id": 7}
        assert "traceact_context" not in json.dumps(record["inputs"])

    def test_explicit_correlation_id_wins(self, sink_file):
        @traced_action(action="job.run", kind="job",
                       correlation_id="corr_explicit")
        def job():
            pass

        job(traceact_context={"traceact-trace-id": "trc_x",
                              "traceact-correlation-id": "corr_incoming"})
        record = _records(sink_file)[0]
        assert record["correlation_id"] == "corr_explicit"
        assert record["upstream_trace_id"] == "trc_x"

    def test_without_context_kwarg_behaviour_unchanged(self, sink_file):
        @traced_action(action="job.run", kind="job")
        def job(x):
            return x + 1

        assert job(1) == 2
        record = _records(sink_file)[0]
        assert record["upstream_trace_id"] is None

    def test_context_does_not_leak_past_the_call(self, sink_file):
        @traced_action(action="job.run", kind="job")
        def job():
            pass

        job(traceact_context={"traceact-trace-id": "trc_x"})

        # A trace started after the job must not inherit the job's context.
        with ActionTrace.start(action="after", kind="app"):
            pass
        after = [r for r in _records(sink_file) if r["action"] == "after"][0]
        assert after["upstream_trace_id"] is None

    def test_async_worker(self, sink_file):
        @traced_action(action="job.run.async", kind="job")
        async def job(x):
            return x * 3

        result = asyncio.run(job(3, traceact_context={
            "traceact-trace-id": "trc_async",
            "traceact-correlation-id": "corr_async",
        }))
        assert result == 9

        record = _records(sink_file)[0]
        assert record["upstream_trace_id"] == "trc_async"
        assert record["correlation_id"] == "corr_async"

    def test_disabled_tracing_still_consumes_kwarg(self, tmp_path):
        path = tmp_path / "traces.jsonl"
        configure(project="queuetest",
                  config=TraceConfig(enabled=False, sink_mode="blocking"),
                  sinks=[JsonlSink(str(path))])
        try:
            @traced_action(action="job.run", kind="job")
            def job(x):
                return x

            # The reserved kwarg must not reach the function even when no
            # trace is created.
            assert job(5, traceact_context={"traceact-trace-id": "t"}) == 5
            assert _records(path) == []
        finally:
            reset_config()

    def test_full_payload_dict_accepted_as_context(self, sink_file):
        # propagate()/the decorator only read the traceact-* keys, so the
        # whole job payload can be passed as the context without filtering.
        @traced_action(action="job.run", kind="job")
        def job():
            pass

        job(traceact_context={"user_id": 42,
                              "traceact-trace-id": "trc_whole"})
        record = _records(sink_file)[0]
        assert record["upstream_trace_id"] == "trc_whole"


class TestQueueHelper:
    def test_queue_event_and_touch(self, sink_file):
        with ActionTrace.start(action="export.start", kind="app") as trace:
            trace.queue(operation="publish", target="exports",
                        message_id="m_1")
        record = _records(sink_file)[0]
        evt = record["events"][0]
        assert evt["kind"] == "queue"
        assert evt["operation"] == "publish"
        assert evt["target"] == "exports"
        assert evt["message_id"] == "m_1"
        assert {"kind": "queue", "target": "exports"} in record["touches"]

    def test_noop_trace_absorbs_queue_and_tool(self, tmp_path):
        configure(project="queuetest",
                  config=TraceConfig(enabled=False),
                  sinks=[])
        try:
            with ActionTrace.start(action="t", kind="app") as trace:
                trace.queue(operation="publish", target="q")
                trace.tool(operation="call", target="t")
        finally:
            reset_config()
