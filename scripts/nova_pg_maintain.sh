#!/bin/zsh
# nova_pg_maintain.sh — Weekly PostgreSQL maintenance for nova_memories.
#
# Runs via OpenClaw scheduler (Sunday 3:00 AM).
# - VACUUM ANALYZE: reclaims dead tuples, updates planner statistics
# - REINDEX CONCURRENTLY on HNSW: rebuilds vector index for optimal recall quality
# - Reports stats and any issues to Slack
#
# Written by Jordan Koch.

set -uo pipefail

DB_NAME="nova_memories"
DB_USER="kochj"
LOG_FILE="$HOME/.openclaw/logs/nova_pg_maintain.log"
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
notify() {
    "$SCRIPT_DIR/nova_slack_post.sh" "$1" "C0ATAF7NZG9" 2>/dev/null || true
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

if ! pg_isready -q 2>/dev/null; then
    log "ERROR: PostgreSQL is not running"
    notify ":x: *PG Maintenance Failed* — PostgreSQL is not running"
    exit 1
fi

START=$(date +%s)
log "Starting weekly maintenance..."

# ── Set maintenance mode flag — pauses ingest queue and Big Brother restarts ──
redis-cli SET nova:maintenance:active "1" EX 14400 > /dev/null 2>&1  # expires in 4h as safety net
log "Maintenance mode ON (nova:maintenance:active = 1)"

cleanup_maintenance() {
    redis-cli DEL nova:maintenance:active > /dev/null 2>&1
    log "Maintenance mode OFF"
}
trap cleanup_maintenance EXIT INT TERM

# ── Gather pre-maintenance stats ─────────────────────────────────────────────
ROW_COUNT=$(psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT count(*) FROM memories;" 2>/dev/null || echo "?")
DEAD_TUPLES=$(psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT n_dead_tup FROM pg_stat_user_tables WHERE relname = 'memories';" 2>/dev/null || echo "?")
DB_SIZE=$(psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT pg_size_pretty(pg_total_relation_size('memories'));" 2>/dev/null || echo "?")

log "Pre-maintenance: ${ROW_COUNT} rows, ${DEAD_TUPLES} dead tuples, ${DB_SIZE} total size"

# ── VACUUM ANALYZE ────────────────────────────────────────────────────────────
log "Running VACUUM ANALYZE..."
VACUUM_START=$(date +%s)
psql -U "$DB_USER" -d "$DB_NAME" -c "VACUUM ANALYZE memories;" 2>>"$LOG_FILE"
VACUUM_EXIT=$?
VACUUM_DURATION=$(( $(date +%s) - VACUUM_START ))

if [ $VACUUM_EXIT -ne 0 ]; then
    log "ERROR: VACUUM ANALYZE failed (exit $VACUUM_EXIT)"
    notify ":x: *PG Maintenance Failed* — VACUUM ANALYZE exit code $VACUUM_EXIT"
    exit 1
fi
log "VACUUM ANALYZE complete (${VACUUM_DURATION}s)"

# ── REINDEX HNSW (monthly — only on first Sunday of month) ────────────────────
DAY_OF_MONTH=$(date +%d)
if [ "$DAY_OF_MONTH" -le 7 ]; then
    log "First Sunday of month — rebuilding HNSW index..."
    REINDEX_START=$(date +%s)
    psql -U "$DB_USER" -d "$DB_NAME" -c "REINDEX INDEX CONCURRENTLY memories_embedding_hnsw;" 2>>"$LOG_FILE"
    REINDEX_EXIT=$?
    REINDEX_DURATION=$(( $(date +%s) - REINDEX_START ))

    if [ $REINDEX_EXIT -ne 0 ]; then
        log "WARNING: HNSW reindex failed (exit $REINDEX_EXIT) — non-fatal, index still usable"
        REINDEX_MSG="HNSW reindex failed (non-fatal)"
    else
        log "HNSW reindex complete (${REINDEX_DURATION}s)"
        REINDEX_MSG="HNSW reindex: ${REINDEX_DURATION}s"
    fi
else
    REINDEX_MSG="skipped (not first week)"
fi

# ── Check for stale pg_stat_statements and reset if bloated ──────────────────
STMT_COUNT=$(psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT count(*) FROM pg_stat_statements;" 2>/dev/null || echo "0")
if [ "$STMT_COUNT" -gt 5000 ]; then
    psql -U "$DB_USER" -d "$DB_NAME" -c "SELECT pg_stat_statements_reset();" 2>/dev/null
    log "Reset pg_stat_statements (had $STMT_COUNT entries)"
fi

# ── Post-maintenance stats ───────────────────────────────────────────────────
POST_DEAD=$(psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT n_dead_tup FROM pg_stat_user_tables WHERE relname = 'memories';" 2>/dev/null || echo "?")
POST_SIZE=$(psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT pg_size_pretty(pg_total_relation_size('memories'));" 2>/dev/null || echo "?")

TOTAL_DURATION=$(( $(date +%s) - START ))

# ── Report ───────────────────────────────────────────────────────────────────
MSG=":broom: *Weekly PG Maintenance Complete*
- Rows: ${ROW_COUNT} | Dead tuples: ${DEAD_TUPLES} → ${POST_DEAD}
- Size: ${DB_SIZE} → ${POST_SIZE}
- VACUUM ANALYZE: ${VACUUM_DURATION}s
- HNSW reindex: ${REINDEX_MSG}
- Total: ${TOTAL_DURATION}s"

notify "$MSG"
log "Maintenance complete (${TOTAL_DURATION}s)"
