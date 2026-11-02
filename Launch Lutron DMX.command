#!/bin/bash
# Double-click this file (macOS) to start Lutron DMX from this folder: it opens a log window and the
# web GUI in your browser. Needs Python 3.10+ (python.org, or `brew install python@3.12 python-tk@3.12`).
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
VENV=".venv"
PY="$VENV/bin/python"
STAMP="$VENV/.lutron-dmx-installed"

fail() {
  echo
  echo "$1"
  echo
  read -n 1 -s -r -p "Press any key to close this window."
  exit 1
}

if [ ! -x "$PY" ]; then
  BASE=""
  for candidate in python3.12 python3.13 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1; then BASE="$candidate"; break; fi
  done
  [ -n "$BASE" ] || fail "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ and double-click this file again."
  echo "First run: setting up Lutron DMX with $($BASE --version) (about a minute)..."
  "$BASE" -m venv "$VENV" || fail "Could not create the .venv folder."
fi

if [ ! -f "$STAMP" ] || [ pyproject.toml -nt "$STAMP" ]; then
  echo "Installing Lutron DMX and its requirements..."
  "$PY" -m pip install --quiet -e . || fail "Installing the requirements failed (are you online?)."
  touch "$STAMP"
fi

if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
  echo "Note: this Python has no tkinter, so there is no log window; the log shows in this Terminal window instead."
  echo "To get the window: brew install python-tk@3.12, delete the .venv folder, and double-click this file again."
  echo
fi

exec "$PY" -m lutron_dmx.launcher "$@"
