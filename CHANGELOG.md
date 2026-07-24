# Changelog

All notable changes to TraceAct are documented here.

## [Unreleased]

### Fixed

- **Package-level `configure(config=TraceConfig(capture_inputs=True))` was silently ignored.** `@traced_action`'s wrapper gated automatic input capture on its own local `capture_inputs` parameter (default `False`), never on the trace's fully resolved `_effective_config.capture_inputs` — so a package-wide "capture everything" default did nothing unless every single decorator also repeated `capture_inputs=True` itself. Fixed by changing the decorator's `capture_inputs` default to `None` and folding it into the `TraceConfig` override at decoration time, so it resolves through the same package-default → `configure()` → decorator-override chain (kill switch included) as every other setting. A decorator that already explicitly set `capture_inputs=True`/`False`/a field list is unaffected.

### Added

- **`traceact doctor [SOURCE]`** — runs local health checks (Python version, `~/.traceact` directory writability, whether a viewer is currently running, and — if `SOURCE` is given — whether the file or folder parses as valid trace data). Exits `0` if every checkable item passed, `1` otherwise; a missing running viewer is never a failure by itself.
- **`JsonlSink(path, max_bytes=...)`** — optional size-based rotation. Once the next write would exceed `max_bytes`, the active file is renamed to `<path>.<UTC timestamp>` and a fresh file starts at `path`. Rotation renames rather than deletes, so history is preserved; point the viewer at the containing folder to see the active file plus every rotated segment merged.
- **`TraceConfig(redaction_presets=[...])`** and the new `traceact.REDACTION_PRESETS` registry — opt-in field-name pattern groups (`"api_keys"`, `"http"`, `"filesystem_paths"`, `"env_vars"`) layered on top of the always-on baseline (`password`, `token`, `secret`, `api_key`, etc). Unknown preset names raise `ValueError` at `TraceConfig(...)` construction.
- **Recursive redaction** — `trace.input()`, `trace.output()`, and event `result` values are now sanitised recursively into nested dicts and lists-of-dicts, not just at the top level. A sensitive field buried inside a request body (e.g. `{"request": {"headers": {"authorization": "..."}}}`) is now redacted; previously only top-level keys were checked.
- **Viewer**: the log search box now matches `correlation_id` in addition to action, kind, status, and touch targets — useful for finding all traces belonging to one background job. The trace inspector's summary card also shows `correlation_id` (in full, unlike the shortened trace/parent/root IDs) when present.
- **Test suite** — a `tests/` directory (pytest + pytest-asyncio, both already declared as dev dependencies) covering `capture_inputs` resolution precedence, redaction (baseline, presets, recursion), `JsonlSink` rotation, and the viewer reader's delete+recreate detection. Run with `pip install -e ".[dev]" && pytest`.
- **Settings > Run diagnostics** — a button in the viewer's Settings page that runs the same checks as `traceact doctor` via a new `GET /api/doctor` endpoint, rendered as a staggered checklist with a progress indicator. Each failing check shows a one-line hint explaining what it means and what to do. The check logic itself moved into a shared `traceact/viewer/doctor.py` module so the CLI and the API return identical results, just rendered differently (text vs. JSON).
- **Documentation**: Django and FastAPI recipes for where to call `configure()` and wrap functions with `@traced_action`; a background-jobs guide for propagating `correlation_id` across a Celery/RQ queue boundary; an admin-safe viewer pattern documenting that the viewer has no built-in authentication and how to gate access to it behind your own app's auth; redaction presets and nested-redaction documentation.

## [0.2.1] — 2026-07-24

### Fixed

- **Viewer SSE tail stuck on "No traces yet" after a source file is deleted and recreated.** `SourceReader.poll()` tracked only a byte offset per file, so if a trace file was deleted and rewritten with more bytes than the old offset, the reader would seek into the new file at that stale offset and silently miss every trace before it — an already-open tab would see nothing new arrive, while a fresh tab (which calls `snapshot()` and reads from byte 0) worked fine. Fixed by also tracking each file's inode: a changed inode at the same path now triggers a full snapshot rebuild instead of an incremental append, and the SSE stream sends `{"kind": "snapshot"}` in that case so the client replaces its trace list instead of prepending onto stale data.

### Added

- **Viewer** — a local, dependency-free web UI shipped inside the package (`traceact view`). No separate install required.
  - Trace log: live-tailing table with search and configurable row limit.
  - Trace inspector: shows the selected trace's ID, parent/root IDs (child traces only), kind, duration, touch/error/budget counts, and a "Copy JSON" button.
  - Trace map: visual step-through replay of a trace; speed slider 1×–10× (persisted); pause/play.
  - Settings page: accent colour, display density, default trace view, row limit, default replay speed (all persisted to localStorage).
- **Single-instance coordination** — a second `traceact view` reuses a running viewer instead of spawning a new server. State is stored in `~/.traceact/viewer.json` and verified via a health probe. Pass `--new` to force a fresh instance.
- **`--new` flag** — bypass single-instance detection and start a second viewer.
- **`launch_or_connect()`** — utility for embedding viewer launch in app backends. Checks for a running viewer, adds a source, and returns the URL; spawns a subprocess if nothing is running. FastAPI example in USAGE.md.
- **Native OS file/folder picker** — `GET /api/pick?type=file|folder` opens an `osascript` dialog on macOS, falling back to `tkinter` elsewhere.
- **Drag-and-drop import** — `POST /api/import` receives file content, saves to `~/.traceact/imports/`, and registers as a source (static snapshot).
- **`GET /api/health`** — health endpoint for single-instance detection and integration polling.
- **`launch.command`** — macOS double-clickable launcher. Probes for a running instance, creates/reuses a `.venv/`, installs or upgrades `traceact`, and opens the browser.
- **`TraceBudget.production()`** — convenience preset: `sample_rate=0.1, always_trace_errors=True`.
- **Port auto-increment** — if the requested port is taken, tries up to 20 successive ports.
- **`traceact show`** — alias for `traceact view`.

### Changed

- `JsonlSink` now holds a `threading.Lock` to serialise concurrent same-process appends.
- The `traceact` CLI entry point is now declared in `[project.scripts]`, so `pip install traceact` gives you the `traceact` command without a separate install step.

### Internal

- `AsyncSink` is implemented in `sinks.py` but not yet exported; deferred to v0.3.
- Viewer static assets (HTML/CSS/JS) are shipped inside the wheel via `[tool.setuptools.package-data]`.

## [0.1.1] — 2026-06-01

Initial public release. Core tracing primitives: `ActionTrace`, `@traced_action`, `TraceConfig`, `TraceBudget`, `JsonlSink`, `ConsoleSink`.
