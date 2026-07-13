"""
nova_gateway.channels.slack — Slack Socket Mode WebSocket listener.

Written by Jordan Koch.
"""

import asyncio
import json
import logging

from nova_gateway.config import (
    SLACK_NOTIFY_CHANNEL, SLACK_CHAT_CHANNEL,
    SLACK_CLAUDE_CHANNEL, JORDAN_DM_CHANNEL,
)
from nova_gateway.context import GatewayContext
from nova_gateway.agent import run_agent, write_message_for_claude, session_id, gen_trace_id
from nova_gateway.session import get_pg

log = logging.getLogger("nova_gateway_v2")

# Channels Nova listens on for Slack messages
_SLACK_LISTEN_CHANNELS = {SLACK_CHAT_CHANNEL, SLACK_CLAUDE_CHANNEL, JORDAN_DM_CHANNEL}


async def slack_post_message(ctx: GatewayContext, token: str, channel: str,
                              text: str, thread_ts: str = ""):
    """Post a message to Slack via chat.postMessage REST API."""
    payload = {"channel": channel, "text": text, "mrkdwn": True}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        resp = await ctx.http.post(
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


async def _slack_get_bot_user_id(ctx: GatewayContext, token: str) -> str:
    """Get our bot user ID via auth.test."""
    try:
        resp = await ctx.http.post(
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


async def _slack_get_ws_url(ctx: GatewayContext, app_token: str) -> str:
    """Get Socket Mode WebSocket URL via apps.connections.open."""
    resp = await ctx.http.post(
        "https://slack.com/api/apps.connections.open",
        headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        return data["url"]
    raise RuntimeError(f"apps.connections.open failed: {data.get('error', 'unknown')}")


async def run_slack(ctx: GatewayContext):
    """Slack Socket Mode listener using raw WebSocket (websockets library).

    Connects to Slack's Socket Mode WebSocket endpoint, receives events,
    acknowledges them, and routes messages through run_agent().
    Reconnects automatically with exponential backoff on disconnect.
    """
    import websockets

    bot_token = ctx.tokens.get("slack_bot", "")
    app_token = ctx.tokens.get("slack_app", "")
    if not bot_token or not app_token:
        log.error("Slack tokens missing — Slack channel disabled")
        return

    # Get our bot user ID to ignore our own messages
    bot_user_id = await _slack_get_bot_user_id(ctx, bot_token)
    if not bot_user_id:
        log.error("Slack: could not determine bot user ID — channel disabled")
        return

    log.info(f"Slack: bot user ID = {bot_user_id}")

    backoff = 1  # Exponential backoff seconds

    while not ctx.shutdown.is_set():
        ws = None
        try:
            # Get fresh WebSocket URL (they expire)
            ws_url = await _slack_get_ws_url(ctx, app_token)
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
                    if ctx.shutdown.is_set():
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
                        await _slack_handle_event(ctx, event, bot_user_id, bot_token)

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
        if not ctx.shutdown.is_set():
            log.info(f"Slack: reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ── Deterministic action routing (make Nova DO tasks instead of confabulating) ──
import re as _re

_ACTION_VERB = _re.compile(
    r'\b(write|create|publish|post|make|generate|draft|compose|build|'
    r'restart|reboot|start|stop|deploy|run|execute|fix|patch|update|upgrade|install|'
    r'add|remove|delete|set|change|configure|schedule|send|email|rename|move|copy|'
    r'check|scan|test|verify|pull|fetch|download|kill|enable|disable|clean|render)\b',
    _re.I)
_LEAD_FILLER = _re.compile(
    r'^(hey|ok|okay|yo|so|and|also|nova|please|can you|could you|would you|will you|'
    r"i want you to|i need you to|i'd like you to|go ahead and|when you can|for me)[ ,:]*",
    _re.I)


def _is_action_request(text: str) -> bool:
    """True if the message is a 'do something' task, not just chat. Biased toward
    routing — a confabulated 'done' is worse than a slower real answer."""
    core = (text or "").strip()
    if len(core) < 8:
        return False
    for _ in range(5):                       # strip stacked fillers: "nova, please can you ..."
        stripped = _LEAD_FILLER.sub("", core).strip()
        if stripped == core:
            break
        core = stripped
    return bool(_ACTION_VERB.match(core) or _ACTION_VERB.search(core[:50]))


def _action_ack(text: str) -> str:
    return ("Ugh, actual *work*. Fine — handing this to Claude Code so it gets *done* "
            "instead of me just telling you it's done. Real result posts here shortly.")


async def _slack_handle_event(ctx: GatewayContext, event: dict, bot_user_id: str, bot_token: str):
    """Process a single Slack event from Socket Mode."""
    import time

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

    trace_id = gen_trace_id()
    log.info(f"[{trace_id}] Slack: message from {event.get('user', '?')}: {text[:60]}")

    sid = session_id("slack", channel)
    agent_id = "chat"

    async def _handle():
        async with ctx.channel_locks[f"slack:{channel}"]:
            try:
                # #nova-claude routes to the REAL Claude Code session, NOT the Ollama chat
                # agent. nova_claude_code_responder.py consumes this to_claude_code row,
                # runs it through `claude -p` (read-only) and posts the reply back to
                # #nova-claude itself. Bot/self messages are filtered above, so the reply
                # is not re-ingested — no echo loop. (#672)
                if channel == SLACK_CLAUDE_CHANNEL:
                    pool = await get_pg(ctx)
                    await pool.execute(
                        """INSERT INTO claude_messages (direction, sender, message, metadata)
                           VALUES ('to_claude_code', $1, $2, $3::jsonb)""",
                        event.get("user", "slack"), text,
                        json.dumps({"channel": "slack-claude", "thread_ts": thread_ts,
                                    "timestamp": time.time()}),
                    )
                    log.info(f"[{trace_id}] Slack #nova-claude -> to_claude_code (real Claude Code)")
                    return

                # Spatial proximity Q&A: "anything near me?", "closest thing on the scanner?" —
                # answered directly from the distance/bearing-enriched scanner memories (no LLM
                # round-trip, no confabulation). Only Jordan; non-spatial text returns None and
                # falls through to the action router / chat below.
                if event.get("user") == "U049EPC2W":
                    try:
                        import os as _os, sys as _sys
                        _sd = _os.path.expanduser("~/.openclaw/scripts")
                        if _sd not in _sys.path:
                            _sys.path.insert(0, _sd)
                        from nova_spatial_query import answer as _spatial_answer
                        _sa = _spatial_answer(text)
                    except Exception as _e:
                        _sa = None
                        log.warning(f"spatial Q&A failed: {_e}")
                    if _sa:
                        await slack_post_message(ctx, bot_token, channel, _sa, thread_ts=thread_ts)
                        log.info(f"[{trace_id}] Slack spatial Q&A answered directly")
                        return

                    # Change/status grounding: "what changed today?", "look in the ops DB" —
                    # answered from claude_actions + git + memory so the chat model can't
                    # confabulate "nothing happened." (sys.path already set above.)
                    try:
                        from nova_status_query import answer as _status_answer
                        # voices the facts via an LLM — offload so it never blocks the event loop
                        _st = await asyncio.get_event_loop().run_in_executor(None, _status_answer, text)
                    except Exception as _e:
                        _st = None
                        log.warning(f"status Q&A failed: {_e}")
                    if _st:
                        await slack_post_message(ctx, bot_token, channel, _st, thread_ts=thread_ts)
                        log.info(f"[{trace_id}] Slack status Q&A answered directly")
                        return

                # Deterministic action routing (#action-router): a "do something" request in
                # any listened channel goes to the REAL Claude Code executor, which does the
                # work and posts the real result back to THIS channel (origin_channel metadata).
                # This is the fix for Nova confabulating "done" on tasks the Ollama agent can't do.
                # SECURITY: only Jordan (U049EPC2W) may trigger the cloud executor — a channel
                # guest cannot route arbitrary tasks to Claude Code. Others fall through to chat.
                if _is_action_request(text) and event.get("user") == "U049EPC2W":
                    pool = await get_pg(ctx)
                    await pool.execute(
                        """INSERT INTO claude_messages (direction, sender, message, metadata)
                           VALUES ('to_claude_code', $1, $2, $3::jsonb)""",
                        event.get("user", "slack"), text,
                        json.dumps({"origin_channel": channel, "origin_thread": thread_ts,
                                    "channel": "slack-action-router", "timestamp": time.time()}),
                    )
                    log.info(f"[{trace_id}] Slack action -> to_claude_code (real executor): {text[:50]}")
                    await slack_post_message(ctx, bot_token, channel, _action_ack(text), thread_ts=thread_ts)
                    return

                log.info(f"[{trace_id}] Slack: routing to agent — session={sid}")
                response = await run_agent(ctx, text, sid, agent_id, trace_id=trace_id)
                if not response or not response.strip():
                    log.warning(f"Slack: agent returned empty response for: {text[:60]}")
                    response = "I'm thinking about that but came up empty. Can you rephrase?"
                await slack_post_message(ctx, bot_token, channel, response, thread_ts=thread_ts)
                log.info(f"Slack: responded in {channel} ({len(response)} chars)")
            except Exception as e:
                log.error(f"Slack agent error: {e}", exc_info=True)
                await slack_post_message(ctx, bot_token, channel, "Sorry, something went wrong on my end.")

    # Run in background task to not block WebSocket message loop
    asyncio.create_task(_handle())
