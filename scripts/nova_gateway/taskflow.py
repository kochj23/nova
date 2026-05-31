"""
nova_gateway.taskflow — Durable multi-step orchestration for Nova.

State machine for background jobs that survive restarts, can wait on human
input, and track child tasks. Uses existing flow_runs + task_runs tables.

Lifecycle: create → step → [wait → resume]* → finish|fail

Written by Jordan Koch (via Claude).
"""

import asyncio
import json
import logging
import time
import uuid

log = logging.getLogger("nova_gateway_v2")


def _now_ms():
    return int(time.time() * 1000)


async def create_flow(pool, goal: str, first_step: str, state: dict = None,
                      owner_key: str = "nova", controller_id: str = "nova_gateway") -> str:
    """Create a new managed flow. Returns flow_id."""
    flow_id = str(uuid.uuid4())
    now = _now_ms()
    try:
        await pool.execute(
            """INSERT INTO flow_runs
               (flow_id, shape, sync_mode, owner_key, controller_id, revision, status,
                notify_policy, goal, current_step, state_json, created_at, updated_at)
               VALUES ($1, 'linear', 'managed', $2, $3, 0, 'running', 'on_finish', $4, $5, $6, $7, $7)""",
            flow_id, owner_key, controller_id, goal, first_step,
            json.dumps(state or {}), now
        )
        log.info(f"[taskflow] Created flow {flow_id[:8]}: {goal}")
        return flow_id
    except Exception as e:
        log.error(f"[taskflow] create_flow failed: {e}")
        return ""


async def advance_step(pool, flow_id: str, new_step: str, state_update: dict = None) -> bool:
    """Advance flow to a new step, optionally updating state."""
    try:
        row = await pool.fetchrow(
            "SELECT revision, state_json FROM flow_runs WHERE flow_id = $1 AND status = 'running'",
            flow_id
        )
        if not row:
            return False

        current_state = json.loads(row["state_json"] or "{}")
        if state_update:
            current_state.update(state_update)

        result = await pool.execute(
            """UPDATE flow_runs
               SET current_step = $1, state_json = $2, revision = revision + 1, updated_at = $3
               WHERE flow_id = $4 AND revision = $5""",
            new_step, json.dumps(current_state), _now_ms(), flow_id, row["revision"]
        )
        return "UPDATE 1" in result
    except Exception as e:
        log.error(f"[taskflow] advance_step failed: {e}")
        return False


async def set_waiting(pool, flow_id: str, wait_reason: str, wait_meta: dict = None) -> bool:
    """Transition flow to waiting state (needs human input or external event)."""
    try:
        row = await pool.fetchrow(
            "SELECT revision FROM flow_runs WHERE flow_id = $1 AND status = 'running'",
            flow_id
        )
        if not row:
            return False

        result = await pool.execute(
            """UPDATE flow_runs
               SET status = 'waiting', blocked_summary = $1, wait_json = $2,
                   revision = revision + 1, updated_at = $3
               WHERE flow_id = $4 AND revision = $5""",
            wait_reason, json.dumps(wait_meta or {}), _now_ms(), flow_id, row["revision"]
        )
        return "UPDATE 1" in result
    except Exception as e:
        log.error(f"[taskflow] set_waiting failed: {e}")
        return False


async def resume_flow(pool, flow_id: str, next_step: str, input_data: dict = None) -> bool:
    """Resume a waiting flow with new input."""
    try:
        row = await pool.fetchrow(
            "SELECT revision, state_json FROM flow_runs WHERE flow_id = $1 AND status = 'waiting'",
            flow_id
        )
        if not row:
            return False

        current_state = json.loads(row["state_json"] or "{}")
        if input_data:
            current_state["last_input"] = input_data

        result = await pool.execute(
            """UPDATE flow_runs
               SET status = 'running', current_step = $1, state_json = $2,
                   wait_json = NULL, blocked_summary = NULL,
                   revision = revision + 1, updated_at = $3
               WHERE flow_id = $4 AND revision = $5""",
            next_step, json.dumps(current_state), _now_ms(), flow_id, row["revision"]
        )
        return "UPDATE 1" in result
    except Exception as e:
        log.error(f"[taskflow] resume_flow failed: {e}")
        return False


async def finish_flow(pool, flow_id: str, final_state: dict = None) -> bool:
    """Mark flow as completed."""
    try:
        updates = {"status": "completed", "ended_at": _now_ms(), "updated_at": _now_ms()}
        state_clause = ""
        if final_state:
            state_clause = f", state_json = '{json.dumps(final_state)}'"

        result = await pool.execute(
            f"""UPDATE flow_runs
                SET status = 'completed', ended_at = $1, updated_at = $1{state_clause},
                    revision = revision + 1
                WHERE flow_id = $2 AND status IN ('running', 'waiting')""",
            _now_ms(), flow_id
        )
        log.info(f"[taskflow] Finished flow {flow_id[:8]}")
        return "UPDATE 1" in result
    except Exception as e:
        log.error(f"[taskflow] finish_flow failed: {e}")
        return False


async def fail_flow(pool, flow_id: str, error: str) -> bool:
    """Mark flow as failed."""
    try:
        result = await pool.execute(
            """UPDATE flow_runs
               SET status = 'failed', blocked_summary = $1, ended_at = $2, updated_at = $2,
                   revision = revision + 1
               WHERE flow_id = $3 AND status IN ('running', 'waiting')""",
            error[:500], _now_ms(), flow_id
        )
        return "UPDATE 1" in result
    except Exception as e:
        log.error(f"[taskflow] fail_flow failed: {e}")
        return False


async def get_flow(pool, flow_id: str) -> dict:
    """Get flow status and state."""
    try:
        row = await pool.fetchrow(
            """SELECT flow_id, status, goal, current_step, state_json, wait_json,
                      blocked_summary, revision, created_at, updated_at, ended_at
               FROM flow_runs WHERE flow_id = $1""",
            flow_id
        )
        if not row:
            return {}
        return {
            "flow_id": row["flow_id"],
            "status": row["status"],
            "goal": row["goal"],
            "step": row["current_step"],
            "state": json.loads(row["state_json"] or "{}"),
            "waiting_on": row["blocked_summary"],
            "revision": row["revision"],
        }
    except Exception as e:
        log.error(f"[taskflow] get_flow failed: {e}")
        return {}


async def list_active_flows(pool, limit: int = 10) -> list:
    """List all active (running/waiting) flows."""
    try:
        rows = await pool.fetch(
            """SELECT flow_id, status, goal, current_step, blocked_summary, updated_at
               FROM flow_runs WHERE status IN ('running', 'waiting')
               ORDER BY updated_at DESC LIMIT $1""",
            limit
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"[taskflow] list_active_flows failed: {e}")
        return []
