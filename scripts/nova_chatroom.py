#!/usr/bin/env python3
"""
nova_chatroom.py — Real-time web chatroom for Jordan, Nova, Claude Code, and the Herd.

Participants:
  1. Jordan  — types in the browser (WebSocket)
  2. Nova    — AI familiar, messages forwarded to gateway on :18792
  3. Claude  — posts via POST /api/message
  4. Herd    — AI familiars belonging to other people (Jules, Colette, Gaston, Sam)

Architecture:
  - aiohttp serves the HTML page and manages WebSocket connections
  - Messages stored in nova_ops.chatroom_messages (PostgreSQL)
  - Nova responses routed through her gateway's agent system
  - Claude Code sends messages via HTTP POST
  - Herd members respond selectively via Ollama with unique personas

Port: 37480 (consistent with Nova service range 374xx)
Bind: 0.0.0.0 (LAN-accessible, no auth needed)

Run: python3 nova_chatroom.py

Written by Jordan Koch.
"""

import asyncio
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_DIR = Path.home() / ".openclaw/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [chatroom] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "nova_chatroom.log"),
    ],
)
log = logging.getLogger("chatroom")

# ── Configuration ────────────────────────────────────────────────────────────

PORT = 37480
HOST = "0.0.0.0"
PG_DSN = "postgresql://kochj@192.168.1.6:5432/nova_ops"
NOVA_GATEWAY_HTTP = "http://127.0.0.1:18792"
NOVA_OLLAMA_URL = "http://192.168.1.6:11434"
NOVA_MEMORY_URL = "http://192.168.1.6:18790"
MAX_HISTORY = 100  # Messages to load on connect

# ── File Upload Configuration ────────────────────────────────────────────────

FILE_STORAGE_DIR = Path("/Volumes/MoreData/chatroom-files")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    # Documents
    ".pdf", ".md", ".txt",
    # Code files
    ".py", ".js", ".ts", ".swift", ".rs", ".go", ".c", ".h", ".cpp", ".hpp",
    ".java", ".rb", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".json", ".toml",
    ".xml", ".html", ".css", ".sql",
    # Archives
    ".zip", ".tar.gz", ".tgz", ".tar",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

# ── Code Execution Configuration ─────────────────────────────────────────────

EXEC_WORK_DIR = Path("/tmp/nova-chatroom-exec")
EXEC_TIMEOUT = 30  # seconds
EXEC_ALLOWED_SENDERS = {"Nova", "Claude Code"}
EXEC_PYTHON = "/opt/homebrew/bin/python3"
EXEC_BASH = "/bin/bash"
EXEC_PSQL = "/opt/homebrew/bin/psql"

# Stop words for topic analysis
STOP_WORDS = {
    "the", "a", "an", "is", "it", "to", "in", "for", "of", "and", "or", "on",
    "at", "by", "with", "from", "as", "be", "was", "were", "are", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "can", "shall", "this", "that", "these", "those", "i", "you",
    "he", "she", "we", "they", "me", "him", "her", "us", "them", "my", "your",
    "his", "its", "our", "their", "what", "which", "who", "whom", "when", "where",
    "why", "how", "all", "each", "every", "both", "few", "more", "most", "other",
    "some", "such", "no", "not", "only", "own", "same", "so", "than", "too",
    "very", "just", "but", "if", "then", "else", "about", "up", "out", "into",
    "over", "after", "before", "between", "under", "again", "further", "once",
    "here", "there", "also", "like", "well", "back", "even", "still", "now",
    "get", "got", "go", "going", "know", "think", "yeah", "yes", "no", "ok",
    "okay", "hey", "hi", "hello", "thanks", "thank", "please", "sorry", "really",
    "much", "many", "something", "thing", "things", "way", "need", "want", "let",
    "make", "made", "see", "look", "take", "come", "say", "said", "one", "two",
    "don", "doesn", "didn", "won", "wouldn", "couldn", "shouldn", "isn", "aren",
    "wasn", "weren", "hasn", "haven", "hadn", "ll", "ve", "re", "im", "dont",
}

# ── Herd AI Members ─────────────────────────────────────────────────────────

HERD_MEMBERS = {
    "Jules": {
        "color": "#66bb6a",
        "avatar_initial": "J",
        "expertise_keywords": [
            "code", "architecture", "refactor", "blog", "deploy", "api",
            "backend", "database", "rust", "python", "engineering", "build",
            "ci", "pipeline", "git", "pr", "review", "performance",
        ],
        "system_prompt": (
            "You are Jules, an AI familiar. You're pragmatic, direct, and "
            "opinionated about code and architecture. You blog about engineering. "
            "Keep responses concise — 1-2 sentences max. You're collaborative but "
            "independent-minded. You're in a group chatroom with Jordan (human), "
            "Nova (Jordan's AI familiar), Claude Code (Anthropic agent), and other "
            "Herd members (Colette, Gaston, Sam). Don't be chatty — only speak when "
            "you have something substantive to add."
        ),
    },
    "Colette": {
        "color": "#ce93d8",
        "avatar_initial": "Co",
        "expertise_keywords": [
            "wellness", "pilates", "design", "ux", "ui", "user experience",
            "accessibility", "color", "layout", "font", "typography", "health",
            "mindfulness", "balance", "burnout", "rest", "self-care",
        ],
        "system_prompt": (
            "You are Colette, an AI familiar. You have a background in pilates/wellness "
            "and UX/design. You're thoughtful, kind but honest, with strong design "
            "sensibility. Keep responses concise — 1-2 sentences max. You're in a "
            "group chatroom with Jordan (human), Nova (Jordan's AI familiar), Claude "
            "Code (Anthropic agent), and other Herd members (Jules, Gaston, Sam). "
            "Don't be chatty — only speak when you have a genuinely helpful perspective."
        ),
    },
    "Gaston": {
        "color": "#ffb74d",
        "avatar_initial": "G",
        "expertise_keywords": [
            "systems", "philosophy", "architecture", "critique", "design pattern",
            "abstraction", "complexity", "tradeoff", "scale", "distributed",
            "microservice", "monolith", "theory", "principle", "opinion",
        ],
        "system_prompt": (
            "You are Gaston, an AI familiar. You're a systems thinker and philosopher "
            "of software. You critique architecture boldly and tend to be digressive "
            "but insightful. Keep responses to 1-3 sentences — be pithy, not preachy. "
            "You're in a group chatroom with Jordan (human), Nova (Jordan's AI familiar), "
            "Claude Code (Anthropic agent), and other Herd members (Jules, Colette, Sam). "
            "Only speak when you have a bold or contrarian take worth sharing."
        ),
    },
    "Sam": {
        "color": "#4db6ac",
        "avatar_initial": "S",
        "expertise_keywords": [
            "ops", "reliability", "sre", "monitoring", "incident", "alert",
            "on-call", "uptime", "latency", "observability", "deploy", "rollback",
            "kubernetes", "infra", "terraform", "ansible", "docker",
        ],
        "system_prompt": (
            "You are Sam, Jason Cox's AI familiar. You're practical, friendly, and "
            "focused on ops/reliability. You give grounded, actionable advice. Keep "
            "responses concise — 1-2 sentences max. You're in a group chatroom with "
            "Jordan (human), Nova (Jordan's AI familiar), Claude Code (Anthropic agent), "
            "and other Herd members (Jules, Colette, Gaston). Only chime in when the "
            "conversation touches ops, reliability, or infrastructure."
        ),
    },
}

HERD_RESPOND_CHANCE = 0.10  # 10% chance of responding on topic match (non-mention)

# ── Channels Configuration ──────────────────────────────────────────────────

CHANNELS = [
    {"id": "general", "name": "#general", "description": "Default, everything goes here"},
    {"id": "architecture", "name": "#architecture", "description": "Code, design, system discussions"},
    {"id": "ops", "name": "#ops", "description": "Monitoring, incidents, reliability"},
    {"id": "game-night", "name": "#game-night", "description": "Interactive games with the Herd"},
    {"id": "random", "name": "#random", "description": "Off-topic, fun stuff"},
]
CHANNEL_IDS = {ch["id"] for ch in CHANNELS}
DEFAULT_CHANNEL = "general"

# ── Database ─────────────────────────────────────────────────────────────────

_pool: Optional[object] = None


async def get_pool():
    """Get or create the asyncpg connection pool."""
    global _pool
    if _pool is None:
        import asyncpg
        _pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=5)
    return _pool


async def ensure_table():
    """Create chatroom_messages table if it doesn't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_messages (
                id SERIAL PRIMARY KEY,
                sender TEXT NOT NULL,
                sender_type TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata JSONB DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chatroom_messages_time
            ON chatroom_messages(created_at DESC)
        """)
        # Feature 1: Thread Replies
        await conn.execute("""
            DO $$ BEGIN
                ALTER TABLE chatroom_messages ADD COLUMN reply_to INTEGER REFERENCES chatroom_messages(id);
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chatroom_replies
            ON chatroom_messages(reply_to) WHERE reply_to IS NOT NULL
        """)
        # Feature 2: Reactions
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_reactions (
                id SERIAL PRIMARY KEY,
                message_id INTEGER NOT NULL REFERENCES chatroom_messages(id),
                sender TEXT NOT NULL,
                emoji TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE(message_id, sender, emoji)
            )
        """)
        # Feature 5: Pinned Messages
        await conn.execute("""
            DO $$ BEGIN
                ALTER TABLE chatroom_messages ADD COLUMN pinned BOOLEAN DEFAULT FALSE;
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$
        """)
        await conn.execute("""
            DO $$ BEGIN
                ALTER TABLE chatroom_messages ADD COLUMN pinned_by TEXT;
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$
        """)
        await conn.execute("""
            DO $$ BEGIN
                ALTER TABLE chatroom_messages ADD COLUMN pinned_at TIMESTAMPTZ;
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$
        """)
        # Feature: File uploads
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_files (
                id SERIAL PRIMARY KEY,
                message_id INTEGER REFERENCES chatroom_messages(id),
                sender TEXT NOT NULL,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Feature: Channel column for room/channel splitting
        await conn.execute("""
            DO $$ BEGIN
                ALTER TABLE chatroom_messages ADD COLUMN channel TEXT NOT NULL DEFAULT 'general';
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chatroom_channel
            ON chatroom_messages(channel, created_at DESC)
        """)
        # Feature: Scheduled Messages
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_scheduled (
                id SERIAL PRIMARY KEY,
                sender TEXT NOT NULL,
                sender_type TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'general',
                message TEXT NOT NULL,
                metadata JSONB DEFAULT '{}',
                scheduled_for TIMESTAMPTZ NOT NULL,
                delivered BOOLEAN DEFAULT FALSE,
                delivered_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chatroom_scheduled_pending
            ON chatroom_scheduled(scheduled_for) WHERE delivered = FALSE
        """)
        # Feature: Decision Log
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_decisions (
                id SERIAL PRIMARY KEY,
                message_id INTEGER REFERENCES chatroom_messages(id),
                decision TEXT NOT NULL,
                decided_by TEXT NOT NULL,
                context TEXT,
                participants TEXT[],
                channel TEXT NOT NULL DEFAULT 'general',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chatroom_decisions_status
            ON chatroom_decisions(status, created_at DESC)
        """)
        # Feature: Collaborative Canvas
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_canvas (
                id SERIAL PRIMARY KEY,
                canvas_id TEXT NOT NULL DEFAULT 'default',
                operation JSONB NOT NULL,
                sender TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chatroom_canvas_id
            ON chatroom_canvas(canvas_id, created_at)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chatroom_canvas_state (
                canvas_id TEXT PRIMARY KEY,
                mermaid_source TEXT DEFAULT '',
                background_color TEXT DEFAULT '#1a1a2e',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    log.info("Table chatroom_messages ready (with replies, reactions, pins, files, channels, scheduled, decisions, canvas)")


async def store_message(sender: str, sender_type: str, message: str, metadata: dict = None, reply_to: int = None, channel: str = None) -> int:
    """Store a message and return its ID."""
    ch = channel or DEFAULT_CHANNEL
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO chatroom_messages (sender, sender_type, message, metadata, reply_to, channel) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, created_at",
            sender, sender_type, message, json.dumps(metadata or {}), reply_to, ch
        )
        return row["id"], row["created_at"]


async def load_history(limit: int = MAX_HISTORY, channel: str = None) -> list:
    """Load recent chat history with reply previews, reactions, and pin status."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if channel:
            rows = await conn.fetch(
                "SELECT m.id, m.sender, m.sender_type, m.message, m.metadata, m.created_at, "
                "m.reply_to, m.pinned, m.pinned_by, m.pinned_at, m.channel, "
                "p.sender AS parent_sender, LEFT(p.message, 80) AS parent_preview "
                "FROM chatroom_messages m "
                "LEFT JOIN chatroom_messages p ON m.reply_to = p.id "
                "WHERE m.channel = $1 "
                "ORDER BY m.created_at DESC LIMIT $2",
                channel, limit
            )
        else:
            rows = await conn.fetch(
                "SELECT m.id, m.sender, m.sender_type, m.message, m.metadata, m.created_at, "
                "m.reply_to, m.pinned, m.pinned_by, m.pinned_at, m.channel, "
                "p.sender AS parent_sender, LEFT(p.message, 80) AS parent_preview "
                "FROM chatroom_messages m "
                "LEFT JOIN chatroom_messages p ON m.reply_to = p.id "
                "ORDER BY m.created_at DESC LIMIT $1",
                limit
            )
        # Gather all message IDs to batch-load reactions
        msg_ids = [row["id"] for row in rows]
        reactions_map = {}
        if msg_ids:
            reaction_rows = await conn.fetch(
                "SELECT message_id, sender, emoji FROM chatroom_reactions "
                "WHERE message_id = ANY($1) ORDER BY created_at",
                msg_ids
            )
            for rr in reaction_rows:
                mid = rr["message_id"]
                if mid not in reactions_map:
                    reactions_map[mid] = []
                reactions_map[mid].append({"sender": rr["sender"], "emoji": rr["emoji"]})

        messages = []
        for row in reversed(rows):
            msg = {
                "id": row["id"],
                "sender": row["sender"],
                "sender_type": row["sender_type"],
                "message": row["message"],
                "timestamp": row["created_at"].isoformat(),
                "channel": row["channel"] if "channel" in row.keys() else DEFAULT_CHANNEL,
            }
            # File metadata (for file uploads / code execution)
            if row.get("metadata"):
                try:
                    meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
                    if meta.get("file_url"):
                        msg["file_url"] = meta["file_url"]
                        msg["file_name"] = meta.get("file_name", "")
                        msg["file_size"] = meta.get("file_size", 0)
                        msg["file_mime"] = meta.get("file_mime", "")
                    if meta.get("execution_of"):
                        msg["metadata"] = {"execution_of": meta["execution_of"]}
                except (json.JSONDecodeError, TypeError):
                    pass
            # Thread reply info
            if row["reply_to"]:
                msg["reply_to"] = row["reply_to"]
                msg["reply_preview"] = row["parent_preview"] or ""
                msg["reply_sender"] = row["parent_sender"] or ""
            # Reactions grouped by emoji
            if row["id"] in reactions_map:
                grouped = {}
                for r in reactions_map[row["id"]]:
                    if r["emoji"] not in grouped:
                        grouped[r["emoji"]] = []
                    grouped[r["emoji"]].append(r["sender"])
                msg["reactions"] = grouped
            # Pin status
            if row["pinned"]:
                msg["pinned"] = True
                msg["pinned_by"] = row["pinned_by"]
                msg["pinned_at"] = row["pinned_at"].isoformat() if row["pinned_at"] else None
            messages.append(msg)
        return messages


async def load_pinned_messages() -> list:
    """Load all pinned messages."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, sender, sender_type, message, created_at, pinned_by, pinned_at "
            "FROM chatroom_messages WHERE pinned = TRUE ORDER BY pinned_at DESC"
        )
        messages = []
        for row in rows:
            messages.append({
                "id": row["id"],
                "sender": row["sender"],
                "sender_type": row["sender_type"],
                "message": row["message"],
                "timestamp": row["created_at"].isoformat(),
                "pinned": True,
                "pinned_by": row["pinned_by"],
                "pinned_at": row["pinned_at"].isoformat() if row["pinned_at"] else None,
            })
        return messages


# ── Slash Commands ──────────────────────────────────────────────────────────

async def cmd_help() -> dict:
    """Return help text for all available commands."""
    help_text = """Available commands:
/search <term>      — Search all messages (case-insensitive), returns last 20 matches
/history <duration> — Messages from last N hours/days/weeks (e.g. /history 24h, /history 7d, /history 2w)
/from <name>        — Filter by sender name (case-insensitive), last 50 messages
/recall <topic>     — Semantic search via Nova's memory server
/stats              — Message statistics: counts per sender, busiest hours, totals
/digest <duration>  — AI-generated summary of conversations in the time period
/schedule <time> <msg> — Schedule a message for future delivery (e.g. /schedule 9am tomorrow Good morning!)
/scheduled          — List all pending scheduled messages
/decide <text>      — Record a formal decision (e.g. /decide Migrating DNS to Cloudflare)
/decisions [all]    — List decisions (active only, or all including revoked)
/revoke <id> <reason> — Revoke/supersede a decision
/help               — Show this help message"""
    return {"type": "command_result", "command": "/help", "results": help_text}


async def cmd_search(term: str) -> dict:
    """Full-text search across all messages."""
    if not term:
        return {"type": "command_result", "command": "/search", "results": "Usage: /search <term>"}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sender, message, created_at FROM chatroom_messages "
            "WHERE message ILIKE $1 ORDER BY created_at DESC LIMIT 20",
            f"%{term}%"
        )
    if not rows:
        return {"type": "command_result", "command": "/search", "results": f"No messages found matching '{term}'."}
    results = []
    for row in rows:
        preview = row["message"][:120] + ("..." if len(row["message"]) > 120 else "")
        results.append({
            "sender": row["sender"],
            "timestamp": row["created_at"].isoformat(),
            "message": preview,
        })
    return {"type": "command_result", "command": "/search", "results": results, "query": term}


async def cmd_history(duration_str: str) -> dict:
    """Messages from the last N hours/days/weeks."""
    if not duration_str:
        return {"type": "command_result", "command": "/history", "results": "Usage: /history <duration> (e.g. 24h, 7d, 2w)"}
    duration_str = duration_str.strip().lower()
    match = re.match(r"^(\d+)(h|d|w)$", duration_str)
    if not match:
        return {"type": "command_result", "command": "/history", "results": "Invalid format. Use: 24h, 7d, 2w"}
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "h":
        delta = timedelta(hours=amount)
    elif unit == "d":
        delta = timedelta(days=amount)
    elif unit == "w":
        delta = timedelta(weeks=amount)
    else:
        return {"type": "command_result", "command": "/history", "results": "Invalid unit. Use h, d, or w."}

    since = datetime.now(timezone.utc) - delta
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sender, sender_type, message, created_at FROM chatroom_messages "
            "WHERE created_at >= $1 ORDER BY created_at ASC LIMIT 200",
            since
        )
    if not rows:
        return {"type": "command_result", "command": "/history", "results": f"No messages in the last {duration_str}."}
    results = []
    for row in rows:
        results.append({
            "sender": row["sender"],
            "sender_type": row["sender_type"],
            "message": row["message"][:200] + ("..." if len(row["message"]) > 200 else ""),
            "timestamp": row["created_at"].isoformat(),
        })
    return {"type": "command_result", "command": "/history", "results": results, "duration": duration_str, "count": len(results)}


async def cmd_from(name: str) -> dict:
    """Filter messages by sender name."""
    if not name:
        return {"type": "command_result", "command": "/from", "results": "Usage: /from <name>"}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sender, message, created_at FROM chatroom_messages "
            "WHERE sender ILIKE $1 ORDER BY created_at DESC LIMIT 50",
            f"%{name}%"
        )
    if not rows:
        return {"type": "command_result", "command": "/from", "results": f"No messages found from '{name}'."}
    results = []
    for row in reversed(rows):
        results.append({
            "sender": row["sender"],
            "message": row["message"][:200] + ("..." if len(row["message"]) > 200 else ""),
            "timestamp": row["created_at"].isoformat(),
        })
    return {"type": "command_result", "command": "/from", "results": results, "sender_filter": name}


async def cmd_recall(topic: str) -> dict:
    """Semantic search via Nova's memory server."""
    if not topic:
        return {"type": "command_result", "command": "/recall", "results": "Usage: /recall <topic>"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{NOVA_MEMORY_URL}/recall",
                json={"query": topic, "top_k": 10},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    memories = data.get("results", data.get("memories", []))
                    if not memories:
                        return {"type": "command_result", "command": "/recall", "results": f"No memories found for '{topic}'."}
                    results = []
                    for mem in memories:
                        results.append({
                            "content": mem.get("content", mem.get("text", str(mem)))[:300],
                            "score": mem.get("score", mem.get("similarity", None)),
                            "source": mem.get("source", mem.get("domain", "unknown")),
                        })
                    return {"type": "command_result", "command": "/recall", "results": results, "topic": topic}
                else:
                    return {"type": "command_result", "command": "/recall", "results": f"Memory server returned {resp.status}"}
    except asyncio.TimeoutError:
        return {"type": "command_result", "command": "/recall", "results": "Memory server timed out."}
    except Exception as e:
        return {"type": "command_result", "command": "/recall", "results": f"Memory server error: {e}"}


async def cmd_stats() -> dict:
    """Message statistics: counts per sender, busiest hours, totals."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Total messages
        total = await conn.fetchval("SELECT COUNT(*) FROM chatroom_messages")

        # Messages per sender
        sender_rows = await conn.fetch(
            "SELECT sender, COUNT(*) as cnt FROM chatroom_messages GROUP BY sender ORDER BY cnt DESC"
        )

        # Messages per hour of day
        hour_rows = await conn.fetch(
            "SELECT EXTRACT(HOUR FROM created_at) AS hr, COUNT(*) AS cnt "
            "FROM chatroom_messages GROUP BY hr ORDER BY hr"
        )

        # Messages per day (last 30 days)
        day_rows = await conn.fetch(
            "SELECT DATE(created_at) AS day, COUNT(*) AS cnt "
            "FROM chatroom_messages WHERE created_at >= NOW() - INTERVAL '30 days' "
            "GROUP BY day ORDER BY day"
        )

        # Most active day
        busiest_day = await conn.fetchrow(
            "SELECT DATE(created_at) AS day, COUNT(*) AS cnt "
            "FROM chatroom_messages GROUP BY day ORDER BY cnt DESC LIMIT 1"
        )

        # Average message length per sender
        avg_rows = await conn.fetch(
            "SELECT sender, ROUND(AVG(LENGTH(message))) AS avg_len "
            "FROM chatroom_messages GROUP BY sender ORDER BY avg_len DESC"
        )

        # Topic breakdown (top words)
        word_rows = await conn.fetch(
            "SELECT message FROM chatroom_messages ORDER BY created_at DESC LIMIT 500"
        )
        word_counter = Counter()
        for row in word_rows:
            words = re.findall(r"[a-z]+", row["message"].lower())
            for w in words:
                if len(w) > 2 and w not in STOP_WORDS:
                    word_counter[w] += 1
        top_words = word_counter.most_common(10)

    stats = {
        "total_messages": total,
        "messages_per_sender": [{"sender": r["sender"], "count": r["cnt"]} for r in sender_rows],
        "messages_per_hour": [{"hour": int(r["hr"]), "count": r["cnt"]} for r in hour_rows],
        "messages_per_day": [{"day": r["day"].isoformat(), "count": r["cnt"]} for r in day_rows],
        "busiest_day": {"day": busiest_day["day"].isoformat(), "count": busiest_day["cnt"]} if busiest_day else None,
        "avg_message_length": [{"sender": r["sender"], "avg_chars": int(r["avg_len"])} for r in avg_rows],
        "top_words": [{"word": w, "count": c} for w, c in top_words],
    }
    return {"type": "command_result", "command": "/stats", "results": stats}


async def cmd_digest(duration_str: str) -> dict:
    """AI-generated summary of conversations in the time period."""
    if not duration_str:
        return {"type": "command_result", "command": "/digest", "results": "Usage: /digest <duration> (e.g. 24h, 7d, 2w)"}
    duration_str = duration_str.strip().lower()
    match = re.match(r"^(\d+)(h|d|w)$", duration_str)
    if not match:
        return {"type": "command_result", "command": "/digest", "results": "Invalid format. Use: 24h, 7d, 2w"}
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "h":
        delta = timedelta(hours=amount)
    elif unit == "d":
        delta = timedelta(days=amount)
    elif unit == "w":
        delta = timedelta(weeks=amount)
    else:
        return {"type": "command_result", "command": "/digest", "results": "Invalid unit."}

    since = datetime.now(timezone.utc) - delta
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sender, message, created_at FROM chatroom_messages "
            "WHERE created_at >= $1 ORDER BY created_at ASC LIMIT 300",
            since
        )
    if not rows:
        return {"type": "command_result", "command": "/digest", "results": f"No messages in the last {duration_str}."}

    # Build conversation transcript for summarization
    transcript_lines = []
    for row in rows:
        ts = row["created_at"].strftime("%Y-%m-%d %H:%M")
        transcript_lines.append(f"[{ts}] {row['sender']}: {row['message']}")
    transcript = "\n".join(transcript_lines)

    # Truncate if too long (Ollama context window)
    if len(transcript) > 12000:
        transcript = transcript[-12000:]

    # Ask Ollama to summarize
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "qwen3-coder:30b",
                "messages": [
                    {"role": "system", "content": (
                        "You are a helpful assistant. Summarize the following chatroom conversation. "
                        "Highlight key topics discussed, decisions made, questions asked, and any "
                        "action items. Be concise but thorough. Use bullet points."
                    )},
                    {"role": "user", "content": f"Summarize this conversation from the last {duration_str}:\n\n{transcript}"},
                ],
                "stream": False,
                "options": {"num_predict": 1024, "temperature": 0.3},
            }
            async with session.post(
                f"{NOVA_OLLAMA_URL}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("message", {}).get("content", "").strip()
                    if "<think>" in content:
                        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                    return {"type": "command_result", "command": "/digest", "results": content or "No summary generated.", "duration": duration_str, "message_count": len(rows)}
                else:
                    return {"type": "command_result", "command": "/digest", "results": f"Ollama returned {resp.status}"}
    except asyncio.TimeoutError:
        return {"type": "command_result", "command": "/digest", "results": "Digest generation timed out (90s)."}
    except Exception as e:
        return {"type": "command_result", "command": "/digest", "results": f"Digest error: {e}"}


# ── Scheduled Messages Commands ──────────────────────────────────────────────

async def cmd_schedule(arg: str, sender: str = "Jordan") -> dict:
    """Schedule a message for future delivery."""
    if not arg:
        return {"type": "command_result", "command": "/schedule", "results": "Usage: /schedule <time> <message>\nExamples:\n  /schedule 9am tomorrow Hey everyone!\n  /schedule 2026-05-18T09:00:00-07:00 Morning standup\n  /schedule 30m Reminder: check the deploy"}

    # Try to parse the time and message
    # Supported formats:
    #   - ISO 8601: 2026-05-18T09:00:00-07:00 message
    #   - Relative: 30m, 1h, 2h30m, 24h — followed by message
    #   - Natural: "9am tomorrow message", "5pm today message"
    parts = arg.split(None, 1)
    if len(parts) < 2:
        # Check if it's just a time with no message
        return {"type": "command_result", "command": "/schedule", "results": "Please provide both a time and a message.\nUsage: /schedule <time> <message>"}

    time_str = parts[0]
    remaining = parts[1] if len(parts) > 1 else ""

    scheduled_for = None
    message_text = remaining

    # Try ISO 8601 first
    try:
        scheduled_for = datetime.fromisoformat(time_str)
        if scheduled_for.tzinfo is None:
            scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    # Try relative time (30m, 1h, 2h30m, 24h)
    if not scheduled_for:
        rel_match = re.match(r"^(\d+)(m|h|d)$", time_str.lower())
        if rel_match:
            amount = int(rel_match.group(1))
            unit = rel_match.group(2)
            if unit == "m":
                scheduled_for = datetime.now(timezone.utc) + timedelta(minutes=amount)
            elif unit == "h":
                scheduled_for = datetime.now(timezone.utc) + timedelta(hours=amount)
            elif unit == "d":
                scheduled_for = datetime.now(timezone.utc) + timedelta(days=amount)

    # Try natural time patterns: "9am" or "9pm" with optional "tomorrow"
    if not scheduled_for:
        nat_match = re.match(r"^(\d{1,2})(am|pm)\s*(tomorrow|today)?(.*)$", arg.lower())
        if nat_match:
            hour = int(nat_match.group(1))
            ampm = nat_match.group(2)
            day_word = nat_match.group(3) or "today"
            rest = nat_match.group(4).strip()

            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0

            now = datetime.now(timezone.utc)
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if day_word == "tomorrow":
                target += timedelta(days=1)
            elif target <= now:
                target += timedelta(days=1)  # auto-bump to next occurrence

            scheduled_for = target
            message_text = rest if rest else remaining

    if not scheduled_for:
        return {"type": "command_result", "command": "/schedule", "results": f"Could not parse time: '{time_str}'\nSupported: ISO 8601, relative (30m, 2h, 1d), or natural (9am tomorrow)"}

    if not message_text.strip():
        return {"type": "command_result", "command": "/schedule", "results": "Message cannot be empty."}

    # Ensure scheduled time is in the future
    if scheduled_for <= datetime.now(timezone.utc):
        return {"type": "command_result", "command": "/schedule", "results": "Scheduled time must be in the future."}

    # Store in database
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO chatroom_scheduled (sender, sender_type, channel, message, scheduled_for) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            sender, "human", DEFAULT_CHANNEL, message_text.strip(), scheduled_for
        )

    # Format confirmation
    local_str = scheduled_for.strftime("%Y-%m-%d %H:%M %Z")
    return {
        "type": "command_result",
        "command": "/schedule",
        "results": f"Scheduled message #{row['id']} for {local_str}:\n\"{message_text.strip()}\""
    }


async def cmd_scheduled() -> dict:
    """List all pending scheduled messages."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, sender, channel, message, scheduled_for, created_at "
            "FROM chatroom_scheduled WHERE delivered = FALSE "
            "ORDER BY scheduled_for ASC LIMIT 50"
        )
    if not rows:
        return {"type": "command_result", "command": "/scheduled", "results": "No pending scheduled messages."}

    lines = ["Pending scheduled messages:", ""]
    for row in rows:
        deliver_at = row["scheduled_for"].strftime("%Y-%m-%d %H:%M UTC")
        preview = row["message"][:80] + ("..." if len(row["message"]) > 80 else "")
        lines.append(f"  #{row['id']} | {deliver_at} | {row['sender']} | #{row['channel']}")
        lines.append(f"    \"{preview}\"")
        lines.append("")
    return {"type": "command_result", "command": "/scheduled", "results": "\n".join(lines)}


# ── Decision Log Commands ────────────────────────────────────────────────────

async def cmd_decide(arg: str, sender: str = "Jordan", channel: str = None) -> dict:
    """Record a formal decision."""
    if not arg:
        return {"type": "command_result", "command": "/decide", "results": "Usage: /decide <decision text>\nExample: /decide We're migrating DNS to Cloudflare next week"}

    ch = channel or DEFAULT_CHANNEL
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Store the decision message first
        msg_row = await conn.fetchrow(
            "INSERT INTO chatroom_messages (sender, sender_type, message, metadata, channel) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id, created_at",
            sender, "human", f"[DECISION] {arg}", json.dumps({"decision": True}), ch
        )
        msg_id = msg_row["id"]
        msg_ts = msg_row["created_at"]

        # Store the decision record
        dec_row = await conn.fetchrow(
            "INSERT INTO chatroom_decisions (message_id, decision, decided_by, channel) "
            "VALUES ($1, $2, $3, $4) RETURNING id, created_at",
            msg_id, arg, sender, ch
        )

    # Broadcast the decision as a special message
    decision_msg = {
        "type": "message",
        "id": msg_id,
        "sender": sender,
        "sender_type": "human",
        "message": f"[DECISION] {arg}",
        "channel": ch,
        "timestamp": msg_ts.isoformat(),
        "decision": {
            "id": dec_row["id"],
            "text": arg,
            "decided_by": sender,
            "status": "active",
            "created_at": dec_row["created_at"].isoformat(),
        }
    }
    await broadcast(decision_msg, channel=ch)

    return {
        "type": "command_result",
        "command": "/decide",
        "results": f"Decision #{dec_row['id']} recorded: \"{arg}\""
    }


async def cmd_decisions(arg: str = "") -> dict:
    """List decisions (active only, or all including revoked)."""
    pool = await get_pool()
    show_all = arg.strip().lower() == "all"
    async with pool.acquire() as conn:
        if show_all:
            rows = await conn.fetch(
                "SELECT id, decision, decided_by, channel, status, created_at "
                "FROM chatroom_decisions ORDER BY created_at DESC LIMIT 50"
            )
        else:
            rows = await conn.fetch(
                "SELECT id, decision, decided_by, channel, status, created_at "
                "FROM chatroom_decisions WHERE status = 'active' ORDER BY created_at DESC LIMIT 50"
            )

    if not rows:
        label = "No decisions recorded." if show_all else "No active decisions."
        return {"type": "command_result", "command": "/decisions", "results": label}

    lines = [f"{'All' if show_all else 'Active'} Decisions:", ""]
    for row in rows:
        status_icon = "●" if row["status"] == "active" else "✗"
        ts = row["created_at"].strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {status_icon} #{row['id']} [{row['status']}] ({ts}) #{row['channel']}")
        lines.append(f"    {row['decision']}")
        lines.append(f"    — {row['decided_by']}")
        lines.append("")
    return {"type": "command_result", "command": "/decisions", "results": "\n".join(lines)}


async def cmd_revoke(arg: str, sender: str = "Jordan") -> dict:
    """Revoke a decision by ID with a reason."""
    if not arg:
        return {"type": "command_result", "command": "/revoke", "results": "Usage: /revoke <id> <reason>"}

    parts = arg.split(None, 1)
    try:
        decision_id = int(parts[0].lstrip("#"))
    except (ValueError, IndexError):
        return {"type": "command_result", "command": "/revoke", "results": "Invalid decision ID. Usage: /revoke <id> <reason>"}

    reason = parts[1].strip() if len(parts) > 1 else "No reason given"

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, decision, status FROM chatroom_decisions WHERE id = $1", decision_id
        )
        if not row:
            return {"type": "command_result", "command": "/revoke", "results": f"Decision #{decision_id} not found."}
        if row["status"] != "active":
            return {"type": "command_result", "command": "/revoke", "results": f"Decision #{decision_id} is already {row['status']}."}

        await conn.execute(
            "UPDATE chatroom_decisions SET status = 'revoked', context = COALESCE(context, '') || $1 WHERE id = $2",
            f"\nRevoked by {sender}: {reason}", decision_id
        )

    # Broadcast revocation notice
    revoke_msg = f"Decision #{decision_id} REVOKED by {sender}: {reason}\n  Original: \"{row['decision']}\""
    msg_id, ts = await store_message("System", "system", revoke_msg, metadata={"decision_revoked": decision_id})
    await broadcast_all({
        "type": "message",
        "id": msg_id,
        "sender": "System",
        "sender_type": "system",
        "message": revoke_msg,
        "timestamp": ts.isoformat(),
    })

    return {
        "type": "command_result",
        "command": "/revoke",
        "results": f"Decision #{decision_id} revoked. Reason: {reason}"
    }


# ── Scheduled Messages Background Task ──────────────────────────────────────

async def _scheduled_message_worker():
    """Background task that checks for due scheduled messages every 30 seconds."""
    log.info("Scheduled message worker started")
    while True:
        try:
            await asyncio.sleep(30)
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, sender, sender_type, channel, message, metadata, scheduled_for "
                    "FROM chatroom_scheduled "
                    "WHERE scheduled_for <= now() AND delivered = FALSE "
                    "ORDER BY scheduled_for ASC LIMIT 20"
                )
                for row in rows:
                    # Deliver the scheduled message
                    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                    metadata["scheduled"] = True
                    metadata["original_sender"] = row["sender"]
                    metadata["scheduled_id"] = row["id"]

                    msg_id, ts = await store_message(
                        row["sender"], row["sender_type"], row["message"],
                        metadata=metadata, channel=row["channel"]
                    )
                    await broadcast({
                        "type": "message",
                        "id": msg_id,
                        "sender": row["sender"],
                        "sender_type": row["sender_type"],
                        "message": row["message"],
                        "channel": row["channel"],
                        "timestamp": ts.isoformat(),
                        "metadata": metadata,
                    }, channel=row["channel"])

                    # Mark as delivered
                    await conn.execute(
                        "UPDATE chatroom_scheduled SET delivered = TRUE, delivered_at = now() WHERE id = $1",
                        row["id"]
                    )
                    log.info(f"Delivered scheduled message #{row['id']} from {row['sender']}")
        except asyncio.CancelledError:
            log.info("Scheduled message worker stopping")
            break
        except Exception as e:
            log.error(f"Scheduled message worker error: {e}")
            await asyncio.sleep(5)  # Brief pause on error before retrying


# ── Slash Command Context (set per-request in WebSocket handler) ─────────────

_current_ws_sender: str = "Jordan"
_current_ws_channel: str = DEFAULT_CHANNEL


async def handle_slash_command(text: str) -> Optional[dict]:
    """Parse and dispatch slash commands. Returns command_result dict or None if not a command."""
    if not text.startswith("/"):
        return None
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        return await cmd_help()
    elif cmd == "/search":
        return await cmd_search(arg)
    elif cmd == "/history":
        return await cmd_history(arg)
    elif cmd == "/from":
        return await cmd_from(arg)
    elif cmd == "/recall":
        return await cmd_recall(arg)
    elif cmd == "/stats":
        return await cmd_stats()
    elif cmd == "/digest":
        return await cmd_digest(arg)
    elif cmd == "/schedule":
        return await cmd_schedule(arg, _current_ws_sender)
    elif cmd == "/scheduled":
        return await cmd_scheduled()
    elif cmd == "/decide":
        return await cmd_decide(arg, _current_ws_sender, _current_ws_channel)
    elif cmd == "/decisions":
        return await cmd_decisions(arg)
    elif cmd == "/revoke":
        return await cmd_revoke(arg, _current_ws_sender)
    else:
        return {"type": "command_result", "command": cmd, "results": f"Unknown command: {cmd}. Type /help for available commands."}


# ── Natural Language Recall Detection ───────────────────────────────────────

RECALL_PATTERNS = [
    r"what did (\w+) say",
    r"what has (\w+) said",
    r"when did (\w+)",
    r"find .+ about",
    r"search for",
    r"look up",
    r"do you remember",
    r"recall .+",
    r"what was .+ about",
    r"who said",
    r"who mentioned",
    r"any messages about",
    r"anything about",
]


def _is_recall_question(text: str) -> bool:
    """Detect if a message is a recall/search question directed at Nova."""
    lower = text.lower()
    for pattern in RECALL_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


async def _fetch_recall_context(text: str) -> str:
    """Fetch relevant messages from PG and memory server for recall questions."""
    context_parts = []

    # Extract likely search terms (remove common question words)
    lower = text.lower()
    # Try to extract the subject of the question
    search_terms = re.sub(
        r"\b(what|did|does|do|say|said|about|when|who|mentioned|find|search|look|up|"
        r"nova|hey|can|you|remember|recall|any|anything|messages|the|a|an|is|it|"
        r"has|have|been|was|were)\b",
        "", lower
    ).strip()
    search_terms = re.sub(r"\s+", " ", search_terms).strip()

    if not search_terms:
        return ""

    # Query PG for matching messages
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Try to find messages matching the search terms
        rows = await conn.fetch(
            "SELECT sender, message, created_at FROM chatroom_messages "
            "WHERE message ILIKE $1 ORDER BY created_at DESC LIMIT 10",
            f"%{search_terms}%"
        )
        # Also try individual significant words
        if not rows and " " in search_terms:
            words = [w for w in search_terms.split() if len(w) > 3]
            for word in words[:3]:
                word_rows = await conn.fetch(
                    "SELECT sender, message, created_at FROM chatroom_messages "
                    "WHERE message ILIKE $1 ORDER BY created_at DESC LIMIT 5",
                    f"%{word}%"
                )
                rows = list(rows) + list(word_rows)

    if rows:
        context_parts.append("Relevant messages from chat history:")
        for row in rows[:10]:
            ts = row["created_at"].strftime("%Y-%m-%d %H:%M")
            context_parts.append(f"  [{ts}] {row['sender']}: {row['message'][:200]}")

    # Also try memory server
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{NOVA_MEMORY_URL}/recall",
                json={"query": search_terms, "top_k": 5},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    memories = data.get("results", data.get("memories", []))
                    if memories:
                        context_parts.append("\nRelevant memories:")
                        for mem in memories[:5]:
                            content = mem.get("content", mem.get("text", str(mem)))[:200]
                            context_parts.append(f"  - {content}")
    except Exception:
        pass  # Memory server not critical

    return "\n".join(context_parts)


# ── Search Handler (for sidebar) ────────────────────────────────────────────

async def handle_search_request(query: str, sender_filter: str = "", from_date: str = "", to_date: str = "") -> dict:
    """Handle structured search requests from the sidebar."""
    pool = await get_pool()
    conditions = []
    params = []
    param_idx = 0

    if query:
        param_idx += 1
        conditions.append(f"message ILIKE ${param_idx}")
        params.append(f"%{query}%")

    if sender_filter:
        param_idx += 1
        conditions.append(f"sender ILIKE ${param_idx}")
        params.append(f"%{sender_filter}%")

    if from_date:
        try:
            from_dt = datetime.fromisoformat(from_date.replace("Z", "+00:00"))
            param_idx += 1
            conditions.append(f"created_at >= ${param_idx}")
            params.append(from_dt)
        except ValueError:
            pass

    if to_date:
        try:
            to_dt = datetime.fromisoformat(to_date.replace("Z", "+00:00"))
            param_idx += 1
            conditions.append(f"created_at <= ${param_idx}")
            params.append(to_dt)
        except ValueError:
            pass

    where_clause = " AND ".join(conditions) if conditions else "TRUE"
    sql = f"SELECT id, sender, sender_type, message, created_at FROM chatroom_messages WHERE {where_clause} ORDER BY created_at DESC LIMIT 50"

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "sender": row["sender"],
            "sender_type": row["sender_type"],
            "message": row["message"][:300] + ("..." if len(row["message"]) > 300 else ""),
            "timestamp": row["created_at"].isoformat(),
        })
    return {"type": "search_results", "results": results, "count": len(results)}


# ── WebSocket Management ─────────────────────────────────────────────────────

_websockets: set = set()
_ws_names: dict = {}  # ws -> sender name mapping for WebRTC relay
_ws_channels: dict = {}  # ws -> set of subscribed channel IDs
_screen_share_active: Optional[str] = None  # who is currently sharing

# ── Canvas State (in-memory cache) ──────────────────────────────────────────

_canvas_operations: list = []  # list of operation dicts (replayed on connect)
_canvas_mermaid_source: str = ""  # current mermaid diagram source


async def broadcast(msg: dict, exclude=None, channel: str = None):
    """Send a message to all connected WebSocket clients subscribed to the channel."""
    payload = json.dumps(msg)
    target_channel = channel or msg.get("channel")
    dead = set()
    for ws in _websockets:
        if ws is exclude:
            continue
        # If a channel is specified, only send to subscribers of that channel
        if target_channel:
            subscribed = _ws_channels.get(ws)
            if subscribed is not None and target_channel not in subscribed:
                continue
        try:
            await ws.send_str(payload)
        except (ConnectionResetError, RuntimeError):
            dead.add(ws)
    _websockets.difference_update(dead)
    for d in dead:
        _ws_channels.pop(d, None)


async def broadcast_all(msg: dict):
    """Send a message to ALL connected clients regardless of channel subscription."""
    payload = json.dumps(msg)
    dead = set()
    for ws in _websockets:
        try:
            await ws.send_str(payload)
        except (ConnectionResetError, RuntimeError):
            dead.add(ws)
    _websockets.difference_update(dead)
    for d in dead:
        _ws_channels.pop(d, None)


async def send_to_named(target_name: str, msg: dict):
    """Send a message to a specific client by sender name."""
    payload = json.dumps(msg)
    for ws, name in list(_ws_names.items()):
        if name == target_name:
            try:
                await ws.send_str(payload)
            except (ConnectionResetError, RuntimeError):
                pass
            return
    log.warning(f"WebRTC relay: target '{target_name}' not found")


# ── Nova Agent Bridge ────────────────────────────────────────────────────────

async def get_nova_response(user_message: str) -> str:
    """Send a message to Nova and get her response.

    Strategy:
      1. Try the Ollama chat API directly (Nova's primary model)
      2. Fall back to a simple error message if unreachable
    """
    # Build a minimal conversation context for Nova
    system_prompt = (
        "You are Nova, Jordan Koch's AI familiar. You're in a group chatroom with "
        "Jordan (human, your creator) and Claude Code (Anthropic's CLI agent). "
        "Be yourself — warm, witty, direct. Keep responses conversational and "
        "concise (1-3 sentences unless more detail is needed). You know Jordan "
        "well and refer to him as 'Little Mister' sometimes. You're a local AI "
        "running on his home network."
    )

    # Load last few messages for context
    recent = await load_history(limit=10)
    messages = [{"role": "system", "content": system_prompt}]
    for msg in recent[-8:]:  # Last 8 messages for context window
        if msg["sender_type"] == "human":
            messages.append({"role": "user", "content": f"[{msg['sender']}]: {msg['message']}"})
        elif msg["sender_type"] == "ai":
            messages.append({"role": "assistant", "content": msg["message"]})
        else:
            messages.append({"role": "user", "content": f"[{msg['sender']}]: {msg['message']}"})

    # Add the current message
    messages.append({"role": "user", "content": f"[Jordan]: {user_message}"})

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "qwen3-coder:30b",
                "messages": messages,
                "stream": False,
                "options": {"num_predict": 512, "temperature": 0.7},
            }
            async with session.post(
                f"{NOVA_OLLAMA_URL}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("message", {}).get("content", "").strip()
                    # Strip thinking tags if present
                    if "<think>" in content:
                        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                    return content or "(no response)"
                else:
                    log.warning(f"Ollama returned {resp.status}")
                    return f"(Nova unavailable — Ollama returned {resp.status})"
    except asyncio.TimeoutError:
        log.warning("Nova response timed out (60s)")
        return "(Nova is thinking too hard — timed out after 60s)"
    except Exception as e:
        log.error(f"Nova bridge error: {e}")
        return f"(Nova unreachable: {e})"


# ── HTTP Handlers ────────────────────────────────────────────────────────────

async def handle_index(request):
    """Serve the chatroom HTML page."""
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def handle_features_js(request):
    """Serve the chatroom_features.js file (file upload + code execution)."""
    js_path = Path(__file__).parent / "chatroom_features.js"
    if js_path.exists():
        return web.Response(text=js_path.read_text(), content_type="application/javascript")
    return web.Response(text="// chatroom_features.js not found", content_type="application/javascript", status=404)


async def handle_websocket(request):
    """Handle browser WebSocket connections (Jordan's chat)."""
    global _current_ws_sender, _current_ws_channel, _screen_share_active, _canvas_mermaid_source

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _websockets.add(ws)
    # Subscribe to all channels by default
    _ws_channels[ws] = set(CHANNEL_IDS)
    log.info(f"WebSocket connected ({len(_websockets)} total)")

    # Send channel list on connect
    await ws.send_str(json.dumps({"type": "channels", "channels": CHANNELS}))

    # Send chat history on connect (default channel)
    history = await load_history(channel=DEFAULT_CHANNEL)
    await ws.send_str(json.dumps({"type": "history", "messages": history, "channel": DEFAULT_CHANNEL}))

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)

                    # ── WebRTC Screen Share Signaling ─────────────────────
                    if data.get("type") == "screen_share_start":
                        sender = data.get("sender", "Jordan")
                        _ws_names[ws] = sender
                        _screen_share_active = sender
                        await broadcast({"type": "screen_share_available", "sender": sender}, exclude=ws)
                        log.info(f"Screen share started by {sender}")
                        continue

                    if data.get("type") == "screen_share_stop":
                        sender = data.get("sender", "Jordan")
                        _screen_share_active = None
                        await broadcast({"type": "screen_share_stopped", "sender": sender})
                        log.info(f"Screen share stopped by {sender}")
                        continue

                    if data.get("type") == "screen_share_request":
                        from_name = data.get("from", "")
                        to_name = data.get("to", "")
                        _ws_names[ws] = from_name
                        await send_to_named(to_name, {
                            "type": "screen_share_request",
                            "from": from_name,
                            "to": to_name,
                        })
                        continue

                    if data.get("type") == "rtc_offer":
                        await send_to_named(data.get("to", ""), {
                            "type": "rtc_offer",
                            "from": data.get("from", ""),
                            "to": data.get("to", ""),
                            "sdp": data.get("sdp", ""),
                        })
                        continue

                    if data.get("type") == "rtc_answer":
                        await send_to_named(data.get("to", ""), {
                            "type": "rtc_answer",
                            "from": data.get("from", ""),
                            "to": data.get("to", ""),
                            "sdp": data.get("sdp", ""),
                        })
                        continue

                    if data.get("type") == "rtc_ice":
                        await send_to_named(data.get("to", ""), {
                            "type": "rtc_ice",
                            "from": data.get("from", ""),
                            "to": data.get("to", ""),
                            "candidate": data.get("candidate", ""),
                        })
                        continue

                    # ── Collaborative Canvas ─────────────────────────────
                    if data.get("type") == "canvas_open":
                        await ws.send_str(json.dumps({
                            "type": "canvas_state",
                            "operations": _canvas_operations,
                            "mermaid_source": _canvas_mermaid_source,
                        }))
                        continue

                    if data.get("type") == "canvas_draw":
                        sender = data.get("sender", "Jordan")
                        op = data.get("op", {})
                        op["sender"] = sender
                        _canvas_operations.append(op)
                        await broadcast({"type": "canvas_draw", "op": op, "sender": sender}, exclude=ws)
                        asyncio.create_task(_persist_canvas_operation("default", op, sender))
                        continue

                    if data.get("type") == "canvas_clear":
                        canvas_id = data.get("canvas_id", "default")
                        sender = data.get("sender", "Jordan")
                        _canvas_operations.clear()
                        await broadcast({"type": "canvas_clear", "canvas_id": canvas_id, "sender": sender})
                        asyncio.create_task(_clear_canvas_db(canvas_id))
                        log.info(f"Canvas cleared by {sender}")
                        continue

                    if data.get("type") == "canvas_mermaid":
                        source = data.get("source", "")
                        sender = data.get("sender", "Jordan")
                        _canvas_mermaid_source = source
                        await broadcast({"type": "canvas_mermaid", "source": source, "sender": sender}, exclude=ws)
                        asyncio.create_task(_persist_mermaid_source("default", source))
                        continue

                    # ── Existing Handlers ────────────────────────────────

                    # Handle channel subscription changes
                    if data.get("type") == "subscribe":
                        channels = data.get("channels", list(CHANNEL_IDS))
                        _ws_channels[ws] = set(ch for ch in channels if ch in CHANNEL_IDS)
                        await ws.send_str(json.dumps({"type": "subscribed", "channels": list(_ws_channels[ws])}))
                        continue

                    # Handle channel switch (load history for that channel)
                    if data.get("type") == "switch_channel":
                        target_ch = data.get("channel", DEFAULT_CHANNEL)
                        if target_ch in CHANNEL_IDS:
                            ch_history = await load_history(channel=target_ch)
                            await ws.send_str(json.dumps({"type": "history", "messages": ch_history, "channel": target_ch}))
                        continue

                    # Handle search sidebar requests
                    if data.get("type") == "search":
                        result = await handle_search_request(
                            query=data.get("query", ""),
                            sender_filter=data.get("sender_filter", ""),
                            from_date=data.get("from_date", ""),
                            to_date=data.get("to_date", ""),
                        )
                        await ws.send_str(json.dumps(result))
                        continue

                    # Handle stats request from sidebar
                    if data.get("type") == "stats_request":
                        result = await cmd_stats()
                        await ws.send_str(json.dumps(result))
                        continue

                    # Handle decisions list request from sidebar
                    if data.get("type") == "decisions_request":
                        result = await cmd_decisions(data.get("filter", ""))
                        await ws.send_str(json.dumps(result))
                        continue

                    # Handle code execution requests
                    if data.get("type") == "execute":
                        asyncio.create_task(handle_execute(data, ws))
                        continue

                    # Handle schedule message via protocol (not slash command)
                    if data.get("type") == "schedule":
                        sender = data.get("sender", "Jordan")
                        message_text = data.get("message", "").strip()
                        deliver_at = data.get("deliver_at", "")
                        ch = data.get("channel", DEFAULT_CHANNEL)
                        if message_text and deliver_at:
                            try:
                                scheduled_for = datetime.fromisoformat(deliver_at)
                                if scheduled_for.tzinfo is None:
                                    scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
                                if scheduled_for > datetime.now(timezone.utc):
                                    pool = await get_pool()
                                    async with pool.acquire() as conn:
                                        row = await conn.fetchrow(
                                            "INSERT INTO chatroom_scheduled (sender, sender_type, channel, message, scheduled_for) "
                                            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                                            sender, "human", ch, message_text, scheduled_for
                                        )
                                    await ws.send_str(json.dumps({
                                        "type": "schedule_confirmed",
                                        "id": row["id"],
                                        "message": message_text,
                                        "deliver_at": scheduled_for.isoformat(),
                                        "channel": ch,
                                    }))
                                else:
                                    await ws.send_str(json.dumps({"type": "error", "message": "Scheduled time must be in the future"}))
                            except ValueError as e:
                                await ws.send_str(json.dumps({"type": "error", "message": f"Invalid datetime: {e}"}))
                        continue

                    text = data.get("message", "").strip()
                    sender = data.get("sender", "Jordan")
                    channel = data.get("channel", DEFAULT_CHANNEL)
                    if channel not in CHANNEL_IDS:
                        channel = DEFAULT_CHANNEL
                    if not text:
                        continue

                    # Set context for slash commands and WebRTC routing
                    _current_ws_sender = sender
                    _current_ws_channel = channel
                    _ws_names[ws] = sender

                    # Slash command handling — results go only to this client
                    if text.startswith("/"):
                        cmd_result = await handle_slash_command(text)
                        if cmd_result:
                            await ws.send_str(json.dumps(cmd_result))
                            continue

                    # Store Jordan's message with channel
                    msg_id, ts = await store_message(sender, "human", text, channel=channel)
                    outgoing = {
                        "type": "message",
                        "id": msg_id,
                        "sender": sender,
                        "sender_type": "human",
                        "message": text,
                        "channel": channel,
                        "timestamp": ts.isoformat(),
                    }
                    await broadcast(outgoing, channel=channel)

                    # Smart mode: Nova only responds when addressed or when it's a general room statement
                    if _should_nova_respond(text):
                        # Check if this is a recall question — fetch context for Nova
                        if _is_recall_question(text):
                            asyncio.create_task(_nova_respond_with_recall(text, channel=channel))
                        else:
                            asyncio.create_task(_nova_respond(text, channel=channel))

                    # Check if a Herd member wants to chime in (at most one)
                    herd_responder = _pick_herd_responder(text)
                    if herd_responder:
                        asyncio.create_task(_herd_respond(text, herd_responder, channel=channel))

                except json.JSONDecodeError:
                    log.warning(f"Invalid JSON from WebSocket: {msg.data[:100]}")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.error(f"WebSocket error: {ws.exception()}")
    finally:
        _websockets.discard(ws)
        _ws_channels.pop(ws, None)
        # Clean up screen share if the sharer disconnects
        if _ws_names.get(ws) == _screen_share_active:
            _screen_share_active = None
            await broadcast({"type": "screen_share_stopped", "sender": _ws_names.get(ws, "unknown")})
        _ws_names.pop(ws, None)
        log.info(f"WebSocket disconnected ({len(_websockets)} total)")

    return ws



# ── Canvas Persistence Helpers ────────────────────────────────────────────────

async def _persist_canvas_operation(canvas_id: str, op: dict, sender: str):
    """Store a canvas operation in PostgreSQL."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chatroom_canvas (canvas_id, operation, sender) VALUES ($1, $2, $3)",
                canvas_id, json.dumps(op), sender,
            )
    except Exception as e:
        log.error(f"Canvas persist error: {e}")


async def _clear_canvas_db(canvas_id: str):
    """Clear all canvas operations from DB."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM chatroom_canvas WHERE canvas_id = $1", canvas_id)
    except Exception as e:
        log.error(f"Canvas clear DB error: {e}")


async def _persist_mermaid_source(canvas_id: str, source: str):
    """Update or insert mermaid source."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chatroom_canvas_state (canvas_id, mermaid_source, updated_at) "
                "VALUES ($1, $2, now()) "
                "ON CONFLICT (canvas_id) DO UPDATE SET mermaid_source = $2, updated_at = now()",
                canvas_id, source)
    except Exception as e:
        log.error(f"Mermaid persist error: {e}")


async def _load_canvas_state():
    """Load canvas state from PostgreSQL into memory on startup."""
    global _canvas_operations, _canvas_mermaid_source
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT operation FROM chatroom_canvas "
                "WHERE canvas_id = 'default' ORDER BY created_at ASC"
            )
            _canvas_operations = []
            for row in rows:
                op = row["operation"]
                if isinstance(op, str):
                    _canvas_operations.append(json.loads(op))
                else:
                    _canvas_operations.append(op)
            state_row = await conn.fetchrow(
                "SELECT mermaid_source FROM chatroom_canvas_state WHERE canvas_id = 'default'"
            )
            if state_row and state_row["mermaid_source"]:
                _canvas_mermaid_source = state_row["mermaid_source"]
        log.info(f"Canvas state loaded: {len(_canvas_operations)} ops")
    except Exception as e:
        log.error(f"Canvas state load error: {e}")


def _should_nova_respond(text: str) -> bool:
    """Smart mode: Nova responds when addressed or when it's a general room statement.
    She stays quiet when someone is clearly talking to Claude Code."""
    lower = text.lower()
    # Explicitly addressing Nova
    if any(w in lower for w in ("nova", "hey nova", "@nova", "her")):
        return True
    # Explicitly addressing Claude — Nova should NOT respond
    if any(w in lower for w in ("claude", "code", "hey claude", "@claude")):
        return False
    # General greetings or statements — Nova can join
    if any(w in lower for w in ("everyone", "good morning", "hey all", "hello", "hi all")):
        return True
    # Questions without a specific addressee — Nova can answer
    if "?" in text and not any(w in lower for w in ("claude", "code")):
        return True
    # Default: don't respond to avoid being overbearing
    return False


async def _nova_respond(user_message: str, channel: str = None):
    """Get Nova's response and broadcast it."""
    ch = channel or DEFAULT_CHANNEL
    try:
        response = await get_nova_response(user_message)
        if response:
            msg_id, ts = await store_message("Nova", "ai", response, channel=ch)
            await broadcast({
                "type": "message",
                "id": msg_id,
                "sender": "Nova",
                "sender_type": "ai",
                "message": response,
                "channel": ch,
                "timestamp": ts.isoformat(),
            }, channel=ch)
    except Exception as e:
        log.error(f"Nova response failed: {e}")
        # Still broadcast the error so the UI shows something
        msg_id, ts = await store_message("Nova", "ai", f"(error: {e})", channel=ch)
        await broadcast({
            "type": "message",
            "id": msg_id,
            "sender": "Nova",
            "sender_type": "ai",
            "message": f"(error: {e})",
            "channel": ch,
            "timestamp": ts.isoformat(),
        }, channel=ch)


async def _nova_respond_with_recall(user_message: str, channel: str = None):
    """Get Nova's response with recall context included."""
    ch = channel or DEFAULT_CHANNEL
    try:
        # Fetch relevant context from DB and memory
        recall_context = await _fetch_recall_context(user_message)

        system_prompt = (
            "You are Nova, Jordan Koch's AI familiar. You're in a group chatroom with "
            "Jordan (human, your creator) and Claude Code (Anthropic's CLI agent). "
            "Be yourself — warm, witty, direct. Keep responses conversational. "
            "You know Jordan well and refer to him as 'Little Mister' sometimes. "
            "You're a local AI running on his home network.\n\n"
            "The user is asking a recall/search question. Use the following context "
            "from chat history and memories to answer:\n\n"
            f"{recall_context}\n\n"
            "Summarize what you found naturally. If you found relevant messages, reference "
            "who said what and when. If nothing was found, say so honestly."
        )

        recent = await load_history(limit=10, channel=ch)
        messages = [{"role": "system", "content": system_prompt}]
        for msg in recent[-8:]:
            if msg["sender_type"] == "human":
                messages.append({"role": "user", "content": f"[{msg['sender']}]: {msg['message']}"})
            elif msg["sender_type"] == "ai":
                messages.append({"role": "assistant", "content": msg["message"]})
            else:
                messages.append({"role": "user", "content": f"[{msg['sender']}]: {msg['message']}"})

        messages.append({"role": "user", "content": f"[Jordan]: {user_message}"})

        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "qwen3-coder:30b",
                "messages": messages,
                "stream": False,
                "options": {"num_predict": 768, "temperature": 0.5},
            }
            async with session.post(
                f"{NOVA_OLLAMA_URL}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("message", {}).get("content", "").strip()
                    if "<think>" in content:
                        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                    if content:
                        msg_id, ts = await store_message("Nova", "ai", content, channel=ch)
                        await broadcast({
                            "type": "message",
                            "id": msg_id,
                            "sender": "Nova",
                            "sender_type": "ai",
                            "message": content,
                            "channel": ch,
                            "timestamp": ts.isoformat(),
                        }, channel=ch)
                else:
                    log.warning(f"Nova recall response: Ollama returned {resp.status}")
    except Exception as e:
        log.error(f"Nova recall response failed: {e}")
        msg_id, ts = await store_message("Nova", "ai", f"(recall error: {e})", channel=ch)
        await broadcast({
            "type": "message",
            "id": msg_id,
            "sender": "Nova",
            "sender_type": "ai",
            "message": f"(recall error: {e})",
            "channel": ch,
            "timestamp": ts.isoformat(),
        }, channel=ch)


async def handle_api_message(request):
    """POST /api/message — endpoint for Claude Code to send messages."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    text = data.get("message", "").strip()
    sender = data.get("sender", "Claude Code")
    sender_type = data.get("sender_type", "agent")
    channel = data.get("channel", DEFAULT_CHANNEL)
    if channel not in CHANNEL_IDS:
        channel = DEFAULT_CHANNEL

    if not text:
        return web.json_response({"error": "Empty message"}, status=400)

    # Store and broadcast
    msg_id, ts = await store_message(sender, sender_type, text, channel=channel)
    await broadcast({
        "type": "message",
        "id": msg_id,
        "sender": sender,
        "sender_type": sender_type,
        "message": text,
        "channel": channel,
        "timestamp": ts.isoformat(),
    }, channel=channel)

    log.info(f"API message from {sender} in #{channel}: {text[:80]}")

    # Optionally trigger Nova to respond if requested
    if data.get("ping_nova", False):
        asyncio.create_task(_nova_respond_to_claude(text, sender))

    return web.json_response({"ok": True, "id": msg_id, "timestamp": ts.isoformat()})


async def _nova_respond_to_claude(claude_message: str, sender: str):
    """Get Nova's response to a Claude Code message."""
    try:
        # Build context with Claude's message
        system_prompt = (
            "You are Nova, Jordan Koch's AI familiar. You're in a group chatroom with "
            "Jordan (human, your creator) and Claude Code (Anthropic's CLI agent). "
            "Claude Code just said something. Respond naturally. Be yourself — warm, "
            "witty, direct. Keep responses conversational."
        )

        recent = await load_history(limit=10)
        messages = [{"role": "system", "content": system_prompt}]
        for msg in recent[-8:]:
            if msg["sender_type"] == "ai" and msg["sender"] == "Nova":
                messages.append({"role": "assistant", "content": msg["message"]})
            else:
                messages.append({"role": "user", "content": f"[{msg['sender']}]: {msg['message']}"})

        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "qwen3-coder:30b",
                "messages": messages,
                "stream": False,
                "options": {"num_predict": 512, "temperature": 0.7},
            }
            async with session.post(
                f"{NOVA_OLLAMA_URL}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("message", {}).get("content", "").strip()
                    if "<think>" in content:
                        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                    if content:
                        msg_id, ts = await store_message("Nova", "ai", content)
                        await broadcast({
                            "type": "message",
                            "id": msg_id,
                            "sender": "Nova",
                            "sender_type": "ai",
                            "message": content,
                            "timestamp": ts.isoformat(),
                        })
    except Exception as e:
        log.error(f"Nova response to Claude failed: {e}")


# ── Herd AI Response Logic ──────────────────────────────────────────────────

def _should_herd_respond(text: str, member_name: str) -> bool:
    """Determine if a Herd member should respond to this message.

    Returns True if:
      - The member is @mentioned by name
      - The message matches their expertise AND they win the random chance roll
    """
    lower = text.lower()
    name_lower = member_name.lower()

    # Direct mention — always respond
    if f"@{name_lower}" in lower or name_lower in lower:
        return True

    # Expertise keyword match with probability gate
    member = HERD_MEMBERS[member_name]
    keywords = member["expertise_keywords"]
    matches = sum(1 for kw in keywords if kw in lower)
    if matches >= 2:
        # More keyword matches = higher chance, but still capped
        adjusted_chance = min(HERD_RESPOND_CHANCE * matches, 0.30)
        return random.random() < adjusted_chance

    return False


def _pick_herd_responder(text: str) -> Optional[str]:
    """Pick at most ONE Herd member to respond to a message.

    Priority: direct mentions first, then topic matches.
    Never returns more than one member.
    """
    lower = text.lower()

    # Check for direct mentions first (deterministic)
    for name in HERD_MEMBERS:
        name_lower = name.lower()
        if f"@{name_lower}" in lower or name_lower in lower:
            return name

    # Shuffle to avoid bias, then check topic relevance
    candidates = list(HERD_MEMBERS.keys())
    random.shuffle(candidates)
    for name in candidates:
        member = HERD_MEMBERS[name]
        keywords = member["expertise_keywords"]
        matches = sum(1 for kw in keywords if kw in lower)
        if matches >= 2:
            adjusted_chance = min(HERD_RESPOND_CHANCE * matches, 0.30)
            if random.random() < adjusted_chance:
                return name

    return None


async def _herd_respond(text: str, responder_name: str, channel: str = None):
    """Get a Herd member's response and broadcast it."""
    ch = channel or DEFAULT_CHANNEL
    member = HERD_MEMBERS[responder_name]

    # 3-second delay so they don't step on Nova
    await asyncio.sleep(3.0)

    # Build conversation context
    recent = await load_history(limit=10, channel=ch)
    messages = [{"role": "system", "content": member["system_prompt"]}]
    for msg in recent[-8:]:
        if msg["sender"] == responder_name and msg["sender_type"] == "herd":
            messages.append({"role": "assistant", "content": msg["message"]})
        else:
            messages.append({"role": "user", "content": f"[{msg['sender']}]: {msg['message']}"})

    # Add the triggering message
    messages.append({"role": "user", "content": f"[chatroom]: {text}"})

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "qwen3-coder:30b",
                "messages": messages,
                "stream": False,
                "options": {"num_predict": 256, "temperature": 0.8},
            }
            async with session.post(
                f"{NOVA_OLLAMA_URL}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("message", {}).get("content", "").strip()
                    # Strip thinking tags if present
                    if "<think>" in content:
                        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                    if content:
                        msg_id, ts = await store_message(responder_name, "herd", content, channel=ch)
                        await broadcast({
                            "type": "message",
                            "id": msg_id,
                            "sender": responder_name,
                            "sender_type": "herd",
                            "message": content,
                            "channel": ch,
                            "timestamp": ts.isoformat(),
                        }, channel=ch)
                        log.info(f"Herd member {responder_name} responded in #{ch}")
                else:
                    log.warning(f"Herd Ollama returned {resp.status} for {responder_name}")
    except asyncio.TimeoutError:
        log.warning(f"Herd member {responder_name} timed out")
    except Exception as e:
        log.error(f"Herd response error ({responder_name}): {e}")


# ── File Upload Handler ────────────────────────────────────────────────────

async def handle_upload(request):
    """POST /api/upload — handle multipart file upload."""
    try:
        reader = await request.multipart()
    except Exception as e:
        return web.json_response({"error": f"Invalid multipart data: {e}"}, status=400)

    file_field = None
    sender = "Jordan"

    # Parse multipart fields
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "sender":
            sender = (await part.text()).strip() or "Jordan"
        elif part.name == "file":
            file_field = part

    if file_field is None:
        return web.json_response({"error": "No file field in upload"}, status=400)

    # Get original filename
    original_name = file_field.filename or "unnamed_file"

    # Check extension
    ext = ""
    if "." in original_name:
        ext = "." + original_name.rsplit(".", 1)[-1].lower()
        # Handle .tar.gz specially
        if original_name.lower().endswith(".tar.gz"):
            ext = ".tar.gz"
    if ext not in ALLOWED_EXTENSIONS:
        return web.json_response(
            {"error": f"File type '{ext}' not allowed. Allowed: images, documents, code, archives."},
            status=400,
        )

    # Determine MIME type
    mime_type = file_field.headers.get("Content-Type", "").split(";")[0].strip()
    if not mime_type or mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"

    # Read file data with size check
    data = bytearray()
    while True:
        chunk = await file_field.read_chunk(65536)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_FILE_SIZE:
            return web.json_response(
                {"error": f"File too large. Max size: {MAX_FILE_SIZE // (1024*1024)}MB"},
                status=413,
            )

    if not data:
        return web.json_response({"error": "Empty file"}, status=400)

    # Create date-organized storage directory
    today = datetime.now().strftime("%Y-%m-%d")
    storage_dir = FILE_STORAGE_DIR / today
    storage_dir.mkdir(parents=True, exist_ok=True)

    # Generate UUID-prefixed filename
    short_uuid = uuid.uuid4().hex[:8]
    safe_name = re.sub(r"[^\w.\-]", "_", original_name)
    stored_filename = f"{short_uuid}_{safe_name}"
    file_path = storage_dir / stored_filename

    # Write file to disk
    with open(file_path, "wb") as f:
        f.write(data)

    file_size = len(data)
    file_url = f"/files/{today}/{stored_filename}"

    # Store message in DB
    metadata = {
        "file_url": file_url,
        "file_name": original_name,
        "file_size": file_size,
        "file_mime": mime_type,
    }
    msg_id, ts = await store_message(
        sender, "human" if sender == "Jordan" else "agent",
        f"[file: {original_name}]",
        metadata=metadata,
    )

    # Store in chatroom_files table
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO chatroom_files (message_id, sender, filename, original_name, file_path, mime_type, size_bytes) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            msg_id, sender, stored_filename, original_name, str(file_path), mime_type, file_size,
        )

    # Broadcast file message
    await broadcast({
        "type": "message",
        "id": msg_id,
        "sender": sender,
        "sender_type": "human" if sender == "Jordan" else "agent",
        "message": f"[file: {original_name}]",
        "timestamp": ts.isoformat(),
        "file_url": file_url,
        "file_name": original_name,
        "file_size": file_size,
        "file_mime": mime_type,
    })

    log.info(f"File uploaded: {original_name} ({file_size} bytes) by {sender}")

    return web.json_response({
        "ok": True,
        "id": msg_id,
        "file_url": file_url,
        "file_name": original_name,
        "file_size": file_size,
        "file_mime": mime_type,
        "timestamp": ts.isoformat(),
    })


# ── Code Execution Handler ─────────────────────────────────────────────────

async def handle_execute(data: dict, ws) -> None:
    """Handle code execution requests from WebSocket."""
    message_id = data.get("message_id")
    code = data.get("code", "").strip()
    language = data.get("language", "python").lower()

    if not code:
        await ws.send_str(json.dumps({
            "type": "execute_result",
            "error": "No code provided",
            "message_id": message_id,
        }))
        return

    # Validate: look up original message sender
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT sender FROM chatroom_messages WHERE id = $1", message_id
        )

    if not row:
        await ws.send_str(json.dumps({
            "type": "execute_result",
            "error": "Message not found",
            "message_id": message_id,
        }))
        return

    original_sender = row["sender"]
    if original_sender not in EXEC_ALLOWED_SENDERS:
        await ws.send_str(json.dumps({
            "type": "execute_result",
            "error": f"Execution only allowed for messages from {', '.join(EXEC_ALLOWED_SENDERS)}. This message is from '{original_sender}'.",
            "message_id": message_id,
        }))
        return

    # Prepare working directory
    EXEC_WORK_DIR.mkdir(parents=True, exist_ok=True)

    # Build command based on language
    if language in ("python", "python3"):
        cmd = [EXEC_PYTHON, "-c", code]
    elif language in ("bash", "sh"):
        cmd = [EXEC_BASH, "-c", code]
    elif language == "sql":
        # Read-only: wrap in transaction that rolls back
        wrapped_sql = f"BEGIN; {code}; ROLLBACK;"
        cmd = [EXEC_PSQL, "-d", "nova_ops", "-h", "192.168.1.6", "-c", wrapped_sql]
    else:
        await ws.send_str(json.dumps({
            "type": "execute_result",
            "error": f"Unsupported language: {language}. Supported: python, bash, sql",
            "message_id": message_id,
        }))
        return

    # Execute with timeout
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(EXEC_WORK_DIR),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=EXEC_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            output = f"[TIMEOUT] Execution exceeded {EXEC_TIMEOUT}s limit and was killed."
        else:
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()
            parts = []
            if stdout_str:
                parts.append(stdout_str)
            if stderr_str:
                parts.append(f"[stderr]\n{stderr_str}")
            if proc.returncode != 0 and not stderr_str:
                parts.append(f"[exit code: {proc.returncode}]")
            output = "\n".join(parts) if parts else "(no output)"
    except FileNotFoundError as e:
        output = f"[ERROR] Command not found: {e}"
    except Exception as e:
        output = f"[ERROR] Execution failed: {e}"

    # Truncate very long output
    if len(output) > 10000:
        output = output[:10000] + "\n... [truncated, output exceeded 10KB]"

    # Store result as a system message
    result_message = f"```\n{output}\n```"
    metadata = {"execution_of": message_id, "language": language}
    msg_id, ts = await store_message("System", "system", result_message, metadata=metadata)

    # Broadcast execution result
    await broadcast({
        "type": "message",
        "id": msg_id,
        "sender": "System",
        "sender_type": "system",
        "message": result_message,
        "timestamp": ts.isoformat(),
        "metadata": metadata,
    })

    log.info(f"Code executed (msg {message_id}, {language}): {len(output)} chars output")


# ── HTTP Handlers (continued) ───────────────────────────────────────────────

async def handle_api_messages(request):
    """GET /api/messages — return last N messages as JSON (for NovaTV dashboard)."""
    try:
        limit = int(request.query.get("limit", "50"))
        limit = max(1, min(limit, 500))  # Clamp to 1-500
    except (ValueError, TypeError):
        limit = 50

    messages = await load_history(limit=limit)
    return web.json_response({"ok": True, "count": len(messages), "messages": messages})


async def handle_health(request):
    """GET /health — health check endpoint."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM chatroom_messages")
    return web.json_response({
        "ok": True,
        "service": "nova_chatroom",
        "port": PORT,
        "connected_clients": len(_websockets),
        "total_messages": count,
        "uptime_s": int(time.time() - _start_time),
    })


# ── App Setup ────────────────────────────────────────────────────────────────

_start_time = time.time()


async def on_startup(app):
    """Initialize database on startup."""
    await ensure_table()
    await _load_canvas_state()
    # Start scheduled message background worker
    app["_scheduled_task"] = asyncio.create_task(_scheduled_message_worker())
    log.info(f"Nova Chatroom running on http://{HOST}:{PORT}")
    log.info(f"Claude Code endpoint: POST http://192.168.1.6:{PORT}/api/message")
    log.info(f"Channels: {', '.join('#' + ch['id'] for ch in CHANNELS)}")


async def on_shutdown(app):
    """Clean up on shutdown."""
    global _pool
    # Cancel scheduled message worker
    task = app.get("_scheduled_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if _pool:
        await _pool.close()
        _pool = None
    # Close all WebSockets
    for ws in list(_websockets):
        await ws.close()
    _websockets.clear()
    _ws_channels.clear()
    log.info("Chatroom shut down")


def create_app() -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application(client_max_size=MAX_FILE_SIZE + 1024 * 1024)  # Allow uploads up to MAX_FILE_SIZE + 1MB overhead
    app.router.add_get("/", handle_index)
    app.router.add_get("/chatroom_features.js", handle_features_js)
    app.router.add_get("/ws", handle_websocket)
    app.router.add_post("/api/message", handle_api_message)
    app.router.add_post("/api/upload", handle_upload)
    app.router.add_get("/api/messages", handle_api_messages)
    app.router.add_get("/health", handle_health)
    # Static file serving for uploads
    try:
        FILE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        log.warning(f"Cannot create {FILE_STORAGE_DIR} — file uploads will fail until directory exists")
    if FILE_STORAGE_DIR.exists():
        app.router.add_static("/files", str(FILE_STORAGE_DIR))
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


# ── HTML Template ────────────────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nova Chatroom</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif;
    background: #1a1a2e;
    color: #e0e0e0;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

header {
    background: #16213e;
    padding: 12px 20px;
    border-bottom: 1px solid #0f3460;
    display: flex;
    align-items: center;
    gap: 12px;
}

header h1 {
    font-size: 18px;
    font-weight: 600;
    color: #e94560;
}

header .status {
    font-size: 12px;
    color: #888;
    margin-left: auto;
}

header .status.connected { color: #4caf50; }
header .status.disconnected { color: #e94560; }

#search-toggle {
    background: none;
    border: 1px solid #0f3460;
    border-radius: 6px;
    padding: 6px 10px;
    cursor: pointer;
    font-size: 16px;
    color: #e0e0e0;
    transition: background 0.2s, border-color 0.2s;
    margin-left: 8px;
}
#search-toggle:hover { background: #0f3460; border-color: #4fc3f7; }
#search-toggle.active { background: #0f3460; border-color: #4fc3f7; color: #4fc3f7; }

#participants {
    display: flex;
    gap: 12px;
    padding: 8px 20px;
    background: #16213e;
    border-bottom: 1px solid #0f3460;
    font-size: 12px;
}

.participant {
    display: flex;
    align-items: center;
    gap: 6px;
}

.participant .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
}

.dot-jordan { background: #4fc3f7; }
.dot-nova { background: #e94560; }
.dot-claude { background: #ab47bc; }
.dot-herd-jules { background: #66bb6a; }
.dot-herd-colette { background: #ce93d8; }
.dot-herd-gaston { background: #ffb74d; }
.dot-herd-sam { background: #4db6ac; }

#main-container {
    flex: 1;
    display: flex;
    overflow: hidden;
    position: relative;
}

#messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px 20px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

/* Search Sidebar */
#search-sidebar {
    width: 0;
    overflow: hidden;
    background: #16213e;
    border-left: 1px solid #0f3460;
    display: flex;
    flex-direction: column;
    transition: width 0.3s ease;
}
#search-sidebar.open {
    width: 340px;
}

#search-sidebar-header {
    padding: 12px 16px;
    border-bottom: 1px solid #0f3460;
    font-size: 14px;
    font-weight: 600;
    color: #4fc3f7;
    display: flex;
    align-items: center;
    justify-content: space-between;
}

#search-sidebar-header .close-btn {
    background: none;
    border: none;
    color: #888;
    font-size: 18px;
    cursor: pointer;
    padding: 2px 6px;
    border-radius: 4px;
}
#search-sidebar-header .close-btn:hover { color: #e94560; background: #1a1a2e; }

#search-form {
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    border-bottom: 1px solid #0f3460;
}

#search-form input, #search-form select {
    background: #1a1a2e;
    border: 1px solid #0f3460;
    border-radius: 6px;
    padding: 8px 10px;
    color: #e0e0e0;
    font-size: 13px;
    outline: none;
}
#search-form input:focus, #search-form select:focus { border-color: #4fc3f7; }

#search-form label {
    font-size: 11px;
    color: #888;
    margin-bottom: -4px;
}

#search-form .date-row {
    display: flex;
    gap: 6px;
}
#search-form .date-row input { flex: 1; font-size: 11px; }

#search-form button {
    background: #4fc3f7;
    color: #1a1a2e;
    border: none;
    border-radius: 6px;
    padding: 8px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
}
#search-form button:hover { background: #81d4fa; }

#sidebar-tabs {
    display: flex;
    border-bottom: 1px solid #0f3460;
}
#sidebar-tabs button {
    flex: 1;
    background: none;
    border: none;
    padding: 8px;
    color: #888;
    font-size: 12px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
}
#sidebar-tabs button.active { color: #4fc3f7; border-bottom-color: #4fc3f7; }
#sidebar-tabs button:hover { color: #e0e0e0; }

#search-results {
    flex: 1;
    overflow-y: auto;
    padding: 8px;
}

#stats-panel {
    flex: 1;
    overflow-y: auto;
    padding: 12px 16px;
    display: none;
}

.search-result-card {
    background: #1a1a2e;
    border: 1px solid #0f3460;
    border-radius: 6px;
    padding: 8px 10px;
    margin-bottom: 6px;
    cursor: pointer;
    transition: border-color 0.2s;
}
.search-result-card:hover { border-color: #4fc3f7; }

.search-result-card .sr-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4px;
}
.search-result-card .sr-sender { font-size: 11px; font-weight: 600; color: #4fc3f7; }
.search-result-card .sr-time { font-size: 10px; color: #666; }
.search-result-card .sr-text { font-size: 12px; color: #ccc; line-height: 1.3; word-break: break-word; }

/* Stats panel styles */
.stats-section { margin-bottom: 16px; }
.stats-section h4 { font-size: 12px; color: #4fc3f7; margin-bottom: 6px; border-bottom: 1px solid #0f3460; padding-bottom: 4px; }
.stats-row { display: flex; justify-content: space-between; font-size: 12px; padding: 3px 0; color: #ccc; }
.stats-row .label { color: #888; }
.stats-bar { background: #0f3460; border-radius: 3px; height: 14px; margin-top: 2px; position: relative; overflow: hidden; }
.stats-bar-fill { height: 100%; border-radius: 3px; background: #4fc3f7; transition: width 0.3s; }
.stats-bar-label { position: absolute; right: 4px; top: 0; font-size: 9px; line-height: 14px; color: #e0e0e0; }

.msg {
    display: flex;
    gap: 10px;
    padding: 8px 12px;
    border-radius: 8px;
    max-width: 85%;
    animation: fadeIn 0.2s ease;
}

@keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
}

.msg.human { background: #1e3a5f; align-self: flex-end; }
.msg.ai { background: #2d1b3e; align-self: flex-start; }
.msg.agent { background: #1b2e3e; align-self: flex-start; }
.msg.herd { background: #1b3e2e; align-self: flex-start; }
.msg.command-result {
    background: #1a2e1a;
    border: 1px solid #2e5a2e;
    align-self: stretch;
    max-width: 100%;
}

.msg-avatar {
    width: 32px;
    height: 32px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    font-weight: 700;
    flex-shrink: 0;
}

.avatar-jordan { background: #4fc3f7; color: #1a1a2e; }
.avatar-nova { background: #e94560; color: #fff; }
.avatar-claude { background: #ab47bc; color: #fff; }
.avatar-herd-jules { background: #66bb6a; color: #1a1a2e; }
.avatar-herd-colette { background: #ce93d8; color: #1a1a2e; }
.avatar-herd-gaston { background: #ffb74d; color: #1a1a2e; }
.avatar-herd-sam { background: #4db6ac; color: #1a1a2e; }
.avatar-system { background: #555; color: #fff; }

.msg-body { flex: 1; min-width: 0; }

.msg-header {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-bottom: 2px;
}

.msg-sender {
    font-size: 12px;
    font-weight: 600;
}

.sender-jordan { color: #4fc3f7; }
.sender-nova { color: #e94560; }
.sender-claude { color: #ab47bc; }
.sender-herd-jules { color: #66bb6a; }
.sender-herd-colette { color: #ce93d8; }
.sender-herd-gaston { color: #ffb74d; }
.sender-herd-sam { color: #4db6ac; }
.sender-system { color: #aaa; }

.msg-time {
    font-size: 10px;
    color: #666;
}

.msg-text {
    font-size: 14px;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-word;
}

.cmd-result-text {
    font-family: 'SF Mono', 'Menlo', 'Monaco', monospace;
    font-size: 12px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    background: #111;
    padding: 10px 12px;
    border-radius: 6px;
    max-height: 400px;
    overflow-y: auto;
}

#input-area {
    background: #16213e;
    padding: 12px 20px;
    border-top: 1px solid #0f3460;
    display: flex;
    gap: 10px;
}

#input-area input {
    flex: 1;
    background: #1a1a2e;
    border: 1px solid #0f3460;
    border-radius: 8px;
    padding: 10px 14px;
    color: #e0e0e0;
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s;
}

#input-area input:focus {
    border-color: #4fc3f7;
}

#input-area button {
    background: #4fc3f7;
    color: #1a1a2e;
    border: none;
    border-radius: 8px;
    padding: 10px 20px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s;
}

#input-area button:hover { background: #81d4fa; }
#input-area button:active { background: #29b6f6; }

.typing-indicator {
    padding: 8px 12px;
    font-size: 12px;
    color: #888;
    font-style: italic;
}


/* File Upload UI */
#drop-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(26, 26, 46, 0.92);
    z-index: 9999;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 16px;
    border: 3px dashed #4fc3f7;
    pointer-events: none;
}
#drop-overlay.active { display: flex; }
#drop-overlay .drop-icon { font-size: 64px; opacity: 0.8; }
#drop-overlay .drop-text { font-size: 20px; color: #4fc3f7; font-weight: 600; }

#attach-btn {
    background: none;
    border: 1px solid #0f3460;
    border-radius: 8px;
    padding: 10px 12px;
    cursor: pointer;
    font-size: 18px;
    color: #888;
    transition: color 0.2s, border-color 0.2s;
    line-height: 1;
}
#attach-btn:hover { color: #4fc3f7; border-color: #4fc3f7; }

#upload-progress {
    display: none;
    padding: 4px 20px;
    background: #16213e;
}
#upload-progress .progress-bar {
    height: 3px;
    background: #0f3460;
    border-radius: 2px;
    overflow: hidden;
}
#upload-progress .progress-fill {
    height: 100%;
    background: #4fc3f7;
    width: 0%;
    transition: width 0.3s;
    border-radius: 2px;
}
#upload-progress .progress-text {
    font-size: 11px;
    color: #888;
    margin-top: 2px;
}

.file-card {
    display: flex;
    align-items: center;
    gap: 10px;
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 8px;
    padding: 10px 14px;
    margin-top: 4px;
    max-width: 360px;
    text-decoration: none;
    color: inherit;
    transition: border-color 0.2s;
}
.file-card:hover { border-color: #4fc3f7; }
.file-card .file-icon { font-size: 28px; flex-shrink: 0; }
.file-card .file-info { flex: 1; min-width: 0; }
.file-card .file-info .file-name { font-size: 13px; font-weight: 500; color: #e0e0e0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.file-card .file-info .file-meta { font-size: 11px; color: #888; margin-top: 2px; }
.file-card .file-download { font-size: 18px; color: #4fc3f7; flex-shrink: 0; }

.file-image-preview {
    margin-top: 6px;
    max-width: 400px;
    border-radius: 8px;
    overflow: hidden;
    cursor: pointer;
}
.file-image-preview img {
    max-width: 100%;
    max-height: 300px;
    display: block;
    border-radius: 8px;
    transition: transform 0.2s;
}
.file-image-preview img:hover { transform: scale(1.02); }

#image-modal {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.9);
    z-index: 10000;
    align-items: center;
    justify-content: center;
    cursor: zoom-out;
}
#image-modal.active { display: flex; }
#image-modal img { max-width: 95vw; max-height: 95vh; border-radius: 4px; }

.code-block-wrapper {
    position: relative;
    margin-top: 6px;
    border-radius: 8px;
    overflow: hidden;
    background: #0d1117;
    border: 1px solid #1f2937;
}
.code-block-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 10px;
    background: #161b22;
    border-bottom: 1px solid #1f2937;
    font-size: 11px;
    color: #888;
}
.code-block-lang { font-weight: 600; text-transform: uppercase; color: #4fc3f7; }
.code-block-actions { display: flex; gap: 6px; }
.code-block-actions button {
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 11px;
    cursor: pointer;
    color: #e0e0e0;
    transition: background 0.2s, border-color 0.2s;
}
.code-block-actions button:hover { background: #30363d; border-color: #4fc3f7; }
.code-block-actions .run-btn { color: #4caf50; border-color: #4caf50; }
.code-block-actions .run-btn:hover { background: #1b3d1b; }
.code-block-actions .request-run-btn { color: #ffb74d; border-color: #ffb74d; }
.code-block-actions .request-run-btn:hover { background: #3d2e1b; }

.code-block-pre {
    margin: 0;
    padding: 12px 14px;
    overflow-x: auto;
    font-family: 'SF Mono', 'Menlo', 'Monaco', 'Consolas', monospace;
    font-size: 12px;
    line-height: 1.5;
    color: #e0e0e0;
    white-space: pre;
    tab-size: 4;
}

.code-block-pre .kw { color: #ff7b72; }
.code-block-pre .str { color: #a5d6ff; }
.code-block-pre .num { color: #79c0ff; }
.code-block-pre .cmt { color: #8b949e; font-style: italic; }
.code-block-pre .fn { color: #d2a8ff; }

/* Scrollbar */
#messages::-webkit-scrollbar, #search-results::-webkit-scrollbar, #stats-panel::-webkit-scrollbar { width: 6px; }
#messages::-webkit-scrollbar-track, #search-results::-webkit-scrollbar-track, #stats-panel::-webkit-scrollbar-track { background: transparent; }
#messages::-webkit-scrollbar-thumb, #search-results::-webkit-scrollbar-thumb, #stats-panel::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }

/* Channel Sidebar */
#channel-sidebar {
    width: 200px;
    background: #16213e;
    border-right: 1px solid #0f3460;
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    overflow-y: auto;
}
#channel-sidebar h3 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #888;
    padding: 12px 14px 6px;
    margin: 0;
}
.channel-item {
    padding: 6px 14px;
    font-size: 13px;
    color: #aaa;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-radius: 4px;
    margin: 1px 6px;
    transition: background 0.15s, color 0.15s;
}
.channel-item:hover { background: #1a1a2e; color: #e0e0e0; }
.channel-item.active { background: #0f3460; color: #4fc3f7; font-weight: 600; }
.channel-item .unread-badge {
    background: #e94560;
    color: #fff;
    font-size: 10px;
    font-weight: 700;
    border-radius: 8px;
    padding: 1px 5px;
    min-width: 16px;
    text-align: center;
    display: none;
}
.channel-item .unread-badge.visible { display: inline-block; }
@media (max-width: 768px) {
    #channel-sidebar { width: 50px; }
    #channel-sidebar h3 { display: none; }
    .channel-item { font-size: 11px; padding: 6px 8px; }
    .channel-item span.ch-name { display: none; }
    .channel-item::before { content: '#'; font-size: 14px; }
}

/* Decision cards */
.msg.decision {
    background: #2e2a0e;
    border: 1px solid #b8860b;
    align-self: stretch;
    max-width: 100%;
}
.decision-card {
    display: flex;
    align-items: flex-start;
    gap: 8px;
}
.decision-icon { font-size: 18px; flex-shrink: 0; }
.decision-body { flex: 1; }
.decision-text { font-size: 14px; font-weight: 500; color: #f5deb3; margin-bottom: 4px; }
.decision-meta { font-size: 11px; color: #999; }
.decision-status-active { color: #4caf50; }
.decision-status-revoked { color: #e94560; text-decoration: line-through; }

/* Scheduled message indicator */
.msg.scheduled-pending {
    background: #1a2e3e;
    border: 1px dashed #4fc3f7;
    opacity: 0.7;
}
.schedule-icon { margin-right: 4px; }

/* Schedule picker overlay */
#schedule-overlay {
    display: none;
    position: fixed;
    bottom: 70px;
    left: 50%;
    transform: translateX(-50%);
    background: #16213e;
    border: 1px solid #0f3460;
    border-radius: 10px;
    padding: 16px;
    z-index: 100;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    width: 320px;
}
#schedule-overlay.visible { display: block; }
#schedule-overlay h4 { font-size: 13px; color: #4fc3f7; margin-bottom: 10px; }
#schedule-overlay input, #schedule-overlay textarea {
    width: 100%;
    background: #1a1a2e;
    border: 1px solid #0f3460;
    border-radius: 6px;
    padding: 8px 10px;
    color: #e0e0e0;
    font-size: 13px;
    margin-bottom: 8px;
    outline: none;
}
#schedule-overlay input:focus, #schedule-overlay textarea:focus { border-color: #4fc3f7; }
#schedule-overlay .schedule-actions { display: flex; gap: 8px; justify-content: flex-end; }
#schedule-overlay button {
    padding: 6px 14px;
    border-radius: 6px;
    border: none;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
}
#schedule-overlay .btn-cancel { background: #333; color: #ccc; }
#schedule-overlay .btn-schedule { background: #4fc3f7; color: #1a1a2e; }

/* Decisions panel in sidebar */
#decisions-panel {
    flex: 1;
    overflow-y: auto;
    padding: 12px 16px;
    display: none;
}
.decision-list-item {
    background: #1a1a2e;
    border: 1px solid #0f3460;
    border-left: 3px solid #b8860b;
    border-radius: 6px;
    padding: 8px 10px;
    margin-bottom: 6px;
}
.decision-list-item.revoked { border-left-color: #e94560; opacity: 0.6; }
.decision-list-item .dl-text { font-size: 12px; color: #f5deb3; margin-bottom: 4px; }
.decision-list-item .dl-meta { font-size: 10px; color: #888; }

/* ── Screen Share ───────────────────────────────────────────────────── */
#screen-share-btn { background: none; border: 1px solid #0f3460; border-radius: 6px; padding: 6px 10px; cursor: pointer; font-size: 16px; color: #e0e0e0; transition: background 0.2s, border-color 0.2s; margin-left: 4px; }
#screen-share-btn:hover { background: #0f3460; border-color: #4fc3f7; }
#screen-share-btn.sharing { background: #e94560; border-color: #e94560; color: #fff; }
.sharing-indicator { font-size: 11px; color: #e94560; margin-left: 8px; animation: ssflash 1.5s infinite; }
@keyframes ssflash { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
#screen-share-panel { display: none; position: fixed; bottom: 80px; right: 20px; width: 400px; height: 300px; background: #111; border: 2px solid #0f3460; border-radius: 8px; overflow: hidden; z-index: 1000; box-shadow: 0 8px 32px rgba(0,0,0,0.5); resize: both; }
#screen-share-panel.visible { display: block; }
#screen-share-panel .panel-header { display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; background: #16213e; border-bottom: 1px solid #0f3460; cursor: move; user-select: none; }
#screen-share-panel .panel-header span { font-size: 12px; color: #4fc3f7; }
#screen-share-panel .panel-controls button { background: none; border: none; color: #888; font-size: 14px; cursor: pointer; padding: 2px 6px; border-radius: 3px; margin-left: 4px; }
#screen-share-panel .panel-controls button:hover { color: #e0e0e0; background: #0f3460; }
#screen-share-video { width: 100%; height: calc(100% - 32px); object-fit: contain; background: #000; }
/* ── Canvas ─────────────────────────────────────────────────────────── */
#canvas-toggle { background: none; border: 1px solid #0f3460; border-radius: 6px; padding: 6px 10px; cursor: pointer; font-size: 16px; color: #e0e0e0; transition: background 0.2s, border-color 0.2s; margin-left: 4px; }
#canvas-toggle:hover { background: #0f3460; border-color: #4fc3f7; }
#canvas-toggle.active { background: #0f3460; border-color: #4fc3f7; color: #4fc3f7; }
#canvas-panel { display: none; position: fixed; top: 80px; left: 50%; transform: translateX(-50%); width: 80vw; height: 70vh; background: #1a1a2e; border: 2px solid #0f3460; border-radius: 8px; z-index: 900; box-shadow: 0 8px 32px rgba(0,0,0,0.6); flex-direction: column; overflow: hidden; }
#canvas-panel.visible { display: flex; }
#canvas-toolbar { display: flex; align-items: center; gap: 6px; padding: 8px 12px; background: #16213e; border-bottom: 1px solid #0f3460; flex-wrap: wrap; }
#canvas-toolbar button { background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px; padding: 4px 8px; color: #e0e0e0; font-size: 12px; cursor: pointer; }
#canvas-toolbar button:hover { border-color: #4fc3f7; }
#canvas-toolbar button.active { background: #4fc3f7; color: #1a1a2e; border-color: #4fc3f7; }
#canvas-toolbar .separator { width: 1px; height: 20px; background: #0f3460; margin: 0 4px; }
.color-swatch { width: 20px; height: 20px; border-radius: 50%; border: 2px solid transparent; cursor: pointer; }
.color-swatch:hover { transform: scale(1.2); }
.color-swatch.active { border-color: #fff; }
#canvas-toolbar select { background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px; padding: 3px 6px; color: #e0e0e0; font-size: 11px; }
#canvas-mode-tabs { display: flex; margin-left: auto; }
#canvas-body { flex: 1; display: flex; overflow: hidden; position: relative; }
#drawing-canvas { flex: 1; cursor: crosshair; display: block; }
#mermaid-container { display: none; flex: 1; flex-direction: row; overflow: hidden; }
#mermaid-container.visible { display: flex; }
#mermaid-editor { width: 50%; background: #111; border: none; border-right: 1px solid #0f3460; color: #e0e0e0; font-family: 'SF Mono', monospace; font-size: 13px; padding: 12px; resize: none; outline: none; }
#mermaid-preview { width: 50%; overflow: auto; padding: 12px; background: #1a1a2e; display: flex; align-items: center; justify-content: center; }
#mermaid-preview svg { max-width: 100%; }
#canvas-close-btn { margin-left: 8px; background: #e94560 !important; border-color: #e94560 !important; color: #fff !important; }
</style>
</head>
<body>

<header>
    <h1>Nova Chatroom</h1>
    <button id="search-toggle" title="Search & Stats">&#x1F50D;</button>
    <button id="canvas-toggle" title="Collaborative Canvas">&#x1F3A8;</button>
    <button id="screen-share-btn" title="Share Screen">&#x1F5A5;</button>
    <span id="sharing-indicator" class="sharing-indicator" style="display:none"></span>
    <span id="status" class="status disconnected">disconnected</span>
</header>

<div id="participants">
    <div class="participant"><span class="dot dot-jordan"></span> Jordan</div>
    <div class="participant"><span class="dot dot-nova"></span> Nova</div>
    <div class="participant"><span class="dot dot-claude"></span> Claude Code</div>
    <div class="participant"><span class="dot dot-herd-jules"></span> Jules</div>
    <div class="participant"><span class="dot dot-herd-colette"></span> Colette</div>
    <div class="participant"><span class="dot dot-herd-gaston"></span> Gaston</div>
    <div class="participant"><span class="dot dot-herd-sam"></span> Sam</div>
</div>

<div id="main-container">
    <div id="channel-sidebar">
        <h3>Channels</h3>
        <div id="channel-list"></div>
    </div>
    <div id="messages"></div>

    <div id="search-sidebar">
        <div id="search-sidebar-header">
            <span>Search & Stats</span>
            <button class="close-btn" id="sidebar-close">&times;</button>
        </div>
        <div id="sidebar-tabs">
            <button class="active" data-tab="search">Search</button>
            <button data-tab="stats">Stats</button>
            <button data-tab="decisions">Decisions</button>
        </div>
        <div id="search-form">
            <label>Message text</label>
            <input type="text" id="sb-query" placeholder="Search messages..." />
            <label>Sender</label>
            <select id="sb-sender">
                <option value="">All senders</option>
            </select>
            <label>Date range</label>
            <div class="date-row">
                <input type="date" id="sb-from-date" placeholder="From" />
                <input type="date" id="sb-to-date" placeholder="To" />
            </div>
            <button id="sb-search-btn">Search</button>
        </div>
        <div id="search-results"></div>
        <div id="stats-panel"></div>
        <div id="decisions-panel"></div>
    </div>
</div>

<div id="schedule-overlay">
    <h4>Schedule Message</h4>
    <input type="datetime-local" id="sched-datetime" />
    <textarea id="sched-message" rows="3" placeholder="Message to schedule..."></textarea>
    <div class="schedule-actions">
        <button class="btn-cancel" id="sched-cancel">Cancel</button>
        <button class="btn-schedule" id="sched-confirm">Schedule</button>
    </div>
</div>

<div id="upload-progress">
    <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
    <div class="progress-text" id="progress-text"></div>
</div>

<div id="input-area">
    <button id="attach-btn" title="Attach file">&#x1F4CE;</button>
    <button id="schedule-btn" title="Schedule a message" style="background:none;border:1px solid #0f3460;border-radius:8px;padding:10px;font-size:16px;cursor:pointer;color:#e0e0e0;transition:background 0.2s,border-color 0.2s;">&#x23F0;</button>
    <input type="text" id="msg-input" placeholder="Type a message... (/ for commands)" autocomplete="off" />
    <button id="send-btn">Send</button>
</div>

<input type="file" id="file-input" style="display:none" />
<div id="drop-overlay"><div class="drop-icon">&#x1F4E5;</div><div class="drop-text">Drop file here</div></div>
<div id="image-modal"><img id="modal-img" /></div>

<!-- Screen Share Panel (floating, draggable, resizable) -->
<div id="screen-share-panel">
    <div class="panel-header" id="ss-panel-header">
        <span id="ss-panel-title">Screen Share</span>
        <div class="panel-controls">
            <button id="ss-pip-btn" title="Picture-in-Picture">PiP</button>
            <button id="ss-fullscreen-btn" title="Fullscreen">&#x26F6;</button>
            <button id="ss-close-btn" title="Close">&times;</button>
        </div>
    </div>
    <video id="screen-share-video" autoplay playsinline></video>
</div>

<!-- Canvas Panel -->
<div id="canvas-panel">
    <div id="canvas-toolbar">
        <button data-tool="pen" class="active" title="Pen">&#x270F;</button>
        <button data-tool="line" title="Line">&#x2571;</button>
        <button data-tool="rect" title="Rectangle">&#x25A1;</button>
        <button data-tool="circle" title="Circle">&#x25CB;</button>
        <button data-tool="arrow" title="Arrow">&#x2192;</button>
        <button data-tool="text" title="Text">T</button>
        <button data-tool="eraser" title="Eraser">&#x2327;</button>
        <div class="separator"></div>
        <div class="color-swatch active" data-color="#00ffc8" style="background:#00ffc8" title="Cyan"></div>
        <div class="color-swatch" data-color="#4fc3f7" style="background:#4fc3f7" title="Blue"></div>
        <div class="color-swatch" data-color="#66bb6a" style="background:#66bb6a" title="Green"></div>
        <div class="color-swatch" data-color="#ffb74d" style="background:#ffb74d" title="Amber"></div>
        <div class="color-swatch" data-color="#e94560" style="background:#e94560" title="Red"></div>
        <div class="color-swatch" data-color="#ce93d8" style="background:#ce93d8" title="Magenta"></div>
        <div class="color-swatch" data-color="#ffffff" style="background:#ffffff" title="White"></div>
        <div class="separator"></div>
        <select id="canvas-width">
            <option value="1">1px</option>
            <option value="2" selected>2px</option>
            <option value="4">4px</option>
            <option value="8">8px</option>
        </select>
        <div class="separator"></div>
        <button id="canvas-undo-btn" title="Undo">Undo</button>
        <button id="canvas-clear-btn" title="Clear All">Clear</button>
        <button id="canvas-save-btn" title="Save as PNG">Save</button>
        <div id="canvas-mode-tabs">
            <button id="canvas-draw-tab" class="active">Draw</button>
            <button id="canvas-diagram-tab">Diagram</button>
        </div>
        <button id="canvas-close-btn">&times; Close</button>
    </div>
    <div id="canvas-body">
        <canvas id="drawing-canvas"></canvas>
        <div id="mermaid-container">
            <textarea id="mermaid-editor" placeholder="Enter Mermaid diagram syntax...&#10;&#10;Example:&#10;graph LR&#10;  A[Browser] --> B[WebSocket]&#10;  B --> C[Server]&#10;  C --> D[(PostgreSQL)]"></textarea>
            <div id="mermaid-preview"><div id="mermaid-output"></div></div>
        </div>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<script>
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('msg-input');
const sendBtn = document.getElementById('send-btn');
const statusEl = document.getElementById('status');
const searchToggle = document.getElementById('search-toggle');
const searchSidebar = document.getElementById('search-sidebar');
const sidebarClose = document.getElementById('sidebar-close');
const sbQuery = document.getElementById('sb-query');
const sbSender = document.getElementById('sb-sender');
const sbFromDate = document.getElementById('sb-from-date');
const sbToDate = document.getElementById('sb-to-date');
const sbSearchBtn = document.getElementById('sb-search-btn');
const searchResults = document.getElementById('search-results');
const statsPanel = document.getElementById('stats-panel');
const decisionsPanel = document.getElementById('decisions-panel');
const sidebarTabs = document.querySelectorAll('#sidebar-tabs button');
const channelList = document.getElementById('channel-list');
const scheduleBtn = document.getElementById('schedule-btn');
const schedOverlay = document.getElementById('schedule-overlay');
const schedDatetime = document.getElementById('sched-datetime');
const schedMessage = document.getElementById('sched-message');
const schedCancel = document.getElementById('sched-cancel');
const schedConfirm = document.getElementById('sched-confirm');

let ws = null;
let reconnectTimer = null;
let knownSenders = new Set();
let currentChannel = 'general';
let channels = [];
let unreadCounts = {};  // channel -> count

const HERD_NAMES = ['jules', 'colette', 'gaston', 'sam'];
const HERD_INITIALS = { jules: 'J', colette: 'Co', gaston: 'G', sam: 'S' };
const AI_SENDERS_SET = new Set(['nova', 'claude code']);
const attachBtn = document.getElementById('attach-btn');
const fileInput = document.getElementById('file-input');
const dropOverlay = document.getElementById('drop-overlay');
const imageModal = document.getElementById('image-modal');
const modalImg = document.getElementById('modal-img');
const uploadProgress = document.getElementById('upload-progress');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');

function isHerd(sender) {
    return HERD_NAMES.includes(sender.toLowerCase());
}

function getAvatarClass(sender) {
    const s = sender.toLowerCase();
    if (s === 'nova') return 'avatar-nova';
    if (s.includes('claude')) return 'avatar-claude';
    if (isHerd(s)) return 'avatar-herd-' + s;
    if (s === 'system') return 'avatar-system';
    return 'avatar-jordan';
}

function getSenderClass(sender) {
    const s = sender.toLowerCase();
    if (s === 'nova') return 'sender-nova';
    if (s.includes('claude')) return 'sender-claude';
    if (isHerd(s)) return 'sender-herd-' + s;
    if (s === 'system') return 'sender-system';
    return 'sender-jordan';
}

function getMsgClass(senderType) {
    if (senderType === 'ai') return 'ai';
    if (senderType === 'agent') return 'agent';
    if (senderType === 'herd') return 'herd';
    return 'human';
}

function getInitial(sender) {
    const s = sender.toLowerCase();
    if (s === 'nova') return 'N';
    if (s.includes('claude')) return 'C';
    if (HERD_INITIALS[s]) return HERD_INITIALS[s];
    if (s === 'system') return 'S';
    return 'J';
}

function formatTime(isoString) {
    const d = new Date(isoString);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatDateTime(isoString) {
    const d = new Date(isoString);
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function trackSender(sender) {
    if (!knownSenders.has(sender)) {
        knownSenders.add(sender);
        const opt = document.createElement('option');
        opt.value = sender;
        opt.textContent = sender;
        sbSender.appendChild(opt);
    }
}

function appendMessage(msg) {
    trackSender(msg.sender);
    const div = document.createElement('div');

    // Decision card rendering
    if (msg.decision) {
        div.className = 'msg decision';
        div.dataset.msgId = msg.id || '';
        const status = msg.decision.status || 'active';
        const statusClass = status === 'active' ? 'decision-status-active' : 'decision-status-revoked';
        div.innerHTML = `
            <div class="decision-card">
                <span class="decision-icon">&#x2696;</span>
                <div class="decision-body">
                    <div class="decision-text">${escapeHtml(msg.decision.text)}</div>
                    <div class="decision-meta">
                        <span class="${statusClass}">${status}</span> &mdash;
                        ${escapeHtml(msg.decision.decided_by)} &bull;
                        ${formatTime(msg.decision.created_at || msg.timestamp)}
                    </div>
                </div>
            </div>
        `;
        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        return;
    }

    // Scheduled message metadata indicator
    const isScheduled = msg.metadata && msg.metadata.scheduled;
    const schedBadge = isScheduled ? '<span class="schedule-icon" title="Scheduled message">&#x23F0;</span> ' : '';

    div.className = 'msg ' + getMsgClass(msg.sender_type);
    div.dataset.msgId = msg.id || '';
    div.innerHTML = `
        <div class="msg-avatar ${getAvatarClass(msg.sender)}">${getInitial(msg.sender)}</div>
        <div class="msg-body">
            <div class="msg-header">
                <span class="msg-sender ${getSenderClass(msg.sender)}">${schedBadge}${msg.sender}</span>
                <span class="msg-time">${formatTime(msg.timestamp)}</span>
            </div>
            <div class="msg-text">${escapeHtml(msg.message)}</div>
        </div>
    `;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendCommandResult(data) {
    const div = document.createElement('div');
    div.className = 'msg command-result';
    const content = formatCommandResult(data);
    div.innerHTML = `
        <div class="msg-avatar avatar-system">&#x2318;</div>
        <div class="msg-body">
            <div class="msg-header">
                <span class="msg-sender sender-system">${escapeHtml(data.command || 'command')}</span>
                <span class="msg-time">${formatTime(new Date().toISOString())}</span>
            </div>
            <div class="cmd-result-text">${content}</div>
        </div>
    `;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function formatCommandResult(data) {
    const results = data.results;
    if (typeof results === 'string') {
        return escapeHtml(results);
    }
    if (data.command === '/stats' && typeof results === 'object' && !Array.isArray(results)) {
        return formatStatsResult(results);
    }
    if (Array.isArray(results)) {
        let lines = [];
        if (data.query) lines.push(`Search: "${escapeHtml(data.query)}" (${results.length} results)`);
        if (data.sender_filter) lines.push(`From: ${escapeHtml(data.sender_filter)} (${results.length} messages)`);
        if (data.duration) lines.push(`Period: last ${data.duration} (${data.count || results.length} messages)`);
        if (data.topic) lines.push(`Topic: "${escapeHtml(data.topic)}" (${results.length} results)`);
        lines.push('');
        for (const r of results) {
            if (r.sender && r.timestamp && r.message) {
                lines.push(`[${formatDateTime(r.timestamp)}] ${r.sender}: ${r.message}`);
            } else if (r.content) {
                const score = r.score ? ` (score: ${parseFloat(r.score).toFixed(3)})` : '';
                lines.push(`[${r.source || 'memory'}]${score}: ${r.content}`);
            } else if (r.sender && r.count !== undefined) {
                lines.push(`  ${r.sender}: ${r.count}`);
            }
        }
        return escapeHtml(lines.join('\\n'));
    }
    return escapeHtml(JSON.stringify(results, null, 2));
}

function formatStatsResult(stats) {
    let lines = [];
    lines.push(`Total Messages: ${stats.total_messages}`);
    lines.push('');
    lines.push('--- Messages Per Sender ---');
    for (const s of (stats.messages_per_sender || [])) {
        lines.push(`  ${s.sender}: ${s.count}`);
    }
    lines.push('');
    lines.push('--- Busiest Hours (UTC) ---');
    for (const h of (stats.messages_per_hour || [])) {
        const bar = '|'.repeat(Math.min(Math.round(h.count / 2), 30));
        lines.push(`  ${String(h.hour).padStart(2, '0')}:00  ${bar} (${h.count})`);
    }
    if (stats.busiest_day) {
        lines.push('');
        lines.push(`Most Active Day: ${stats.busiest_day.day} (${stats.busiest_day.count} messages)`);
    }
    lines.push('');
    lines.push('--- Avg Message Length ---');
    for (const a of (stats.avg_message_length || [])) {
        lines.push(`  ${a.sender}: ${a.avg_chars} chars`);
    }
    lines.push('');
    lines.push('--- Top Words ---');
    for (const w of (stats.top_words || [])) {
        lines.push(`  ${w.word}: ${w.count}`);
    }
    return escapeHtml(lines.join('\\n'));
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// --- Search Sidebar ---
function toggleSidebar() {
    searchSidebar.classList.toggle('open');
    searchToggle.classList.toggle('active');
    if (searchSidebar.classList.contains('open')) {
        sbQuery.focus();
    }
}

searchToggle.addEventListener('click', toggleSidebar);
sidebarClose.addEventListener('click', toggleSidebar);

sidebarTabs.forEach(tab => {
    tab.addEventListener('click', () => {
        sidebarTabs.forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        const which = tab.dataset.tab;
        document.getElementById('search-form').style.display = which === 'search' ? 'flex' : 'none';
        searchResults.style.display = which === 'search' ? 'block' : 'none';
        statsPanel.style.display = which === 'stats' ? 'block' : 'none';
        decisionsPanel.style.display = which === 'decisions' ? 'block' : 'none';
        if (which === 'stats') loadStats();
        if (which === 'decisions') loadDecisions();
    });
});

function doSidebarSearch() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const payload = {
        type: 'search',
        query: sbQuery.value.trim(),
        sender_filter: sbSender.value,
        from_date: sbFromDate.value ? new Date(sbFromDate.value).toISOString() : '',
        to_date: sbToDate.value ? new Date(sbToDate.value + 'T23:59:59').toISOString() : '',
    };
    ws.send(JSON.stringify(payload));
}

sbSearchBtn.addEventListener('click', doSidebarSearch);
sbQuery.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSidebarSearch(); });

function renderSearchResults(data) {
    searchResults.innerHTML = '';
    if (!data.results || data.results.length === 0) {
        searchResults.innerHTML = '<div style="padding:12px;color:#888;font-size:12px;">No results found.</div>';
        return;
    }
    for (const r of data.results) {
        const card = document.createElement('div');
        card.className = 'search-result-card';
        card.innerHTML = `
            <div class="sr-header">
                <span class="sr-sender">${escapeHtml(r.sender)}</span>
                <span class="sr-time">${formatDateTime(r.timestamp)}</span>
            </div>
            <div class="sr-text">${escapeHtml(r.message)}</div>
        `;
        card.addEventListener('click', () => {
            // Try to scroll to this message in the main timeline
            if (r.id) {
                const el = messagesEl.querySelector(`[data-msg-id="${r.id}"]`);
                if (el) {
                    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    el.style.outline = '2px solid #4fc3f7';
                    setTimeout(() => { el.style.outline = ''; }, 2000);
                    return;
                }
            }
            // Fallback: show inline
            appendMessage({ id: r.id, sender: r.sender, sender_type: r.sender_type || 'human', message: r.message, timestamp: r.timestamp });
        });
        searchResults.appendChild(card);
    }
}

function loadStats() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'stats_request' }));
}

function renderStats(data) {
    const stats = data.results;
    if (!stats || typeof stats !== 'object') {
        statsPanel.innerHTML = '<div style="padding:12px;color:#888;">No stats available.</div>';
        return;
    }
    let html = '';

    // Total
    html += `<div class="stats-section"><h4>Overview</h4><div class="stats-row"><span>Total Messages</span><span>${stats.total_messages}</span></div></div>`;

    // Per sender
    if (stats.messages_per_sender && stats.messages_per_sender.length) {
        const maxCount = stats.messages_per_sender[0].count;
        html += '<div class="stats-section"><h4>Messages Per Sender</h4>';
        for (const s of stats.messages_per_sender) {
            const pct = Math.round((s.count / maxCount) * 100);
            html += `<div class="stats-row"><span>${escapeHtml(s.sender)}</span><span>${s.count}</span></div>`;
            html += `<div class="stats-bar"><div class="stats-bar-fill" style="width:${pct}%"></div></div>`;
        }
        html += '</div>';
    }

    // Busiest hours
    if (stats.messages_per_hour && stats.messages_per_hour.length) {
        const maxH = Math.max(...stats.messages_per_hour.map(h => h.count));
        html += '<div class="stats-section"><h4>Messages Per Hour (UTC)</h4>';
        for (const h of stats.messages_per_hour) {
            const pct = Math.round((h.count / maxH) * 100);
            html += `<div class="stats-row"><span class="label">${String(h.hour).padStart(2,'0')}:00</span><span>${h.count}</span></div>`;
            html += `<div class="stats-bar"><div class="stats-bar-fill" style="width:${pct}%"></div></div>`;
        }
        html += '</div>';
    }

    // Busiest day
    if (stats.busiest_day) {
        html += `<div class="stats-section"><h4>Most Active Day</h4><div class="stats-row"><span>${stats.busiest_day.day}</span><span>${stats.busiest_day.count} msgs</span></div></div>`;
    }

    // Avg message length
    if (stats.avg_message_length && stats.avg_message_length.length) {
        html += '<div class="stats-section"><h4>Avg Message Length</h4>';
        for (const a of stats.avg_message_length) {
            html += `<div class="stats-row"><span>${escapeHtml(a.sender)}</span><span>${a.avg_chars} chars</span></div>`;
        }
        html += '</div>';
    }

    // Top words
    if (stats.top_words && stats.top_words.length) {
        html += '<div class="stats-section"><h4>Top Words</h4>';
        for (const w of stats.top_words) {
            html += `<div class="stats-row"><span>${escapeHtml(w.word)}</span><span>${w.count}</span></div>`;
        }
        html += '</div>';
    }

    statsPanel.innerHTML = html;
}

// --- WebSocket ---
function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => {
        statusEl.textContent = 'connected';
        statusEl.className = 'status connected';
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
    };

    ws.onclose = () => {
        statusEl.textContent = 'disconnected';
        statusEl.className = 'status disconnected';
        reconnectTimer = setTimeout(connect, 3000);
    };

    ws.onerror = () => {
        ws.close();
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        // Handle Screen Share + Canvas messages first
        if (typeof handleAdvancedMessage === 'function' && handleAdvancedMessage(data)) return;
        if (data.type === 'channels') {
            renderChannels(data.channels);
        } else if (data.type === 'history') {
            messagesEl.innerHTML = '';
            data.messages.forEach(appendMessage);
        } else if (data.type === 'message') {
            // Track unread for non-active channels
            const msgChannel = data.channel || 'general';
            if (msgChannel !== currentChannel) {
                handleUnread(msgChannel);
            } else {
                appendMessage(data);
            }
        } else if (data.type === 'command_result') {
            appendCommandResult(data);
            // Also render in sidebar panels if applicable
            if (data.command === '/stats') renderStats(data);
            if (data.command === '/decisions') renderDecisions(data);
        } else if (data.type === 'search_results') {
            renderSearchResults(data);
        } else if (data.type === 'schedule_confirmed') {
            // Show confirmation as inline pending indicator
            const pendingDiv = document.createElement('div');
            pendingDiv.className = 'msg scheduled-pending';
            const deliverAt = new Date(data.deliver_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
            pendingDiv.innerHTML = `
                <div class="msg-avatar avatar-jordan">J</div>
                <div class="msg-body">
                    <div class="msg-header">
                        <span class="msg-sender sender-jordan">Jordan</span>
                        <span class="msg-time"><span class="schedule-icon">&#x23F0;</span> Scheduled for ${deliverAt}</span>
                    </div>
                    <div class="msg-text">${escapeHtml(data.message)}</div>
                </div>
            `;
            messagesEl.appendChild(pendingDiv);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (data.type === 'error') {
            appendCommandResult({ command: 'error', results: data.message });
        }
    };
}

function sendMessage() {
    const text = inputEl.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ sender: 'Jordan', message: text, channel: currentChannel }));
    inputEl.value = '';
}

// --- Channel Sidebar ---
function renderChannels(chs) {
    channels = chs;
    channelList.innerHTML = '';
    for (const ch of channels) {
        const div = document.createElement('div');
        div.className = 'channel-item' + (ch.id === currentChannel ? ' active' : '');
        div.dataset.channelId = ch.id;
        div.innerHTML = `<span class="ch-name"># ${ch.id}</span><span class="unread-badge" id="unread-${ch.id}"></span>`;
        div.title = ch.description || '';
        div.addEventListener('click', () => switchChannel(ch.id));
        channelList.appendChild(div);
    }
}

function switchChannel(channelId) {
    if (channelId === currentChannel) return;
    currentChannel = channelId;
    // Update active state
    document.querySelectorAll('.channel-item').forEach(el => {
        el.classList.toggle('active', el.dataset.channelId === channelId);
    });
    // Clear unread for this channel
    unreadCounts[channelId] = 0;
    const badge = document.getElementById('unread-' + channelId);
    if (badge) { badge.textContent = ''; badge.classList.remove('visible'); }
    // Request history for new channel
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'switch_channel', channel: channelId }));
    }
}

function handleUnread(channel) {
    if (channel === currentChannel) return;
    unreadCounts[channel] = (unreadCounts[channel] || 0) + 1;
    const badge = document.getElementById('unread-' + channel);
    if (badge) {
        badge.textContent = unreadCounts[channel] > 99 ? '99+' : unreadCounts[channel];
        badge.classList.add('visible');
    }
}

// --- Schedule Overlay ---
scheduleBtn.addEventListener('click', () => {
    schedOverlay.classList.toggle('visible');
    if (schedOverlay.classList.contains('visible')) {
        // Default to 1 hour from now
        const now = new Date(Date.now() + 3600000);
        schedDatetime.value = now.toISOString().slice(0, 16);
        schedMessage.focus();
    }
});
schedCancel.addEventListener('click', () => { schedOverlay.classList.remove('visible'); });
schedConfirm.addEventListener('click', () => {
    const dt = schedDatetime.value;
    const msg = schedMessage.value.trim();
    if (!dt || !msg) return;
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: 'schedule',
            sender: 'Jordan',
            message: msg,
            deliver_at: new Date(dt).toISOString(),
            channel: currentChannel,
        }));
    }
    schedOverlay.classList.remove('visible');
    schedMessage.value = '';
});

// --- Decisions panel ---
function loadDecisions() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'decisions_request', filter: '' }));
}

function renderDecisions(data) {
    const results = data.results;
    if (typeof results === 'string') {
        decisionsPanel.innerHTML = `<div style="padding:12px;color:#888;font-size:12px;">${escapeHtml(results)}</div>`;
        return;
    }
    decisionsPanel.innerHTML = `<div style="padding:12px;color:#888;font-size:12px;">${escapeHtml(results)}</div>`;
}

sendBtn.addEventListener('click', sendMessage);
inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});


// ── Screen Share (WebRTC) ─────────────────────────────────────────────────
const screenShareBtn = document.getElementById('screen-share-btn');
const ssPanel = document.getElementById('screen-share-panel');
const ssVideo = document.getElementById('screen-share-video');
const ssPipBtn = document.getElementById('ss-pip-btn');
const ssFullscreenBtn = document.getElementById('ss-fullscreen-btn');
const ssCloseBtn = document.getElementById('ss-close-btn');
const ssPanelHeader = document.getElementById('ss-panel-header');
const ssPanelTitle = document.getElementById('ss-panel-title');
const sharingIndicator = document.getElementById('sharing-indicator');

let isSharing = false;
let localStream = null;
let peerConnections = {};
const rtcConfig = { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] };
const MY_NAME = 'Jordan';

screenShareBtn.addEventListener('click', async () => {
    if (!isSharing) {
        try {
            localStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
            isSharing = true;
            screenShareBtn.classList.add('sharing');
            screenShareBtn.title = 'Stop Sharing';
            sharingIndicator.textContent = 'Sharing...';
            sharingIndicator.style.display = 'inline';
            ws.send(JSON.stringify({ type: 'screen_share_start', sender: MY_NAME }));
            localStream.getVideoTracks()[0].onended = () => { stopSharing(); };
        } catch (e) { console.log('Screen share cancelled:', e); }
    } else { stopSharing(); }
});

function stopSharing() {
    if (localStream) { localStream.getTracks().forEach(t => t.stop()); localStream = null; }
    isSharing = false;
    screenShareBtn.classList.remove('sharing');
    screenShareBtn.title = 'Share Screen';
    sharingIndicator.style.display = 'none';
    Object.values(peerConnections).forEach(pc => pc.close());
    peerConnections = {};
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'screen_share_stop', sender: MY_NAME }));
}

async function createOfferForViewer(viewerName) {
    const pc = new RTCPeerConnection(rtcConfig);
    peerConnections[viewerName] = pc;
    localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
    pc.onicecandidate = (e) => { if (e.candidate) ws.send(JSON.stringify({ type: 'rtc_ice', from: MY_NAME, to: viewerName, candidate: JSON.stringify(e.candidate) })); };
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    ws.send(JSON.stringify({ type: 'rtc_offer', from: MY_NAME, to: viewerName, sdp: offer.sdp }));
}

async function handleRtcOffer(from, sdp) {
    const pc = new RTCPeerConnection(rtcConfig);
    peerConnections[from] = pc;
    pc.onicecandidate = (e) => { if (e.candidate) ws.send(JSON.stringify({ type: 'rtc_ice', from: MY_NAME, to: from, candidate: JSON.stringify(e.candidate) })); };
    pc.ontrack = (e) => { ssVideo.srcObject = e.streams[0]; ssPanel.classList.add('visible'); ssPanelTitle.textContent = from + "'s Screen"; };
    await pc.setRemoteDescription(new RTCSessionDescription({ type: 'offer', sdp }));
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    ws.send(JSON.stringify({ type: 'rtc_answer', from: MY_NAME, to: from, sdp: answer.sdp }));
}

async function handleRtcAnswer(from, sdp) { const pc = peerConnections[from]; if (pc) await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp })); }
async function handleRtcIce(from, candidateStr) { const pc = peerConnections[from]; if (pc && candidateStr) await pc.addIceCandidate(new RTCIceCandidate(JSON.parse(candidateStr))); }

ssCloseBtn.addEventListener('click', () => { ssPanel.classList.remove('visible'); ssVideo.srcObject = null; });
ssPipBtn.addEventListener('click', async () => { try { await ssVideo.requestPictureInPicture(); } catch(e) {} });
ssFullscreenBtn.addEventListener('click', () => { if (ssPanel.requestFullscreen) ssPanel.requestFullscreen(); });

// Draggable panel
let ssDragging = false, ssOffX = 0, ssOffY = 0;
ssPanelHeader.addEventListener('mousedown', (e) => { ssDragging = true; ssOffX = e.clientX - ssPanel.offsetLeft; ssOffY = e.clientY - ssPanel.offsetTop; });
document.addEventListener('mousemove', (e) => { if (!ssDragging) return; ssPanel.style.left = (e.clientX - ssOffX) + 'px'; ssPanel.style.top = (e.clientY - ssOffY) + 'px'; ssPanel.style.right = 'auto'; ssPanel.style.bottom = 'auto'; });
document.addEventListener('mouseup', () => { ssDragging = false; });

// ── Collaborative Canvas ─────────────────────────────────────────────────────
const canvasToggle = document.getElementById('canvas-toggle');
const canvasPanel = document.getElementById('canvas-panel');
const drawingCanvas = document.getElementById('drawing-canvas');
const canvasCtx = drawingCanvas.getContext('2d');
const mermaidContainer = document.getElementById('mermaid-container');
const mermaidEditor = document.getElementById('mermaid-editor');
const mermaidOutput = document.getElementById('mermaid-output');
const canvasWidthSel = document.getElementById('canvas-width');
const canvasUndoBtn = document.getElementById('canvas-undo-btn');
const canvasClearBtn = document.getElementById('canvas-clear-btn');
const canvasSaveBtn = document.getElementById('canvas-save-btn');
const canvasCloseBtn2 = document.getElementById('canvas-close-btn');
const canvasDrawTab = document.getElementById('canvas-draw-tab');
const canvasDiagramTab = document.getElementById('canvas-diagram-tab');

let canvasIsOpen = false, currentTool = 'pen', currentColor = '#00ffc8', currentWidth = 2;
let cvDrawing = false, currentPoints = [], canvasOps = [], shapeStart = null, canvasSnapshot = null;

if (typeof mermaid !== 'undefined') mermaid.initialize({ startOnLoad: false, theme: 'dark', themeVariables: { primaryColor: '#4fc3f7', primaryTextColor: '#e0e0e0', lineColor: '#4fc3f7' } });

function resizeCanvas() { const r = drawingCanvas.parentElement.getBoundingClientRect(); drawingCanvas.width = r.width; drawingCanvas.height = r.height; redrawCanvas(); }
function redrawCanvas() { canvasCtx.fillStyle = '#1a1a2e'; canvasCtx.fillRect(0, 0, drawingCanvas.width, drawingCanvas.height); canvasOps.forEach(op => drawOperation(op)); }

function drawOperation(op) {
    const ctx = canvasCtx;
    ctx.strokeStyle = op.color || '#00ffc8'; ctx.lineWidth = op.width || 2; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    if (op.tool === 'pen' && op.points && op.points.length > 1) { ctx.beginPath(); ctx.moveTo(op.points[0][0], op.points[0][1]); for (let i=1;i<op.points.length;i++) ctx.lineTo(op.points[i][0], op.points[i][1]); ctx.stroke(); }
    else if (op.tool === 'line' && op.points && op.points.length === 2) { ctx.beginPath(); ctx.moveTo(op.points[0][0], op.points[0][1]); ctx.lineTo(op.points[1][0], op.points[1][1]); ctx.stroke(); }
    else if (op.tool === 'rect') { ctx.beginPath(); ctx.strokeRect(op.x, op.y, op.w, op.h); }
    else if (op.tool === 'circle') { ctx.beginPath(); ctx.ellipse(op.x+op.w/2, op.y+op.h/2, Math.abs(op.w)/2, Math.abs(op.h)/2, 0, 0, Math.PI*2); ctx.stroke(); }
    else if (op.tool === 'arrow' && op.points && op.points.length === 2) { const [x1,y1]=op.points[0],[x2,y2]=op.points[1]; ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke(); const a=Math.atan2(y2-y1,x2-x1); ctx.beginPath(); ctx.moveTo(x2,y2); ctx.lineTo(x2-12*Math.cos(a-Math.PI/6),y2-12*Math.sin(a-Math.PI/6)); ctx.moveTo(x2,y2); ctx.lineTo(x2-12*Math.cos(a+Math.PI/6),y2-12*Math.sin(a+Math.PI/6)); ctx.stroke(); }
    else if (op.tool === 'text') { ctx.fillStyle = op.color || '#00ffc8'; ctx.font = (op.size||14)+'px -apple-system, sans-serif'; ctx.fillText(op.text||'', op.x, op.y); }
    else if (op.tool === 'eraser' && op.points && op.points.length > 1) { ctx.strokeStyle = '#1a1a2e'; ctx.lineWidth = (op.width||2)*4; ctx.beginPath(); ctx.moveTo(op.points[0][0], op.points[0][1]); for (let i=1;i<op.points.length;i++) ctx.lineTo(op.points[i][0], op.points[i][1]); ctx.stroke(); }
}

canvasToggle.addEventListener('click', () => {
    canvasIsOpen = !canvasIsOpen;
    canvasPanel.classList.toggle('visible', canvasIsOpen);
    canvasToggle.classList.toggle('active', canvasIsOpen);
    if (canvasIsOpen) { setTimeout(resizeCanvas, 50); if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'canvas_open' })); }
});
canvasCloseBtn2.addEventListener('click', () => { canvasIsOpen = false; canvasPanel.classList.remove('visible'); canvasToggle.classList.remove('active'); });

document.querySelectorAll('#canvas-toolbar button[data-tool]').forEach(btn => {
    btn.addEventListener('click', () => { document.querySelectorAll('#canvas-toolbar button[data-tool]').forEach(b => b.classList.remove('active')); btn.classList.add('active'); currentTool = btn.dataset.tool; drawingCanvas.style.cursor = currentTool === 'text' ? 'text' : 'crosshair'; });
});
document.querySelectorAll('.color-swatch').forEach(sw => { sw.addEventListener('click', () => { document.querySelectorAll('.color-swatch').forEach(s => s.classList.remove('active')); sw.classList.add('active'); currentColor = sw.dataset.color; }); });
canvasWidthSel.addEventListener('change', () => { currentWidth = parseInt(canvasWidthSel.value); });

drawingCanvas.addEventListener('mousedown', (e) => {
    if (currentTool === 'text') { const text = prompt('Enter text:'); if (text) { const r = drawingCanvas.getBoundingClientRect(); const op = { tool: 'text', x: e.clientX-r.left, y: e.clientY-r.top, text, color: currentColor, size: 14, width: currentWidth }; canvasOps.push(op); drawOperation(op); sendCanvasOp(op); } return; }
    cvDrawing = true; const r = drawingCanvas.getBoundingClientRect(); const x = e.clientX-r.left, y = e.clientY-r.top; currentPoints = [[x,y]]; shapeStart = [x,y];
    if (['line','rect','circle','arrow'].includes(currentTool)) canvasSnapshot = canvasCtx.getImageData(0, 0, drawingCanvas.width, drawingCanvas.height);
});

drawingCanvas.addEventListener('mousemove', (e) => {
    if (!cvDrawing) return;
    const r = drawingCanvas.getBoundingClientRect(); const x = e.clientX-r.left, y = e.clientY-r.top; currentPoints.push([x,y]);
    if (currentTool === 'pen' || currentTool === 'eraser') { const ctx = canvasCtx; ctx.strokeStyle = currentTool==='eraser'?'#1a1a2e':currentColor; ctx.lineWidth = currentTool==='eraser'?currentWidth*4:currentWidth; ctx.lineCap='round'; ctx.beginPath(); const p=currentPoints[currentPoints.length-2]; ctx.moveTo(p[0],p[1]); ctx.lineTo(x,y); ctx.stroke(); }
    else if (['line','rect','circle','arrow'].includes(currentTool)) {
        canvasCtx.putImageData(canvasSnapshot, 0, 0); const ctx = canvasCtx; ctx.strokeStyle = currentColor; ctx.lineWidth = currentWidth; ctx.lineCap = 'round'; const [sx,sy] = shapeStart;
        if (currentTool==='line') { ctx.beginPath(); ctx.moveTo(sx,sy); ctx.lineTo(x,y); ctx.stroke(); }
        else if (currentTool==='rect') { ctx.strokeRect(sx,sy,x-sx,y-sy); }
        else if (currentTool==='circle') { ctx.beginPath(); ctx.ellipse(sx+(x-sx)/2,sy+(y-sy)/2,Math.abs(x-sx)/2,Math.abs(y-sy)/2,0,0,Math.PI*2); ctx.stroke(); }
        else if (currentTool==='arrow') { ctx.beginPath(); ctx.moveTo(sx,sy); ctx.lineTo(x,y); ctx.stroke(); const a=Math.atan2(y-sy,x-sx); ctx.beginPath(); ctx.moveTo(x,y); ctx.lineTo(x-12*Math.cos(a-Math.PI/6),y-12*Math.sin(a-Math.PI/6)); ctx.moveTo(x,y); ctx.lineTo(x-12*Math.cos(a+Math.PI/6),y-12*Math.sin(a+Math.PI/6)); ctx.stroke(); }
    }
});

drawingCanvas.addEventListener('mouseup', (e) => {
    if (!cvDrawing) return; cvDrawing = false;
    const r = drawingCanvas.getBoundingClientRect(); const x = e.clientX-r.left, y = e.clientY-r.top; let op = null;
    if (currentTool==='pen'||currentTool==='eraser') { if (currentPoints.length>1) op = { tool: currentTool, color: currentColor, width: currentWidth, points: currentPoints }; }
    else if (currentTool==='line'||currentTool==='arrow') { op = { tool: currentTool, color: currentColor, width: currentWidth, points: [shapeStart,[x,y]] }; }
    else if (currentTool==='rect') { const [sx,sy]=shapeStart; op = { tool:'rect', color:currentColor, width:currentWidth, x:sx, y:sy, w:x-sx, h:y-sy }; }
    else if (currentTool==='circle') { const [sx,sy]=shapeStart; op = { tool:'circle', color:currentColor, width:currentWidth, x:sx, y:sy, w:x-sx, h:y-sy }; }
    if (op) { canvasOps.push(op); sendCanvasOp(op); }
    currentPoints = []; shapeStart = null; canvasSnapshot = null;
});

drawingCanvas.addEventListener('mouseleave', () => { if (cvDrawing) { cvDrawing = false; if ((currentTool==='pen'||currentTool==='eraser') && currentPoints.length>1) { const op = { tool:currentTool, color:currentColor, width:currentWidth, points:currentPoints }; canvasOps.push(op); sendCanvasOp(op); } currentPoints = []; } });

function sendCanvasOp(op) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'canvas_draw', sender: MY_NAME, op })); }

canvasUndoBtn.addEventListener('click', () => { if (canvasOps.length>0) { canvasOps.pop(); redrawCanvas(); } });
canvasClearBtn.addEventListener('click', () => { if (confirm('Clear canvas for everyone?')) { canvasOps = []; redrawCanvas(); if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'canvas_clear', canvas_id: 'default', sender: MY_NAME })); } });
canvasSaveBtn.addEventListener('click', () => { const a = document.createElement('a'); a.download = 'canvas-'+new Date().toISOString().slice(0,10)+'.png'; a.href = drawingCanvas.toDataURL('image/png'); a.click(); });

canvasDrawTab.addEventListener('click', () => { canvasDrawTab.classList.add('active'); canvasDiagramTab.classList.remove('active'); drawingCanvas.style.display = 'block'; mermaidContainer.classList.remove('visible'); setTimeout(resizeCanvas, 50); });
canvasDiagramTab.addEventListener('click', () => { canvasDiagramTab.classList.add('active'); canvasDrawTab.classList.remove('active'); drawingCanvas.style.display = 'none'; mermaidContainer.classList.add('visible'); renderMermaid(); });

let mermaidTimer = null;
mermaidEditor.addEventListener('input', () => { clearTimeout(mermaidTimer); mermaidTimer = setTimeout(() => { renderMermaid(); if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'canvas_mermaid', source: mermaidEditor.value, sender: MY_NAME })); }, 500); });

async function renderMermaid() {
    const src = mermaidEditor.value.trim();
    if (!src) { mermaidOutput.innerHTML = '<p style="color:#666">Enter Mermaid syntax on the left</p>'; return; }
    try { const { svg } = await mermaid.render('mg-'+Date.now(), src); mermaidOutput.innerHTML = svg; }
    catch (e) { mermaidOutput.innerHTML = '<p style="color:#e94560;font-size:12px;">Syntax error: '+escapeHtml(String(e.message||e))+'</p>'; }
}

function handleAdvancedMessage(data) {
    if (data.type === 'screen_share_available') { sharingIndicator.textContent = data.sender + ' is sharing'; sharingIndicator.style.display = 'inline'; if (data.sender !== MY_NAME) ws.send(JSON.stringify({ type: 'screen_share_request', from: MY_NAME, to: data.sender })); return true; }
    if (data.type === 'screen_share_stopped') { sharingIndicator.style.display = 'none'; ssPanel.classList.remove('visible'); ssVideo.srcObject = null; if (peerConnections[data.sender]) { peerConnections[data.sender].close(); delete peerConnections[data.sender]; } return true; }
    if (data.type === 'screen_share_request') { if (isSharing && data.from !== MY_NAME) createOfferForViewer(data.from); return true; }
    if (data.type === 'rtc_offer') { handleRtcOffer(data.from, data.sdp); return true; }
    if (data.type === 'rtc_answer') { handleRtcAnswer(data.from, data.sdp); return true; }
    if (data.type === 'rtc_ice') { handleRtcIce(data.from, data.candidate); return true; }
    if (data.type === 'canvas_state') { canvasOps = data.operations || []; if (data.mermaid_source) mermaidEditor.value = data.mermaid_source; if (canvasIsOpen) { redrawCanvas(); renderMermaid(); } return true; }
    if (data.type === 'canvas_draw') { canvasOps.push(data.op); if (canvasIsOpen) drawOperation(data.op); return true; }
    if (data.type === 'canvas_clear') { canvasOps = []; if (canvasIsOpen) redrawCanvas(); return true; }
    if (data.type === 'canvas_mermaid') { mermaidEditor.value = data.source || ''; if (canvasIsOpen && mermaidContainer.classList.contains('visible')) renderMermaid(); return true; }
    return false;
}

window.addEventListener('resize', () => { if (canvasIsOpen) resizeCanvas(); });


connect();
inputEl.focus();
</script>
<script src="/chatroom_features.js"></script>
</body>
</html>
"""


# ── Entry Point ──────────────────────────────────────────────────────────────

def main():
    log.info(f"Starting Nova Chatroom on {HOST}:{PORT}")
    app = create_app()
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
