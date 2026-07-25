# Changelog

All notable changes to TraceAct are documented here.

## [Unreleased]

### Added

- **`SqliteSink`** — writes finished traces to a local SQLite database using stdlib `sqlite3`. The full trace record is stored as JSON in a `record` column so no detail is lost; common fields (`action`, `kind`, `status`, `started_at`, `correlation_id`, etc.) are also stored as indexed scalar columns for fast filtering. Schema is created automatically on first write; `INSERT OR REPLACE` handles duplicate `trace_id`s cleanly. WAL mode is enabled for concurrent read/write performance. Write errors print to stderr (observable, not silent) and never propagate to the caller. For high-concurrency workloads, wrap in `AsyncSink`.

- **`HttpSink`** — POSTs each finished trace as a JSON body to an HTTP/HTTPS endpoint, using stdlib `urllib` only. Failed deliveries (network error, timeout, non-2xx response) are counted in `HttpSink.failed` — observable by choice, never silently lost. Always wrap in `AsyncSink` for production use so HTTP latency stays off the application's hot path.

- **`OtlpSink`** — exports finished traces to any OTLP-compatible collector (Jaeger, Grafana Tempo, Honeycomb, Datadog agent, OpenTelemetry Collector) via OTLP/HTTP+JSON, using stdlib `urllib` only — zero additional dependencies. TraceAct IDs are hashed with MD5 to produce stable, deterministic 128-bit trace / 64-bit span hex IDs; the original IDs are always preserved as `traceact.trace_id` (and `root`, `correlation`) span attributes so the two systems stay cross-referenceable. Fields with a direct OTel equivalent (name, kind, timestamps, status, steps → events, errors → exception events) are mapped explicitly; every other TraceAct field is emitted as a `traceact.*` span attribute — nothing is silently lost when the schema grows. Failed deliveries counted in `OtlpSink.failed`. Always wrap in `AsyncSink` for production use.

- **`AsyncSink`** — exported from the top-level `traceact` package. Wraps any other sink(s) and performs all writes on a background thread, so the traced application never blocks on sink I/O. Designed for use with slow or remote inner sinks (e.g. a future `HttpSink`). Features: lazy worker start (no thread until first write), three backpressure policies (`drop_newest` / `drop_oldest` / `block`), a `.dropped` counter so loss under overload is observable rather than silent, graceful `close()` with `atexit` registration, and fork safety via `os.register_at_fork`. The implementation was already complete since v0.2; this release wires it into the public API and adds a full test suite.

- **`TraceLog`** — programmatic query interface for TraceAct JSONL files. Solves the "code needs to read traces" problem: an AI agent, test suite, or script can query trace files without opening a browser or parsing JSONL manually.
  - Accepts the same source types as the viewer: a `.jsonl` file, or a folder of them.
  - `filter(**kwargs)` adds predicates (AND logic); returns a new `TraceLog` — the original is unchanged.
  - Supported filter operators: exact equality (`status="failed"`), `__contains`, `__startswith`, `__endswith`, `__re` (regex search). All string operators are case-insensitive.
  - Terminal methods: `.all()` (oldest-first), `.last(n)` (n most recent), `.first(n)` (n oldest), `.count()`, `.render_table(n=None)` (stdout table for quick inspection).
  - **`TraceLog.view()`** — opens the human viewer (launching or reusing an existing instance) pre-filtered to match the `TraceLog`'s current filters. The viewer shows the active filters as dismissable badges above the trace list; the human can remove any badge to widen the view, and the search box still works on top. The viewer's normal behaviour when opened without `TraceLog.view()` is completely unchanged.
  - No dependencies beyond the standard library (except `view()`, which uses the bundled viewer).
  - Exported from the top-level `traceact` package.

## [0.3.0] — 2026-07-25

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
