#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SETUP_PY="$SCRIPT_DIR/tools/easy_setup.py"

if [ ! -f "$SETUP_PY" ]; then
    echo "Please extract the downloaded .zip archive first, then run ./1_Click_Uninstall.sh" >&2
    exit 1
fi

for PY_CAND in python3.12 python3 python; do
    if command -v "$PY_CAND" >/dev/null 2>&1; then
        if "$PY_CAND" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
            exec "$PY_CAND" -B "$SETUP_PY" uninstall "$@"
        fi
    fi
done

STATE_BASE="${XDG_STATE_HOME:-$HOME/.local/state}/BeamNG-Earth-Autodrive-Fixes"
if [ -f "$STATE_BASE/last_target.txt" ]; then
    BNE_TARGET="$(head -n 1 "$STATE_BASE/last_target.txt" | tr -d '\r')"
    if [ -n "$BNE_TARGET" ] && [ -x "$BNE_TARGET" ]; then
        exec "$BNE_TARGET" --run-addon "$SETUP_PY" uninstall --self-exe "$BNE_TARGET" "$@"
    fi
fi

echo "Could not find Python 3.10+ to run uninstall. Install python3 and try again." >&2
exit 1
