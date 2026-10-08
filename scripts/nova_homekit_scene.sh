#!/bin/bash
#
# nova_homekit_scene.sh — Execute a HomeKit scene via NovaControl API.
# Falls back to Shortcuts CLI if the API is unavailable.
#
# Usage: nova_homekit_scene.sh "Good Morning"
#        nova_homekit_scene.sh --list
#
# Written by Jordan Koch.

set -euo pipefail

SCENE_NAME="${1:-}"
API_URL="http://127.0.0.1:37400"

# Log a successful scene activation to Postgres. Guarded so a psql failure
# (network, auth, etc.) can never break scene execution — only log on success.
log_scene_activation() {
    local name="$1"
    # Bound as a psql variable (:'scene' = properly quoted literal) — a scene name containing $$
    # could otherwise break out of dollar-quoting and inject SQL.
    echo "INSERT INTO public.home_scene_activations (ts, scene_name) VALUES (now(), :'scene')" | \
        psql -h pg-primary.digitalnoise.net -d nova_ops -v scene="$name" \
        >/dev/null 2>&1 || true
}

if [ -z "$SCENE_NAME" ]; then
    echo "Usage: nova_homekit_scene.sh <scene_name>"
    echo "       nova_homekit_scene.sh --list"
    exit 1
fi

# List scenes
if [ "$SCENE_NAME" = "--list" ]; then
    result=$(curl -s --connect-timeout 3 "$API_URL/api/homekit/scenes" 2>/dev/null) || true
    if [ -n "$result" ]; then
        echo "$result"
    else
        # Fallback to Shortcuts
        shortcuts run "List HomeKit Scenes" --output-type public.plain-text 2>/dev/null || echo "[]"
    fi
    exit 0
fi

# Proteus rule (P1): refuse a scene that may lock doors / close the garage / arm an alarm
# unless Jordan confirmed it. Guard exit 0 = allowed; anything else (incl. python missing) = refuse.
GUARD_PY="$(command -v python3 || echo /opt/homebrew/bin/python3)"
if ! "$GUARD_PY" "$(dirname "$0")/nova_safety_guards.py" scene-check "$SCENE_NAME" >&2; then
    echo "{\"error\": \"refused by physical guard: scene '$SCENE_NAME'\"}" >&2
    exit 3
fi

# Kill switch (P8): Nova's actuations stop; the house holds its state.
if [ -e "$HOME/.openclaw/.autonomy-kill" ]; then
    echo "{\"error\": \"kill switch engaged: not running scene\"}" >&2
    exit 3
fi

# JSON-escape the scene name (backslash, then double quote) so it can't break the request body.
SCENE_JSON="${SCENE_NAME//\\/\\\\}"
SCENE_JSON="${SCENE_JSON//\"/\\\"}"

# Execute scene — try API first (2 retries with backoff on connection failure)
result=$(curl -s --connect-timeout 3 --retry 2 --retry-delay 1 --retry-connrefused -X POST \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"$SCENE_JSON\"}" \
    "$API_URL/api/homekit/scenes/execute" 2>/dev/null) || true

if echo "$result" | grep -q '"status" *: *"executed"'; then
    log_scene_activation "$SCENE_NAME"
    echo "$result"
    exit 0
fi

# Fallback to Shortcuts CLI
echo "API failed, trying Shortcuts CLI..." >&2
# (Tested inside `if` so set -e can't kill the script silently before the error JSON; 3 attempts, backoff.)
sc_ok=0
for delay in 1 2 0; do
    if echo "$SCENE_NAME" | shortcuts run "Execute HomeKit Scene" --input-type public.plain-text --output-type public.plain-text 2>/dev/null; then
        sc_ok=1; break
    fi
    [ "$delay" -gt 0 ] && sleep "$delay"
done

if [ "$sc_ok" -eq 1 ]; then
    log_scene_activation "$SCENE_NAME"
    echo "{\"status\": \"executed\", \"scene\": \"$SCENE_JSON\", \"backend\": \"Shortcuts CLI\"}"
else
    echo "{\"error\": \"Failed to execute scene '$SCENE_NAME'\"}" >&2
    exit 1
fi
