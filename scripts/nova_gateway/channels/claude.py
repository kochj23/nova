"""
nova_gateway.channels.claude — Claude Code bidirectional bridge channel.

Written by Jordan Koch.
"""

import asyncio
import json
import logging
import time

from nova_gateway.config import (
    SLACK_NOTIFY_CHANNEL, CLAUDE_BRIDGE_SESSION,
)
from nova_gateway.context import GatewayContext
from nova_gateway.agent import (
    run_agent, write_message_for_claude, session_id, gen_trace_id,
    _get_redis,
)
from nova_gateway.session import get_pg
from nova_gateway.channels.slack import slack_post_message

log = logging.getLogger("nova_gateway_v2")


async def run_claude_channel(ctx: GatewayContext):
    """Poll claude_messages for messages from Claude Code and respond.

    Also monitors the Redis scratchpad for Claude's current task,
    and cleans up stale messages when Claude is inactive.
    """
    log.info("Claude Code channel starting (poll mode)...")

    # Track last processed message ID to avoid re-processing
    last_processed_id = 0

    # Initialize: get the current max ID so we don't replay old messages
    try:
        pool = await get_pg(ctx)
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

    while not ctx.shutdown.is_set():
        try:
            # ── Poll for new messages from Claude Code ────────────────────────
            pool = await get_pg(ctx)
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

                trace_id = gen_trace_id()
                log.info(f"[{trace_id}] Claude channel: processing message #{msg_id}: {message_text[:60]}")

                sid = session_id("claude-code", CLAUDE_BRIDGE_SESSION)
                agent_id = "chat"

                async with ctx.channel_locks["claude-code"]:
                    try:
                        response = await run_agent(
                            ctx, message_text, sid, agent_id, trace_id=trace_id
                        )
                        # Write response back to claude_messages
                        await write_message_for_claude(
                            ctx,
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
                        await write_message_for_claude(
                            ctx,
                            f"Error processing your message: {e}",
                            metadata={"channel": "bridge", "in_reply_to": msg_id, "error": True},
                        )

            # ── Scratchpad check (every 60s = ~12 poll cycles at 5s each) ────
            scratchpad_check_counter += 1
            if scratchpad_check_counter >= 12:
                scratchpad_check_counter = 0
                await _check_claude_scratchpad(ctx)

            # ── Notify Jordan every 5 min if Claude-Nova chat is active ──
            notify_counter += 1
            if notify_counter >= 60:  # 60 * 5s = 5 min
                notify_counter = 0
                if claude_msg_count > 0:
                    try:
                        summary = f":robot_face: *Claude <-> Nova Activity* ({claude_msg_count} messages in last 5 min)\n  Current task: {ctx.claude_active_task or 'general collaboration'}"
                        await slack_post_message(
                            ctx, ctx.tokens["slack_bot"], SLACK_NOTIFY_CHANNEL, summary)
                    except Exception:
                        pass
                    claude_msg_count = 0

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Claude channel error: {e}")

        # Poll interval: 5 seconds
        await asyncio.sleep(5)


async def _check_claude_scratchpad(ctx: GatewayContext):
    """Check Redis scratchpad for Claude's current task and editing locks.

    When the key exists, store it internally so Nova avoids restarting
    services Claude is working on. When expired/empty, clean up stale messages.
    Also scans for nova:editing:* keys to track files Claude is editing.
    """
    try:
        r = _get_redis(ctx)
        if not r:
            return

        task_value = r.get("nova:scratchpad:claude_current_task")

        if task_value:
            if task_value != ctx.claude_active_task:
                log.info(f"Claude active task detected: {task_value}")
            ctx.claude_active_task = task_value
        else:
            if ctx.claude_active_task:
                log.info("Claude task cleared (session ended or task completed)")
            ctx.claude_active_task = None

            # Claude is not active — clean up stale messages older than 24 hours
            try:
                pool = await get_pg(ctx)
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
            ctx.claude_editing_files = [k.replace("nova:editing:", "", 1) for k in editing_keys]
        else:
            ctx.claude_editing_files = []

    except Exception as e:
        log.debug(f"Scratchpad check failed (non-fatal): {e}")
