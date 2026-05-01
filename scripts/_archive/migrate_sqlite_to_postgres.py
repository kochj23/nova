#!/usr/bin/env python3
"""
migrate_sqlite_to_postgres.py — One-time migration from SQLite+FAISS to PostgreSQL+pgvector.

Reads all rows from nova_memories.db and inserts into PostgreSQL nova_memories table.
Uses batch inserts for speed. Skips rows that already exist (safe to re-run).

Usage: python3 migrate_sqlite_to_postgres.py
Author: Jordan Koch / kochj23
"""

import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

SQLITE_PATH = Path.home() / ".openclaw" / "memory_db" / "nova_memories.db"
PG_DSN      = "postgresql://localhost/nova_memories"
BATCH_SIZE  = 500

def log(msg):
    print(f"[migrate {time.strftime('%H:%M:%S')}] {msg}", flush=True)

async def migrate():
    log(f"Opening SQLite: {SQLITE_PATH}")
    sqlite = sqlite3.connect(str(SQLITE_PATH))
    sqlite.row_factory = sqlite3.Row

    total = sqlite.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    log(f"SQLite rows to migrate: {total:,}")

    log("Connecting to PostgreSQL...")
    pg = await asyncpg.create_pool(PG_DSN, min_size=4, max_size=8)

    # Enable pgvector
    async with pg.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        already = await conn.fetchval("SELECT COUNT(*) FROM memories")
        log(f"PostgreSQL already has {already:,} rows — will skip duplicates")

    rows = sqlite.execute(
        "SELECT id, text, metadata, embedding, source, created_at FROM memories"
    ).fetchall()

    inserted = 0
    skipped  = 0
    errors   = 0
    t0       = time.time()

    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch = rows[batch_start:batch_start + BATCH_SIZE]
        records = []
        for row in batch:
            try:
                vec = json.loads(row["embedding"])
                vec_str = "[" + ",".join(str(v) for v in vec) + "]"
                # Parse ISO string to datetime (asyncpg requires datetime object)
                created_str = row["created_at"] or datetime.utcnow().isoformat()
                try:
                    created_dt = datetime.fromisoformat(created_str).replace(tzinfo=timezone.utc)
                except Exception:
                    created_dt = datetime.now(timezone.utc)
                records.append((
                    row["id"], row["text"],
                    row["metadata"] or "{}",
                    vec_str,
                    row["source"] or "unknown",
                    created_dt,
                ))
            except Exception as e:
                errors += 1
                continue

        if not records:
            continue

        try:
            async with pg.acquire() as conn:
                result = await conn.executemany(
                    """INSERT INTO memories (id, text, metadata, embedding, source, created_at)
                       VALUES ($1, $2, $3::jsonb, $4::vector, $5, $6)
                       ON CONFLICT (id) DO NOTHING""",
                    records
                )
            inserted += len(records)
        except Exception as e:
            log(f"  Batch error at row {batch_start}: {e}")
            errors += len(batch)

        if (batch_start + BATCH_SIZE) % 5000 == 0:
            elapsed = time.time() - t0
            rate = inserted / elapsed if elapsed > 0 else 0
            pct  = (batch_start + BATCH_SIZE) / total * 100
            log(f"  Progress: {batch_start + BATCH_SIZE:,}/{total:,} ({pct:.0f}%) "
                f"— {rate:.0f} rows/sec — eta {(total - batch_start) / max(rate,1):.0f}s")

    elapsed = time.time() - t0
    log(f"Migration complete in {elapsed:.1f}s")
    log(f"  Inserted: {inserted:,}")
    log(f"  Skipped (duplicates): {skipped:,}")
    log(f"  Errors: {errors:,}")

    # Verify
    async with pg.acquire() as conn:
        pg_count = await conn.fetchval("SELECT COUNT(*) FROM memories")
        log(f"  PostgreSQL final count: {pg_count:,}")

    if pg_count < total * 0.99:
        log(f"WARNING: Only {pg_count}/{total} rows migrated ({pg_count/total*100:.1f}%)")
        sys.exit(1)
    else:
        log(f"✅ Migration verified: {pg_count:,} rows in PostgreSQL")

    await pg.close()
    sqlite.close()

if __name__ == "__main__":
    asyncio.run(migrate())
