#!/bin/zsh
# nova_yt_refresh_cookies.sh — Refresh YouTube cookies from Safari session.
# Must run in a GUI/user context (Terminal, not launchd) to access Keychain/TCC.
# Scheduled via scheduler but only works when called interactively or via osascript.
# Written by Jordan Koch.

COOKIES_FILE="$HOME/.openclaw/cache/yt_cookies.txt"
YT_DLP="/opt/homebrew/bin/yt-dlp"

echo "[yt-cookies] Refreshing YouTube cookies from Safari..."

# Try cookie file export
if "$YT_DLP" --cookies-from-browser chrome \
    --cookies "$COOKIES_FILE" \
    --skip-download --print "%(id)s" \
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ" >/dev/null 2>&1; then
    chmod 600 "$COOKIES_FILE"
    SIZE=$(wc -c < "$COOKIES_FILE")
    echo "[yt-cookies] Refreshed: $COOKIES_FILE ($SIZE bytes)"
    exit 0
else
    echo "[yt-cookies] FAILED — run manually from Terminal: nova_yt_refresh_cookies.sh"
    exit 1
fi
