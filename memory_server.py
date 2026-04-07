#!/usr/bin/env python3
"""
Nova Vector Memory Server — PostgreSQL + pgvector + Redis Edition (v3.0)

Port:     18790
Database: PostgreSQL 17 + pgvector 0.8.2 (nova_memories)
Index:    HNSW (vector_cosine_ops) — millisecond recall, filtered queries
Queue:    Redis — async write queue for bulk ingest (POST /remember?async=true)
Embeddings: nomic-embed-text via Ollama (http://127.0.0.1:11434)

Architecture:
  - /remember (sync)   → embed → INSERT immediately → return id
  - /remember?async=1  → push to Redis queue → background worker → INSERT
  - /recall            → HNSW cosine search → fetch rows → return results
  - /random, /stats, /health → direct SQL queries

Endpoints:
  POST /remember[?async=1]  { "text": "...", "source": "...", "metadata": {...} }
  GET  /recall?q=...&n=5[&source=...&min_score=0.0]
  GET  /random[?n=1&source=...]
  GET  /health
  GET  /stats
  GET  /queue/stats          <- Redis queue status
  DELETE /forget?id=...
  DELETE /forget_all[?source=...]

Author: Jordan Koch / kochj23
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import asyncpg
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger("memory_server")

# ── Config ─────────────────────────────────────────────────────────────────────
PG_DSN      = "postgresql://localhost/nova_memories"
REDIS_URL   = "redis://localhost:6379"
REDIS_QUEUE = "nova:memory:ingest"          # list key for write queue
OLLAMA_BASE = "http://127.0.0.1:11434"
EMBED_MODEL = "nomic-embed-text"
DIMS        = 768
DEFAULT_N   = 5
MAX_N       = 50

# ── Global connections ──────────────────────────────────────────────────────────
_pg_pool:    asyncpg.Pool | None = None
_redis:      aioredis.Redis | None = None
_http:       httpx.AsyncClient | None = None
_worker_task: asyncio.Task | None = None

# ── Embedding ───────────────────────────────────────────────────────────────────
async def embed(text: str) -> list[float]:
    resp = await _http.post(
        f"{OLLAMA_BASE}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
    )
    resp.raise_for_status()
    data = resp.json()
    embeddings = data.get("embeddings") or data.get("embedding")
    return embeddings[0] if isinstance(embeddings[0], list) else embeddings

# ── Redis ingest worker ──────────────────────────────────────────────────────────
async def _ingest_worker():
    """Background worker: drains Redis queue → embeds → inserts into PostgreSQL."""
    logger.info("Redis ingest worker started")
    while True:
        try:
            item = await _redis.blpop(REDIS_QUEUE, timeout=5)
            if item is None:
                continue
            data = json.loads(item[1])
            text      = data["text"]
            source    = data.get("source", "unknown")
            metadata  = data.get("metadata", {})
            memory_id = data.get("id", str(uuid.uuid4()))
            created   = data.get("created_at", datetime.utcnow().isoformat())

            try:
                vector = await embed(text)
                vec_str = "[" + ",".join(str(v) for v in vector) + "]"
                async with _pg_pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO memories (id, text, metadata, embedding, source, created_at)
                           VALUES ($1, $2, $3, $4::vector, $5, $6)
                           ON CONFLICT (id) DO NOTHING""",
                        memory_id, text, json.dumps(metadata), vec_str, source, created
                    )
            except Exception as e:
                logger.warning(f"Worker failed to ingest {memory_id}: {e}")
                # Re-queue with a delay marker (simple retry)
                await _redis.rpush(REDIS_QUEUE, item[1])
                await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Worker error: {e}")
            await asyncio.sleep(1)

# ── App lifecycle ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pg_pool, _redis, _http, _worker_task

    _http    = httpx.AsyncClient(timeout=60.0)
    _pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=10)
    _redis   = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Ensure pgvector extension and table exist
    async with _pg_pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id         TEXT PRIMARY KEY,
                text       TEXT NOT NULL,
                metadata   JSONB NOT NULL DEFAULT '{}',
                embedding  vector(768),
                source     TEXT NOT NULL DEFAULT 'unknown',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS memories_source_idx ON memories (source)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS memories_created_idx ON memories (created_at DESC)"
        )
        # HNSW index — only create if not already present (slow to build on large tables)
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'memories_embedding_hnsw'"
        )
        if not exists:
            logger.info("Creating HNSW index (first run only, takes ~1 min on 100K rows)...")
            await conn.execute("""
                CREATE INDEX memories_embedding_hnsw
                ON memories USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
            """)
            logger.info("HNSW index created")

    # Start Redis ingest worker
    _worker_task = asyncio.create_task(_ingest_worker())
    logger.info("PostgreSQL pool ready, Redis worker started")

    yield

    _worker_task.cancel()
    await _pg_pool.close()
    await _redis.aclose()
    await _http.aclose()

app = FastAPI(title="Nova Memory Server", version="3.0.0-pgvector", lifespan=lifespan)

# ── Models ───────────────────────────────────────────────────────────────────────
class RememberRequest(BaseModel):
    text: str
    metadata: dict = {}
    source: str = "slack"

class MemoryResult(BaseModel):
    id: str
    text: str
    metadata: dict
    source: str
    created_at: str
    score: float

# ── Helpers ──────────────────────────────────────────────────────────────────────
def _vec_str(vector: list[float]) -> str:
    return "[" + ",".join(str(v) for v in vector) + "]"

def _row_to_result(row, score: float) -> MemoryResult:
    return MemoryResult(
        id=row["id"], text=row["text"],
        metadata=row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"]),
        source=row["source"],
        created_at=str(row["created_at"]),
        score=round(score, 4),
    )

# ── Endpoints ─────────────────────────────────────────────────────────────────────

@app.post("/remember")
async def remember(req: RememberRequest, async_mode: bool = Query(False, alias="async")):
    """Store a memory. Use ?async=1 for fire-and-forget bulk ingest (returns immediately)."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text cannot be empty")

    memory_id = str(uuid.uuid4())
    created   = datetime.utcnow().isoformat()

    if async_mode:
        # Push to Redis queue — returns instantly
        payload = json.dumps({
            "id": memory_id, "text": req.text,
            "source": req.source, "metadata": req.metadata,
            "created_at": created,
        })
        await _redis.rpush(REDIS_QUEUE, payload)
        queue_len = await _redis.llen(REDIS_QUEUE)
        return {"id": memory_id, "status": "queued", "queue_length": queue_len}

    # Sync path: embed + insert immediately
    vector = await embed(req.text)
    async with _pg_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO memories (id, text, metadata, embedding, source, created_at)
               VALUES ($1, $2, $3, $4::vector, $5, $6)
               ON CONFLICT (id) DO NOTHING""",
            memory_id, req.text, json.dumps(req.metadata),
            _vec_str(vector), req.source, created,
        )
    return {"id": memory_id, "dims": len(vector), "status": "stored"}


@app.get("/recall")
async def recall(
    q: str = Query(...),
    n: int = Query(DEFAULT_N, ge=1, le=MAX_N),
    source: Optional[str] = Query(None),
    min_score: float = Query(0.0),
):
    """Semantic search using HNSW cosine similarity."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="q cannot be empty")

    query_vec = await embed(q)
    vec_str   = _vec_str(query_vec)

    # Fetch more candidates when filtering by source (post-filter)
    k = n * 5 if source else n * 2

    async with _pg_pool.acquire() as conn:
        # pgvector cosine distance: 1 - cosine_similarity
        # <=> operator returns distance (0=identical, 2=opposite)
        # Convert to similarity: 1 - distance
        if source:
            rows = await conn.fetch(
                """SELECT id, text, metadata, source, created_at,
                          1 - (embedding <=> $1::vector) AS score
                   FROM memories
                   WHERE source = $2
                   ORDER BY embedding <=> $1::vector
                   LIMIT $3""",
                vec_str, source, k
            )
        else:
            rows = await conn.fetch(
                """SELECT id, text, metadata, source, created_at,
                          1 - (embedding <=> $1::vector) AS score
                   FROM memories
                   ORDER BY embedding <=> $1::vector
                   LIMIT $2""",
                vec_str, k
            )

    results = [_row_to_result(r, float(r["score"])) for r in rows
               if float(r["score"]) >= min_score]
    results.sort(key=lambda x: x.score, reverse=True)
    top = results[:n]
    return {"memories": [m.model_dump() for m in top], "query": q, "count": len(top)}


@app.get("/random")
async def random_memory(source: Optional[str] = Query(None), n: int = Query(1)):
    async with _pg_pool.acquire() as conn:
        if source:
            rows = await conn.fetch(
                "SELECT id, text, metadata, source, created_at FROM memories "
                "WHERE source = $1 ORDER BY RANDOM() LIMIT $2", source, n
            )
        else:
            rows = await conn.fetch(
                "SELECT id, text, metadata, source, created_at FROM memories "
                "ORDER BY RANDOM() LIMIT $1", n
            )
    memories = [{"id": r["id"], "text": r["text"],
                 "metadata": r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"]),
                 "source": r["source"], "created_at": str(r["created_at"])} for r in rows]
    return {"memories": memories, "count": len(memories)}


@app.get("/health")
async def health():
    async with _pg_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM memories")
    queue_len = await _redis.llen(REDIS_QUEUE)
    return {"status": "ok", "count": count, "model": EMBED_MODEL,
            "backend": "postgresql+pgvector", "queue_length": queue_len}


@app.get("/stats")
async def stats():
    async with _pg_pool.acquire() as conn:
        count    = await conn.fetchval("SELECT COUNT(*) FROM memories")
        by_src   = await conn.fetch(
            "SELECT source, COUNT(*) as n FROM memories GROUP BY source ORDER BY n DESC"
        )
        db_size  = await conn.fetchval(
            "SELECT pg_size_pretty(pg_database_size('nova_memories'))"
        )
    queue_len = await _redis.llen(REDIS_QUEUE)
    return {
        "count": count,
        "dims": DIMS,
        "backend": "postgresql+pgvector",
        "db_size": db_size,
        "model": EMBED_MODEL,
        "queue_length": queue_len,
        "by_source": {row["source"]: row["n"] for row in by_src},
    }


@app.get("/queue/stats")
async def queue_stats():
    queue_len = await _redis.llen(REDIS_QUEUE)
    return {"queue": REDIS_QUEUE, "pending": queue_len}


@app.delete("/forget")
async def forget(id: str = Query(...)):
    async with _pg_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM memories WHERE id = $1", id)
    deleted = int(result.split()[-1])
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True, "id": id}


@app.delete("/forget_all")
async def forget_all(source: Optional[str] = Query(None)):
    async with _pg_pool.acquire() as conn:
        if source:
            result = await conn.execute("DELETE FROM memories WHERE source = $1", source)
        else:
            result = await conn.execute("DELETE FROM memories")
    deleted = int(result.split()[-1])
    return {"deleted": deleted}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=18790, log_level="info", log_config=None)
