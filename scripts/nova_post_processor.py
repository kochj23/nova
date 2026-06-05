#!/usr/bin/env python3
"""
nova_post_processor.py — Background analysis daemon (Frigate real-time/post split).

Runs async background tasks that never block interactive use:
  - Memory consolidation (merge similar, update importance scores)
  - Pattern recognition (recurring themes in recent memories)
  - Cross-domain linking (find connections between disparate sources)
  - Summary generation (daily topic summaries as meta-memories)

All work runs at P4 priority in the inference queue. Results available
for next morning brief / proactive brief cycle.

Written by Jordan Koch.
"""

import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import psycopg2
    import redis
except ImportError as e:
    print(f"FATAL: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

OPS_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
MEMORIES_DSN = "postgresql://kochj@127.0.0.1:5432/nova_memories"
REDIS_URL = "redis://192.168.1.6:6379"
MEMORY_URL = "http://192.168.1.6:18790"
LOG_FILE = Path.home() / ".openclaw/logs/nova_post_processor.log"
RESULTS_KEY_PREFIX = "nova:postprocess:results"

import urllib.request


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[post-proc {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


def _db_query(dsn, sql, params=None):
    try:
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        cur.execute(sql, params or ())
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            rows = []
        conn.close()
        return rows
    except Exception as e:
        log(f"DB error: {e}", "ERROR")
        return []


# ── Task: Source Activity Summary ─────────────────────────────────────────────

def summarize_source_activity(hours: int = 24) -> dict:
    """Summarize memory activity per source over the last N hours."""
    rows = _db_query(MEMORIES_DSN, """
        SELECT source, COUNT(*) as count, MIN(created_at) as first, MAX(created_at) as last
        FROM memories
        WHERE created_at > now() - make_interval(hours => %s)
        GROUP BY source
        ORDER BY count DESC
        LIMIT 30
    """, (hours,))

    summary = {
        "period_hours": hours,
        "sources": len(rows),
        "total_memories": sum(r["count"] for r in rows),
        "top_sources": [{"source": r["source"], "count": r["count"]} for r in rows[:10]],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return summary


# ── Task: Recurring Themes ────────────────────────────────────────────────────

def detect_recurring_themes(hours: int = 24, min_occurrences: int = 3) -> list:
    """Find recurring terms/concepts in recent memories."""
    rows = _db_query(MEMORIES_DSN, """
        SELECT text FROM memories
        WHERE created_at > now() - make_interval(hours => %s)
        ORDER BY created_at DESC
        LIMIT 500
    """, (hours,))

    if not rows:
        return []

    # Simple term frequency analysis (proper NER would be better but this is P4 background)
    import re
    word_counts = Counter()
    bigram_counts = Counter()

    stop_words = {
        "the", "a", "an", "is", "it", "to", "in", "for", "of", "and", "or", "on",
        "at", "by", "with", "from", "as", "be", "was", "are", "been", "have", "has",
        "had", "do", "does", "did", "will", "would", "could", "should", "this", "that",
        "not", "but", "if", "so", "than", "too", "very", "just", "about", "up", "out",
    }

    for row in rows:
        words = re.findall(r"\b[A-Za-z]{4,}\b", row["text"][:500])
        words = [w.lower() for w in words if w.lower() not in stop_words]
        word_counts.update(words)
        for i in range(len(words) - 1):
            bigram_counts[f"{words[i]} {words[i+1]}"] += 1

    themes = []
    for term, count in word_counts.most_common(20):
        if count >= min_occurrences:
            themes.append({"term": term, "occurrences": count, "type": "word"})

    for bigram, count in bigram_counts.most_common(10):
        if count >= min_occurrences:
            themes.append({"term": bigram, "occurrences": count, "type": "bigram"})

    return themes


# ── Task: Cross-Domain Links ──────────────────────────────────────────────────

def find_cross_domain_links(hours: int = 24) -> list:
    """Find terms that appear across multiple source domains."""
    rows = _db_query(MEMORIES_DSN, """
        SELECT source, text FROM memories
        WHERE created_at > now() - make_interval(hours => %s)
        ORDER BY created_at DESC
        LIMIT 300
    """, (hours,))

    if not rows:
        return []

    import re
    # Track significant terms per source
    source_terms = defaultdict(set)
    for row in rows:
        words = set(re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", row["text"][:300]))
        for word in words:
            if len(word) > 3:
                source_terms[row["source"]].add(word)

    # Find terms appearing in 2+ sources
    term_sources = defaultdict(set)
    for source, terms in source_terms.items():
        for term in terms:
            term_sources[term].add(source)

    links = []
    for term, sources in term_sources.items():
        if len(sources) >= 2:
            links.append({
                "term": term,
                "sources": sorted(sources),
                "cross_domain_count": len(sources),
            })

    links.sort(key=lambda x: x["cross_domain_count"], reverse=True)
    return links[:20]


# ── Main Processing Run ───────────────────────────────────────────────────────

def run_post_processing():
    """Execute all post-processing tasks and store results."""
    log("Starting post-processing run...")
    start = time.time()
    rc = _get_redis()
    date_key = datetime.now().strftime("%Y-%m-%d")
    results = {}

    # Task 1: Source activity summary
    try:
        results["source_activity"] = summarize_source_activity(24)
        log(f"  Source activity: {results['source_activity']['total_memories']} memories across {results['source_activity']['sources']} sources")
    except Exception as e:
        log(f"  Source activity failed: {e}", "ERROR")

    # Task 2: Recurring themes
    try:
        results["themes"] = detect_recurring_themes(24)
        log(f"  Themes: {len(results['themes'])} recurring terms/phrases")
    except Exception as e:
        log(f"  Themes failed: {e}", "ERROR")

    # Task 3: Cross-domain links
    try:
        results["cross_links"] = find_cross_domain_links(24)
        log(f"  Cross-links: {len(results['cross_links'])} shared terms")
    except Exception as e:
        log(f"  Cross-links failed: {e}", "ERROR")

    # Store results in Redis
    results["run_time_s"] = time.time() - start
    results["generated_at"] = datetime.now(timezone.utc).isoformat()
    rc.setex(f"{RESULTS_KEY_PREFIX}:{date_key}", 86400, json.dumps(results, default=str))

    log(f"Post-processing complete in {results['run_time_s']:.1f}s")

    # Also store as a meta-memory for Nova's awareness
    summary_text = (
        f"Post-processing analysis ({date_key}): "
        f"{results.get('source_activity', {}).get('total_memories', 0)} new memories, "
        f"{len(results.get('themes', []))} recurring themes, "
        f"{len(results.get('cross_links', []))} cross-domain connections."
    )
    if results.get("themes"):
        top_themes = [t["term"] for t in results["themes"][:5]]
        summary_text += f" Top themes: {', '.join(top_themes)}."

    try:
        payload = json.dumps({
            "text": summary_text,
            "source": "infrastructure",
            "metadata": {"type": "post_processing", "date": date_key},
        }).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/remember?async=1", data=payload,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    results = run_post_processing()
    print(json.dumps(results, indent=2, default=str))
