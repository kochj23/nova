#!/usr/bin/env python3
"""
nova_proactive_brief.py — Proactive context surfacing from Nova's memory DB.

Monitors incoming messages (Slack chatroom + email) and surfaces relevant
cross-domain memories Jordan might not think to ask about.

"I noticed you were discussing X — I have Y relevant memories in Z domain."

Runs every 2 hours during work hours (9am-6pm).
Cron: 0 9,11,13,15,17 * * 1-5

Written by Jordan Koch.
"""

import asyncio
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:
    import asyncpg
except ImportError:
    print("[proactive_brief] FATAL: asyncpg not installed", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Configuration ────────────────────────────────────────────────────────────

OPS_DSN = "postgresql://kochj@192.168.1.6:5432/nova_ops"
MEMORIES_DSN = "postgresql://kochj@192.168.1.6:5432/nova_memories"
MEMORY_SERVER = "http://192.168.1.6:18790"
STATE_DIR = Path.home() / ".openclaw" / "workspace" / "state"
STATE_FILE = STATE_DIR / "proactive_brief_state.json"
LOOKBACK_HOURS = 2
SIMILARITY_THRESHOLD = 0.82
MAX_BRIEFINGS_PER_RUN = 3
MIN_MESSAGE_LENGTH = 50
NOW = datetime.now(timezone.utc)


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[proactive_brief {ts}] {msg}", flush=True)


# ── State Management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load previously-briefed message IDs to avoid duplicates."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            # Prune entries older than 7 days
            cutoff = (NOW - timedelta(days=7)).isoformat()
            data["briefed"] = [
                b for b in data.get("briefed", [])
                if b.get("ts", "") > cutoff
            ]
            return data
        except (json.JSONDecodeError, KeyError):
            pass
    return {"briefed": [], "last_run": None}


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["last_run"] = NOW.isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def already_briefed(state: dict, fingerprint: str) -> bool:
    """Check if we already briefed on this message content."""
    return any(b.get("fp") == fingerprint for b in state.get("briefed", []))


def record_briefed(state: dict, fingerprint: str, topic: str):
    state.setdefault("briefed", []).append({
        "fp": fingerprint,
        "topic": topic,
        "ts": NOW.isoformat(),
    })


# ── Embedding via Memory Server ──────────────────────────────────────────────

def get_embedding(text: str) -> Optional[list[float]]:
    """Get embedding vector from Nova's memory server."""
    try:
        payload = json.dumps({"text": text[:2000]}).encode()
        req = urllib.request.Request(
            f"{MEMORY_SERVER}/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("embedding") or data.get("vector")
    except Exception as e:
        log(f"Embedding failed: {e}")
        return None


# ── Database Queries ─────────────────────────────────────────────────────────

async def get_recent_chatroom_messages(pool) -> list[dict]:
    """Pull Jordan's messages from the last LOOKBACK_HOURS hours."""
    cutoff = NOW - timedelta(hours=LOOKBACK_HOURS)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, sender, message, metadata, created_at
            FROM chatroom_messages
            WHERE created_at > $1
              AND sender = 'Jordan'
              AND length(message) > $2
            ORDER BY created_at DESC
            LIMIT 20
        """, cutoff, MIN_MESSAGE_LENGTH)
    return [dict(r) for r in rows]


async def get_recent_emails(pool) -> list[dict]:
    """Pull recent email archive entries from nova_memories."""
    cutoff = NOW - timedelta(hours=LOOKBACK_HOURS)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, text, source, metadata, created_at
            FROM memories
            WHERE source = 'email_archive'
              AND created_at > $1
              AND length(text) > $2
            ORDER BY created_at DESC
            LIMIT 10
        """, cutoff, MIN_MESSAGE_LENGTH)
    return [dict(r) for r in rows]


async def search_memories_by_vector(pool, embedding: list[float], exclude_source: str) -> list[dict]:
    """
    Search full memory DB for high-similarity matches from DIFFERENT domains.
    Uses pgvector cosine distance: 1 - (embedding <=> query) = similarity.
    """
    vec_str = f"[{','.join(str(x) for x in embedding)}]"
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                id,
                text,
                source,
                metadata,
                1 - (embedding <=> $1::vector) AS similarity
            FROM memories
            WHERE source != $2
              AND 1 - (embedding <=> $1::vector) > $3
            ORDER BY embedding <=> $1::vector
            LIMIT 10
        """, vec_str, exclude_source, SIMILARITY_THRESHOLD)
    return [dict(r) for r in rows]


# ── Message Processing ───────────────────────────────────────────────────────

def is_substantive(text: str) -> bool:
    """Filter out greetings, acknowledgements, and trivial messages."""
    lower = text.lower().strip()
    trivial_patterns = [
        "thanks", "thank you", "ok", "okay", "got it", "sounds good",
        "good morning", "good night", "hello", "hey", "hi nova",
        "nice", "cool", "perfect", "great", "awesome", "yep", "nope",
    ]
    if any(lower == p or lower.startswith(p + " ") for p in trivial_patterns):
        return False
    if len(text.strip()) < MIN_MESSAGE_LENGTH:
        return False
    return True


def extract_topic(text: str) -> str:
    """Extract a brief topic description from message text."""
    # Take first sentence or first 80 chars
    first_line = text.split("\n")[0].strip()
    if len(first_line) > 80:
        # Try to break at a word boundary
        truncated = first_line[:80].rsplit(" ", 1)[0]
        return truncated + "..."
    return first_line


def fingerprint(text: str) -> str:
    """Simple content fingerprint for deduplication."""
    import hashlib
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


# ── Briefing Formatter ───────────────────────────────────────────────────────

def format_briefing(topic: str, matches: list[dict]) -> str:
    """Format a proactive briefing message for Slack."""
    # Group by source domain
    domains = {}
    for m in matches:
        src = m.get("source", "unknown")
        domains.setdefault(src, []).append(m)

    domain_summaries = []
    for src, mems in sorted(domains.items(), key=lambda x: -len(x[1])):
        excerpt = mems[0]["text"][:120].replace("\n", " ").strip()
        sim = mems[0].get("similarity", 0)
        domain_summaries.append(
            f"  - *{src}* ({len(mems)} match{'es' if len(mems) > 1 else ''}, "
            f"{sim:.0%} relevance): _{excerpt}_"
        )

    total = sum(len(v) for v in domains.values())
    header = f":brain: *Proactive Brief* — Re: _{topic}_"
    body = f"I have {total} relevant memories across {len(domains)} domain(s):\n"
    body += "\n".join(domain_summaries[:5])
    footer = "\nWant me to pull the full context?"

    return f"{header}\n{body}{footer}"


# ── Main Logic ───────────────────────────────────────────────────────────────

async def run():
    # Check work hours (9am-6pm local)
    local_hour = datetime.now().hour
    if local_hour < 9 or local_hour >= 18:
        log("Outside work hours (9am-6pm). Exiting.")
        return

    state = load_state()
    briefings_posted = 0

    # Connect to both databases
    try:
        ops_pool = await asyncpg.create_pool(OPS_DSN, min_size=1, max_size=2, command_timeout=15)
        mem_pool = await asyncpg.create_pool(MEMORIES_DSN, min_size=1, max_size=2, command_timeout=15)
    except Exception as e:
        log(f"Database connection failed: {e}")
        sys.exit(1)

    try:
        # Gather recent messages from both sources
        chatroom_msgs = await get_recent_chatroom_messages(ops_pool)
        email_msgs = await get_recent_emails(mem_pool)
        log(f"Found {len(chatroom_msgs)} chatroom messages, {len(email_msgs)} emails in last {LOOKBACK_HOURS}h")

        # Build unified message list
        messages = []
        for m in chatroom_msgs:
            messages.append({
                "text": m["message"],
                "source": "chatroom",
                "created_at": m["created_at"],
            })
        for m in email_msgs:
            messages.append({
                "text": m["text"],
                "source": "email_archive",
                "created_at": m["created_at"],
            })

        if not messages:
            log("No recent messages to process. Done.")
            save_state(state)
            return

        # Process each message for cross-domain relevance
        for msg in messages:
            if briefings_posted >= MAX_BRIEFINGS_PER_RUN:
                log(f"Hit max briefings ({MAX_BRIEFINGS_PER_RUN}). Stopping.")
                break

            text = msg["text"]
            if not is_substantive(text):
                continue

            fp = fingerprint(text)
            if already_briefed(state, fp):
                continue

            # Generate embedding
            embedding = get_embedding(text)
            if not embedding:
                continue

            # Search for cross-domain matches
            matches = await search_memories_by_vector(mem_pool, embedding, msg["source"])

            # Filter out private sources that shouldn't be surfaced in Slack
            matches = [
                m for m in matches
                if not nova_config.is_private_source(m.get("source", ""))
            ]

            if not matches:
                continue

            # We have genuinely useful cross-domain context
            topic = extract_topic(text)
            briefing = format_briefing(topic, matches)
            log(f"Posting briefing for: {topic[:60]}")
            nova_config.post_both(briefing, slack_channel=nova_config.SLACK_NOTIFY)
            record_briefed(state, fp, topic)
            briefings_posted += 1

        log(f"Run complete. Posted {briefings_posted} briefing(s).")
        save_state(state)

    finally:
        await ops_pool.close()
        await mem_pool.close()


def main():
    log("Starting proactive brief run...")
    asyncio.run(run())
    log("Done.")
    sys.exit(0)


if __name__ == "__main__":
    main()
