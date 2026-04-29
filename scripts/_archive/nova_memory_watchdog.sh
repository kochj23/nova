#!/bin/bash
# nova_memory_watchdog.sh — Restart the vector memory server if it's down.
# Runs every 5 minutes via system crontab.
# Written by Jordan Koch.

MEMORY_SERVER="$HOME/.openclaw/memory_server.py"
LOG="$HOME/.openclaw/logs/memory-server.log"
ERR="$HOME/.openclaw/logs/memory-server-error.log"

# Check if server is healthy
response=$(curl -s --max-time 3 http://127.0.0.1:18790/health 2>/dev/null)
status=$(echo "$response" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','down'))" 2>/dev/null)

if [ "$status" != "ok" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Memory server down — restarting..."
    pkill -f memory_server.py 2>/dev/null
    sleep 2
    nohup /opt/homebrew/bin/python3 "$MEMORY_SERVER" >> "$LOG" 2>> "$ERR" &
    sleep 3
    status=$(curl -s --max-time 5 http://127.0.0.1:18790/health 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','failed'))" 2>/dev/null)
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Restart result: $status"
fi
