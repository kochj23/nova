#!/usr/bin/env bash
# nova_voice.sh — The Scotty Moment
#
# "Computer???" — Say "Nova" anywhere near your Mac and she answers
# through the nearest HomePod.
#
# Pipeline:
#   whisper-stream (live mic, VAD) → detect "nova" hotword → strip hotword
#   → POST full utterance to Nova via Slack → poll for response
#   → speak response through HomePod
#
# Usage:
#   ~/.openclaw/scripts/nova_voice.sh [room_name]
#   room_name defaults to "Office"
#
# Setup (one time):
#   Grant Terminal microphone access in System Settings > Privacy > Microphone
#   Download whisper model: see instructions at top of this file
#
# Written by Jordan Koch.

set -uo pipefail

WHISPER_MODEL="/opt/homebrew/share/whisper-cpp/ggml-base.en.bin"
SLACK_TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w)
SLACK_CHAN="C0AMNQ5GX70"
HOMEPOD_ROOM="${1:-Office}"
SCRIPTS="$HOME/.openclaw/scripts"
POLL_TIMEOUT=60    # seconds to wait for Nova's response
POLL_INTERVAL=2    # seconds between Slack polls

log() { echo "[nova_voice $(date '+%H:%M:%S')] $*" >&2; }

# ── Check model ───────────────────────────────────────────────────────────────
if [ ! -f "$WHISPER_MODEL" ]; then
    echo "Whisper model not found at $WHISPER_MODEL"
    echo "Download it with:"
    echo "  curl -L https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin \\"
    echo "       -o $WHISPER_MODEL"
    exit 1
fi

log "Nova voice interface starting. Listening for 'Nova...' in room: $HOMEPOD_ROOM"
log "Say 'Nova, <your message>' clearly. Press Ctrl+C to stop."
echo ""
echo "  👂  Listening... (say 'Nova, ...')"
echo ""

# ── Post to Slack & get Nova's response ───────────────────────────────────────
ask_nova() {
    local utterance="$1"
    log "Sending to Nova: $utterance"

    # Get timestamp before posting (to find Nova's reply)
    local ts_before
    ts_before=$(date +%s)

    # Post to Slack
    /opt/homebrew/bin/python3 - "$utterance" "$SLACK_TOKEN" "$SLACK_CHAN" << 'PYEOF'
import sys, json, urllib.request
text, token, chan = sys.argv[1], sys.argv[2], sys.argv[3]
# Prefix so it's clear this is a voice message
message = f"🎙️ [voice] {text}"
data = json.dumps({"channel": chan, "text": message, "mrkdwn": False}).encode()
req = urllib.request.Request(
    "https://slack.com/api/chat.postMessage", data=data,
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
)
with urllib.request.urlopen(req, timeout=15) as r:
    result = json.loads(r.read())
    if result.get("ok"):
        print(result["ts"])
    else:
        print("ERROR: " + result.get("error","unknown"), file=sys.stderr)
PYEOF

    # Poll Slack for Nova's response (bot messages after our post)
    local deadline=$((ts_before + POLL_TIMEOUT))
    local response=""

    while [ $(date +%s) -lt $deadline ]; do
        sleep $POLL_INTERVAL
        response=$(/opt/homebrew/bin/python3 - "$SLACK_TOKEN" "$SLACK_CHAN" "$ts_before" << 'PYEOF'
import sys, json, urllib.request, time
token, chan, ts_before = sys.argv[1], sys.argv[2], float(sys.argv[3])
oldest = str(int(ts_before))
url = f"https://slack.com/api/conversations.history?channel={chan}&oldest={oldest}&limit=10"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    messages = data.get("messages", [])
    # Find a bot message (Nova) that's newer than our post
    for msg in reversed(messages):
        bot_id = msg.get("bot_id") or msg.get("subtype") == "bot_message"
        if bot_id and float(msg.get("ts", 0)) > ts_before:
            text = msg.get("text", "")
            # Strip Slack formatting for TTS
            import re
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"[*_`]", "", text)
            print(text[:500])
            sys.exit(0)
except Exception as e:
    print("", file=sys.stderr)
PYEOF
)
        if [ -n "$response" ] && [ "$response" != "" ]; then
            log "Got response, speaking through $HOMEPOD_ROOM"
            # Speak through HomePod
            /opt/homebrew/bin/python3 "$SCRIPTS/nova_homepod.py" say "$HOMEPOD_ROOM" "$response" 2>/dev/null &
            # Also speak locally via macOS say (backup)
            say -v Samantha "$response" &
            return 0
        fi
    done

    log "No response received within ${POLL_TIMEOUT}s"
    say -v Samantha "I didn't get a response. Try again."
}

# ── Main loop — whisper-stream with VAD ───────────────────────────────────────
# whisper-stream outputs transcription lines as it detects speech.
# We buffer lines and look for the hotword "nova".

WHISPER_CMD=(
    whisper-stream
    -m "$WHISPER_MODEL"
    -c 0                 # explicitly use MX Brio (device 0) — avoids Teams Audio 470 error
    --step 500
    --length 8000
    --keep 200
    --vad-thold 0.65
    --freq-thold 100
    --language en
    --threads 4
)

"${WHISPER_CMD[@]}" 2>/dev/null | while IFS= read -r line; do
    # Strip ANSI escape codes (whisper-stream uses [2K erase-line sequences)
    # Then strip timestamps like [00:00:00.000 --> 00:00:02.000]
    text=$(echo "$line" \
        | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' \
        | sed 's/\[.*-->[^]]*\]//g' \
        | sed 's/^[[:space:]]*//' \
        | tr '[:upper:]' '[:lower:]')

    [ -z "$text" ] && continue

    # Check for hotword
    if echo "$text" | grep -qi "nova"; then
        # Strip the hotword and everything before it
        utterance=$(echo "$text" | sed 's/.*nova[,.]* *//i')
        utterance=$(echo "$utterance" | sed 's/^[[:space:]]*//')

        if [ -n "$utterance" ] && [ ${#utterance} -gt 3 ]; then
            echo "  🗣️  Heard: $utterance"
            ask_nova "$utterance"
            echo ""
            echo "  👂  Listening... (say 'Nova, ...')"
        fi
    fi
done
