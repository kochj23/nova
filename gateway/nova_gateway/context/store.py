"""
store.py — PostgreSQL-based shared context/memory bus.

Uses asyncpg for async PostgreSQL access. All AI backends can read/write
shared context via the gateway API.

Tables (in the nova_ops database):
  gateway_sessions         — active session metadata
  gateway_context          — message-style context (role/content per session)
  gateway_context_entries  — key/value pairs per session with optional TTL
  gateway_query_log        — analytics log of every query routed through the gateway

Author: Jordan Koch
"""

import asyncpg
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from .. import config

logger = logging.getLogger(__name__)

# Additional tables the store needs beyond the two pre-created ones.
# gateway_sessions and gateway_context are assumed to exist already.
_EXTRA_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS gateway_context_entries (
        id          SERIAL PRIMARY KEY,
        session_id  TEXT   NOT NULL,
        key         TEXT   NOT NULL,
        value       TEXT   NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at  TIMESTAMPTZ,
        UNIQUE(session_id, key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_gce_session
        ON gateway_context_entries(session_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_gce_expires
        ON gateway_context_entries(expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS gateway_query_log (
        id              SERIAL PRIMARY KEY,
        session_id      TEXT,
        task_type       TEXT,
        backend_used    TEXT,
        model_used      TEXT,
        prompt_length   INTEGER,
        response_length INTEGER,
        latency_ms      DOUBLE PRECISION,
        fallback_used   BOOLEAN DEFAULT FALSE,
        validated       BOOLEAN DEFAULT FALSE,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_gql_created
        ON gateway_query_log(created_at)
    """,
]


class ContextStore:
    def __init__(self):
        self._dsn = config.pg_dsn()
        self._pool: Optional[asyncpg.Pool] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        self._pool = await asyncpg.create_pool(dsn=self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            for stmt in _EXTRA_SCHEMA:
                await conn.execute(stmt)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(f"ContextStore: PostgreSQL pool opened ({self._dsn})")

    async def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._pool:
            await self._pool.close()

    # -- Context read/write --------------------------------------------------

    async def write(self, session_id: str, key: str, value: str, ttl_seconds: Optional[int] = None):
        now = _now()
        expires = None
        if ttl_seconds is not None:
            expires = _future(ttl_seconds)
        elif config.context_ttl():
            expires = _future(config.context_ttl())

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO gateway_context_entries (session_id, key, value, created_at, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (session_id, key)
                DO UPDATE SET value = EXCLUDED.value,
                              created_at = EXCLUDED.created_at,
                              expires_at = EXCLUDED.expires_at
                """,
                session_id, key, value, now, expires,
            )
        await self._touch_session(session_id)

    async def read(self, session_id: str, key: str) -> Optional[str]:
        now = _now()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT value FROM gateway_context_entries
                WHERE session_id = $1 AND key = $2
                  AND (expires_at IS NULL OR expires_at > $3)
                """,
                session_id, key, now,
            )
            return row["value"] if row else None

    async def read_all(self, session_id: str) -> dict[str, str]:
        now = _now()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, value FROM gateway_context_entries
                WHERE session_id = $1
                  AND (expires_at IS NULL OR expires_at > $2)
                """,
                session_id, now,
            )
            return {r["key"]: r["value"] for r in rows}

    async def delete(self, session_id: str, key: str):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM gateway_context_entries WHERE session_id = $1 AND key = $2",
                session_id, key,
            )

    async def delete_session(self, session_id: str):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM gateway_context_entries WHERE session_id = $1",
                session_id,
            )
            await conn.execute(
                "DELETE FROM gateway_context WHERE session_id = $1",
                session_id,
            )
            await conn.execute(
                "DELETE FROM gateway_sessions WHERE session_id = $1",
                session_id,
            )

    # -- Analytics -----------------------------------------------------------

    async def log_query(
        self, session_id: Optional[str], task_type: str, backend_used: str,
        model_used: Optional[str], prompt_length: int, response_length: int,
        latency_ms: float, fallback_used: bool = False, validated: bool = False,
    ):
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO gateway_query_log
                    (session_id, task_type, backend_used, model_used,
                     prompt_length, response_length, latency_ms,
                     fallback_used, validated, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                session_id, task_type, backend_used, model_used,
                prompt_length, response_length, latency_ms,
                fallback_used, validated, _now(),
            )
        if session_id:
            await self._touch_session(session_id, increment=True)

    async def stats(self) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(DISTINCT session_id) AS n FROM gateway_sessions"
            )
            active_sessions = row["n"] if row else 0
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM gateway_query_log"
            )
            total_queries = row["n"] if row else 0
        return {"active_sessions": active_sessions, "total_queries": total_queries}

    async def recent_queries(self, limit: int = 20) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM gateway_query_log ORDER BY created_at DESC LIMIT $1",
                limit,
            )
            result = []
            for row in rows:
                d = dict(row)
                # Convert datetime objects to ISO strings for JSON serialization
                for k, v in d.items():
                    if isinstance(v, datetime):
                        d[k] = v.isoformat()
                result.append(d)
            return result

    # -- Internal ------------------------------------------------------------

    async def _touch_session(self, session_id: str, increment: bool = False):
        """Upsert session row, updating last_activity_at.

        The `increment` parameter is accepted for API compatibility but has no
        effect — query counts are derived from gateway_query_log instead of a
        counter column.
        """
        now = _now()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO gateway_sessions (session_id, created_at, last_activity_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (session_id)
                DO UPDATE SET last_activity_at = EXCLUDED.last_activity_at
                """,
                session_id, now, now,
            )

    async def _cleanup_loop(self):
        interval = config.get().get("context", {}).get("cleanup_interval_seconds", 300)
        while True:
            await asyncio.sleep(interval)
            try:
                now = _now()
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """
                        DELETE FROM gateway_context_entries
                        WHERE expires_at IS NOT NULL AND expires_at <= $1
                        """,
                        now,
                    )
                    # Remove sessions with no context entries and last seen > 24h
                    cutoff = _future(-86400)
                    await conn.execute(
                        """
                        DELETE FROM gateway_sessions
                        WHERE last_activity_at < $1
                          AND session_id NOT IN (
                              SELECT DISTINCT session_id FROM gateway_context_entries
                          )
                        """,
                        cutoff,
                    )
                logger.debug("ContextStore: cleanup pass complete")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"ContextStore: cleanup error: {e}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _future(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)
