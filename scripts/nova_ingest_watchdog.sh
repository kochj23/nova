#!/bin/zsh
# nova_ingest_watchdog.sh — Safety net for the ingest pipeline.
# Checks every 30 min if the chain stalled. If batch 2 or 3 died
# without completing, restarts them. Posts status to Slack.
#
# Self-terminates when all batches are done.
#
# Written by Jordan Koch.

SCRIPTS="$HOME/.openclaw/scripts"
LOGS="$HOME/.openclaw/logs"
TVSHOWS="/Volumes/external/videos/TVShows"
SLACK_NOTIFY="C0ATAF7NZG9"

_slack() {
    local token=$(security find-generic-password -a nova -s nova-slack-bot-token -w 2>/dev/null)
    [ -z "$token" ] && return
    curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -d "{\"channel\": \"$SLACK_NOTIFY\", \"text\": \"$1\"}" > /dev/null 2>&1
}

_is_running() {
    pgrep -f "$1" > /dev/null 2>&1
}

while true; do
    sleep 1800  # 30 minutes

    b1_running=$(_is_running "nova_tvshow_ingest.*Born" && echo yes || echo no)
    b2_running=$(_is_running "nova_vehicles_batch2" && echo yes || echo no)
    b2_ingest=$(_is_running "nova_tvshow_ingest.*vehicles" && echo yes || echo no)
    b3_running=$(_is_running "nova_homerepair_batch3" && echo yes || echo no)
    b3_ingest=$(_is_running "nova_tvshow_ingest.*home_repair" && echo yes || echo no)
    b4_ingest=$(_is_running "nova_tvshow_ingest.*cooking" && echo yes || echo no)
    qmon=$(_is_running "nova_queue_monitor" && echo yes || echo no)

    # Check if everything is done
    if [[ "$b1_running" == "no" && "$b2_running" == "no" && "$b3_running" == "no" && "$b2_ingest" == "no" && "$b3_ingest" == "no" && "$b4_ingest" == "no" ]]; then
        # Check log files for completion markers
        if grep -q "complete" "$LOGS/tvshow-ingest-batch3-homerepair.log" 2>/dev/null || \
           grep -q "complete" "$LOGS/tvshow-ingest-batch4-cooking.log" 2>/dev/null; then
            _slack ":white_check_mark: *Ingest Watchdog* — All batches complete. Watchdog exiting."
            exit 0
        fi

        # Batch 2 queue died but batch 2 never ran
        if [[ "$b2_running" == "no" ]] && ! grep -q "Starting batch 2" "$LOGS/tvshow-batch2-queue.log" 2>/dev/null; then
            # Batch 1 finished but batch 2 queue process died — restart batch 2 directly
            _slack ":warning: *Ingest Watchdog* — Batch 2 queue died. Restarting batch 2 directly..."
            nohup python3 "$SCRIPTS/nova_tvshow_ingest.py" \
                "$TVSHOWS/American Muscle Car" "$TVSHOWS/Car Craft" "$TVSHOWS/Chasing Classic Cars" \
                "$TVSHOWS/Classic Car Restoration" "$TVSHOWS/Dream Car Garage" "$TVSHOWS/FourWheeler" \
                "$TVSHOWS/Hot Rod Garage" "$TVSHOWS/Hot Rod TV" "$TVSHOWS/JDM Legends" \
                "$TVSHOWS/Two Guys Garage" "$TVSHOWS/Victory By Design" "$TVSHOWS/Wheeler Dealers" \
                "$TVSHOWS/MotorWeek (1992)" "$TVSHOWS/Super 2NR TV" \
                --source vehicles >> "$LOGS/tvshow-ingest-batch2.log" 2>&1 &
        fi

        # Batch 3 queue died but batch 3 never ran
        if [[ "$b3_running" == "no" && "$b2_ingest" == "no" ]] && ! grep -q "Starting home repair" "$LOGS/tvshow-batch3-queue.log" 2>/dev/null; then
            _slack ":warning: *Ingest Watchdog* — Batch 3 queue died. Restarting batch 3 directly..."
            nohup "$SCRIPTS/nova_homerepair_batch3.sh" >> "$LOGS/tvshow-batch3-queue.log" 2>&1 &
        fi
    fi

    # Restart queue monitor if it died while queue still has items
    qdepth=$(redis-cli LLEN nova:memory:ingest 2>/dev/null)
    if [[ "$qmon" == "no" && "$qdepth" -gt 0 ]] 2>/dev/null; then
        _slack ":bar_chart: *Ingest Watchdog* — Queue monitor died (queue: ${qdepth}). Restarting..."
        nohup python3 "$SCRIPTS/nova_queue_monitor.py" >> "$LOGS/queue-monitor.log" 2>&1 &
    fi
done
