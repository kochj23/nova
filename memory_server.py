#!/usr/bin/env python3
"""
Nova Vector Memory Server — PostgreSQL + pgvector + Redis Edition (v3.1)

Port:     18790
Database: PostgreSQL 17 + pgvector 0.8.2 (nova_memories)
Index:    HNSW (vector_cosine_ops) — millisecond recall, filtered queries
Queue:    Redis — async write queue for bulk ingest (POST /remember?async=true)
Embeddings: nomic-embed-text via Ollama (http://127.0.0.1:11434)

v3.1 changes (2026-05-12):
  - access_count + accessed_at updated on every recall hit (recency scoring)
  - privacy column (GENERATED ALWAYS AS metadata->>'privacy' STORED) used for
    fast indexed privacy routing — no per-row JSONB eval
  - ef_search tiered: 'fast' (40), 'standard' (100), 'deep' (400) via ?tier=
  - Partial HNSW indexes used automatically when source filter matches an
    indexed source (email_archive, cloud_governance, work_internal, imessage)
  - /recall_deep: boosts recently-accessed memories via accessed_at recency score
  - /stats includes index sizes and HNSW parameters

Architecture:
  - /remember (sync)   → embed → INSERT immediately → return id
  - /remember?async=1  → push to Redis queue → background worker → INSERT
  - /recall            → HNSW cosine search → fetch rows → return results
  - /random, /stats, /health → direct SQL queries

Endpoints:
  POST /remember[?async=1]  { "text": "...", "source": "...", "metadata": {...} }
  GET  /recall?q=...&n=5[&source=...&min_score=0.0&tier=standard]
  POST /recall_batch         { "queries": [{"q": "...", "n": 5, "source": "..."}, ...] }
  GET  /search?q=...&n=10[&source=...]   <- full-text ILIKE, best for proper names
  GET  /random[?n=1&source=...]
  GET  /health
  GET  /stats
  GET  /queue/stats          <- Redis queue status
  DELETE /forget?id=...
  DELETE /forget_all[?source=...]

Author: Jordan Koch / kochj23
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("memory_server")

# ── Config ─────────────────────────────────────────────────────────────────────
PG_DSN      = "postgresql://kochj@127.0.0.1:5432/nova_memories?sslmode=disable"
REDIS_URL   = "redis://127.0.0.1:6379"
REDIS_QUEUE = "nova:memory:ingest"          # list key for write queue
REDIS_CACHE      = "nova:memory:cache"      # hash key for recall cache
CACHE_TTL        = 300                      # 5-minute recall cache TTL
REDIS_DEAD_LETTER = "nova:memory:dead-letter"  # items that fail 3× go here
OLLAMA_BASE      = "http://127.0.0.1:11434"
EMBED_MODEL      = "nomic-embed-text"
DIMS             = 768
DEFAULT_N        = 5
MAX_N            = 50
MAX_EMBED_CHARS  = 6000   # nomic-embed-text: tested safe cutoff (dense Unicode content fails at 7000+)
MAX_INGEST_RETRIES = 3    # items that fail this many times go to dead-letter

# Sources with dedicated partial HNSW indexes (built separately via rebuild script).
# When a recall is filtered to one of these sources, the partial index is used
# automatically — planner sees a smaller, faster index.
PARTIAL_INDEX_SOURCES = frozenset({
    "email_archive",
    "cloud_governance",
    "work_internal",
    "imessage",
})

# ef_search tiers — set per query type via ?tier= param
EF_SEARCH = {
    "fast":     40,    # casual chat, low-stakes — ~40ms
    "standard": 100,   # normal recall — ~150ms
    "deep":     400,   # research agent, important context — ~400ms
}

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
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    embeddings = data.get("embeddings") or data.get("embedding")
    return embeddings[0] if isinstance(embeddings[0], list) else embeddings

# ── Text sanitization ───────────────────────────────────────────────────────────
def _sanitize_text(text: str) -> str:
    """Strip null bytes, control chars, and truncate to embed limit."""
    text = text.replace('\x00', '')                    # null bytes → Postgres UTF8 error
    text = ''.join(c for c in text if c >= ' ' or c in '\n\r\t')  # strip other control chars
    if len(text) > MAX_EMBED_CHARS:
        cut = text[:MAX_EMBED_CHARS]
        last_space = cut.rfind(' ')
        text = cut[:last_space] if last_space > MAX_EMBED_CHARS * 0.8 else cut
    return text.strip()

# ── Redis ingest worker ──────────────────────────────────────────────────────────
async def _ingest_worker():
    """Background worker: drains Redis queue → embeds → inserts into PostgreSQL.

    Sanitizes text before embedding (null bytes, truncation).
    After MAX_INGEST_RETRIES failures, moves item to dead-letter queue.
    """
    logger.info("Redis ingest worker started")
    while True:
        try:
            # Pause during maintenance window (HNSW reindex, VACUUM) to avoid lock contention
            if await _redis.get("nova:maintenance:active"):
                await asyncio.sleep(30)
                continue

            item = await _redis.blpop(REDIS_QUEUE, timeout=5)
            if item is None:
                continue
            data = json.loads(item[1])
            raw_text  = data["text"]
            source    = data.get("source", "unknown")
            metadata  = data.get("metadata", {})
            memory_id = data.get("id", str(uuid.uuid4()))
            created   = data.get("created_at", datetime.now(timezone.utc).isoformat())
            retries   = data.get("_retries", 0)

            text = _sanitize_text(raw_text)
            if not text:
                logger.debug(f"Skipping empty text after sanitization for {memory_id}")
                continue

            try:
                vector = await embed(text)
                vec_str = "[" + ",".join(str(v) for v in vector) + "]"
                text_hash = hashlib.md5(text.encode()).hexdigest()
                try:
                    created_dt = datetime.fromisoformat(created).replace(tzinfo=timezone.utc)
                except Exception:
                    created_dt = datetime.now(timezone.utc)
                async with _pg_pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO memories
                             (id, text, metadata, embedding, source, created_at, text_hash)
                           VALUES ($1, $2, $3, $4::vector, $5, $6, $7)
                           ON CONFLICT (text_hash) DO NOTHING""",
                        memory_id, text, json.dumps(metadata), vec_str, source, created_dt, text_hash
                    )
            except Exception as e:
                retries += 1
                logger.warning(f"Worker failed to ingest {memory_id} (attempt {retries}): {e}")
                if retries >= MAX_INGEST_RETRIES:
                    dead_item = json.dumps({**data, "_retries": retries, "_error": str(e)})
                    await _redis.rpush(REDIS_DEAD_LETTER, dead_item)
                    logger.error(f"Dead-lettered {memory_id} after {retries} failures: {e}")
                else:
                    retry_item = json.dumps({**data, "_retries": retries})
                    await _redis.rpush(REDIS_QUEUE, retry_item)
                    await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Worker error: {e}")
            await asyncio.sleep(1)

# ── App lifecycle ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pg_pool, _redis, _http, _worker_task

    _http    = httpx.AsyncClient(timeout=60.0)

    # PgBouncer runs in transaction pooling mode — session GUCs (like hnsw.ef_search)
    # are reset between transactions. We set them per-query inside transactions.
    # The pool init sets a sensible default for any connection that escapes that pattern.
    async def _pg_init(conn):
        await conn.execute("SET hnsw.ef_search = 100")

    # Retry pool creation — PG may be in recovery or briefly unavailable after restart
    import time as _time
    for _attempt in range(15):
        try:
            _pg_pool = await asyncpg.create_pool(
                PG_DSN, min_size=2, max_size=8, init=_pg_init,
                max_inactive_connection_lifetime=600.0,
                command_timeout=120.0,
                ssl=False,
                direct_tls=False,
            )
            break
        except Exception as _e:
            if _attempt == 14:
                raise
            logger.warning(f"PG pool creation attempt {_attempt+1}/15 failed: {_e} — retrying in 5s")
            await asyncio.sleep(5)
    _redis   = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Ensure pgvector extension and table exist — skip index creation if table already has rows
    async with _pg_pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id           TEXT PRIMARY KEY,
                text         TEXT NOT NULL,
                metadata     JSONB NOT NULL DEFAULT '{}',
                embedding    vector(768),
                source       TEXT NOT NULL DEFAULT 'unknown',
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                text_hash    TEXT,
                tier         TEXT NOT NULL DEFAULT 'long_term',
                tsv          TSVECTOR,
                accessed_at  TIMESTAMPTZ DEFAULT NOW(),
                access_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Fast check if table has rows (avoid full count on 1.6M row table)
        row_count = await conn.fetchval(
            "SELECT reltuples::bigint FROM pg_class WHERE relname = 'memories'"
        )
        if row_count is None or row_count < 0:
            row_count = 0
        if row_count == 0:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS memories_source_created_idx ON memories (source, created_at DESC)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS memories_created_idx ON memories (created_at DESC)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_accessed ON memories (accessed_at DESC NULLS LAST)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories (tier)"
            )
        if row_count == 0:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_indexes WHERE indexname = 'memories_embedding_hnsw'"
            )
            if not exists:
                logger.info("Creating HNSW index (first run only)...")
                await conn.execute("""
                    CREATE INDEX memories_embedding_hnsw
                    ON memories USING hnsw (embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 128)
                """)
                logger.info("HNSW index created")

    _worker_tasks = [asyncio.create_task(_ingest_worker()) for _ in range(4)]
    logger.info("PostgreSQL pool ready, 4 Redis workers started")

    yield

    for t in _worker_tasks:
        t.cancel()
    await _pg_pool.close()
    await _redis.aclose()
    await _http.aclose()

app = FastAPI(title="Nova Memory Server", version="3.1.0-pgvector", lifespan=lifespan)

# ── Models ───────────────────────────────────────────────────────────────────────
class RememberRequest(BaseModel):
    text: str
    metadata: dict = {}
    source: str = "unknown"

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

async def _update_access(ids: list[str]) -> None:
    """Fire-and-forget: update accessed_at + increment access_count for recalled memories."""
    if not ids:
        return
    try:
        async with _pg_pool.acquire() as conn:
            await conn.execute(
                """UPDATE memories
                   SET accessed_at  = now(),
                       access_count = access_count + 1
                   WHERE id = ANY($1::text[])""",
                ids,
            )
    except Exception as e:
        logger.debug(f"access update failed (non-critical): {e}")

# ── Endpoints ─────────────────────────────────────────────────────────────────────

@app.post("/remember")
async def remember(req: RememberRequest, async_mode: bool = Query(False, alias="async")):
    """Store a memory. Use ?async=1 for fire-and-forget bulk ingest (returns immediately)."""
    clean_text = _sanitize_text(req.text)
    if not clean_text:
        raise HTTPException(status_code=400, detail="text cannot be empty after sanitization")

    memory_id = str(uuid.uuid4())
    created   = datetime.now(timezone.utc).isoformat()

    if async_mode:
        payload = json.dumps({
            "id": memory_id, "text": clean_text,
            "source": req.source, "metadata": req.metadata,
            "created_at": created,
        })
        await _redis.rpush(REDIS_QUEUE, payload)
        queue_len = await _redis.llen(REDIS_QUEUE)
        return {"id": memory_id, "status": "queued", "queue_length": queue_len}

    vector = await embed(clean_text)
    text_hash = hashlib.md5(clean_text.encode()).hexdigest()
    try:
        created_dt = datetime.fromisoformat(created).replace(tzinfo=timezone.utc)
    except Exception:
        created_dt = datetime.now(timezone.utc)
    async with _pg_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO memories
                 (id, text, metadata, embedding, source, created_at, text_hash)
               VALUES ($1, $2, $3, $4::vector, $5, $6, $7)
               ON CONFLICT (text_hash) DO NOTHING""",
            memory_id, clean_text, json.dumps(req.metadata),
            _vec_str(vector), req.source, created_dt, text_hash,
        )
    return {"id": memory_id, "dims": len(vector), "status": "stored"}


async def _do_recall(
    q: str,
    n: int = DEFAULT_N,
    source: Optional[str] = None,
    min_score: float = 0.0,
    tier: str = "standard",
) -> dict:
    """Core recall logic shared by /recall and /recall_batch.

    tier controls ef_search: 'fast' (40), 'standard' (100), 'deep' (400).
    When source matches a PARTIAL_INDEX_SOURCES entry, PostgreSQL automatically
    uses the smaller partial HNSW index for that source — no query change needed.

    access_count + accessed_at are updated asynchronously after results are returned.
    """
    if not q.strip():
        return {"memories": [], "query": q, "count": 0}

    n = max(1, min(n, MAX_N))
    ef = EF_SEARCH.get(tier, EF_SEARCH["standard"])

    cache_raw = f"{q}:{n}:{source or 'all'}:{tier}"
    cache_key = f"recall:{hashlib.md5(cache_raw.encode()).hexdigest()}"
    try:
        cached = await _redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    query_vec = await embed(q)
    vec_str   = _vec_str(query_vec)

    # Fetch more candidates than needed so post-filter (min_score, tier) has room
    k = n * 20 if source else n * 3

    async with _pg_pool.acquire() as conn:
        if source:
            # For sources with partial HNSW indexes, the planner will pick the
            # partial index automatically — no query change required.
            # For others, dynamic ef_search compensates for post-filter selectivity.
            if source in PARTIAL_INDEX_SOURCES:
                query_ef = ef
            else:
                src_cache_key = f"src_count:{source}"
                try:
                    src_count_raw = await _redis.get(src_cache_key)
                    src_count = int(src_count_raw) if src_count_raw else None
                except Exception:
                    src_count = None
                if src_count is None:
                    src_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM memories WHERE source = $1 AND tier != 'scratchpad'",
                        source
                    ) or 1
                    try:
                        await _redis.setex(src_cache_key, 300, str(src_count))
                    except Exception:
                        pass
                total = 1535145
                fraction = src_count / total
                query_ef = int(n / max(fraction, 0.0001))
                query_ef = max(ef, min(query_ef, 1000))

            async with conn.transaction():
                await conn.execute(f"SET LOCAL hnsw.ef_search = {query_ef}")
                rows = await conn.fetch(
                    """SELECT id, text, metadata, source, created_at,
                              1 - (embedding <=> $1::vector) AS score
                       FROM memories
                       WHERE source = $2 AND tier != 'scratchpad'
                       ORDER BY embedding <=> $1::vector
                       LIMIT $3""",
                    vec_str, source, k
                )
        else:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL hnsw.ef_search = {ef}")
                rows = await conn.fetch(
                    """SELECT id, text, metadata, source, created_at,
                              1 - (embedding <=> $1::vector) AS score
                       FROM memories
                       WHERE tier != 'scratchpad'
                       ORDER BY embedding <=> $1::vector
                       LIMIT $2""",
                    vec_str, k
                )

    results = [_row_to_result(r, float(r["score"])) for r in rows
               if float(r["score"]) >= min_score]
    results.sort(key=lambda x: x.score, reverse=True)
    top = results[:n]

    # Update access tracking asynchronously — don't block the response
    asyncio.create_task(_update_access([m.id for m in top]))

    response = {"memories": [m.model_dump() for m in top], "query": q, "count": len(top)}

    try:
        await _redis.setex(cache_key, CACHE_TTL, json.dumps(response, default=str))
    except Exception:
        pass

    return response


@app.get("/recall")
async def recall(
    q: str = Query(...),
    n: int = Query(DEFAULT_N, ge=1, le=MAX_N),
    source: Optional[str] = Query(None),
    min_score: float = Query(0.0),
    tier: str = Query("standard", pattern="^(fast|standard|deep)$"),
):
    """Semantic search using HNSW cosine similarity with Redis caching.

    tier: 'fast' (~40ms), 'standard' (~150ms, default), 'deep' (~400ms)
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="q cannot be empty")
    return await _do_recall(q, n, source, min_score, tier)


@app.post("/recall_batch")
async def recall_batch(request: Request):
    """Batch recall: run multiple semantic queries in one request.

    Body: {"queries": [{"q": "...", "n": 5, "source": "...", "tier": "standard"}, ...]}
    Max 5 queries per batch.
    """
    body = await request.json()
    queries = body.get("queries", [])

    if not queries:
        return JSONResponse({"results": [], "count": 0})

    queries = queries[:5]

    tasks = []
    for query in queries:
        q        = query.get("q", "")
        n        = query.get("n", DEFAULT_N)
        source   = query.get("source")
        min_score = query.get("min_score", 0.0)
        tier     = query.get("tier", "standard")
        tasks.append(_do_recall(q, n, source, min_score, tier))

    results_raw = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for i, res in enumerate(results_raw):
        q = queries[i].get("q", "")
        if isinstance(res, Exception):
            results.append({"query": q, "memories": [], "error": str(res)})
        else:
            results.append({"query": q, "memories": res.get("memories", [])})

    return JSONResponse({"results": results, "count": len(results)})


@app.get("/search")
async def text_search(
    q: str = Query(...),
    n: int = Query(10, ge=1, le=50),
    source: Optional[str] = Query(None),
    mode: str = Query("auto"),
):
    """Text search. mode=fts uses tsvector (fast), mode=ilike uses pattern match, mode=auto tries FTS first."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="q cannot be empty")

    async with _pg_pool.acquire() as conn:
        rows = []

        if mode in ("fts", "auto"):
            try:
                if source:
                    rows = await conn.fetch(
                        "SELECT id, text, metadata, source, created_at, "
                        "ts_rank(tsv, plainto_tsquery('english', $1)) as rank "
                        "FROM memories WHERE tsv @@ plainto_tsquery('english', $1) AND source = $2 "
                        "ORDER BY rank DESC LIMIT $3",
                        q, source, n
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT id, text, metadata, source, created_at, "
                        "ts_rank(tsv, plainto_tsquery('english', $1)) as rank "
                        "FROM memories WHERE tsv @@ plainto_tsquery('english', $1) "
                        "ORDER BY rank DESC LIMIT $2",
                        q, n
                    )
            except Exception:
                rows = []

        if not rows and mode in ("ilike", "auto"):
            pattern = f"%{q}%"
            if source:
                rows = await conn.fetch(
                    "SELECT id, text, metadata, source, created_at FROM memories "
                    "WHERE text ILIKE $1 AND source = $2 ORDER BY created_at DESC LIMIT $3",
                    pattern, source, n
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, text, metadata, source, created_at FROM memories "
                    "WHERE text ILIKE $1 ORDER BY created_at DESC LIMIT $2",
                    pattern, n
                )

    memories = [{"id": r["id"], "text": r["text"],
                 "metadata": r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"]),
                 "source": r["source"], "created_at": str(r["created_at"])} for r in rows]
    return {"memories": memories, "query": q, "count": len(memories), "mode": "fts" if rows and mode != "ilike" else "ilike"}


@app.get("/recall/deep")
async def deep_recall(
    q: str = Query(...),
    n: int = Query(5, ge=1, le=50),
    source: Optional[str] = Query(None),
    min_score: float = Query(0.0),
):
    """Tier-aware recall with cross-link expansion and recency boost.
    Returns working memory first, then long-term, with linked memories attached.
    Boosts recently-accessed memories — frequently recalled memories surface higher."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="q cannot be empty")

    query_vec = await embed(q)
    vec_str = _vec_str(query_vec)
    k = n * 20 if source else n * 3

    async with _pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL hnsw.ef_search = 400")
            if source:
                rows = await conn.fetch(
                    """SELECT id, text, metadata, source, created_at, tier,
                              accessed_at, access_count,
                              1 - (embedding <=> $1::vector) AS score
                       FROM memories
                       WHERE source = $2 AND tier IN ('working', 'long_term')
                       ORDER BY CASE tier WHEN 'working' THEN 0 ELSE 1 END,
                                embedding <=> $1::vector
                       LIMIT $3""",
                    vec_str, source, k
                )
            else:
                rows = await conn.fetch(
                    """SELECT id, text, metadata, source, created_at, tier,
                              accessed_at, access_count,
                              1 - (embedding <=> $1::vector) AS score
                       FROM memories
                       WHERE tier IN ('working', 'long_term')
                       ORDER BY CASE tier WHEN 'working' THEN 0 ELSE 1 END,
                                embedding <=> $1::vector
                       LIMIT $2""",
                    vec_str, k
                )

        # Recency boost: memories accessed recently score higher
        now_ts = datetime.now(timezone.utc).timestamp()
        scored = []
        for r in rows:
            base_score = float(r["score"])
            if base_score < min_score:
                continue
            # Decay: accessed within 7 days → +0.05 boost, within 1 day → +0.10
            if r["accessed_at"]:
                age_days = (now_ts - r["accessed_at"].timestamp()) / 86400
                recency_boost = 0.10 if age_days < 1 else (0.05 if age_days < 7 else 0.0)
            else:
                recency_boost = 0.0
            # Frequency boost: cap at +0.05 for memories accessed 10+ times
            freq_boost = min(r["access_count"] / 200, 0.05)
            scored.append((r, base_score + recency_boost + freq_boost))

        scored.sort(key=lambda x: x[1], reverse=True)
        results = [r for r, _ in scored[:n]]

        # 2-hop graph traversal: top results → direct links → their links
        linked = []
        if results:
            top_ids = [r["id"] for r in results[:3]]

            hop1_rows = await conn.fetch(
                """SELECT DISTINCT m.id, m.text, m.source, m.created_at,
                         ml.link_type, ml.strength, 1 as hop
                   FROM memory_links ml
                   JOIN memories m ON m.id = CASE
                       WHEN ml.source_id = ANY($1::text[]) THEN ml.target_id
                       ELSE ml.source_id END
                   WHERE ml.source_id = ANY($1::text[]) OR ml.target_id = ANY($1::text[])
                   ORDER BY ml.strength DESC
                   LIMIT 5""",
                top_ids
            )

            hop1_ids = []
            for lr in hop1_rows:
                linked.append({
                    "id": lr["id"], "text": lr["text"], "source": lr["source"],
                    "created_at": str(lr["created_at"]),
                    "link_type": lr["link_type"], "strength": float(lr["strength"]),
                    "hop": 1,
                })
                hop1_ids.append(lr["id"])

            if hop1_ids:
                all_seen = set(top_ids + hop1_ids)
                hop2_rows = await conn.fetch(
                    """SELECT DISTINCT m.id, m.text, m.source, m.created_at,
                              ml.link_type, ml.strength, 2 as hop
                       FROM memory_links ml
                       JOIN memories m ON m.id = CASE
                           WHEN ml.source_id = ANY($1::text[]) THEN ml.target_id
                           ELSE ml.source_id END
                       WHERE (ml.source_id = ANY($1::text[]) OR ml.target_id = ANY($1::text[]))
                         AND m.id != ALL($2::text[])
                       ORDER BY ml.strength DESC
                       LIMIT 3""",
                    hop1_ids, list(all_seen)
                )
                for lr in hop2_rows:
                    linked.append({
                        "id": lr["id"], "text": lr["text"], "source": lr["source"],
                        "created_at": str(lr["created_at"]),
                        "link_type": lr["link_type"], "strength": float(lr["strength"]),
                        "hop": 2,
                    })

    # Update access tracking for top results
    asyncio.create_task(_update_access([r["id"] for r in results]))

    memories = []
    for r, boosted_score in [(r, s) for r, s in scored[:n]]:
        memories.append({
            "id": r["id"], "text": r["text"],
            "metadata": r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"]),
            "source": r["source"], "created_at": str(r["created_at"]),
            "tier": r["tier"],
            "score": round(boosted_score, 4),
            "access_count": r["access_count"],
        })

    return {"memories": memories, "linked": linked, "query": q, "count": len(memories)}


@app.post("/memory/working")
async def set_working_memory(request: Request):
    """Promote a memory to working tier (active conversation context)."""
    body = await request.json()
    memory_id = body.get("id")
    if not memory_id:
        raise HTTPException(status_code=400, detail="id required")
    async with _pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET tier = 'working' WHERE id = $1 AND tier = 'long_term'",
            memory_id
        )
    return {"status": "ok", "id": memory_id, "tier": "working"}


@app.post("/memory/demote")
async def demote_working_memory():
    """Demote all working memories back to long_term (call on session reset)."""
    async with _pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "UPDATE memories SET tier = 'long_term' WHERE tier = 'working' RETURNING COUNT(*)"
        ) or 0
    return {"status": "ok", "demoted": count}


@app.get("/links")
async def get_links(id: str = Query(...)):
    """Get all memories linked to a given memory ID."""
    async with _pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT m.id, m.text, m.source, m.created_at, ml.link_type, ml.strength
               FROM memory_links ml
               JOIN memories m ON m.id = CASE WHEN ml.source_id = $1 THEN ml.target_id ELSE ml.source_id END
               WHERE ml.source_id = $1 OR ml.target_id = $1
               ORDER BY ml.strength DESC
               LIMIT 20""",
            id
        )
    links = [{"id": r["id"], "text": r["text"], "source": r["source"],
              "created_at": str(r["created_at"]), "link_type": r["link_type"],
              "strength": float(r["strength"])} for r in rows]
    return {"id": id, "links": links, "count": len(links)}


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
        count = await conn.fetchval(
            "SELECT reltuples::bigint FROM pg_class WHERE relname = 'memories'"
        )
    queue_len = await _redis.llen(REDIS_QUEUE)
    return {"status": "ok", "count": count or 0, "model": EMBED_MODEL,
            "backend": "postgresql+pgvector", "queue_length": queue_len,
            "version": "3.1.0"}


@app.get("/stats")
async def stats():
    async with _pg_pool.acquire() as conn:
        count    = await conn.fetchval(
            "SELECT reltuples::bigint FROM pg_class WHERE relname = 'memories'"
        ) or 0
        by_src   = await conn.fetch(
            "SELECT source, COUNT(*) as n FROM memories GROUP BY source ORDER BY n DESC"
        )
        db_size  = await conn.fetchval(
            "SELECT pg_size_pretty(pg_database_size('nova_memories'))"
        )
        idx_info = await conn.fetch(
            """SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) as size,
                      idx_scan
               FROM pg_stat_user_indexes WHERE relname = 'memories'
               ORDER BY pg_relation_size(indexrelid) DESC"""
        )
        hnsw_params = await conn.fetchrow(
            """SELECT reloptions FROM pg_class
               WHERE relname = 'memories_embedding_hnsw'"""
        )
    queue_len = await _redis.llen(REDIS_QUEUE)
    return {
        "count": count,
        "dims": DIMS,
        "backend": "postgresql+pgvector",
        "db_size": db_size,
        "model": EMBED_MODEL,
        "queue_length": queue_len,
        "hnsw_params": hnsw_params["reloptions"] if hnsw_params else None,
        "indexes": [{"name": r["indexrelname"], "size": r["size"], "scans": r["idx_scan"]}
                    for r in idx_info],
        "by_source": {row["source"]: row["n"] for row in by_src},
    }


@app.get("/queue/stats")
async def queue_stats():
    queue_len = await _redis.llen(REDIS_QUEUE)
    dead_len  = await _redis.llen(REDIS_DEAD_LETTER)
    return {"queue": REDIS_QUEUE, "pending": queue_len,
            "dead_letter": REDIS_DEAD_LETTER, "dead_letter_count": dead_len}


@app.get("/queue/dead-letter")
async def dead_letter_queue(n: int = Query(20)):
    """Inspect items that failed MAX_INGEST_RETRIES times and were dead-lettered."""
    items_raw = await _redis.lrange(REDIS_DEAD_LETTER, 0, n - 1)
    items = []
    for raw in items_raw:
        try:
            d = json.loads(raw)
            items.append({
                "id": d.get("id"), "source": d.get("source"),
                "retries": d.get("_retries"), "error": d.get("_error"),
                "text_preview": (d.get("text") or "")[:80],
            })
        except Exception:
            pass
    return {"count": len(items), "total": await _redis.llen(REDIS_DEAD_LETTER), "items": items}


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
    uvicorn.run(app, host="0.0.0.0", port=18790, log_level="info", log_config=None)
