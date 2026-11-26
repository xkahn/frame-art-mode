#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

VENV_PY="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: missing $VENV_PY. Recreate the virtual environment with Python 3.11:" >&2
    echo "  python3.11 -m venv .venv" >&2
    exit 2
fi

if ! "$VENV_PY" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' ; then
    PYTHON_VERSION=$("$VENV_PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    echo "ERROR: $VENV_PY uses Python $PYTHON_VERSION. Recreate .venv with Python 3.11 or newer:" >&2
    echo "  python3.11 -m venv .venv" >&2
    exit 2
fi

exec "$VENV_PY" -m frame_art_mode.supervisor --stop --config "$SCRIPT_DIR/config.json" "$@"
