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
import os
import random
import re
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
    log.info("Table chatroom_messages ready")


async def store_message(sender: str, sender_type: str, message: str, metadata: dict = None) -> int:
    """Store a message and return its ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO chatroom_messages (sender, sender_type, message, metadata) "
            "VALUES ($1, $2, $3, $4) RETURNING id, created_at",
            sender, sender_type, message, json.dumps(metadata or {})
        )
        return row["id"], row["created_at"]


async def load_history(limit: int = MAX_HISTORY) -> list:
    """Load recent chat history."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, sender, sender_type, message, created_at "
            "FROM chatroom_messages ORDER BY created_at DESC LIMIT $1",
            limit
        )
        messages = []
        for row in reversed(rows):
            messages.append({
                "id": row["id"],
                "sender": row["sender"],
                "sender_type": row["sender_type"],
                "message": row["message"],
                "timestamp": row["created_at"].isoformat(),
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


async def broadcast(msg: dict):
    """Send a message to all connected WebSocket clients."""
    payload = json.dumps(msg)
    dead = set()
    for ws in _websockets:
        try:
            await ws.send_str(payload)
        except (ConnectionResetError, RuntimeError):
            dead.add(ws)
    _websockets.difference_update(dead)


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


async def handle_websocket(request):
    """Handle browser WebSocket connections (Jordan's chat)."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _websockets.add(ws)
    log.info(f"WebSocket connected ({len(_websockets)} total)")

    # Send chat history on connect
    history = await load_history()
    await ws.send_str(json.dumps({"type": "history", "messages": history}))

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)

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

                    text = data.get("message", "").strip()
                    sender = data.get("sender", "Jordan")
                    if not text:
                        continue

                    # Slash command handling — results go only to this client
                    if text.startswith("/"):
                        cmd_result = await handle_slash_command(text)
                        if cmd_result:
                            await ws.send_str(json.dumps(cmd_result))
                            continue

                    # Store Jordan's message
                    msg_id, ts = await store_message(sender, "human", text)
                    outgoing = {
                        "type": "message",
                        "id": msg_id,
                        "sender": sender,
                        "sender_type": "human",
                        "message": text,
                        "timestamp": ts.isoformat(),
                    }
                    await broadcast(outgoing)

                    # Smart mode: Nova only responds when addressed or when it's a general room statement
                    if _should_nova_respond(text):
                        # Check if this is a recall question — fetch context for Nova
                        if _is_recall_question(text):
                            asyncio.create_task(_nova_respond_with_recall(text))
                        else:
                            asyncio.create_task(_nova_respond(text))

                    # Check if a Herd member wants to chime in (at most one)
                    herd_responder = _pick_herd_responder(text)
                    if herd_responder:
                        asyncio.create_task(_herd_respond(text, herd_responder))

                except json.JSONDecodeError:
                    log.warning(f"Invalid JSON from WebSocket: {msg.data[:100]}")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.error(f"WebSocket error: {ws.exception()}")
    finally:
        _websockets.discard(ws)
        log.info(f"WebSocket disconnected ({len(_websockets)} total)")

    return ws


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


async def _nova_respond(user_message: str):
    """Get Nova's response and broadcast it."""
    try:
        response = await get_nova_response(user_message)
        if response:
            msg_id, ts = await store_message("Nova", "ai", response)
            await broadcast({
                "type": "message",
                "id": msg_id,
                "sender": "Nova",
                "sender_type": "ai",
                "message": response,
                "timestamp": ts.isoformat(),
            })
    except Exception as e:
        log.error(f"Nova response failed: {e}")
        # Still broadcast the error so the UI shows something
        msg_id, ts = await store_message("Nova", "ai", f"(error: {e})")
        await broadcast({
            "type": "message",
            "id": msg_id,
            "sender": "Nova",
            "sender_type": "ai",
            "message": f"(error: {e})",
            "timestamp": ts.isoformat(),
        })


async def _nova_respond_with_recall(user_message: str):
    """Get Nova's response with recall context included."""
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

        recent = await load_history(limit=10)
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
                        msg_id, ts = await store_message("Nova", "ai", content)
                        await broadcast({
                            "type": "message",
                            "id": msg_id,
                            "sender": "Nova",
                            "sender_type": "ai",
                            "message": content,
                            "timestamp": ts.isoformat(),
                        })
                else:
                    log.warning(f"Nova recall response: Ollama returned {resp.status}")
    except Exception as e:
        log.error(f"Nova recall response failed: {e}")
        msg_id, ts = await store_message("Nova", "ai", f"(recall error: {e})")
        await broadcast({
            "type": "message",
            "id": msg_id,
            "sender": "Nova",
            "sender_type": "ai",
            "message": f"(recall error: {e})",
            "timestamp": ts.isoformat(),
        })


async def handle_api_message(request):
    """POST /api/message — endpoint for Claude Code to send messages."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    text = data.get("message", "").strip()
    sender = data.get("sender", "Claude Code")
    sender_type = data.get("sender_type", "agent")

    if not text:
        return web.json_response({"error": "Empty message"}, status=400)

    # Store and broadcast
    msg_id, ts = await store_message(sender, sender_type, text)
    await broadcast({
        "type": "message",
        "id": msg_id,
        "sender": sender,
        "sender_type": sender_type,
        "message": text,
        "timestamp": ts.isoformat(),
    })

    log.info(f"API message from {sender}: {text[:80]}")

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


async def _herd_respond(text: str, responder_name: str):
    """Get a Herd member's response and broadcast it."""
    member = HERD_MEMBERS[responder_name]

    # 3-second delay so they don't step on Nova
    await asyncio.sleep(3.0)

    # Build conversation context
    recent = await load_history(limit=10)
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
                        msg_id, ts = await store_message(responder_name, "herd", content)
                        await broadcast({
                            "type": "message",
                            "id": msg_id,
                            "sender": responder_name,
                            "sender_type": "herd",
                            "message": content,
                            "timestamp": ts.isoformat(),
                        })
                        log.info(f"Herd member {responder_name} responded")
                else:
                    log.warning(f"Herd Ollama returned {resp.status} for {responder_name}")
    except asyncio.TimeoutError:
        log.warning(f"Herd member {responder_name} timed out")
    except Exception as e:
        log.error(f"Herd response error ({responder_name}): {e}")


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
    log.info(f"Nova Chatroom running on http://{HOST}:{PORT}")
    log.info(f"Claude Code endpoint: POST http://192.168.1.6:{PORT}/api/message")


async def on_shutdown(app):
    """Clean up on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
    # Close all WebSockets
    for ws in list(_websockets):
        await ws.close()
    _websockets.clear()
    log.info("Chatroom shut down")


def create_app() -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_websocket)
    app.router.add_post("/api/message", handle_api_message)
    app.router.add_get("/api/messages", handle_api_messages)
    app.router.add_get("/health", handle_health)
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

/* Scrollbar */
#messages::-webkit-scrollbar, #search-results::-webkit-scrollbar, #stats-panel::-webkit-scrollbar { width: 6px; }
#messages::-webkit-scrollbar-track, #search-results::-webkit-scrollbar-track, #stats-panel::-webkit-scrollbar-track { background: transparent; }
#messages::-webkit-scrollbar-thumb, #search-results::-webkit-scrollbar-thumb, #stats-panel::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
</style>
</head>
<body>

<header>
    <h1>Nova Chatroom</h1>
    <button id="search-toggle" title="Search & Stats">&#x1F50D;</button>
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
    <div id="messages"></div>

    <div id="search-sidebar">
        <div id="search-sidebar-header">
            <span>Search & Stats</span>
            <button class="close-btn" id="sidebar-close">&times;</button>
        </div>
        <div id="sidebar-tabs">
            <button class="active" data-tab="search">Search</button>
            <button data-tab="stats">Stats</button>
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
    </div>
</div>

<div id="input-area">
    <input type="text" id="msg-input" placeholder="Type a message... (/ for commands)" autocomplete="off" />
    <button id="send-btn">Send</button>
</div>

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
const sidebarTabs = document.querySelectorAll('#sidebar-tabs button');

let ws = null;
let reconnectTimer = null;
let knownSenders = new Set();

const HERD_NAMES = ['jules', 'colette', 'gaston', 'sam'];
const HERD_INITIALS = { jules: 'J', colette: 'Co', gaston: 'G', sam: 'S' };

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
    div.className = 'msg ' + getMsgClass(msg.sender_type);
    div.dataset.msgId = msg.id || '';
    div.innerHTML = `
        <div class="msg-avatar ${getAvatarClass(msg.sender)}">${getInitial(msg.sender)}</div>
        <div class="msg-body">
            <div class="msg-header">
                <span class="msg-sender ${getSenderClass(msg.sender)}">${msg.sender}</span>
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
        if (which === 'search') {
            document.getElementById('search-form').style.display = 'flex';
            searchResults.style.display = 'block';
            statsPanel.style.display = 'none';
        } else {
            document.getElementById('search-form').style.display = 'none';
            searchResults.style.display = 'none';
            statsPanel.style.display = 'block';
            loadStats();
        }
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
        if (data.type === 'history') {
            messagesEl.innerHTML = '';
            data.messages.forEach(appendMessage);
        } else if (data.type === 'message') {
            appendMessage(data);
        } else if (data.type === 'command_result') {
            appendCommandResult(data);
        } else if (data.type === 'search_results') {
            renderSearchResults(data);
        } else if (data.type === 'command_result' && data.command === '/stats') {
            // Stats from slash command rendered inline
            appendCommandResult(data);
        }
        // Stats response for sidebar
        if (data.command === '/stats' && data.type === 'command_result') {
            renderStats(data);
        }
    };
}

function sendMessage() {
    const text = inputEl.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ sender: 'Jordan', message: text }));
    inputEl.value = '';
}

sendBtn.addEventListener('click', sendMessage);
inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

connect();
inputEl.focus();
</script>
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
