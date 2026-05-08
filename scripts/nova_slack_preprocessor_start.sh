#!/bin/zsh
# nova_slack_preprocessor_start.sh — Load Slack token from Keychain then exec preprocessor.
# Keeps the token out of plist files on disk.
# Written by Jordan Koch.

TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w 2>/dev/null)

MAX_RETRIES=8
RETRIES=0
DELAY=5
while [ -z "$TOKEN" ] && [ $RETRIES -lt $MAX_RETRIES ]; do
    echo "[preprocessor_start] Keychain not ready, retry $((RETRIES+1))/$MAX_RETRIES (wait ${DELAY}s)..." >&2
    sleep $DELAY
    TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w 2>/dev/null)
    RETRIES=$((RETRIES + 1))
    DELAY=$((DELAY < 30 ? DELAY + 5 : 30))
done

if [ -z "$TOKEN" ]; then
    echo "[preprocessor_start] FATAL: Keychain still locked after $MAX_RETRIES retries" >&2
    exit 1
fi

export NOVA_SLACK_BOT_TOKEN="$TOKEN"
exec /opt/homebrew/bin/python3 /Users/kochj/.openclaw/scripts/nova_slack_preprocessor.py
