#!/bin/bash
# nova_quick_capture.sh — Send clipboard or typed text to Nova's vector memory.
#
# Usage:
#   nova_quick_capture.sh                  # Capture clipboard contents
#   nova_quick_capture.sh "remember this"  # Capture a specific string
#   nova_quick_capture.sh --prompt         # macOS dialog box for typed input
#   nova_quick_capture.sh --file /path     # Capture file contents
#
# Stores in vector memory with source="quick_capture" and posts
# confirmation to Slack DM.
#
# To set up as global hotkey:
#   1. Automator > Quick Action > Run Shell Script
#   2. Paste: ~/.openclaw/scripts/nova_quick_capture.sh --prompt
#   3. System Settings > Keyboard > Shortcuts > Services > assign hotkey
#   Or use Shortcuts app:
#   1. New Shortcut > Run Shell Script
#   2. Paste: ~/.openclaw/scripts/nova_quick_capture.sh --prompt
#   3. Assign keyboard shortcut in System Settings
#
# Written by Jordan Koch.

set -euo pipefail

SCRIPTS_DIR="$HOME/.openclaw/scripts"
VECTOR_URL="http://127.0.0.1:18790/remember"
SLACK_CHANNEL="D0AMPB3F4T0"  # Jordan's DM with Nova

# ── Input capture ────────────────────────────────────────────────────────────

capture_text() {
    if [[ "${1:-}" == "--prompt" ]]; then
        # macOS dialog for typed input
        TEXT=$(osascript -e 'display dialog "Quick capture for Nova:" default answer "" buttons {"Cancel", "Save"} default button "Save" with title "Nova Quick Capture"' -e 'text returned of result' 2>/dev/null)
        if [[ -z "$TEXT" ]]; then
            echo "Cancelled."
            exit 0
        fi
    elif [[ "${1:-}" == "--file" ]]; then
        if [[ -z "${2:-}" || ! -f "$2" ]]; then
            echo "Usage: nova_quick_capture.sh --file /path/to/file"
            exit 1
        fi
        TEXT=$(head -c 10000 "$2")
        TEXT="[File: $(basename "$2")] $TEXT"
    elif [[ -n "${1:-}" ]]; then
        # Direct text argument
        TEXT="$1"
    else
        # Clipboard
        TEXT=$(pbpaste 2>/dev/null)
        if [[ -z "$TEXT" ]]; then
            echo "Clipboard is empty."
            # Try dialog as fallback
            TEXT=$(osascript -e 'display dialog "Clipboard empty. Type your note:" default answer "" buttons {"Cancel", "Save"} default button "Save" with title "Nova Quick Capture"' -e 'text returned of result' 2>/dev/null || true)
            if [[ -z "$TEXT" ]]; then
                exit 0
            fi
        fi
    fi
    echo "$TEXT"
}

# ── Store in vector memory ───────────────────────────────────────────────────

store_memory() {
    local text="$1"
    local timestamp
    timestamp=$(date +"%Y-%m-%d %H:%M:%S")
    local truncated="${text:0:500}"

    # Store in vector memory
    local payload
    payload=$(python3 -c "
import json, sys
text = sys.stdin.read()
print(json.dumps({
    'text': text,
    'source': 'quick_capture',
    'metadata': {'timestamp': '$timestamp', 'type': 'quick_capture'}
}))
" <<< "$text")

    local result
    result=$(curl -s -w "%{http_code}" -o /dev/null \
        -X POST "$VECTOR_URL" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --connect-timeout 3 --max-time 10 2>/dev/null)

    if [[ "$result" == "200" || "$result" == "201" ]]; then
        echo "stored"
    else
        echo "failed:$result"
    fi
}

# ── Notify via Slack DM ─────────────────────────────────────────────────────

notify_slack() {
    local text="$1"
    local status="$2"
    local preview="${text:0:100}"
    if [[ ${#text} -gt 100 ]]; then
        preview="${preview}..."
    fi

    if [[ "$status" == "stored" ]]; then
        local message="Captured: _${preview}_"
    else
        local message="Failed to capture: _${preview}_ ($status)"
    fi

    bash "$SCRIPTS_DIR/nova_slack_post.sh" "$message" "$SLACK_CHANNEL" 2>/dev/null || true
}

# ── macOS notification ───────────────────────────────────────────────────────

notify_macos() {
    local status="$1"
    if [[ "$status" == "stored" ]]; then
        osascript -e 'display notification "Saved to Nova memory" with title "Nova" sound name "Glass"' 2>/dev/null || true
    else
        osascript -e 'display notification "Failed to save — memory server may be down" with title "Nova" sound name "Basso"' 2>/dev/null || true
    fi
}

# ── Main ─────────────────────────────────────────────────────────────────────

TEXT=$(capture_text "$@")
if [[ -z "$TEXT" ]]; then
    echo "Nothing to capture."
    exit 0
fi

STATUS=$(store_memory "$TEXT")
notify_macos "$STATUS"
notify_slack "$TEXT" "$STATUS"

if [[ "$STATUS" == "stored" ]]; then
    echo "Captured ${#TEXT} chars to Nova memory."
else
    echo "Capture failed: $STATUS"
    exit 1
fi
