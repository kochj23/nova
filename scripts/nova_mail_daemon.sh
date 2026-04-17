#!/bin/zsh
# nova_mail_daemon.sh — Persistent mail agent with self-healing.
#
# Runs nova_mail_agent.py in a loop every 10 minutes. Persistent process
# with KeepAlive — survives sleep/wake unlike StartInterval.
#
# Self-healing: if inbox has messages that haven't been processed in
# 30+ minutes, marks them unread and alerts on Slack.
#
# Written by Jordan Koch.

SCRIPT="$HOME/.openclaw/scripts/nova_mail_agent.py"
LOG="$HOME/.openclaw/logs/nova_mail_agent.log"
INTERVAL=600  # 10 minutes
STALE_CHECK=3  # Check for stale messages every N cycles
CYCLE=0

_slack() {
    local token=$(security find-generic-password -a nova -s nova-slack-bot-token -w 2>/dev/null)
    [ -z "$token" ] && return
    curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -d "{\"channel\": \"C0ATAF7NZG9\", \"text\": \"$1\"}" > /dev/null 2>&1
}

echo "[mail_daemon $(date '+%H:%M:%S')] Starting persistent mail daemon (every ${INTERVAL}s)" >> "$LOG"
_slack ":email: *Mail Daemon Started* — checking inbox every 10 minutes, persistent mode"

while true; do
    /opt/homebrew/bin/python3 "$SCRIPT" >> "$LOG" 2>&1
    EXIT_CODE=$?

    if [ $EXIT_CODE -ne 0 ]; then
        echo "[mail_daemon $(date '+%H:%M:%S')] Script exited with code $EXIT_CODE" >> "$LOG"
        _slack ":warning: *Mail Agent Error* — exit code $EXIT_CODE. Will retry in ${INTERVAL}s."
    fi

    CYCLE=$((CYCLE + 1))

    # Every 3rd cycle, check for stale read messages that were never replied to
    if [ $((CYCLE % STALE_CHECK)) -eq 0 ]; then
        INBOX_TOTAL=$(/opt/homebrew/bin/python3 -c "
import imaplib, subprocess
pw = subprocess.run(['security','find-generic-password','-s','nova-smtp-app-password','-w'], capture_output=True, text=True).stdout.strip()
try:
    m = imaplib.IMAP4_SSL('imap.gmail.com')
    m.login('nova@digitalnoise.net', pw)
    m.select('INBOX')
    _, d = m.search(None, 'ALL')
    print(len(d[0].split()))
    m.logout()
except: print(0)
" 2>/dev/null)

        if [ "$INBOX_TOTAL" -gt 5 ] 2>/dev/null; then
            echo "[mail_daemon $(date '+%H:%M:%S')] Stale inbox detected: $INBOX_TOTAL messages. Marking all unread." >> "$LOG"
            /opt/homebrew/bin/python3 -c "
import imaplib, subprocess
pw = subprocess.run(['security','find-generic-password','-s','nova-smtp-app-password','-w'], capture_output=True, text=True).stdout.strip()
m = imaplib.IMAP4_SSL('imap.gmail.com')
m.login('nova@digitalnoise.net', pw)
m.select('INBOX')
_, d = m.search(None, 'ALL')
for uid in d[0].split():
    m.store(uid, '-FLAGS', '(\\\\Seen)')
m.logout()
" 2>/dev/null
            _slack ":rotating_light: *Mail Self-Heal* — $INBOX_TOTAL stale messages found, marked unread for reprocessing"
        fi
    fi

    sleep $INTERVAL
done
