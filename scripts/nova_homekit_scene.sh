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
    psql -h pg-primary.digitalnoise.net -d nova_ops \
        -c "INSERT INTO public.home_scene_activations (ts, scene_name) VALUES (now(), \$\$${name}\$\$)" \
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

# Execute scene — try API first
result=$(curl -s --connect-timeout 3 -X POST \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"$SCENE_NAME\"}" \
    "$API_URL/api/homekit/scenes/execute" 2>/dev/null) || true

if echo "$result" | grep -q '"status" *: *"executed"'; then
    log_scene_activation "$SCENE_NAME"
    echo "$result"
    exit 0
fi

# Fallback to Shortcuts CLI
echo "API failed, trying Shortcuts CLI..." >&2
echo "$SCENE_NAME" | shortcuts run "Execute HomeKit Scene" --input-type public.plain-text --output-type public.plain-text 2>/dev/null

if [ $? -eq 0 ]; then
    log_scene_activation "$SCENE_NAME"
    echo "{\"status\": \"executed\", \"scene\": \"$SCENE_NAME\", \"backend\": \"Shortcuts CLI\"}"
else
    echo "{\"error\": \"Failed to execute scene '$SCENE_NAME'\"}" >&2
    exit 1
fi
