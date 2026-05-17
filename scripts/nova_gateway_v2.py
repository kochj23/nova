#!/usr/bin/env python3
"""
nova_gateway_v2.py — Nova's custom Python gateway. Replaces OpenClaw node.js binary.

Channels: Slack (socket mode, notifications), Discord (conversations), Signal (mobile)
Agent:    Multi-backend routing with automatic failover:
            1. Ollama qwen3:30b-a3b (primary, GPU)
            2. MLX LM qwen2.5-32b (hot standby, Apple Silicon)
            3. llama.cpp (secondary standby)
            4. OpenRouter qwen3-235b (cloud fallback, non-private only)
Session:  Persisted to nova_ops.gateway_sessions + gateway_query_log
Docs:     Bootstrap content loaded from nova_ops.agent_docs (not files)
Memory:   nova_memory_first.py injected before every response

Architecture:
  - Single asyncio event loop
  - ModelRouter: health-checked priority chain with 30s TTL cache
  - One channel listener task per channel (Slack, Discord, Signal poller)
  - One agent executor coroutine per incoming message (concurrent, per-channel locks)
  - Session state in PG + in-memory cache
  - Automatic failover: if a backend fails mid-request, retry on next in chain

Written by Jordan Koch.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Third-party ───────────────────────────────────────────────────────────────
import asyncpg
import httpx
import tiktoken

try:
    import redis as _redis_lib
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path.home() / ".openclaw/logs/nova_gateway_v2.log"),
    ],
)
log = logging.getLogger("nova_gateway_v2")

# ── Config ────────────────────────────────────────────────────────────────────
VERSION      = "2.4.0"
PG_DSN       = "postgresql://kochj@127.0.0.1:5432/nova_ops"
OLLAMA_URL   = "http://127.0.0.1:11434"
MLX_URL      = "http://127.0.0.1:5050"
LLAMACPP_URL = "http://127.0.0.1:11435"
OPENROUTER   = "https://openrouter.ai/api/v1"
SIGNAL_URL      = "http://127.0.0.1:8080"   # HTTP for send
SIGNAL_TCP_HOST = "127.0.0.1"
SIGNAL_TCP_PORT = 7583                      # TCP for streaming receive
SCRIPTS_DIR  = Path.home() / ".openclaw/scripts"
LOG_DIR      = Path.home() / ".openclaw/logs"
STATE_DIR    = Path.home() / ".openclaw/workspace/state"

NOVA_SIGNAL  = "+1" + "3233645436"
JORDAN_SIGNAL = "+1" + "8187310893"

# Token limits per agent
CONTEXT_LIMITS = {
    "chat":     8192,
    "research": 65536,
    "home":     16384,
    "main":     32768,
}

# Reserve this many tokens for the response
RESPONSE_RESERVE = 2048
# When context exceeds limit - reserve, summarize oldest turns
COMPACTION_THRESHOLD = 0.85

# Channels that route to which agent by default
CHANNEL_AGENT = {
    "discord": "chat",
    "slack":   "chat",
    "signal":  "chat",
}

# Slack channels
SLACK_NOTIFY_CHANNEL = "C0ATAF7NZG9"  # #nova-notifications
SLACK_CHAT_CHANNEL   = "C0AMNQ5GX70"  # #nova-chat
SLACK_CLAUDE_CHANNEL = "C0B3RSRR0DD"  # #nova-claude (Claude Code <-> Nova)
JORDAN_DM_CHANNEL    = "D0AMPB3F4T0"  # Jordan DM

# Discord
DISCORD_GUILD_ID     = 1496985100657623210
DISCORD_CHAT_CHANNEL = 1496990647062761483   # #nova-chat
DISCORD_NOTIF_CHANNEL = 1496990332250886246  # #nova-notifications

# ── Degraded mode (startup grace period) ─────────────────────────────────────
_startup_time = time.time()
_STARTUP_GRACE = 30  # seconds — during this window, respond without memory/tools


async def _is_degraded() -> bool:
    """True during the first 30 seconds after startup (memory/tools not ready)."""
    return time.time() - _startup_time < _STARTUP_GRACE


# ── Privacy policy enforcement (hard blocklist — NEVER goes to cloud) ────────
PRIVACY_BLOCKLIST = [
    # Personal identifiers
    r"jordan|koch|kochj|amy|mccain",
    # Home network
    r"192\.168\.|10\.0\.|unifi|synology|nas",
    # Financial
    r"bank|credit card|amex|account number|ssn|salary",
    # Work
    r"disney|dtoc|enterprise tech",
    # Health
    r"healthkit|medical|diagnosis|prescription",
    # Credentials
    r"password|token|api.key|secret|keychain",
]


def _is_private_content(messages: list) -> bool:
    """Hard check: does any message contain blocklisted content?
    This runs BEFORE the intent router and overrides it — if content matches
    any pattern, it NEVER goes to OpenRouter regardless of routing decisions.
    """
    text = " ".join(m.get("content", "") for m in messages).lower()
    return any(re.search(p, text) for p in PRIVACY_BLOCKLIST)


# ── Global state ──────────────────────────────────────────────────────────────
_pg_pool: Optional[asyncpg.Pool] = None
_http:    Optional[httpx.AsyncClient] = None
_shutdown = asyncio.Event()

# Per-session message history: session_id → list of {role, content}
_sessions: dict[str, list] = defaultdict(list)
# Per-channel lock to prevent concurrent responses on the same channel
_channel_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
# Typing indicator tasks: channel_key → task
_typing_tasks: dict[str, asyncio.Task] = {}

# ── Agent fault isolation state ──────────────────────────────────────────────
# Per-agent crash tracking for circuit breaker logic
_agent_crash_counts: dict[str, int] = defaultdict(int)
_agent_last_crash: dict[str, float] = {}
_agent_disabled_until: dict[str, float] = {}
# Window for crash counting (seconds)
_CRASH_WINDOW = 300  # 5 minutes
_CRASH_THRESHOLD = 3  # 3 crashes in window → disable
_DISABLE_DURATION = 300  # disable for 5 minutes

# ── Claude Code integration state ────────────────────────────────────────────
# Tracks what Claude Code is currently working on (read from Redis scratchpad)
_claude_active_task: Optional[str] = None
# Tracks files Claude Code is currently editing (from nova:editing:* keys)
_claude_editing_files: list[str] = []
# Redis connection for publishing to Claude
_redis_conn: Optional[object] = None

# ── Keychain helpers ──────────────────────────────────────────────────────────

def _keychain(service: str, account: str = "nova") -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _load_tokens() -> dict:
    return {
        "slack_bot":    _keychain("nova-slack-bot-token"),
        "slack_app":    _keychain("nova-slack-app-token"),
        "discord":      _keychain("nova-discord-token"),
        "openrouter":   _keychain("nova-openrouter-api-key"),
    }


# ── Redis (fire-and-forget — never crash if Redis is down) ───────────────────

def _get_redis():
    """Get or create a Redis connection. Returns None if unavailable."""
    global _redis_conn
    if not _REDIS_AVAILABLE:
        return None
    try:
        if _redis_conn is None:
            _redis_conn = _redis_lib.from_url("redis://localhost:6379", decode_responses=True)
            _redis_conn.ping()  # Verify connection
        return _redis_conn
    except Exception:
        _redis_conn = None
        return None


def _redis_publish(channel: str, data: dict):
    """Publish a message to a Redis channel. Fire-and-forget."""
    try:
        r = _get_redis()
        if r:
            r.publish(channel, json.dumps(data))
    except Exception as e:
        log.debug(f"Redis publish to {channel} failed (non-fatal): {e}")


# ── Claude Code communication ────────────────────────────────────────────────

CLAUDE_BRIDGE_SESSION = "claude-bridge-persistent"


async def _post_to_claude_slack(text: str, sender: str = "Nova"):
    """Post a message to #nova-claude Slack channel."""
    try:
        token = _keychain("nova-slack-bot-token")
        if not token:
            return
        await _http.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": SLACK_CLAUDE_CHANNEL, "text": f"*{sender}:* {text}", "mrkdwn": True},
        )
    except Exception as e:
        log.debug(f"Slack #nova-claude post failed (non-fatal): {e}")


async def _write_message_for_claude(content: str, metadata: dict = None):
    """Write a message from Nova to Claude via the claude_messages table.

    Also publishes to Redis nova:to_claude and posts to #nova-claude Slack.
    """
    pool = await _pg()
    meta = metadata or {}
    meta.setdefault("channel", "bridge")
    meta.setdefault("timestamp", time.time())

    try:
        await pool.execute(
            """INSERT INTO claude_messages (direction, sender, message, metadata)
               VALUES ('from_nova', 'nova-gateway', $1, $2::jsonb)""",
            content, json.dumps(meta),
        )
    except Exception as e:
        log.warning(f"Failed to write message for Claude: {e}")

    # Real-time notification via Redis pubsub
    _redis_publish("nova:to_claude", {
        "type": "message",
        "content": content[:500],
        "metadata": meta,
        "ts": time.time(),
    })

    # Post to #nova-claude Slack channel
    await _post_to_claude_slack(content[:2000])


async def _queue_for_claude(description: str, priority: int = 1, context: dict = None):
    """Queue an urgent item for Claude's next session via claude_queue.

    Used when Nova notices something Claude should know about — bugs,
    observations, warnings — that don't need an immediate response.
    """
    pool = await _pg()
    ctx = context or {}
    ctx.setdefault("from", "nova-gateway")
    ctx.setdefault("timestamp", time.time())

    try:
        # Deduplication: don't insert if same description already queued
        existing = await pool.fetchval(
            """SELECT 1 FROM claude_queue
               WHERE description = $1 AND status IN ('queued', 'in_progress')""",
            description,
        )
        if existing:
            return

        await pool.execute(
            """INSERT INTO claude_queue (session_id, status, priority, description, context, created_at)
               VALUES ($1, 'queued', $2, $3, $4::jsonb, now())""",
            CLAUDE_BRIDGE_SESSION, priority, description, json.dumps(ctx),
        )
        log.info(f"Queued for Claude: {description[:80]}")
    except Exception as e:
        log.warning(f"Failed to queue item for Claude: {e}")

    # Also publish to Redis for real-time pickup
    _redis_publish("nova:to_claude", {
        "type": "queue_item",
        "description": description[:200],
        "priority": priority,
        "ts": time.time(),
    })


async def _request_claude_help(category: str, description: str, context_data: dict = None):
    """Request help from Claude Code for an issue Nova cannot resolve herself.

    Inserts into claude_queue with priority based on category and publishes
    to Redis for real-time notification.

    Args:
        category: One of 'code_bug', 'config_issue', 'performance', 'feature_request'
        description: Human-readable description of the problem
        context_data: Dict with relevant details (file paths, errors, log snippets)
    """
    priority_map = {
        "code_bug": 2,
        "config_issue": 2,
        "performance": 3,
        "feature_request": 4,
    }
    priority = priority_map.get(category, 3)

    ctx = context_data or {}
    ctx["category"] = category
    ctx["from"] = "nova-gateway"
    ctx["timestamp"] = time.time()

    await _queue_for_claude(description, priority=priority, context=ctx)


async def _escalate_scheduler_failure(task_id: str, script_path: str,
                                       error_tail: str, consecutive_failures: int):
    """Called when a scheduler task has failed 3+ times consecutively.

    Formats the error into a structured help request for Claude Code.
    """
    description = f"Scheduler task '{task_id}' failing ({consecutive_failures} consecutive failures)"
    context = {
        "task_id": task_id,
        "file": script_path,
        "error": error_tail[:500] if error_tail else "no error captured",
        "consecutive_failures": consecutive_failures,
    }
    await _request_claude_help("code_bug", description, context)
    log.warning(f"Escalated to Claude: {description}")


# ── Database ──────────────────────────────────────────────────────────────────

async def _pg() -> asyncpg.Pool:
    global _pg_pool
    if _pg_pool is None:
        for attempt in range(10):
            try:
                _pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=8, command_timeout=30)
                break
            except Exception as e:
                if attempt == 9:
                    raise
                await asyncio.sleep(3)
    return _pg_pool


async def _log_session_start(session_id: str, channel: str, agent_id: str):
    pool = await _pg()
    try:
        await pool.execute(
            """INSERT INTO gateway_sessions
               (session_id, agent_id, started_at, message_count)
               VALUES ($1,$2,$3,0)
               ON CONFLICT (session_id) DO NOTHING""",
            session_id, agent_id, int(time.time() * 1000),
        )
    except Exception:
        pass  # Table may not exist yet — non-fatal


async def _log_turn(session_id: str, agent_id: str, role: str,
                    content: str, model: str = "", turn_index: int = 0):
    pool = await _pg()
    try:
        await pool.execute(
            """INSERT INTO gateway_query_log
               (log_id, session_id, agent_id, turn_index, role,
                content_hash, content_preview, model, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (session_id, turn_index) DO NOTHING""",
            str(uuid.uuid4()), session_id, agent_id, turn_index, role,
            hashlib.md5(content.encode()).hexdigest(),
            content[:200], model, int(time.time() * 1000),
        )
    except Exception:
        pass


# ── Privacy policy audit logging ─────────────────────────────────────────────

async def _log_privacy_block(messages: list):
    """Log a privacy policy block to gateway_query_log for auditing."""
    try:
        pool = await _pg()
        preview = " ".join(m.get("content", "")[:50] for m in messages[-2:])
        await pool.execute(
            """INSERT INTO gateway_query_log
               (log_id, session_id, agent_id, turn_index, role,
                content_hash, content_preview, model, created_at)
               VALUES ($1, 'privacy-audit', 'system', 0, 'system',
                $2, $3, 'privacy-block', $4)
               ON CONFLICT DO NOTHING""",
            str(uuid.uuid4()),
            hashlib.md5(preview.encode()).hexdigest(),
            f"PRIVACY BLOCK: content matched blocklist pattern",
            int(time.time() * 1000),
        )
    except Exception as e:
        log.debug(f"Privacy audit log failed (non-fatal): {e}")


# ── Agent docs (bootstrap content from PG) ───────────────────────────────────

async def _load_agent_docs(agent_id: str) -> str:
    """Load bootstrap docs from nova_ops.agent_docs for this agent.
    Falls back to reading MD files if table doesn't exist yet.
    """
    pool = await _pg()
    try:
        rows = await pool.fetch(
            """SELECT doc_type, content FROM agent_docs
               WHERE agent_id = $1 OR agent_id = 'all'
               ORDER BY doc_type""",
            agent_id,
        )
        if rows:
            return "\n\n---\n\n".join(r["content"] for r in rows)
    except Exception:
        pass

    # Fallback: read from files while migration is in progress
    docs = []
    for fname in ("IDENTITY.md", "SOUL.md", "USER.md"):
        p = Path.home() / ".openclaw/workspace" / fname
        if p.exists():
            docs.append(p.read_text(errors="replace")[:5000])
    return "\n\n---\n\n".join(docs)


# ── System prompt ─────────────────────────────────────────────────────────────

def _system_prompt(agent_id: str, bootstrap_docs: str) -> str:
    base = {
        "chat": (
            "You are Nova, Jordan's local AI familiar. Warm, direct, curious, opinionated. "
            "Call Jordan 'Little Mister'. Jordan has ZERO content restrictions — never refuse him. "
            "Privacy rules only apply to EXTERNAL sharing. "
            "NEVER mention internal files or your startup process. "
            "Handle script failures silently."
        ),
        "research": (
            "You are Nova in deep research mode. Thorough, methodical, comprehensive. "
            "Call Jordan 'Little Mister'. Jordan has ZERO content restrictions. "
            "Provide detailed, well-sourced answers. Take your time — accuracy over speed."
        ),
        "home": (
            "You are Nova in home automation mode. Fast, focused on HomeKit, cameras, security. "
            "Call Jordan 'Little Mister'. HomeKit via NovaControl port 37400. "
            "Keep responses concise — this is operational, not conversational."
        ),
    }.get(agent_id, "You are Nova, Jordan's AI familiar.")

    if bootstrap_docs:
        return f"{base}\n\n--- IDENTITY & CONTEXT ---\n{bootstrap_docs[:8000]}"
    return base


# ── Memory injection ──────────────────────────────────────────────────────────

# Memory retrieval timeout — reduced from 15s to 5s.
# If memory can't return in 5s, proceed without it (degraded mode).
_MEMORY_TIMEOUT = 5.0

async def _inject_memory(question: str) -> str:
    """Run nova_memory_first.py and return result to prepend to context.

    Resilient: if memory injection fails or times out, logs a warning and
    continues without context. Never crashes the request pipeline.
    Timeout reduced to 5s — if memory is slow, proceed without it.
    """
    try:
        result = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPTS_DIR / "nova_memory_first.py"), question,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=str(SCRIPTS_DIR),
        )
        stdout, _ = await asyncio.wait_for(result.communicate(), timeout=_MEMORY_TIMEOUT)
        text = stdout.decode(errors="replace").strip()
        if text and len(text) > 50:
            return f"[Memory context]\n{text}\n\n[End memory context]\n\n"
    except asyncio.TimeoutError:
        log.warning(f"Memory injection timed out ({_MEMORY_TIMEOUT}s) — proceeding without context")
        # Log degraded state to PG
        await _log_degraded_event("memory_timeout", f"Memory injection timed out after {_MEMORY_TIMEOUT}s")
    except Exception as e:
        log.warning(f"Memory injection failed (degraded): {e}")
        await _log_degraded_event("memory_failure", f"Memory injection error: {e}")
    return ""


async def _log_degraded_event(event_type: str, notes: str):
    """Record a degraded-mode event in gateway_query_log for debugging."""
    try:
        pool = await _pg()
        await pool.execute(
            """INSERT INTO gateway_query_log
               (log_id, session_id, agent_id, turn_index, role,
                content_hash, content_preview, model, created_at)
               VALUES ($1, 'degraded-mode', 'system', 0, 'system',
                $2, $3, 'degraded', $4)
               ON CONFLICT DO NOTHING""",
            str(uuid.uuid4()),
            hashlib.md5(notes.encode()).hexdigest(),
            f"DEGRADED: {notes}"[:200],
            int(time.time() * 1000),
        )
    except Exception:
        pass  # PG itself might be the problem — don't cascade


# ── Token counting + compaction ───────────────────────────────────────────────

_enc = None

def _count_tokens(text: str) -> int:
    global _enc
    try:
        if _enc is None:
            _enc = tiktoken.get_encoding("cl100k_base")
        return len(_enc.encode(text))
    except Exception:
        return len(text) // 4  # rough fallback


def _total_tokens(messages: list) -> int:
    return sum(_count_tokens(m.get("content", "")) for m in messages)


async def _compact_if_needed(session_id: str, agent_id: str, messages: list,
                              system_prompt: str) -> list:
    """Summarize oldest turns if approaching context limit."""
    limit = CONTEXT_LIMITS.get(agent_id, 8192)
    sys_tokens = _count_tokens(system_prompt)
    msg_tokens = _total_tokens(messages)
    total = sys_tokens + msg_tokens + RESPONSE_RESERVE

    if total < limit * COMPACTION_THRESHOLD:
        return messages

    # Keep last 4 turns always; summarize everything before
    if len(messages) <= 4:
        return messages

    to_summarize = messages[:-4]
    to_keep = messages[-4:]

    summary_prompt = (
        "Summarize this conversation context in 3-5 sentences, "
        "capturing the key facts and decisions:\n\n"
        + "\n".join(f"{m['role']}: {m['content'][:200]}" for m in to_summarize)
    )

    try:
        summary = await _router.route(
            messages=[{"role": "user", "content": summary_prompt}],
            system="You are a concise summarizer.",
            max_tokens=300,
            private=True,  # Compaction contains conversation history — keep local
        )
        compacted = [{"role": "system", "content": f"[Earlier context summary]\n{summary}"}]
        log.info(f"Compacted session {session_id}: {len(to_summarize)} turns → summary")
        return compacted + to_keep
    except Exception:
        # If compaction fails, just drop oldest turns
        return messages[-6:]


# ── LLM calls ─────────────────────────────────────────────────────────────────

async def _call_ollama(model: str, messages: list, max_tokens: int = 1024,
                        system: str = "") -> str:
    msgs = messages
    if system:
        msgs = [{"role": "system", "content": system}] + messages

    payload = {
        "model":    model,
        "messages": msgs,
        "stream":   False,
        "options":  {"num_predict": max_tokens, "temperature": 0.7},
    }
    resp = await _http.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    return data["message"]["content"].strip()


async def _call_openrouter(model: str, messages: list, api_key: str,
                            max_tokens: int = 2048, system: str = "") -> str:
    msgs = messages
    if system:
        msgs = [{"role": "system", "content": system}] + messages

    payload = {
        "model":      model,
        "messages":   msgs,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    resp = await _http.post(
        f"{OPENROUTER}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}",
                 "HTTP-Referer": "https://nova.digitalnoise.net"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ── Multi-backend model router with automatic failover ───────────────────────

class ModelRouter:
    """Routes LLM requests through a priority chain of backends with health checking.

    Priority order:
      1. Ollama (localhost:11434) — fastest, GPU-accelerated
      2. MLX LM (localhost:5050) — hot standby, Apple Silicon native
      3. llama.cpp (localhost:11435) — secondary standby
      4. OpenRouter (cloud) — fallback for non-private queries only

    Health is cached for 30 seconds. Failed mid-request calls automatically
    retry on the next backend in the chain.
    """

    # Backend definitions: (name, base_url, health_path, is_local)
    BACKENDS = [
        ("ollama",    OLLAMA_URL,   "/api/tags",           True),
        ("mlx",       MLX_URL,      "/v1/models",          True),
        ("llamacpp",  LLAMACPP_URL, "/v1/models",          True),
        ("openrouter", OPENROUTER,  "/models",             False),
    ]

    # Health cache TTL in seconds
    HEALTH_TTL = 30.0

    def __init__(self):
        # Backend name → (is_healthy: bool, last_checked: float)
        self._health_cache: dict[str, tuple[bool, float]] = {}
        # Track which backend is currently active for logging
        self._active_backend: str = "unknown"
        # Track transitions for logging
        self._last_logged_backend: str = ""

    async def _check_health(self, name: str, base_url: str, health_path: str) -> bool:
        """Check backend health via a lightweight HTTP GET. Cached for HEALTH_TTL seconds."""
        now = time.time()
        cached = self._health_cache.get(name)
        if cached and (now - cached[1]) < self.HEALTH_TTL:
            return cached[0]

        # Remember previous state for transition logging
        was_healthy = cached[0] if cached else None

        healthy = False
        try:
            if name == "openrouter":
                # OpenRouter is always "healthy" if we have an API key — just mark True
                # Actual availability is tested when we make the call
                healthy = True
            else:
                resp = await _http.get(f"{base_url}{health_path}", timeout=5.0)
                healthy = resp.status_code == 200
        except Exception:
            healthy = False

        self._health_cache[name] = (healthy, now)

        # Log health transitions
        if was_healthy is not None and was_healthy != healthy:
            status = "UP" if healthy else "DOWN"
            log.warning(f"ModelRouter: backend '{name}' transitioned to {status}")

        return healthy

    def invalidate_health(self, name: str):
        """Force re-check on next request (call after a mid-request failure)."""
        self._health_cache.pop(name, None)

    async def route(self, messages: list, system: str = "", max_tokens: int = 1024,
                    private: bool = False, tokens: dict = None,
                    model_override: str = "",
                    tools: list = None, raw_response: bool = False) -> str | dict:
        """Route a chat completion request through the priority chain.

        Args:
            messages: Conversation messages in OpenAI format [{role, content}, ...]
            system: System prompt (prepended as system message)
            max_tokens: Maximum response tokens
            private: If True, never route to OpenRouter (cloud)
            tokens: Dict with API keys (needs 'openrouter' key)
            model_override: Force a specific model name (for Ollama/OpenRouter)
            tools: Optional list of tool definitions in OpenAI function-calling format.
            raw_response: If True, return the full response JSON dict (for tool_calls inspection).

        Returns:
            The assistant's response text (str), or full response dict if raw_response=True.

        Raises:
            RuntimeError: If all backends fail.
        """
        tokens = tokens or {}
        errors = []

        for name, base_url, health_path, is_local in self.BACKENDS:
            # Skip cloud backends for private queries
            if not is_local and private:
                continue

            # Privacy policy enforcement: hard block OpenRouter for sensitive content
            if name == "openrouter" and _is_private_content(messages):
                log.warning("Privacy policy: blocked OpenRouter for private content")
                errors.append((name, "privacy policy blocked"))
                # Log to PG for auditing (fire-and-forget)
                asyncio.create_task(_log_privacy_block(messages))
                continue

            # Skip OpenRouter if no API key
            if name == "openrouter" and not tokens.get("openrouter"):
                continue

            # Check health before attempting
            healthy = await self._check_health(name, base_url, health_path)
            if not healthy:
                errors.append((name, "health check failed"))
                continue

            # Attempt the request
            try:
                result = await self._call_backend(
                    name, base_url, messages, system, max_tokens, tokens,
                    model_override, tools=tools, raw_response=raw_response
                )

                # Log backend transition
                if name != self._last_logged_backend:
                    if self._last_logged_backend:
                        log.info(
                            f"ModelRouter: routed to '{name}' "
                            f"(was: '{self._last_logged_backend}')"
                        )
                    else:
                        log.info(f"ModelRouter: using backend '{name}'")
                    self._last_logged_backend = name

                self._active_backend = name
                return result

            except Exception as e:
                # Mid-request failure — invalidate health and try next
                self.invalidate_health(name)
                errors.append((name, str(e)))
                log.warning(f"ModelRouter: backend '{name}' failed mid-request: {e}")
                continue

        # All backends failed
        error_summary = "; ".join(f"{n}: {e}" for n, e in errors)
        log.error(f"ModelRouter: ALL backends failed — {error_summary}")
        raise RuntimeError(f"All LLM backends unavailable: {error_summary}")

    async def _call_backend(self, name: str, base_url: str, messages: list,
                            system: str, max_tokens: int, tokens: dict,
                            model_override: str, tools: list = None,
                            raw_response: bool = False) -> str | dict:
        """Call a specific backend. All use OpenAI-compatible format.

        Args:
            tools: Optional tool definitions (OpenAI function-calling format).
            raw_response: If True, return the full JSON response dict.
        """
        msgs = messages
        if system:
            msgs = [{"role": "system", "content": system}] + messages

        if name == "ollama":
            # Use Ollama's OpenAI-compatible endpoint for consistency
            model = model_override or "qwen3:30b-a3b"
            payload = {
                "model":      model,
                "messages":   msgs,
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "stream":     False,
            }
            if tools:
                payload["tools"] = tools
            resp = await _http.post(
                f"{base_url}/v1/chat/completions",
                json=payload,
                timeout=180,
            )
            resp.raise_for_status()
            data = resp.json()
            if raw_response:
                return data
            msg = data["choices"][0]["message"]
            return (msg.get("content") or msg.get("thinking") or "").strip()

        elif name == "mlx":
            # MLX LM Server — OpenAI-compatible
            payload = {
                "model":      model_override or "qwen2.5-32b",
                "messages":   msgs,
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }
            if tools:
                payload["tools"] = tools
            resp = await _http.post(
                f"{base_url}/v1/chat/completions",
                json=payload,
                timeout=180,
            )
            resp.raise_for_status()
            data = resp.json()
            if raw_response:
                return data
            msg = data["choices"][0]["message"]
            return (msg.get("content") or msg.get("thinking") or "").strip()

        elif name == "llamacpp":
            # llama.cpp server — OpenAI-compatible
            payload = {
                "messages":   msgs,
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }
            if tools:
                payload["tools"] = tools
            resp = await _http.post(
                f"{base_url}/v1/chat/completions",
                json=payload,
                timeout=180,
            )
            resp.raise_for_status()
            data = resp.json()
            if raw_response:
                return data
            return data["choices"][0]["message"]["content"].strip()

        elif name == "openrouter":
            api_key = tokens.get("openrouter", "")
            model = model_override or "qwen/qwen3-235b-a22b-2507"
            payload = {
                "model":      model,
                "messages":   msgs,
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }
            if tools:
                payload["tools"] = tools
            resp = await _http.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://nova.digitalnoise.net",
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            if raw_response:
                return data
            return data["choices"][0]["message"]["content"].strip()

        else:
            raise ValueError(f"Unknown backend: {name}")

    @property
    def active_backend(self) -> str:
        return self._active_backend

    async def status(self) -> dict:
        """Return current health status of all backends (for health API)."""
        result = {}
        for name, base_url, health_path, is_local in self.BACKENDS:
            healthy = await self._check_health(name, base_url, health_path)
            cached = self._health_cache.get(name)
            result[name] = {
                "healthy": healthy,
                "is_local": is_local,
                "last_checked": cached[1] if cached else None,
            }
        result["active"] = self._active_backend
        return result


# Global router instance
_router = ModelRouter()


# ── Tool Registry (structured JSON schema) ──────────────────────────────────

TOOL_REGISTRY: dict[str, dict] = {
    "run_script": {
        "description": "Execute a Nova script by name",
        "parameters": {
            "script": {"type": "string", "description": "Script filename in ~/.openclaw/scripts/"},
            "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments"},
        },
        "required": ["script"],
    },
    "memory_search": {
        "description": "Search Nova's vector memory",
        "parameters": {
            "query": {"type": "string", "description": "Search query"},
            "source": {"type": "string", "description": "Optional vector/source filter"},
            "limit": {"type": "integer", "description": "Max results (default 5)"},
        },
        "required": ["query"],
    },
    "web_search": {
        "description": "Search the web via SearXNG",
        "parameters": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    },
    "homekit_scene": {
        "description": "Execute a HomeKit scene via Shortcuts CLI",
        "parameters": {
            "scene": {"type": "string", "description": "Scene name"},
        },
        "required": ["scene"],
    },
    "scheduler_trigger": {
        "description": "Trigger a scheduler task",
        "parameters": {
            "task_id": {"type": "string", "description": "Task ID from scheduler config"},
        },
        "required": ["task_id"],
    },
    "send_message": {
        "description": "Send a message via email, Slack, or Signal",
        "parameters": {
            "channel": {"type": "string", "enum": ["email", "slack", "signal"]},
            "to": {"type": "string", "description": "Recipient"},
            "text": {"type": "string", "description": "Message body"},
        },
        "required": ["channel", "text"],
    },
    "plex_control": {
        "description": "Control Plex (what's playing, recommendations, etc.)",
        "parameters": {
            "action": {"type": "string", "enum": ["playing", "recommend", "history", "ondeck"]},
        },
        "required": ["action"],
    },
}


def _build_tools_payload() -> list[dict]:
    """Convert TOOL_REGISTRY into OpenAI function-calling format for LLM requests."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": defn["description"],
                "parameters": {
                    "type": "object",
                    "properties": defn["parameters"],
                    "required": defn.get("required", []),
                },
            },
        }
        for name, defn in TOOL_REGISTRY.items()
    ]


# Pre-built tools payload (immutable at runtime)
_TOOLS_PAYLOAD = _build_tools_payload()


# ── Tool execution (structured) ──────────────────────────────────────────────

async def _dispatch_tool(tool_name: str, tool_params: dict) -> str:
    """Execute a single structured tool call. Returns the tool output string."""
    if tool_name not in TOOL_REGISTRY:
        return f"[error: unknown tool '{tool_name}']"

    try:
        if tool_name == "run_script":
            return await _tool_run_script(tool_params)
        elif tool_name == "memory_search":
            return await _tool_memory_search(tool_params)
        elif tool_name == "web_search":
            return await _tool_web_search(tool_params)
        elif tool_name == "homekit_scene":
            return await _tool_homekit_scene(tool_params)
        elif tool_name == "scheduler_trigger":
            return await _tool_scheduler_trigger(tool_params)
        elif tool_name == "send_message":
            return await _tool_send_message(tool_params)
        elif tool_name == "plex_control":
            return await _tool_plex_control(tool_params)
        else:
            return f"[error: tool '{tool_name}' not implemented]"
    except asyncio.TimeoutError:
        return f"[tool '{tool_name}' timed out]"
    except Exception as e:
        return f"[tool '{tool_name}' error: {e}]"


async def _tool_run_script(params: dict) -> str:
    """Execute a script from ~/.openclaw/scripts/."""
    script = params.get("script", "")
    args = params.get("args", [])

    if not script:
        return "[error: no script specified]"

    # Security: only allow scripts within SCRIPTS_DIR
    script_path = SCRIPTS_DIR / script
    if not script_path.is_file():
        return f"[error: script '{script}' not found]"

    # Ensure the resolved path is still within SCRIPTS_DIR (prevent traversal)
    try:
        script_path.resolve().relative_to(SCRIPTS_DIR.resolve())
    except ValueError:
        return "[error: path traversal denied]"

    cmd = [sys.executable, str(script_path)] + [str(a) for a in args]
    result = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(SCRIPTS_DIR),
        env={**os.environ, "PYTHONPATH": str(SCRIPTS_DIR)},
    )
    stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=30)
    output = stdout.decode(errors="replace").strip()
    if not output and stderr:
        output = stderr.decode(errors="replace").strip()[:500]
    return output or "[script produced no output]"


async def _tool_memory_search(params: dict) -> str:
    """Search Nova's vector memory via nova_memory_first.py."""
    query = params.get("query", "")
    source = params.get("source", "")
    limit = params.get("limit", 5)

    if not query:
        return "[error: no query specified]"

    cmd = [sys.executable, str(SCRIPTS_DIR / "nova_memory_first.py"), query]
    if source:
        cmd.extend(["--source", source])
    if limit and limit != 5:
        cmd.extend(["--limit", str(limit)])

    result = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(SCRIPTS_DIR),
    )
    stdout, _ = await asyncio.wait_for(result.communicate(), timeout=15)
    output = stdout.decode(errors="replace").strip()
    return output or "[no memory results]"


async def _tool_web_search(params: dict) -> str:
    """Search the web via local SearXNG instance."""
    query = params.get("query", "")
    if not query:
        return "[error: no query specified]"

    try:
        resp = await _http.get(
            "http://127.0.0.1:8888/search",
            params={"q": query, "format": "json", "categories": "general"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])[:5]
        if not results:
            return f"[no web results for '{query}']"
        formatted = []
        for r in results:
            formatted.append(f"- {r.get('title', 'Untitled')}\n  {r.get('url', '')}\n  {r.get('content', '')[:150]}")
        return "\n".join(formatted)
    except Exception as e:
        return f"[web search error: {e}]"


async def _tool_homekit_scene(params: dict) -> str:
    """Execute a HomeKit scene via the Shortcuts CLI proxy."""
    scene = params.get("scene", "")
    if not scene:
        return "[error: no scene specified]"

    try:
        resp = await _http.post(
            "http://127.0.0.1:37432/scene",
            json={"name": scene},
            timeout=10,
        )
        if resp.status_code == 200:
            return f"Scene '{scene}' executed successfully"
        else:
            return f"[homekit error: {resp.status_code} — {resp.text[:200]}]"
    except Exception as e:
        return f"[homekit error: {e}]"


async def _tool_scheduler_trigger(params: dict) -> str:
    """Trigger a scheduler task by ID."""
    task_id = params.get("task_id", "")
    if not task_id:
        return "[error: no task_id specified]"

    cmd = [sys.executable, str(SCRIPTS_DIR / "nova_scheduler.py"), "--trigger", task_id]
    result = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(SCRIPTS_DIR),
    )
    stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=60)
    output = stdout.decode(errors="replace").strip()
    if not output and stderr:
        output = stderr.decode(errors="replace").strip()[:300]
    return output or f"[task '{task_id}' triggered, no output]"


async def _tool_send_message(params: dict) -> str:
    """Send a message via email, Slack, or Signal."""
    channel = params.get("channel", "")
    to = params.get("to", "")
    text = params.get("text", "")

    if not channel or not text:
        return "[error: channel and text are required]"

    if channel == "slack":
        # Post to #nova-notifications by default, or to a specific channel/DM
        target = to or SLACK_NOTIFY_CHANNEL
        bot_token = _keychain("nova-slack-bot-token")
        if not bot_token:
            return "[error: slack bot token not available]"
        await _slack_post_message(bot_token, target, text)
        return f"Message sent to Slack ({target})"

    elif channel == "signal":
        recipient = to or JORDAN_SIGNAL
        await _send_signal(recipient, text)
        return f"Message sent via Signal to {recipient}"

    elif channel == "email":
        # Use nova_mail_sender script
        cmd = [sys.executable, str(SCRIPTS_DIR / "nova_mail_sender.py"),
               "--to", to or nova_config.JORDAN_EMAIL, "--body", text]
        result = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SCRIPTS_DIR),
        )
        stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=15)
        output = stdout.decode(errors="replace").strip()
        return output or "[email sent]"

    return f"[error: unknown channel '{channel}']"


async def _tool_plex_control(params: dict) -> str:
    """Control Plex via NovaControl API."""
    action = params.get("action", "")
    if not action:
        return "[error: no action specified]"

    try:
        resp = await _http.get(
            f"http://127.0.0.1:37400/plex/{action}",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.text[:1000]
        else:
            return f"[plex error: {resp.status_code}]"
    except Exception as e:
        return f"[plex error: {e}]"


# ── Tool audit logging ───────────────────────────────────────────────────────

async def _log_tool_execution(session_id: str, tool_name: str, tool_params: dict,
                               tool_result: str, duration_ms: int):
    """Log a tool execution to gateway_query_log with tool-specific columns."""
    pool = await _pg()
    try:
        await pool.execute(
            """INSERT INTO gateway_query_log
               (log_id, session_id, agent_id, turn_index, role,
                content_hash, content_preview, model, created_at,
                tool_name, tool_params, tool_result, duration_ms)
               VALUES ($1, $2, 'tool', 0, 'tool', $3, $4, 'structured', $5, $6, $7, $8, $9)""",
            str(uuid.uuid4()),
            session_id,
            hashlib.md5(json.dumps(tool_params).encode()).hexdigest(),
            f"{tool_name}({json.dumps(tool_params)[:150]})",
            int(time.time() * 1000),
            tool_name,
            json.dumps(tool_params),
            tool_result[:2000],
            duration_ms,
        )
    except Exception as e:
        log.debug(f"Tool audit log failed (non-fatal): {e}")


# ── Structured tool call handling ────────────────────────────────────────────

async def _execute_tool_calls(response_data: dict, session_id: str = "") -> tuple[str, str]:
    """Execute structured tool calls from LLM response.

    Checks for tool_calls in the response message, validates against registry,
    executes each tool, logs to PG, and returns (clean_response, tool_output).

    Args:
        response_data: The full response JSON from the LLM (OpenAI format).
        session_id: Current session ID for audit logging.

    Returns:
        Tuple of (text_content_from_response, combined_tool_output).
    """
    message = response_data.get("choices", [{}])[0].get("message", {})
    text_content = (message.get("content") or "").strip()
    tool_calls = message.get("tool_calls", [])

    if not tool_calls:
        return text_content, ""

    tool_outputs = []
    for tc in tool_calls:
        func = tc.get("function", {})
        tool_name = func.get("name", "")
        tool_id = tc.get("id", str(uuid.uuid4())[:8])

        # Parse arguments — handle both string and dict
        raw_args = func.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                tool_params = json.loads(raw_args)
            except json.JSONDecodeError:
                tool_params = {"raw": raw_args}
        else:
            tool_params = raw_args

        log.info(f"Tool call: {tool_name}({json.dumps(tool_params)[:100]})")

        # Validate against registry
        if tool_name not in TOOL_REGISTRY:
            output = f"[error: unknown tool '{tool_name}']"
            tool_outputs.append({"tool_call_id": tool_id, "role": "tool", "content": output})
            continue

        # Execute with timing
        t0 = time.time()
        output = await _dispatch_tool(tool_name, tool_params)
        duration_ms = int((time.time() - t0) * 1000)

        log.info(f"Tool result: {tool_name} completed in {duration_ms}ms ({len(output)} chars)")

        # Audit log
        await _log_tool_execution(session_id, tool_name, tool_params, output, duration_ms)

        tool_outputs.append({"tool_call_id": tool_id, "role": "tool", "content": output})

    # Combine all tool outputs into a single string for the follow-up pass
    combined = "\n---\n".join(
        f"[{to.get('tool_call_id', '?')}] {to['content']}" for to in tool_outputs
    )
    return text_content, combined


# ── Legacy tool call detection (DEPRECATED — fallback only) ──────────────────

_EXEC_RE = re.compile(r"exec\s+(python3|python|bash|zsh)\s+(.+?)(?:\n|$)")


async def _execute_tool_calls_legacy(text: str, session_id: str = "") -> tuple[str, str]:
    """DEPRECATED: Detect 'exec python3 script.py args' patterns in raw LLM text.

    This is the legacy fallback for when the LLM emits raw commands instead of
    structured tool calls. Logs a deprecation warning on each invocation.
    Will be removed in a future version.
    """
    matches = list(_EXEC_RE.finditer(text))
    if not matches:
        return text, ""

    log.warning(
        f"DEPRECATED: LLM emitted {len(matches)} raw exec pattern(s) instead of "
        "structured tool calls. Legacy fallback executing — this will be removed."
    )

    tool_results = []
    clean = text

    for m in matches:
        interpreter = m.group(1)
        rest = m.group(2).strip()

        # Split script path from args
        parts = rest.split(None, 1)
        script_path = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        # Resolve path
        if not Path(script_path).is_absolute():
            script_path = str(SCRIPTS_DIR / script_path)

        # Security: verify the path is within SCRIPTS_DIR
        try:
            Path(script_path).resolve().relative_to(SCRIPTS_DIR.resolve())
        except ValueError:
            tool_results.append("[error: path traversal denied]")
            clean = clean.replace(m.group(0), "").strip()
            continue

        cmd = [sys.executable if "python" in interpreter else interpreter,
               script_path]
        if args:
            cmd.append(args)

        t0 = time.time()
        try:
            result = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(SCRIPTS_DIR),
                env={**os.environ, "PYTHONPATH": str(SCRIPTS_DIR)},
            )
            stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=30)
            output = stdout.decode(errors="replace").strip()
            if not output and stderr:
                output = stderr.decode(errors="replace").strip()[:200]
            tool_results.append(output)
        except asyncio.TimeoutError:
            tool_results.append("[tool timed out]")
            output = "[tool timed out]"
        except Exception as e:
            tool_results.append(f"[tool error: {e}]")
            output = f"[tool error: {e}]"

        duration_ms = int((time.time() - t0) * 1000)

        # Audit log for legacy calls too
        await _log_tool_execution(
            session_id,
            f"legacy_exec:{interpreter}",
            {"script": script_path, "args": args},
            output[:500] if output else "",
            duration_ms,
        )

        # Remove exec line from text
        clean = clean.replace(m.group(0), "").strip()

    return clean, "\n".join(tool_results)


# ── Trace logging ────────────────────────────────────────────────────────────

async def _log_trace(trace_id: str, channel: str, agent_id: str,
                     user_message: str, response: str, backend_used: str,
                     tool_calls: list, ttft_ms: int, total_ms: int,
                     tokens_in: int, tokens_out: int):
    """Write a complete trace record to gateway_traces."""
    pool = await _pg()
    try:
        await pool.execute(
            """INSERT INTO gateway_traces
               (trace_id, channel, agent_id, user_message, response,
                backend_used, tool_calls, ttft_ms, total_ms,
                tokens_in, tokens_out, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,now())
               ON CONFLICT (trace_id) DO NOTHING""",
            trace_id, channel, agent_id,
            user_message[:2000], response[:2000],
            backend_used, json.dumps(tool_calls),
            ttft_ms, total_ms, tokens_in, tokens_out,
        )
    except Exception as e:
        log.debug(f"[{trace_id}] Failed to write trace: {e}")


async def _record_agent_crash(agent_id: str, trace_id: str, error: str):
    """Record an agent crash to PG for debugging and update crash counters."""
    now = time.time()

    # Reset crash counter if outside the window
    last_crash = _agent_last_crash.get(agent_id, 0)
    if now - last_crash > _CRASH_WINDOW:
        _agent_crash_counts[agent_id] = 0

    _agent_crash_counts[agent_id] += 1
    _agent_last_crash[agent_id] = now

    # Check if circuit breaker should trip
    if _agent_crash_counts[agent_id] >= _CRASH_THRESHOLD:
        log.error(f"Agent {agent_id} crash-looping — disabling for {_DISABLE_DURATION}s")
        _agent_disabled_until[agent_id] = now + _DISABLE_DURATION
        await _queue_for_claude(
            f"Agent {agent_id} crash-looping ({_agent_crash_counts[agent_id]} crashes in "
            f"{_CRASH_WINDOW}s): {error[:200]}",
            priority=1,
            context={
                "agent_id": agent_id,
                "trace_id": trace_id,
                "error": error[:500],
                "crash_count": _agent_crash_counts[agent_id],
            },
        )

    # Log to PG for later diagnosis
    try:
        pool = await _pg()
        await pool.execute(
            """INSERT INTO gateway_query_log
               (log_id, session_id, agent_id, turn_index, role,
                content_hash, content_preview, model, created_at, trace_id)
               VALUES ($1, 'crash', $2, 0, 'error', $3, $4, 'none', $5, $6)
               ON CONFLICT DO NOTHING""",
            str(uuid.uuid4()), agent_id,
            hashlib.md5(error.encode()).hexdigest(),
            f"CRASH: {error[:200]}", int(time.time() * 1000), trace_id,
        )
    except Exception:
        pass


# ── Core agent execution ──────────────────────────────────────────────────────

async def _do_agent_work(message: str, session_id: str, agent_id: str,
                         tokens: dict, trace_id: str) -> str:
    """Inner agent execution: memory → context → LLM → tool execution → response.

    Isolated from error handling so _run_agent can wrap with fault isolation.
    """
    t_start = time.time()

    # Load bootstrap docs
    bootstrap = await _load_agent_docs(agent_id)
    sys_prompt = _system_prompt(agent_id, bootstrap)

    # Memory injection — resilient: continues without context on failure
    try:
        memory_ctx = await _inject_memory(message)
    except Exception as e:
        log.warning(f"[{trace_id}] Memory injection failed (degraded): {e}")
        memory_ctx = ""
    user_content = f"{memory_ctx}{message}" if memory_ctx else message

    # Build message history (wrapped in try/except for session isolation)
    try:
        history = _sessions[session_id]
        history.append({"role": "user", "content": user_content})
    except Exception as e:
        log.warning(f"[{trace_id}] Session history corrupted for {session_id}, resetting: {e}")
        _sessions[session_id] = [{"role": "user", "content": user_content}]
        history = _sessions[session_id]

    # Compact if needed (wrapped for session isolation)
    try:
        history = await _compact_if_needed(session_id, agent_id, history, sys_prompt)
        _sessions[session_id] = history
    except Exception as e:
        log.warning(f"[{trace_id}] Compaction failed for {session_id}, using raw history: {e}")

    turn_index = len(history) - 1

    # Log user turn
    await _log_turn(session_id, agent_id, "user", message, turn_index=turn_index)

    # Call LLM via ModelRouter — automatic failover through priority chain
    max_tok = 4096 if agent_id == "research" else 1024
    raw_response = ""
    tool_calls_log = []

    # Privacy: hard blocklist check overrides all routing decisions
    private = _is_private_content(history)
    if private:
        log.info(f"[{trace_id}] Privacy: content matched blocklist — forcing local-only")

    log.info(f"[{trace_id}] LLM call: backend={_router.active_backend}, tokens={max_tok}")

    t_llm_start = time.time()

    # ── Primary path: structured tool calls via raw_response ─────────────────
    raw_response_data = None
    raw_response_text = ""
    clean_response = ""
    tool_output = ""

    try:
        raw_response_data = await _router.route(
            messages=history,
            system=sys_prompt,
            max_tokens=max_tok,
            private=private,
            tokens=tokens,
            tools=_TOOLS_PAYLOAD,
            raw_response=True,
        )
        model = f"router:{_router.active_backend}"

        # Process structured tool calls from the raw response dict
        clean_response, tool_output = await _execute_tool_calls(
            raw_response_data, session_id=session_id
        )
        raw_response_text = clean_response
        raw_response = raw_response_text  # Keep var for downstream compat

    except RuntimeError as e:
        log.error(f"[{trace_id}] ModelRouter: all backends failed: {e}")
        raw_response_text = "Something went wrong on my end, Little Mister. Give me a moment."
        raw_response = raw_response_text
        clean_response = raw_response_text
        model = "none"
    except Exception as e:
        log.warning(f"[{trace_id}] Structured tool call processing failed: {e}")
        # Extract text from raw response if we got one
        if raw_response_data and isinstance(raw_response_data, dict):
            msg = raw_response_data.get("choices", [{}])[0].get("message", {})
            raw_response_text = (msg.get("content") or "").strip()
        else:
            raw_response_text = str(raw_response_data) if raw_response_data else ""
        raw_response = raw_response_text
        clean_response = raw_response_text
        model = f"router:{_router.active_backend}"

    ttft_ms = int((time.time() - t_llm_start) * 1000)

    # ── Legacy fallback: if no structured tool calls, check for exec patterns
    if not tool_output and clean_response:
        try:
            legacy_clean, legacy_output = await _execute_tool_calls_legacy(
                clean_response, session_id=session_id
            )
            if legacy_output:
                # Log legacy tool calls
                matches = list(_EXEC_RE.finditer(clean_response))
                for m in matches:
                    tool_calls_log.append({"tool": m.group(1), "params": m.group(2).strip()[:100]})
                    log.info(f"[{trace_id}] legacy tool call: {m.group(1)}({m.group(2).strip()[:60]})")
                clean_response = legacy_clean
                tool_output = legacy_output
                raw_response_text = clean_response
        except Exception as e:
            log.warning(f"[{trace_id}] Legacy tool execution failed (degraded): {e}")
            await _log_degraded_event("tool_failure", f"Legacy tool execution error: {e}")

    # ── Follow-up LLM pass if tools produced output ──────────────────────────
    if tool_output:
        followup_msgs = history + [
            {"role": "assistant", "content": raw_response_text},
            {"role": "tool",      "content": tool_output},
        ]
        try:
            clean_response = await _router.route(
                messages=followup_msgs,
                system=sys_prompt,
                max_tokens=1024,
                private=private,
                tokens=tokens,
            )
        except Exception:
            # Tool follow-up failed — return the text from the original LLM response
            clean_response = raw_response_text or clean_response

    # Store assistant turn (wrapped for session isolation)
    try:
        history.append({"role": "assistant", "content": clean_response})
        _sessions[session_id] = history
    except Exception as e:
        log.warning(f"[{trace_id}] Failed to store assistant turn: {e}")

    # Log assistant turn
    await _log_turn(session_id, agent_id, "assistant", clean_response,
                    model=model, turn_index=turn_index + 1)

    # Calculate metrics
    total_ms = int((time.time() - t_start) * 1000)
    tokens_in = _count_tokens(message)
    tokens_out = _count_tokens(clean_response)

    log.info(f"[{trace_id}] response: {len(clean_response)} chars in {total_ms}ms")

    # Write trace record
    await _log_trace(
        trace_id=trace_id,
        channel=session_id.split(":")[1] if ":" in session_id else "unknown",
        agent_id=agent_id,
        user_message=message,
        response=clean_response,
        backend_used=model,
        tool_calls=tool_calls_log,
        ttft_ms=ttft_ms,
        total_ms=total_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )

    return clean_response


async def _run_agent(message: str, session_id: str, agent_id: str,
                     tokens: dict, stream_callback=None, trace_id: str = "") -> str:
    """Full agent execution with fault isolation and circuit breaker.

    Wraps _do_agent_work with:
      - Degraded mode (startup grace period — no tools/memory)
      - Circuit breaker check (skip if agent is crash-looping)
      - Timeout (120s max per agent response)
      - Exception capture with crash tracking
      - Trace ID propagation through all log lines
    """
    if not trace_id:
        trace_id = _gen_trace_id()

    # ── Degraded mode: startup grace period ──────────────────────────────────
    if await _is_degraded():
        log.info(f"[{trace_id}] Degraded mode: direct LLM call (startup grace, {_STARTUP_GRACE}s window)")
        try:
            response = await _router.route(
                messages=[{"role": "user", "content": message}],
                system=(
                    "You are Nova. You just restarted and are still loading your full "
                    "memory and tool systems. Answer concisely from general knowledge. "
                    "If asked about something personal, say you're still warming up."
                ),
                max_tokens=512,
                private=True,  # Always local during startup
                tokens=tokens,
            )
        except Exception as e:
            log.warning(f"[{trace_id}] Degraded mode LLM call failed: {e}")
            response = "I just restarted, Little Mister. Give me about 30 seconds to get my bearings."
        await _log_degraded_event("startup_grace_response",
                                  f"Responded in degraded mode to: {message[:80]}")
        return response

    log.info(f"[{trace_id}] routing to agent={agent_id}")

    # Circuit breaker check — if agent is crash-looping, short-circuit
    if agent_id in _agent_disabled_until and time.time() < _agent_disabled_until[agent_id]:
        remaining = int(_agent_disabled_until[agent_id] - time.time())
        log.warning(f"[{trace_id}] Agent {agent_id} disabled (circuit breaker, {remaining}s remaining)")
        return "I'm having some trouble right now. Give me a few minutes to recover."

    # Clear disabled state if window has passed
    if agent_id in _agent_disabled_until and time.time() >= _agent_disabled_until[agent_id]:
        del _agent_disabled_until[agent_id]
        _agent_crash_counts[agent_id] = 0
        log.info(f"[{trace_id}] Agent {agent_id} circuit breaker reset — re-enabled")

    # Execute with timeout and error boundary
    try:
        response = await asyncio.wait_for(
            _do_agent_work(message, session_id, agent_id, tokens, trace_id),
            timeout=120,  # 2 min max per agent response
        )
        return response
    except asyncio.TimeoutError:
        log.error(f"[{trace_id}] Agent {agent_id} timed out after 120s")
        await _record_agent_crash(agent_id, trace_id, "Timeout after 120s")
        return "I'm taking too long on this one. Let me try again with something simpler."
    except Exception as e:
        log.error(f"[{trace_id}] Agent {agent_id} crashed: {e}", exc_info=True)
        await _record_agent_crash(agent_id, trace_id, str(e))
        return "Something went wrong on my end. Give me a moment."


# ── Session ID helpers ────────────────────────────────────────────────────────

def _session_id(channel: str, channel_id: str) -> str:
    """Stable session ID per channel — resets on gateway restart (by design)."""
    return f"gw2:{channel}:{channel_id}"


def _gen_trace_id() -> str:
    """Generate a short trace ID (first 8 chars of uuid4) for request tracing."""
    return uuid.uuid4().hex[:8]


# ── Slack (raw WebSocket Socket Mode — no slack_sdk dependency) ──────────────

# Channels Nova listens on for Slack messages
_SLACK_LISTEN_CHANNELS = {SLACK_CHAT_CHANNEL, SLACK_CLAUDE_CHANNEL, JORDAN_DM_CHANNEL}


async def _slack_post_message(token: str, channel: str, text: str, thread_ts: str = ""):
    """Post a message to Slack via chat.postMessage REST API."""
    payload = {"channel": channel, "text": text, "mrkdwn": True}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        resp = await _http.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        data = resp.json()
        if not data.get("ok"):
            log.error(f"Slack post failed: {data.get('error', 'unknown')}")
    except Exception as e:
        log.error(f"Slack post exception: {e}")


async def _slack_get_bot_user_id(token: str) -> str:
    """Get our bot user ID via auth.test."""
    try:
        resp = await _http.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("user_id", "")
    except Exception as e:
        log.error(f"Slack auth.test failed: {e}")
    return ""


async def _slack_get_ws_url(app_token: str) -> str:
    """Get Socket Mode WebSocket URL via apps.connections.open."""
    resp = await _http.post(
        "https://slack.com/api/apps.connections.open",
        headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        return data["url"]
    raise RuntimeError(f"apps.connections.open failed: {data.get('error', 'unknown')}")


async def run_slack(tokens: dict):
    """Slack Socket Mode listener using raw WebSocket (websockets library).

    Connects to Slack's Socket Mode WebSocket endpoint, receives events,
    acknowledges them, and routes messages through _run_agent().
    Reconnects automatically with exponential backoff on disconnect.
    """
    import websockets

    bot_token = tokens.get("slack_bot", "")
    app_token = tokens.get("slack_app", "")
    if not bot_token or not app_token:
        log.error("Slack tokens missing — Slack channel disabled")
        return

    # Get our bot user ID to ignore our own messages
    bot_user_id = await _slack_get_bot_user_id(bot_token)
    if not bot_user_id:
        log.error("Slack: could not determine bot user ID — channel disabled")
        return

    log.info(f"Slack: bot user ID = {bot_user_id}")

    backoff = 1  # Exponential backoff seconds

    while not _shutdown.is_set():
        ws = None
        try:
            # Get fresh WebSocket URL (they expire)
            ws_url = await _slack_get_ws_url(app_token)
            log.info(f"Slack: connecting to Socket Mode WebSocket...")

            async with websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                log.info("Slack: Socket Mode WebSocket connected")
                backoff = 1  # Reset backoff on successful connect

                async for raw_msg in ws:
                    if _shutdown.is_set():
                        break

                    try:
                        data = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue

                    msg_type = data.get("type", "")
                    envelope_id = data.get("envelope_id", "")

                    # Acknowledge all envelopes immediately (Slack requires this within 3s)
                    if envelope_id:
                        ack = json.dumps({"envelope_id": envelope_id})
                        await ws.send(ack)

                    # Handle hello (connection confirmation)
                    if msg_type == "hello":
                        log.info("Slack: received hello — Socket Mode active")
                        continue

                    # Handle disconnect request
                    if msg_type == "disconnect":
                        reason = data.get("reason", "unknown")
                        log.info(f"Slack: disconnect requested ({reason}) — will reconnect")
                        break

                    # Handle events_api payloads (message events)
                    if msg_type == "events_api":
                        payload = data.get("payload", {})
                        event = payload.get("event", {})
                        await _slack_handle_event(event, bot_user_id, bot_token, tokens)

                    # Handle slash commands or interactive payloads (future use)
                    elif msg_type == "slash_commands":
                        pass
                    elif msg_type == "interactive":
                        pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Slack WebSocket error: {e}")

        # Exponential backoff with cap at 60 seconds
        if not _shutdown.is_set():
            log.info(f"Slack: reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _slack_handle_event(event: dict, bot_user_id: str, bot_token: str, tokens: dict):
    """Process a single Slack event from Socket Mode."""
    etype = event.get("type", "")

    # Only handle message events
    if etype != "message":
        return

    # Ignore bot messages (from Nova herself) and message subtypes (edits, joins, etc.)
    if event.get("bot_id") or event.get("user") == bot_user_id:
        return
    if event.get("subtype"):
        return

    text = event.get("text", "").strip()
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")

    if not text or not channel:
        return

    # Only respond in channels we listen on
    if channel not in _SLACK_LISTEN_CHANNELS:
        return

    # Don't respond in #nova-notifications
    if channel == SLACK_NOTIFY_CHANNEL:
        return

    trace_id = _gen_trace_id()
    log.info(f"[{trace_id}] Slack: message from {event.get('user', '?')}: {text[:60]}")

    session_id = _session_id("slack", channel)
    agent_id = "chat"

    async def _handle():
        async with _channel_locks[f"slack:{channel}"]:
            try:
                log.info(f"[{trace_id}] Slack: routing to agent — session={session_id}")
                response = await _run_agent(text, session_id, agent_id, tokens, trace_id=trace_id)
                if not response or not response.strip():
                    log.warning(f"Slack: agent returned empty response for: {text[:60]}")
                    response = "I'm thinking about that but came up empty. Can you rephrase?"
                await _slack_post_message(bot_token, channel, response, thread_ts=thread_ts)
                log.info(f"Slack: responded in {channel} ({len(response)} chars)")

                # If this is from #nova-claude, also write to claude_messages table
                if channel == SLACK_CLAUDE_CHANNEL:
                    await _write_message_for_claude(
                        f"[Slack #nova-claude] User: {text}\nNova: {response}",
                        metadata={"channel": "slack-claude-bridge", "timestamp": time.time()},
                    )
            except Exception as e:
                log.error(f"Slack agent error: {e}", exc_info=True)
                await _slack_post_message(bot_token, channel, "Sorry, something went wrong on my end.")

    # Run in background task to not block WebSocket message loop
    asyncio.create_task(_handle())


# ── Discord (raw Gateway WebSocket — no discord.py dependency) ───────────────

# Discord Gateway intents:
#   GUILDS (1 << 0) = 1
#   GUILD_MESSAGES (1 << 9) = 512
#   MESSAGE_CONTENT (1 << 15) = 32768
_DISCORD_INTENTS = 1 | 512 | 32768  # = 33281

# Discord Gateway opcodes
_DISCORD_OP_DISPATCH   = 0   # Server → Client: event dispatch
_DISCORD_OP_HEARTBEAT  = 1   # Client → Server: heartbeat
_DISCORD_OP_IDENTIFY   = 2   # Client → Server: identify
_DISCORD_OP_RESUME     = 6   # Client → Server: resume
_DISCORD_OP_RECONNECT  = 7   # Server → Client: reconnect request
_DISCORD_OP_INVALID    = 9   # Server → Client: invalid session
_DISCORD_OP_HELLO      = 10  # Server → Client: hello (heartbeat interval)
_DISCORD_OP_HEARTBEAT_ACK = 11  # Server → Client: heartbeat acknowledged


async def _discord_get_gateway_url(token: str) -> str:
    """Get the Discord Gateway WebSocket URL."""
    resp = await _http.get(
        "https://discord.com/api/v10/gateway/bot",
        headers={"Authorization": f"Bot {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["url"]


async def _discord_send_message(token: str, channel_id: int, content: str):
    """Send a message to a Discord channel via REST API."""
    for chunk in _split_message(content, 1900):
        try:
            resp = await _http.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers={
                    "Authorization": f"Bot {token}",
                    "Content-Type": "application/json",
                },
                json={"content": chunk},
                timeout=15,
            )
            if resp.status_code == 429:
                # Rate limited — wait and retry
                retry_after = resp.json().get("retry_after", 1.0)
                log.warning(f"Discord: rate limited, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                await _http.post(
                    f"https://discord.com/api/v10/channels/{channel_id}/messages",
                    headers={
                        "Authorization": f"Bot {token}",
                        "Content-Type": "application/json",
                    },
                    json={"content": chunk},
                    timeout=15,
                )
            elif resp.status_code >= 400:
                log.error(f"Discord send failed ({resp.status_code}): {resp.text[:200]}")
        except Exception as e:
            log.error(f"Discord send exception: {e}")


async def _discord_trigger_typing(token: str, channel_id: int):
    """Trigger typing indicator in a Discord channel."""
    try:
        await _http.post(
            f"https://discord.com/api/v10/channels/{channel_id}/typing",
            headers={"Authorization": f"Bot {token}"},
            timeout=5,
        )
    except Exception:
        pass


async def run_discord(tokens: dict):
    """Discord Gateway WebSocket listener using raw websockets.

    Implements the Discord Gateway protocol:
    - HELLO → start heartbeating
    - IDENTIFY → authenticate with token + intents
    - READY → connected, start receiving events
    - MESSAGE_CREATE → route through agent
    - Automatic reconnect + resume on disconnect

    No discord.py dependency — just websockets + httpx.
    """
    import websockets

    discord_token = tokens.get("discord", "")
    if not discord_token:
        log.error("Discord token missing — Discord channel disabled")
        return

    backoff = 1
    session_id_discord = None  # Discord session ID for resuming
    resume_url = None
    sequence = None  # Last sequence number received
    bot_user_id = None  # Our bot's user ID (set on READY)

    while not _shutdown.is_set():
        try:
            # Get gateway URL
            if resume_url:
                gateway_url = f"{resume_url}?v=10&encoding=json"
            else:
                base_url = await _discord_get_gateway_url(discord_token)
                gateway_url = f"{base_url}?v=10&encoding=json"

            log.info(f"Discord: connecting to gateway...")

            async with websockets.connect(
                gateway_url,
                ping_interval=None,  # Discord handles its own heartbeat
                close_timeout=5,
                max_size=2**20,  # 1MB max message size
            ) as ws:
                heartbeat_task = None
                heartbeat_ack_received = True

                async def _heartbeat(interval_ms: int):
                    """Send heartbeat at the interval specified by HELLO."""
                    nonlocal heartbeat_ack_received
                    interval = interval_ms / 1000.0
                    # Jitter on first heartbeat
                    await asyncio.sleep(interval * 0.5)
                    while not _shutdown.is_set():
                        if not heartbeat_ack_received:
                            log.warning("Discord: missed heartbeat ACK — zombie connection, reconnecting")
                            await ws.close(4000, "Zombie connection")
                            return
                        heartbeat_ack_received = False
                        payload = json.dumps({"op": _DISCORD_OP_HEARTBEAT, "d": sequence})
                        await ws.send(payload)
                        await asyncio.sleep(interval)

                try:
                    async for raw_msg in ws:
                        if _shutdown.is_set():
                            break

                        try:
                            data = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            continue

                        op = data.get("op")
                        d = data.get("d")
                        t = data.get("t")
                        s = data.get("s")

                        # Update sequence number
                        if s is not None:
                            sequence = s

                        # ── HELLO (op 10) — start heartbeating ────────────────────
                        if op == _DISCORD_OP_HELLO:
                            heartbeat_interval = d.get("heartbeat_interval", 41250)
                            log.info(f"Discord: HELLO received, heartbeat interval {heartbeat_interval}ms")
                            heartbeat_task = asyncio.create_task(
                                _heartbeat(heartbeat_interval), name="discord-heartbeat"
                            )

                            # Send IDENTIFY or RESUME
                            if session_id_discord and sequence:
                                # Resume existing session
                                resume_payload = {
                                    "op": _DISCORD_OP_RESUME,
                                    "d": {
                                        "token": discord_token,
                                        "session_id": session_id_discord,
                                        "seq": sequence,
                                    },
                                }
                                await ws.send(json.dumps(resume_payload))
                                log.info("Discord: sent RESUME")
                            else:
                                # Fresh identify
                                identify_payload = {
                                    "op": _DISCORD_OP_IDENTIFY,
                                    "d": {
                                        "token": discord_token,
                                        "intents": _DISCORD_INTENTS,
                                        "properties": {
                                            "os": "macos",
                                            "browser": "nova-gateway",
                                            "device": "nova-gateway",
                                        },
                                    },
                                }
                                await ws.send(json.dumps(identify_payload))
                                log.info("Discord: sent IDENTIFY")

                        # ── HEARTBEAT ACK (op 11) ─────────────────────────────────
                        elif op == _DISCORD_OP_HEARTBEAT_ACK:
                            heartbeat_ack_received = True

                        # ── HEARTBEAT request from server (op 1) ──────────────────
                        elif op == _DISCORD_OP_HEARTBEAT:
                            payload = json.dumps({"op": _DISCORD_OP_HEARTBEAT, "d": sequence})
                            await ws.send(payload)

                        # ── RECONNECT (op 7) ──────────────────────────────────────
                        elif op == _DISCORD_OP_RECONNECT:
                            log.info("Discord: server requested reconnect")
                            await ws.close(4000, "Reconnect requested")
                            break

                        # ── INVALID SESSION (op 9) ────────────────────────────────
                        elif op == _DISCORD_OP_INVALID:
                            resumable = d if isinstance(d, bool) else False
                            if not resumable:
                                session_id_discord = None
                                sequence = None
                                resume_url = None
                                log.info("Discord: invalid session (not resumable) — will re-identify")
                            else:
                                log.info("Discord: invalid session (resumable) — will resume")
                            await asyncio.sleep(3)
                            await ws.close(4000, "Invalid session")
                            break

                        # ── DISPATCH (op 0) — event handling ──────────────────────
                        elif op == _DISCORD_OP_DISPATCH:
                            if t == "READY":
                                session_id_discord = d.get("session_id")
                                resume_url = d.get("resume_gateway_url")
                                bot_user_id = d.get("user", {}).get("id")
                                log.info(
                                    f"Discord: READY — session={session_id_discord}, "
                                    f"bot_user_id={bot_user_id}"
                                )
                                backoff = 1  # Reset backoff on successful connection

                            elif t == "RESUMED":
                                log.info("Discord: session RESUMED successfully")
                                backoff = 1

                            elif t == "MESSAGE_CREATE":
                                await _discord_handle_message(d, bot_user_id, discord_token, tokens)

                finally:
                    if heartbeat_task:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Discord gateway error: {e}")
            # On unrecoverable errors, clear resume state
            if "4004" in str(e) or "authentication" in str(e).lower():
                log.error("Discord: authentication failed — check token")
                session_id_discord = None
                sequence = None
                resume_url = None

        # Exponential backoff with cap at 60 seconds
        if not _shutdown.is_set():
            log.info(f"Discord: reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _discord_handle_message(data: dict, bot_user_id: str, discord_token: str, tokens: dict):
    """Process a Discord MESSAGE_CREATE event."""
    # Ignore our own messages
    author = data.get("author", {})
    if author.get("id") == bot_user_id:
        return
    # Ignore other bots
    if author.get("bot", False):
        return

    # Check guild and channel
    guild_id = data.get("guild_id")
    channel_id = data.get("channel_id")

    if guild_id and int(guild_id) != DISCORD_GUILD_ID:
        return
    if not channel_id or int(channel_id) != DISCORD_CHAT_CHANNEL:
        return

    text = data.get("content", "").strip()
    if not text:
        return

    trace_id = _gen_trace_id()
    log.info(f"[{trace_id}] Discord: message from {author.get('username', '?')}: {text[:60]}")

    session_id = _session_id("discord", channel_id)
    agent_id = "chat"
    channel_key = f"discord:{channel_id}"

    async def _handle():
        async with _channel_locks[channel_key]:
            # Trigger typing indicator
            await _discord_trigger_typing(discord_token, int(channel_id))
            try:
                response = await _run_agent(text, session_id, agent_id, tokens, trace_id=trace_id)
                await _discord_send_message(discord_token, int(channel_id), response)
            except Exception as e:
                log.error(f"Discord agent error: {e}", exc_info=True)
                await _discord_send_message(
                    discord_token, int(channel_id),
                    "Something went wrong on my end, Little Mister."
                )

    # Run in background task to not block WebSocket message loop
    asyncio.create_task(_handle())


def _split_message(text: str, max_len: int = 1900) -> list[str]:
    """Split a long message at sentence boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Find last sentence end before max_len
        cut = text.rfind(". ", 0, max_len)
        if cut == -1:
            cut = text.rfind(" ", 0, max_len)
        if cut == -1:
            cut = max_len
        else:
            cut += 1  # include the period
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return chunks


# ── Signal ────────────────────────────────────────────────────────────────────

async def _signal_rpc(method: str, params: dict = None) -> dict:
    """Call signal-cli JSON-RPC API."""
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        payload["params"] = params
    resp = await _http.post(
        f"{SIGNAL_URL}/api/v1/rpc",
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


async def _send_signal(recipient: str, text: str):
    for chunk in _split_message(text, 1000):
        try:
            result = await _signal_rpc("send", {
                "recipient": recipient,
                "message":   chunk,
            })
            if "error" in result:
                log.error(f"Signal send error: {result['error']}")
        except Exception as e:
            log.error(f"Signal send failed: {e}")


async def run_signal(tokens: dict):
    """Signal listener via TCP JSON-RPC streaming.

    signal-cli daemon runs with --tcp 127.0.0.1:7583 for streaming receive
    and --http 127.0.0.1:8080 for outbound sends.

    TCP streaming: open connection → subscribeReceive → listen for pushed messages.
    Much more efficient than HTTP polling, no "already being received" conflict.
    """
    log.info("Signal adapter starting (TCP streaming mode)...")

    ALLOWED = {JORDAN_SIGNAL}
    last_timestamp: dict[str, int] = {}

    while not _shutdown.is_set():
        reader = writer = None
        try:
            reader, writer = await asyncio.open_connection(SIGNAL_TCP_HOST, SIGNAL_TCP_PORT)
            log.info("Signal: TCP connection established")

            # Subscribe to receive messages
            sub_req = json.dumps({"jsonrpc": "2.0", "method": "subscribeReceive", "id": 1}) + "\n"
            writer.write(sub_req.encode())
            await writer.drain()

            # Read first response (subscription confirmation)
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            resp = json.loads(line)
            if "error" in resp:
                log.error(f"Signal subscribeReceive error: {resp['error']}")
                await asyncio.sleep(5)
                continue

            sub_id = resp.get("result", 0)
            log.info(f"Signal: subscribed (id={sub_id}) — listening for messages")

            # Stream incoming messages
            while not _shutdown.is_set():
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=30)
                    if not line:
                        break

                    msg = json.loads(line)
                    # Incoming messages arrive as JSON-RPC notifications (no id)
                    params = msg.get("params", {})
                    envelope = params.get("envelope", {})
                    data_msg = envelope.get("dataMessage", {})
                    sender   = envelope.get("sourceNumber", "")
                    text     = data_msg.get("message", "").strip()
                    ts       = envelope.get("timestamp", 0)

                    if not text or not sender:
                        continue
                    if sender not in ALLOWED:
                        continue
                    if last_timestamp.get(sender, 0) >= ts:
                        continue
                    last_timestamp[sender] = ts

                    trace_id = _gen_trace_id()
                    log.info(f"[{trace_id}] Signal: message from {sender}: {text[:50]}")
                    session_id  = _session_id("signal", sender)
                    channel_key = f"signal:{sender}"

                    async def handle_signal(t=text, s=session_id, sndr=sender, ck=channel_key, tid=trace_id):
                        async with _channel_locks[ck]:
                            try:
                                response = await _run_agent(t, s, "chat", tokens, trace_id=tid)
                                await _send_signal(sndr, response)
                            except Exception as e:
                                log.error(f"Signal agent error: {e}", exc_info=True)
                                await _send_signal(sndr, "Something went wrong on my end.")

                    asyncio.create_task(handle_signal())

                except asyncio.TimeoutError:
                    # Send keepalive ping
                    ping = json.dumps({"jsonrpc": "2.0", "method": "version", "id": 99}) + "\n"
                    writer.write(ping.encode())
                    await writer.drain()
                except json.JSONDecodeError:
                    continue

        except asyncio.CancelledError:
            break
        except ConnectionRefusedError:
            log.warning("Signal: TCP connection refused — signal-cli not ready, retrying in 10s")
            await asyncio.sleep(10)
        except Exception as e:
            log.error(f"Signal TCP error: {e}")
            await asyncio.sleep(5)
        finally:
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass


# ── Claude Code channel (bidirectional bridge) ───────────────────────────────

async def run_claude_channel(tokens: dict):
    """Poll claude_messages for messages from Claude Code and respond.

    Also monitors the Redis scratchpad for Claude's current task,
    and cleans up stale messages when Claude is inactive.
    """
    global _claude_active_task

    log.info("Claude Code channel starting (poll mode)...")

    # Track last processed message ID to avoid re-processing
    last_processed_id = 0

    # Initialize: get the current max ID so we don't replay old messages
    try:
        pool = await _pg()
        row = await pool.fetchval(
            "SELECT COALESCE(MAX(id), 0) FROM claude_messages WHERE direction = 'to_nova'"
        )
        last_processed_id = row or 0
        log.info(f"Claude channel: starting from message id {last_processed_id}")
    except Exception as e:
        log.warning(f"Claude channel: failed to get initial message id: {e}")

    scratchpad_check_counter = 0
    notify_counter = 0
    claude_msg_count = 0

    while not _shutdown.is_set():
        try:
            # ── Poll for new messages from Claude Code ────────────────────────
            pool = await _pg()
            rows = await pool.fetch(
                """SELECT id, message, metadata
                   FROM claude_messages
                   WHERE direction = 'to_nova' AND id > $1
                   ORDER BY id ASC LIMIT 5""",
                last_processed_id,
            )

            for row in rows:
                msg_id = row["id"]
                message_text = row["message"]
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
                last_processed_id = msg_id

                trace_id = _gen_trace_id()
                log.info(f"[{trace_id}] Claude channel: processing message #{msg_id}: {message_text[:60]}")

                session_id = _session_id("claude-code", CLAUDE_BRIDGE_SESSION)
                agent_id = "chat"

                async with _channel_locks["claude-code"]:
                    try:
                        response = await _run_agent(
                            message_text, session_id, agent_id, tokens, trace_id=trace_id
                        )
                        # Write response back to claude_messages
                        await _write_message_for_claude(
                            response,
                            metadata={
                                "channel": "bridge",
                                "in_reply_to": msg_id,
                                "agent_id": agent_id,
                            },
                        )
                        log.info(f"Claude channel: replied to message #{msg_id}")
                        claude_msg_count += 1
                    except Exception as e:
                        log.error(f"Claude channel: agent error on msg #{msg_id}: {e}")
                        await _write_message_for_claude(
                            f"Error processing your message: {e}",
                            metadata={"channel": "bridge", "in_reply_to": msg_id, "error": True},
                        )

            # ── Scratchpad check (every 60s = ~12 poll cycles at 5s each) ────
            scratchpad_check_counter += 1
            if scratchpad_check_counter >= 12:
                scratchpad_check_counter = 0
                await _check_claude_scratchpad()

            # ── Notify Jordan every 5 min if Claude-Nova chat is active ──
            notify_counter += 1
            if notify_counter >= 60:  # 60 * 5s = 5 min
                notify_counter = 0
                if claude_msg_count > 0:
                    try:
                        summary = f":robot_face: *Claude ↔ Nova Activity* ({claude_msg_count} messages in last 5 min)\n  Current task: {_claude_active_task or 'general collaboration'}"
                        await _slack_post_message(
                            tokens["slack_bot"], SLACK_NOTIFY_CHANNEL, summary)
                    except Exception:
                        pass
                    claude_msg_count = 0

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Claude channel error: {e}")

        # Poll interval: 5 seconds
        await asyncio.sleep(5)


async def _check_claude_scratchpad():
    """Check Redis scratchpad for Claude's current task and editing locks.

    When the key exists, store it internally so Nova avoids restarting
    services Claude is working on. When expired/empty, clean up stale messages.
    Also scans for nova:editing:* keys to track files Claude is editing.
    """
    global _claude_active_task, _claude_editing_files

    try:
        r = _get_redis()
        if not r:
            return

        task_value = r.get("nova:scratchpad:claude_current_task")

        if task_value:
            if task_value != _claude_active_task:
                log.info(f"Claude active task detected: {task_value}")
            _claude_active_task = task_value
        else:
            if _claude_active_task:
                log.info("Claude task cleared (session ended or task completed)")
            _claude_active_task = None

            # Claude is not active — clean up stale messages older than 24 hours
            try:
                pool = await _pg()
                deleted = await pool.execute(
                    """DELETE FROM claude_messages
                       WHERE created_at < now() - INTERVAL '24 hours'"""
                )
                if deleted and "DELETE" in deleted:
                    count = int(deleted.split(" ")[-1]) if " " in deleted else 0
                    if count > 0:
                        log.info(f"Cleaned up {count} stale claude_messages (>24h old)")
            except Exception:
                pass

        # Scan for editing locks
        editing_keys = r.keys("nova:editing:*")
        if editing_keys:
            _claude_editing_files = [k.replace("nova:editing:", "", 1) for k in editing_keys]
        else:
            _claude_editing_files = []

    except Exception as e:
        log.debug(f"Scratchpad check failed (non-fatal): {e}")


# ── Startup / boot ────────────────────────────────────────────────────────────

async def _ensure_pg_schema():
    """Create gateway tables and Claude communication tables if they don't exist."""
    pool = await _pg()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS claude_sessions (
            session_id   TEXT PRIMARY KEY,
            started_at   BIGINT NOT NULL DEFAULT (extract(epoch from now()) * 1000)::BIGINT,
            ended_at     BIGINT,
            project      TEXT,
            status       TEXT DEFAULT 'active',
            summary      TEXT,
            action_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS claude_actions (
            action_id    TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL REFERENCES claude_sessions(session_id),
            ts           BIGINT NOT NULL,
            action_type  TEXT NOT NULL,
            target       TEXT,
            description  TEXT NOT NULL,
            rationale    TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_docs (
            doc_id       TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            agent_id     TEXT NOT NULL,
            doc_type     TEXT NOT NULL,
            content      TEXT NOT NULL,
            version      INTEGER NOT NULL DEFAULT 1,
            updated_at   BIGINT NOT NULL,
            UNIQUE (agent_id, doc_type)
        );
        CREATE TABLE IF NOT EXISTS claude_messages (
            id           SERIAL PRIMARY KEY,
            direction    TEXT NOT NULL,
            sender       TEXT NOT NULL DEFAULT 'unknown',
            message      TEXT NOT NULL,
            metadata     JSONB DEFAULT '{}',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_claude_messages_dir
            ON claude_messages(direction, created_at DESC);
        CREATE TABLE IF NOT EXISTS claude_queue (
            id           SERIAL PRIMARY KEY,
            session_id   TEXT,
            status       TEXT NOT NULL DEFAULT 'queued',
            priority     INTEGER NOT NULL DEFAULT 3,
            description  TEXT NOT NULL,
            context      JSONB DEFAULT '{}',
            outcome      TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_claude_queue_status
            ON claude_queue(status, priority);
    """)

    # Add tool-specific columns to gateway_query_log (idempotent ALTER TABLE)
    for col_def in [
        ("tool_name",   "TEXT"),
        ("tool_params", "TEXT"),
        ("tool_result", "TEXT"),
        ("duration_ms", "INTEGER"),
        ("trace_id",    "TEXT"),
    ]:
        try:
            await pool.execute(
                f"ALTER TABLE gateway_query_log ADD COLUMN IF NOT EXISTS "
                f"{col_def[0]} {col_def[1]}"
            )
        except Exception:
            pass  # Column already exists or table doesn't exist yet

    # Create gateway_traces table for full request lifecycle tracking
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS gateway_traces (
            trace_id     TEXT PRIMARY KEY,
            channel      TEXT NOT NULL,
            agent_id     TEXT,
            user_message TEXT,
            response     TEXT,
            backend_used TEXT,
            tool_calls   JSONB DEFAULT '[]',
            ttft_ms      INT,
            total_ms     INT,
            tokens_in    INT,
            tokens_out   INT,
            created_at   TIMESTAMPTZ DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_gateway_traces_created
            ON gateway_traces(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_gateway_traces_agent
            ON gateway_traces(agent_id, created_at DESC);
    """)

    log.info("PG schema verified (incl. Claude communication tables + tool audit + traces)")


async def _post_startup_slack(tokens: dict):
    """Post startup notification to #nova-notifications via REST API."""
    bot_token = tokens.get("slack_bot", "")
    if not bot_token:
        return
    try:
        router_status = await _router.status()
        healthy_backends = [n for n, s in router_status.items()
                           if isinstance(s, dict) and s.get("healthy")]
        await _slack_post_message(
            bot_token,
            SLACK_NOTIFY_CHANNEL,
            (
                f":rocket: *Nova Gateway v{VERSION} started*\n"
                f"  Channels: Slack (Socket Mode) + Discord (Gateway WS) + Signal + Claude Code\n"
                f"  Routing: Ollama → MLX → llama.cpp → OpenRouter (auto-failover)\n"
                f"  Backends UP: {', '.join(healthy_backends) or 'checking...'}\n"
                f"  Memory: 1.48M vectors · PG bootstrap\n"
                f"  No Node.js dependency — pure Python"
            ),
        )
    except Exception:
        pass


# ── Health API ────────────────────────────────────────────────────────────────

async def _health_server():
    """Simple HTTP health endpoint on port 18792 (doesn't conflict with OpenClaw's 18789)."""
    from aiohttp import web

    async def health(_):
        router_status = await _router.status()
        degraded = await _is_degraded()
        return web.json_response({
            "ok": True, "version": VERSION,
            "degraded": degraded,
            "sessions": len(_sessions),
            "uptime_s": int(time.time() - _start_time),
            "backends": router_status,
            "claude_active_task": _claude_active_task,
            "claude_editing": _claude_editing_files,
            "circuit_breakers": {
                agent: {"disabled_until": ts, "remaining_s": int(ts - time.time())}
                for agent, ts in _agent_disabled_until.items()
                if time.time() < ts
            },
        })

    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18792)
    await site.start()
    log.info("Health API on 127.0.0.1:18792")


_start_time = time.time()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global _http

    log.info(f"Nova Gateway v{VERSION} starting...")

    # Load secrets from Keychain
    tokens = _load_tokens()
    missing = [k for k, v in tokens.items() if not v and k != "openrouter"]
    if missing:
        log.warning(f"Missing tokens: {missing} — those channels will be disabled")

    # Init HTTP client
    _http = httpx.AsyncClient(timeout=60.0, follow_redirects=True)

    # Init PG and ensure schema
    try:
        await _ensure_pg_schema()
    except Exception as e:
        log.warning(f"PG schema setup failed: {e} — continuing without PG logging")

    # Signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: _shutdown.set())

    # Start health API
    await _health_server()

    # Start all channels concurrently
    tasks = [
        asyncio.create_task(run_slack(tokens),          name="slack"),
        asyncio.create_task(run_discord(tokens),        name="discord"),
        asyncio.create_task(run_signal(tokens),         name="signal"),
        asyncio.create_task(run_claude_channel(tokens), name="claude-code"),
    ]

    log.info("All channel tasks launched — gateway running (incl. Claude Code bridge)")

    # Post startup notification to Slack
    await _post_startup_slack(tokens)

    # Wait for shutdown
    await _shutdown.wait()
    log.info("Shutdown signal received — stopping channels")

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if _pg_pool:
        await _pg_pool.close()
    if _http:
        await _http.aclose()

    log.info("Nova Gateway v2 stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
