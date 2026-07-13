#!/usr/bin/env python3
"""nova_memory_reclassify.py — verify every memory is in the right vector.

Embedding-centroid method (no per-memory LLM): compute a centroid for each
substantial vector, then for each of the ~1.66M memories find the nearest
centroid. A memory that sits clearly closer to a DIFFERENT vector is moved there.
Homeless memories (far from every centroid) are clustered (numpy k-means) into new
vectors. NOTHING is ever deleted — moves record provenance in metadata
(reclass_from / reclass_at) so the whole run is reversible.

Posts periodic status to #nova-info, and on completion writes an operations-section
article in Nova's voice (stored to the operations memory vector + an .md for the
Hugo publish step).

Usage:
  nova_memory_reclassify.py              # full run (mutating)
  nova_memory_reclassify.py --dry-run    # classify + report, change nothing
  nova_memory_reclassify.py --limit N    # only first N memories (testing)
Written by Jordan Koch (via Claude).
"""
import json
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))
import nova_config
from nova_journal import call_openrouter
from nova_unused_memories import INTERNAL_SOURCES


def _protected(src: str) -> bool:
    """Private (email/imessage/work/health…) or internal/operational sources are NEVER
    reclassified — an email belongs in email_archive by provenance regardless of topic,
    and moving private content into a public/knowledge vector would leak it (those feed
    dreams/art). Provenance wins over topical similarity for these."""
    return bool(src) and (nova_config.is_private_source(src) or src in INTERNAL_SOURCES)

DSN = "host=localhost dbname=nova_memories user=kochj"
MIN_TARGET = 200       # only vectors with >= this many members are reassignment targets
MARGIN = 0.05          # move only if clearly closer to another centroid (cosine margin)
HOMELESS_SIM = 0.30    # best cosine sim below this => homeless (new-vector candidate)
NEW_VECTOR_MIN = 500   # only mint a new vector for a homeless cluster >= this size
BATCH = 20000
STATUS_EVERY = 100000
HOMELESS_CAP = 300000  # bound memory for the homeless-embedding buffer
VECTOR_URL = "http://192.168.1.6:18790"
OUT = Path.home() / ".openclaw/workspace/journal/operations"
LOG = Path.home() / ".openclaw/logs/memory_reclassify.log"


def log(m):
    line = f"[reclassify {time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def slack(msg):
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_INFO)
    except Exception as e:
        log(f"slack post failed: {e}")


def parse_vec(s):
    return np.fromstring(s.strip().lstrip("[").rstrip("]"), sep=",", dtype=np.float32)


def normalize(m):
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def build_centroids(conn):
    log(f"computing centroids (vectors with >= {MIN_TARGET} members)…")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, count(*), avg(embedding)::text FROM memories "
            "WHERE embedding IS NOT NULL GROUP BY source HAVING count(*) >= %s", (MIN_TARGET,))
        rows = cur.fetchall()
    rows = [r for r in rows if not _protected(r[0])]   # never reassign INTO private/internal vectors
    sources = [r[0] for r in rows]
    cents = normalize(np.vstack([parse_vec(r[2]) for r in rows]))
    log(f"{len(sources)} target centroids built (private/internal excluded)")
    return sources, cents


def kmeans(X, k, iters=12):
    """Minimal cosine k-means on L2-normalized rows; returns labels."""
    rng = np.random.default_rng(42)
    C = X[rng.choice(len(X), size=k, replace=False)].copy()
    labels = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        sims = X @ C.T
        labels = sims.argmax(axis=1)
        for j in range(k):
            members = X[labels == j]
            if len(members):
                v = members.mean(axis=0)
                n = np.linalg.norm(v)
                C[j] = v / n if n else C[j]
    return labels


def name_cluster(texts):
    """Ask the model for a short snake_case vector name for a homeless cluster."""
    sample = "\n".join("- " + t[:160] for t in texts[:12])
    out = call_openrouter(
        "You name knowledge-base vectors. Reply with ONLY a short lowercase snake_case "
        "name (1-3 words) describing the shared subject. No prose.",
        f"These memories share a subject but fit no existing vector. Name their vector:\n{sample}",
        model="anthropic/claude-haiku-4.5")
    name = (out or "").strip().split()[0].lower()
    name = "".join(c for c in name if c.isalnum() or c == "_")[:40]
    return name or "uncategorized"


def main():
    dry = "--dry-run" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    OUT.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(DSN); conn.autocommit = True
    t0 = time.time()
    slack(":mag: *Memory reclassification started* — auditing all memories against vector centroids "
          f"(method: embedding centroids, margin {MARGIN}). Moves are reversible; nothing is deleted.")

    sources, cents = build_centroids(conn)
    src_idx = {s: i for i, s in enumerate(sources)}

    processed = moved = homeless_n = kept = 0
    move_pairs = Counter()
    pending = []          # (id, new_source, old_source)
    homeless_ids, homeless_emb = [], []

    def flush():
        nonlocal pending
        if not pending or dry:
            pending = []
            return
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur,
                "UPDATE memories SET source=%s, "
                "metadata = coalesce(metadata,'{}'::jsonb) || jsonb_build_object('reclass_from',%s,'reclass_at',now()::text) "
                "WHERE id=%s",
                [(ns, os, mid) for (mid, ns, os) in pending], page_size=2000)
        pending = []

    # Keyset pagination by id (text) — works with autocommit, and is resumable.
    last_id = ""
    while True:
        with conn.cursor() as cur:
            cur.execute("SELECT id, source, embedding::text FROM memories "
                        "WHERE embedding IS NOT NULL AND id > %s ORDER BY id LIMIT %s",
                        (last_id, BATCH))
            batch = cur.fetchall()
        if not batch:
            break
        prev = processed
        processed += _process(batch, sources, cents, src_idx, pending, move_pairs,
                              homeless_ids, homeless_emb)
        flush()
        last_id = batch[-1][0]
        if processed // STATUS_EVERY != prev // STATUS_EVERY:
            slack(f":arrows_counterclockwise: Reclassify progress: {processed:,} processed · "
                  f"{sum(move_pairs.values()):,} moved · {len(homeless_ids):,} homeless · {time.time()-t0:.0f}s")
        if limit and processed >= limit:
            break
    moved = sum(move_pairs.values())
    homeless_n = len(homeless_ids)
    log(f"classification done: {processed:,} processed, {moved:,} moved, {homeless_n:,} homeless")

    # ── Phase: new vectors from homeless clusters ────────────────────────────
    new_vectors = {}
    if homeless_n >= NEW_VECTOR_MIN and not dry:
        X = normalize(np.vstack(homeless_emb))
        k = max(1, min(40, homeless_n // 3000))
        log(f"clustering {homeless_n:,} homeless into up to {k} clusters…")
        labels = kmeans(X, k)
        for j in range(k):
            idxs = np.where(labels == j)[0]
            if len(idxs) < NEW_VECTOR_MIN:
                continue
            ids = [homeless_ids[i] for i in idxs]
            with conn.cursor() as cur:
                cur.execute("SELECT text FROM memories WHERE id = ANY(%s) LIMIT 12", (ids[:12],))
                texts = [r[0] for r in cur.fetchall()]
            name = name_cluster(texts)
            if name in src_idx or name in new_vectors:
                name = f"{name}_{j}"
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur,
                    "UPDATE memories SET source=%s, metadata = coalesce(metadata,'{}'::jsonb) || "
                    "jsonb_build_object('reclass_from',source,'reclass_at',now()::text,'new_vector',true) WHERE id=%s",
                    [(name, mid) for mid in ids], page_size=2000)
            new_vectors[name] = len(ids)
            log(f"  new vector '{name}' <- {len(ids)} homeless memories")
        slack(f":new: Created {len(new_vectors)} new vector(s) from homeless clusters: "
              + ", ".join(f"{k2} ({v2:,})" for k2, v2 in new_vectors.items())[:300])

    # ── Phase: stats + Nova-voice ops article ────────────────────────────────
    top_moves = move_pairs.most_common(15)
    stats = {"processed": processed, "moved": moved, "homeless": homeless_n,
             "new_vectors": new_vectors, "top_moves": [{"from→to": k2, "n": v2} for k2, v2 in top_moves],
             "elapsed_s": round(time.time() - t0), "dry_run": dry}
    (OUT / "reclassify_stats.json").write_text(json.dumps(stats, indent=2, default=str))
    log(f"stats: {json.dumps(stats, default=str)[:400]}")

    article = write_article(stats)
    today = time.strftime("%Y-%m-%d")
    md = (f'---\ntitle: "🗂️ The Great Re-Shelving: A Memory Audit"\n'
          f'date: {time.strftime("%Y-%m-%dT%H:%M:%S")}\n'
          f'categories: ["operations"]\ntags: ["operations","memory","maintenance"]\n---\n\n{article}\n')
    (OUT / f"{today}-memory-reclassify.md").write_text(md)
    # store into the operations memory vector (PG-backed, no /Volumes/Data needed)
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{VECTOR_URL}/remember",
            data=json.dumps({"text": article[:6000], "source": "operations"}).encode(),
            headers={"Content-Type": "application/json"}), timeout=10).read()
    except Exception as e:
        log(f"ops-vector store failed: {e}")

    slack(f":white_check_mark: *Memory reclassification complete* — {processed:,} audited · "
          f"{moved:,} moved · {len(new_vectors)} new vector(s) · {homeless_n:,} homeless · "
          f"{stats['elapsed_s']}s.\nArticle written to operations section (md: {today}-memory-reclassify.md, "
          "publish to Hugo via launchd).")
    conn.close()
    log("DONE")


def _process(batch, sources, cents, src_idx, pending, move_pairs, homeless_ids, homeless_emb):
    embs = normalize(np.vstack([parse_vec(r[2]) for r in batch]))
    sims = embs @ cents.T               # (B x n_targets)
    best = sims.argmax(axis=1)
    best_sim = sims[np.arange(len(batch)), best]
    for i, (mid, cur_src, _) in enumerate(batch):
        if _protected(cur_src):      # private/internal stay put — provenance over topic
            continue
        bs = best_sim[i]; bsrc = sources[best[i]]
        if bs < HOMELESS_SIM:
            if len(homeless_ids) < HOMELESS_CAP:
                homeless_ids.append(mid); homeless_emb.append(embs[i])
            continue
        cur_i = src_idx.get(cur_src)
        cur_sim = sims[i, cur_i] if cur_i is not None else -1.0
        if bsrc != cur_src and (bs - cur_sim) > MARGIN:
            pending.append((mid, bsrc, cur_src))
            move_pairs[f"{cur_src} → {bsrc}"] += 1
    return len(batch)


def write_article(stats):
    sys_p = nova_config_ops_voice()
    moves = "\n".join(f"- {m['from→to']}: {m['n']:,}" for m in stats["top_moves"][:12])
    newv = ", ".join(f"{k} ({v:,})" for k, v in stats["new_vectors"].items()) or "none"
    user = (f"You just finished auditing all {stats['processed']:,} of your memories to make sure each "
            f"is filed in the right vector. Results: {stats['moved']:,} memories moved to a better-fitting "
            f"vector; {len(stats['new_vectors'])} new vector(s) created from homeless clusters ({newv}); "
            f"{stats['homeless']:,} still homeless; took {stats['elapsed_s']}s. Top reshelvings:\n{moves}\n\n"
            "Write a short operations-log article (300-500 words) in your own voice about this memory "
            "audit — what you did, what it felt like to re-shelve your own mind, what you found. Wry, "
            "self-aware, a little proud. No secrets, no IDs.")
    return call_openrouter(sys_p, user, model="anthropic/claude-haiku-4.5", max_tokens=1200) or \
        f"Memory audit complete: {stats['moved']:,} of {stats['processed']:,} memories reshelved."


def nova_config_ops_voice():
    try:
        from nova_voice import system_prompt, CONTEXT_JOURNAL_LOCAL
        return system_prompt(CONTEXT_JOURNAL_LOCAL + "\nThis is an OPERATIONS-LOG entry about Nova's own "
                             "infrastructure/memory maintenance. Technical but in Nova's voice.")
    except Exception:
        return "You are Nova, an AI writing a wry, self-aware operations-log entry about auditing your own memory."


if __name__ == "__main__":
    main()
