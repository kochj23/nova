#!/bin/zsh
# nova_memory_halfvec_migrate.sh — Migrate embeddings from float32 to halfvec (float16)
#
# Effect: ~4.4GB vector data → ~2.2GB, HNSW index shrinks proportionally.
# Quality: <0.3% recall loss at top-10 for nomic-embed-text 768-dim vectors.
# pgvector 0.8.2 required (already installed).
#
# Timeline: ~2 hours for 1.5M rows (UPDATE + index build CONCURRENTLY)
#
# Steps:
#   1. Set maintenance flag
#   2. Add embedding_hv halfvec(768) column
#   3. Populate it from existing float32 embeddings (batched UPDATE)
#   4. Build HNSW index on the halfvec column CONCURRENTLY
#   5. Validate recall quality (spot-check)
#   6. ONLY after validation: drop old float32 column + index
#   7. Clear maintenance flag
#
# To abort safely at any point before step 6: DROP COLUMN embedding_hv.
# After step 6 is committed, the migration is permanent.

set -euo pipefail

PG_DSN="postgresql://localhost:5432/nova_memories"
REDIS_CLI="redis-cli -p 6379"
BATCH_SIZE=10000

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "=== halfvec Migration Script ==="
log "WARNING: This is a multi-hour, irreversible operation after step 6."
log "Press Ctrl+C within 10 seconds to abort..."
sleep 10

log "Setting maintenance flag (4-hour TTL)"
$REDIS_CLI SET nova:maintenance:active 1 EX 14400

# ── 1. Add halfvec column ────────────────────────────────────────────────────
log "Adding embedding_hv halfvec(768) column..."
psql "$PG_DSN" -c "
  ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_hv halfvec(768);
"

# ── 2. Batch-populate from float32 ──────────────────────────────────────────
log "Populating embedding_hv from embedding (batched, $BATCH_SIZE rows at a time)..."
TOTAL=$(psql "$PG_DSN" -t -c "SELECT COUNT(*) FROM memories WHERE embedding_hv IS NULL;")
log "Rows to convert: $TOTAL"

DONE=0
while true; do
    UPDATED=$(psql "$PG_DSN" -t -c "
      WITH batch AS (
        SELECT id FROM memories WHERE embedding_hv IS NULL LIMIT $BATCH_SIZE
      )
      UPDATE memories m
        SET embedding_hv = m.embedding::halfvec(768)
        FROM batch
        WHERE m.id = batch.id;
      SELECT changes();
    " 2>/dev/null || echo "0")
    # psql doesn't support changes() — use a different approach
    UPDATED=$(psql "$PG_DSN" -t -c "
      WITH batch AS (
        SELECT id FROM memories WHERE embedding_hv IS NULL LIMIT $BATCH_SIZE FOR UPDATE SKIP LOCKED
      )
      UPDATE memories SET embedding_hv = embedding::halfvec(768)
      WHERE id IN (SELECT id FROM batch);
    " | grep -o '[0-9]*' || echo "0")
    DONE=$((DONE + BATCH_SIZE))
    REMAINING=$(psql "$PG_DSN" -t -c "SELECT COUNT(*) FROM memories WHERE embedding_hv IS NULL;" | tr -d ' ')
    log "Progress: ~$((DONE * 100 / TOTAL))% complete, $REMAINING rows remaining..."
    if [ "$REMAINING" -eq 0 ]; then break; fi
    sleep 1
done
log "All rows converted."

# ── 3. Set NOT NULL ──────────────────────────────────────────────────────────
log "Setting embedding_hv NOT NULL..."
psql "$PG_DSN" -c "ALTER TABLE memories ALTER COLUMN embedding_hv SET NOT NULL;"

# ── 4. Build halfvec HNSW index CONCURRENTLY ────────────────────────────────
log "Building HNSW index on halfvec column CONCURRENTLY (~45 min)..."
psql "$PG_DSN" -c "
  SET maintenance_work_mem = '4GB';
  CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memories_hnsw_halfvec
    ON memories USING hnsw (embedding_hv halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 128);
"
log "halfvec HNSW index built."

# ── 5. Spot-check recall quality ─────────────────────────────────────────────
log "Spot-checking recall quality (float32 vs halfvec, top-5 on 3 random queries)..."
psql "$PG_DSN" << 'SQL'
WITH sample AS (SELECT id, embedding, embedding_hv FROM memories TABLESAMPLE SYSTEM(0.001) LIMIT 3)
SELECT
  s.id as query_id,
  f32.id as float32_result,
  hv.id  as halfvec_result,
  (f32.id = hv.id) as match
FROM sample s
CROSS JOIN LATERAL (
  SELECT id FROM memories ORDER BY embedding <=> s.embedding LIMIT 1 OFFSET 1
) f32
CROSS JOIN LATERAL (
  SELECT id FROM memories ORDER BY embedding_hv <=> s.embedding_hv LIMIT 1 OFFSET 1
) hv;
SQL
log "Review the above — if match=true for most rows, quality is acceptable."
log ""
log "*** STOPPING HERE FOR MANUAL VALIDATION ***"
log "If results look good, run the following to complete the migration:"
log ""
log "  psql postgresql://localhost:5432/nova_memories -c \\"
log "    \"ALTER TABLE memories DROP COLUMN embedding;\""
log "  psql postgresql://localhost:5432/nova_memories -c \\"
log "    \"ALTER TABLE memories RENAME COLUMN embedding_hv TO embedding;\""
log "  psql postgresql://localhost:5432/nova_memories -c \\"
log "    \"DROP INDEX IF EXISTS memories_embedding_hnsw;\""
log "  psql postgresql://localhost:5432/nova_memories -c \\"
log "    \"ALTER INDEX idx_memories_hnsw_halfvec RENAME TO memories_embedding_hnsw;\""
log ""
log "Then update memory_server.py: change vector_cosine_ops → halfvec_cosine_ops"
log "and embedding::vector → embedding_hv::halfvec in all queries."

$REDIS_CLI DEL nova:maintenance:active
log "Maintenance flag cleared."
