#!/usr/bin/env python3
"""
nova_reembed.py — Re-embed all memories with a new embedding model.

This is a DESTRUCTIVE, LONG-RUNNING operation (~18-24 hours for 1.35M memories).
Run overnight with nohup. Posts progress to Slack every 5 minutes.

Steps:
  1. Alter embedding column dimension if needed
  2. Drop HNSW indexes (they'll be wrong dimension)
  3. Re-embed all memories in batches via Ollama
  4. Rebuild HNSW indexes
  5. Update memory_server.py config

Usage:
  # Dry run — just count and estimate time
  python3 nova_reembed.py --dry-run

  # Full re-embed (run overnight with nohup)
  nohup python3 nova_reembed.py --model snowflake-arctic-embed:335m --dims 1024 > /tmp/reembed.log 2>&1 &

  # Resume after interruption (skips already-embedded rows)
  python3 nova_reembed.py --model snowflake-arctic-embed:335m --dims 1024 --resume

Written by Jordan Koch.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

PG_CONN = "host=127.0.0.1 dbname=nova_memories"
OLLAMA_URL = "http://127.0.0.1:11434/api/embed"
BATCH_SIZE = 100
STATUS_INTERVAL = 300


def log(msg):
    print(f"[reembed {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def post_slack(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def embed(text, model):
    payload = json.dumps({"model": model, "input": text}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    embeddings = data.get("embeddings") or data.get("embedding")
    return embeddings[0] if isinstance(embeddings[0], list) else embeddings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="snowflake-arctic-embed:335m")
    parser.add_argument("--dims", type=int, default=1024)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    import psycopg2

    conn = psycopg2.connect(PG_CONN)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM memories")
    total = cur.fetchone()[0]

    # Check current dimension
    cur.execute("SELECT embedding FROM memories WHERE embedding IS NOT NULL LIMIT 1")
    row = cur.fetchone()
    current_dims = len(row[0]) if row and row[0] else 768

    log(f"Total memories: {total:,}")
    log(f"Current dims: {current_dims}, target dims: {args.dims}")
    log(f"Model: {args.model}")
    log(f"Estimated time: {total * 0.05 / 3600:.1f} hours (at ~20 embeds/sec)")

    if args.dry_run:
        log("Dry run — no changes made.")
        return

    post_slack(
        f":brain: *Re-embedding Started*\n"
        f"• Memories: {total:,}\n"
        f"• Model: {args.model} ({args.dims} dims)\n"
        f"• Estimated: {total * 0.05 / 3600:.1f} hours"
    )

    # Step 1: Alter column dimension if needed
    if current_dims != args.dims:
        log(f"Altering embedding column: vector({current_dims}) -> vector({args.dims})...")
        cur.execute("DROP INDEX IF EXISTS memories_embedding_hnsw")
        cur.execute("DROP INDEX IF EXISTS memories_hnsw_email")
        cur.execute("DROP INDEX IF EXISTS memories_hnsw_imessage")
        cur.execute("DROP INDEX IF EXISTS memories_hnsw_music")
        cur.execute("DROP INDEX IF EXISTS memories_hnsw_vehicles")
        cur.execute("DROP INDEX IF EXISTS memories_hnsw_health")
        log("Dropped HNSW indexes")

        cur.execute(f"ALTER TABLE memories ALTER COLUMN embedding TYPE vector({args.dims})")
        log(f"Column altered to vector({args.dims})")

    # Step 2: Re-embed in batches
    if args.resume:
        cur.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NULL")
        remaining = cur.fetchone()[0]
        log(f"Resume mode: {remaining:,} memories need embedding")
        cur.execute("SELECT id, text FROM memories WHERE embedding IS NULL ORDER BY created_at")
    else:
        log("Setting all embeddings to NULL for fresh re-embed...")
        cur.execute("UPDATE memories SET embedding = NULL")
        log("All embeddings cleared")
        cur.execute("SELECT id, text FROM memories ORDER BY created_at")

    embedded = 0
    errors = 0
    start = time.time()
    last_status = start

    rows = cur.fetchmany(BATCH_SIZE)
    while rows:
        for mem_id, text in rows:
            try:
                vec = embed(text[:2000], args.model)
                vec_str = "[" + ",".join(str(v) for v in vec) + "]"
                cur.execute("UPDATE memories SET embedding = %s::vector WHERE id = %s", (vec_str, mem_id))
                embedded += 1
            except Exception as e:
                errors += 1
                if errors % 100 == 0:
                    log(f"  {errors} errors so far, latest: {e}")

            if embedded % 1000 == 0:
                elapsed = time.time() - start
                rate = embedded / elapsed if elapsed > 0 else 0
                eta = (total - embedded) / rate / 3600 if rate > 0 else 0
                log(f"  {embedded:,}/{total:,} ({embedded/total*100:.1f}%) | {rate:.1f}/sec | ETA: {eta:.1f}h | errors: {errors}")

            now = time.time()
            if now - last_status >= STATUS_INTERVAL:
                elapsed = now - start
                rate = embedded / elapsed if elapsed > 0 else 0
                eta = (total - embedded) / rate / 3600 if rate > 0 else 0
                post_slack(
                    f":brain: *Re-embedding Progress*\n"
                    f"• {embedded:,}/{total:,} ({embedded/total*100:.0f}%)\n"
                    f"• Rate: {rate:.1f}/sec | ETA: {eta:.1f}h\n"
                    f"• Errors: {errors}"
                )
                last_status = now

        rows = cur.fetchmany(BATCH_SIZE)

    elapsed = time.time() - start
    log(f"Re-embedding complete: {embedded:,} in {elapsed/3600:.1f}h, {errors} errors")

    # Step 3: Rebuild HNSW indexes
    log("Rebuilding HNSW indexes...")
    cur.execute(f"""
        CREATE INDEX memories_embedding_hnsw ON memories
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 32, ef_construction = 200)
    """)
    log("Main HNSW index built")

    for source, idx_name in [
        ("email_archive", "memories_hnsw_email"),
        ("imessage", "memories_hnsw_imessage"),
        ("vehicles", "memories_hnsw_vehicles"),
    ]:
        cur.execute(f"""
            CREATE INDEX {idx_name} ON memories
            USING hnsw (embedding vector_cosine_ops)
            WHERE source = '{source}'
            WITH (m = 32, ef_construction = 200)
        """)
        log(f"  {idx_name} built")

    conn.close()

    post_slack(
        f":white_check_mark: *Re-embedding Complete*\n"
        f"• {embedded:,} memories re-embedded\n"
        f"• Model: {args.model} ({args.dims} dims)\n"
        f"• Time: {elapsed/3600:.1f}h | Errors: {errors}\n"
        f"• HNSW indexes rebuilt"
    )
    log("Done.")


if __name__ == "__main__":
    main()
