#!/bin/bash
# nova_backup_nas.sh — Daily backup of Nova's ~/.openclaw to NAS
# Target: /Volumes/NAS/GoogleDriveBackups/nova-backups/
# Called by OpenClaw cron (nova-backup-daily)

set -euo pipefail

NAS_TARGET="/Volumes/NAS/GoogleDriveBackups/nova-backups"
OPENCLAW_DIR="$HOME/.openclaw"
DATE=$(date +%Y-%m-%d)
LOG_FILE="/tmp/nova_backup_${DATE}.log"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "=== Nova NAS Backup — $DATE ==="
echo "Start: $(date)"

# Verify NAS is mounted and writable
if [ ! -d "$NAS_TARGET" ]; then
    echo "ERROR: NAS target not accessible: $NAS_TARGET"
    echo "Check that /Volumes/NAS is mounted."
    exit 1
fi

if ! touch "$NAS_TARGET/.write_test" 2>/dev/null; then
    echo "ERROR: NAS target not writable: $NAS_TARGET"
    exit 1
fi
rm -f "$NAS_TARGET/.write_test"

# Create dated snapshot directory and latest symlink
SNAPSHOT_DIR="$NAS_TARGET/snapshots/$DATE"
LATEST_DIR="$NAS_TARGET/latest"

mkdir -p "$SNAPSHOT_DIR"

echo ""
echo "--- Syncing to: $SNAPSHOT_DIR ---"

# rsync key directories — exclude ephemeral/large-but-recoverable content
/opt/homebrew/bin/rsync -av --delete \
    --exclude='cache/' \
    --exclude='logs/' \
    --exclude='browser/' \
    --exclude='delivery-queue/' \
    --exclude='media/' \
    --exclude='paste-cache/' \
    --exclude='shell-snapshots/' \
    --exclude='statsig/' \
    --exclude='*.tmp' \
    --exclude='*.log' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$OPENCLAW_DIR/" \
    "$SNAPSHOT_DIR/"

echo ""
echo "--- Updating latest symlink ---"
rm -f "$LATEST_DIR"
ln -s "$SNAPSHOT_DIR" "$LATEST_DIR"

echo ""
echo "--- Backup size ---"
du -sh "$SNAPSHOT_DIR"

# Prune snapshots older than 30 days (keep latest 30)
echo ""
echo "--- Pruning snapshots older than 30 days ---"
TOTAL=$(find "$NAS_TARGET/snapshots" -maxdepth 1 -type d -name "20*" | wc -l | tr -d ' ')
TO_DELETE=$(( TOTAL > 30 ? TOTAL - 30 : 0 ))
if [ "$TO_DELETE" -gt 0 ]; then
    find "$NAS_TARGET/snapshots" -maxdepth 1 -type d -name "20*" | sort | head -n "$TO_DELETE" | while read -r old_dir; do
        echo "Removing: $old_dir"
        rm -rf "$old_dir"
    done
else
    echo "Nothing to prune ($TOTAL snapshots, limit 30)"
fi

SNAPSHOT_COUNT=$(find "$NAS_TARGET/snapshots" -maxdepth 1 -type d -name "20*" | wc -l | tr -d ' ')

echo ""
echo "=== Backup complete ==="
echo "End: $(date)"
echo "Snapshots retained: $SNAPSHOT_COUNT"
echo "Latest: $SNAPSHOT_DIR"
