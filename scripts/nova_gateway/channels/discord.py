"""
nova_gateway.channels.discord — Discord Gateway WebSocket listener.

Written by Jordan Koch.
"""

import asyncio
import json
import logging

from nova_gateway.config import DISCORD_GUILD_ID, DISCORD_CHAT_CHANNEL
from nova_gateway.context import GatewayContext
from nova_gateway.agent import run_agent, session_id, gen_trace_id

log = logging.getLogger("nova_gateway_v2")

# Discord Gateway intents:
#   GUILDS (1 << 0) = 1
#   GUILD_MESSAGES (1 << 9) = 512
#   MESSAGE_CONTENT (1 << 15) = 32768
_DISCORD_INTENTS = 1 | 512 | 32768  # = 33281

# Discord Gateway opcodes
_DISCORD_OP_DISPATCH   = 0   # Server -> Client: event dispatch
_DISCORD_OP_HEARTBEAT  = 1   # Client -> Server: heartbeat
_DISCORD_OP_IDENTIFY   = 2   # Client -> Server: identify
_DISCORD_OP_RESUME     = 6   # Client -> Server: resume
_DISCORD_OP_RECONNECT  = 7   # Server -> Client: reconnect request
_DISCORD_OP_INVALID    = 9   # Server -> Client: invalid session
_DISCORD_OP_HELLO      = 10  # Server -> Client: hello (heartbeat interval)
_DISCORD_OP_HEARTBEAT_ACK = 11  # Server -> Client: heartbeat acknowledged


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


async def _discord_get_gateway_url(ctx: GatewayContext, token: str) -> str:
    """Get the Discord Gateway WebSocket URL."""
    resp = await ctx.http.get(
        "https://discord.com/api/v10/gateway/bot",
        headers={"Authorization": f"Bot {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["url"]


async def discord_send_message(ctx: GatewayContext, token: str, channel_id: int, content: str):
    """Send a message to a Discord channel via REST API."""
    for chunk in _split_message(content, 1900):
        try:
            resp = await ctx.http.post(
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
                await ctx.http.post(
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


async def _discord_trigger_typing(ctx: GatewayContext, token: str, channel_id: int):
    """Trigger typing indicator in a Discord channel."""
    try:
        await ctx.http.post(
            f"https://discord.com/api/v10/channels/{channel_id}/typing",
            headers={"Authorization": f"Bot {token}"},
            timeout=5,
        )
    except Exception:
        pass


async def run_discord(ctx: GatewayContext):
    """Discord Gateway WebSocket listener using raw websockets.

    Implements the Discord Gateway protocol:
    - HELLO -> start heartbeating
    - IDENTIFY -> authenticate with token + intents
    - READY -> connected, start receiving events
    - MESSAGE_CREATE -> route through agent
    - Automatic reconnect + resume on disconnect

    No discord.py dependency — just websockets + httpx.
    """
    import websockets

    discord_token = ctx.tokens.get("discord", "")
    if not discord_token:
        log.error("Discord token missing — Discord channel disabled")
        return

    backoff = 1
    session_id_discord = None  # Discord session ID for resuming
    resume_url = None
    sequence = None  # Last sequence number received
    bot_user_id = None  # Our bot's user ID (set on READY)

    while not ctx.shutdown.is_set():
        try:
            # Get gateway URL
            if resume_url:
                gateway_url = f"{resume_url}?v=10&encoding=json"
            else:
                base_url = await _discord_get_gateway_url(ctx, discord_token)
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
                    while not ctx.shutdown.is_set():
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
                        if ctx.shutdown.is_set():
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
                                await _discord_handle_message(ctx, d, bot_user_id, discord_token)

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
        if not ctx.shutdown.is_set():
            log.info(f"Discord: reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _discord_handle_message(ctx: GatewayContext, data: dict,
                                   bot_user_id: str, discord_token: str):
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

    trace_id = gen_trace_id()
    log.info(f"[{trace_id}] Discord: message from {author.get('username', '?')}: {text[:60]}")

    sid = session_id("discord", channel_id)
    agent_id = "chat"
    channel_key = f"discord:{channel_id}"

    async def _handle():
        async with ctx.channel_locks[channel_key]:
            # Trigger typing indicator
            await _discord_trigger_typing(ctx, discord_token, int(channel_id))
            try:
                response = await run_agent(ctx, text, sid, agent_id, trace_id=trace_id)
                await discord_send_message(ctx, discord_token, int(channel_id), response)
            except Exception as e:
                log.error(f"Discord agent error: {e}", exc_info=True)
                await discord_send_message(
                    ctx, discord_token, int(channel_id),
                    "Something went wrong on my end, Little Mister."
                )

    # Run in background task to not block WebSocket message loop
    asyncio.create_task(_handle())
