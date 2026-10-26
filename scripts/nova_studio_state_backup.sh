#!/bin/bash
# nova_studio_state_backup.sh — the Studio's (.6) irreplaceable local state, encrypted, to the NAS.
#
# Little Mister, 2026-10-14 (before handing the Studio to Nova): "Let's get these backed up on the NAS."
# What a macOS reinstall would otherwise lose (code is in the repo, data is on pg-primary, secrets are in
# the 1Password "Nova" vault, launchd jobs are in config/launchd/studio):
#   - signal-cli account keys        ~/.local/share/signal-cli   (Nova's Signal number)
#   - Home Assistant config + .storage (Zigbee/ZHA network, HomeKit Bridge pairings, Lutron certs);
#     the recorder DB is copied with sqlite3 .backup so a live write can't corrupt it
#   - NovaHomeKit.app                ~/migrate/NovaHomeKit.app
#   - ~/.openclaw minus regenerable bulk (workspace images, logs, venvs, caches, models)
#   - live LaunchAgents/LaunchDaemons (config/launchd/studio is a manual snapshot and drifts)
# Output: /Volumes/nas/backups/studio-state/studio-state-<UTC stamp>.tar.gz.enc (AES-256, PBKDF2), last 7 kept.
# Key: Keychain + 1Password item `nova-studio-backup-key`. Restore:
#   NOVA_BK_KEY=$(security find-generic-password -s nova-studio-backup-key -w) openssl enc -d -aes-256-cbc -pbkdf2 -pass env:NOVA_BK_KEY \
#     -in FILE | tar -xzf - -C /     (paths are stored relative to /)
# Written by Jordan Koch (via Claude).
set -euo pipefail
DEST=/Volumes/nas/backups/studio-state
KEEP=7
export NOVA_BK_KEY=$(security find-generic-password -s nova-studio-backup-key -w)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$DEST"
chmod 700 "$DEST"

HA="$HOME/homeassistant/config"
mkdir -p "$STAGE/${HA#/}"
sqlite3 "$HA/home-assistant_v2.db" ".backup '$STAGE/${HA#/}/recorder-backup.sqlite'"   # restore: rename to home-assistant_v2.db

OUT="$DEST/studio-state-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.enc"
# ponytail: tar -C / keeps the absolute layout so restore is one command; the DB copy rides in from $STAGE.
tar -czf - -C / \
    --exclude "${HA#/}/home-assistant_v2.db*" --exclude "${HA#/}/*.corrupt*" --exclude "${HA#/}/*.log*" \
    --exclude "${HA#/}/deps" --exclude "${HA#/}/.cache" --exclude "${HA#/}/tts" \
    --exclude ".openclaw/workspace" --exclude ".openclaw/logs" --exclude ".openclaw/backup-staging" \
    --exclude ".openclaw/venv" --exclude ".openclaw/venvs" --exclude ".openclaw/cache" \
    --exclude ".openclaw/media" --exclude ".openclaw/browser" --exclude ".openclaw/models" \
    --exclude ".openclaw/embeddings" --exclude ".openclaw/voice_out" --exclude "__pycache__" \
    "${HOME#/}/.local/share/signal-cli" "${HA#/}" "${HOME#/}/migrate/NovaHomeKit.app" "${HOME#/}/.openclaw" \
    "${HOME#/}/Library/LaunchAgents" $(cd / && echo Library/LaunchDaemons/net.digitalnoise.*) \
    -C "$STAGE" "${HA#/}/recorder-backup.sqlite" \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:NOVA_BK_KEY -out "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
chmod 600 "$OUT"

# keep the newest $KEEP
ls -1t "$DEST"/studio-state-*.tar.gz.enc | tail -n +$((KEEP + 1)) | while read -r f; do rm -f "$f"; done
echo "[studio-state] $(basename "$OUT") $(du -h "$OUT" | cut -f1)"
