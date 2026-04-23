#!/bin/bash
# nova_session_reset.sh — Gracefully reset Nova's session when context bloat causes timeouts.
#
# Usage:
#   nova_session_reset.sh              # reset if session > 5MB (default threshold)
#   nova_session_reset.sh --force      # reset unconditionally
#   nova_session_reset.sh --check      # print session size and exit (no reset)
#   nova_session_reset.sh --threshold 8 # reset if session > 8MB
#
# What it does:
#   1. Finds Nova's active session file
#   2. Checks its size against threshold
#   3. Archives it with a timestamp (does not delete — history is preserved)
#   4. Posts a Slack notification so Jordan knows the session was reset
#
# Written by Jordan Koch / kochj23

set -euo pipefail

SESSIONS_DIR="$HOME/.openclaw/agents/main/sessions"
THRESHOLD_MB=20
FORCE=false
CHECK_ONLY=false
# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)      FORCE=true; shift ;;
        --check)      CHECK_ONLY=true; shift ;;
        --threshold)  THRESHOLD_MB="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# ── Find active session (largest non-deleted .jsonl) ─────────────────────────
ACTIVE_SESSION=$(find "$SESSIONS_DIR" -name "*.jsonl" \
    ! -name "*.deleted*" ! -name "*.reset*" ! -name "*.bak*" \
    -exec ls -s {} \; | sort -rn | head -1 | awk '{print $2}')

if [[ -z "$ACTIVE_SESSION" ]]; then
    echo "[session_reset] No active session found."
    exit 0
fi

SESSION_BYTES=$(stat -f%z "$ACTIVE_SESSION" 2>/dev/null || stat -c%s "$ACTIVE_SESSION")
SESSION_MB=$(echo "scale=1; $SESSION_BYTES / 1048576" | bc)
SESSION_NAME=$(basename "$ACTIVE_SESSION")

echo "[session_reset] Active session: $SESSION_NAME"
echo "[session_reset] Size: ${SESSION_MB}MB (threshold: ${THRESHOLD_MB}MB)"

if [[ "$CHECK_ONLY" == true ]]; then
    exit 0
fi

# ── Decide whether to reset ───────────────────────────────────────────────────
if [[ "$FORCE" == false ]]; then
    # Compare sizes using Python for reliable float comparison
    SHOULD_RESET=$(python3 -c "print('yes' if $SESSION_MB > $THRESHOLD_MB else 'no')")
    if [[ "$SHOULD_RESET" == "no" ]]; then
        echo "[session_reset] Session within threshold — no reset needed."
        exit 0
    fi
fi

# ── Archive the session ───────────────────────────────────────────────────────
TIMESTAMP=$(date -u +"%Y-%m-%dT%H-%M-%S")
ARCHIVE_NAME="${ACTIVE_SESSION%.jsonl}.reset.${TIMESTAMP}.jsonl"
mv "$ACTIVE_SESSION" "$ARCHIVE_NAME"
echo "[session_reset] Archived to: $(basename "$ARCHIVE_NAME")"

# ── Restart the gateway so it picks up the fresh session ─────────────────────
launchctl kickstart -k "gui/$(id -u)/ai.openclaw.gateway" 2>/dev/null || true
echo "[session_reset] Gateway restarted."

# ── Slack notification ────────────────────────────────────────────────────────
MSG="🔄 *Nova session reset* — context was ${SESSION_MB}MB (threshold ${THRESHOLD_MB}MB). Archived \`$(basename "$ARCHIVE_NAME")\`. Fresh session started."
bash ~/.openclaw/scripts/nova_slack_post.sh "$MSG" "C0ATAF7NZG9"
echo "[session_reset] Slack+Discord notification sent."

echo "[session_reset] Done. Nova has a fresh session."
