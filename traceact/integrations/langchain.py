# integrations/langchain.py
#
# TraceActCallbackHandler — a LangChain callback handler that records chain,
# LLM, and tool runs as TraceAct traces.
#
# Usage:
#
#     from traceact.integrations.langchain import TraceActCallbackHandler
#
#     handler = TraceActCallbackHandler()
#     chain.invoke(inputs, config={"callbacks": [handler]})
#
# Design constraints this module lives under:
#
# 1. Zero package-level dependencies. langchain-core is imported here and
#    only here; this module is loaded only when the user asks for it, so
#    `import traceact` stays dependency-free.
#
# 2. No use of the ambient trace context. LangChain delivers parentage as
#    data (run_id / parent_run_id) and may fire the start and end callbacks
#    of one run on different stacks or threads, so the with-block protocol —
#    which pins a trace to one stack via a ContextVar token — cannot apply.
#    Traces are created with an explicit parent (ActionTrace.start(parent=…))
#    and finished by calling __exit__ directly, which on a never-entered
#    trace finalises the record without touching the context stack.
#
# 3. Prompt and response text is NOT recorded by default. Model inputs are
#    the most sensitive payload an agent app handles; capture_content=True
#    opts in, and the captured text still flows through trace.input(), so
#    redaction (including the ai_prompts preset) applies to it.
#
# 4. Callbacks must never raise into the host application. Every handler
#    method swallows its own errors; a callback that cannot record simply
#    records nothing. (LangChain also guards handler exceptions, but relying
#    on the framework's tolerance would make its logging our failure mode.)

import threading
from typing import Any, Dict, Optional

try:
    from langchain_core.callbacks.base import BaseCallbackHandler
except ImportError as _e:  # pragma: no cover - exercised only without the dep
    raise ImportError(
        "TraceActCallbackHandler requires langchain-core. "
        "Install it with: pip install langchain-core"
    ) from _e

from traceact.ids import new_correlation_id
from traceact.trace import ActionTrace


def _name_of(serialized: Optional[Dict[str, Any]], fallback: str,
             kwargs: Optional[Dict[str, Any]] = None) -> str:
    """
    Best-effort run name. Where it lives moved across langchain-core
    versions: newer releases pass ``serialized=None`` and put the name in
    the ``name`` kwarg; older ones carry a "name" key or an "id" list whose
    last element is the class name inside ``serialized``. Check all three,
    fall back to the given default.
    """
    if kwargs is not None:
        name = kwargs.get("name")
        if isinstance(name, str) and name:
            return name
    if isinstance(serialized, dict):
        name = serialized.get("name")
        if isinstance(name, str) and name:
            return name
        ident = serialized.get("id")
        if isinstance(ident, list) and ident and isinstance(ident[-1], str):
            return ident[-1]
    return fallback


def _token_usage(response: Any) -> Dict[str, int]:
    """
    Pull token counts out of an LLMResult, wherever this provider put them.
    Returns {} when no usage is reported (fake models, some providers).
    """
    usage: Dict[str, int] = {}
    try:
        llm_output = getattr(response, "llm_output", None) or {}
        raw = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if raw.get("prompt_tokens") is not None:
            usage["tokens_in"] = int(raw["prompt_tokens"])
        if raw.get("completion_tokens") is not None:
            usage["tokens_out"] = int(raw["completion_tokens"])
        if usage:
            return usage
        # Newer chat models report usage_metadata on the generated message.
        for gens in getattr(response, "generations", []) or []:
            for gen in gens:
                message = getattr(gen, "message", None)
                meta = getattr(message, "usage_metadata", None) or {}
                if meta.get("input_tokens") is not None:
                    usage["tokens_in"] = int(meta["input_tokens"])
                if meta.get("output_tokens") is not None:
                    usage["tokens_out"] = int(meta["output_tokens"])
                if usage:
                    return usage
    except Exception:
        return {}
    return usage


def _model_name(serialized: Optional[Dict[str, Any]],
                kwargs: Dict[str, Any]) -> str:
    """The model identifier, from invocation params when present."""
    params = kwargs.get("invocation_params") or {}
    for key in ("model", "model_name", "model_id"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    return _name_of(serialized, "unknown-model", kwargs)


class TraceActCallbackHandler(BaseCallbackHandler):
    """
    Records LangChain runs as TraceAct traces.

    One handler instance can serve many concurrent runs: state is a
    run_id-keyed table guarded by a lock, and parentage comes from
    LangChain's parent_run_id rather than any thread-local state.

    What maps to what:

        chain / runnable run  →  trace kind="app",       action="chain.<name>"
        LLM / chat model run  →  trace kind="model",     action="model.<name>",
                                 plus a model event carrying token counts
        tool run              →  trace kind="tool",      action="tool.<name>",
                                 plus a tool event
        retriever run         →  trace kind="retrieval", action="retriever.<name>"
        agent action          →  a step on the enclosing run's trace

    Every trace in one top-level run shares a correlation_id (generated per
    root run unless a fixed one is passed in), so the whole run can be pulled
    together in the viewer or TraceLog even where parent links can't reach
    (e.g. across a process boundary you propagate it over).

    Args:
        project:
            Project name stamped on traces created by this handler. Defaults
            to the package-level configure(project=...) value.
        actor:
            The actor recorded on traces. Default "agent".
        correlation_id:
            Fix one correlation ID for every run this handler sees. Default:
            a fresh one per top-level run.
        capture_content:
            Record prompts, tool inputs, and response text via trace.input()/
            trace.output(). Off by default — model I/O is the most sensitive
            payload an agent app handles. When on, redaction still applies;
            pair with TraceConfig(redaction_presets=["ai_prompts"]) to keep
            captured structure while stripping prompt-shaped fields.
    """

    # LangChain flags: never re-raise handler errors into the app, and run
    # inline so parent_run_id ordering is deterministic.
    raise_error = False
    run_inline = True

    def __init__(
        self,
        project: Optional[str] = None,
        actor: str = "agent",
        correlation_id: Optional[str] = None,
        capture_content: bool = False,
    ) -> None:
        self._project = project
        self._actor = actor
        self._fixed_correlation_id = correlation_id
        self._capture_content = capture_content
        self._runs: Dict[Any, Any] = {}
        self._lock = threading.Lock()

    # -- run-table plumbing -------------------------------------------------

    def _begin(self, run_id: Any, parent_run_id: Any, action: str,
               kind: str) -> Any:
        """Create the trace for a run and file it under its run_id."""
        with self._lock:
            parent = self._runs.get(parent_run_id)
        if parent is not None:
            correlation = getattr(parent, "correlation_id", None)
        else:
            correlation = self._fixed_correlation_id or new_correlation_id()
        trace = ActionTrace.start(
            action=action,
            kind=kind,
            actor=self._actor,
            project=self._project,
            correlation_id=correlation,
            parent=parent,
        )
        with self._lock:
            self._runs[run_id] = trace
        return trace

    def _end(self, run_id: Any, error: Optional[BaseException] = None) -> None:
        """Finish and forget a run's trace. Unknown run_ids are ignored."""
        with self._lock:
            trace = self._runs.pop(run_id, None)
        if trace is None:
            return
        if error is not None:
            trace.__exit__(type(error), error, getattr(error, "__traceback__", None))
        else:
            trace.__exit__(None, None, None)

    def _get(self, run_id: Any) -> Any:
        with self._lock:
            return self._runs.get(run_id)

    # -- chains -------------------------------------------------------------

    def on_chain_start(self, serialized: Any, inputs: Any, *, run_id: Any,
                       parent_run_id: Any = None, **kwargs: Any) -> None:
        try:
            name = _name_of(serialized, "chain", kwargs)
            trace = self._begin(run_id, parent_run_id, f"chain.{name}", "app")
            if self._capture_content and isinstance(inputs, dict):
                trace.input(inputs)
        except Exception:
            pass

    def on_chain_end(self, outputs: Any, *, run_id: Any, **kwargs: Any) -> None:
        try:
            if self._capture_content and isinstance(outputs, dict):
                trace = self._get(run_id)
                if trace is not None:
                    trace.output(outputs)
            self._end(run_id)
        except Exception:
            pass

    def on_chain_error(self, error: BaseException, *, run_id: Any,
                       **kwargs: Any) -> None:
        try:
            self._end(run_id, error)
        except Exception:
            pass

    # -- LLMs and chat models ----------------------------------------------

    def on_llm_start(self, serialized: Any, prompts: Any, *, run_id: Any,
                     parent_run_id: Any = None, **kwargs: Any) -> None:
        try:
            model = _model_name(serialized, kwargs)
            trace = self._begin(run_id, parent_run_id, f"model.{model}", "model")
            if self._capture_content and prompts:
                trace.input({"prompts": list(prompts)})
        except Exception:
            pass

    def on_chat_model_start(self, serialized: Any, messages: Any, *,
                            run_id: Any, parent_run_id: Any = None,
                            **kwargs: Any) -> None:
        try:
            model = _model_name(serialized, kwargs)
            trace = self._begin(run_id, parent_run_id, f"model.{model}", "model")
            if self._capture_content and messages:
                trace.input({
                    "messages": [
                        [getattr(m, "content", "[message]") for m in batch]
                        for batch in messages
                    ],
                })
        except Exception:
            pass

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        try:
            trace = self._get(run_id)
            if trace is not None:
                usage = _token_usage(response)
                # The action name already carries the model; repeat it as the
                # event target so touches and OTLP output identify it too.
                model = getattr(trace, "action", "model.unknown")
                model = model.split(".", 1)[1] if "." in model else model
                trace.model(operation="completion", target=model, **usage)
                if self._capture_content:
                    texts = []
                    for gens in getattr(response, "generations", []) or []:
                        for gen in gens:
                            text = getattr(gen, "text", None)
                            if text:
                                texts.append(text)
                    if texts:
                        trace.output({"completions": texts})
            self._end(run_id)
        except Exception:
            pass

    def on_llm_error(self, error: BaseException, *, run_id: Any,
                     **kwargs: Any) -> None:
        try:
            self._end(run_id, error)
        except Exception:
            pass

    # -- tools --------------------------------------------------------------

    def on_tool_start(self, serialized: Any, input_str: Any, *, run_id: Any,
                      parent_run_id: Any = None, **kwargs: Any) -> None:
        try:
            name = _name_of(serialized, "tool", kwargs)
            trace = self._begin(run_id, parent_run_id, f"tool.{name}", "tool")
            trace.tool(operation="call", target=name)
            if self._capture_content and input_str is not None:
                trace.input({"input": input_str})
        except Exception:
            pass

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        try:
            if self._capture_content:
                trace = self._get(run_id)
                if trace is not None:
                    trace.output({"output": output})
            self._end(run_id)
        except Exception:
            pass

    def on_tool_error(self, error: BaseException, *, run_id: Any,
                      **kwargs: Any) -> None:
        try:
            self._end(run_id, error)
        except Exception:
            pass

    # -- retrievers ---------------------------------------------------------

    def on_retriever_start(self, serialized: Any, query: Any, *, run_id: Any,
                           parent_run_id: Any = None, **kwargs: Any) -> None:
        try:
            # kind="retrieval", not "db": LangChain's retriever abstraction
            # covers vector stores, web search, and file search alike, and
            # the callback can't see which is behind it. The kind names the
            # operation (fetching context); the target carries the retriever
            # class name, which is what identifies the backend.
            name = _name_of(serialized, "retriever", kwargs)
            trace = self._begin(run_id, parent_run_id,
                                f"retriever.{name}", "retrieval")
            trace.event(kind="retrieval", operation="retrieve", target=name)
            if self._capture_content and query is not None:
                trace.input({"query": query})
        except Exception:
            pass

    def on_retriever_end(self, documents: Any, *, run_id: Any,
                         **kwargs: Any) -> None:
        try:
            trace = self._get(run_id)
            if trace is not None:
                try:
                    trace.set_meta("documents_returned", len(documents))
                except Exception:
                    pass
            self._end(run_id)
        except Exception:
            pass

    def on_retriever_error(self, error: BaseException, *, run_id: Any,
                           **kwargs: Any) -> None:
        try:
            self._end(run_id, error)
        except Exception:
            pass

    # -- agent decisions ----------------------------------------------------

    def on_agent_action(self, action: Any, *, run_id: Any,
                        **kwargs: Any) -> None:
        try:
            trace = self._get(run_id)
            if trace is not None:
                tool = getattr(action, "tool", "unknown")
                trace.step(f"Agent chose tool: {tool}")
        except Exception:
            pass

    def on_agent_finish(self, finish: Any, *, run_id: Any,
                        **kwargs: Any) -> None:
        try:
            trace = self._get(run_id)
            if trace is not None:
                trace.step("Agent finished")
        except Exception:
            pass
