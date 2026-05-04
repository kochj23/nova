#!/bin/zsh
# nova_pg_post_optimize.sh — Run after the HNSW/backfill operations complete.
# One-time script. Safe to re-run (all operations are idempotent).
#
# Drops the old tier index, drops redundant single-column source index,
# restarts memory server to pick up code changes.
#
# Written by Jordan Koch.

set -uo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"

DB="nova_memories"
USER="kochj"

echo "[$(date)] Starting post-optimization..."

# Wait for any concurrent index builds to finish
while psql -U "$USER" -d "$DB" -tAc "SELECT count(*) FROM pg_stat_activity WHERE query ILIKE '%CREATE INDEX%' AND state = 'active' AND pid != pg_backend_pid();" 2>/dev/null | grep -qv "^0$"; do
    echo "[$(date)] Waiting for index builds to finish..."
    sleep 30
done

# Wait for backfill to finish
while psql -U "$USER" -d "$DB" -tAc "SELECT count(*) FROM pg_stat_activity WHERE query ILIKE '%text_hash%' AND state = 'active' AND pid != pg_backend_pid();" 2>/dev/null | grep -qv "^0$"; do
    echo "[$(date)] Waiting for text_hash backfill to finish..."
    sleep 30
done

echo "[$(date)] All background operations complete."

# Verify the new partial index exists
NEW_IDX=$(psql -U "$USER" -d "$DB" -tAc "SELECT 1 FROM pg_indexes WHERE indexname = 'memories_embedding_hnsw_active';")
if [ "$NEW_IDX" != "1" ]; then
    echo "[$(date)] ERROR: memories_embedding_hnsw_active not found. Aborting."
    exit 1
fi

# Drop the old tier index (redundant — partial HNSW handles this)
echo "[$(date)] Dropping memories_tier_idx (redundant)..."
psql -U "$USER" -d "$DB" -c "DROP INDEX IF EXISTS memories_tier_idx;"

# Drop the old single-column source index (replaced by composite source+created_at)
COMPOSITE_IDX=$(psql -U "$USER" -d "$DB" -tAc "SELECT 1 FROM pg_indexes WHERE indexname = 'memories_source_created_idx';")
if [ "$COMPOSITE_IDX" = "1" ]; then
    echo "[$(date)] Dropping memories_source_idx (replaced by composite)..."
    psql -U "$USER" -d "$DB" -c "DROP INDEX IF EXISTS memories_source_idx;"
fi

# Final ANALYZE with updated stats
echo "[$(date)] Running final ANALYZE..."
psql -U "$USER" -d "$DB" -c "ANALYZE memories;"

# Show final state
echo "[$(date)] Final index state:"
psql -U "$USER" -d "$DB" -c "SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) as size FROM pg_stat_user_indexes WHERE relname = 'memories' ORDER BY pg_relation_size(indexrelid) DESC;"

echo "[$(date)] Final table stats:"
psql -U "$USER" -d "$DB" -c "SELECT count(*) as rows, pg_size_pretty(pg_total_relation_size('memories')) as total_size FROM memories;"

# Restart memory server to pick up code changes
echo "[$(date)] Restarting memory server..."
launchctl kickstart -k "gui/$(id -u)/net.digitalnoise.nova-memory-server"
sleep 3

# Verify it came back
if curl -s http://127.0.0.1:18790/health | grep -q "ok"; then
    echo "[$(date)] Memory server restarted successfully."
else
    echo "[$(date)] WARNING: Memory server may not have restarted cleanly. Check logs."
fi

echo "[$(date)] Post-optimization complete."
