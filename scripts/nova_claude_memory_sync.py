#!/usr/bin/env python3
"""
nova_claude_memory_sync.py — Sync Claude's memories from nova_ops into Nova's vector memory.

Reads claude_memories table, ingests each memory into the vector DB under
source="claude_memory". Tracks what's been synced to avoid duplicates.
Runs daily via scheduler.

Written by Jordan Koch.
"""

import nova_dsn as _nova_dsn  # noqa: E402
import hashlib
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_notify import notify

try:
    import psycopg2
except ImportError:
    print("FATAL: psycopg2 not installed", file=sys.stderr)
    sys.exit(1)

MEMORY_URL = "http://memory-server.digitalnoise.net:18790/remember"
OPS_DSN = _nova_dsn.pg_url("nova_ops")
SOURCE = "claude_memory"
LOG_FILE = Path.home() / ".openclaw/logs/claude_memory_sync.log"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[claude_memory_sync {ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def remember(text, metadata):
    payload = json.dumps({
        "text": nova_config.truncate_at_boundary(text),
        "source": SOURCE,
        "tier": "long_term",
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:
        log(f"  Ingest error: {e}")
        return False


def get_synced_hashes() -> set:
    """Content hashes already in vector memory, in ONE read of nova_memories (read-only).
    The old per-memory check was a semantic /recall of the NAME with min_score=0.95 — a name never
    scores 0.95 against the full text (~0.69), so every run re-POSTed all ~200 memories (~1 s each
    through the embedder) and hit the 120 s task timeout daily (2026-10-21)."""
    try:
        with psycopg2.connect(_nova_dsn.pg_dsn("nova_memories", "connect_timeout=10")) as c, c.cursor() as cur:
            # any source: the memory server dedups on text, and some were first stored as nova_project_docs
            cur.execute("SELECT DISTINCT metadata->>'hash' FROM memories WHERE source = %s "
                        "OR text LIKE '[Claude Memory: %%'", (SOURCE,))
            return {r[0] for r in cur.fetchall() if r[0]}
    except Exception as e:
        log(f"could not read synced hashes ({e.__class__.__name__}) — syncing everything")
        return set()


def main():
    log("=== Claude Memory Sync starting ===")

    conn = psycopg2.connect(OPS_DSN)
    cur = conn.cursor()

    cur.execute("SELECT name, description, type, content, updated_at FROM claude_memories ORDER BY updated_at")
    rows = cur.fetchall()
    log(f"Found {len(rows)} Claude memories in nova_ops")

    synced = 0
    skipped = 0
    have = get_synced_hashes()

    for name, description, mem_type, content, updated_at in rows:
        text_hash = hashlib.md5(content.strip().encode()).hexdigest()

        if text_hash in have:            # this exact content is already in vector memory
            skipped += 1
            continue

        # Ingest the memory
        metadata = {
            "name": name,
            "type": mem_type,
            "description": description or "",
            "hash": text_hash,
            "synced_at": datetime.now().isoformat(),
            "updated_at": str(updated_at) if updated_at else "",
        }

        full_text = f"[Claude Memory: {name}] ({mem_type}) {description or ''}\n\n{content}"

        if remember(full_text, metadata):
            synced += 1
            log(f"  ✓ {name} ({mem_type})")
        else:
            log(f"  ✗ {name} — failed")

    cur.close()
    conn.close()

    log(f"Sync complete: {synced} synced, {skipped} unchanged")
    if synced > 0:
        notify(
            "Claude Memory Sync",
            body=f"{synced} memories synced to Nova vector DB ({skipped} unchanged)",
            level="info", category="memory_ingest", dedup_key="claude-memory-sync",
        )


if __name__ == "__main__":
    import urllib.parse
    main()
