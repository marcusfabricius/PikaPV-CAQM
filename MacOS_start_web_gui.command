#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
PYTHON=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
fi
if [ -z "$PYTHON" ]; then
  echo "Python 3 is not installed or not in PATH."
  echo "Install Python 3 and try again."
  read -p "Press Enter to close..."
  exit 1
fi
"$PYTHON" "$SCRIPT_DIR/start_web_gui.py" "$@"
