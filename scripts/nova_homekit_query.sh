#!/bin/bash
#
# nova_homekit_query.sh — Query HomeKit via the Shortcuts CLI.
# Runs the "Nova HomeKit Status" Shortcut and outputs JSON to stdout.
#
# The Shortcut must be created in the Shortcuts app — see nova_home_watchdog.py comments.
# Written by Jordan Koch.

set -euo pipefail

SHORTCUT_NAME="Nova HomeKit Status"
OUTPUT_FILE="$HOME/.openclaw/workspace/state/nova_homekit_status.json"
MAX_RETRIES=3

for attempt in $(seq 1 $MAX_RETRIES); do
    shortcuts run "$SHORTCUT_NAME" --output-type public.plain-text --output "$OUTPUT_FILE" 2>&1 | head -5 >&2

    if [ -f "$OUTPUT_FILE" ] && [ -s "$OUTPUT_FILE" ]; then
        cat "$OUTPUT_FILE"
        rm -f "$OUTPUT_FILE"
        exit 0
    fi

    if [ "$attempt" -lt "$MAX_RETRIES" ]; then
        sleep 2
    fi
done

echo '{"error": "HomeKit query failed after 3 attempts", "accessories": []}' >&2
echo "[]"
exit 1
