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
import re
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
        rows = await pool.fetch("SELECT action_type, channel, level, arg_pattern, priority FROM autonomy_rules")
        new_cache = {}
        for row in rows:
            # 2026-10-01: rules may be argument-scoped. Plain rules keep the old (tool, channel)
            # key; scoped rules live in a list under ("__scoped__", tool) and are tested by regex
            # against the call's JSON arguments. Most specific match wins (see check_autonomy).
            pat = row["arg_pattern"] if "arg_pattern" in row.keys() else None
            if pat:
                try:
                    rx = re.compile(pat, re.IGNORECASE)
                except re.error as e:
                    log.warning(f"[autonomy] bad arg_pattern for {row['action_type']}: {e}")
                    continue
                new_cache.setdefault(("__scoped__", row["action_type"]), []).append(
                    (row["channel"], rx, row["level"], int(row["priority"] or 0) if "priority" in row.keys() else 0))
            else:
                new_cache[(row["action_type"], row["channel"])] = row["level"]
        _cache = new_cache
        _cache_ts = time.time()
        log.info(f"[autonomy] Loaded {len(_cache)} rules")
    except Exception as e:
        log.error(f"[autonomy] Failed to load rules: {e}")
    return _cache


def channel_of(session_id: str) -> str:
    """'gw2:slack:C123' -> 'slack'; anything else -> '*'."""
    parts = (session_id or "").split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "gw2" and parts[1] else "*"


def scoped_level(tool_name: str, channel: str, params: dict | None) -> str | None:
    """Argument-scoped rule lookup against the in-memory cache (pure; used by check_autonomy)."""
    import json as _json
    rules = _cache.get(("__scoped__", tool_name)) or []
    if not rules:
        return None
    blob = _json.dumps(params or {}, sort_keys=True, default=str)
    best = None
    for ch, rx, level, prio in rules:
        if ch not in (channel, "*") or not rx.search(blob):
            continue
        score = (2 if ch == channel and ch != "*" else 0) + prio
        if best is None or score > best[0]:
            best = (score, level)
    return best[1] if best else None


async def check_autonomy(pool, tool_name: str, channel: str = "*", params: dict | None = None) -> str:
    """Check autonomy level for a tool on a channel. Returns 'auto'|'notify'|'approve'.
    Precedence: argument-scoped rule (exact channel beats wildcard, then priority) >
    exact tool+channel > tool+'*' > default 'notify'."""
    global _cache, _cache_ts

    if time.time() - _cache_ts > _CACHE_TTL:
        await load_autonomy_cache(pool)
    scoped = scoped_level(tool_name, channel, params)
    if scoped:
        return scoped

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
                      reason: str = "", set_by: str = "jordan", arg_pattern: str | None = None):
    """Create or update an autonomy rule."""
    global _cache_ts
    try:
        await pool.execute(
            """INSERT INTO autonomy_rules (action_type, channel, level, reason, set_by, arg_pattern)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (action_type, channel, (coalesce(arg_pattern, '')))
               DO UPDATE SET level = $3, reason = $4, set_by = $5, updated_at = now()""",
            action_type, channel, level, reason, set_by, arg_pattern
        )
        _cache_ts = 0  # Force cache refresh
    except Exception as e:
        log.error(f"[autonomy] Failed to update rule: {e}")
