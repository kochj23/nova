#!/bin/zsh
# yt_download_forgotten_weapons.sh
# Downloads all Forgotten Weapons videos in Plex-compatible naming.
# Oldest episodes first, 720p max, 32.674s delay between downloads.
# Sends progress notifications to #nova-notifications every 5 minutes.

CHANNEL_URL="https://www.youtube.com/@ForgottenWeapons"
SHOW_NAME="Forgotten Weapons"
BASE_DIR="/Volumes/external/videos/TVShows"
SHOW_DIR="${BASE_DIR}/${SHOW_NAME}"
SEASON_DIR="${SHOW_DIR}/Season 01"
LOG_FILE="${HOME}/.openclaw/logs/yt_forgotten_weapons.log"
URLS_FILE="${HOME}/.openclaw/logs/yt_forgotten_weapons_urls.txt"
TITLES_FILE="${HOME}/.openclaw/logs/yt_forgotten_weapons_titles.txt"
DELAY=32.674
NOTIFY_INTERVAL=300

# Create directories
mkdir -p "${SEASON_DIR}"
mkdir -p "$(dirname "${LOG_FILE}")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "${LOG_FILE}"
}

send_slack() {
    local msg="$1"
    local token
    token=$(security find-generic-password -a nova -s nova-slack-bot-token -w 2>/dev/null)
    [[ -z "$token" ]] && return
    local payload
    payload=$(python3 -c "import json,sys; print(json.dumps({'channel':'C0ATAF7NZG9','text':sys.argv[1]}))" "$msg")
    curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -d "$payload" > /dev/null 2>&1
}

log "=== Forgotten Weapons Download Started ==="
log "Output: ${SEASON_DIR}"

# Step 1: Build URL and title lists (oldest first) if not already done
if [[ ! -s "${URLS_FILE}" ]]; then
    log "Fetching video list (oldest first, this may take a few minutes)..."

    # Fetch URLs oldest-first
    yt-dlp \
        --cookies-from-browser safari \
        --flat-playlist \
        --playlist-reverse \
        --print "%(webpage_url)s" \
        "${CHANNEL_URL}" \
        > "${URLS_FILE}" 2>> "${LOG_FILE}"

    # Fetch titles oldest-first (separate pass to avoid separator issues)
    yt-dlp \
        --cookies-from-browser safari \
        --flat-playlist \
        --playlist-reverse \
        --print "%(title)s" \
        "${CHANNEL_URL}" \
        > "${TITLES_FILE}" 2>> "${LOG_FILE}"

    URL_COUNT=$(wc -l < "${URLS_FILE}" | tr -d ' ')
    TITLE_COUNT=$(wc -l < "${TITLES_FILE}" | tr -d ' ')
    log "Fetched ${URL_COUNT} URLs and ${TITLE_COUNT} titles"
else
    log "Using existing URL list: $(wc -l < "${URLS_FILE}" | tr -d ' ') entries"
fi

TOTAL=$(wc -l < "${URLS_FILE}" | tr -d ' ')
log "Total episodes: ${TOTAL}"

send_slack ":clapper: *Forgotten Weapons download started*
Total episodes: ${TOTAL}
Output: ${SEASON_DIR}
Max quality: 720p | Delay: ${DELAY}s between episodes"

EPISODE=0
FAILED=0
LAST_NOTIFY=$(date +%s)

while IFS= read -r url; do
    EPISODE=$((EPISODE + 1))
    PADDED=$(printf "%02d" "${EPISODE}")

    # Get matching title from titles file
    TITLE=$(sed -n "${EPISODE}p" "${TITLES_FILE}" 2>/dev/null)
    [[ -z "$TITLE" ]] && TITLE="Episode ${EPISODE}"

    # Sanitize title: remove filesystem-unsafe characters
    SAFE_TITLE=$(echo "${TITLE}" | sed 's/[/:*?"<>|\\]//g' | sed 's/  */ /g' | sed 's/^ //;s/ $//')

    EPISODE_NAME="S01E${PADDED} - ${SAFE_TITLE}"
    OUTPUT_TEMPLATE="${SEASON_DIR}/${EPISODE_NAME}.%(ext)s"

    # Skip if already downloaded
    if ls "${SEASON_DIR}/S01E${PADDED} - "* 2>/dev/null | grep -q .; then
        log "SKIP [${EPISODE}/${TOTAL}] Already exists: ${EPISODE_NAME}"
        continue
    fi

    log "START [${EPISODE}/${TOTAL}] ${EPISODE_NAME}"

    yt-dlp \
        --cookies-from-browser safari \
        --format "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best" \
        --merge-output-format mp4 \
        --output "${OUTPUT_TEMPLATE}" \
        --no-playlist \
        --retries 5 \
        --fragment-retries 5 \
        --retry-sleep 10 \
        --write-thumbnail \
        --convert-thumbnails jpg \
        --ignore-errors \
        --no-warnings \
        "${url}" >> "${LOG_FILE}" 2>&1

    if [[ $? -eq 0 ]]; then
        log "OK   [${EPISODE}/${TOTAL}] ${EPISODE_NAME}"
    else
        FAILED=$((FAILED + 1))
        log "FAIL [${EPISODE}/${TOTAL}] ${EPISODE_NAME}"
    fi

    # Slack notification every 5 minutes
    NOW=$(date +%s)
    if (( NOW - LAST_NOTIFY >= NOTIFY_INTERVAL )); then
        PCT=$(python3 -c "print(f'{${EPISODE}*100/${TOTAL}:.1f}')")
        REMAINING=$((TOTAL - EPISODE))
        ETA=$(python3 -c "s=int(${REMAINING}*(${DELAY}+90)); print(f'{s//3600}h {(s%3600)//60}m')")
        NEXT_TITLE=$(sed -n "$((EPISODE + 1))p" "${TITLES_FILE}" 2>/dev/null)
        AFTER_TITLE=$(sed -n "$((EPISODE + 2))p" "${TITLES_FILE}" 2>/dev/null)

        send_slack ":tv: *Forgotten Weapons — Download Progress*
:white_check_mark: *Just finished:* S01E${PADDED} — ${TITLE}
:arrow_down: *Downloading now:* S01E$(printf '%02d' $((EPISODE+1))) — ${NEXT_TITLE:-unknown}
:next_track_button: *Up next:* S01E$(printf '%02d' $((EPISODE+2))) — ${AFTER_TITLE:-unknown}
:bar_chart: *Progress:* ${EPISODE}/${TOTAL} (${PCT}%) | Failed: ${FAILED}
:clock3: *ETA:* ~${ETA} remaining"

        LAST_NOTIFY=${NOW}
    fi

    # Delay between downloads
    if (( EPISODE < TOTAL )); then
        sleep ${DELAY}
    fi

done < "${URLS_FILE}"

send_slack ":white_check_mark: *Forgotten Weapons download COMPLETE*
Total: ${TOTAL} | Failed: ${FAILED}
Output: ${SEASON_DIR}"

log "=== COMPLETE. Total: ${TOTAL}, Failed: ${FAILED} ==="
