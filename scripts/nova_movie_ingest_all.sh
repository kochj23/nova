#!/bin/bash
#
# nova_movie_ingest_all.sh — Launch all movie franchise ingests in parallel via nohup.
#
# Each franchise runs as a separate background process.
# Logs go to /tmp/nova-movie-ingest/<franchise>.log
# Status updates to Slack #nova-notifications every 5 minutes.
#
# Usage:
#   ./nova_movie_ingest_all.sh
#   ./nova_movie_ingest_all.sh --dry-run
#
# Written by Jordan Koch.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INGEST_SCRIPT="$SCRIPT_DIR/nova_movie_script_ingest.py"
LOG_DIR="/tmp/nova-movie-ingest"
PYTHON="/opt/homebrew/bin/python3"
DRY_RUN=""

if [ "$1" = "--dry-run" ]; then
    DRY_RUN="--dry-run"
    echo "[orchestrator] DRY RUN mode"
fi

mkdir -p "$LOG_DIR"

# All franchises to process
FRANCHISES=(
    death_wish
    cannon_films
    van_damme
    men_in_black
    john_wick
    hunger_games
    oceans
    batman
    red_dawn
    taken
    bruce_lee
    evil_dead
    rambo
    ghostbusters
    uhf
    valley_girl
    decline_western_civ
    walken
    godfather
    i_remember_mama
    john_hughes
    john_waters
    rounders
    deniro
    hanks
    american_history_x
    stephen_king
    hellraiser
)

echo "============================================================"
echo "  Nova Movie Script Ingest — Parallel Launch"
echo "  $(date)"
echo "  Franchises: ${#FRANCHISES[@]}"
echo "  Log dir: $LOG_DIR"
echo "============================================================"
echo ""

PIDS=()
for franchise in "${FRANCHISES[@]}"; do
    echo "  Launching: $franchise"
    nohup "$PYTHON" "$INGEST_SCRIPT" --franchise "$franchise" $DRY_RUN \
        > "$LOG_DIR/${franchise}.log" 2>&1 &
    PIDS+=($!)
    # Stagger launches by 2 seconds to avoid Wikipedia rate limiting
    sleep 2
done

echo ""
echo "  All ${#FRANCHISES[@]} franchises launched."
echo "  PIDs: ${PIDS[*]}"
echo ""
echo "  Monitor progress:"
echo "    tail -f $LOG_DIR/*.log"
echo "    grep 'Complete\|ERROR' $LOG_DIR/*.log"
echo ""
echo "  Kill all:"
echo "    kill ${PIDS[*]}"
echo ""

# Save PIDs for later reference
echo "${PIDS[*]}" > "$LOG_DIR/pids.txt"
echo "${FRANCHISES[*]}" > "$LOG_DIR/franchises.txt"

# Wait for all to complete and report
echo "  Waiting for all processes to complete..."
FAILED=0
for i in "${!PIDS[@]}"; do
    wait "${PIDS[$i]}"
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        echo "  FAILED: ${FRANCHISES[$i]} (exit $EXIT_CODE)"
        ((FAILED++))
    fi
done

echo ""
echo "============================================================"
echo "  All processes complete."
echo "  Failed: $FAILED / ${#FRANCHISES[@]}"
echo "  $(date)"
echo "============================================================"

# Final summary from logs
echo ""
echo "  Per-franchise results:"
for franchise in "${FRANCHISES[@]}"; do
    RESULT=$(grep -o "memories_stored.*[0-9]" "$LOG_DIR/${franchise}.log" 2>/dev/null | tail -1)
    MEMORIES=$(grep "Complete:.*memories" "$LOG_DIR/${franchise}.log" 2>/dev/null | tail -1)
    if [ -n "$MEMORIES" ]; then
        echo "    $franchise: $MEMORIES"
    else
        STORED=$(grep -c "memories_stored" "$LOG_DIR/${franchise}.log" 2>/dev/null)
        echo "    $franchise: check $LOG_DIR/${franchise}.log"
    fi
done
