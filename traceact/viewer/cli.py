# viewer/cli.py
#
# The `traceact` command line. Installed as a console script (see pyproject.toml
# [project.scripts]) so that after `pip install traceact` the user has a
# `traceact` command available.
#
# Commands:
#   traceact view [SOURCE]     open the viewer in a browser
#   traceact show [SOURCE]     alias of view (identical behaviour)
#   traceact doctor [SOURCE]   run local health checks
#
# SOURCE is optional and may be a .jsonl file or a folder of them. When given,
# the viewer opens straight onto that source. When omitted, the viewer opens
# empty and prompts the user to add a source.
#
# On `view`/`show`:
# Both names are kept deliberately, as aliases of one command, until a single
# preferred verb is chosen. They share one handler, so there's no duplicated
# behaviour — only two spellings pointing at the same code.
#
# Flags:
#   --port N        port to serve on (default 8765; auto-increments if taken)
#   --host HOST     interface to bind (default 127.0.0.1, localhost only)
#   --no-browser    start the server but don't open a browser tab
#   --map           open straight onto the map for SOURCE's newest trace
#   --focus-hook URL  show a Focus control on every trace; clicking it POSTs
#                   the full record to URL (see _serve_focus in server.py)

import argparse
import os
import secrets
import sys
import threading
import webbrowser
from typing import Optional

from traceact.viewer import doctor as _doctor
from traceact.viewer.server import (
    ViewerServer,
    ViewerState,
    _normalise_base_path,
    _validate_focus_hook,
    focus_hook_is_loopback,
)
import traceact.viewer.instance as _instance

_DEFAULT_PORT = 8765
_DEFAULT_HOST = "127.0.0.1"


def main(argv: Optional[list] = None) -> int:
    """
    Entry point for the `traceact` command. Returns a process exit code.
    """
    args = _build_parser().parse_args(argv)

    # argparse routes both `view` and `show` here via the shared handler set on
    # each subparser. If no subcommand was given, show help and exit.
    handler = getattr(args, "handler", None)
    if handler is None:
        _build_parser().print_help()
        return 1
    return handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traceact",
        description="TraceAct — X-ray vision for Python code. Open the trace viewer.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # `view` is the canonical command; `show` is registered as an alias, so both
    # appear in help under one entry and run the exact same handler.
    view = subparsers.add_parser(
        "view",
        aliases=["show"],
        help="Open the TraceAct viewer in a browser.",
        description="Open the TraceAct viewer. Optionally load a source (a "
        ".jsonl file or a folder of them) on start.",
    )
    view.add_argument(
        "source",
        nargs="?",
        default=None,
        help="A .jsonl file or a folder of them to load on start. "
        "If omitted, the viewer opens empty and prompts for a source.",
    )
    view.add_argument(
        "--port", type=int, default=_DEFAULT_PORT,
        help=f"Port to serve on (default {_DEFAULT_PORT}; increments if taken).",
    )
    view.add_argument(
        "--host", default=_DEFAULT_HOST,
        help=f"Interface to bind (default {_DEFAULT_HOST}, localhost only).",
    )
    view.add_argument(
        "--no-browser", action="store_true",
        help="Start the server without opening a browser tab.",
    )
    view.add_argument(
        "--new", action="store_true",
        help="Force a new viewer instance even if one is already running.",
    )
    view.add_argument(
        "--base-path", default="",
        help="Serve every route under a path prefix (e.g. /audit-viewer) so "
             "the viewer can sit behind another app's reverse proxy. Defaults "
             "to the root.",
    )
    view.add_argument(
        "--map", action="store_true",
        help="Open straight onto the Trace map for the newest trace in "
             "SOURCE, instead of the trace log. Has no effect without "
             "SOURCE (there's nothing to select onto).",
    )
    view.add_argument(
        "--focus-hook", default=None, metavar="URL",
        help="Show a Focus control on every trace; clicking it POSTs the "
             "full trace record as JSON to URL. Any http(s) URL is accepted "
             "— passing this flag is your consent to send records there, "
             "and the destination is printed at startup. A hook that "
             "doesn't answer 2xx within a second shows a notice in the "
             "viewer and never blocks it. A non-loopback URL auto-enables "
             "--require-token, since the API now has a reason to be "
             "reached from off this machine.",
    )
    view.add_argument(
        "--require-token", action="store_true",
        help="Require a random token on every API request (printed as part "
             "of the URL). Keeps other OS users on a shared machine out of "
             "the viewer; your own tools pick the token up automatically. "
             "The token is generated here, never accepted as a value — a "
             "token on a command line would be readable in the process list.",
    )
    view.set_defaults(handler=_run_view)

    doctor = subparsers.add_parser(
        "doctor",
        help="Run local health checks: Python version, state directory, "
        "a running viewer, and (optionally) a source's trace data.",
        description="Check that TraceAct is set up correctly on this machine. "
        "Pass a SOURCE to also validate a .jsonl file or folder.",
    )
    doctor.add_argument(
        "source",
        nargs="?",
        default=None,
        help="A .jsonl file or folder to validate (optional).",
    )
    doctor.add_argument(
        "--scan", action="store_true",
        help="Scan the source's files for known credential formats (AWS "
             "keys, sk- tokens, JWTs, PEM blocks, ...) — the same registry "
             "that redacts captured values at trace time, run over what is "
             "already on disk. Requires SOURCE. Found secrets fail the check.",
    )
    doctor.set_defaults(handler=_run_doctor)

    return parser


def _run_view(args: argparse.Namespace) -> int:
    """
    Start the viewer server (or reuse an existing one) and open a browser tab.

    Single-instance behaviour: unless --new is passed or a specific --port was
    requested, we first probe for an already-running viewer. If one is found we
    add the requested source to it and open a browser tab on it — no second
    server is started. This avoids accumulating zombie viewer processes across
    repeated `traceact view` calls during a dev session.

    When a NEW instance is still wanted:
      - the user passed --new explicitly
      - the user specified a --port (implies they want control of that port)
      - no existing viewer is responding (stale state file)
    """
    user_chose_port = (args.port != _DEFAULT_PORT)
    # Normalised here so the printed URL, the state file, and the server all
    # agree on one spelling of the prefix.
    base_path = _normalise_base_path(getattr(args, "base_path", ""))

    # Validated before anything starts, so a mistyped URL fails here with a
    # clear message instead of surfacing on the first Focus click.
    try:
        focus_hook = _validate_focus_hook(getattr(args, "focus_hook", None))
    except ValueError as exc:
        print(f"traceact view: {exc}", file=sys.stderr)
        return 2

    if not args.new and not user_chose_port:
        existing = _instance.find_running()
        if existing is not None:
            host, port = existing["host"], existing["port"]
            # A running viewer's own prefix and token, which may differ from
            # this call's: both are fixed when a server starts, so the running
            # instance's settings win over whatever was asked for here.
            running_base = existing.get("base_path", "")
            running_token = existing.get("token")
            if args.require_token and not running_token:
                print(
                    "Note: reusing a viewer that was started without "
                    "--require-token; its API stays open to local callers. "
                    "Stop it and relaunch to turn token auth on.",
                    file=sys.stderr,
                )
            if focus_hook:
                # A server's hook is fixed when it starts, like its token and
                # base path — a running instance can't gain one, and one it
                # already has keeps the URL it started with.
                if existing["health"].get("focus_hook"):
                    print(
                        "Note: the running viewer keeps the focus hook it "
                        "was started with; the URL passed here is ignored.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "Note: reusing a viewer that was started without "
                        "--focus-hook; Focus controls won't appear. Stop it "
                        "and relaunch with the flag to enable the hook.",
                        file=sys.stderr,
                    )
            source_name = None
            if args.source is not None:
                added = _instance.add_source_to(host, port, args.source,
                                                base_path=running_base,
                                                token=running_token)
                if added:
                    source_name = added["name"]
                    print(f"Added source '{added['name']}' to running viewer.")
                else:
                    print(
                        "Could not add source to the running viewer. "
                        "The server may have restarted.",
                        file=sys.stderr,
                    )
            # ?source= pins the tab to the source just added. Without it the
            # app opens on its picker rather than auto-attaching to whatever
            # stream happens to be first — see init() in static/app.js.
            url = _instance._viewer_url(host, port, running_base,
                                        source=source_name,
                                        token=running_token,
                                        open_map=args.map)
            print(f"Reusing existing viewer at {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0

    # No existing viewer (or --new / explicit --port): start one.
    _configure_app_tracing()
    state = ViewerState()
    source_name = None
    if args.source is not None:
        source_name = state.add_source(args.source)
        print(f"Loaded source '{source_name}' → {state.sources[source_name]}")

    # A focus hook that isn't on this machine gives the viewer's API a
    # reason to be reached from somewhere other than "whatever's running
    # locally" — auto-require a token in that case even without
    # --require-token, same as if the user had asked for it. A loopback
    # hook (the common case) leaves the viewer just as open as always.
    require_token = args.require_token
    if focus_hook and not require_token and not focus_hook_is_loopback(focus_hook):
        require_token = True
        print(
            "Note: --focus-hook points off this machine, so token auth is "
            "on automatically (pass --require-token to silence this note).",
            file=sys.stderr,
        )

    # The token is generated here, in-process, and reaches clients only via
    # the printed URL and the 0600 state file — never a command line.
    token = secrets.token_urlsafe(24) if require_token else None

    server, port = _start_server(args.host, args.port, state,
                                 base_path=base_path, token=token,
                                 focus_hook=focus_hook)
    if server is None:
        print(
            f"Could not bind a port near {args.port}. "
            "Try --new or a different --port.",
            file=sys.stderr,
        )
        return 1

    url = _instance._viewer_url(args.host, port, base_path,
                                source=source_name, token=token,
                                open_map=args.map)
    print(f"TraceAct viewer running at {url}")
    if token:
        print("Token auth is on: API requests need the token from the URL "
              "above (?token= or an X-TraceAct-Token header).")
    if focus_hook:
        # The destination is always visible: every Focus click sends the
        # full trace record here.
        print(f"Focus hook: {focus_hook}")
    print("Press Ctrl+C to stop.")

    # Only a default-port instance advertises itself as the shared viewer.
    # An instance the user deliberately separated — via --new or an explicit
    # --port — stays private: claiming the state file would point the next
    # launch_or_connect() caller (another app entirely) at this instance, so
    # that app's traces would be POSTed here and it would open a viewer
    # showing this one's source instead of its own.
    if not args.new and not user_chose_port:
        _instance.write_state(args.host, port, base_path=base_path,
                              token=token)

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping viewer.")
    finally:
        server.shutdown()
        server.server_close()
        # Only clear the state file if this instance wrote it. A private
        # instance clearing it would evict a still-running shared viewer's
        # entry, making the next caller spawn a duplicate.
        if not args.new and not user_chose_port:
            _instance.clear_state()
    return 0


def _configure_app_tracing() -> None:
    """
    Route package tracing to a file for the lifetime of this viewer process.

    The CLI is the app here, so it owns the process's tracing configuration
    — and only when nothing else has configured it first. Libraries the
    viewer imports may trace themselves with traceact (rates, used for cost
    estimates, does); with no sinks configured those traces would print to
    stdout via the console fallback, interleaved with the viewer's own
    startup output. A capped JsonlSink keeps them on disk and inspectable
    instead.

    An unwritable home directory skips this step: the console fallback
    then applies, which is traceact's standard no-sink behaviour.
    """
    from traceact import JsonlSink, configure
    from traceact.config import get_package_sinks

    if get_package_sinks():
        return
    path = os.path.expanduser("~/.traceact/viewer-traces.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        configure(sinks=[JsonlSink(path, max_bytes=10_000_000)])
    except OSError:
        pass


def _run_doctor(args: argparse.Namespace) -> int:
    """
    Run traceact.viewer.doctor.run_checks() and print a pass/fail report.

    The Settings page's "Run diagnostics" button (GET /api/doctor) runs the
    exact same checks via the same shared module — this function only differs
    in how it renders the result (text here, JSON there).

    Returns 0 if every check that can fail passed, 1 otherwise. A missing
    running viewer is never a failure by itself (reported as "info").

    With --scan, additionally runs the credential scan over the source's
    files; any finding fails the run.
    """
    if getattr(args, "scan", False):
        if args.source is None:
            print("doctor --scan needs a SOURCE to scan.", file=sys.stderr)
            return 1
        return _run_scan(args.source)

    result = _doctor.run_checks(args.source)

    print("traceact doctor")
    print()
    for check in result["checks"]:
        if check["status"] == "info":
            print(f"  ·  {check['message']}")
        else:
            print(f"  {'✓' if check['status'] == 'pass' else '✗'}  {check['message']}")
            if check.get("hint"):
                print(f"       → {check['hint']}")

    print()
    if result["ok"]:
        print("All checks passed.")
    else:
        print("Some checks failed — see above.", file=sys.stderr)
    return 0 if result["ok"] else 1


def _run_scan(source: str) -> int:
    """
    Render doctor.scan_source() as a text report. Exit 0 when clean, 1 when
    anything was found — a planted secret in trace data is a failure, full
    stop, so the exit code is scriptable in CI.
    """
    result = _doctor.scan_source(source)

    print("traceact doctor --scan")
    print()
    print(f"  Scanned {result['lines']} line(s) across "
          f"{result['files']} file(s).")
    print()

    if result["ok"]:
        print("  ✓  No known credential formats found.")
        return 0

    for hit in result["hits"]:
        print(f"  ✗  {hit['pattern']}  {hit['file']}:{hit['line']}")
    if result["hits_capped"]:
        print("  …  more findings exist; output capped at "
              f"{len(result['hits'])}.")
    print()
    print(
        f"{len(result['hits'])}{'+' if result['hits_capped'] else ''} "
        "credential-shaped value(s) found — see above.",
        file=sys.stderr,
    )
    print(
        "These are already on disk: rotate the credentials if they are "
        "live, and delete or rewrite the affected files. Records written "
        "from now on are covered by value-pattern redaction "
        "(TraceConfig(redact_values=True), the default).",
        file=sys.stderr,
    )
    return 1


def _start_server(host: str, port: int, state: ViewerState,
                  base_path: str = "", token: Optional[str] = None,
                  focus_hook: Optional[str] = None):
    """
    Try to bind the requested port, incrementing a few times if it's in use.
    Returns (server, actual_port) or (None, port) if no nearby port was free.
    """
    for candidate in range(port, port + 20):
        try:
            server = ViewerServer(host, candidate, state,
                                  base_path=base_path, token=token,
                                  focus_hook=focus_hook)
            return server, candidate
        except OSError:
            # Port in use; try the next one.
            continue
    return None, port


# Allow `python -m traceact.viewer.cli ...` in addition to the installed
# `traceact` console script.
if __name__ == "__main__":
    sys.exit(main())
