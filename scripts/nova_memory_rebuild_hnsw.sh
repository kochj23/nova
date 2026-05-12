#!/bin/zsh
# nova_memory_rebuild_hnsw.sh — Rebuild HNSW index m=32 → m=16 + add partial indexes
#
# What this does:
#   1. Sets the Nova maintenance flag so Big Brother stops healing memory server
#   2. Builds a new HNSW index (m=16, ef_construction=128) CONCURRENTLY
#      → no downtime, memory server stays running while this builds (~45 min)
#   3. Atomically swaps old → new index
#   4. Builds partial HNSW indexes for the 4 largest sources CONCURRENTLY
#   5. Clears the maintenance flag
#
# Run time: ~3-4 hours total (CONCURRENTLY = no table lock, but slow)
# Run as: zsh nova_memory_rebuild_hnsw.sh
#
# IMPORTANT: Do not run while a halfvec migration is in progress.

set -euo pipefail

PG_DSN="postgresql://localhost:5432/nova_memories"
REDIS_CLI="redis-cli -p 6379"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "=== HNSW Rebuild Script ==="
log "Setting maintenance flag (Big Brother will pause healing memory server)"
$REDIS_CLI SET nova:maintenance:active 1 EX 14400   # 4-hour TTL safety

# ── 1. Rebuild main HNSW index ───────────────────────────────────────────────
log "Building memories_embedding_hnsw_v2 (m=16, ef_construction=128) CONCURRENTLY..."
log "This will take ~45-90 minutes. Memory server stays online."
psql "$PG_DSN" -c "
  SET maintenance_work_mem = '4GB';
  CREATE INDEX CONCURRENTLY IF NOT EXISTS memories_embedding_hnsw_v2
    ON memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
"
log "New index built. Swapping in..."
psql "$PG_DSN" << 'SQL'
BEGIN;
DROP INDEX CONCURRENTLY memories_embedding_hnsw;
ALTER INDEX memories_embedding_hnsw_v2 RENAME TO memories_embedding_hnsw;
COMMIT;
SQL
log "Main HNSW index swapped: m=32 → m=16"

# ── 2. Partial HNSW indexes ──────────────────────────────────────────────────
log "Building partial HNSW index for email_archive (~600K rows)..."
psql "$PG_DSN" -c "
  SET maintenance_work_mem = '4GB';
  CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memories_hnsw_email
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128)
    WHERE source = 'email_archive';
"
log "email_archive partial index done."

log "Building partial HNSW index for cloud_governance (~100K rows)..."
psql "$PG_DSN" -c "
  SET maintenance_work_mem = '4GB';
  CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memories_hnsw_cloud_gov
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128)
    WHERE source = 'cloud_governance';
"
log "cloud_governance partial index done."

log "Building partial HNSW index for disney_internal (~92K rows)..."
psql "$PG_DSN" -c "
  SET maintenance_work_mem = '4GB';
  CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memories_hnsw_disney
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128)
    WHERE source = 'disney_internal';
"
log "disney_internal partial index done."

log "Building partial HNSW index for imessage (~73K rows)..."
psql "$PG_DSN" -c "
  SET maintenance_work_mem = '4GB';
  CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memories_hnsw_imessage
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128)
    WHERE source = 'imessage';
"
log "imessage partial index done."

# ── 3. Summary ───────────────────────────────────────────────────────────────
log "Clearing maintenance flag"
$REDIS_CLI DEL nova:maintenance:active

psql "$PG_DSN" -c "
SELECT indexname,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size,
       idx_scan AS scans
FROM pg_stat_user_indexes
WHERE tablename = 'memories'
ORDER BY pg_relation_size(indexrelid) DESC;
"

log "=== Rebuild complete. Restart Nova memory server to pick up new indexes. ==="
log "Run: launchctl kickstart -k gui/\$(id -u)/net.digitalnoise.nova-memory-server"
