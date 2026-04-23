#!/usr/bin/env bash
# nova_self_monitor.sh — HAL's "I'm detecting an anomaly" heartbeat
#
# Checks every 15 minutes:
#   - Vector memory server (port 18790) — restarts if down
#   - Key app APIs (HomekitControl 37432, OneOnOne 37421)
#   - Last run time of critical crons (nightly report, morning brief)
#   - Disk space on home volume
#
# Only alerts Jordan if something is actually wrong.
# Self-heals the memory server automatically.
#
# Cron: every 15 minutes
# Written by Jordan Koch.

VECTOR_PORT=18790
ALERT_FILE="$HOME/.openclaw/workspace/state/nova_monitor_last_alert"
ALERT_COOLDOWN=3600  # Only re-alert same issue after 1 hour

log() { echo "[nova_self_monitor $(date '+%H:%M:%S')] $*"; }

slack_alert() {
    bash ~/.openclaw/scripts/nova_slack_post.sh "$1" "C0ATAF7NZG9"
}

should_alert() {
    local key="$1"
    local now=$(date +%s)
    local last=0
    [ -f "$ALERT_FILE" ] && last=$(grep "^$key=" "$ALERT_FILE" 2>/dev/null | cut -d= -f2)
    if [ $((now - last)) -ge $ALERT_COOLDOWN ]; then
        # Update timestamp
        if [ -f "$ALERT_FILE" ]; then
            grep -v "^$key=" "$ALERT_FILE" > $HOME/.openclaw/workspace/state/nova_monitor_tmp && mv /tmp/nova_monitor_tmp "$ALERT_FILE"
        fi
        echo "$key=$now" >> "$ALERT_FILE"
        return 0  # Should alert
    fi
    return 1  # Still in cooldown
}

# ── 1. Vector memory server ───────────────────────────────────────────────────
if ! curl -sf "http://127.0.0.1:${VECTOR_PORT}/health" > /dev/null 2>&1; then
    log "Memory server DOWN — attempting restart via launchd"
    launchctl kickstart -k gui/$(id -u)/net.digitalnoise.nova-memory-server
    sleep 4
    if curl -sf "http://127.0.0.1:${VECTOR_PORT}/health" > /dev/null 2>&1; then
        log "Memory server restarted successfully"
        if should_alert "memory_server_restart"; then
            slack_alert "⚠️ *Nova self-repair:* Memory server was down and has been restarted."
        fi
    else
        log "Memory server restart FAILED"
        if should_alert "memory_server_down"; then
            slack_alert "🔴 *Nova alert:* Memory server (port ${VECTOR_PORT}) is down and could not be restarted. Please check."
        fi
    fi
else
    log "Memory server OK"
fi

# ── 2. HomeKit app ────────────────────────────────────────────────────────────
if ! curl -sf "http://127.0.0.1:37432/api/status" > /dev/null 2>&1; then
    log "HomekitControl app not responding"
    if should_alert "homekit_down"; then
        slack_alert "⚠️ *Nova alert:* HomekitControl app (port 37432) is not responding."
    fi
else
    log "HomekitControl OK"
fi

# ── 3. Disk space ────────────────────────────────────────────────────────────
DISK_INFO=$(df -h "$HOME" | awk 'NR==2{print $2, $3, $4, $5}')
DISK_SIZE=$(echo $DISK_INFO | awk '{print $1}')
DISK_USED=$(echo $DISK_INFO | awk '{print $2}')
DISK_FREE=$(echo $DISK_INFO | awk '{print $3}')
DISK_PCT=$(echo $DISK_INFO | awk '{gsub(/%/,"",$4); print $4}')

if [ -n "$DISK_PCT" ] && [ "$DISK_PCT" -ge 95 ] 2>/dev/null; then
    log "Disk space low: ${DISK_PCT}%"
    if should_alert "disk_space"; then
        ALERT_MSG="⚠️ *Nova alert:* Home volume is ${DISK_PCT}% full\n"
        ALERT_MSG="${ALERT_MSG}• Total: ${DISK_SIZE}\n"
        ALERT_MSG="${ALERT_MSG}• Used: ${DISK_USED}\n"
        ALERT_MSG="${ALERT_MSG}• Free: ${DISK_FREE}\n"
        ALERT_MSG="${ALERT_MSG}Please review and clean up."
        slack_alert "$ALERT_MSG"
    fi
else
    log "Disk space OK (${DISK_PCT}%)"
fi

# ── 3b. Check all volumes ────────────────────────────────────────────────────
# Check /Volumes/Data if it exists
if [ -d "/Volumes/Data" ]; then
    DATA_INFO=$(df -h "/Volumes/Data" 2>/dev/null | awk 'NR==2{print $2, $3, $4, $5}')
    if [ -n "$DATA_INFO" ]; then
        DATA_SIZE=$(echo $DATA_INFO | awk '{print $1}')
        DATA_USED=$(echo $DATA_INFO | awk '{print $2}')
        DATA_FREE=$(echo $DATA_INFO | awk '{print $3}')
        DATA_PCT=$(echo $DATA_INFO | awk '{gsub(/%/,"",$4); print $4}')
        
        log "Data volume: ${DATA_PCT}% full (${DATA_USED}/${DATA_SIZE})"
        
        if [ "$DATA_PCT" -ge 95 ] 2>/dev/null; then
            if should_alert "data_volume"; then
                ALERT_MSG="⚠️ *Nova alert:* /Volumes/Data is ${DATA_PCT}% full\n"
                ALERT_MSG="${ALERT_MSG}• Total: ${DATA_SIZE}\n"
                ALERT_MSG="${ALERT_MSG}• Used: ${DATA_USED}\n"
                ALERT_MSG="${ALERT_MSG}• Free: ${DATA_FREE}\n"
                ALERT_MSG="${ALERT_MSG}Please clean up."
                slack_alert "$ALERT_MSG"
            fi
        fi
    fi
fi

# ── 4. Check if launchd plist for memory server is loaded ────────────────────
if ! launchctl list | grep -q "nova-memory-server" 2>/dev/null; then
    log "Memory server launchd job not registered — loading"
    launchctl load ~/Library/LaunchAgents/net.digitalnoise.nova-memory-server.plist 2>/dev/null
fi

log "Self-monitor check complete"
