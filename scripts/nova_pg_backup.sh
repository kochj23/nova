#!/bin/zsh
# nova_pg_backup.sh — Nightly pg_dump of ALL PostgreSQL databases to local + NAS.
#
# Runs via the scheduler at 2:00 AM. Auto-discovers every user database (so new DBs
# are backed up automatically) and dumps each in directory format (-Fd, 4 parallel
# jobs). Verifies each archive's TOC, rsyncs to NAS, rotates to RETENTION_DAYS.
#
# Usage: nova_pg_backup.sh [db1 db2 ...]   (no args = all user databases)
#
# Written by Jordan Koch. 2026-06-24: generalized from nova_memories-only to ALL DBs.

set -uo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
DB_USER="kochj"
LOCAL_DIR="$HOME/.openclaw/backup-staging/postgres"   # transient internal staging (was /Volumes/Data — off the FDA-blocked volume); deleted after NAS copy
NAS_DIR="/Volumes/nas/backups/postgres"
RETENTION_DAYS=7    # 7 nightly dumps is plenty; the streaming replicas are the real HA
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$HOME/.openclaw/logs/nova_pg_backup.log"
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
notify() { "$SCRIPT_DIR/nova_slack_post.sh" "$1" "C0ATAF7NZG9" 2>/dev/null || true }
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE" }

# ── Pre-flight ───────────────────────────────────────────────────────────────
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

# ── Which databases? (args override; else auto-discover ALL user DBs) ─────────
if [ $# -gt 0 ]; then
    DATABASES=("$@")
else
    DATABASES=($(psql -U "$DB_USER" -d postgres -tAc \
      "SELECT datname FROM pg_database WHERE datistemplate=false \
       AND datname NOT IN ('postgres') AND datname NOT LIKE '%restoretest%' \
       ORDER BY pg_database_size(datname) DESC" 2>/dev/null))
fi
log "Starting backup of ${#DATABASES[@]} database(s): ${DATABASES[*]}"

RESULTS=()
FAILED=0

backup_one() {
    local DB="$1"
    local DUMP_DIR="${DB}_${TIMESTAMP}"
    local start=$(date +%s)
    log "Dumping $DB (directory format, 4 parallel jobs)..."
    pg_dump -U "$DB_USER" -d "$DB" --no-owner --no-privileges -Fd -j 4 \
        -f "$LOCAL_DIR/$DUMP_DIR" 2>>"$LOG_FILE"
    local rc=$?
    if [ $rc -ne 0 ]; then
        log "ERROR: pg_dump $DB failed (exit $rc)"
        RESULTS+=("✗ $DB — DUMP FAILED (exit $rc)"); FAILED=$((FAILED+1)); return 1
    fi
    # integrity: archive TOC must be readable + non-empty
    local toc
    toc=$(pg_restore --list "$LOCAL_DIR/$DUMP_DIR" 2>/dev/null | wc -l | tr -d ' ')
    if [ "${toc:-0}" -lt 1 ]; then
        log "CRITICAL: $DB archive corrupt/empty TOC"
        RESULTS+=("✗ $DB — ARCHIVE CORRUPT"); FAILED=$((FAILED+1)); return 1
    fi
    local size=$(du -sh "$LOCAL_DIR/$DUMP_DIR" | cut -f1)
    local dur=$(( $(date +%s) - start ))
    # off-site copy
    local nas="local-only"
    if $NAS_AVAILABLE; then
        if rsync -a --timeout=600 "$LOCAL_DIR/$DUMP_DIR" "$NAS_DIR/" 2>>"$LOG_FILE"; then
            nas="NAS"; rm -rf "$LOCAL_DIR/$DUMP_DIR"   # NAS is authoritative — free the internal staging
        else
            log "WARNING: NAS copy of $DB failed — kept on internal staging as fallback"; nas="internal-staging-only (NAS FAILED)"
        fi
    fi
    # rotation (per-DB)
    find "$LOCAL_DIR" -maxdepth 1 -name "${DB}_*" -type d -mtime +${RETENTION_DAYS} -exec rm -rf {} \; 2>/dev/null
    $NAS_AVAILABLE && find "$NAS_DIR" -maxdepth 1 -name "${DB}_*" -type d -mtime +${RETENTION_DAYS} -exec rm -rf {} \; 2>/dev/null
    log "$DB done: $size, ${toc} TOC entries, ${dur}s, $nas"
    RESULTS+=("✓ $DB — $size, ${dur}s, $nas")
    return 0
}

OVERALL_START=$(date +%s)
for DB in $DATABASES; do backup_one "$DB"; done
TOTAL=$(( $(date +%s) - OVERALL_START ))

# ── Report ───────────────────────────────────────────────────────────────────
SUMMARY=$(printf '%s\n' "${RESULTS[@]}")
if [ "$FAILED" -eq 0 ]; then
    notify ":white_check_mark: *Postgres Backup — all ${#DATABASES[@]} DB(s) OK* (${TOTAL}s, ${RETENTION_DAYS}d retention)\n${SUMMARY}"
else
    notify ":rotating_light: *Postgres Backup — ${FAILED}/${#DATABASES[@]} DB(s) FAILED* (${TOTAL}s)\n${SUMMARY}"
fi
log "Backup run complete: ${#DATABASES[@]} DBs, ${FAILED} failed, ${TOTAL}s"
[ "$FAILED" -eq 0 ]
