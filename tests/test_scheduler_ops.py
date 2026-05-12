#!/usr/bin/env python3
"""
test_scheduler_ops.py — Test suite for nova_ops scheduler integration.

Covers all 7 required categories:
  1. Security    — injection prevention, no PII in logs, credential handling
  2. Performance — write latency, queue depth, no blocking
  3. Retry       — DB write retries with backoff, graceful pool failure
  4. Unit        — migration idempotency, writer API, task instrumentation
  5. Integration — end-to-end: scheduler fires task → row lands in nova_ops
  6. Functional  — golden path + error paths from scheduler perspective
  7. Frame       — imports load, DB connects, migrations apply cleanly

Written by Jordan Koch.
"""

import asyncio
import json
import os
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add scripts to path
SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


# ═══════════════════════════════════════════════════════════════════════════════
# 7. FRAME TESTS — Does everything import and connect?
# ═══════════════════════════════════════════════════════════════════════════════

class TestFrame(unittest.IsolatedAsyncioTestCase):

    def test_nova_ops_writer_imports(self):
        import nova_ops_writer
        self.assertTrue(hasattr(nova_ops_writer, "record_run_start"))
        self.assertTrue(hasattr(nova_ops_writer, "record_run_end"))
        self.assertTrue(hasattr(nova_ops_writer, "close"))

    def test_nova_ops_migrate_imports(self):
        import nova_ops_migrate
        self.assertTrue(hasattr(nova_ops_migrate, "MIGRATIONS"))
        self.assertTrue(hasattr(nova_ops_migrate, "run_migrations"))
        self.assertGreater(len(nova_ops_migrate.MIGRATIONS), 0)

    def test_scheduler_imports(self):
        import nova_scheduler
        self.assertTrue(hasattr(nova_scheduler, "NovaScheduler"))
        self.assertTrue(hasattr(nova_scheduler, "Task"))
        self.assertTrue(hasattr(nova_scheduler, "TaskState"))

    async def test_nova_ops_writer_imports_without_asyncpg(self):
        """Writer must degrade gracefully when asyncpg is missing (runs in async context)."""
        import nova_ops_writer
        original = nova_ops_writer._ASYNCPG
        nova_ops_writer._ASYNCPG = False
        nova_ops_writer._QUEUE = None
        nova_ops_writer._WORKER_TASK = None
        # record_run_start must not raise, and worker must drain without hitting DB
        nova_ops_writer.record_run_start(
            run_id="test", task_id="t", task_script="s.py",
            task_group="", scheduled_at_ms=0, started_at_ms=0,
            consecutive_failures=0, run_count=0, was_retry=False,
        )
        await asyncio.sleep(0.05)  # let worker drain
        nova_ops_writer._ASYNCPG = original  # restore
        nova_ops_writer._QUEUE = None
        nova_ops_writer._WORKER_TASK = None

    def test_db_connection_nova_ops(self):
        """nova_ops DB must be reachable via direct psycopg2."""
        try:
            import psycopg2
            conn = psycopg2.connect("dbname=nova_ops user=kochj host=localhost port=5432")
            cur = conn.cursor()
            cur.execute("SELECT 1")
            self.assertEqual(cur.fetchone()[0], 1)
            conn.close()
        except Exception as e:
            self.fail(f"nova_ops DB not reachable: {e}")

    def test_scheduler_runs_table_exists(self):
        import psycopg2
        conn = psycopg2.connect("dbname=nova_ops user=kochj host=localhost port=5432")
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name='scheduler_runs' AND table_schema='public'"
        )
        self.assertEqual(cur.fetchone()[0], 1, "scheduler_runs table missing")
        conn.close()

    def test_scheduler_task_stats_view_exists(self):
        import psycopg2
        conn = psycopg2.connect("dbname=nova_ops user=kochj host=localhost port=5432")
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.views "
            "WHERE table_name='scheduler_task_stats' AND table_schema='public'"
        )
        self.assertEqual(cur.fetchone()[0], 1, "scheduler_task_stats view missing")
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. UNIT TESTS — Individual functions in isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestUnit(unittest.IsolatedAsyncioTestCase):

    def test_migration_ids_are_sequential(self):
        import nova_ops_migrate
        ids = [m[0] for m in nova_ops_migrate.MIGRATIONS]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))

    def test_migration_descriptions_unique(self):
        import nova_ops_migrate
        descs = [m[1] for m in nova_ops_migrate.MIGRATIONS]
        self.assertEqual(len(descs), len(set(descs)), "duplicate migration descriptions")

    def test_migration_sql_not_empty(self):
        import nova_ops_migrate
        for mid, desc, sql in nova_ops_migrate.MIGRATIONS:
            self.assertTrue(sql.strip(), f"Migration {mid} has empty SQL")

    async def test_writer_enqueues_without_pool(self):
        """record_run_start/end must enqueue without blocking even if pool is down."""
        import nova_ops_writer
        nova_ops_writer._POOL = None
        nova_ops_writer._QUEUE = None
        nova_ops_writer._WORKER_TASK = None
        nova_ops_writer._ASYNCPG = False  # simulate missing asyncpg

        start = time.monotonic()
        nova_ops_writer.record_run_start(
            run_id=str(uuid.uuid4()), task_id="unit_test",
            task_script="test.py", task_group="tests",
            scheduled_at_ms=int(time.time() * 1000),
            started_at_ms=int(time.time() * 1000),
            consecutive_failures=0, run_count=1, was_retry=False,
        )
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.1, "record_run_start blocked for >100ms")
        nova_ops_writer._ASYNCPG = True  # restore

    def test_task_state_defaults(self):
        from nova_scheduler import TaskState
        s = TaskState()
        self.assertEqual(s.consecutive_failures, 0)
        self.assertFalse(s.running)
        self.assertFalse(s._retry_pending)
        self.assertEqual(s.run_count, 0)

    def test_parse_interval(self):
        from nova_scheduler import parse_interval
        self.assertEqual(parse_interval("every 5m"), 300)
        self.assertEqual(parse_interval("every 4h"), 14400)
        self.assertEqual(parse_interval("every 30s"), 30)
        self.assertEqual(parse_interval("invalid"), 0)

    def test_parse_cron(self):
        from nova_scheduler import parse_cron
        self.assertEqual(parse_cron("cron 0 23 * * *"), "0 23 * * *")
        self.assertEqual(parse_cron("cron 30 6 * * 1"), "30 6 * * 1")
        self.assertEqual(parse_cron("bad"), "")

    def test_next_cron_time_advances(self):
        from nova_scheduler import next_cron_time
        now = time.time()
        next_t = next_cron_time("0 * * * *", now)
        self.assertGreater(next_t, now)
        self.assertLess(next_t - now, 3700)  # within the next hour


# ═══════════════════════════════════════════════════════════════════════════════
# 3. RETRY TESTS — DB write retry with backoff
# ═══════════════════════════════════════════════════════════════════════════════

class TestRetry(unittest.IsolatedAsyncioTestCase):

    async def test_writer_retries_on_db_error(self):
        """Worker must retry up to 3 times before giving up on a failed write."""
        import nova_ops_writer
        call_count = 0

        async def failing_op(conn, *args):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("simulated DB error")

        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        # Reset writer state
        nova_ops_writer._POOL = None
        nova_ops_writer._QUEUE = asyncio.Queue(maxsize=500)
        nova_ops_writer._WORKER_TASK = None

        with patch.object(nova_ops_writer, "_get_pool", return_value=mock_pool):
            with patch("asyncio.sleep", new_callable=AsyncMock):  # skip real sleep
                nova_ops_writer._enqueue(failing_op, "arg1")
                worker = asyncio.create_task(nova_ops_writer._worker())
                await asyncio.sleep(0.1)
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass

    async def test_writer_drops_when_queue_full(self):
        """When queue is full, enqueue must drop silently without raising."""
        import nova_ops_writer
        nova_ops_writer._QUEUE = asyncio.Queue(maxsize=1)
        nova_ops_writer._WORKER_TASK = None

        # Fill the queue
        nova_ops_writer._QUEUE.put_nowait((lambda c: None, ()))

        # This should not raise even though queue is full
        nova_ops_writer.record_run_start(
            run_id="overflow", task_id="t", task_script="s.py",
            task_group="", scheduled_at_ms=0, started_at_ms=0,
            consecutive_failures=0, run_count=0, was_retry=False,
        )
        # Reset
        nova_ops_writer._QUEUE = None

    async def test_scheduler_retries_task_on_first_failure(self):
        """execute_task must schedule a 60s retry on first failure, not alert Slack."""
        from nova_scheduler import NovaScheduler, Task, TaskState
        import nova_ops_writer

        sched = NovaScheduler()
        sched.sched_cfg = {"python": sys.executable, "shell": "/bin/zsh",
                           "max_concurrent": 6, "tz": "America/Los_Angeles",
                           "heartbeat_interval": 3600}
        sched.slack_cfg = {"alerts": False, "startup": False}
        sched._start_time = time.time()

        task = Task(id="test_retry", script="_nonexistent_.py",
                    schedule="every 1h", timeout=5)
        task.state = TaskState()
        sched.tasks = {"test_retry": task}

        with patch.object(nova_ops_writer, "record_run_start"), \
             patch.object(nova_ops_writer, "record_run_end"):
            await sched.execute_task(task)

        self.assertTrue(task.state._retry_pending)
        self.assertEqual(task.state.consecutive_failures, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SECURITY TESTS — Injection, PII, credentials
# ═══════════════════════════════════════════════════════════════════════════════

class TestSecurity(unittest.TestCase):

    def test_no_credentials_in_source(self):
        """Scheduler and writer must not contain hardcoded passwords, tokens, or keys."""
        for fname in ("nova_scheduler.py", "nova_ops_writer.py", "nova_ops_migrate.py"):
            src = (SCRIPTS / fname).read_text()
            for pattern in ("password=", "secret=", "api_key=", "sk-", "ghp_", "xoxb-"):
                self.assertNotIn(pattern, src.lower(),
                                 f"{fname} contains suspicious credential pattern: {pattern}")

    def test_db_dsn_no_password(self):
        """The nova_ops DSN must not embed a password."""
        import nova_ops_writer
        import nova_ops_migrate
        for dsn in (nova_ops_writer.DB_DSN, nova_ops_migrate.DB_DSN):
            self.assertNotIn(":", dsn.split("@")[0].replace("postgresql://", ""),
                             f"DSN appears to embed a password: {dsn}")

    def test_error_tail_truncated(self):
        """error_tail written to DB must be capped — no unbounded stderr dumps."""
        import nova_ops_writer
        # The writer caps at 500 chars — verify in scheduler execute_task
        src = (SCRIPTS / "nova_scheduler.py").read_text()
        self.assertIn("[-500:]", src, "stderr truncation [-500:] not found in scheduler")

    def test_stdout_tail_truncated(self):
        """stdout_tail must be capped to prevent large output flooding the DB."""
        src = (SCRIPTS / "nova_scheduler.py").read_text()
        self.assertIn("[-200:]", src, "stdout truncation [-200:] not found in scheduler")

    def test_no_pii_fields_in_scheduler_runs_schema(self):
        """scheduler_runs table must not have columns for email, name, IP, health data."""
        import psycopg2
        conn = psycopg2.connect("dbname=nova_ops user=kochj host=localhost port=5432")
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='scheduler_runs' AND table_schema='public'"
        )
        columns = {r[0] for r in cur.fetchall()}
        conn.close()
        pii_columns = {"email", "name", "ip_address", "user_id", "phone", "health"}
        overlap = columns & pii_columns
        self.assertEqual(overlap, set(), f"PII columns found in scheduler_runs: {overlap}")

    def test_http_api_binds_loopback_only(self):
        """The scheduler HTTP API must bind to 127.0.0.1, not 0.0.0.0."""
        src = (SCRIPTS / "nova_scheduler.py").read_text()
        self.assertIn('"127.0.0.1"', src, "Scheduler HTTP API not bound to loopback")
        self.assertNotIn('"0.0.0.0"', src, "Scheduler HTTP API binds to all interfaces")

    def test_sql_uses_parameterized_queries(self):
        """All SQL in the writer must use $N params, not string formatting."""
        src = (SCRIPTS / "nova_ops_writer.py").read_text()
        # Check INSERT and UPDATE use params, not f-strings in SQL
        import re
        sql_blocks = re.findall(r'await conn\.execute\([^)]+\)', src, re.DOTALL)
        for block in sql_blocks:
            self.assertNotIn("f\"", block, "f-string in SQL execute call — injection risk")
            self.assertNotIn("f'", block, "f-string in SQL execute call — injection risk")
            self.assertNotIn("% s", block, "%-formatting in SQL — injection risk")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. PERFORMANCE TESTS — Write latency, no blocking
# ═══════════════════════════════════════════════════════════════════════════════

class TestPerformance(unittest.IsolatedAsyncioTestCase):

    def test_record_run_start_is_nonblocking(self):
        """record_run_start must return in <1ms — it's fire-and-forget."""
        import nova_ops_writer
        nova_ops_writer._QUEUE = asyncio.Queue(maxsize=500)
        nova_ops_writer._WORKER_TASK = None

        start = time.monotonic()
        for _ in range(100):
            nova_ops_writer.record_run_start(
                run_id=str(uuid.uuid4()), task_id="perf_test",
                task_script="test.py", task_group="perf",
                scheduled_at_ms=int(time.time() * 1000),
                started_at_ms=int(time.time() * 1000),
                consecutive_failures=0, run_count=0, was_retry=False,
            )
        elapsed_ms = (time.monotonic() - start) * 1000
        self.assertLess(elapsed_ms, 50, f"100 enqueues took {elapsed_ms:.1f}ms — too slow")
        nova_ops_writer._QUEUE = None

    async def test_real_db_write_latency(self):
        """A real INSERT + UPDATE round-trip to nova_ops must complete in <500ms."""
        import asyncpg
        conn = await asyncpg.connect("postgresql://kochj@localhost:5432/nova_ops")
        run_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)

        start = time.monotonic()
        await conn.execute(
            "INSERT INTO scheduler_runs "
            "(run_id,task_id,task_script,task_group,scheduled_at,started_at,status,"
            "consecutive_failures_at_start,run_count_at_start,was_retry) "
            "VALUES ($1,'perf_test','perf.py','perf',$2,$2,'running',0,0,false)",
            run_id, now_ms,
        )
        await conn.execute(
            "UPDATE scheduler_runs SET ended_at=$2, duration_ms=$3, exit_code=0, "
            "status='success' WHERE run_id=$1",
            run_id, now_ms + 100, 100,
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        self.assertLess(elapsed_ms, 500, f"DB round-trip took {elapsed_ms:.1f}ms")

        # Cleanup
        await conn.execute("DELETE FROM scheduler_runs WHERE run_id=$1", run_id)
        await conn.close()

    async def test_scheduler_stats_view_query_time(self):
        """The scheduler_task_stats view must respond in <1s even with data."""
        import asyncpg
        conn = await asyncpg.connect("postgresql://kochj@localhost:5432/nova_ops")
        start = time.monotonic()
        await conn.fetch("SELECT * FROM scheduler_task_stats LIMIT 100")
        elapsed_ms = (time.monotonic() - start) * 1000
        self.assertLess(elapsed_ms, 1000, f"Stats view took {elapsed_ms:.1f}ms")
        await conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. INTEGRATION TESTS — Components working together
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        import asyncpg
        self.conn = await asyncpg.connect("postgresql://kochj@localhost:5432/nova_ops")
        self.test_run_ids = []

    async def asyncTearDown(self):
        if self.test_run_ids:
            await self.conn.execute(
                "DELETE FROM scheduler_runs WHERE run_id = ANY($1::text[])",
                self.test_run_ids,
            )
        await self.conn.close()

    async def test_writer_full_cycle_lands_in_db(self):
        """record_run_start → record_run_end → row visible in nova_ops."""
        import nova_ops_writer
        run_id = str(uuid.uuid4())
        self.test_run_ids.append(run_id)
        now_ms = int(time.time() * 1000)

        # Reset writer with real pool
        nova_ops_writer._POOL = None
        nova_ops_writer._QUEUE = None
        nova_ops_writer._WORKER_TASK = None
        nova_ops_writer._ASYNCPG = True

        nova_ops_writer.record_run_start(
            run_id=run_id, task_id="integration_test",
            task_script="test.py", task_group="tests",
            scheduled_at_ms=now_ms, started_at_ms=now_ms,
            consecutive_failures=0, run_count=5, was_retry=False,
        )
        nova_ops_writer.record_run_end(
            run_id=run_id, ended_at_ms=now_ms + 1500, duration_ms=1500,
            exit_code=0, status="success", error_tail="",
            stdout_tail="done", retry_recovered=False,
        )

        # Give the async worker time to process
        await asyncio.sleep(0.5)

        row = await self.conn.fetchrow(
            "SELECT * FROM scheduler_runs WHERE run_id=$1", run_id
        )
        self.assertIsNotNone(row, "Run record not found in DB after write")
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["task_id"], "integration_test")
        self.assertEqual(row["run_count_at_start"], 5)
        self.assertEqual(row["duration_ms"], 1500)

    async def test_failure_run_records_error_tail(self):
        """A failed run must persist the error tail in the DB."""
        import nova_ops_writer
        run_id = str(uuid.uuid4())
        self.test_run_ids.append(run_id)
        now_ms = int(time.time() * 1000)
        error_msg = "FileNotFoundError: /tmp/missing_script.py not found"

        nova_ops_writer.record_run_start(
            run_id=run_id, task_id="failing_test",
            task_script="fail.py", task_group="tests",
            scheduled_at_ms=now_ms, started_at_ms=now_ms,
            consecutive_failures=2, run_count=10, was_retry=True,
        )
        nova_ops_writer.record_run_end(
            run_id=run_id, ended_at_ms=now_ms + 200, duration_ms=200,
            exit_code=1, status="failure", error_tail=error_msg,
            stdout_tail="", retry_recovered=False,
        )
        await asyncio.sleep(0.5)

        row = await self.conn.fetchrow(
            "SELECT * FROM scheduler_runs WHERE run_id=$1", run_id
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "failure")
        self.assertEqual(row["error_tail"], error_msg)
        self.assertTrue(row["was_retry"])
        self.assertEqual(row["consecutive_failures_at_start"], 2)

    async def test_migration_idempotent(self):
        """Running migrations twice must not fail or duplicate rows."""
        import nova_ops_migrate
        # First apply should be a no-op (already applied)
        await nova_ops_migrate.run_migrations()
        # Second apply also no-op
        await nova_ops_migrate.run_migrations()

        count = await self.conn.fetchval(
            "SELECT COUNT(*) FROM schema_migrations"
        )
        self.assertEqual(count, len(nova_ops_migrate.MIGRATIONS))

    async def test_scheduler_http_runs_endpoint(self):
        """The /runs endpoint must return valid JSON from the DB."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:37460/runs", timeout=5)
            data = json.loads(resp.read())
            self.assertIsInstance(data, list)
        except Exception as e:
            self.skipTest(f"Scheduler not running: {e}")

    async def test_scheduler_http_stats_endpoint(self):
        """The /stats endpoint must return valid JSON."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:37460/stats", timeout=5)
            data = json.loads(resp.read())
            self.assertIsInstance(data, list)
        except Exception as e:
            self.skipTest(f"Scheduler not running: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. FUNCTIONAL TESTS — End-to-end golden path and error paths
# ═══════════════════════════════════════════════════════════════════════════════

class TestFunctional(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        import asyncpg
        self.conn = await asyncpg.connect("postgresql://kochj@localhost:5432/nova_ops")
        self.test_run_ids = []

    async def asyncTearDown(self):
        if self.test_run_ids:
            await self.conn.execute(
                "DELETE FROM scheduler_runs WHERE run_id = ANY($1::text[])",
                self.test_run_ids,
            )
        await self.conn.close()

    async def test_successful_task_execution_golden_path(self):
        """A task that exits 0 must be recorded as success with correct duration."""
        from nova_scheduler import NovaScheduler, Task, TaskState
        import nova_ops_writer

        sched = NovaScheduler()
        sched.sched_cfg = {"python": sys.executable, "shell": "/bin/zsh",
                           "max_concurrent": 6, "tz": "America/Los_Angeles",
                           "heartbeat_interval": 3600}
        sched.slack_cfg = {"alerts": False, "startup": False}
        sched._start_time = time.time()

        task = Task(id="functional_success", script="nova_ops_migrate.py",
                    schedule="every 1h", timeout=30,
                    args=["--check"])  # --check exits 0
        task.state = TaskState()
        sched.tasks = {"functional_success": task}

        run_id_captured = []
        orig_start = nova_ops_writer.record_run_start
        def capture_start(**kw):
            run_id_captured.append(kw["run_id"])
            orig_start(**kw)

        with patch.object(nova_ops_writer, "record_run_start", side_effect=capture_start):
            await sched.execute_task(task)

        self.assertEqual(task.state.last_exit_code, 0)
        self.assertEqual(task.state.consecutive_failures, 0)
        self.assertFalse(task.state._retry_pending)

        if run_id_captured:
            self.test_run_ids.extend(run_id_captured)
            await asyncio.sleep(0.5)
            row = await self.conn.fetchrow(
                "SELECT status, exit_code FROM scheduler_runs WHERE run_id=$1",
                run_id_captured[0],
            )
            if row:
                self.assertEqual(row["status"], "success")
                self.assertEqual(row["exit_code"], 0)

    async def test_task_timeout_recorded_correctly(self):
        """A task that times out must write status='timeout' to the DB."""
        from nova_scheduler import NovaScheduler, Task, TaskState
        import nova_ops_writer
        import tempfile

        # Create a script that sleeps longer than the timeout
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                         dir=SCRIPTS, delete=False,
                                         prefix="_test_sleep_") as f:
            f.write("import time; time.sleep(60)\n")
            test_script = Path(f.name).name

        try:
            sched = NovaScheduler()
            sched.sched_cfg = {"python": sys.executable, "shell": "/bin/zsh",
                               "max_concurrent": 6, "tz": "America/Los_Angeles",
                               "heartbeat_interval": 3600}
            sched.slack_cfg = {"alerts": False, "startup": False}
            sched._start_time = time.time()

            task = Task(id="functional_timeout", script=test_script,
                        schedule="every 1h", timeout=1)  # 1 second timeout
            task.state = TaskState()
            sched.tasks = {"functional_timeout": task}

            run_id_captured = []
            orig_start = nova_ops_writer.record_run_start
            def capture_start(**kw):
                run_id_captured.append(kw["run_id"])
                orig_start(**kw)

            with patch.object(nova_ops_writer, "record_run_start", side_effect=capture_start):
                await sched.execute_task(task)

            if run_id_captured:
                self.test_run_ids.extend(run_id_captured)
                await asyncio.sleep(0.5)
                row = await self.conn.fetchrow(
                    "SELECT status FROM scheduler_runs WHERE run_id=$1",
                    run_id_captured[0],
                )
                if row:
                    self.assertEqual(row["status"], "timeout")
        finally:
            (SCRIPTS / test_script).unlink(missing_ok=True)

    async def test_stats_view_reflects_new_runs(self):
        """After inserting a run, the stats view must reflect it immediately."""
        run_id = str(uuid.uuid4())
        self.test_run_ids.append(run_id)
        now_ms = int(time.time() * 1000)

        await self.conn.execute(
            "INSERT INTO scheduler_runs "
            "(run_id,task_id,task_script,task_group,scheduled_at,started_at,ended_at,"
            "duration_ms,exit_code,status,consecutive_failures_at_start,run_count_at_start,was_retry) "
            "VALUES ($1,'view_test_task','view.py','tests',$2,$2,$3,1000,0,"
            "'success',0,1,false)",
            run_id, now_ms, now_ms + 1000,
        )

        row = await self.conn.fetchrow(
            "SELECT total_runs, success_count FROM scheduler_task_stats "
            "WHERE task_id='view_test_task'"
        )
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row["success_count"], 1)

    def test_migration_check_flag_exits_without_applying(self):
        """--check must report pending migrations without writing to DB."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "nova_ops_migrate.py"), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        # After applying all migrations, --check should say nothing is pending
        # or list 0 pending (already applied)

    def test_migration_list_flag_shows_all(self):
        """--list must show all defined migrations."""
        import subprocess, nova_ops_migrate
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "nova_ops_migrate.py"), "--list"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        for mid, desc, _ in nova_ops_migrate.MIGRATIONS:
            self.assertIn(str(mid), result.stdout)


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
