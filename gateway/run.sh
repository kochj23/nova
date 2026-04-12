#!/bin/bash
# run.sh — Start Nova-NextGen Gateway
# Usage: ./run.sh [--port PORT] [--reload]
#
# Author: Jordan Koch

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${PORT:-34750}"
LOG_LEVEL="${LOG_LEVEL:-info}"

# Prefer ~/.nova_gateway/venv (LaunchAgent-safe), fall back to local .venv
if [ -d "$HOME/.nova_gateway/venv" ]; then
    source "$HOME/.nova_gateway/venv/bin/activate"
elif [ -d "$SCRIPT_DIR/.venv" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

# Parse args
RELOAD=""
for arg in "$@"; do
    case $arg in
        --port=*) PORT="${arg#*=}" ;;
        --reload)  RELOAD="--reload" ;;
        --debug)   LOG_LEVEL="debug" ;;
    esac
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Nova-NextGen AI Gateway"
echo "  http://localhost:${PORT}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

exec uvicorn nova_gateway.main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level "$LOG_LEVEL" \
    $RELOAD
