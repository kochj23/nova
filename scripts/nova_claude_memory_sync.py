#!/usr/bin/env python3
"""
nova_claude_memory_sync.py — Sync Claude's memories from nova_ops into Nova's vector memory.

Reads claude_memories table, ingests each memory into the vector DB under
source="claude_memory". Tracks what's been synced to avoid duplicates.
Runs daily via scheduler.

Written by Jordan Koch.
"""

import hashlib
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

try:
    import psycopg2
except ImportError:
    print("FATAL: psycopg2 not installed", file=sys.stderr)
    sys.exit(1)

MEMORY_URL = "http://192.168.1.6:18790/remember"
OPS_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
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


def get_synced_hashes(conn):
    """Get hashes of already-synced memories from vector DB."""
    try:
        payload = json.dumps({"source": SOURCE, "n": 50000}).encode()
        req = urllib.request.Request(
            "http://192.168.1.6:18790/recall?source=claude_memory&n=1&q=test",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            count = data.get("total", 0)
    except Exception:
        count = 0
    return count


def main():
    log("=== Claude Memory Sync starting ===")

    conn = psycopg2.connect(OPS_DSN)
    cur = conn.cursor()

    cur.execute("SELECT name, description, type, content, updated_at FROM claude_memories ORDER BY updated_at")
    rows = cur.fetchall()
    log(f"Found {len(rows)} Claude memories in nova_ops")

    synced = 0
    skipped = 0

    for name, description, mem_type, content, updated_at in rows:
        text_hash = hashlib.md5(content.strip().encode()).hexdigest()

        # Check if this exact content is already in vector memory
        try:
            check_url = f"http://192.168.1.6:18790/recall?q={urllib.parse.quote(name[:50])}&source={SOURCE}&n=1&min_score=0.95"
            req = urllib.request.Request(check_url)
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
                existing = data.get("memories", [])
                if existing and existing[0].get("metadata", {}).get("hash") == text_hash:
                    skipped += 1
                    continue
        except Exception:
            pass

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
        nova_config.post_both(
            f":brain: *Claude Memory Sync* — {synced} memories synced to Nova vector DB"
            f" ({skipped} unchanged)",
            slack_channel=nova_config.SLACK_NOTIFY,
        )


if __name__ == "__main__":
    import urllib.parse
    main()
