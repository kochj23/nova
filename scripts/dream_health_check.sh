#!/bin/bash
# dream_health_check.sh — Runs at 10am to verify the dream pipeline worked.
# Posts failures to Slack #nova-chat so Jordan knows immediately.
# Written by Jordan Koch.

set -eo pipefail

TODAY=$(date +%Y-%m-%d)
JOURNAL="$HOME/.openclaw/workspace/journal/dreams/${TODAY}.md"
PENDING="$HOME/.openclaw/workspace/journal/pending_delivery.json"
DEAD_LETTER="$HOME/.openclaw/workspace/journal/failed_deliveries/${TODAY}.json"
LOG="$HOME/.openclaw/logs/dream-pipeline.log"

failures=()

# Check 1: Was a dream generated at 2am?
if [ ! -f "$JOURNAL" ]; then
    failures+=("Dream generation failed — no journal file for $TODAY")
fi

# Check 2: Is pending_delivery.json still sitting around? (should be consumed by 9am)
if [ -f "$PENDING" ]; then
    pending_date=$(python3 -c "import json; print(json.load(open('$PENDING')).get('date','?'))" 2>/dev/null || echo "?")
    if [ "$pending_date" = "$TODAY" ]; then
        failures+=("Dream delivery failed — pending_delivery.json still exists for $TODAY (not consumed by 9am delivery)")
    fi
fi

# Check 3: Did delivery land in dead-letter queue?
if [ -f "$DEAD_LETTER" ]; then
    failures+=("Dream delivery exhausted retries — moved to dead-letter queue: $DEAD_LETTER")
fi

# Check 4: Is Ollama reachable?
if ! curl -s --connect-timeout 5 http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
    failures+=("Ollama is not reachable — dream generation will fail tonight")
fi

# Report
if [ ${#failures[@]} -eq 0 ]; then
    echo "[dream_health $(date +%H:%M:%S)] All checks passed for $TODAY"
    exit 0
fi

# Build failure message
MSG=":warning: *Dream Pipeline Health Check — $TODAY*\n"
for f in "${failures[@]}"; do
    MSG+="• ${f}\n"
done
MSG+="\n_Check logs: ~/.openclaw/logs/dream-pipeline.log_"

echo "[dream_health $(date +%H:%M:%S)] FAILURES DETECTED:"
for f in "${failures[@]}"; do
    echo "  - $f"
done

# Post to Slack+Discord
bash ~/.openclaw/scripts/nova_slack_post.sh "$(echo -e "$MSG")" "C0ATAF7NZG9"
echo "[dream_health $(date +%H:%M:%S)] Failure alert posted to Slack+Discord"
