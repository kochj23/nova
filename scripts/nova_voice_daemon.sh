#!/usr/bin/env bash
# nova_voice_daemon.sh — Background voice interface daemon
# Runs as a launchd service (net.digitalnoise.nova-voice).
# Say "Nova, <question>" anywhere near the mic — response plays through HomePod.
# Logs to ~/.openclaw/logs/nova-voice.log

WHISPER_MODEL="/opt/homebrew/share/whisper-cpp/ggml-base.en.bin"
SLACK_TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w)
SLACK_CHAN="C0AMNQ5GX70"
HOMEPOD_ROOM="${NOVA_VOICE_ROOM:-Office}"
SCRIPTS="$HOME/.openclaw/scripts"
LOG_FILE="$HOME/.openclaw/logs/nova-voice.log"
POLL_TIMEOUT=90
POLL_INTERVAL=2

mkdir -p "$HOME/.openclaw/logs"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

log "Nova voice daemon starting. Room: $HOMEPOD_ROOM"

if [ ! -f "$WHISPER_MODEL" ]; then
    log "ERROR: Whisper model not found at $WHISPER_MODEL"
    exit 1
fi

ask_nova() {
    local utterance="$1"
    log "Heard: $utterance"
    local ts_before
    ts_before=$(date +%s)

    /opt/homebrew/bin/python3 "$SCRIPTS/_voice_slack_post.py" \
        "$utterance" "$SLACK_TOKEN" "$SLACK_CHAN"

    local deadline=$((ts_before + POLL_TIMEOUT))
    while [ $(date +%s) -lt $deadline ]; do
        sleep $POLL_INTERVAL
        local response
        response=$(/opt/homebrew/bin/python3 "$SCRIPTS/_voice_slack_poll.py" \
            "$SLACK_TOKEN" "$SLACK_CHAN" "$ts_before")
        if [ -n "$response" ]; then
            log "Response: ${response:0:120}"
            /opt/homebrew/bin/python3 "$SCRIPTS/nova_homepod.py" \
                say "$HOMEPOD_ROOM" "$response" 2>>"$LOG_FILE" &
            /usr/bin/say -v Samantha "$response" &
            return 0
        fi
    done
    log "No response within ${POLL_TIMEOUT}s"
    /usr/bin/say -v Samantha "Nova didn't respond. Try again."
}

# Detect MX Brio device number dynamically (order shifts when iPhone/headphones connect)
MIC_DEVICE=$(/opt/homebrew/bin/python3 "$SCRIPTS/_voice_find_device.py" "MX Brio" 2>/dev/null || echo 0)
log "Listening for 'Nova...' on device $MIC_DEVICE (MX Brio preferred)"

whisper-stream \
    -m "$WHISPER_MODEL" \
    -c "$MIC_DEVICE" \
    --step 500 \
    --length 8000 \
    --keep 200 \
    --vad-thold 0.65 \
    --freq-thold 100 \
    --language en \
    --threads 4 \
    2>>"$LOG_FILE" | while IFS= read -r line; do

    text=$(echo "$line" \
        | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' \
        | sed 's/\[.*-->[^]]*\]//g' \
        | sed 's/^[[:space:]]*//' \
        | tr '[:upper:]' '[:lower:]')
    [ -z "$text" ] && continue
    [ "$text" = "[blank_audio]" ] && continue

    if echo "$text" | grep -qi "nova"; then
        utterance=$(echo "$text" | sed 's/.*nova[,.]* *//i' | sed 's/^[[:space:]]*//')
        if [ -n "$utterance" ] && [ ${#utterance} -gt 3 ]; then
            ask_nova "$utterance"
        fi
    fi
done

log "whisper-stream exited — launchd will restart"
