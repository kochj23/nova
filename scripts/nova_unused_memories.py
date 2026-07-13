#!/usr/bin/env python3
"""nova_unused_memories.py — the oldest never-used memories, privacy-filtered.

Shared by nova_art_corner.py and dream_generate.py. Each run bases its piece on
the top-100 OLDEST unused memories (access_count = 0), then marks them used so the
pool advances and the same 100 don't recur.

PRIVATE sources (email, iMessage, work, health, financial…) are excluded via the
single authoritative gate nova_config.is_private_source — these memories feed
PUBLIC output (nova.digitalnoise.net), so this filter is mandatory, not optional.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))
import psycopg2
import psycopg2.extras
import nova_config

DSN = "host=localhost dbname=nova_memories user=kochj"

# Nova's own internal/operational/meta memories — these contain identifiers (Slack
# workspace/user/channel IDs, IPs, service names), past dreams, work-pattern syntheses,
# and daily ops logs. They are NOT knowledge and must NEVER feed public creative output
# (dreams, art). This is on TOP of nova_config.is_private_source (personal/work/health).
INTERNAL_SOURCES = {
    "nova_operational", "nova_meta", "synthesis", "nightly", "dream", "dreams",
    "morning_brief", "homekit", "home_automation", "claude_memory", "app_watchdog",
    "face_recognition", "plex_watch_history", "livetv_dream_fuel", "livetv_news",
    "herd_blog", "blog_post_chunk", "scratchpad", "local_burbank", "infrastructure",
    "nova_canary", "agent_analyst", "agent_coder", "agent_librarian",
}


def fetch_unused(n: int = 100) -> list[dict]:
    """Oldest `access_count = 0` memories with private sources excluded.

    Returns up to `n` dicts (id, text, source, metadata, created_at). Over-fetches
    (n*4) then drops private rows in Python so substring-private sources the SQL
    IN-list misses (work_*, *email*, *health*) are still caught."""
    blocked = tuple(set(nova_config.PRIVATE_SOURCES) | INTERNAL_SOURCES) or ("",)
    with psycopg2.connect(DSN) as c, \
         c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, text, source, metadata, created_at FROM memories "
            "WHERE coalesce(access_count,0) = 0 "
            "  AND privacy IS DISTINCT FROM 'local-only' "
            "  AND source NOT IN %s "
            "ORDER BY created_at ASC LIMIT %s",
            (blocked, n * 4))
        rows = cur.fetchall()
    safe = [dict(r) for r in rows
            if not nova_config.is_private_source(r.get("source", ""))
            and r.get("source", "") not in INTERNAL_SOURCES]
    return safe[:n]


def mark_used(ids) -> int:
    """Bump access_count + stamp accessed_at for these memory ids. Returns rowcount."""
    ids = [i for i in ids if i]
    if not ids:
        return 0
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute(
            "UPDATE memories SET access_count = coalesce(access_count,0) + 1, accessed_at = now() "
            "WHERE id = ANY(%s)", (ids,))
        c.commit()
        return cur.rowcount


def demo():
    rows = fetch_unused(100)
    assert isinstance(rows, list) and len(rows) <= 100
    assert all(not nova_config.is_private_source(r.get("source", "")) for r in rows), \
        "PRIVATE SOURCE LEAKED — abort"
    from collections import Counter
    top = Counter(r["source"] for r in rows).most_common(6)
    print(f"OK: {len(rows)} oldest-unused, zero private. top sources: {top}")


if __name__ == "__main__":
    demo()
