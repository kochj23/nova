#!/bin/bash
# nova_video_batch.sh — Batch transcribe all videos with audio
# Reads list of video files from stdin or argument file
# Runs transcript-only (no vision) to maximize throughput
# Posts progress to Slack every 10 videos
#
# Usage:
#   nova_video_batch.sh /tmp/videos_with_audio.txt
#   find /path -name "*.mp4" | nova_video_batch.sh
#
# Written by Jordan Koch.

SCRIPTS="$HOME/.openclaw/scripts"
PYTHON="/opt/homebrew/bin/python3"
LOG="/Volumes/Data/nova-video-batch.log"
SLACK_SCRIPT="$SCRIPTS/nova_slack_post.sh"

INPUT="${1:-/dev/stdin}"
TOTAL=$(wc -l < "$INPUT" | tr -d ' ')
COUNT=0
SUCCESS=0
ERRORS=0
START=$(date +%s)

echo "[$(date '+%H:%M:%S')] Starting batch transcription of $TOTAL videos" | tee -a "$LOG"
bash "$SLACK_SCRIPT" "Video batch transcription started: $TOTAL videos with audio" 2>/dev/null

while IFS= read -r video; do
    [ -z "$video" ] && continue
    COUNT=$((COUNT + 1))

    echo "[$(date '+%H:%M:%S')] ($COUNT/$TOTAL) Processing: $(basename "$video")" | tee -a "$LOG"

    $PYTHON "$SCRIPTS/nova_video_ingest.py" "$video" --transcript-only 2>&1 | tee -a "$LOG"

    if [ $? -eq 0 ]; then
        SUCCESS=$((SUCCESS + 1))
    else
        ERRORS=$((ERRORS + 1))
    fi

    # Progress update every 10 videos
    if [ $((COUNT % 10)) -eq 0 ]; then
        ELAPSED=$(( $(date +%s) - START ))
        RATE=$(echo "scale=1; $COUNT / ($ELAPSED / 60)" | bc 2>/dev/null || echo "?")
        REMAINING=$(( TOTAL - COUNT ))
        bash "$SLACK_SCRIPT" "Video transcription: $COUNT/$TOTAL ($SUCCESS ok, $ERRORS err, $RATE/min)" 2>/dev/null
        echo "[$(date '+%H:%M:%S')] Progress: $COUNT/$TOTAL, $SUCCESS ok, $ERRORS err" | tee -a "$LOG"
    fi

done < "$INPUT"

ELAPSED=$(( $(date +%s) - START ))
MINS=$((ELAPSED / 60))

echo "[$(date '+%H:%M:%S')] DONE: $SUCCESS/$TOTAL transcribed, $ERRORS errors, ${MINS}m elapsed" | tee -a "$LOG"
bash "$SLACK_SCRIPT" "Video batch transcription complete: $SUCCESS/$TOTAL transcribed, $ERRORS errors, ${MINS}m" 2>/dev/null
