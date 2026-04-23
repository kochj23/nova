#!/usr/bin/env python3
"""
nova_rem_sleep.py — Deep memory consolidation inspired by biological REM sleep.

Runs nightly (3:30am). Five phases:

  Phase 1 — TRIAGE: Identify clusters of semantically similar memories within
            each source. Flag near-duplicates and low-value entries.
  Phase 2 — CONSOLIDATION: For each cluster, generate a synthesis memory that
            captures the essence of the cluster. Cross-link originals to synthesis.
  Phase 3 — LINKING: Find cross-source connections (e.g., an email about a
            project linked to a GitHub commit about the same project).
  Phase 4 — PRUNING: Mark very short (<30 char) or empty memories as scratchpad
            tier (not deleted — just deprioritized in recall).
  Phase 5 — REPORT: Post summary to Slack, store run metadata.

NEVER deletes memories. Only adds synthesis memories, cross-links, and tier changes.

Cron: 3:30am PT daily (after dreams at 2am, before morning brief at 7am)
Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = "http://127.0.0.1:18790"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
CONSOLIDATION_MODEL = "nova:latest"
PG_CONN = "host=127.0.0.1 dbname=nova_memories"
TODAY = date.today().isoformat()
MAX_CLUSTERS_PER_RUN = 20
CLUSTER_SIMILARITY_THRESHOLD = 0.85


def log(msg):
    print(f"[rem_sleep {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def post_slack(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def pg_connect():
    import psycopg2
    return psycopg2.connect(PG_CONN)


def ollama_generate(prompt, max_tokens=300):
    try:
        payload = json.dumps({
            "model": CONSOLIDATION_MODEL,
            "prompt": f"/no_think\n\n{prompt}",
            "stream": False,
            "think": False,
            "options": {"temperature": 0.2, "num_predict": max_tokens},
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return data.get("response", "").strip()
    except Exception as e:
        log(f"Ollama error: {e}")
        return ""


def vector_remember(text, source, metadata):
    try:
        payload = json.dumps({"text": text, "source": source, "metadata": metadata}).encode()
        req = urllib.request.Request(
            f"{VECTOR_URL}/remember?async=1", data=payload,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data.get("id")
    except Exception:
        return None


# ── Phase 1: TRIAGE — Find clusters of similar memories ─────────────────────

def phase_triage(conn):
    log("Phase 1: TRIAGE — scanning for clusters...")
    cur = conn.cursor()

    # Pick sources to consolidate (prioritize large, unconsolidated sources)
    cur.execute("""
        SELECT source, COUNT(*) as cnt
        FROM memories
        WHERE tier = 'long_term'
          AND created_at > now() - interval '30 days'
          AND source NOT IN ('synthesis', 'correction')
        GROUP BY source
        HAVING COUNT(*) > 50
        ORDER BY cnt DESC
        LIMIT 5
    """)
    sources = cur.fetchall()

    clusters = []
    total_scanned = 0

    for source_name, count in sources:
        log(f"  Scanning {source_name} ({count} recent memories)...")

        # Sample recent memories from this source
        cur.execute("""
            SELECT id, text, embedding
            FROM memories
            WHERE source = %s AND tier = 'long_term'
              AND created_at > now() - interval '30 days'
              AND embedding IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 200
        """, (source_name,))
        rows = cur.fetchall()
        total_scanned += len(rows)

        if len(rows) < 5:
            continue

        # Find near-duplicate pairs using cosine similarity
        # We can't do all-pairs in Python efficiently, so use PG
        cur.execute("""
            WITH recent AS (
                SELECT id, text, embedding
                FROM memories
                WHERE source = %s AND tier = 'long_term'
                  AND created_at > now() - interval '30 days'
                  AND embedding IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 200
            )
            SELECT a.id, b.id, 1 - (a.embedding <=> b.embedding) as similarity,
                   LEFT(a.text, 100) as text_a, LEFT(b.text, 100) as text_b
            FROM recent a, recent b
            WHERE a.id < b.id
              AND 1 - (a.embedding <=> b.embedding) > %s
            ORDER BY similarity DESC
            LIMIT 50
        """, (source_name, CLUSTER_SIMILARITY_THRESHOLD))

        pairs = cur.fetchall()
        if not pairs:
            continue

        # Group pairs into clusters (union-find)
        parent = {}
        def find(x):
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for aid, bid, sim, _, _ in pairs:
            union(aid, bid)

        groups = {}
        for aid, bid, sim, text_a, text_b in pairs:
            root = find(aid)
            if root not in groups:
                groups[root] = {"ids": set(), "texts": [], "source": source_name, "avg_sim": 0}
            groups[root]["ids"].add(aid)
            groups[root]["ids"].add(bid)

        # Get full text for each cluster member
        for root, group in groups.items():
            ids = list(group["ids"])
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(f"SELECT id, text FROM memories WHERE id IN ({placeholders})", ids)
            group["texts"] = [(r[0], r[1]) for r in cur.fetchall()]
            if len(group["texts"]) >= 2:
                clusters.append(group)

    log(f"  Found {len(clusters)} clusters across {len(sources)} sources ({total_scanned} memories scanned)")
    return clusters, total_scanned


# ── Phase 2: CONSOLIDATION — Synthesize cluster summaries ────────────────────

def phase_consolidation(conn, clusters):
    log(f"Phase 2: CONSOLIDATION — synthesizing {min(len(clusters), MAX_CLUSTERS_PER_RUN)} clusters...")
    cur = conn.cursor()
    syntheses_created = 0
    links_created = 0

    for cluster in clusters[:MAX_CLUSTERS_PER_RUN]:
        source = cluster["source"]
        texts = [t[1][:300] for t in cluster["texts"]]
        ids = [t[0] for t in cluster["texts"]]

        combined = "\n---\n".join(texts[:8])
        prompt = f"""These {len(texts)} memory fragments from source "{source}" are very similar.
Write a single concise synthesis (2-3 sentences) that captures the essential information
from all of them. Preserve specific facts, names, dates. Do not add information not present.

Fragments:
{combined}

Synthesis:"""

        synthesis_text = ollama_generate(prompt, max_tokens=200)
        if not synthesis_text or len(synthesis_text) < 20:
            continue

        synthesis_text = f"[Consolidation {TODAY}] {synthesis_text}"

        # Store synthesis memory
        synthesis_id = vector_remember(
            synthesis_text,
            "synthesis",
            {
                "type": "consolidation",
                "date": TODAY,
                "original_source": source,
                "cluster_size": len(ids),
                "original_ids": ids[:10],
            }
        )

        if not synthesis_id:
            continue

        syntheses_created += 1

        # Create cross-links from originals to synthesis
        for orig_id in ids:
            try:
                cur.execute("""
                    INSERT INTO memory_links (source_id, target_id, link_type, strength)
                    VALUES (%s, %s, 'synthesis', 0.9)
                    ON CONFLICT (source_id, target_id, link_type) DO NOTHING
                """, (orig_id, synthesis_id))
                links_created += 1
            except Exception:
                pass

        conn.commit()
        log(f"  Synthesized {len(ids)} {source} memories → {synthesis_id[:8]}...")

    return syntheses_created, links_created


# ── Phase 3: LINKING — Cross-source connections ──────────────────────────────

def phase_linking(conn):
    log("Phase 3: LINKING — finding cross-source connections...")
    cur = conn.cursor()
    links_created = 0

    # Find memories from different sources that are semantically very similar
    cur.execute("""
        WITH recent_diverse AS (
            SELECT id, text, embedding, source
            FROM memories
            WHERE tier = 'long_term'
              AND created_at > now() - interval '7 days'
              AND embedding IS NOT NULL
              AND source NOT IN ('synthesis', 'correction', 'apple_health', 'healthkit')
            ORDER BY created_at DESC
            LIMIT 500
        )
        SELECT a.id, b.id, a.source, b.source,
               1 - (a.embedding <=> b.embedding) as similarity
        FROM recent_diverse a, recent_diverse b
        WHERE a.id < b.id
          AND a.source != b.source
          AND 1 - (a.embedding <=> b.embedding) > 0.80
        ORDER BY similarity DESC
        LIMIT 30
    """)

    pairs = cur.fetchall()
    for aid, bid, src_a, src_b, sim in pairs:
        try:
            cur.execute("""
                INSERT INTO memory_links (source_id, target_id, link_type, strength)
                VALUES (%s, %s, 'related', %s)
                ON CONFLICT (source_id, target_id, link_type) DO NOTHING
            """, (aid, bid, round(sim, 3)))
            links_created += 1
        except Exception:
            pass

    conn.commit()
    log(f"  Created {links_created} cross-source links")
    return links_created


# ── Phase 4: PRUNING — Deprioritize low-value memories ──────────────────────

def phase_pruning(conn):
    log("Phase 4: PRUNING — deprioritizing low-value memories...")
    cur = conn.cursor()

    # Move very short memories to scratchpad tier (not deleted)
    cur.execute("""
        UPDATE memories
        SET tier = 'scratchpad'
        WHERE tier = 'long_term'
          AND LENGTH(text) < 30
          AND source NOT IN ('synthesis', 'correction')
    """)
    short_count = cur.rowcount

    # Move empty/whitespace-only memories to scratchpad
    cur.execute("""
        UPDATE memories
        SET tier = 'scratchpad'
        WHERE tier = 'long_term'
          AND TRIM(text) = ''
    """)
    empty_count = cur.rowcount

    conn.commit()
    total = short_count + empty_count
    log(f"  Moved {total} low-value memories to scratchpad (short: {short_count}, empty: {empty_count})")
    return total


# ── Phase 5: REPORT ──────────────────────────────────────────────────────────

def phase_report(conn, stats):
    log("Phase 5: REPORT")
    cur = conn.cursor()

    # Record the run
    cur.execute("""
        INSERT INTO consolidation_runs (source, memories_scanned, clusters_found, syntheses_created, links_created, duration_seconds)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        "nightly",
        stats["scanned"],
        stats["clusters"],
        stats["syntheses"],
        stats["links"],
        stats["duration"],
    ))
    conn.commit()

    # Get tier breakdown
    cur.execute("SELECT tier, COUNT(*) FROM memories GROUP BY tier ORDER BY tier")
    tiers = {r[0]: r[1] for r in cur.fetchall()}

    # Get link count
    cur.execute("SELECT COUNT(*) FROM memory_links")
    total_links = cur.fetchone()[0]

    report = (
        f":brain: *REM Sleep Consolidation — {TODAY}*\n"
        f"• Scanned: {stats['scanned']:,} recent memories\n"
        f"• Clusters found: {stats['clusters']}\n"
        f"• Syntheses created: {stats['syntheses']}\n"
        f"• Cross-links created: {stats['links']}\n"
        f"• Pruned to scratchpad: {stats['pruned']}\n"
        f"• Duration: {stats['duration']:.0f}s\n"
        f"• Tiers: {', '.join(f'{k}={v:,}' for k, v in sorted(tiers.items()))}\n"
        f"• Total links: {total_links:,}"
    )
    log(report.replace("*", "").replace(":brain:", ""))
    post_slack(report)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("REM Sleep consolidation starting...")
    start = time.time()

    try:
        import psycopg2
    except ImportError:
        log("ERROR: psycopg2 not installed")
        return

    conn = pg_connect()

    try:
        clusters, scanned = phase_triage(conn)
        syntheses, synth_links = phase_consolidation(conn, clusters)
        cross_links = phase_linking(conn)
        pruned = phase_pruning(conn)

        duration = time.time() - start
        stats = {
            "scanned": scanned,
            "clusters": len(clusters),
            "syntheses": syntheses,
            "links": synth_links + cross_links,
            "pruned": pruned,
            "duration": duration,
        }

        phase_report(conn, stats)
    finally:
        conn.close()

    log(f"REM Sleep complete in {time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
