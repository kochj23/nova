#!/usr/bin/env python3
"""Tests for nova_ops_migrate.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import asyncpg

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ops_migrate.py"
SRC = SCRIPT.read_text()
QUIET_ENV = {**os.environ, "NOVA_TEST_QUIET": "1", "NOVA_LOG_LEVEL": "fatal"}   # nova_logger drops everything below fatal


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load("ops_migrate_under_test", SCRIPT)
M.log = MagicMock()                     # nova_logger.log would append to ~/.openclaw/logs/nova.jsonl — keep it in memory


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.tx_opened += 1

    async def __aexit__(self, *a):
        self.conn.tx_closed += 1
        return False


class _Conn:
    """asyncpg connection stand-in: `applied` answers the schema_migrations read; records every execute."""
    def __init__(self, applied=None, fetch_exc=None, fail_on=None):
        self.applied, self.fetch_exc, self.fail_on = applied, fetch_exc, fail_on
        self.executed, self.closed, self.tx_opened, self.tx_closed = [], False, 0, 0

    async def fetch(self, sql, *args):
        if self.fetch_exc:
            raise self.fetch_exc
        return [{"migration_id": m} for m in (self.applied or ())]

    async def execute(self, sql, *args):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"stub failure on {self.fail_on}")
        self.executed.append((" ".join(sql.split()), args))

    def transaction(self):
        return _Tx(self)

    async def close(self):
        self.closed = True

    def inserts(self):
        return [a for s, a in self.executed if s.startswith("INSERT INTO schema_migrations")]


def _run(conn, argv=("nova_ops_migrate.py",), connect_exc=None):
    """Drive main() with asyncpg.connect replaced; returns (stdout, conn, connect attempts)."""
    attempts = []

    async def connect(dsn):
        attempts.append(dsn)
        if connect_exc:
            raise connect_exc
        return conn
    with patch.object(asyncpg, "connect", connect), patch.object(sys, "argv", list(argv)), redirect_stdout(io.StringIO()) as out:
        M.main()
    return out.getvalue(), conn, attempts


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", M.DB_DSN)
        self.assertTrue(M.DB_DSN.startswith("postgresql://kochj@"))

    def test_only_sql_executed_comes_from_the_registry_and_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        _, conn, _ = _run(_Conn(applied=set()))
        registry = " ".join(" ".join(sql.split()) for _, _, sql in M.MIGRATIONS)
        for stmt, args in conn.executed:
            if stmt.startswith("INSERT INTO schema_migrations"):
                self.assertIn("VALUES ($1, $2, $3)", stmt)          # bind params, never interpolated
                self.assertEqual(len(args), 3)
            else:
                self.assertIn(stmt, registry)                        # nothing outside MIGRATIONS ever runs
                self.assertEqual(args, ())

    def test_no_destructive_statements_in_the_registry(self):
        for mid, desc, sql in M.MIGRATIONS:
            self.assertIsNone(re.search(r"\b(DROP|TRUNCATE|DELETE)\b", sql, re.I), f"migration {mid}")


class TestPerformance(unittest.TestCase):
    def test_statement_splitting_fast_on_10k_statements(self):
        conn = _Conn()
        sql = "SELECT 1;\n" * 10_000
        t0 = time.perf_counter()
        asyncio.run(M.apply_migration(conn, 99, "perf", sql))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(conn.executed), 10_001)              # 10k statements + the tracking row


class TestRetry(unittest.TestCase):
    def test_get_applied_fails_open_to_empty_set(self):
        # RETRY GAP: get_applied — one read; a missing schema_migrations table means "nothing applied yet"
        conn = _Conn(fetch_exc=RuntimeError('relation "schema_migrations" does not exist'))
        self.assertEqual(asyncio.run(M.get_applied(conn)), set())

    def test_connect_failure_is_one_attempt_and_escapes(self):
        # RETRY GAP: run_migrations/asyncpg.connect — no retry; the error surfaces to the scheduler as a non-zero exit
        attempts = []

        async def connect(dsn):
            attempts.append(dsn); raise OSError("down")
        with patch.object(asyncpg, "connect", connect), patch.object(sys, "argv", ["x"]):
            with self.assertRaises(OSError):
                M.main()
        self.assertEqual(len(attempts), 1)

    def test_failed_migration_stops_the_run_and_still_closes_the_connection(self):
        # RETRY GAP: apply_migration — a failing statement is not retried; the transaction unwinds and later migrations wait
        conn = _Conn(applied=set(), fail_on="scheduler_task_stats")
        with self.assertRaises(RuntimeError):
            _run(conn)
        self.assertTrue(conn.closed)
        self.assertEqual([a[0] for a in conn.inserts()], [1, 2])       # 3 never recorded, 4/5 never attempted
        self.assertEqual(conn.tx_opened, conn.tx_closed)


class TestUnit(unittest.TestCase):
    def test_registry_ids_are_sequential_unique_and_described(self):
        ids = [m[0] for m in M.MIGRATIONS]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))
        for mid, desc, sql in M.MIGRATIONS:
            self.assertTrue(desc.strip() and sql.strip(), mid)

    def test_every_migration_is_idempotent_by_construction(self):
        for mid, desc, sql in M.MIGRATIONS:
            for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                self.assertRegex(stmt, r"IF NOT EXISTS|OR REPLACE", f"migration {mid}: {stmt[:60]}")

    def test_migration_one_creates_the_tracking_table(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS schema_migrations", M.MIGRATIONS[0][2])

    def test_apply_migration_records_the_row_inside_the_transaction(self):
        conn = _Conn()
        asyncio.run(M.apply_migration(conn, 7, "seven", "CREATE TABLE IF NOT EXISTS a (x int); ; CREATE INDEX IF NOT EXISTS i ON a (x)"))
        stmts = [s for s, _ in conn.executed]
        self.assertEqual(len(stmts), 3)                                # empty fragment between ';;' is skipped
        self.assertEqual(conn.inserts()[0][:2], (7, "seven"))
        self.assertIsInstance(conn.inserts()[0][2], int)               # applied_at is epoch ms
        self.assertEqual((conn.tx_opened, conn.tx_closed), (1, 1))


class TestIntegration(unittest.TestCase):
    def test_logging_is_the_shared_logger_not_print(self):
        self.assertIn("from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN", SRC)
        self.assertIn("/nova_ops", M.DB_DSN)

    def test_applied_set_gates_which_migrations_run_in_order(self):
        out, conn, _ = _run(_Conn(applied={1, 2}))
        self.assertEqual([a[0] for a in conn.inserts()], [3, 4, 5])
        self.assertIn("Applied 3 migration(s)", out)
        self.assertTrue(conn.closed)

    def test_get_applied_feeds_the_list_view(self):
        out, conn, _ = _run(_Conn(applied={1, 3}), argv=["x", "--list"])
        rows = {int(l.split()[0]): l.split()[1] for l in out.splitlines() if l[:4].strip().isdigit()}
        self.assertEqual(rows, {1: "APPLIED", 2: "PENDING", 3: "APPLIED", 4: "PENDING", 5: "PENDING"})
        self.assertEqual(conn.executed, [])


class TestFunctional(unittest.TestCase):
    def test_golden_path_applies_everything_on_a_fresh_database(self):
        out, conn, attempts = _run(_Conn(applied=set()))
        self.assertEqual(attempts, [M.DB_DSN])
        self.assertEqual([a[0] for a in conn.inserts()], [1, 2, 3, 4, 5])
        self.assertEqual(conn.tx_opened, 5)
        self.assertIn("Applied 5 migration(s)", out)
        self.assertTrue(conn.closed)

    def test_up_to_date_database_runs_nothing(self):
        out, conn, _ = _run(_Conn(applied={1, 2, 3, 4, 5}))
        self.assertEqual(conn.executed, [])
        self.assertIn("All migrations applied", out)

    def test_check_reports_pending_without_applying(self):
        out, conn, _ = _run(_Conn(applied={1}), argv=["x", "--check"])
        self.assertIn("Pending migrations (4):", out)
        self.assertIn("[2] Create scheduler_runs table", out)
        self.assertEqual(conn.executed, [])
        self.assertTrue(conn.closed)

    def test_missing_asyncpg_exits_one_without_touching_the_network(self):
        code = "import sys; sys.modules['asyncpg'] = None; import nova_ops_migrate as m; m.main()"
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env=QUIET_ENV)
        self.assertEqual(r.returncode, 1)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=QUIET_ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--check", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ops_migrate"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=QUIET_ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
