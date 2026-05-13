#!/usr/bin/env python3
"""
nova_gateway_v2.py — Nova's custom Python gateway. Replaces OpenClaw node.js binary.

Channels: Slack (socket mode, notifications), Discord (conversations), Signal (mobile)
Agent:    Ollama qwen3:30b-a3b (chat/home) + OpenRouter qwen3-235b (research)
Session:  Persisted to nova_ops.gateway_sessions + gateway_query_log
Docs:     Bootstrap content loaded from nova_ops.agent_docs (not files)
Memory:   nova_memory_first.py injected before every response

Architecture:
  - Single asyncio event loop
  - One channel listener task per channel (Slack, Discord, Signal poller)
  - One agent executor coroutine per incoming message (concurrent, per-channel locks)
  - Session state in PG + in-memory cache

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
VERSION      = "2.0.0"
PG_DSN       = "postgresql://kochj@192.168.1.6:5432/nova_ops"
OLLAMA_URL   = "http://127.0.0.1:11434"
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
JORDAN_DM_CHANNEL    = "D0AMPB3F4T0"  # Jordan DM

# Discord
DISCORD_GUILD_ID     = 1496985100657623210
DISCORD_CHAT_CHANNEL = 1496990647062761483   # #nova-chat
DISCORD_NOTIF_CHANNEL = 1496990332250886246  # #nova-notifications

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

async def _inject_memory(question: str) -> str:
    """Run nova_memory_first.py and return result to prepend to context."""
    try:
        result = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPTS_DIR / "nova_memory_first.py"), question,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=str(SCRIPTS_DIR),
        )
        stdout, _ = await asyncio.wait_for(result.communicate(), timeout=15)
        text = stdout.decode(errors="replace").strip()
        if text and len(text) > 50:
            return f"[Memory context]\n{text}\n\n[End memory context]\n\n"
    except Exception:
        pass
    return ""


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
        summary = await _call_ollama("qwen3:30b-a3b", [
            {"role": "user", "content": summary_prompt}
        ], max_tokens=300, system="You are a concise summarizer.")
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


# ── Tool call detection + execution ──────────────────────────────────────────

_EXEC_RE = re.compile(r"exec\s+(python3|python|bash|zsh)\s+(.+?)(?:\n|$)")

async def _execute_tool_calls(text: str) -> tuple[str, str]:
    """Detect 'exec python3 script.py args' patterns, run them, return (clean_text, tool_output)."""
    matches = list(_EXEC_RE.finditer(text))
    if not matches:
        return text, ""

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

        cmd = [sys.executable if "python" in interpreter else interpreter,
               script_path]
        if args:
            cmd.append(args)

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
        except Exception as e:
            tool_results.append(f"[tool error: {e}]")

        # Remove exec line from text
        clean = clean.replace(m.group(0), "").strip()

    return clean, "\n".join(tool_results)


# ── Core agent execution ──────────────────────────────────────────────────────

async def _run_agent(message: str, session_id: str, agent_id: str,
                     tokens: dict, stream_callback=None) -> str:
    """Full agent execution: memory → context → LLM → tool execution → response."""

    # Load bootstrap docs
    bootstrap = await _load_agent_docs(agent_id)
    sys_prompt = _system_prompt(agent_id, bootstrap)

    # Memory injection
    memory_ctx = await _inject_memory(message)
    user_content = f"{memory_ctx}{message}" if memory_ctx else message

    # Build message history
    history = _sessions[session_id]
    history.append({"role": "user", "content": user_content})

    # Compact if needed
    history = await _compact_if_needed(session_id, agent_id, history, sys_prompt)
    _sessions[session_id] = history

    turn_index = len(history) - 1

    # Log user turn
    await _log_turn(session_id, agent_id, "user", message, turn_index=turn_index)

    # Call LLM
    model = "qwen3:30b-a3b"
    raw_response = ""

    try:
        if agent_id == "research":
            or_key = tokens.get("openrouter", "")
            if or_key:
                model = "openrouter/qwen/qwen3-235b-a22b-2507"
                raw_response = await _call_openrouter(
                    "qwen/qwen3-235b-a22b-2507", history, or_key,
                    max_tokens=4096, system=sys_prompt,
                )
            else:
                raw_response = await _call_ollama(model, history, max_tokens=2048, system=sys_prompt)
        else:
            raw_response = await _call_ollama(model, history, max_tokens=1024, system=sys_prompt)
    except Exception as e:
        log.error(f"LLM call failed: {e}")
        raw_response = "Something went wrong on my end, Little Mister. Give me a moment."

    # Tool execution — check for exec patterns
    clean_response, tool_output = await _execute_tool_calls(raw_response)

    # If tool ran and produced output, run a second LLM pass with the result
    if tool_output:
        followup_msgs = history + [
            {"role": "assistant", "content": raw_response},
            {"role": "tool",      "content": tool_output},
        ]
        try:
            clean_response = await _call_ollama(
                model, followup_msgs, max_tokens=1024, system=sys_prompt
            )
        except Exception:
            clean_response = raw_response  # fallback to raw if followup fails

    # Store assistant turn
    history.append({"role": "assistant", "content": clean_response})
    _sessions[session_id] = history

    # Log assistant turn
    await _log_turn(session_id, agent_id, "assistant", clean_response,
                    model=model, turn_index=turn_index + 1)

    return clean_response


# ── Session ID helpers ────────────────────────────────────────────────────────

def _session_id(channel: str, channel_id: str) -> str:
    """Stable session ID per channel — resets on gateway restart (by design)."""
    return f"gw2:{channel}:{channel_id}"


# ── Slack ─────────────────────────────────────────────────────────────────────

async def _slack_post(client, channel: str, text: str, thread_ts: str = ""):
    kwargs = {"channel": channel, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        await client.chat_postMessage(**kwargs)
    except Exception as e:
        log.error(f"Slack post failed: {e}")


async def _slack_typing(client, channel: str):
    """Post a '...' message as a typing indicator (Slack doesn't have native typing)."""
    pass  # Slack socket mode doesn't support typing indicators — just respond quickly


async def run_slack(tokens: dict):
    """Slack socket mode listener."""
    from slack_sdk.web.async_client import AsyncWebClient
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    bot_token = tokens.get("slack_bot", "")
    app_token = tokens.get("slack_app", "")
    if not bot_token or not app_token:
        log.error("Slack tokens missing — Slack channel disabled")
        return

    web = AsyncWebClient(token=bot_token)
    sm  = SocketModeClient(app_token=app_token, web_client=web)

    # Get our own bot user ID to avoid responding to ourselves
    try:
        auth = await web.auth_test()
        bot_user_id = auth["user_id"]
    except Exception as e:
        log.error(f"Slack auth_test failed: {e}")
        return

    log.info("Slack socket mode connecting...")

    async def handle(client, req):
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        if req.type != "events_api":
            return

        event = req.payload.get("event", {})
        etype = event.get("type", "")

        # Only handle messages, not bot messages or our own
        if etype != "message":
            return
        if event.get("bot_id") or event.get("user") == bot_user_id:
            return
        if event.get("subtype"):
            return

        text     = event.get("text", "").strip()
        channel  = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user     = event.get("user", "")

        if not text or not channel:
            return

        # Only respond in nova-chat or DMs — not in #nova-notifications
        if channel == SLACK_NOTIFY_CHANNEL:
            return

        session_id = _session_id("slack", channel)
        agent_id   = "chat"

        async with _channel_locks[f"slack:{channel}"]:
            try:
                response = await _run_agent(text, session_id, agent_id, tokens)
                await _slack_post(web, channel, response, thread_ts=thread_ts)
            except Exception as e:
                log.error(f"Slack agent error: {e}", exc_info=True)
                await _slack_post(web, channel, "Sorry, something went wrong on my end.")

    sm.socket_mode_request_listeners.append(handle)

    try:
        await sm.connect()
        log.info("Slack socket mode connected")
        while not _shutdown.is_set():
            await asyncio.sleep(1)
    except Exception as e:
        log.error(f"Slack connection error: {e}")
    finally:
        await sm.close()


# ── Discord ───────────────────────────────────────────────────────────────────

async def run_discord(tokens: dict):
    """Discord bot using discord.py."""
    import discord

    discord_token = tokens.get("discord", "")
    if not discord_token:
        log.error("Discord token missing — Discord channel disabled")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        log.info(f"Discord connected as {client.user} (ID: {client.user.id})")

    @client.event
    async def on_message(message):
        # Ignore our own messages
        if message.author == client.user:
            return

        # Only handle in the Koch Family guild and nova-chat channel
        if not message.guild or message.guild.id != DISCORD_GUILD_ID:
            return
        if message.channel.id != DISCORD_CHAT_CHANNEL:
            return

        text = message.content.strip()
        if not text:
            return

        session_id = _session_id("discord", str(message.channel.id))
        agent_id   = "chat"
        channel_key = f"discord:{message.channel.id}"

        async with _channel_locks[channel_key]:
            async with message.channel.typing():
                try:
                    response = await _run_agent(text, session_id, agent_id, tokens)
                    # Split long responses at sentence boundaries
                    for chunk in _split_message(response, 1900):
                        await message.channel.send(chunk)
                except Exception as e:
                    log.error(f"Discord agent error: {e}", exc_info=True)
                    await message.channel.send("Something went wrong on my end, Little Mister.")

    try:
        log.info("Discord connecting...")
        await client.start(discord_token)
    except Exception as e:
        log.error(f"Discord connection error: {e}")
    finally:
        if not client.is_closed():
            await client.close()


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

                    log.info(f"Signal: message from {sender}: {text[:50]}")
                    session_id  = _session_id("signal", sender)
                    channel_key = f"signal:{sender}"

                    async def handle_signal(t=text, s=session_id, sndr=sender, ck=channel_key):
                        async with _channel_locks[ck]:
                            try:
                                response = await _run_agent(t, s, "chat", tokens)
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


# ── Startup / boot ────────────────────────────────────────────────────────────

async def _ensure_pg_schema():
    """Create claude_sessions and claude_actions tables if they don't exist."""
    pool = await _pg()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS claude_sessions (
            session_id   TEXT PRIMARY KEY,
            started_at   BIGINT NOT NULL,
            ended_at     BIGINT,
            project      TEXT,
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
    """)
    log.info("PG schema verified")


async def _post_startup_slack(web_client):
    """Post startup notification to #nova-notifications."""
    try:
        await web_client.chat_postMessage(
            channel=SLACK_NOTIFY_CHANNEL,
            text=(
                f":rocket: *Nova Gateway v{VERSION} started*\n"
                f"  Channels: Slack + Discord + Signal\n"
                f"  Model: qwen3:30b-a3b (chat) · qwen3-235b (research)\n"
                f"  Memory: 1.43M vectors · PG bootstrap\n"
                f"  OpenClaw: still running in parallel (48h window)"
            ),
        )
    except Exception:
        pass


# ── Health API ────────────────────────────────────────────────────────────────

async def _health_server():
    """Simple HTTP health endpoint on port 18792 (doesn't conflict with OpenClaw's 18789)."""
    from aiohttp import web

    async def health(_):
        return web.json_response({
            "ok": True, "version": VERSION,
            "sessions": len(_sessions),
            "uptime_s": int(time.time() - _start_time),
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
        asyncio.create_task(run_slack(tokens),   name="slack"),
        asyncio.create_task(run_discord(tokens), name="discord"),
        asyncio.create_task(run_signal(tokens),  name="signal"),
    ]

    log.info("All channel tasks launched — gateway running")

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
