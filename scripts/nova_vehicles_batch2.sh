#!/bin/zsh
# nova_vehicles_batch2.sh — Queue vehicle show ingest batch 2.
# Waits for the "Born" shows ingest (PID 47210) to finish, then starts
# the next wave: all car/JDM/hot rod/NHRA/garage/victory/wheeler shows.
#
# Written by Jordan Koch.

SCRIPTS="$HOME/.openclaw/scripts"
LOGS="$HOME/.openclaw/logs"
TVSHOWS="/Volumes/external/videos/TVShows"
BORN_PID=47210

echo "[$(date '+%H:%M:%S')] Waiting for 'Born' shows ingest (PID $BORN_PID) to finish..."

# Wait for the Born shows to finish
while kill -0 $BORN_PID 2>/dev/null; do
    sleep 30
done

echo "[$(date '+%H:%M:%S')] Born shows done. Starting batch 2..."

python3 "$SCRIPTS/nova_tvshow_ingest.py" \
    "$TVSHOWS/American Muscle Car" \
    "$TVSHOWS/Car Craft" \
    "$TVSHOWS/Chasing Classic Cars" \
    "$TVSHOWS/Classic Car Restoration" \
    "$TVSHOWS/Dream Car Garage" \
    "$TVSHOWS/FourWheeler" \
    "$TVSHOWS/Hot Rod Garage" \
    "$TVSHOWS/Hot Rod TV" \
    "$TVSHOWS/JDM Legends" \
    "$TVSHOWS/Two Guys Garage" \
    "$TVSHOWS/Victory By Design" \
    "$TVSHOWS/Wheeler Dealers" \
    --source vehicles \
    >> "$LOGS/tvshow-ingest-batch2.log" 2>&1

echo "[$(date '+%H:%M:%S')] Batch 2 complete."
