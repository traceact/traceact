# tests/test_tool_tracking.py
#
# Tests for the "tool" event kind (trace.tool()) and for explicit parenting
# (ActionTrace.start(parent=...)) — the two core pieces the agent-framework
# adapters build on. No framework involved here; the LangChain-specific
# behaviour lives in test_integration_langchain.py.

import json

import pytest

from traceact import (
    ActionTrace,
    JsonlSink,
    TraceBudget,
    TraceConfig,
    configure,
    reset_config,
)
from traceact.context import get_active_trace


@pytest.fixture
def sink_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(project="tooltest",
              config=TraceConfig(sink_mode="blocking"),
              sinks=[JsonlSink(str(path))])
    yield path
    reset_config()


def _records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestToolHelper:
    def test_tool_records_a_tool_event(self, sink_file):
        with ActionTrace.start(action="agent.turn") as t:
            t.tool(operation="call", target="web_search", duration_ms=840)
        rec = _records(sink_file)[0]
        event = rec["events"][0]
        assert event["kind"] == "tool"
        assert event["operation"] == "call"
        assert event["target"] == "web_search"
        assert event["duration_ms"] == 840

    def test_tool_event_derives_a_tool_touch(self, sink_file):
        with ActionTrace.start(action="agent.turn") as t:
            t.tool(operation="call", target="python_repl")
        rec = _records(sink_file)[0]
        assert {"kind": "tool", "target": "python_repl"} in rec["touches"]

    def test_repeated_tool_calls_dedupe_to_one_touch(self, sink_file):
        with ActionTrace.start(action="agent.turn") as t:
            t.tool(operation="call", target="web_search")
            t.tool(operation="call", target="web_search")
        rec = _records(sink_file)[0]
        tool_touches = [x for x in rec["touches"] if x["kind"] == "tool"]
        assert len(tool_touches) == 1
        assert len([e for e in rec["events"] if e["kind"] == "tool"]) == 2


class TestExplicitParent:
    def test_explicit_parent_links_without_shared_context(self, sink_file):
        # The callback pattern: parent and child are created on unrelated
        # stacks; neither is ever entered, so the ContextVar never sees them.
        parent = ActionTrace.start(action="chain.run", parent=None)
        child = ActionTrace.start(action="model.call", kind="model",
                                  parent=parent)
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)

        records = _records(sink_file)
        child_rec = [r for r in records if r["action"] == "model.call"][0]
        parent_rec = [r for r in records if r["action"] == "chain.run"][0]
        assert child_rec["parent_trace_id"] == parent_rec["trace_id"]
        assert child_rec["root_trace_id"] == parent_rec["trace_id"]

    def test_explicit_parent_beats_ambient_context(self, sink_file):
        # If both exist, the argument wins: the ambient trace belongs to
        # whatever stack the callback happens to run on.
        outside = ActionTrace.start(action="outer.op", parent=None)
        with ActionTrace.start(action="ambient.op"):
            child = ActionTrace.start(action="child.op", parent=outside)
            child.__exit__(None, None, None)
        outside.__exit__(None, None, None)

        records = _records(sink_file)
        child_rec = [r for r in records if r["action"] == "child.op"][0]
        outer_rec = [r for r in records if r["action"] == "outer.op"][0]
        assert child_rec["parent_trace_id"] == outer_rec["trace_id"]

    def test_no_parent_argument_keeps_ambient_behaviour(self, sink_file):
        with ActionTrace.start(action="parent.op") as p:
            with ActionTrace.start(action="child.op"):
                pass
        records = _records(sink_file)
        child_rec = [r for r in records if r["action"] == "child.op"][0]
        assert child_rec["parent_trace_id"] == p.trace_id

    def test_suppressed_parent_suppresses_the_child(self, tmp_path):
        # A parent dropped by sampling hands back a stand-in; a child started
        # under that stand-in must not surface as an orphan root.
        path = tmp_path / "traces.jsonl"
        configure(project="tooltest",
                  config=TraceConfig(sink_mode="blocking"),
                  budget=TraceBudget(sample_rate=0.0,
                                     always_trace_errors=False),
                  sinks=[JsonlSink(str(path))])
        try:
            parent = ActionTrace.start(action="sampled.out")
            assert not isinstance(parent, ActionTrace)  # it was suppressed
            child = ActionTrace.start(action="child.op", parent=parent)
            child.__exit__(None, None, None)
            parent.__exit__(None, None, None)
            assert _records(path) == []
        finally:
            reset_config()

    def test_never_entered_traces_leave_context_untouched(self, sink_file):
        parent = ActionTrace.start(action="a", parent=None)
        child = ActionTrace.start(action="b", parent=parent)
        assert get_active_trace() is None
        child.__exit__(None, None, None)
        parent.__exit__(None, None, None)
        assert get_active_trace() is None

    def test_explicit_parent_failure_records_failed_status(self, sink_file):
        parent = ActionTrace.start(action="chain.run", parent=None)
        child = ActionTrace.start(action="tool.call", kind="tool",
                                  parent=parent)
        err = RuntimeError("tool broke")
        child.__exit__(type(err), err, None)
        parent.__exit__(None, None, None)

        records = _records(sink_file)
        child_rec = [r for r in records if r["action"] == "tool.call"][0]
        assert child_rec["status"] == "failed"
        assert child_rec["errors"][0]["type"] == "RuntimeError"
