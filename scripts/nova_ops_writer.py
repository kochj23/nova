"""
nova_ops_writer.py — Async PostgreSQL writer for nova_ops operational data.

Provides a fire-and-forget write path for scheduler run records.
Uses a small asyncio queue so DB writes never block task execution.
Connection pool is lazily initialized on first write.

Written by Jordan Koch.
"""

import asyncio
import time
import uuid
from pathlib import Path
from typing import Optional

try:
    import asyncpg
    _ASYNCPG = True
except ImportError:
    _ASYNCPG = False

import sys
sys.path.insert(0, str(Path(__file__).parent))
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN, LOG_DEBUG

DB_DSN = "postgresql://kochj@192.168.1.6:5432/nova_ops"
_POOL: Optional[object] = None
_POOL_LOCK: Optional[asyncio.Lock] = None  # created lazily inside a running loop
_QUEUE: Optional[asyncio.Queue] = None
_WORKER_TASK = None


async def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    if not _ASYNCPG:
        return None
    # Use a module-level lock created at first coroutine call
    global _POOL_LOCK
    if _POOL_LOCK is None:
        _POOL_LOCK = asyncio.Lock()
    async with _POOL_LOCK:
        if _POOL is not None:
            return _POOL
        try:
            _POOL = await asyncpg.create_pool(
                DB_DSN,
                min_size=1,
                max_size=3,
                command_timeout=10,
                max_inactive_connection_lifetime=300,
            )
            log("nova_ops writer pool ready", level=LOG_INFO, source="nova_ops_writer")
        except Exception as e:
            log(f"nova_ops pool init failed: {e} — run history disabled", level=LOG_WARN, source="nova_ops_writer")
            _POOL = None
    return _POOL


async def _worker():
    """Drain the write queue, retrying failed writes up to 3 times with backoff."""
    global _QUEUE
    while True:
        try:
            op, args = await _QUEUE.get()
            pool = await _get_pool()
            if pool is None:
                _QUEUE.task_done()
                continue
            for attempt in range(3):
                try:
                    async with pool.acquire() as conn:
                        await op(conn, *args)
                    break
                except Exception as e:
                    if attempt == 2:
                        log(f"nova_ops write failed after 3 attempts: {e}", level=LOG_WARN, source="nova_ops_writer")
                    else:
                        await asyncio.sleep(2 ** attempt)
            _QUEUE.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log(f"nova_ops worker error: {e}", level=LOG_ERROR, source="nova_ops_writer")


def _ensure_worker():
    global _QUEUE, _WORKER_TASK
    if _QUEUE is None:
        _QUEUE = asyncio.Queue(maxsize=500)
    if _WORKER_TASK is None or _WORKER_TASK.done():
        _WORKER_TASK = asyncio.create_task(_worker())


def _enqueue(op, *args):
    """Enqueue a write operation. Drops silently if no running loop or queue is full."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Called from sync context (e.g. tests) — no loop running, skip silently
        return
    _ensure_worker()
    try:
        _QUEUE.put_nowait((op, args))
    except asyncio.QueueFull:
        log("nova_ops queue full — dropping write", level=LOG_WARN, source="nova_ops_writer")


# ── Public API ────────────────────────────────────────────────────────────────

def record_run_start(
    run_id: str,
    task_id: str,
    task_script: str,
    task_group: str,
    scheduled_at_ms: int,
    started_at_ms: int,
    consecutive_failures: int,
    run_count: int,
    was_retry: bool,
):
    async def _write(conn, *a):
        await conn.execute(
            """
            INSERT INTO scheduler_runs
                (run_id, task_id, task_script, task_group, scheduled_at, started_at,
                 status, consecutive_failures_at_start, run_count_at_start, was_retry)
            VALUES ($1,$2,$3,$4,$5,$6,'running',$7,$8,$9)
            ON CONFLICT (run_id) DO NOTHING
            """,
            *a,
        )
    _enqueue(_write, run_id, task_id, task_script, task_group,
             scheduled_at_ms, started_at_ms,
             consecutive_failures, run_count, was_retry)


def record_run_end(
    run_id: str,
    ended_at_ms: int,
    duration_ms: int,
    exit_code: int,
    status: str,          # success | failure | timeout
    error_tail: str,
    stdout_tail: str,
    retry_recovered: bool,
):
    async def _write(conn, *a):
        await conn.execute(
            """
            UPDATE scheduler_runs
            SET ended_at        = $2,
                duration_ms     = $3,
                exit_code       = $4,
                status          = $5,
                error_tail      = $6,
                stdout_tail     = $7,
                retry_recovered = $8
            WHERE run_id = $1
            """,
            *a,
        )
    _enqueue(_write, run_id, ended_at_ms, duration_ms,
             exit_code, status, error_tail, stdout_tail, retry_recovered)


async def close():
    """Drain queue and close pool. Call during scheduler shutdown."""
    global _POOL, _WORKER_TASK, _QUEUE
    if _QUEUE is not None:
        try:
            await asyncio.wait_for(_QUEUE.join(), timeout=10)
        except asyncio.TimeoutError:
            log("nova_ops queue drain timed out — some writes may be lost", level=LOG_WARN, source="nova_ops_writer")
    if _WORKER_TASK and not _WORKER_TASK.done():
        _WORKER_TASK.cancel()
        try:
            await _WORKER_TASK
        except asyncio.CancelledError:
            pass
    if _POOL is not None:
        await _POOL.close()
        _POOL = None
