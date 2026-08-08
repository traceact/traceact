# Changelog

All notable changes to TraceAct are documented here.

## [0.14.0] — 2026-08-08

### Added

- **SQLite sources: the viewer and `TraceLog` read `SqliteSink` databases natively.** `traceact view data/traces.db` shows the sink's `traces` table and tails new rows live; `TraceLog("data/traces.db")` supports the full filter/query API with semantics identical to a JSONL source; `/api/export` downloads the table as NDJSON; `traceact doctor` validates the database and `--scan` audits its record column (findings reported by table row id). Details that make it safe and correct:
  - **Detection is by file content** (the SQLite magic header), not extension — `.db`, `.sqlite`, or anything else works, and a JSONL file named `.db` is read as JSONL.
  - **Connections are read-only with a 0.5s timeout**, so a viewer or query can never lock, block, or hang on the writing application's database. WAL mode (which the sink enables) means reads proceed even during an exclusive write transaction.
  - **The live tail cursors on the table's autoincrement id** — the exact analogue of a JSONL byte offset. Because the sink upserts by `trace_id`, an updated trace re-surfaces through the tail with a fresh id and replaces its row in the viewer; in-flight streaming stubs collapse in the database itself, and an orphaned stub (a crashed trace) surfaces as a `running` record. A recreated database (reset id sequence) triggers a full re-snapshot, the same response a JSONL source gives a changed inode.
  - Default `traces` table only; custom `table=` names can be added later if usage calls for it.

## [0.13.2] — 2026-08-03

### Added

- **Standard event kinds `"gate"` and `"qstate"`** for quantum circuit instrumentation (used by downstream tooling such as Qlens). A `gate` event is an operation applied to qubits; a `qstate` event is an observation recorded about them — two kinds because they feed different consumers (circuit rendering vs state visualisation) under opposite capture policies (batched vs sparse). Both derive `qubit` touches, so a trace's touch list answers "which qubits were involved" regardless of how; both export to OTLP as INTERNAL spans (simulation is in-process; a submission to remote hardware is the client library's own HTTP call). Heavy state payloads belong in a sidecar file referenced from the event, not in the record — the payload budget truncates values over `max_payload_bytes`.
- **`ARCHITECTURE.md`** — recording-pipeline and viewer diagrams, per-component contracts, and the ordering facts that constrain extensions (the sanitiser runs before sinks; final records supersede in-flight stubs; sink failures never raise).

## [0.13.1] — 2026-08-02

### Fixed

- **README links to USAGE.md were relative, which PyPI renders as dead links.** PyPI resolves relative markdown links against `pypi.org/project/traceact/`, not GitHub, so `[USAGE.md](USAGE.md)` became `pypi.org/project/traceact/USAGE.md` — a 404. Both occurrences now point to the absolute GitHub URL.

### Added

- **`USAGE.md` now ships inside the installed package, not just the sdist and GitHub.** Previously only `pip download --no-binary` (the sdist) carried the full docs locally; a normal `pip install traceact` (the wheel) did not. `USAGE.md`'s source of truth stays at the repo root; the release build copies it into `traceact/` right before packaging, so it's importable-adjacent — `import traceact, os; os.path.dirname(traceact.__file__)` — for air-gapped machines, containers with no egress, or anyone who'd rather not leave the terminal. The root file is the only copy anyone should ever hand-edit; the packaged copy is generated and gitignored.

## [0.13.0] — 2026-08-01

### Added

- **Event-level input recording, opt-in.** `trace.event(input=...)` — and the same argument on every helper (`trace.db()`, `trace.http()`, `trace.model()`, `trace.tool()`, ...) — records what went INTO a specific operation: the query parameters behind a DB call, the payload of an HTTP post. Gated by `TraceConfig(capture_event_inputs=True)` and off by default so event records stay lean; without the opt-in, `input=` is silently dropped, so call sites can pass it unconditionally and let config decide (the same contract as `trace.output()` under `capture_outputs=False`). Recorded inputs run the full safety pipeline: field-name redaction for dicts, value-pattern scanning, and the payload cap. A package-level explicit `False` is a kill switch no decorator can re-enable — the same rule as `capture_inputs`. The viewer's event inspector shows the captured input as an `in ·` line above the result. Every event now carries an `input` field (`null` when not captured), schema parity with `result`.

- **Queue / background job tracing.** A queue boundary breaks ambient context — the worker runs in a different process with a fresh, empty `ContextVar` context — so trace context now travels as job data, first-class:
  - `inject_context(payload)` stamps the active trace's ID and correlation ID into a job payload dict, the queue-boundary counterpart of `inject_headers()`. Same key names as the HTTP headers, plain-string values, JSON-serialiser-safe. No active trace forwards the incoming propagation context (an untraced enqueue hop still passes the chain along); no context at all returns the payload unchanged — safe to call unconditionally.
  - The reserved `traceact_context` kwarg on any `@traced_action` function is the worker-side half: the decorator consumes it (the function never sees the kwarg, input capture never records it) and links the job's trace to the enqueuing trace — `upstream_trace_id` for causal lineage, `correlation_id` for the workflow group, explicit `correlation_id=` on the decorator still winning. Works sync and async; the whole job payload can be passed unfiltered, since only the `traceact-*` keys are read. Celery and RQ integration is one line on each side.
  - `trace.queue(operation, target, ...)` — the helper for `kind="queue"` events. The producer records the publish, the worker records the consume; standard operations `publish`, `consume`, `ack`, `retry`, `reject`.

### Changed

- **Python 3.10 is now the minimum supported version.** `requires-python`, classifiers, docs, `traceact doctor`, and the macOS launcher all move from 3.9 to 3.10.

### Fixed

- **`trace.tool()` and `trace.queue()` no longer raise on a disabled trace.** `_NoOpTrace` — the stand-in returned when tracing is disabled, sampled out, or depth-capped — was missing the `tool` helper stub added in 0.12.0, so `trace.tool(...)` under `enabled=False` raised `AttributeError` instead of being silently absorbed. Both helpers now have no-op stubs.

### Development

- The dev extra now installs `httpx2` (Starlette's TestClient backend, replacing the deprecated httpx path) and `celery` — the queue-tracing tests drive real Celery dispatch through its in-memory broker and testing worker, so the job crosses a genuine boundary (kombu JSON-serialises the message; the worker thread starts with its own empty ContextVar context) without needing Redis or any external service.

## [0.12.0] — 2026-07-30

### Added

- **Tool-call tracking.** New standard event kind `"tool"` with a `trace.tool(operation, target, ...)` helper, a `tool` touch kind, and OTLP export as INTERNAL spans per OTel's GenAI conventions. The model call is the inference; the tool call is what the agent does between inferences — an agent turn typically records one model event and several tool events, and telling them apart is the point of tracing an agent.

- **Explicit parenting: `ActionTrace.start(parent=...)`.** Parent detection is normally ambient (the enclosing with-block or decorator). Callback-style frameworks break that premise: start and end fire on unrelated stacks and parentage arrives as data. The `parent=` argument accepts it as data. The argument wins over the ambient context; a suppressed parent (disabled, sampled out, depth-capped) suppresses its children, so sampling containment holds; `__exit__` on a never-entered trace finalises the record without touching the context stack, making the pattern thread-safe.

- **LangChain adapter — the first agent framework adapter.** `traceact.integrations.langchain.TraceActCallbackHandler` turns chain, LLM, tool, and retriever runs into traces with correct parent links (via `run_id`/`parent_run_id`) and one shared correlation ID per top-level run. Prompt and response text is not recorded by default; `capture_content=True` opts in and still flows through redaction. Zero-dependency positioning intact: `langchain-core` is imported only when the adapter module is, with a clear error naming the missing package. Tested through LangChain's own dispatch (fake models, real tools, real runnables), not hand-built callbacks — which also caught that langchain-core 1.x passes `serialized=None` and delivers run names as kwargs.

- **Value-pattern redaction, on by default.** A second redaction layer that scans captured string *content* for the wire formats of known credential types — AWS keys, `sk-`/`sk_` tokens (OpenAI, Anthropic, Stripe), GitHub and Slack tokens, JWTs, PEM private key blocks, Google API keys, `Bearer` values, and credentials embedded in URLs. Matches become named placeholders (`[redacted:aws-key]`) with surrounding prose intact, closing the hole field-name matching leaves: a key in a field named `location`, or pasted mid-sentence into free text. Only formats distinctive enough to make false positives unlikely are admitted, which is what makes default-on safe; the registry (`traceact.redaction.VALUE_PATTERNS`) is documented in USAGE.md and pinned pattern-by-pattern by the test suite. Disable with `TraceConfig(redact_values=False)`. The deeper recursion this required also closes a redaction hole: dicts nested in lists-of-lists are now redacted by field name too.

- **Capture transforms.** A list-form capture spec can name a transform per field: `capture_inputs=["amount", "user_id:hash", "card_number:last4"]`. `hash` stores a deterministic sha256 prefix (the same value hashes identically across traces and processes, so a hashed ID still correlates — pseudonymisation, not encryption), `last4` keeps the tail, `length` keeps size only. A transform overrides field-name redaction for its field — it is the caller's explicit handling instruction, and redacting a deliberate hash would destroy the correlation that justified it. Unknown transform names raise `ValueError` at construction or decoration time, never silently at capture time.

- **`traceact doctor --scan SOURCE`.** Runs the value-pattern registry over trace files already on disk and reports each finding with pattern, file, and line. Capture-time scanning protects records written from now on; this audits the past. Exits 0 when clean, 1 on any finding — a CI gate in one flag.

- **In-flight trace streaming, opt-in.** `TraceConfig(stream_progress=True | <seconds> | "full")`, resolvable per decorator. While a streaming trace is open it appends slim `"running"` stub lines (~300 bytes: identity plus step/event/error counts and the last step label, `in_flight: true`, monotonic `progress_seq`); the final record supersedes them. Write rules: a grace threshold equal to the throttle interval (fast traces write nothing extra, ever), at most one stub per interval, a heartbeat at 5× the interval so a hung trace keeps reporting, and error-bearing snapshots that bypass the throttle and carry the full record. Readers collapse stubs last-wins per `trace_id`: the viewer shows one row that updates in place and flips to final; `TraceLog` returns the final record, or the latest stub for a trace that never finished. That orphaned stub is the crash evidence — a `SIGKILL` mid-trace previously lost the trace entirely; now the last snapshot on disk says how far it got.

## [0.11.0] — 2026-07-29

### Changed

- **Default `sink_mode` is now `"blocking"`.** The resolved package default was `"buffered"` while the documentation presented `"blocking"` — and buffered as a *default* meant a long-running app that set sinks but not `sink_mode` wrote nothing until process exit, and a crash lost every buffered trace (the exit flush only runs on normal shutdown). Traces now hit the sink the moment they finish, matching the documented behaviour and the first-run experience the package is built around. Buffered mode is unchanged as an explicit opt-in. Apps that already set `sink_mode` are unaffected; apps that relied on the undocumented buffered default now write per-trace instead of deferring to exit.
- **The buffered flush path falls back to `ConsoleSink` when no sinks are configured**, matching the blocking path and the documented fallback. Previously a zero-config app in buffered mode discarded every record at flush with nothing to show it ever existed.

### Fixed

- **Hostile payloads can no longer crash the traced application.** `trace.input()`, `trace.output()`, and event results run inside the traced function's own call, so an exception escaping the sanitiser crashed the app being observed — breaking the `strict=False` promise. Two escape paths existed, both now closed:
  - A **circular structure** (or one nested thousands of levels deep) raised `RecursionError` through the recursive sanitiser. It now carries path-scoped cycle detection and a 100-level depth cap: cycles become `[circular reference]` at the first back-reference, over-deep branches become `[nested too deep]`, and legitimately shared substructures (the same dict under two keys) are not misread as cycles.
  - An **object whose `__str__` raises** escaped through `json.dumps(default=str)`, whose failure was only caught for `TypeError`/`ValueError`. The default hook now guards the `str()` call and the serialisation catch is broadened, so such values degrade to `[TypeName]`.
  - The deeper recursion also closes a redaction hole: a dict inside a list inside a list (`[[{"password": ...}]]`) is now redacted; previously only dicts at the first list level were entered.
- **Sink write failures in blocking mode are reported to stderr** instead of being swallowed with no signal. `SqliteSink` already printed its own errors; a failing `JsonlSink` (disk full, permissions) lost the record invisibly. `strict=True` still raises, and other configured sinks still receive the record as before.

## [0.10.0] — 2026-07-28

### Added

- **Opt-in token auth (`--require-token` / `launch_or_connect(require_token=True)`).** When enabled, every `/api/*` request must carry a random token — `X-TraceAct-Token` header or `?token=` query param — or is refused with 403. The page shell and static assets stay open (they're the same bytes anyone gets from `pip install traceact`); every piece of trace data flows through the gated API. This closes the one hole a localhost server has: on a shared machine, a different OS user can reach `127.0.0.1` and would otherwise read traces through a server running with your file permissions. The token is generated in-process with `secrets` (never accepted as a command-line value, where the process list would expose it), printed as part of the startup URL, and stored in `~/.traceact/viewer.json` at mode `0600` — so your own tools authenticate automatically via the state file while other accounts can't read it. Token comparison is constant-time. Default is off: existing launchers are untouched, and a stray `?token=` param at an untokened server is ignored. On single-instance reuse the running viewer's setting wins (same policy as `base_path`); asking for a token when the running viewer has none prints a notice instead of failing. A state file that lacks the token of a tokened viewer fails closed — the probe reads it as absent and the caller starts its own. An unauthenticated browser page says "this viewer requires a token" rather than posing as an empty trace log.

### Changed

- **Opening the viewer without `?source=` no longer auto-attaches to the first registered source.** A fresh session against a shared viewer previously landed directly on whatever stream happened to be first — some other app's traces, which reads as a leak even though the picker offered the same data one click away. The app now opens on the source picker, and attaching is that deliberate click. Every launch path that knows its source pins it: `traceact view mylog.jsonl` and `launch_or_connect(source=...)` open `?source=<name>` on both the fresh-start and reuse branches, so the common case still opens directly on the right stream.

## [0.9.0] — 2026-07-28

### Added

- **Viewer: `--base-path` / `base_path=` for reverse-proxy mounting.** The viewer can now sit at a subpath inside an existing app's web server (e.g. `/audit-viewer`) rather than requiring a second port. Pass `--base-path /audit-viewer` on the CLI or `base_path="/audit-viewer"` to `launch_or_connect()`. All routes and static-asset URLs are prefixed automatically; the host app's own routes at `/` are left untouched. Works for any depth (`/tools/audit`), handles every reasonable spelling (`audit-viewer`, `/audit-viewer`, `/audit-viewer/`) identically, and maintains single-instance coordination correctly when multiple processes share the same mount.

- **`GET /api/export?source=<name>` — full-source JSONL download.** Returns all trace records for a registered source as an `application/x-ndjson` attachment. Single-file sources are streamed byte-identical (no parsing, no reordering) with a `Content-Length` header. Folder sources merge segments chronologically via `heapq.merge` keyed on `started_at`, the same ordering the viewer displays — malformed or unparseable lines are preserved verbatim rather than silently dropped, blank lines are the only thing removed. Use it to snapshot a running source, export from a remote deployment, or hand trace data to someone who can't reach the viewer's port directly.

- **Viewer: export button in the source picker.** Each source row in the source picker modal now has a `⤓` download button (revealed on hover). Clicking it triggers the `/api/export` endpoint and downloads the source as a `.jsonl` file. The button briefly highlights after the click to confirm the download started. Useful during a live run, though the download captures only what the source has written so far — traces written after the request starts aren't included.

- **Viewer: browser tab title includes the active source name.** The page title is now `TraceAct · <sourceName>` when a source is selected and `TraceAct` when none is. All instances still start with `TraceAct`, so typing the tool name matches every open viewer in the browser's address bar — and the source name after the separator makes each instance distinguishable rather than showing only the port.

## [0.8.1] — 2026-07-27

### Added

- **Viewer: zoom and pan on the trace map.** The mouse wheel zooms about the cursor, left-drag pans, and `+` / `−` / `⟲` buttons in the map toolbar zoom about the centre and reset. Scale is clamped to 0.2×–5×. Large traces previously ran off the panel with only a scrollbar to reach them. Selecting a different trace resets the view; re-rendering the same trace preserves it, so the replay loop doesn't fight the current zoom.

### Fixed

- **Viewer: static assets now send `Cache-Control: no-cache`.** The asset URLs carry no version string, so a browser's cached copy survived a package upgrade and served the previous UI against the new server — a new viewer feature could appear to be missing entirely until a hard refresh. Upgrading *to* this version may still need one hard refresh; upgrades after it won't.

## [0.8.0] — 2026-07-27

### Added

- **`configure(project=...)`** — stamp a project name onto every trace from one call at application startup. The name is stamped onto all root and child traces automatically; a per-trace `project=` argument still overrides it. Traces written without a project name emit a `UserWarning` pointing to this parameter.
- **Viewer: source names read from trace data.** When a source's trace records carry a `project` field, the viewer uses that value as the source name instead of deriving one from the file path. Stable across file moves and works identically for JSONL, SQLite, and future HTTP sources. Sources without a project field show an `unnamed` badge in the source picker — a visible prompt to set `configure(project=...)` rather than a silent fallback.
- **`reset_config()` now clears the trace buffer.** Buffered traces from one test no longer leak into the next test's buffer state.

## [0.7.0] — 2026-07-27

### Changed

- **Viewer: source names identify the project, not the storage layout.** Nearly every app writes to some variant of `<project>/data/traces/traces.jsonl`, so deriving the name from the filename made every project on a machine read as `traces`, `traces-2`, `traces-3` — the one thing the name has to distinguish. Name derivation now skips generic path components (`traces`, `data`, `logs`, `output`, `var`, `tmp`, …) and walks up to the first specific one, so that path reads as `agora`. A filename that already identifies the project (`agora_traces.jsonl`) is honoured as-is, and per-process shards or rotated segments (`traces.1234.jsonl`) reduce to the same project name rather than one entry each.

### Added

- **`launch_or_connect(name=...)`** — label a source explicitly instead of relying on path derivation, for apps that know their own identity: `launch_or_connect(source="data/traces/traces.jsonl", name="agora")`. `add_source_to()` takes the same argument.
- **Viewer: the source picker's list scrolls within its own panel.** A dev machine accumulates a source per project; an unbounded list stretched the modal and pushed the add-source controls off-screen. The list now caps at roughly five visible rows inside a bordered, scrollable panel, and the section header shows the total (`SOURCES (10)`).

## [0.6.2] — 2026-07-27

### Fixed

- **`launch_or_connect()` now opens the viewer on the source it added.** The returned URL carries `?source=<name>`, and the viewer honours it at startup instead of always selecting the first registered source. An app adding its source to a viewer another app had already started previously opened showing that other app's traces. `TraceLog.view()` merges its `?pf_*` params onto this URL, so a pre-filtered view stays pinned to its own source too.
- **Re-adding a source path no longer registers a duplicate.** `add_source()` returns the existing name when the resolved absolute path is already registered, so an app calling `launch_or_connect()` on every run no longer accumulates `traces-2`, `traces-3`, … entries for one unchanging file.
- **`traceact view --port N` / `--new` no longer claims the shared viewer slot.** A deliberately separate instance skips writing the state file (and skips clearing it on exit), so it can't capture the next `launch_or_connect()` call from an unrelated app or evict a running shared viewer's entry.
- **Viewer: long values in the trace inspector wrap instead of overflowing.** The inspector summary is now a two-column grid, so a full-length correlation ID (shown untruncated so it stays copyable) wraps within the value column instead of running past the card edge or spilling under the label column.

### Added

- **Viewer: trace log column headers have tooltips**, including `T·E·B` (Touches · Errors · Budget).

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
