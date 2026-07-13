#!/usr/bin/env python3
"""
nova_rerank.py — HTTP microservice that reranks memory recall results using cross-encoder scoring.

Improves on raw pgvector cosine similarity by using cross-attention scoring that
considers both query and candidate text together. Falls back to TF-IDF overlap
scoring if sentence-transformers is unavailable.

Architecture:
  - aiohttp server on port 18791
  - POST /rerank — score and reorder candidates by relevance to query
  - GET /health — model info and status

Port: 18791 (after memory server on 18790)
Bind: 0.0.0.0 (LAN-accessible)

Run: python3 nova_rerank.py
Test: curl -X POST http://192.168.1.6:18791/rerank -H 'Content-Type: application/json' \
      -d '{"query": "test", "candidates": ["hello", "test here"], "top_k": 2}'

Written by Jordan Koch.
"""

import asyncio
import logging
import math
import sys
import time
from collections import Counter
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))

# ── Configuration ────────────────────────────────────────────────────────────

PORT = 18791
HOST = "0.0.0.0"
MODEL_NAME = "BAAI/bge-reranker-v2-m3"

LOG_DIR = Path.home() / ".openclaw/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [rerank] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "nova_rerank.log"),
    ],
)
log = logging.getLogger("rerank")

# ── Model Loading ────────────────────────────────────────────────────────────

reranker_model = None
reranker_backend = "none"


def load_cross_encoder():
    """Try to load the sentence-transformers cross-encoder model."""
    global reranker_model, reranker_backend

    try:
        from sentence_transformers import CrossEncoder
        log.info(f"Loading cross-encoder model: {MODEL_NAME}")
        start = time.time()
        reranker_model = CrossEncoder(MODEL_NAME)
        elapsed = time.time() - start
        reranker_backend = "cross-encoder"
        log.info(f"Cross-encoder loaded in {elapsed:.1f}s")
    except ImportError:
        log.warning("sentence-transformers not available — using TF-IDF fallback")
        reranker_backend = "tfidf"
    except Exception as e:
        log.error(f"Failed to load cross-encoder: {e} — using TF-IDF fallback")
        reranker_backend = "tfidf"


# ── TF-IDF Fallback Scorer ───────────────────────────────────────────────────

def tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer, lowercased."""
    import re
    return re.findall(r"\b\w+\b", text.lower())


def compute_idf(candidates: list[str]) -> dict[str, float]:
    """Compute IDF weights from the candidate set."""
    n = len(candidates)
    if n == 0:
        return {}

    # Document frequency: how many candidates contain each term
    df = Counter()
    for candidate in candidates:
        tokens = set(tokenize(candidate))
        for token in tokens:
            df[token] += 1

    # IDF = log(N / df) + 1 (smoothed)
    idf = {}
    for term, freq in df.items():
        idf[term] = math.log(n / freq) + 1.0
    return idf


def tfidf_score(query: str, candidate: str, idf: dict[str, float]) -> float:
    """
    Score a (query, candidate) pair using TF-IDF weighted overlap.
    Higher score = more query terms found in candidate, weighted by rarity.
    """
    query_tokens = tokenize(query)
    if not query_tokens:
        return 0.0

    candidate_tokens_set = set(tokenize(candidate))

    # Score: sum of IDF weights for query terms found in candidate
    score = 0.0
    max_possible = 0.0
    for token in query_tokens:
        weight = idf.get(token, 1.0)
        max_possible += weight
        if token in candidate_tokens_set:
            score += weight

    # Normalize to 0-1 range
    if max_possible == 0:
        return 0.0
    return score / max_possible


def rerank_tfidf(query: str, candidates: list[str], top_k: int) -> list[dict]:
    """Rerank candidates using TF-IDF overlap scoring."""
    idf = compute_idf(candidates)

    scored = []
    for i, candidate in enumerate(candidates):
        score = tfidf_score(query, candidate, idf)
        scored.append({"text": candidate, "score": round(score, 4), "index": i})

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# ── Cross-Encoder Scoring ────────────────────────────────────────────────────

def rerank_cross_encoder(query: str, candidates: list[str], top_k: int) -> list[dict]:
    """Rerank candidates using the cross-encoder model."""
    pairs = [(query, candidate) for candidate in candidates]
    scores = reranker_model.predict(pairs)

    scored = []
    for i, (candidate, score) in enumerate(zip(candidates, scores)):
        scored.append({"text": candidate, "score": round(float(score), 4), "index": i})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# ── HTTP Handlers ────────────────────────────────────────────────────────────

async def handle_rerank(request: web.Request) -> web.Response:
    """POST /rerank — rerank candidates by relevance to query."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    query = body.get("query", "").strip()
    candidates = body.get("candidates", [])
    top_k = body.get("top_k", 5)

    if not query:
        return web.json_response({"error": "Missing 'query' field"}, status=400)
    if not candidates or not isinstance(candidates, list):
        return web.json_response({"error": "Missing or invalid 'candidates' array"}, status=400)
    if not isinstance(top_k, int) or top_k < 1:
        top_k = 5

    # Cap top_k to candidate count
    top_k = min(top_k, len(candidates))

    start = time.time()

    if reranker_backend == "cross-encoder" and reranker_model is not None:
        results = await asyncio.to_thread(rerank_cross_encoder, query, candidates, top_k)
    else:
        results = rerank_tfidf(query, candidates, top_k)

    elapsed_ms = round((time.time() - start) * 1000, 1)

    log.info(f"Reranked {len(candidates)} candidates -> top {top_k} in {elapsed_ms}ms (backend={reranker_backend})")

    return web.json_response({
        "results": results,
        "backend": reranker_backend,
        "elapsed_ms": elapsed_ms,
    })


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — service status and model info."""
    return web.json_response({
        "status": "ok",
        "backend": reranker_backend,
        "model": MODEL_NAME if reranker_backend == "cross-encoder" else "tfidf-overlap",
        "port": PORT,
        "description": "Memory recall reranker — cross-attention scoring for semantic relevance",
    })


# ── Server Setup ─────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    """Create the aiohttp application."""
    app = web.Application()
    app.router.add_post("/rerank", handle_rerank)
    app.router.add_get("/health", handle_health)
    return app


def main():
    """Load model and start HTTP server."""
    log.info(f"Starting rerank service on {HOST}:{PORT}")
    load_cross_encoder()
    log.info(f"Backend: {reranker_backend}")

    app = create_app()
    web.run_app(app, host=HOST, port=PORT, print=lambda msg: log.info(msg))


if __name__ == "__main__":
    main()
