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
from datetime import datetime, timezone
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
MAX_HISTORY = 100  # Messages to load on connect

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
                    text = data.get("message", "").strip()
                    sender = data.get("sender", "Jordan")
                    if not text:
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

#messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px 20px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

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
#messages::-webkit-scrollbar { width: 6px; }
#messages::-webkit-scrollbar-track { background: transparent; }
#messages::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
</style>
</head>
<body>

<header>
    <h1>Nova Chatroom</h1>
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

<div id="messages"></div>

<div id="input-area">
    <input type="text" id="msg-input" placeholder="Type a message..." autocomplete="off" />
    <button id="send-btn">Send</button>
</div>

<script>
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('msg-input');
const sendBtn = document.getElementById('send-btn');
const statusEl = document.getElementById('status');

let ws = null;
let reconnectTimer = null;

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
    return 'avatar-jordan';
}

function getSenderClass(sender) {
    const s = sender.toLowerCase();
    if (s === 'nova') return 'sender-nova';
    if (s.includes('claude')) return 'sender-claude';
    if (isHerd(s)) return 'sender-herd-' + s;
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
    return 'J';
}

function formatTime(isoString) {
    const d = new Date(isoString);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function appendMessage(msg) {
    const div = document.createElement('div');
    div.className = 'msg ' + getMsgClass(msg.sender_type);
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

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

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
