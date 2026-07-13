#!/usr/bin/env python3
"""
nova_chatroom_sessions.py — Creates conversation session summaries for the chatroom.

Monitors chatroom_messages in nova_ops and generates periodic summaries using
Ollama when message count thresholds are reached. Summaries provide context
for Nova's responses without re-reading entire chat history.

Usage:
  - Called from nova_chatroom.py after each message (fire-and-forget)
  - OR as standalone cron: python3 nova_chatroom_sessions.py
  - Importable: from nova_chatroom_sessions import maybe_summarize, get_session_context

Table created: chatroom_sessions (in nova_ops)

Written by Jordan Koch.
"""

import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import asyncpg

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config

# ── Configuration ────────────────────────────────────────────────────────────

DB_DSN = "postgresql://kochj@192.168.1.6:5432/nova_ops"
OLLAMA_URL = "http://192.168.1.6:11434/api/chat"
OLLAMA_MODEL = "qwen3-coder:30b"
MESSAGE_THRESHOLD = 20  # summarize after this many new messages

LOG_DIR = Path.home() / ".openclaw/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [chatroom-sessions] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "nova_chatroom_sessions.log"),
    ],
)
log = logging.getLogger("chatroom-sessions")

# ── Schema ───────────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS chatroom_sessions (
    session_id SERIAL PRIMARY KEY,
    channel TEXT DEFAULT 'general',
    started_at TIMESTAMPTZ DEFAULT now(),
    ended_at TIMESTAMPTZ,
    summary TEXT,
    key_topics TEXT[],
    message_count INTEGER DEFAULT 0,
    first_msg_id INTEGER,
    last_msg_id INTEGER
);
"""

SYSTEM_PROMPT = (
    "Summarize this chatroom conversation. Extract 3-5 key topics as short phrases. "
    "Be concise — 2-3 sentences max. Format your response as:\n"
    "SUMMARY: <your summary>\n"
    "TOPICS: <topic1>, <topic2>, <topic3>"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from LLM output."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def parse_summary_response(text: str) -> tuple[str, list[str]]:
    """Parse the LLM response into summary and topics list."""
    text = strip_think_tags(text)

    summary = text
    topics = []

    # Try structured format first
    summary_match = re.search(r"SUMMARY:\s*(.+?)(?=TOPICS:|$)", text, re.DOTALL | re.IGNORECASE)
    topics_match = re.search(r"TOPICS:\s*(.+)", text, re.DOTALL | re.IGNORECASE)

    if summary_match:
        summary = summary_match.group(1).strip()
    if topics_match:
        raw_topics = topics_match.group(1).strip()
        topics = [t.strip().strip("-•") for t in re.split(r"[,\n]", raw_topics) if t.strip()]

    # Fallback: if no structured format, use whole text as summary
    if not topics:
        # Try to extract anything that looks like a list
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) > 1:
            summary = lines[0]
            topics = lines[1:5]

    return summary[:1000], topics[:5]


# ── Core Functions ───────────────────────────────────────────────────────────

async def ensure_table(conn: asyncpg.Connection):
    """Create chatroom_sessions table if it doesn't exist."""
    await conn.execute(CREATE_TABLE_SQL)


async def get_new_message_count(conn: asyncpg.Connection, channel: str = "general") -> tuple[int, int]:
    """Return (count_of_new_messages, last_summarized_msg_id)."""
    last_id = await conn.fetchval(
        """
        SELECT COALESCE(last_msg_id, 0)
        FROM chatroom_sessions
        WHERE channel = $1
        ORDER BY session_id DESC
        LIMIT 1
        """,
        channel,
    )
    last_id = last_id or 0

    count = await conn.fetchval(
        "SELECT count(*) FROM chatroom_messages WHERE id > $1",
        last_id,
    )
    return count, last_id


async def fetch_messages_since(conn: asyncpg.Connection, since_id: int, limit: int = 100) -> list[dict]:
    """Fetch messages after a given ID."""
    rows = await conn.fetch(
        """
        SELECT id, sender, message, created_at
        FROM chatroom_messages
        WHERE id > $1
        ORDER BY id ASC
        LIMIT $2
        """,
        since_id, limit,
    )
    return [dict(r) for r in rows]


async def summarize_with_ollama(messages: list[dict]) -> str | None:
    """Send messages to Ollama for summarization. Returns raw response text."""
    # Format messages into a conversation transcript
    transcript_lines = []
    for msg in messages:
        ts = msg["created_at"].strftime("%H:%M") if msg.get("created_at") else ""
        sender = msg.get("sender", "unknown")
        text = msg.get("message", "")
        transcript_lines.append(f"[{ts}] {sender}: {text}")

    transcript = "\n".join(transcript_lines)

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Summarize this conversation:\n\n{transcript}"},
        ],
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 300},
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_URL, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    log.error(f"Ollama returned {resp.status}: {await resp.text()}")
                    return None
                data = await resp.json()
                return data.get("message", {}).get("content", "")
    except Exception as e:
        log.error(f"Ollama request failed: {e}")
        return None


async def store_session(conn: asyncpg.Connection, channel: str, summary: str,
                        topics: list[str], messages: list[dict]):
    """Write a session summary to the database."""
    first_id = messages[0]["id"]
    last_id = messages[-1]["id"]
    started_at = messages[0].get("created_at")
    ended_at = messages[-1].get("created_at")

    await conn.execute(
        """
        INSERT INTO chatroom_sessions (channel, started_at, ended_at, summary, key_topics,
                                       message_count, first_msg_id, last_msg_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        channel, started_at, ended_at, summary, topics,
        len(messages), first_id, last_id,
    )
    log.info(f"Session stored: {len(messages)} msgs, topics={topics}")


# ── Public API ───────────────────────────────────────────────────────────────

async def maybe_summarize(channel: str = "general") -> bool:
    """
    Check if enough messages have accumulated and summarize if needed.
    Returns True if a summary was created, False otherwise.
    Safe to call frequently — exits quickly if threshold not met.
    """
    conn = await asyncpg.connect(DB_DSN)
    try:
        await ensure_table(conn)
        count, last_id = await get_new_message_count(conn, channel)

        if count < MESSAGE_THRESHOLD:
            log.debug(f"Only {count} new messages (threshold={MESSAGE_THRESHOLD}), skipping")
            return False

        log.info(f"{count} new messages since #{last_id} — generating summary")

        messages = await fetch_messages_since(conn, last_id, limit=100)
        if not messages:
            return False

        raw_response = await summarize_with_ollama(messages)
        if not raw_response:
            log.warning("Ollama returned empty response, skipping summary")
            return False

        summary, topics = parse_summary_response(raw_response)
        await store_session(conn, channel, summary, topics, messages)
        return True

    except Exception as e:
        log.error(f"Summarization failed: {e}")
        return False
    finally:
        await conn.close()


async def get_session_context(channel: str = "general", limit: int = 3) -> str:
    """
    Retrieve recent session summaries for prompt injection.
    Returns a formatted string suitable for including in system prompts.
    """
    conn = await asyncpg.connect(DB_DSN)
    try:
        await ensure_table(conn)
        rows = await conn.fetch(
            """
            SELECT summary, key_topics, message_count, ended_at
            FROM chatroom_sessions
            WHERE channel = $1
            ORDER BY session_id DESC
            LIMIT $2
            """,
            channel, limit,
        )

        if not rows:
            return ""

        parts = ["Recent conversation context:"]
        for row in reversed(rows):  # chronological order
            ts = row["ended_at"].strftime("%Y-%m-%d %H:%M") if row["ended_at"] else "unknown"
            topics = ", ".join(row["key_topics"] or [])
            parts.append(
                f"- [{ts}] ({row['message_count']} msgs) {row['summary']}"
                + (f" Topics: {topics}" if topics else "")
            )

        return "\n".join(parts)

    finally:
        await conn.close()


# ── CLI Entry Point ──────────────────────────────────────────────────────────

async def main():
    """Run as standalone — check and summarize if needed."""
    log.info("Checking for unsummarized messages...")
    created = await maybe_summarize("general")
    if created:
        log.info("Session summary created")
    else:
        log.info("No summary needed (below threshold or no new messages)")


if __name__ == "__main__":
    asyncio.run(main())
