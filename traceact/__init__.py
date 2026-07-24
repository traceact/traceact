# __init__.py
#
# Public API for the traceact package.
#
# Everything a developer needs to use TraceAct is exported from here. Importing
# from the submodules directly (e.g. from traceact.trace import ActionTrace) is
# supported but not the intended pattern — this file is the stable public surface.
#
# Public API (v1):
#
#   ActionTrace   — the live trace object. Use with ActionTrace.start() for manual
#                   tracing inside a with-block.
#
#   TraceConfig   — settings object. Pass to configure() or to @traced_action.
#
#   TraceBudget   — limits object. Pass to configure() or to @traced_action.
#
#   configure()   — set package-wide defaults for all future traces.
#
#   reset_config()— restore package defaults (use in test teardown).
#
#   traced_action — decorator. The primary way to add tracing to a function.
#
#   JsonlSink     — writes traces to a .jsonl file.
#
#   ConsoleSink   — prints traces to stdout.

__version__ = "0.2.1"

from traceact.config import configure, reset_config, TraceConfig
from traceact.budget import TraceBudget
from traceact.trace import ActionTrace
from traceact.decorators import traced_action
from traceact.sinks import JsonlSink, ConsoleSink

__all__ = [
    "__version__",
    "ActionTrace",
    "TraceConfig",
    "TraceBudget",
    "configure",
    "reset_config",
    "traced_action",
    "JsonlSink",
    "ConsoleSink",
]
