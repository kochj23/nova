#!/usr/bin/env python3
"""
Nova Vector Memory Server — FAISS Edition
Port: 18790 | DB: ~/.openclaw/memory_db/nova_memories.db
Embeddings: nomic-embed-text via Ollama (http://127.0.0.1:11434)
Index:      FAISS IndexIDMap(IndexFlatIP) — ~5ms recall vs ~5000ms before

Endpoints:
  POST /remember        { "text": "...", "metadata": {...} }  -> { "id": "...", "dims": N }
  GET  /recall?q=...&n=5                                      -> { "memories": [...] }
  GET  /random?n=1                                            -> { "memories": [...] }
  GET  /health                                                -> { "status": "ok", "count": N }
  GET  /stats                                                 -> { "count": N, "dims": N, ... }
  DELETE /forget?id=...                                       -> { "deleted": true }
  DELETE /forget_all                                          -> { "deleted": N }

Author: Jordan Koch / kochj23
"""

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import faiss
import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger("memory_server")

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH       = Path.home() / ".openclaw" / "memory_db" / "nova_memories.db"
INDEX_PATH    = Path.home() / ".openclaw" / "memory_db" / "nova_memories.faiss"
ROWMAP_PATH   = Path.home() / ".openclaw" / "memory_db" / "nova_rowmap.npy"
OLLAMA_BASE   = "http://127.0.0.1:11434"
EMBED_MODEL   = "nomic-embed-text"
DEFAULT_N     = 5
MAX_N         = 50
DIMS          = 768   # nomic-embed-text output size

# ── FAISS index (global, thread-safe writes via lock) ─────────────────────────
_faiss_index: faiss.IndexIDMap | None = None
_faiss_rowids: list[int] = []          # position i → SQLite rowid
_faiss_lock = threading.Lock()
_faiss_ready = False

def _build_faiss_index() -> tuple[faiss.IndexIDMap, list[int]]:
    """Load all embeddings from SQLite and build a FAISS IndexIDMap."""
    t0 = time.time()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    rows = conn.execute(
        "SELECT rowid, embedding FROM memories ORDER BY rowid"
    ).fetchall()
    conn.close()

    if not rows:
        flat = faiss.IndexFlatIP(DIMS)
        idx  = faiss.IndexIDMap(flat)
        return idx, []

    rowids   = np.array([r[0] for r in rows], dtype=np.int64)
    vecs_raw = [json.loads(r[1]) for r in rows]
    vecs     = np.array(vecs_raw, dtype=np.float32)
    faiss.normalize_L2(vecs)   # cosine similarity = inner product on unit vectors

    flat = faiss.IndexFlatIP(DIMS)
    idx  = faiss.IndexIDMap(flat)
    idx.add_with_ids(vecs, rowids)

    logger.info(f"FAISS index built: {len(rows):,} vectors in {time.time()-t0:.1f}s")
    return idx, rowids.tolist()

def _save_index():
    """Persist FAISS index to disk (background, non-critical)."""
    try:
        if _faiss_index is not None:
            faiss.write_index(_faiss_index, str(INDEX_PATH))
    except Exception as e:
        logger.warning(f"Could not save FAISS index: {e}")

def _load_or_build_index():
    """Try to load persisted index, fall back to building from SQLite."""
    global _faiss_index, _faiss_ready
    try:
        if INDEX_PATH.exists():
            # Verify index count matches DB
            conn = sqlite3.connect(str(DB_PATH), timeout=30)
            db_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            conn.close()
            idx = faiss.read_index(str(INDEX_PATH))
            if abs(idx.ntotal - db_count) <= 100:   # allow small drift
                logger.info(f"FAISS index loaded from disk: {idx.ntotal:,} vectors")
                with _faiss_lock:
                    _faiss_index = idx
                _faiss_ready = True
                return
    except Exception as e:
        logger.warning(f"Could not load FAISS index from disk ({e}), rebuilding...")

    idx, _ = _build_faiss_index()
    with _faiss_lock:
        _faiss_index = idx
    _faiss_ready = True
    _save_index()

def _add_to_faiss(rowid: int, vector: list[float]):
    """Add a single new vector to the in-memory FAISS index."""
    if not _faiss_ready:
        return
    vec = np.array([vector], dtype=np.float32)
    faiss.normalize_L2(vec)
    with _faiss_lock:
        _faiss_index.add_with_ids(vec, np.array([rowid], dtype=np.int64))

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id          TEXT PRIMARY KEY,
                text        TEXT NOT NULL,
                metadata    TEXT NOT NULL DEFAULT '{}',
                embedding   TEXT NOT NULL,
                dims        INTEGER NOT NULL,
                source      TEXT NOT NULL DEFAULT 'unknown',
                created_at  TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source  ON memories(source)")
        conn.commit()

# ── Embedding ─────────────────────────────────────────────────────────────────
_http_client: httpx.AsyncClient | None = None

async def embed(text: str) -> list[float]:
    resp = await _http_client.post(
        f"{OLLAMA_BASE}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
    )
    resp.raise_for_status()
    data = resp.json()
    embeddings = data.get("embeddings") or data.get("embedding")
    if isinstance(embeddings[0], list):
        return embeddings[0]
    return embeddings

# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    init_db()
    _http_client = httpx.AsyncClient(timeout=60.0)
    # Build FAISS index in background so server starts instantly
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _load_or_build_index)
    yield
    await _http_client.aclose()
    _save_index()

app = FastAPI(title="Nova Memory Server", version="2.0.0-faiss", lifespan=lifespan)

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

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/remember")
async def remember(req: RememberRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text cannot be empty")

    vector = await embed(req.text)
    memory_id = str(uuid.uuid4())

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO memories (id, text, metadata, embedding, dims, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (memory_id, req.text, json.dumps(req.metadata), json.dumps(vector),
             len(vector), req.source, datetime.utcnow().isoformat()),
        )
        conn.commit()
        rowid = conn.execute("SELECT rowid FROM memories WHERE id = ?", (memory_id,)).fetchone()[0]

    # Add to live FAISS index
    _add_to_faiss(rowid, vector)

    return {"id": memory_id, "dims": len(vector)}


@app.get("/recall")
async def recall(
    q: str = Query(..., description="Query text"),
    n: int = Query(DEFAULT_N, ge=1, le=MAX_N),
    source: Optional[str] = Query(None, description="Filter by source"),
    min_score: float = Query(0.0, description="Minimum cosine similarity"),
):
    if not q.strip():
        raise HTTPException(status_code=400, detail="q cannot be empty")

    query_vec = np.array(await embed(q), dtype=np.float32).reshape(1, -1)
    faiss.normalize_L2(query_vec)

    # ── FAISS fast path ───────────────────────────────────────────────────────
    if _faiss_ready and _faiss_index is not None and _faiss_index.ntotal > 0:
        # Fetch more candidates when filtering by source
        k = min(n * 10 if source else n * 2, _faiss_index.ntotal)
        with _faiss_lock:
            scores_arr, rowids_arr = _faiss_index.search(query_vec, k)
        scores_arr = scores_arr[0]
        rowids_arr = rowids_arr[0]

        # Filter out invalid results (FAISS returns -1 for empty slots)
        valid = [(int(rid), float(score)) for rid, score in zip(rowids_arr, scores_arr)
                 if rid >= 0 and score >= min_score]
        if not valid:
            return {"memories": [], "query": q, "count": 0}

        rowid_list = [r[0] for r in valid]
        score_map  = {r[0]: r[1] for r in valid}

        placeholders = ",".join("?" * len(rowid_list))
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT rowid, id, text, metadata, source, created_at "
                f"FROM memories WHERE rowid IN ({placeholders})",
                rowid_list,
            ).fetchall()

        results = []
        for row in rows:
            if source and row["source"] != source:
                continue
            results.append(MemoryResult(
                id=row["id"], text=row["text"],
                metadata=json.loads(row["metadata"]),
                source=row["source"], created_at=row["created_at"],
                score=round(score_map[row["rowid"]], 4),
            ))

        results.sort(key=lambda x: x.score, reverse=True)
        top = results[:n]
        return {"memories": [m.model_dump() for m in top], "query": q, "count": len(top)}

    # ── Fallback: numpy scan (used while FAISS is still building) ─────────────
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, text, metadata, embedding, source, created_at FROM memories"
            + (" WHERE source = ?" if source else ""),
            (source,) if source else (),
        ).fetchall()

    if not rows:
        return {"memories": [], "query": q, "count": 0}

    qv = query_vec[0]
    scored = []
    for row in rows:
        vec = np.array(json.loads(row["embedding"]), dtype=np.float32)
        n_q, n_v = np.linalg.norm(qv), np.linalg.norm(vec)
        score = float(np.dot(qv, vec) / (n_q * n_v)) if n_q and n_v else 0.0
        if score >= min_score:
            scored.append(MemoryResult(
                id=row["id"], text=row["text"],
                metadata=json.loads(row["metadata"]),
                source=row["source"], created_at=row["created_at"],
                score=round(score, 4),
            ))

    scored.sort(key=lambda x: x.score, reverse=True)
    return {"memories": [m.model_dump() for m in scored[:n]], "query": q, "count": len(scored[:n])}


@app.get("/health")
async def health():
    faiss_status = "ready" if _faiss_ready else "building"
    faiss_count  = _faiss_index.ntotal if _faiss_index else 0
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    return {"status": "ok", "count": count, "model": EMBED_MODEL,
            "faiss": faiss_status, "faiss_vectors": faiss_count}


@app.get("/stats")
async def stats():
    with get_conn() as conn:
        count    = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        dims_row = conn.execute("SELECT dims FROM memories LIMIT 1").fetchone()
        by_src   = conn.execute(
            "SELECT source, COUNT(*) as n FROM memories GROUP BY source"
        ).fetchall()
    return {
        "count": count,
        "dims": dims_row["dims"] if dims_row else 0,
        "db": str(DB_PATH),
        "model": EMBED_MODEL,
        "faiss": "ready" if _faiss_ready else "building",
        "faiss_vectors": _faiss_index.ntotal if _faiss_index else 0,
        "by_source": {row["source"]: row["n"] for row in by_src},
    }


@app.get("/random")
async def random_memory(
    source: Optional[str] = Query(None),
    n: int = Query(1),
):
    with get_conn() as conn:
        if source:
            rows = conn.execute(
                "SELECT id, text, metadata, source, created_at FROM memories "
                "WHERE source = ? ORDER BY RANDOM() LIMIT ?", (source, n)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, text, metadata, source, created_at FROM memories "
                "ORDER BY RANDOM() LIMIT ?", (n,)
            ).fetchall()
    memories = [{"id": r["id"], "text": r["text"], "metadata": json.loads(r["metadata"]),
                 "source": r["source"], "created_at": r["created_at"]} for r in rows]
    return {"memories": memories, "count": len(memories)}


@app.delete("/forget")
async def forget(id: str = Query(...)):
    with get_conn() as conn:
        result = conn.execute("DELETE FROM memories WHERE id = ?", (id,))
        conn.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True, "id": id}


@app.delete("/forget_all")
async def forget_all(source: Optional[str] = Query(None)):
    with get_conn() as conn:
        if source:
            result = conn.execute("DELETE FROM memories WHERE source = ?", (source,))
        else:
            result = conn.execute("DELETE FROM memories")
        conn.commit()
    # Rebuild FAISS index after bulk delete
    threading.Thread(target=_load_or_build_index, daemon=True).start()
    return {"deleted": result.rowcount}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=18790, log_level="info",
                log_config=None)
