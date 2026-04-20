#!/bin/zsh
# nova_youtube_queue.sh — Queue YouTube playlists for sequential ingest.
# Waits for each to finish before starting the next.
#
# Written by Jordan Koch.

SCRIPTS="$HOME/.openclaw/scripts"
LOGS="$HOME/.openclaw/logs"

# Wait for Lilith playlist (PID 82784) to finish
echo "[$(date '+%H:%M:%S')] Waiting for Lilith playlist to finish..."
while kill -0 82784 2>/dev/null; do sleep 30; done

echo "[$(date '+%H:%M:%S')] Starting Western Esotericism playlist (89 videos, 45 hrs)..."
python3 "$SCRIPTS/nova_youtube_playlist_ingest.py" \
    "https://www.youtube.com/playlist?list=PLZ__PGORcBKx5asl6GprS9p_P0sBng4bV" \
    --source occult \
    --tag "Western Esotericism" \
    >> "$LOGS/youtube-esotericism-ingest.log" 2>&1

echo "[$(date '+%H:%M:%S')] Western Esotericism playlist complete."
