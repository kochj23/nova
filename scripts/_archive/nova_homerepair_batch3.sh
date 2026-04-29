#!/bin/zsh
# nova_homerepair_batch3.sh — Queue home repair show ingest batch 3.
# Waits for vehicle batch 2 to finish, then ingests Ask This Old House
# and This Old House into Nova's memory as home_repair source.
#
# Written by Jordan Koch.

SCRIPTS="$HOME/.openclaw/scripts"
LOGS="$HOME/.openclaw/logs"
TVSHOWS="/Volumes/external/videos/TVShows"
BATCH2_PID=49785

echo "[$(date '+%H:%M:%S')] Waiting for vehicle batch 2 (PID $BATCH2_PID) to finish..."

while kill -0 $BATCH2_PID 2>/dev/null; do
    sleep 60
done

echo "[$(date '+%H:%M:%S')] Batch 2 done. Starting home repair ingest..."

python3 "$SCRIPTS/nova_tvshow_ingest.py" \
    "$TVSHOWS/Ask This Old House (2002)" \
    "$TVSHOWS/This Old House (1979)" \
    "$TVSHOWS/Holmes On Homes" \
    --source home_repair \
    >> "$LOGS/tvshow-ingest-batch3-homerepair.log" 2>&1

echo "[$(date '+%H:%M:%S')] Batch 3 (home repair) complete. Starting batch 4 (cooking)..."

python3 "$SCRIPTS/nova_tvshow_ingest.py" \
    "$TVSHOWS/Iron Chef" \
    --source cooking \
    >> "$LOGS/tvshow-ingest-batch4-cooking.log" 2>&1

python3 "$SCRIPTS/nova_tvshow_ingest.py" \
    "$TVSHOWS/Oz & James Drink to Britain" \
    "$TVSHOWS/Oz and Jame's Big Wine Adventure " \
    --source cocktails \
    >> "$LOGS/tvshow-ingest-batch4-cooking.log" 2>&1

echo "[$(date '+%H:%M:%S')] Batch 4 (cooking/drinks) complete. Starting batch 5 (knowledge)..."

python3 "$SCRIPTS/nova_tvshow_ingest.py" \
    "$TVSHOWS/Connections" \
    --source history \
    >> "$LOGS/tvshow-ingest-batch5-knowledge.log" 2>&1

python3 "$SCRIPTS/nova_tvshow_ingest.py" \
    "$TVSHOWS/Jeopardy (1984)" \
    --source trivia \
    >> "$LOGS/tvshow-ingest-batch5-knowledge.log" 2>&1

python3 "$SCRIPTS/nova_tvshow_ingest.py" \
    "$TVSHOWS/History of Christianity" \
    --source religion \
    >> "$LOGS/tvshow-ingest-batch5-knowledge.log" 2>&1

echo "[$(date '+%H:%M:%S')] Batch 5 (knowledge) complete. Starting batch 6 (documentaries)..."

python3 "$SCRIPTS/nova_tvshow_ingest.py" \
    "/Volumes/external/videos/Documentary" \
    --source history \
    >> "$LOGS/tvshow-ingest-batch6-documentary.log" 2>&1

echo "[$(date '+%H:%M:%S')] Batch 6 (documentaries) complete. Starting batch 7 (music lyrics)..."

python3 "$SCRIPTS/nova_tvshow_ingest.py" \
    "/Volumes/external/videos/Youtube Music Videos" \
    --source music_lyrics \
    >> "$LOGS/tvshow-ingest-batch7-music.log" 2>&1

echo "[$(date '+%H:%M:%S')] Batch 7 (music lyrics) complete. Starting batch 8 (drag/street racing)..."

python3 "$SCRIPTS/nova_tvshow_ingest.py" \
    "$TVSHOWS/Roadkill" \
    "$TVSHOWS/Engine Masters" \
    "$TVSHOWS/NHRA Drag Racing (2010)" \
    "$TVSHOWS/Supercuda" \
    "$TVSHOWS/Build or Bust" \
    "$TVSHOWS/Modified" \
    --source drag_racing \
    >> "$LOGS/tvshow-ingest-batch8-dragracing.log" 2>&1

echo "[$(date '+%H:%M:%S')] Batch 8 (drag/street racing) complete. ALL BATCHES DONE."
