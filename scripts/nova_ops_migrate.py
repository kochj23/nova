#!/usr/bin/env python3
"""
nova_ops_migrate.py — Schema migrations for the nova_ops operational database.

Run manually or via scheduler to apply pending migrations.
Each migration is idempotent — safe to run multiple times.

Usage:
  python3 nova_ops_migrate.py           # apply all pending
  python3 nova_ops_migrate.py --check   # show pending migrations without applying
  python3 nova_ops_migrate.py --list    # show all migrations and status

Written by Jordan Koch.
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN

DB_DSN = "postgresql://kochj@localhost:5432/nova_ops"

# ── Migration registry ────────────────────────────────────────────────────────
# Each migration: (id, description, up_sql)
# IDs are sequential integers. Never reorder or delete applied migrations.

MIGRATIONS = [
    (
        1,
        "Create migrations tracking table",
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id   INTEGER PRIMARY KEY,
            description    TEXT NOT NULL,
            applied_at     BIGINT NOT NULL
        );
        """,
    ),
    (
        2,
        "Create scheduler_runs table",
        """
        CREATE TABLE IF NOT EXISTS scheduler_runs (
            run_id          TEXT PRIMARY KEY,       -- uuid4
            task_id         TEXT NOT NULL,
            task_script     TEXT NOT NULL,
            task_group      TEXT NOT NULL DEFAULT '',
            scheduled_at    BIGINT NOT NULL,        -- unix epoch ms when task was due
            started_at      BIGINT NOT NULL,        -- unix epoch ms
            ended_at        BIGINT,                 -- null if still running
            duration_ms     INTEGER,                -- wall-clock ms (null if still running)
            exit_code       INTEGER,                -- null if still running
            status          TEXT NOT NULL DEFAULT 'running',  -- running|success|failure|timeout
            error_tail      TEXT,                   -- last 500 chars of stderr on failure
            stdout_tail     TEXT,                   -- last 200 chars of stdout
            consecutive_failures_at_start INTEGER NOT NULL DEFAULT 0,
            run_count_at_start            INTEGER NOT NULL DEFAULT 0,
            was_retry       BOOLEAN NOT NULL DEFAULT FALSE,
            retry_recovered BOOLEAN NOT NULL DEFAULT FALSE
        );
        CREATE INDEX IF NOT EXISTS idx_sched_runs_task_id     ON scheduler_runs (task_id);
        CREATE INDEX IF NOT EXISTS idx_sched_runs_started_at  ON scheduler_runs (started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_sched_runs_status      ON scheduler_runs (status);
        CREATE INDEX IF NOT EXISTS idx_sched_runs_task_status ON scheduler_runs (task_id, status, started_at DESC);
        """,
    ),
    (
        3,
        "Create scheduler_task_stats view",
        """
        CREATE OR REPLACE VIEW scheduler_task_stats AS
        SELECT
            task_id,
            task_script,
            task_group,
            COUNT(*)                                              AS total_runs,
            COUNT(*) FILTER (WHERE status = 'success')           AS success_count,
            COUNT(*) FILTER (WHERE status = 'failure')           AS failure_count,
            COUNT(*) FILTER (WHERE status = 'timeout')           AS timeout_count,
            COUNT(*) FILTER (WHERE status = 'running')           AS currently_running,
            ROUND(AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL))::INT
                                                                 AS avg_duration_ms,
            MAX(duration_ms) FILTER (WHERE duration_ms IS NOT NULL)
                                                                 AS max_duration_ms,
            MAX(started_at)                                      AS last_run_ms,
            MAX(started_at) FILTER (WHERE status = 'success')    AS last_success_ms,
            MAX(started_at) FILTER (WHERE status = 'failure')    AS last_failure_ms,
            ROUND(
                100.0 * COUNT(*) FILTER (WHERE status = 'success') /
                NULLIF(COUNT(*) FILTER (WHERE status IN ('success','failure','timeout')), 0),
                1
            )                                                    AS success_rate_pct
        FROM scheduler_runs
        GROUP BY task_id, task_script, task_group;
        """,
    ),
    (
        4,
        "Create scheduler_daily_summary view",
        """
        CREATE OR REPLACE VIEW scheduler_daily_summary AS
        SELECT
            DATE(TO_TIMESTAMP(started_at / 1000.0) AT TIME ZONE 'America/Los_Angeles') AS run_date,
            COUNT(*)                                                AS total_runs,
            COUNT(*) FILTER (WHERE status = 'success')             AS successes,
            COUNT(*) FILTER (WHERE status = 'failure')             AS failures,
            COUNT(*) FILTER (WHERE status = 'timeout')             AS timeouts,
            COUNT(DISTINCT task_id)                                AS unique_tasks,
            ROUND(AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL) / 1000.0, 1)
                                                                   AS avg_duration_s,
            SUM(duration_ms) FILTER (WHERE duration_ms IS NOT NULL) / 1000
                                                                   AS total_cpu_s
        FROM scheduler_runs
        GROUP BY run_date
        ORDER BY run_date DESC;
        """,
    ),
    (
        5,
        "Add stdout_tail column if missing (safe re-run guard)",
        """
        ALTER TABLE scheduler_runs
            ADD COLUMN IF NOT EXISTS stdout_tail TEXT;
        """,
    ),
]


# ── Migration runner ──────────────────────────────────────────────────────────

async def get_applied(conn) -> set[int]:
    try:
        rows = await conn.fetch("SELECT migration_id FROM schema_migrations")
        return {r["migration_id"] for r in rows}
    except Exception:
        # Table doesn't exist yet — migration 1 will create it
        return set()


async def apply_migration(conn, mid: int, desc: str, sql: str):
    async with conn.transaction():
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            await conn.execute(stmt)
        if mid > 1:  # migration 1 creates the table itself — record after
            await conn.execute(
                "INSERT INTO schema_migrations (migration_id, description, applied_at) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                mid, desc, int(time.time() * 1000),
            )
        else:
            # After creating the table, record migration 1
            await conn.execute(
                "INSERT INTO schema_migrations (migration_id, description, applied_at) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                mid, desc, int(time.time() * 1000),
            )
    log(f"  Applied migration {mid}: {desc}", level=LOG_INFO, source="nova_ops_migrate")


async def run_migrations(check_only: bool = False, list_all: bool = False):
    try:
        import asyncpg
    except ImportError:
        log("asyncpg not installed: pip3 install asyncpg", level=LOG_ERROR, source="nova_ops_migrate")
        sys.exit(1)

    conn = await asyncpg.connect(DB_DSN)
    try:
        applied = await get_applied(conn)

        if list_all:
            print(f"{'ID':>4}  {'Status':<12}  Description")
            print("-" * 60)
            for mid, desc, _ in MIGRATIONS:
                status = "APPLIED" if mid in applied else "PENDING"
                print(f"{mid:>4}  {status:<12}  {desc}")
            return

        pending = [(mid, desc, sql) for mid, desc, sql in MIGRATIONS if mid not in applied]

        if not pending:
            log("nova_ops schema is up to date — no pending migrations", level=LOG_INFO, source="nova_ops_migrate")
            print("✓  All migrations applied. nova_ops schema is current.")
            return

        if check_only:
            print(f"Pending migrations ({len(pending)}):")
            for mid, desc, _ in pending:
                print(f"  [{mid}] {desc}")
            return

        log(f"Applying {len(pending)} migration(s)...", level=LOG_INFO, source="nova_ops_migrate")
        for mid, desc, sql in pending:
            await apply_migration(conn, mid, desc, sql)

        print(f"✓  Applied {len(pending)} migration(s). nova_ops schema is current.")

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Apply nova_ops schema migrations")
    parser.add_argument("--check", action="store_true", help="Show pending without applying")
    parser.add_argument("--list",  action="store_true", help="List all migrations and status")
    args = parser.parse_args()
    asyncio.run(run_migrations(check_only=args.check, list_all=args.list))


if __name__ == "__main__":
    main()
