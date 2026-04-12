#!/bin/bash
# nova_log_rotate.sh — Rotate Nova logs that exceed 5MB.
# Keeps 1 backup (.1) and truncates the active log.
# Runs daily at 3:30am via launchd.
# Written by Jordan Koch.

LOG_DIR="$HOME/.openclaw/logs"
MAX_SIZE=$((5 * 1024 * 1024))  # 5MB

rotated=0

for logfile in "$LOG_DIR"/*.log; do
    [ -f "$logfile" ] || continue
    size=$(stat -f %z "$logfile" 2>/dev/null || echo 0)
    if [ "$size" -gt "$MAX_SIZE" ]; then
        # Keep one backup
        mv "$logfile" "${logfile}.1"
        touch "$logfile"
        rotated=$((rotated + 1))
        echo "[log_rotate $(date +%H:%M:%S)] Rotated: $(basename "$logfile") ($(( size / 1024 / 1024 ))MB)"
    fi
done

if [ "$rotated" -eq 0 ]; then
    echo "[log_rotate $(date +%H:%M:%S)] No logs needed rotation"
fi
