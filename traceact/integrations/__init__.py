# integrations/__init__.py
#
# Optional adapters for third-party frameworks. Nothing here is imported by
# the top-level traceact package: TraceAct has zero runtime dependencies, and
# these modules keep it that way by importing their framework only when the
# user imports the adapter module explicitly. Each module raises a clear
# ImportError naming the missing package if the framework isn't installed.
#
# Available adapters:
#
#   traceact.integrations.langchain — TraceActCallbackHandler, a LangChain
#       callback handler that turns chain / LLM / tool runs into TraceAct
#       traces. Requires langchain-core.
