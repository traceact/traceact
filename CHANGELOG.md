# Changelog

All notable changes to TraceAct are documented here.

## [0.6.1] — 2026-07-27

### Fixed

- **`always_trace_errors` now holds under sampling.** The sampling decision is made before the function runs, so with `sample_rate < 1.0` a failure could previously be dropped even with `always_trace_errors=True`. A sampled-out frame now watches for an exception and, on one, writes a failure record after the fact: the action's identity, true start/end timing, and the error, with empty steps/events/inputs (nothing was recording while the action ran) and a new `sampled_out: true` field marking it. Each suppressed frame the exception passes through records its own failure, matching the one-record-per-traced-frame shape of an unsampled run. Successful sampled-out traces are still dropped, and `always_trace_errors=False` keeps suppression absolute. The success path keeps sampling's near-zero overhead. `OtlpSink` emits `traceact.sampled_out` (only when true), and the viewer inspector labels these records.
- **Sampling now suppresses nested traces under `ActionTrace.start()` too.** The skip sentinel was pushed only by the decorator, so a sampled-out `with ActionTrace.start(...)` block could let nested traces record as orphan roots. `_SkippedTrace` now pushes and pops the sentinel in its own `__enter__`/`__exit__`, so the context-manager and decorator APIs suppress nested traces identically.
- **Rotated JSONL segments are visible to folder sources.** Rotation now names segments `<stem>.<timestamp><extension>` (e.g. `traces.20260726T120000000000Z.jsonl`) so the `.jsonl` extension stays last and the folder-source globs in the viewer and `TraceLog` match them. Both readers also match `*.jsonl.*`, so segments written by earlier versions (which placed the timestamp after the extension) stay reachable.
- **Viewer: pre-filter badges escape URL-supplied values.** Field names and values from `?pf_*` params are now escaped before rendering, and the dismiss button uses a data attribute rather than an inline `onclick`, so a crafted link can't inject script through the pre-filter bar. `esc()` now also covers quote characters, closing attribute-context interpolations across the app.
- **Viewer: dismissing a pre-filter badge widens the results.** `state.traces` holds only rows matching every active filter, so dropping a filter now re-runs the server query with the remaining filters (or returns to the live-tail view when none remain) rather than re-filtering the already-narrowed set in memory, which could only narrow further.
- **Buffered sink mode is safe against concurrent writes.** `flush_buffer()` now snapshots and clears the shared buffer atomically under a lock, so a record appended while a flush is in progress is kept for the next flush rather than cleared before it's written.
- **`AsyncSink.write()` after `close()` no longer restarts the worker.** A closed sink now counts such writes in `dropped` instead of spawning a fresh worker and re-registering the atexit hook.
- **`trace.event()` keyword arguments can no longer overwrite core event fields** (`event_id`, `status`, `depth`, ...). Extra kwargs are merged with `setdefault`, so custom fields still attach but collisions with the event's own keys are ignored.

### Changed

- **WSGI middleware preserves `len()` on response bodies that support it.** The streaming-safe response wrapper now forwards `__len__` when the underlying body has it, keeping the one-chunk Content-Length hint PEP 3333 servers use.

## [0.6.0] — 2026-07-26

### Added

- **Viewer: server-side query endpoint (`GET /api/query`).** Fixes a gap in the pre-filter bridge shipped in 0.5.0: `TraceLog.view()`'s pre-filters (and the search box) previously only ever filtered the live-tailed buffer (25–250 most recent traces), so a precise pre-filter could find nothing simply because the matching trace had already scrolled out of that window — not because nothing matched. The viewer now runs pre-filters against the endpoint, which searches the full source via `TraceLog`, not just the tail buffer. The search box is unchanged for now (see USAGE.md for why the two aren't the same kind of query).
- **`TraceLog.query(n=500)`** — new method returning `{"traces": [...], "scan_capped": bool, "limit_reached": bool}`. Backs the viewer endpoint; also usable directly. `scan_capped` is `True` when `max_lines_scanned` (see below) was hit before the scan finished; `limit_reached` is `True` when more than `n` traces matched (there may be more beyond what's returned) — two separate reasons a result might not be every match, both surfaced so a partial result reads as a caveat rather than a silently incomplete answer. The viewer's endpoint applies the same signal to its own server-side `limit` clamp (requesting more than the endpoint's hard ceiling returns only up to that ceiling — `limit_reached` is what tells the caller more may exist).
- **`TraceLog(path, max_lines_scanned=None)`** — new optional constructor parameter capping how many lines a scan reads before stopping and returning what it found so far. Defaults to `None` (unbounded, current behaviour, unaffected for every existing caller). The viewer's endpoint sets a cap so a single request has a bounded worst-case cost regardless of source size.
- **`TraceLog.last()`/`.first()` are now memory-bounded internally.** Previously collected every matching trace before truncating to `n` — a broad filter (or no filter) over a large source held the full match set in memory even though only `n` results were ever returned. Now bounded to `n` records per file at once, provably correct (a trace in the global top-`n` must be in its own file's top-`n`) rather than an approximation. No change to either method's return value or signature.

## [0.5.0] — 2026-07-26

### Added

- **Distributed trace propagation** — link traces across service boundaries via two HTTP headers, kept deliberately separate: `traceact-trace-id` carries causal lineage (received as `upstream_trace_id`), `traceact-correlation-id` carries business-level workflow grouping (received as `correlation_id`, passed through untouched rather than overwritten).
  - **New field: `upstream_trace_id`** — the `trace_id` of the trace in a different service that triggered this one. Present in the trace record schema, `SqliteSink`'s scalar columns (auto-migrated on existing databases), and `OtlpSink`'s span attributes (`traceact.upstream_trace_id`).
  - **`inject_headers(headers=None)`** — stamps the active trace's ID, and its `correlation_id` when set, into an outbound headers dict. Falls back to forwarding the current incoming propagation context when there's no active trace, so an untraced hop doesn't break the chain. Returns a new dict; the original is not modified.
  - **`propagate(headers)`** — context manager for manual inbound propagation. Accepts the framework's header object directly (`request.headers` on Flask, Django, FastAPI, Starlette) or a plain dict in any casing — header name matching is case-insensitive regardless of input shape. Sets `upstream_trace_id` and `correlation_id` on all traces started inside the block; an explicit value passed to `ActionTrace.start()`/`@traced_action` always wins. Thread-safe and async-safe via `contextvars`.
  - **`extract_trace_id(headers)` / `extract_correlation_id(headers)`** — standalone header-parsing helpers, exported for callers who want the raw values without the context-manager form.
  - **`TraceActMiddleware`** — WSGI middleware for Flask and Django. Zero config beyond `app.wsgi_app = TraceActMiddleware(app.wsgi_app)`. Streaming-safe: holds the propagation context until the response iterable is closed (not just until the view function returns), so traces started while generating a streamed body still see it.
  - **`TraceActASGIMiddleware`** — ASGI middleware for FastAPI and Starlette. Add once: `app.add_middleware(TraceActASGIMiddleware)`. Passes through websocket and lifespan scopes unchanged.
  - Both middlewares set their ContextVars on every request unconditionally (including to `None` when no header is present), so a request that never triggers cleanup — a caller that skips the WSGI `close()` contract, for instance — can never leak its propagation context into a later request on a reused thread. `reset_config()` also clears both ContextVars, for test isolation.

- **`ai_prompts` redaction preset** — opt-in preset for AI/LLM pipelines where trace payloads must not store raw prompt text or model responses. Redacts: `raw_prompt`, `prompt_content`, `prompt_text`, `prompt`, `system_prompt`, `system_message`, `raw_response`, `response_content`, `response_text`, `conversation`, `message_content`, `messages`, `file_content`, `source_excerpt`, `context_window`, `completion`, `generation`, `output_text`. Enable with `TraceConfig(redaction_presets=["ai_prompts"])`.

- **Viewer: source paths shortened for safe screenshots.** A local file or folder path shown in the header or the add-source modal now displays only its last two path segments, prefixed with an ellipsis (e.g. `…/data/traces.jsonl`); a URL source is shown in full, since it carries no local directory information. Hovering the shortened path reveals a copy button that copies the full path to the clipboard — the display stays short, but the full value is always one click away. Falls back to `document.execCommand("copy")` when the async Clipboard API is unavailable.
- **Viewer: version badge.** The installed `traceact` version is now shown under the brand mark in the sidebar, read from `GET /api/health`.

## [0.4.0] — 2026-07-25

### Added

- **`SqliteSink`** — writes finished traces to a local SQLite database using stdlib `sqlite3`. The full trace record is stored as JSON in a `record` column so no detail is lost; common fields (`action`, `kind`, `status`, `started_at`, `correlation_id`, etc.) are also stored as indexed scalar columns for fast filtering. Schema is created automatically on first write; `INSERT OR REPLACE` handles duplicate `trace_id`s without error. WAL mode is enabled for concurrent read/write performance. Write errors print to stderr (observable, not silent) and never propagate to the caller. For high-concurrency workloads, wrap in `AsyncSink`.

- **`HttpSink`** — POSTs each finished trace as a JSON body to an HTTP/HTTPS endpoint, using stdlib `urllib` only. Failed deliveries (network error, timeout, non-2xx response) are counted in `HttpSink.failed` — observable by choice, never silently lost. Always wrap in `AsyncSink` for production use so HTTP latency stays off the application's hot path.

- **`OtlpSink`** — exports finished traces to any OTLP-compatible collector (Jaeger, Grafana Tempo, Honeycomb, Datadog agent, OpenTelemetry Collector) via OTLP/HTTP+JSON, using stdlib `urllib` only — zero additional dependencies. TraceAct IDs are hashed with MD5 to produce stable, deterministic 128-bit trace / 64-bit span hex IDs; the original IDs are always preserved as `traceact.trace_id` (and `root`, `correlation`) span attributes so the two systems stay cross-referenceable. Fields with a direct OTel equivalent (name, kind, timestamps, status, steps → events, errors → exception events) are mapped explicitly; every other TraceAct field is emitted as a `traceact.*` span attribute — nothing is silently lost when the schema grows. Failed deliveries counted in `OtlpSink.failed`. Always wrap in `AsyncSink` for production use.

- **`AsyncSink`** — exported from the top-level `traceact` package. Wraps any other sink(s) and performs all writes on a background thread, so the traced application never blocks on sink I/O. Designed for use with slow or remote inner sinks (e.g. a future `HttpSink`). Features: lazy worker start (no thread until first write), three backpressure policies (`drop_newest` / `drop_oldest` / `block`), a `.dropped` counter so loss under overload is observable rather than silent, graceful `close()` with `atexit` registration, and fork safety via `os.register_at_fork`. The implementation was already complete since v0.2; this release wires it into the public API and adds a full test suite.

- **`TraceLog`** — programmatic query interface for TraceAct JSONL files. Solves the "code needs to read traces" problem: an AI agent, test suite, or script can query trace files without opening a browser or parsing JSONL manually.
  - Accepts the same source types as the viewer: a `.jsonl` file, or a folder of them.
  - `filter(**kwargs)` adds predicates (AND logic); returns a new `TraceLog` — the original is unchanged.
  - Supported filter operators: exact equality (`status="failed"`), `__contains`, `__startswith`, `__endswith`, `__re` (regex search). All string operators are case-insensitive.
  - Terminal methods: `.all()` (oldest-first), `.last(n)` (n most recent), `.first(n)` (n oldest), `.count()`, `.render_table(n=None)` (stdout table for quick inspection).
  - **`TraceLog.view()`** — opens the human viewer (launching or reusing an existing instance) pre-filtered to match the `TraceLog`'s current filters via `?pf_*` URL params. The viewer shows the active filters as dismissable badges above the trace list; the human can remove any badge to widen the view, and the search box still works on top. Pass `open_browser=False` to get the URL without opening a browser. The viewer's normal behaviour when opened without `TraceLog.view()` is completely unchanged.
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
