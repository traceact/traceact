# viewer/cli.py
#
# The `traceact` command line. Installed as a console script (see pyproject.toml
# [project.scripts]) so that after `pip install traceact` the user has a
# `traceact` command available.
#
# Commands:
#   traceact view [SOURCE]     open the viewer in a browser
#   traceact show [SOURCE]     alias of view (identical behaviour)
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
import sys
import threading
import time
import webbrowser
from typing import Optional

from traceact.viewer.server import ViewerServer, ViewerState

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
    view.set_defaults(handler=_run_view)

    return parser


def _run_view(args: argparse.Namespace) -> int:
    """
    Start the viewer server and (unless suppressed) open a browser tab.
    """
    state = ViewerState()

    # Seed the source given on the command line, if any.
    if args.source is not None:
        name = state.add_source(args.source)
        print(f"Loaded source '{name}' → {state.sources[name]}")

    server, port = _start_server(args.host, args.port, state)
    if server is None:
        print(
            f"Could not bind a port near {args.port}. "
            "Try a different --port.",
            file=sys.stderr,
        )
        return 1

    url = f"http://{args.host}:{port}/"
    print(f"TraceAct viewer running at {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        # Open the browser shortly after the server starts accepting requests.
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping viewer.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


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
