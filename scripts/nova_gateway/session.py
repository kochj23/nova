"""
nova_gateway.session — PostgreSQL pool management, schema creation, logging, and traces.

Written by Jordan Koch.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid

import asyncpg

from nova_gateway.config import PG_DSN, VERSION
from nova_gateway.context import GatewayContext

log = logging.getLogger("nova_gateway_v2")


async def get_pg(ctx: GatewayContext) -> asyncpg.Pool:
    """Get or create the PG pool."""
    if ctx.pg_pool is None:
        for attempt in range(10):
            try:
                ctx.pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=8, command_timeout=30)
                break
            except Exception as e:
                if attempt == 9:
                    raise
                await asyncio.sleep(3)
    return ctx.pg_pool


async def log_session_start(ctx: GatewayContext, session_id: str, channel: str, agent_id: str):
    pool = await get_pg(ctx)
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


async def log_turn(ctx: GatewayContext, session_id: str, agent_id: str, role: str,
                   content: str, model: str = "", turn_index: int = 0):
    pool = await get_pg(ctx)
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


async def log_privacy_block(ctx: GatewayContext, messages: list):
    """Log a privacy policy block to gateway_query_log for auditing."""
    try:
        pool = await get_pg(ctx)
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


async def log_degraded_event(ctx: GatewayContext, event_type: str, notes: str):
    """Record a degraded-mode event in gateway_query_log for debugging."""
    try:
        pool = await get_pg(ctx)
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


async def log_tool_execution(ctx: GatewayContext, session_id: str, tool_name: str,
                              tool_params: dict, tool_result: str, duration_ms: int):
    """Log a tool execution to gateway_query_log with tool-specific columns."""
    pool = await get_pg(ctx)
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


async def log_trace(ctx: GatewayContext, trace_id: str, channel: str, agent_id: str,
                    user_message: str, response: str, backend_used: str,
                    tool_calls: list, ttft_ms: int, total_ms: int,
                    tokens_in: int, tokens_out: int):
    """Write a complete trace record to gateway_traces."""
    pool = await get_pg(ctx)
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


async def ensure_pg_schema(ctx: GatewayContext):
    """Create gateway tables and Claude communication tables if they don't exist."""
    pool = await get_pg(ctx)
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
