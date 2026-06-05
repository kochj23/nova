#!/usr/bin/env python3
"""
nova_importance_scorer.py — Memory importance scoring (Frigate cache-first pattern).

Decides whether incoming content should be permanently vectorized (PostgreSQL)
or kept in hot cache only (Redis, 24h TTL). Prevents memory pollution from
routine/duplicate/low-value observations.

Score 0.0-1.0:
  >= 0.4 → persist to vector DB (permanent)
  <  0.4 → hot cache only (Redis, 24h TTL, text-searchable)

Scoring tiers:
  1. Rules-based fast path (known-important sources → always persist)
  2. Heuristic layer (length, uniqueness, named entities, temporal refs)
  3. Optional LLM triage (borderline scores, deepseek-r1:8b)

Written by Jordan Koch.
"""

import hashlib
import re
import sys
import time
from pathlib import Path

try:
    import redis
except ImportError:
    redis = None

sys.path.insert(0, str(Path(__file__).parent))

# ── Config ────────────────────────────────────────────────────────────────────

PERSIST_THRESHOLD = 0.4
REDIS_URL = "redis://192.168.1.6:6379"
HOT_CACHE_TTL = 86400  # 24 hours
RECENT_WINDOW = 20     # compare against last N items per source

# Sources that ALWAYS persist (score = 1.0 regardless of content)
ALWAYS_PERSIST_SOURCES = {
    "imessage", "email_archive", "healthkit", "nova_journal",
    "claude_memory", "chatroom", "herd_blog", "morning_brief",
    "security", "incident",
}

# Sources that are typically low-value (need higher content quality to persist)
LOW_VALUE_SOURCES = {
    "livetv_news", "livetv_dream_fuel", "daily_news",
    "game_show", "comedy", "drama", "horror", "action",
    "sports", "music",
}

# ── Heuristic Rules ───────────────────────────────────────────────────────────

_PROPER_NOUN_PATTERN = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")
_TEMPORAL_PATTERN = re.compile(
    r"\b(today|yesterday|tomorrow|this week|next week|last week|"
    r"\d{4}-\d{2}-\d{2}|January|February|March|April|May|June|"
    r"July|August|September|October|November|December)\b", re.IGNORECASE
)
_ACTION_PATTERN = re.compile(
    r"\b(decided|agreed|committed|deployed|fixed|broke|discovered|"
    r"learned|important|critical|urgent|warning|alert|incident)\b", re.IGNORECASE
)
_GARBAGE_PATTERN = re.compile(
    r"^[\W\d\s]+$|^\[?\s*(silence|music|applause)\s*\]?$", re.IGNORECASE
)

# ── Redis Client ──────────────────────────────────────────────────────────────

_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None and redis:
        try:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            _redis_client.ping()
        except Exception:
            _redis_client = None
    return _redis_client


# ── Core Scoring ──────────────────────────────────────────────────────────────

def score_importance(text: str, source: str, metadata: dict = None) -> float:
    """Score content importance from 0.0 to 1.0.

    Returns the score. Caller decides whether to persist based on threshold.
    """
    metadata = metadata or {}

    # Tier 1: Rules-based fast path
    if source in ALWAYS_PERSIST_SOURCES:
        return 1.0

    # Garbage detection
    if not text or len(text.split()) < 10:
        return 0.1
    if _GARBAGE_PATTERN.match(text.strip()):
        return 0.0

    # Tier 2: Heuristic scoring
    score = 0.3  # base score for non-garbage content

    # Length bonus (longer = more likely substantive)
    word_count = len(text.split())
    if word_count > 100:
        score += 0.1
    if word_count > 300:
        score += 0.1

    # Named entities (proper nouns suggest specific, memorable content)
    proper_nouns = _PROPER_NOUN_PATTERN.findall(text)
    if len(proper_nouns) >= 2:
        score += 0.15
    elif len(proper_nouns) >= 1:
        score += 0.05

    # Temporal references (time-anchored content is more valuable)
    if _TEMPORAL_PATTERN.search(text):
        score += 0.1

    # Action/decision language (indicates something happened worth remembering)
    if _ACTION_PATTERN.search(text):
        score += 0.15

    # Source penalty for low-value sources
    if source in LOW_VALUE_SOURCES:
        score -= 0.15

    # Uniqueness check (compare against recent items from same source)
    uniqueness = _check_uniqueness(text, source)
    score += uniqueness * 0.15  # 0.0 (duplicate) to 0.15 (fully unique)

    # Metadata bonuses
    if metadata.get("type") == "recipe":
        score += 0.2
    if metadata.get("type") in ("security_alert", "incident", "decision"):
        score = max(score, 0.9)

    return min(1.0, max(0.0, score))


def _check_uniqueness(text: str, source: str) -> float:
    """Compare text against recent items from the same source. Returns 0.0-1.0."""
    rc = _get_redis()
    if not rc:
        return 0.7  # assume moderately unique if Redis unavailable

    cache_key = f"nova:memory:recent:{source}"
    text_hash = hashlib.md5(text[:500].encode()).hexdigest()[:12]

    try:
        recent = rc.lrange(cache_key, 0, RECENT_WINDOW - 1)

        # Check for exact duplicate
        if text_hash in recent:
            return 0.0

        # Check for near-duplicate (first 100 chars match)
        prefix = text[:100].lower().strip()
        for item in recent:
            if item.startswith("pfx:") and item[4:] == prefix[:80]:
                return 0.2

        # Push to recent list
        rc.lpush(cache_key, text_hash)
        rc.lpush(cache_key, f"pfx:{prefix[:80]}")
        rc.ltrim(cache_key, 0, RECENT_WINDOW * 2 - 1)
        rc.expire(cache_key, 86400)

        return 1.0  # fully unique within window
    except Exception:
        return 0.7


# ── Hot Cache ─────────────────────────────────────────────────────────────────

def cache_hot(text: str, source: str, metadata: dict = None, score: float = 0.0):
    """Store in Redis hot cache (not persisted to PostgreSQL)."""
    rc = _get_redis()
    if not rc:
        return False

    text_hash = hashlib.md5(text.encode()).hexdigest()[:16]
    key = f"nova:memory:hot:{text_hash}"
    try:
        rc.setex(key, HOT_CACHE_TTL, text[:2000])
        # Index by source for searchability
        idx_key = f"nova:memory:hot:idx:{source}"
        rc.lpush(idx_key, f"{text_hash}:{text[:200]}")
        rc.ltrim(idx_key, 0, 499)
        rc.expire(idx_key, HOT_CACHE_TTL)
        return True
    except Exception:
        return False


def search_hot(query: str, source: str = "", limit: int = 10) -> list:
    """Search the hot cache by text substring match."""
    rc = _get_redis()
    if not rc:
        return []

    results = []
    query_lower = query.lower()

    try:
        if source:
            idx_key = f"nova:memory:hot:idx:{source}"
            items = rc.lrange(idx_key, 0, 499)
        else:
            # Scan all source indices
            items = []
            for key in rc.scan_iter("nova:memory:hot:idx:*", count=100):
                items.extend(rc.lrange(key, 0, 99))

        for item in items:
            if ":" in item:
                hash_part, preview = item.split(":", 1)
                if query_lower in preview.lower():
                    # Fetch full text from hash key
                    full = rc.get(f"nova:memory:hot:{hash_part}")
                    if full:
                        results.append({"text": full, "preview": preview, "hash": hash_part})
                        if len(results) >= limit:
                            break
    except Exception:
        pass

    return results


# ── Public API ────────────────────────────────────────────────────────────────

def should_persist(text: str, source: str, metadata: dict = None) -> tuple:
    """Convenience function: score and decide in one call.

    Returns (should_persist: bool, score: float).
    If not persisting, automatically caches to hot store.
    """
    s = score_importance(text, source, metadata)
    if s >= PERSIST_THRESHOLD:
        return True, s
    else:
        cache_hot(text, source, metadata, s)
        return False, s
