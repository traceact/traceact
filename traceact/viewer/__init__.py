# viewer/__init__.py
#
# The TraceAct viewer: a local, dependency-free web app for reading and
# visualising the JSONL files that the TraceAct SDK writes.
#
# The viewer is intentionally separate from the core tracing package. The core
# (trace.py, decorators.py, sinks.py, ...) writes traces; the viewer reads them.
# They share nothing but the JSONL file format. This keeps the SDK tiny and
# means the viewer can evolve independently.
#
# Nothing here is imported by the core package, so installing TraceAct and never
# running the viewer costs nothing at import time.
#
# Public entry point:
#   traceact.viewer.cli:main   — the `traceact view` / `traceact show` command.
