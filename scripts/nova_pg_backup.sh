#!/bin/zsh
# nova_pg_backup.sh — Nightly pg_dump of nova_memories to NAS with 7-day rotation.
#
# Runs via OpenClaw scheduler at 2:00 AM. Backs up to local, then rsync to NAS.
# Uses pg_dump directory format (-Fd) with 4 parallel jobs for faster dumps and restores.
#
# Written by Jordan Koch.

set -uo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
DB_NAME="nova_memories"
DB_USER="kochj"
LOCAL_DIR="/Volumes/Data/backups/postgres"
NAS_DIR="/Volumes/nas/backups/postgres"
RETENTION_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DUMP_DIR="nova_memories_${TIMESTAMP}"
LOG_FILE="$HOME/.openclaw/logs/nova_pg_backup.log"
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"

# Slack notification via nova_slack_post.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
notify() {
    "$SCRIPT_DIR/nova_slack_post.sh" "$1" "C0ATAF7NZG9" 2>/dev/null || true
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# ── Pre-flight ───────────────────────────────────────────────────────────────
log "Starting nova_memories backup..."

if ! pg_isready -q 2>/dev/null; then
    log "ERROR: PostgreSQL is not running"
    notify ":x: *Postgres Backup Failed* — PostgreSQL is not running"
    exit 1
fi

if [ ! -d "$NAS_DIR" ]; then
    log "WARNING: NAS not mounted at $NAS_DIR — backing up to local only"
    NAS_AVAILABLE=false
else
    NAS_AVAILABLE=true
fi

mkdir -p "$LOCAL_DIR"

# ── Dump (directory-format parallel dump, pg17) ─────────────────────────────
log "Dumping $DB_NAME (directory format, 4 parallel jobs)..."
DUMP_START=$(date +%s)

pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --no-privileges \
    -Fd -j 4 -f "$LOCAL_DIR/$DUMP_DIR" 2>>"$LOG_FILE"

DUMP_EXIT=$?
DUMP_END=$(date +%s)
DUMP_DURATION=$((DUMP_END - DUMP_START))

if [ $DUMP_EXIT -ne 0 ]; then
    log "ERROR: pg_dump failed with exit code $DUMP_EXIT"
    notify ":x: *Postgres Backup Failed* — pg_dump exit code $DUMP_EXIT after ${DUMP_DURATION}s"
    exit 1
fi

DUMP_SIZE=$(du -sh "$LOCAL_DIR/$DUMP_DIR" | cut -f1)
log "Dump complete: $DUMP_DIR ($DUMP_SIZE in ${DUMP_DURATION}s)"

# ── Verify backup integrity ────────────────────────────────────────────────
log "Verifying backup integrity..."
VERIFY_OUTPUT=$(pg_restore --list "$LOCAL_DIR/$DUMP_DIR" 2>&1)
VERIFY_EXIT=$?
if [ $VERIFY_EXIT -ne 0 ]; then
    log "WARNING: Backup verification failed (exit $VERIFY_EXIT)"
    notify ":warning: *Postgres Backup Verification Failed* — dump may be corrupt. Exit: $VERIFY_EXIT"
else
    TOC_COUNT=$(echo "$VERIFY_OUTPUT" | wc -l | tr -d ' ')
    log "Verification passed: $TOC_COUNT TOC entries"
fi

# ── Copy to NAS via rsync (faster than cp for large files over AFP) ──────────
if $NAS_AVAILABLE; then
    COPY_START=$(date +%s)
    rsync -a --progress --timeout=600 "$LOCAL_DIR/$DUMP_DIR" "$NAS_DIR/" 2>>"$LOG_FILE"
    COPY_EXIT=$?
    COPY_END=$(date +%s)
    COPY_DURATION=$((COPY_END - COPY_START))

    if [ $COPY_EXIT -eq 0 ]; then
        log "Copied to NAS: $NAS_DIR/$DUMP_DIR (${COPY_DURATION}s)"
    else
        log "WARNING: NAS copy failed (exit $COPY_EXIT) after ${COPY_DURATION}s — local backup is safe"
        NAS_AVAILABLE=false
    fi
fi

# ── Rotation (keep last 7 days) ─────────────────────────────────────────────
_rotate() {
    local dir="$1"
    local count=$(find "$dir" -maxdepth 1 -name "nova_memories_*" -type d -mtime +${RETENTION_DAYS} 2>/dev/null | wc -l | tr -d ' ')
    if [ "$count" -gt 0 ]; then
        find "$dir" -maxdepth 1 -name "nova_memories_*" -type d -mtime +${RETENTION_DAYS} -exec rm -rf {} \;
        log "Rotated $count old backup(s) from $dir"
    fi
}

_rotate "$LOCAL_DIR"
if $NAS_AVAILABLE; then
    _rotate "$NAS_DIR"
fi

# ── Row count for verification ───────────────────────────────────────────────
ROW_COUNT=$(psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT count(*) FROM memories;" 2>/dev/null || echo "?")

# ── Report ───────────────────────────────────────────────────────────────────
TOTAL_DURATION=$(($(date +%s) - DUMP_START))
if $NAS_AVAILABLE; then
    MSG=":white_check_mark: *Postgres Backup Complete*\n- DB: $DB_NAME ($ROW_COUNT rows)\n- Size: $DUMP_SIZE (directory format, 4 parallel jobs)\n- Dump: ${DUMP_DURATION}s, Copy: ${COPY_DURATION:-0}s, Total: ${TOTAL_DURATION}s\n- Local: $LOCAL_DIR/$DUMP_DIR\n- NAS: $NAS_DIR/$DUMP_DIR\n- Retention: ${RETENTION_DAYS} days"
else
    MSG=":warning: *Postgres Backup Complete (Local Only)*\n- DB: $DB_NAME ($ROW_COUNT rows)\n- Size: $DUMP_SIZE (directory format)\n- Duration: ${TOTAL_DURATION}s\n- NAS unavailable — only local backup saved\n- Local: $LOCAL_DIR/$DUMP_DIR"
fi

notify "$MSG"
log "Backup complete. Rows: $ROW_COUNT. Total: ${TOTAL_DURATION}s"
