# Manifest

Last updated: 2026-09-02 10:34:32 UTC

Every source file in the repository, with what it does and what it touches. A map for orienting, not a second copy of the docstrings.

## Package — recording

| File | What it does |
|---|---|
| `traceact/__init__.py` | Public exports and `__version__`. Everything importable from the package root is declared here. |
| `traceact/trace.py` | `ActionTrace`: the trace lifecycle — start/finish, steps, events, touches, inputs/outputs, errors, parent/child linking, budget enforcement, in-flight streaming snapshots, ending classification (cancelled vs failed). Writes to the configured sinks on finish. |
| `traceact/decorators.py` | `@traced_action` for sync and async functions: argument capture (with per-field transforms), the reserved `traceact_context` kwarg for queue propagation, status/exception handling. |
| `traceact/config.py` | `TraceConfig`, `configure()`, `reset_config()`, `get_package_sinks()`: package-level configuration state and its resolution order. |
| `traceact/budget.py` | `TraceBudget` and the `TraceBudget.production()` preset: recording limits (events, steps, depth, payload bytes, sampling). |
| `traceact/context.py` | The `ContextVar` holding the active trace, plus the skip sentinel used to suppress children of sampled-out traces. |
| `traceact/helpers.py` | `TraceHelpersMixin`: `trace.db`, `trace.http`, `trace.file`, `trace.model`, `trace.tool`, `trace.queue` — shorthand wrappers over `trace.event()`. |
| `traceact/ids.py` | ID generation (`trc_`, `evt_`, `stp_`, `corr_` prefixes). |
| `traceact/redaction.py` | Field-name redaction patterns, `REDACTION_PRESETS`, and the `VALUE_PATTERNS` credential-format registry scanned over captured string content. |
| `traceact/sinks.py` | `JsonlSink` (file, thread-safe, `max_bytes` rotation), `ConsoleSink` (stdout), `AsyncSink` (background-thread wrapper, bounded queue), `SqliteSink` (local database), `HttpSink` and `OtlpSink` (network delivery via stdlib `urllib`, guarded by `_netguard`, `network_policy` warn/enforce/off). |
| `traceact/_netguard.py` | The shared outbound-network guard: destination classification (private/link-local/reserved/multicast/unspecified rejected by default, loopback always permitted), plain-`http://` policy, redirect-refusing opener. Used by `HttpSink`, `OtlpSink`, and the viewer's focus-hook forward. Touches DNS (`socket.getaddrinfo`). |
| `traceact/log.py` | `TraceLog`: programmatic filter/query over JSONL files, folders, and `SqliteSink` databases; `view()` opens the viewer pre-filtered; dotted filter fields walk nested paths. Reads sources from disk on every terminal call. |
| `traceact/propagation.py` | Cross-service linking: `inject_headers`, `inject_context`, `propagate`, `extract_trace_id` — the `traceact-trace-id` / `traceact-correlation-id` header pair. |
| `traceact/middleware.py` | `TraceActMiddleware` (WSGI) and `TraceActASGIMiddleware` (ASGI): automatic inbound propagation for Flask, Django, FastAPI, Starlette. |
| `traceact/integrations/__init__.py` | Empty namespace marker; nothing in `integrations/` is imported by the top-level package. |
| `traceact/integrations/langchain.py` | `TraceActCallbackHandler`: maps LangChain runs (chains, models, tools, retrievers) to traces with correct parentage and token counts. Imports `langchain-core` only when the module itself is imported. |
| `traceact/py.typed` | PEP 561 marker so type checkers read the package's annotations. |

## Package — viewer

| File | What it does |
|---|---|
| `traceact/viewer/__init__.py` | Empty namespace marker. |
| `traceact/viewer/cli.py` | The `traceact` command: `view`/`show` (start or reuse the viewer, auto-token for non-loopback focus hooks, routes package tracing to `~/.traceact/viewer-traces.jsonl` when its process has no sinks) and `doctor` (health checks, `--scan` credential audit). |
| `traceact/viewer/server.py` | `ViewerServer`/`ViewerState` on stdlib `ThreadingHTTPServer`: static SPA, `/api/health`, `/api/sources`, `/api/pick`, `/api/import`, `/api/stream` (SSE), `/api/query`, `/api/export`, `/api/doctor`, `/api/focus` (forwards a record to the focus hook via `_netguard`), `/api/cost` (prices a model call via `viewer/cost.py`). Token gate and base-path mounting apply to every API route; POST bodies are capped. |
| `traceact/viewer/reader.py` | `SourceReader`: snapshot plus live tail for JSONL files, folders of shards, and SQLite databases — byte-offset/id cursors, delete+recreate detection (inode plus first-bytes fingerprint), in-flight stub dedupe. |
| `traceact/viewer/doctor.py` | `run_checks()` (Python version, rates presence, state directory, running viewer, source validity) and `scan_source()` (the `VALUE_PATTERNS` registry over files on disk). Shared by the CLI and `GET /api/doctor`. |
| `traceact/viewer/instance.py` | Single-instance coordination: the `~/.traceact/viewer.json` state file, health probing, `launch_or_connect()` for embedding apps (spawns the CLI as a subprocess when nothing is running). |
| `traceact/viewer/cost.py` | Cost estimates for model events via the optional `rates` package: lazy import, one bundled-snapshot registry load per process, provider+model price lookup (ambiguous or unknown pairs refused). Never touches traceact's global configuration or the network. |
| `traceact/viewer/static/index.html` | The single-page app shell. |
| `traceact/viewer/static/styles.css` | The viewer's design system: dark theme, CSS custom properties, hover popups. |
| `traceact/viewer/static/app.js` | All front-end logic, no framework: source management, SSE stream handling, trace log, inspector, map replay, search and pre-filters, settings, diagnostics, focus controls, cost estimates. |

## Tests

| File | What it covers |
|---|---|
| `tests/conftest.py` | `_clean_config` autouse fixture: `reset_config()` around every test. |
| `tests/test_async_sink.py` | `AsyncSink`: queue policies, drop counters, shutdown flush, fork safety. |
| `tests/test_decorators.py` | `@traced_action`'s `capture_inputs` resolution through the package-default → `configure()` → decorator chain. |
| `tests/test_endings.py` | Ending classification and recording: cancelled coroutines are written (the dropped-trace defect), cancelled vs failed status mapping, sampled-out promotion of cancellations, the `errors=` code map. |
| `tests/test_docs.py` | Docs hygiene: no internal references in public docs or shipped source; absolute README links. |
| `tests/test_doctor.py` | `run_checks()` output shape and per-check statuses. |
| `tests/test_event_inputs.py` | `capture_event_inputs`: opt-in recording, kill switch, redaction of event inputs. |
| `tests/test_http_sink.py` | `HttpSink`: delivery, headers, failure counting, `network_policy` modes. |
| `tests/test_integration_celery.py` | Queue propagation through a Celery-shaped task boundary. |
| `tests/test_integration_langchain.py` | The LangChain adapter: run mapping, parentage, token counts, content opt-in. |
| `tests/test_nested_filters.py` | Dotted-path filtering in `TraceLog` and over `/api/query`: list fan-out, operator composition, missing-path semantics, unchanged top-level behaviour. |
| `tests/test_netguard.py` | `_netguard`: address classification, multi-answer DNS, redirect refusal (live loopback servers), public export pins. |
| `tests/test_otlp_sink.py` | `OtlpSink`: span mapping, delivery, failure counting, `network_policy` modes. |
| `tests/test_payload_hostility.py` | Hostile payloads can't crash the traced app; sink failures stay visible; the default sink mode writes immediately. |
| `tests/test_project_name.py` | `configure(project=...)` cascading to traces, and the warning for a root trace written without one. |
| `tests/test_propagation.py` | Header inject/extract across framework header objects; `propagate()`. |
| `tests/test_quantum_kinds.py` | `gate`/`qstate` event kinds and their qubit touch derivation. |
| `tests/test_queue_tracing.py` | `inject_context`, the `traceact_context` kwarg, `trace.queue()`. |
| `tests/test_reader.py` | `SourceReader`: tailing, rotation, delete+recreate detection (incl. inode reuse), stub collapse. |
| `tests/test_redaction.py` | Field-name redaction, presets, nested structures, transforms. |
| `tests/test_sinks.py` | `JsonlSink` writes, size-based rotation, and the shared buffered-mode record buffer. |
| `tests/test_sqlite_sink.py` | `SqliteSink`: schema, upsert by trace id, WAL, write-error reporting. |
| `tests/test_sqlite_source.py` | Reading SqliteSink databases through the viewer reader and `TraceLog`. |
| `tests/test_stream_progress.py` | In-flight streaming: grace, throttle, heartbeat, error snapshots, reader collapse. |
| `tests/test_tool_tracking.py` | `kind="tool"`, `trace.tool()`, explicit parenting. |
| `tests/test_trace_hardening.py` | Failure promotion under sampling, SKIP propagation parity, and event kwargs never overwriting core event fields. |
| `tests/test_tracelog.py` | `TraceLog` filters, operators, terminal methods, `query()` flags, scan caps. |
| `tests/test_value_redaction.py` | Value-pattern scanning (every registered pattern pinned), capture transforms, and `doctor --scan`. |
| `tests/test_viewer_base_path.py` | Base-path mounting: route prefixes, 404 outside the mount, asset rewriting. |
| `tests/test_viewer_cost.py` | `viewer/cost.py` and `GET /api/cost`: pricing, refusals, rates-absent world, health flag, doctor line, the CLI's app-level sink setup. |
| `tests/test_viewer_export.py` | `/api/export`: byte-identical single files, folder merges, SQLite export. |
| `tests/test_viewer_focus.py` | The focus hook end to end: forwarding, redirect refusal, body caps, auto-token (CLI and `launch_or_connect`). |
| `tests/test_viewer_instance.py` | State-file coordination, probing, `launch_or_connect()` reuse and spawn. |
| `tests/test_viewer_query.py` | `/api/query`: filter parsing, operator allow-list, limit clamp, completeness flags. |
| `tests/test_viewer_token.py` | Token auth: gated routes, constant-time comparison, state-file pickup. |

## Repository

| File | What it does |
|---|---|
| `pyproject.toml` | Package metadata, version, dev extras, the `traceact` console script, wheel package-data (USAGE.md, static assets, `py.typed`), pytest and mypy configuration. |
| `MANIFEST.in` | sdist contents beyond the package (USAGE.md and friends). |
| `launch.command` | Double-clickable macOS launcher: finds Python 3.10+, creates/reuses `.venv/`, installs traceact, starts the viewer. |
| `README.md` | Front page: install, quick start, viewer tour, links to the full docs. |
| `USAGE.md` | The full manual — every API, flag, endpoint, and recipe. |
| `ARCHITECTURE.md` | Pipeline and viewer diagrams, component contracts, security considerations. |
| `CHANGELOG.md` | Public, dated release history. |
| `CLA.md` | The contributor license agreement outside pull requests sign. |
| `LICENSE` | MIT. |
| `.github/workflows/ci.yml` | Pytest across Python 3.10/3.11/3.12 plus a mypy job, on every push and pull request. |
| `.github/workflows/publish.yml` | Builds and publishes to PyPI on a published GitHub release (OIDC trusted publishing). |
| `.github/workflows/cla.yml` | CLA signing gate on pull requests (`contributor-assistant`). |
| `.github/dependabot.yml` | Dependency update PRs, `github-actions` ecosystem included. |
| `.gitignore` | Untracked local state (venvs, caches, per-developer tool folders). |
| `signatures/version1/cla.json` | CLA signature records, committed by the CLA workflow's bot. Never hand-edited. |
