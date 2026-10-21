#!/bin/bash
# nova_tv_dash_import.sh — TV-Movies-3 (.7): publish the newest Nova Cluster frames for the Apple TV screensaver.
# Deployed to .7:~/bin/nova_tv_dash_import.sh, run by LaunchAgent net.digitalnoise.nova-tv-dash (hourly, GUI session).
# Frames arrive in ~/Library/Caches/nova-dash, pushed by nova_cluster_render.py on the Studio (.6).
# Wish #60 / queue #3113 tier 1.
#
# PRIMARY (2026-10-21): Home Sharing FOLDER mode. The newest KEEP frames are mirrored into ~/Pictures/NovaTV/Nova;
# Home Sharing's "Share photos with Apple TV" pointed at ~/Pictures/NovaTV shows the subfolder "Nova" as an album.
# No Photos.app, no Apple-event TCC prompt, and old frames really get deleted.
# FALLBACK: Photos album "Nova" import, only when ~/.nova-tv-dash-photos exists. It never worked unattended:
# 392/392 hourly runs 2026-10-04..21 timed out (-1712) — Photos on .7 never finishes opening its library for
# scripting (stuck in NSWhoseSpecifier evaluation), even after a restart.
set -u
DIR="$HOME/Library/Caches/nova-dash"
SHARE="$HOME/Pictures/NovaTV/Nova"
KEEP=6
mkdir -p "$DIR" "$SHARE"
NEWEST=$(ls -1 "$DIR"/nova-cluster-*.png 2>/dev/null | sort | tail -1)
[ -n "$NEWEST" ] || { echo "$(date '+%F %T') no frames in $DIR"; exit 0; }

# mirror newest KEEP frames into the shared folder, drop the rest
WANT=$(ls -1 "$DIR"/nova-cluster-*.png | sort | tail -n $KEEP)
for f in $WANT; do [ -f "$SHARE/$(basename "$f")" ] || install -m 644 "$f" "$SHARE/"; done
for f in "$SHARE"/nova-cluster-*.png; do
  [ -e "$f" ] || continue
  echo "$WANT" | grep -q "/$(basename "$f")$" || rm -f "$f"
done
# cache keeps 48 frames
ls -1 "$DIR"/nova-cluster-*.png 2>/dev/null | sort -r | tail -n +49 | xargs -I{} rm -f {}
echo "$(date '+%F %T') folder $SHARE now $(ls -1 "$SHARE"/*.png 2>/dev/null | wc -l | tr -d ' ') frames, newest $(basename "$NEWEST")"

[ -e "$HOME/.nova-tv-dash-photos" ] || exit 0
RES=$(osascript -e 'with timeout of 120 seconds' \
  -e 'tell application "Photos"' \
  -e 'if not (exists album "Nova") then make new album named "Nova"' \
  -e "import (POSIX file \"$NEWEST\") into album \"Nova\" skip check duplicates no" \
  -e 'return (count of media items of album "Nova")' \
  -e 'end tell' -e 'end timeout' 2>&1)
if [ $? -eq 0 ]; then echo "$(date '+%F %T') photos imported $(basename "$NEWEST") (album $RES items)"; else echo "$(date '+%F %T') photos import failed: $RES"; exit 1; fi
