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
# preferred verb is chosen. They share one handler, so there is no duplicated
# behaviour — only two spellings pointing at the same code.
#
# Flags:
#   --port N        port to serve on (default 8765; auto-increments if taken)
#   --host HOST     interface to bind (default 127.0.0.1, localhost only)
#   --no-browser    start the server but do not open a browser tab

import argparse
import json
import os
import sys
import threading
import webbrowser
from typing import Optional

from traceact import __version__
from traceact.viewer.server import ViewerServer, ViewerState
from traceact.viewer.reader import is_valid_trace, _jsonl_files
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

    if not args.new and not user_chose_port:
        existing = _instance.find_running()
        if existing is not None:
            host, port = existing["host"], existing["port"]
            if args.source is not None:
                added = _instance.add_source_to(host, port, args.source)
                if added:
                    print(f"Added source '{added['name']}' to running viewer.")
                else:
                    print(
                        "Could not add source to the running viewer. "
                        "The server may have restarted.",
                        file=sys.stderr,
                    )
            url = f"http://{host}:{port}/"
            print(f"Reusing existing viewer at {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0

    # No existing viewer (or --new / explicit --port): start one.
    state = ViewerState()
    if args.source is not None:
        name = state.add_source(args.source)
        print(f"Loaded source '{name}' → {state.sources[name]}")

    server, port = _start_server(args.host, args.port, state)
    if server is None:
        print(
            f"Could not bind a port near {args.port}. "
            "Try --new or a different --port.",
            file=sys.stderr,
        )
        return 1

    url = f"http://{args.host}:{port}/"
    print(f"TraceAct viewer running at {url}")
    print("Press Ctrl+C to stop.")

    _instance.write_state(args.host, port)

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping viewer.")
    finally:
        server.shutdown()
        server.server_close()
        _instance.clear_state()
    return 0


def _run_doctor(args: argparse.Namespace) -> int:
    """
    Run a handful of local checks and print a pass/fail report:

      - the running Python version meets the 3.9 minimum
      - the ~/.traceact state directory can be created and is writable
        (single-instance coordination and drag-drop imports depend on this)
      - whether a viewer is currently running (informational only — a viewer
        isn't required for tracing to work)
      - if SOURCE is given: that it exists and its lines parse as valid traces

    Returns 0 if every check that can fail passed, 1 otherwise. A missing
    running viewer is never a failure by itself.
    """
    ok = True
    print("traceact doctor")
    print()

    py_ok = sys.version_info >= (3, 9)
    _report(py_ok, f"Python {sys.version_info.major}.{sys.version_info.minor} "
                   f"({'OK, 3.9+ required' if py_ok else 'below the 3.9 minimum'})")
    ok = ok and py_ok

    print(f"  ·  traceact {__version__}")

    state_dir = os.path.expanduser("~/.traceact")
    try:
        os.makedirs(state_dir, exist_ok=True)
        writable = os.access(state_dir, os.W_OK)
    except OSError:
        writable = False
    _report(writable, f"State directory ({state_dir}) is writable")
    ok = ok and writable

    existing = _instance.find_running()
    if existing is not None:
        health = existing["health"]
        print(
            f"  ·  Viewer running at http://{existing['host']}:{existing['port']}/ "
            f"(v{health.get('version', '?')}, {health.get('sources', 0)} source(s))"
        )
    else:
        print("  ·  No viewer currently running (not required).")

    if args.source is not None:
        ok = _check_source(args.source) and ok

    print()
    if ok:
        print("All checks passed.")
    else:
        print("Some checks failed — see above.", file=sys.stderr)
    return 0 if ok else 1


def _check_source(path: str) -> bool:
    """
    Validate a SOURCE passed to `doctor`: it must exist and contain at least
    one .jsonl file whose lines parse as JSON and look like trace records.
    An empty (freshly created) source is reported as fine, not a failure.
    """
    if not os.path.exists(path):
        _report(False, f"Source '{path}' does not exist")
        return False

    files = _jsonl_files(path)
    if not files:
        _report(False, f"Source '{path}' has no .jsonl files")
        return False

    total_lines = 0
    valid_lines = 0
    for filepath in files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    total_lines += 1
                    try:
                        if is_valid_trace(json.loads(line)):
                            valid_lines += 1
                    except (json.JSONDecodeError, ValueError):
                        pass
        except OSError as e:
            _report(False, f"Could not read {filepath}: {e}")
            return False

    if total_lines == 0:
        print(f"  ·  {path}: {len(files)} file(s), no trace lines yet.")
        return True

    passed = valid_lines > 0
    _report(
        passed,
        f"{path}: {valid_lines}/{total_lines} line(s) look like valid traces "
        f"across {len(files)} file(s)",
    )
    return passed


def _report(passed: bool, message: str) -> None:
    print(f"  {'✓' if passed else '✗'}  {message}")


def _start_server(host: str, port: int, state: ViewerState):
    """
    Try to bind the requested port, incrementing a few times if it's in use.
    Returns (server, actual_port) or (None, port) if no nearby port was free.
    """
    for candidate in range(port, port + 20):
        try:
            server = ViewerServer(host, candidate, state)
            return server, candidate
        except OSError:
            # Port in use; try the next one.
            continue
    return None, port


# Allow `python -m traceact.viewer.cli ...` in addition to the installed
# `traceact` console script.
if __name__ == "__main__":
    sys.exit(main())
