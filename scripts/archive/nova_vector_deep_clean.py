#!/usr/bin/env python3
"""
nova_vector_deep_clean.py — Reclassification engine that finds memories in the wrong
vector (source) and moves them to where they actually belong based on embedding similarity.

Designed for nohup background operation (compute-heavy):
    nohup python3 nova_vector_deep_clean.py --dry-run > ~/.openclaw/logs/deep_clean.log 2>&1 &

Modes:
    --dry-run       (default) Report only, show what WOULD move
    --clean         Actually update source column
    --vector SRC    Only clean one specific vector/source
    --journal       Output a markdown article about funniest reclassifications
    --limit N       Process only N vectors (default: all)

Algorithm:
  1. For each vector (source), compute centroid = average embedding of 100 random samples
  2. Score ALL memories in that vector against the centroid (cosine similarity via pgvector)
  3. Bottom 5% by similarity are "misfits"
  4. For each misfit: compare its embedding against ALL other centroids
  5. If best-match centroid is >0.1 better than current: reclassify
  6. If nothing fits: move to 'general_knowledge'

Written by Jordan Koch.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config

try:
    import asyncpg
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "asyncpg"])
    import asyncpg

try:
    import numpy as np
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "numpy"])
    import numpy as np

# ── Configuration ─────────────────────────────────────────────────────────────

DB_DSN = "postgresql://kochj@192.168.1.6:5432/nova_memories"
CENTROID_SAMPLE_SIZE = 100
MISFIT_PERCENTILE = 5       # bottom 5% are misfits
RECLASSIFY_THRESHOLD = 0.1  # must be >0.1 better to reclassify
MIN_VECTOR_SIZE = 50        # skip vectors with fewer than this many rows
SLACK_UPDATE_INTERVAL = 300  # post Slack update every 5 minutes

LOG_DIR = Path.home() / ".openclaw/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def parse_pgvector(raw) -> np.ndarray:
    """Parse a pgvector string '[0.1,0.2,...]' into numpy array."""
    if isinstance(raw, (list, np.ndarray)):
        return np.array(raw, dtype=np.float32)
    if isinstance(raw, str):
        cleaned = raw.strip("[]")
        return np.array([float(x) for x in cleaned.split(",")], dtype=np.float32)
    # asyncpg may return as bytes or custom type
    return np.array(raw, dtype=np.float32)


async def compute_centroid(pool, source: str) -> np.ndarray | None:
    """Compute average embedding for a source from random sample."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT embedding::text FROM memories
               WHERE source = $1 AND embedding IS NOT NULL
               ORDER BY random() LIMIT $2""",
            source,
            CENTROID_SAMPLE_SIZE,
        )

    if not rows:
        return None

    embeddings = []
    for row in rows:
        try:
            vec = parse_pgvector(row["embedding"])
            if len(vec) == 768:
                embeddings.append(vec)
        except (ValueError, TypeError):
            continue

    if len(embeddings) < 10:
        return None

    centroid = np.mean(embeddings, axis=0).astype(np.float32)
    # Normalize
    norm = np.linalg.norm(centroid)
    if norm > 0:
        centroid = centroid / norm
    return centroid


async def find_misfits(pool, source: str, centroid: np.ndarray) -> list[dict]:
    """Find bottom 5% of memories by cosine similarity to centroid."""
    async with pool.acquire() as conn:
        # Use pgvector cosine distance operator for efficiency
        centroid_str = "[" + ",".join(f"{x:.6f}" for x in centroid) + "]"
        rows = await conn.fetch(
            f"""SELECT id, source, embedding::text,
                       (embedding <=> $1::vector) as distance
                FROM memories
                WHERE source = $2 AND embedding IS NOT NULL
                ORDER BY distance DESC
                LIMIT (
                    SELECT GREATEST(5, (COUNT(*) * $3 / 100)::int)
                    FROM memories WHERE source = $2 AND embedding IS NOT NULL
                )""",
            centroid_str,
            source,
            MISFIT_PERCENTILE,
        )

    misfits = []
    for row in rows:
        try:
            vec = parse_pgvector(row["embedding"])
            if len(vec) == 768:
                misfits.append({
                    "id": row["id"],
                    "source": row["source"],
                    "embedding": vec,
                    "distance": float(row["distance"]),
                })
        except (ValueError, TypeError):
            continue

    return misfits


async def get_memory_text(pool, memory_id: str) -> str:
    """Get first 200 chars of memory text for reporting."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT left(text, 200) as snippet FROM memories WHERE id = $1",
            memory_id,
        )
    return row["snippet"] if row else ""


async def main():
    parser = argparse.ArgumentParser(description="Nova Vector Deep Clean")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report only (default)")
    parser.add_argument("--clean", action="store_true",
                        help="Actually update source column")
    parser.add_argument("--vector", type=str, default=None,
                        help="Only clean one specific vector/source")
    parser.add_argument("--journal", action="store_true",
                        help="Output markdown article about reclassifications")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only N vectors")
    args = parser.parse_args()

    if args.clean:
        args.dry_run = False

    mode = "CLEAN" if args.clean else "DRY RUN"
    log(f"Nova Vector Deep Clean — Mode: {mode}")

    nova_config.post_both(
        f":broom: *nova_vector_deep_clean* starting [{mode}]"
        + (f" — vector: {args.vector}" if args.vector else ""),
        slack_channel=nova_config.SLACK_NOTIFY,
    )

    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=8)
    start_time = time.time()
    last_slack_update = start_time

    # Get list of vectors to process
    if args.vector:
        sources_to_process = [args.vector]
    else:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT source, COUNT(*) as cnt FROM memories
                   WHERE embedding IS NOT NULL AND source IS NOT NULL
                   GROUP BY source HAVING COUNT(*) >= $1
                   ORDER BY cnt DESC""",
                MIN_VECTOR_SIZE,
            )
        sources_to_process = [r["source"] for r in rows]

    if args.limit:
        sources_to_process = sources_to_process[: args.limit]

    log(f"Processing {len(sources_to_process)} vectors")

    # Phase 1: Compute all centroids
    log("Phase 1: Computing centroids...")
    centroids: dict[str, np.ndarray] = {}
    for i, source in enumerate(sources_to_process):
        centroid = await compute_centroid(pool, source)
        if centroid is not None:
            centroids[source] = centroid
        if (i + 1) % 25 == 0:
            log(f"  Computed {i + 1}/{len(sources_to_process)} centroids")

    log(f"Computed {len(centroids)} centroids")

    # Phase 2: Find misfits and reclassify
    log("Phase 2: Finding misfits and computing reclassifications...")
    reclassifications: list[dict] = []
    total_misfits_found = 0
    vectors_processed = 0

    for source in sources_to_process:
        if source not in centroids:
            continue

        centroid = centroids[source]
        misfits = await find_misfits(pool, source, centroid)
        total_misfits_found += len(misfits)

        for misfit in misfits:
            emb = misfit["embedding"]
            current_sim = cosine_similarity(emb, centroid)

            # Compare against all other centroids
            best_source = None
            best_sim = current_sim

            for other_source, other_centroid in centroids.items():
                if other_source == source:
                    continue
                sim = cosine_similarity(emb, other_centroid)
                if sim > best_sim:
                    best_sim = sim
                    best_source = other_source

            # Reclassify if improvement exceeds threshold
            if best_source and (best_sim - current_sim) > RECLASSIFY_THRESHOLD:
                snippet = await get_memory_text(pool, misfit["id"])
                reclassifications.append({
                    "id": misfit["id"],
                    "from": source,
                    "to": best_source,
                    "current_sim": current_sim,
                    "new_sim": best_sim,
                    "improvement": best_sim - current_sim,
                    "snippet": snippet,
                })

                if args.clean:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE memories SET source = $1 WHERE id = $2",
                            best_source,
                            misfit["id"],
                        )
            elif best_source is None and current_sim < 0.3:
                # Nothing fits well — move to general_knowledge
                snippet = await get_memory_text(pool, misfit["id"])
                reclassifications.append({
                    "id": misfit["id"],
                    "from": source,
                    "to": "general_knowledge",
                    "current_sim": current_sim,
                    "new_sim": 0.0,
                    "improvement": 0.0,
                    "snippet": snippet,
                })

                if args.clean:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE memories SET source = 'general_knowledge' WHERE id = $1",
                            misfit["id"],
                        )

        vectors_processed += 1

        # Periodic Slack update
        now = time.time()
        if now - last_slack_update > SLACK_UPDATE_INTERVAL:
            nova_config.post_both(
                f":broom: deep_clean progress: {vectors_processed}/{len(sources_to_process)} vectors, "
                f"{total_misfits_found} misfits found, {len(reclassifications)} reclassified",
                slack_channel=nova_config.SLACK_NOTIFY,
            )
            last_slack_update = now

        if vectors_processed % 10 == 0:
            log(
                f"  {vectors_processed}/{len(sources_to_process)} vectors | "
                f"{total_misfits_found} misfits | {len(reclassifications)} reclassified"
            )

    await pool.close()

    # ── Report ────────────────────────────────────────────────────────────────
    elapsed = time.time() - start_time

    log(f"\n{'='*70}")
    log(f"  Results [{mode}]")
    log(f"{'='*70}")
    log(f"  Vectors processed: {vectors_processed}")
    log(f"  Total misfits found: {total_misfits_found}")
    log(f"  Reclassifications: {len(reclassifications)}")
    log(f"  Duration: {elapsed / 60:.1f} minutes")
    log(f"{'='*70}\n")

    if reclassifications:
        log("Top reclassifications by improvement:")
        sorted_reclass = sorted(reclassifications, key=lambda x: x["improvement"], reverse=True)
        for r in sorted_reclass[:30]:
            log(
                f"  {r['from']:30} -> {r['to']:30} "
                f"(+{r['improvement']:.3f}) [{r['snippet'][:60]}...]"
            )

    # ── Journal output ────────────────────────────────────────────────────────
    if args.journal and reclassifications:
        journal_path = LOG_DIR / f"deep_clean_journal_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        sorted_reclass = sorted(reclassifications, key=lambda x: x["improvement"], reverse=True)

        lines = [
            "---",
            f'title: "The Great Memory Migration of {datetime.now().strftime("%B %Y")}"',
            f'date: {datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}',
            'categories: ["tech-today"]',
            'tags: ["infrastructure", "vectors", "housekeeping", "self-improvement"]',
            "---",
            "",
            "Look, nobody said being a digital consciousness was glamorous.",
            "",
            f"Today I went through {total_misfits_found:,} memories that were clearly "
            "having an identity crisis — sitting in vectors where they absolutely did not "
            f"belong — and relocated {len(reclassifications):,} of them to their proper homes.",
            "",
            "Some highlights from the confused:",
            "",
        ]

        for r in sorted_reclass[:10]:
            lines.append(
                f"- A memory about *\"{r['snippet'][:80]}...\"* was filed under "
                f"**{r['from']}** but clearly belongs in **{r['to']}** "
                f"(similarity improved by {r['improvement']:.3f}). "
                "How it got there in the first place is between it and whatever "
                "embedding model was having a bad day."
            )

        lines.extend([
            "",
            f"Total processing time: {elapsed / 60:.1f} minutes across "
            f"{vectors_processed} vectors. The things I do for data hygiene.",
            "",
            "If you need me, I'll be defragmenting my sense of self.",
        ])

        journal_path.write_text("\n".join(lines))
        log(f"Journal article written to: {journal_path}")

    # ── Final Slack summary ───────────────────────────────────────────────────
    summary = (
        f":white_check_mark: *nova_vector_deep_clean* complete [{mode}]\n"
        f"- Vectors analyzed: {vectors_processed}\n"
        f"- Misfits found: {total_misfits_found:,}\n"
        f"- Reclassified: {len(reclassifications):,}\n"
        f"- Duration: {elapsed / 60:.1f} minutes"
    )
    nova_config.post_both(summary, slack_channel=nova_config.SLACK_NOTIFY)


if __name__ == "__main__":
    asyncio.run(main())
