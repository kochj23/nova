#!/usr/bin/env python3
"""
Nova Vector Memory Server — FAISS Edition v2.1
Port: 18790 | DB: ~/.openclaw/memory_db/nova_memories.db
Embeddings: nomic-embed-text via Ollama (http://127.0.0.1:11434)
Index:      FAISS IndexIDMap(IndexFlatIP) — ~24ms recall on 100K+ vectors

Key improvements over v2.0:
- Persistent SQLite connection (single conn reused) — eliminates "unable to open db" crashes
- asyncio write lock — serializes concurrent /remember calls, prevents deadlocks
- 64MB SQLite page cache
- FAISS index persisted to .faiss file — instant startup on restart

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
DB_PATH    = Path.home() / ".openclaw" / "memory_db" / "nova_memories.db"
INDEX_PATH = Path.home() / ".openclaw" / "memory_db" / "nova_memories.faiss"
OLLAMA_BASE = "http://127.0.0.1:11434"
EMBED_MODEL = "nomic-embed-text"
DEFAULT_N   = 5
MAX_N       = 50
DIMS        = 768

# ── Persistent SQLite connection ──────────────────────────────────────────────
# One connection, reused for all requests. Eliminates "unable to open database
# file" errors that occur when many concurrent requests each open/close connections.
# check_same_thread=False is safe because we serialize writes via _write_lock.
_db_conn: sqlite3.Connection | None = None
_db_init_lock = threading.Lock()
_write_lock: asyncio.Lock | None = None   # created in lifespan after event loop starts

def get_db() -> sqlite3.Connection:
    global _db_conn
    with _db_init_lock:
        if _db_conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _db_conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
            _db_conn.row_factory = sqlite3.Row
            _db_conn.execute("PRAGMA journal_mode=WAL")
            _db_conn.execute("PRAGMA synchronous=NORMAL")
            _db_conn.execute("PRAGMA busy_timeout=30000")
            _db_conn.execute("PRAGMA cache_size=-64000")  # 64MB page cache
    return _db_conn

def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id         TEXT PRIMARY KEY,
            text       TEXT NOT NULL,
            metadata   TEXT NOT NULL DEFAULT '{}',
            embedding  TEXT NOT NULL,
            dims       INTEGER NOT NULL,
            source     TEXT NOT NULL DEFAULT 'unknown',
            created_at TEXT NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_source  ON memories(source)")
    db.commit()

# ── FAISS index ───────────────────────────────────────────────────────────────
_faiss_index: faiss.IndexIDMap | None = None
_faiss_lock  = threading.Lock()
_faiss_ready = False

def _build_faiss_index():
    t0   = time.time()
    db   = get_db()
    rows = db.execute("SELECT rowid, embedding FROM memories ORDER BY rowid").fetchall()
    if not rows:
        flat = faiss.IndexFlatIP(DIMS)
        return faiss.IndexIDMap(flat), []
    rowids = np.array([r[0] for r in rows], dtype=np.int64)
    vecs   = np.array([json.loads(r[1]) for r in rows], dtype=np.float32)
    faiss.normalize_L2(vecs)
    flat = faiss.IndexFlatIP(DIMS)
    idx  = faiss.IndexIDMap(flat)
    idx.add_with_ids(vecs, rowids)
    logger.info(f"FAISS index built: {len(rows):,} vectors in {time.time()-t0:.1f}s")
    return idx, rowids.tolist()

def _save_index():
    try:
        if _faiss_index is not None:
            faiss.write_index(_faiss_index, str(INDEX_PATH))
    except Exception as e:
        logger.warning(f"Could not save FAISS index: {e}")

def _load_or_build_index():
    global _faiss_index, _faiss_ready
    try:
        if INDEX_PATH.exists():
            db_count = get_db().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            idx = faiss.read_index(str(INDEX_PATH))
            if abs(idx.ntotal - db_count) <= 200:
                logger.info(f"FAISS index loaded from disk: {idx.ntotal:,} vectors")
                with _faiss_lock:
                    _faiss_index = idx
                _faiss_ready = True
                return
    except Exception as e:
        logger.warning(f"Could not load FAISS index ({e}), rebuilding...")
    idx, _ = _build_faiss_index()
    with _faiss_lock:
        _faiss_index = idx
    _faiss_ready = True
    _save_index()

def _add_to_faiss(rowid: int, vector: list[float]):
    if not _faiss_ready or _faiss_index is None:
        return
    vec = np.array([vector], dtype=np.float32)
    faiss.normalize_L2(vec)
    with _faiss_lock:
        _faiss_index.add_with_ids(vec, np.array([rowid], dtype=np.int64))

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
    return embeddings[0] if isinstance(embeddings[0], list) else embeddings

# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _write_lock
    init_db()
    _http_client = httpx.AsyncClient(timeout=60.0)
    _write_lock  = asyncio.Lock()
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _load_or_build_index)
    yield
    await _http_client.aclose()
    _save_index()

app = FastAPI(title="Nova Memory Server", version="2.1.0-faiss", lifespan=lifespan)

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

    # Embed outside the lock (slow Ollama call, runs concurrently)
    vector    = await embed(req.text)
    memory_id = str(uuid.uuid4())

    # Serialize DB writes — one at a time
    async with _write_lock:
        db = get_db()
        db.execute(
            "INSERT INTO memories (id, text, metadata, embedding, dims, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (memory_id, req.text, json.dumps(req.metadata), json.dumps(vector),
             len(vector), req.source, datetime.utcnow().isoformat()),
        )
        db.commit()
        rowid = db.execute(
            "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()[0]

    _add_to_faiss(rowid, vector)
    return {"id": memory_id, "dims": len(vector)}


@app.get("/recall")
async def recall(
    q: str = Query(...),
    n: int = Query(DEFAULT_N, ge=1, le=MAX_N),
    source: Optional[str] = Query(None),
    min_score: float = Query(0.0),
):
    if not q.strip():
        raise HTTPException(status_code=400, detail="q cannot be empty")

    query_vec = np.array(await embed(q), dtype=np.float32).reshape(1, -1)
    faiss.normalize_L2(query_vec)

    # ── FAISS fast path ───────────────────────────────────────────────────────
    if _faiss_ready and _faiss_index is not None and _faiss_index.ntotal > 0:
        k = min(n * 10 if source else n * 2, _faiss_index.ntotal)
        with _faiss_lock:
            scores_arr, rowids_arr = _faiss_index.search(query_vec, k)
        valid = [(int(rid), float(score)) for rid, score in zip(rowids_arr[0], scores_arr[0])
                 if rid >= 0 and score >= min_score]
        if not valid:
            return {"memories": [], "query": q, "count": 0}

        rowid_list = [r[0] for r in valid]
        score_map  = {r[0]: r[1] for r in valid}
        placeholders = ",".join("?" * len(rowid_list))
        db   = get_db()
        rows = db.execute(
            f"SELECT rowid, id, text, metadata, source, created_at "
            f"FROM memories WHERE rowid IN ({placeholders})", rowid_list
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

    # ── Fallback: numpy scan (while FAISS still building) ─────────────────────
    db   = get_db()
    rows = db.execute(
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
    db    = get_db()
    count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    return {"status": "ok", "count": count, "model": EMBED_MODEL,
            "faiss": "ready" if _faiss_ready else "building",
            "faiss_vectors": _faiss_index.ntotal if _faiss_index else 0}


@app.get("/stats")
async def stats():
    db       = get_db()
    count    = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    dims_row = db.execute("SELECT dims FROM memories LIMIT 1").fetchone()
    by_src   = db.execute("SELECT source, COUNT(*) as n FROM memories GROUP BY source").fetchall()
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
async def random_memory(source: Optional[str] = Query(None), n: int = Query(1)):
    db = get_db()
    if source:
        rows = db.execute(
            "SELECT id, text, metadata, source, created_at FROM memories "
            "WHERE source = ? ORDER BY RANDOM() LIMIT ?", (source, n)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, text, metadata, source, created_at FROM memories "
            "ORDER BY RANDOM() LIMIT ?", (n,)
        ).fetchall()
    memories = [{"id": r["id"], "text": r["text"], "metadata": json.loads(r["metadata"]),
                 "source": r["source"], "created_at": r["created_at"]} for r in rows]
    return {"memories": memories, "count": len(memories)}


@app.delete("/forget")
async def forget(id: str = Query(...)):
    async with _write_lock:
        db = get_db()
        result = db.execute("DELETE FROM memories WHERE id = ?", (id,))
        db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True, "id": id}


@app.delete("/forget_all")
async def forget_all(source: Optional[str] = Query(None)):
    async with _write_lock:
        db = get_db()
        if source:
            result = db.execute("DELETE FROM memories WHERE source = ?", (source,))
        else:
            result = db.execute("DELETE FROM memories")
        db.commit()
    threading.Thread(target=_load_or_build_index, daemon=True).start()
    return {"deleted": result.rowcount}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=18790, log_level="info", log_config=None)
