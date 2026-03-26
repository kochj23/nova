#!/bin/bash
#
# nova_homekit_query.sh — Query HomeKit via the Shortcuts CLI.
# Runs the "Nova HomeKit Status" Shortcut and outputs JSON to stdout.
#
# The Shortcut must be created in the Shortcuts app — see nova_home_watchdog.py comments.
# Written by Jordan Koch.

set -euo pipefail

SHORTCUT_NAME="Nova HomeKit Status"
OUTPUT_FILE="/tmp/nova_homekit_status.json"

# Run the shortcut and capture output
shortcuts run "$SHORTCUT_NAME" --output-type public.plain-text --output "$OUTPUT_FILE" 2>/dev/null

if [ ! -f "$OUTPUT_FILE" ] || [ ! -s "$OUTPUT_FILE" ]; then
    echo "[]"
    exit 0
fi

cat "$OUTPUT_FILE"
rm -f "$OUTPUT_FILE"
