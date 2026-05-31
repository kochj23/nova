"""
nova_gateway.autonomy — Graduated autonomy levels for tool dispatch.

Checks {action_type x channel} -> auto|notify|approve before every tool execution.
- auto: silent execution
- notify: execute + Slack notification to Jordan
- approve: queue + ask Jordan first, execute on approval

Written by Jordan Koch (via Claude).
"""

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("nova_gateway_v2")

_cache: dict = {}
_cache_ts: float = 0
_CACHE_TTL = 300  # 5 minutes


async def load_autonomy_cache(pool) -> dict:
    """Load all autonomy rules into memory cache."""
    global _cache, _cache_ts
    try:
        rows = await pool.fetch("SELECT action_type, channel, level FROM autonomy_rules")
        new_cache = {}
        for row in rows:
            key = (row["action_type"], row["channel"])
            new_cache[key] = row["level"]
        _cache = new_cache
        _cache_ts = time.time()
        log.info(f"[autonomy] Loaded {len(_cache)} rules")
    except Exception as e:
        log.error(f"[autonomy] Failed to load rules: {e}")
    return _cache


async def check_autonomy(pool, tool_name: str, channel: str = "*") -> str:
    """Check autonomy level for a tool on a channel. Returns 'auto'|'notify'|'approve'."""
    global _cache, _cache_ts

    if time.time() - _cache_ts > _CACHE_TTL:
        await load_autonomy_cache(pool)

    # Most specific match: exact tool + exact channel
    level = _cache.get((tool_name, channel))
    if level:
        return level

    # Wildcard channel match
    level = _cache.get((tool_name, "*"))
    if level:
        return level

    # Unknown tool defaults to notify
    return "notify"


async def request_approval(pool, trace_id: str, session_id: str,
                           tool_name: str, params: dict, context: str = "") -> str:
    """Insert a pending approval request. Returns pending_id."""
    import json
    try:
        row = await pool.fetchrow(
            """INSERT INTO autonomy_pending (trace_id, session_id, action_type, tool_params, context_preview)
               VALUES ($1, $2, $3, $4, $5) RETURNING pending_id""",
            trace_id, session_id, tool_name, json.dumps(params), context[:500]
        )
        return row["pending_id"] if row else ""
    except Exception as e:
        log.error(f"[autonomy] Failed to create pending: {e}")
        return ""


async def resolve_pending(pool, pending_id: str, approved: bool, resolved_by: str = "jordan") -> Optional[dict]:
    """Resolve a pending approval. Returns the original params if approved."""
    import json
    try:
        status = "approved" if approved else "denied"
        row = await pool.fetchrow(
            """UPDATE autonomy_pending
               SET status = $1, resolved_at = now(), resolved_by = $2
               WHERE pending_id = $3 AND status = 'pending'
               RETURNING action_type, tool_params""",
            status, resolved_by, pending_id
        )
        if row and approved:
            return {"action_type": row["action_type"], "tool_params": json.loads(row["tool_params"])}
        return None
    except Exception as e:
        log.error(f"[autonomy] Failed to resolve pending: {e}")
        return None


async def notify_execution(pool, tool_name: str, params: dict, result_preview: str,
                           slack_post_fn=None):
    """Fire-and-forget notification that a tool was executed."""
    if slack_post_fn:
        import json
        params_preview = json.dumps(params)[:200]
        msg = (f":gear: *Auto-executed:* `{tool_name}`\n"
               f"Params: `{params_preview}`\n"
               f"Result: {result_preview[:200]}")
        try:
            await slack_post_fn(msg)
        except Exception:
            pass


async def get_pending_approvals(pool, limit: int = 10) -> list:
    """Get all pending approval requests."""
    try:
        rows = await pool.fetch(
            """SELECT pending_id, action_type, tool_params, context_preview, created_at
               FROM autonomy_pending WHERE status = 'pending'
               ORDER BY created_at DESC LIMIT $1""",
            limit
        )
        return [dict(r) for r in rows]
    except Exception:
        return []


async def update_rule(pool, action_type: str, channel: str, level: str,
                      reason: str = "", set_by: str = "jordan"):
    """Create or update an autonomy rule."""
    global _cache_ts
    try:
        await pool.execute(
            """INSERT INTO autonomy_rules (action_type, channel, level, reason, set_by)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (action_type, channel)
               DO UPDATE SET level = $3, reason = $4, set_by = $5, updated_at = now()""",
            action_type, channel, level, reason, set_by
        )
        _cache_ts = 0  # Force cache refresh
    except Exception as e:
        log.error(f"[autonomy] Failed to update rule: {e}")
