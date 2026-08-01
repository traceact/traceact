# tests/test_event_inputs.py
#
# Tests for event-level input recording — the input= argument to trace.event()
# and the capture_event_inputs config flag that gates it.
#
# The contract under test:
#   - Off by default: input= passed to an event is dropped, the event's
#     "input" field stays None, and nothing else about the event changes.
#   - Opt-in via TraceConfig(capture_event_inputs=True) at package or
#     trace/decorator level records the value.
#   - Recorded inputs go through the same safety pipeline as trace.input():
#     field-name redaction for dicts, value-pattern scanning, payload caps.
#   - A package-level explicit False is a kill switch a decorator cannot
#     override — same rule as capture_inputs.
#   - Helper methods (trace.db() etc.) pass input= through to the event.

import json

import pytest

from traceact import (
    ActionTrace,
    JsonlSink,
    TraceBudget,
    TraceConfig,
    configure,
    reset_config,
    traced_action,
)


@pytest.fixture
def sink_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(project="eventinputs",
              config=TraceConfig(sink_mode="blocking"),
              sinks=[JsonlSink(str(path))])
    yield path
    reset_config()


@pytest.fixture
def capturing_sink_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(project="eventinputs",
              config=TraceConfig(sink_mode="blocking",
                                 capture_event_inputs=True),
              sinks=[JsonlSink(str(path))])
    yield path
    reset_config()


def _records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _only_event(path):
    records = _records(path)
    assert len(records) == 1
    events = records[0]["events"]
    assert len(events) == 1
    return events[0]


class TestOffByDefault:
    def test_input_is_dropped_without_opt_in(self, sink_file):
        with ActionTrace.start(action="t", kind="app") as trace:
            trace.event(kind="db", operation="select", target="users",
                        input={"user_id": 42})
        evt = _only_event(sink_file)
        assert evt["input"] is None

    def test_event_still_records_normally(self, sink_file):
        with ActionTrace.start(action="t", kind="app") as trace:
            trace.event(kind="db", operation="select", target="users",
                        input={"user_id": 42}, result={"rows": 1}, rows=1)
        evt = _only_event(sink_file)
        assert evt["operation"] == "select"
        assert evt["result"] == {"rows": 1}
        assert evt["rows"] == 1

    def test_input_field_present_on_events_without_input(self, sink_file):
        # Schema parity with "result": the key exists, None when not captured.
        with ActionTrace.start(action="t", kind="app") as trace:
            trace.event(kind="http", operation="get", target="api")
        evt = _only_event(sink_file)
        assert "input" in evt
        assert evt["input"] is None


class TestOptIn:
    def test_package_level_opt_in_records_input(self, capturing_sink_file):
        with ActionTrace.start(action="t", kind="app") as trace:
            trace.event(kind="db", operation="select", target="users",
                        input={"user_id": 42})
        evt = _only_event(capturing_sink_file)
        assert evt["input"] == {"user_id": 42}

    def test_trace_level_opt_in(self, sink_file):
        with ActionTrace.start(
            action="t", kind="app",
            config=TraceConfig(capture_event_inputs=True),
        ) as trace:
            trace.event(kind="http", operation="post", target="stripe",
                        input={"amount": 1200})
        evt = _only_event(sink_file)
        assert evt["input"] == {"amount": 1200}

    def test_decorator_level_opt_in(self, sink_file):
        @traced_action(action="t", kind="app",
                       config=TraceConfig(capture_event_inputs=True))
        def do_work():
            from traceact.context import get_active_trace
            get_active_trace().db("select", "users", input={"limit": 10})

        do_work()
        evt = _only_event(sink_file)
        assert evt["input"] == {"limit": 10}

    def test_non_dict_input_is_recorded(self, capturing_sink_file):
        with ActionTrace.start(action="t", kind="app") as trace:
            trace.event(kind="model", operation="completion", target="m",
                        input="summarise the report")
        evt = _only_event(capturing_sink_file)
        assert evt["input"] == "summarise the report"

    def test_helpers_pass_input_through(self, capturing_sink_file):
        with ActionTrace.start(action="t", kind="app") as trace:
            trace.tool(operation="call", target="web_search",
                       input={"query": "traceact"})
        evt = _only_event(capturing_sink_file)
        assert evt["input"] == {"query": "traceact"}


class TestKillSwitch:
    def test_package_false_beats_trace_level_true(self, tmp_path):
        path = tmp_path / "traces.jsonl"
        configure(project="eventinputs",
                  config=TraceConfig(sink_mode="blocking",
                                     capture_event_inputs=False),
                  sinks=[JsonlSink(str(path))])
        try:
            with ActionTrace.start(
                action="t", kind="app",
                config=TraceConfig(capture_event_inputs=True),
            ) as trace:
                trace.event(kind="db", operation="select", target="users",
                            input={"user_id": 42})
            evt = _only_event(path)
            assert evt["input"] is None
        finally:
            reset_config()

    def test_unset_package_default_can_be_overridden(self, sink_file):
        # The default is off, but a default (unlike an explicit False) can be
        # opted out of per trace — TestOptIn.test_trace_level_opt_in covers
        # the positive side; this pins that the two cases stay distinct.
        with ActionTrace.start(
            action="t", kind="app",
            config=TraceConfig(capture_event_inputs=True),
        ) as trace:
            trace.event(kind="db", operation="q", target="t", input="x")
        evt = _only_event(sink_file)
        assert evt["input"] == "x"


class TestSafetyPipeline:
    def test_field_name_redaction_applies(self, capturing_sink_file):
        with ActionTrace.start(action="t", kind="app") as trace:
            trace.event(kind="http", operation="post", target="api",
                        input={"user": "mo", "password": "hunter2"})
        evt = _only_event(capturing_sink_file)
        assert evt["input"]["user"] == "mo"
        assert evt["input"]["password"] == "[redacted]"
        assert "hunter2" not in json.dumps(evt)

    def test_value_pattern_scanning_applies(self, capturing_sink_file):
        key = "sk-" + "a" * 40
        with ActionTrace.start(action="t", kind="app") as trace:
            trace.event(kind="model", operation="completion", target="m",
                        input=f"use key {key} for this")
        evt = _only_event(capturing_sink_file)
        assert key not in json.dumps(evt)
        assert "[redacted:" in evt["input"]

    def test_payload_cap_applies(self, tmp_path):
        path = tmp_path / "traces.jsonl"
        configure(project="eventinputs",
                  config=TraceConfig(sink_mode="blocking",
                                     capture_event_inputs=True),
                  budget=TraceBudget(max_payload_bytes=64),
                  sinks=[JsonlSink(str(path))])
        try:
            with ActionTrace.start(action="t", kind="app") as trace:
                trace.event(kind="file", operation="write", target="f",
                            input={"body": "x" * 10_000})
            evt = _only_event(path)
            assert "x" * 10_000 not in json.dumps(evt)
        finally:
            reset_config()

    def test_hostile_input_does_not_crash(self, capturing_sink_file):
        loop = {}
        loop["self"] = loop
        with ActionTrace.start(action="t", kind="app") as trace:
            trace.event(kind="app", operation="op", target="t", input=loop)
        evt = _only_event(capturing_sink_file)
        # The record was written and the circular structure was defused.
        assert "circular" in json.dumps(evt["input"])
