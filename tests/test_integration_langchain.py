# tests/test_integration_langchain.py
#
# Tests for traceact.integrations.langchain.TraceActCallbackHandler.
#
# These run against langchain-core's own callback dispatch — real fake
# models, real tools, real runnables — not hand-built callback invocations.
# The adapter's whole job is meeting LangChain's calling conventions
# (run_id/parent_run_id as UUID kwargs, serialized payload shapes, error
# routing), and a hand-rolled dict can't prove any of that. This is the same
# lesson the propagation tests learned with Flask/Django header objects.

import json

import pytest

lc = pytest.importorskip("langchain_core")

from langchain_core.language_models import FakeListLLM
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool as lc_tool

from traceact import JsonlSink, TraceConfig, configure, reset_config
from traceact.integrations.langchain import TraceActCallbackHandler


@pytest.fixture
def sink_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(project="agent-test",
              config=TraceConfig(sink_mode="blocking"),
              sinks=[JsonlSink(str(path))])
    yield path
    reset_config()


def _records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _by_action(records, prefix):
    return [r for r in records if r["action"].startswith(prefix)]


class TestLlmRuns:
    def test_llm_invoke_produces_a_model_trace(self, sink_file):
        llm = FakeListLLM(responses=["four"])
        handler = TraceActCallbackHandler()
        llm.invoke("What is 2 + 2?", config={"callbacks": [handler]})

        records = _records(sink_file)
        model_traces = _by_action(records, "model.")
        assert len(model_traces) == 1
        rec = model_traces[0]
        assert rec["kind"] == "model"
        assert rec["status"] == "completed"
        assert rec["actor"] == "agent"
        assert rec["project"] == "agent-test"

    def test_model_event_and_touch_recorded(self, sink_file):
        llm = FakeListLLM(responses=["ok"])
        llm.invoke("hi", config={"callbacks": [TraceActCallbackHandler()]})

        rec = _by_action(_records(sink_file), "model.")[0]
        assert any(e["kind"] == "model" for e in rec["events"])
        assert any(t["kind"] == "model" for t in rec["touches"])

    def test_prompt_text_not_recorded_by_default(self, sink_file):
        secret_prompt = "the launch code is 0000"
        llm = FakeListLLM(responses=["no comment"])
        llm.invoke(secret_prompt, config={"callbacks": [TraceActCallbackHandler()]})

        assert secret_prompt not in sink_file.read_text()

    def test_capture_content_records_prompts(self, sink_file):
        llm = FakeListLLM(responses=["hello"])
        handler = TraceActCallbackHandler(capture_content=True)
        llm.invoke("say hello", config={"callbacks": [handler]})

        rec = _by_action(_records(sink_file), "model.")[0]
        assert rec["inputs"]["prompts"] == ["say hello"]
        assert rec["outputs"]["completions"] == ["hello"]

    def test_captured_prompts_respect_ai_prompts_preset(self, tmp_path):
        # capture_content routes through trace.input(), so redaction presets
        # apply to it — capturing content and stripping prompt-shaped fields
        # can be combined.
        path = tmp_path / "traces.jsonl"
        configure(project="agent-test",
                  config=TraceConfig(sink_mode="blocking",
                                     redaction_presets=["ai_prompts"]),
                  sinks=[JsonlSink(str(path))])
        try:
            llm = FakeListLLM(responses=["x"])
            handler = TraceActCallbackHandler(capture_content=True)
            llm.invoke("super secret prompt", config={"callbacks": [handler]})
            rec = _by_action(_records(path), "model.")[0]
            assert rec["inputs"]["prompts"] == "[redacted]"
            assert "super secret prompt" not in path.read_text()
        finally:
            reset_config()


class TestToolRuns:
    def test_tool_invoke_produces_a_tool_trace(self, sink_file):
        @lc_tool
        def word_count(text: str) -> int:
            """Count words."""
            return len(text.split())

        word_count.invoke("one two three",
                          config={"callbacks": [TraceActCallbackHandler()]})

        records = _records(sink_file)
        tool_traces = _by_action(records, "tool.")
        assert len(tool_traces) == 1
        rec = tool_traces[0]
        assert rec["kind"] == "tool"
        assert rec["status"] == "completed"
        assert rec["action"] == "tool.word_count"
        assert any(e["kind"] == "tool" and e["target"] == "word_count"
                   for e in rec["events"])
        assert any(t["kind"] == "tool" and t["target"] == "word_count"
                   for t in rec["touches"])

    def test_failing_tool_records_a_failed_trace(self, sink_file):
        @lc_tool
        def broken(text: str) -> str:
            """Always fails."""
            raise ValueError("tool blew up")

        with pytest.raises(ValueError):
            broken.invoke("x", config={"callbacks": [TraceActCallbackHandler()]})

        rec = _by_action(_records(sink_file), "tool.")[0]
        assert rec["status"] == "failed"
        assert rec["errors"]
        assert rec["errors"][0]["type"] == "ValueError"


class TestRunTreeParentage:
    def test_llm_inside_chain_is_a_child_trace(self, sink_file):
        llm = FakeListLLM(responses=["answer"])

        def step(x, config):
            # Passing the runnable's config through is how LangChain
            # propagates the run tree; the adapter must turn that into
            # parent_trace_id without any shared call stack.
            return llm.invoke(x, config=config)

        chain = RunnableLambda(step)
        chain.invoke("question", config={"callbacks": [TraceActCallbackHandler()]})

        records = _records(sink_file)
        chain_rec = _by_action(records, "chain.")[0]
        model_rec = _by_action(records, "model.")[0]
        assert model_rec["parent_trace_id"] == chain_rec["trace_id"]
        assert model_rec["root_trace_id"] == chain_rec["trace_id"]

    def test_whole_run_shares_one_correlation_id(self, sink_file):
        llm = FakeListLLM(responses=["a"])

        def step(x, config):
            return llm.invoke(x, config=config)

        RunnableLambda(step).invoke(
            "q", config={"callbacks": [TraceActCallbackHandler()]})

        records = _records(sink_file)
        correlations = {r["correlation_id"] for r in records}
        assert len(correlations) == 1
        assert correlations != {None}

    def test_two_runs_get_two_correlation_ids(self, sink_file):
        handler = TraceActCallbackHandler()
        llm = FakeListLLM(responses=["a", "b"])
        llm.invoke("first", config={"callbacks": [handler]})
        llm.invoke("second", config={"callbacks": [handler]})

        records = _by_action(_records(sink_file), "model.")
        assert len(records) == 2
        assert records[0]["correlation_id"] != records[1]["correlation_id"]

    def test_fixed_correlation_id_is_used_when_given(self, sink_file):
        handler = TraceActCallbackHandler(correlation_id="corr_fixed_run")
        FakeListLLM(responses=["a"]).invoke("q", config={"callbacks": [handler]})

        rec = _by_action(_records(sink_file), "model.")[0]
        assert rec["correlation_id"] == "corr_fixed_run"

    def test_chain_completes_and_child_summary_rolls_up(self, sink_file):
        llm = FakeListLLM(responses=["done"])

        def step(x, config):
            return llm.invoke(x, config=config)

        RunnableLambda(step).invoke(
            "q", config={"callbacks": [TraceActCallbackHandler()]})

        chain_rec = _by_action(_records(sink_file), "chain.")[0]
        assert chain_rec["status"] == "completed"
        # The child model trace finished before the chain did, so its
        # summary reached the parent record.
        assert len(chain_rec["child_summaries"]) == 1


class TestRetrieverRuns:
    def test_retriever_produces_a_retrieval_trace(self, sink_file):
        # kind is "retrieval", not "db": the retriever abstraction covers
        # vector stores, web search, and file search alike, and the callback
        # can't see which backs it. The target carries the class name, which
        # is what actually identifies the backend.
        from langchain_core.documents import Document
        from langchain_core.retrievers import BaseRetriever

        class TinyRetriever(BaseRetriever):
            def _get_relevant_documents(self, query, *, run_manager):
                return [Document(page_content="alpha"),
                        Document(page_content="beta")]

        TinyRetriever().invoke(
            "find things", config={"callbacks": [TraceActCallbackHandler()]})

        records = _records(sink_file)
        rec = _by_action(records, "retriever.")[0]
        assert rec["kind"] == "retrieval"
        assert rec["action"] == "retriever.TinyRetriever"
        assert rec["status"] == "completed"
        assert any(e["kind"] == "retrieval" and e["target"] == "TinyRetriever"
                   for e in rec["events"])
        assert rec["meta"]["documents_returned"] == 2

    def test_retriever_query_not_recorded_by_default(self, sink_file):
        from langchain_core.documents import Document
        from langchain_core.retrievers import BaseRetriever

        class TinyRetriever(BaseRetriever):
            def _get_relevant_documents(self, query, *, run_manager):
                return [Document(page_content="x")]

        TinyRetriever().invoke(
            "confidential search terms",
            config={"callbacks": [TraceActCallbackHandler()]})
        assert "confidential search terms" not in sink_file.read_text()


class TestHandlerRobustness:
    def test_end_for_unknown_run_id_is_ignored(self):
        import uuid
        handler = TraceActCallbackHandler()
        handler.on_chain_end({}, run_id=uuid.uuid4())
        handler.on_llm_error(RuntimeError("x"), run_id=uuid.uuid4())
        handler.on_tool_end("out", run_id=uuid.uuid4())

    def test_handler_does_not_leak_ambient_context(self, sink_file):
        # The adapter must never push onto the trace ContextVar: a trace
        # started by a callback that leaked into the ambient context would
        # become the parent of unrelated application traces.
        from traceact.context import get_active_trace

        llm = FakeListLLM(responses=["ok"])
        llm.invoke("q", config={"callbacks": [TraceActCallbackHandler()]})
        assert get_active_trace() is None

    def test_disabled_tracing_records_nothing_and_breaks_nothing(self, tmp_path):
        path = tmp_path / "traces.jsonl"
        configure(project="agent-test",
                  config=TraceConfig(enabled=False),
                  sinks=[JsonlSink(str(path))])
        try:
            llm = FakeListLLM(responses=["ok"])
            result = llm.invoke("q", config={"callbacks": [TraceActCallbackHandler()]})
            assert result == "ok"
            assert _records(path) == []
        finally:
            reset_config()
