"""
store.py — SQLite-based shared context/memory bus.

Replaces Redis with aiosqlite for zero-dependency local operation.
All AI backends can read/write shared context via the gateway API.

Tables:
  context_entries  — key/value pairs per session with optional TTL
  query_log        — analytics log of every query routed through the gateway
  sessions         — active session metadata

Author: Jordan Koch
"""

import aiosqlite
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from .. import config

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT,
    UNIQUE(session_id, key) ON CONFLICT REPLACE
);

CREATE TABLE IF NOT EXISTS query_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT,
    task_type       TEXT,
    backend_used    TEXT,
    model_used      TEXT,
    prompt_length   INTEGER,
    response_length INTEGER,
    latency_ms      REAL,
    fallback_used   INTEGER DEFAULT 0,
    validated       INTEGER DEFAULT 0,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    query_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_context_session ON context_entries(session_id);
CREATE INDEX IF NOT EXISTS idx_context_expires ON context_entries(expires_at);
CREATE INDEX IF NOT EXISTS idx_log_created    ON query_log(created_at);
"""


class ContextStore:
    def __init__(self):
        self._db_path = config.db_path()
        self._db: Optional[aiosqlite.Connection] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(f"ContextStore: SQLite opened at {self._db_path}")

    async def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._db:
            await self._db.close()

    # ── Context read/write ──────────────────────────────────────────────────

    async def write(self, session_id: str, key: str, value: str, ttl_seconds: Optional[int] = None):
        now = _now()
        expires = None
        if ttl_seconds is not None:
            expires = _future(ttl_seconds)
        elif config.context_ttl():
            expires = _future(config.context_ttl())

        await self._db.execute(
            "INSERT OR REPLACE INTO context_entries (session_id, key, value, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, key, value, now, expires)
        )
        await self._db.commit()
        await self._touch_session(session_id)

    async def read(self, session_id: str, key: str) -> Optional[str]:
        now = _now()
        async with self._db.execute(
            "SELECT value FROM context_entries "
            "WHERE session_id=? AND key=? AND (expires_at IS NULL OR expires_at > ?)",
            (session_id, key, now)
        ) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None

    async def read_all(self, session_id: str) -> dict[str, str]:
        now = _now()
        result = {}
        async with self._db.execute(
            "SELECT key, value FROM context_entries "
            "WHERE session_id=? AND (expires_at IS NULL OR expires_at > ?)",
            (session_id, now)
        ) as cur:
            async for row in cur:
                result[row["key"]] = row["value"]
        return result

    async def delete(self, session_id: str, key: str):
        await self._db.execute(
            "DELETE FROM context_entries WHERE session_id=? AND key=?",
            (session_id, key)
        )
        await self._db.commit()

    async def delete_session(self, session_id: str):
        await self._db.execute("DELETE FROM context_entries WHERE session_id=?", (session_id,))
        await self._db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        await self._db.commit()

    # ── Analytics ───────────────────────────────────────────────────────────

    async def log_query(
        self, session_id: Optional[str], task_type: str, backend_used: str,
        model_used: Optional[str], prompt_length: int, response_length: int,
        latency_ms: float, fallback_used: bool = False, validated: bool = False
    ):
        await self._db.execute(
            "INSERT INTO query_log (session_id, task_type, backend_used, model_used, "
            "prompt_length, response_length, latency_ms, fallback_used, validated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, task_type, backend_used, model_used, prompt_length,
             response_length, latency_ms, int(fallback_used), int(validated), _now())
        )
        await self._db.commit()
        if session_id:
            await self._touch_session(session_id, increment=True)

    async def stats(self) -> dict:
        async with self._db.execute("SELECT COUNT(DISTINCT session_id) as n FROM sessions") as cur:
            row = await cur.fetchone()
            active_sessions = row["n"] if row else 0
        async with self._db.execute("SELECT COUNT(*) as n FROM query_log") as cur:
            row = await cur.fetchone()
            total_queries = row["n"] if row else 0
        return {"active_sessions": active_sessions, "total_queries": total_queries}

    async def recent_queries(self, limit: int = 20) -> list[dict]:
        rows = []
        async with self._db.execute(
            "SELECT * FROM query_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            async for row in cur:
                rows.append(dict(row))
        return rows

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _touch_session(self, session_id: str, increment: bool = False):
        now = _now()
        await self._db.execute(
            "INSERT INTO sessions (session_id, created_at, last_seen, query_count) "
            "VALUES (?, ?, ?, 0) ON CONFLICT(session_id) DO UPDATE SET "
            "last_seen=excluded.last_seen" + (", query_count=query_count+1" if increment else ""),
            (session_id, now, now)
        )
        await self._db.commit()

    async def _cleanup_loop(self):
        interval = config.get().get("context", {}).get("cleanup_interval_seconds", 300)
        while True:
            await asyncio.sleep(interval)
            try:
                now = _now()
                await self._db.execute(
                    "DELETE FROM context_entries WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (now,)
                )
                # Remove sessions with no context and last seen > 24h
                cutoff = _future(-86400)
                await self._db.execute(
                    "DELETE FROM sessions WHERE last_seen < ? AND session_id NOT IN "
                    "(SELECT DISTINCT session_id FROM context_entries)",
                    (cutoff,)
                )
                await self._db.commit()
                logger.debug("ContextStore: cleanup pass complete")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"ContextStore: cleanup error: {e}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
