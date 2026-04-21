#!/bin/zsh
# nova_pg_backup.sh — Nightly pg_dump of nova_memories to NAS with 7-day rotation.
#
# Runs via launchd at 2:00 AM. Backs up to both local and NAS storage.
# Reports status to #nova-notifications via Slack.
#
# Written by Jordan Koch.

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
DB_NAME="nova_memories"
DB_USER="kochj"
LOCAL_DIR="/Volumes/Data/backups/postgres"
NAS_DIR="/Volumes/nas/backups/postgres"
RETENTION_DAYS=7
DATE=$(date +%Y-%m-%d)
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DUMP_FILE="nova_memories_${TIMESTAMP}.sql.gz"
LOG_FILE="$HOME/.openclaw/logs/nova_pg_backup.log"

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

# Check Postgres is running
if ! pg_isready -q 2>/dev/null; then
    log "ERROR: PostgreSQL is not running"
    notify ":x: *Postgres Backup Failed* — PostgreSQL is not running"
    exit 1
fi

# Check NAS is mounted
if [ ! -d "$NAS_DIR" ]; then
    log "WARNING: NAS not mounted at $NAS_DIR — backing up to local only"
    NAS_AVAILABLE=false
else
    NAS_AVAILABLE=true
fi

# Create local dir
mkdir -p "$LOCAL_DIR"

# ── Dump ─────────────────────────────────────────────────────────────────────
log "Dumping $DB_NAME..."
DUMP_START=$(date +%s)

/opt/homebrew/opt/postgresql@17/bin/pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --no-privileges \
    | gzip -9 > "$LOCAL_DIR/$DUMP_FILE"

DUMP_END=$(date +%s)
DUMP_DURATION=$((DUMP_END - DUMP_START))
DUMP_SIZE=$(du -sh "$LOCAL_DIR/$DUMP_FILE" | cut -f1)

log "Dump complete: $DUMP_FILE ($DUMP_SIZE in ${DUMP_DURATION}s)"

# ── Copy to NAS ──────────────────────────────────────────────────────────────
if $NAS_AVAILABLE; then
    cp "$LOCAL_DIR/$DUMP_FILE" "$NAS_DIR/$DUMP_FILE"
    log "Copied to NAS: $NAS_DIR/$DUMP_FILE"
fi

# ── Rotation (keep last 7 days) ─────────────────────────────────────────────
_rotate() {
    local dir="$1"
    local count=$(find "$dir" -name "nova_memories_*.sql.gz" -mtime +${RETENTION_DAYS} 2>/dev/null | wc -l | tr -d ' ')
    if [ "$count" -gt 0 ]; then
        find "$dir" -name "nova_memories_*.sql.gz" -mtime +${RETENTION_DAYS} -delete
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
if $NAS_AVAILABLE; then
    MSG=":white_check_mark: *Postgres Backup Complete*\n- DB: $DB_NAME ($ROW_COUNT rows)\n- Size: $DUMP_SIZE (compressed)\n- Duration: ${DUMP_DURATION}s\n- Local: $LOCAL_DIR/$DUMP_FILE\n- NAS: $NAS_DIR/$DUMP_FILE\n- Retention: ${RETENTION_DAYS} days"
else
    MSG=":warning: *Postgres Backup Complete (Local Only)*\n- DB: $DB_NAME ($ROW_COUNT rows)\n- Size: $DUMP_SIZE\n- NAS unavailable — only local backup saved\n- Local: $LOCAL_DIR/$DUMP_FILE"
fi

notify "$MSG"
log "Backup complete. Rows: $ROW_COUNT"
