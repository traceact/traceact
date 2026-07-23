#!/usr/bin/env bash
# launch.command — macOS launcher for the TraceAct viewer.
#
# Double-click this file in Finder to open TraceAct in your browser.
# It handles the full startup sequence:
#   1. Checks whether a viewer is already running → opens it immediately.
#   2. Locates Python 3.9+ (pyenv shim, system Python, Homebrew, etc.).
#   3. Creates (or reuses) a local virtual environment at .venv/.
#   4. Installs or upgrades traceact inside that venv.
#   5. Launches `traceact view` and opens your browser.
#
# Putting it in the project directory keeps the venv right next to the code,
# so moving or deleting the folder cleans up everything.

set -euo pipefail

# ── Change into the script's own directory ──────────────────────────────────
cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"

# ── Helpers ─────────────────────────────────────────────────────────────────

say() { echo "[traceact] $*"; }

die() {
  echo ""
  echo "╔══════════════════════════════════════════════════════╗"
  echo "║  TraceAct launcher error                             ║"
  echo "╠══════════════════════════════════════════════════════╣"
  printf  "║  %-52s║\n" "$*"
  echo "╚══════════════════════════════════════════════════════╝"
  echo ""
  # Keep the Terminal window open so the user can read the error.
  read -n 1 -r -p "Press any key to close…"
  exit 1
}

# ── Step 1: check for an already-running instance ───────────────────────────
# Probe the default port. If anything answers /api/health, open that tab and exit.
VIEWER_PORT=8765
VIEWER_HOST="127.0.0.1"

if curl -sf --max-time 0.5 "http://${VIEWER_HOST}:${VIEWER_PORT}/api/health" >/dev/null 2>&1; then
  say "Viewer already running at http://${VIEWER_HOST}:${VIEWER_PORT}/"
  open "http://${VIEWER_HOST}:${VIEWER_PORT}/"
  exit 0
fi

# ── Step 2: locate Python 3.9+ ──────────────────────────────────────────────
find_python() {
  # Prefer pyenv shim, then common Homebrew / system locations.
  for candidate in \
      "$HOME/.pyenv/shims/python3" \
      "$HOME/.pyenv/shims/python" \
      "$(brew --prefix python 2>/dev/null)/bin/python3" \
      /usr/local/bin/python3 \
      /opt/homebrew/bin/python3 \
      /usr/bin/python3 \
      python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      ver=$("$candidate" -c "import sys; print(sys.version_info >= (3,9))" 2>/dev/null || echo False)
      if [ "$ver" = "True" ]; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON=$(find_python) || die "Python 3.9 or later is required but was not found.
Install it from https://python.org or via Homebrew: brew install python"
say "Using Python: $PYTHON ($($PYTHON --version 2>&1))"

# ── Step 3: create or reuse the virtual environment ─────────────────────────
VENV="$SCRIPT_DIR/.venv"

if [ ! -f "$VENV/bin/activate" ]; then
  say "Creating virtual environment at .venv/ …"
  "$PYTHON" -m venv "$VENV" || die "Could not create virtual environment."
fi

# Activate it so pip and traceact resolve from the venv.
# shellcheck disable=SC1091
source "$VENV/bin/activate"

PIP="$VENV/bin/pip"
TRACEACT="$VENV/bin/traceact"

# ── Step 4: install / ensure traceact is present ────────────────────────────
if [ ! -f "$TRACEACT" ]; then
  say "Installing traceact …"
  "$PIP" install --quiet --upgrade traceact || die "pip install traceact failed.
Check your internet connection and try again."
else
  # Already installed: do a quick silent upgrade check so the viewer stays current.
  say "Checking for traceact updates …"
  "$PIP" install --quiet --upgrade traceact 2>/dev/null || true
fi

# Confirm the command is available.
[ -f "$TRACEACT" ] || die "traceact command not found after install — this is unexpected."
say "traceact $("$TRACEACT" --help 2>&1 | head -1 || echo '(installed)')"

# ── Step 5: launch the viewer ───────────────────────────────────────────────
say "Starting TraceAct viewer …"

# Pass any argument given on the command line as the source (e.g. when opened
# via `open launch.command path/to/traces.jsonl` from a script).
SOURCE_ARG="${1:-}"

if [ -n "$SOURCE_ARG" ]; then
  "$TRACEACT" view "$SOURCE_ARG" &
else
  "$TRACEACT" view &
fi

# Wait briefly for the server to be ready, then open the browser.
MAX_WAIT=8
for i in $(seq 1 $MAX_WAIT); do
  if curl -sf --max-time 0.5 "http://${VIEWER_HOST}:${VIEWER_PORT}/api/health" >/dev/null 2>&1; then
    say "Ready at http://${VIEWER_HOST}:${VIEWER_PORT}/"
    open "http://${VIEWER_HOST}:${VIEWER_PORT}/"
    break
  fi
  sleep 0.5
done

# Keep the Terminal window alive as long as the viewer process is running
# so Ctrl+C in the window stops it cleanly.
wait
