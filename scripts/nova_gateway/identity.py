"""
nova_gateway.identity — Cross-device identity continuity.

Maps all channels to a single user identity. Shares conversation context
across channels so Nova knows what Jordan was just talking about elsewhere.
Adapts response style based on device (mobile=concise, desktop=full).

Written by Jordan Koch (via Claude).
"""

import asyncio
import json
import logging
import time

log = logging.getLogger("nova_gateway_v2")

DEVICE_MODE_MAP = {
    "signal": "mobile",
    "slack": "desktop",
    "discord": "desktop",
    "claude": "desktop",
}

RESPONSE_STYLE = {
    "mobile": {"max_tokens": 256, "tone": "concise", "hint": "Keep responses short — Jordan is on mobile."},
    "desktop": {"max_tokens": 1024, "tone": "full", "hint": ""},
}


async def resolve_identity(pool, channel_type: str, channel_id: str) -> dict:
    """Resolve channel to user identity. Returns user profile + device mode."""
    try:
        # Upsert channel mapping
        await pool.execute(
            """INSERT INTO identity_channel_map (user_id, channel_type, channel_id, device_hint, last_active, message_count)
               VALUES ('jordan', $1, $2, $3, now(), 1)
               ON CONFLICT (channel_type, channel_id)
               DO UPDATE SET last_active = now(), message_count = identity_channel_map.message_count + 1""",
            channel_type, channel_id, DEVICE_MODE_MAP.get(channel_type, "desktop")
        )

        row = await pool.fetchrow(
            "SELECT user_id, display_name, active_channel, device_mode, preferences FROM user_identities WHERE user_id = 'jordan'"
        )
        if row:
            return {
                "user_id": row["user_id"],
                "display_name": row["display_name"],
                "active_channel": row["active_channel"],
                "device_mode": row["device_mode"] or DEVICE_MODE_MAP.get(channel_type, "desktop"),
                "preferences": row["preferences"] or {},
            }
    except Exception as e:
        log.error(f"[identity] resolve failed: {e}")

    return {"user_id": "jordan", "display_name": "Jordan",
            "device_mode": DEVICE_MODE_MAP.get(channel_type, "desktop"), "preferences": {}}


async def update_active_channel(pool, channel_type: str, channel_id: str):
    """Record that Jordan is now active on this channel."""
    try:
        device_mode = DEVICE_MODE_MAP.get(channel_type, "desktop")
        await pool.execute(
            """UPDATE user_identities
               SET active_channel = $1, active_since = now(), device_mode = $2
               WHERE user_id = 'jordan'""",
            f"{channel_type}:{channel_id}", device_mode
        )
    except Exception as e:
        log.error(f"[identity] update_active failed: {e}")


async def get_cross_context(pool, exclude_channel: str = "") -> str:
    """Get recent conversation context from OTHER channels (not the current one).
    Returns a brief summary string for system prompt injection."""
    try:
        # Clean expired entries
        await pool.execute("DELETE FROM cross_channel_context WHERE expires_at < now()")

        rows = await pool.fetch(
            """SELECT summary, source_channel, topics, created_at
               FROM cross_channel_context
               WHERE user_id = 'jordan' AND source_channel != $1
               ORDER BY created_at DESC LIMIT 3""",
            exclude_channel
        )
        if not rows:
            return ""

        parts = []
        for row in rows:
            topics = row["topics"] if isinstance(row["topics"], list) else json.loads(row["topics"] or "[]")
            topic_str = ", ".join(topics[:3]) if topics else "general"
            parts.append(f"[{row['source_channel']}] {row['summary']}")

        return "Recent context from other channels:\n" + "\n".join(parts)
    except Exception as e:
        log.error(f"[identity] get_cross_context failed: {e}")
        return ""


async def save_cross_context(pool, channel_type: str, session_id: str,
                             summary: str, topics: list = None):
    """Save a conversation summary for cross-channel context sharing."""
    if not summary or len(summary) < 20:
        return
    try:
        await pool.execute(
            """INSERT INTO cross_channel_context (user_id, summary, source_channel, source_session, topics)
               VALUES ('jordan', $1, $2, $3, $4)""",
            summary[:500], channel_type, session_id, json.dumps(topics or [])
        )
    except Exception as e:
        log.error(f"[identity] save_cross_context failed: {e}")


def get_response_style(device_mode: str) -> dict:
    """Get response style hints based on device mode."""
    return RESPONSE_STYLE.get(device_mode, RESPONSE_STYLE["desktop"])
