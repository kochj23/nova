#!/usr/bin/env python3
"""7-category tests for sql/claude_fleet_coordination.sql (claim/lease/lock/reap functions).

Static checks always run. The live checks build a THROWAWAY database on the local PostgreSQL
(nova_coord_selftest_<pid>), create a minimal claude_queue/claude_sessions/claude_actions schema,
apply the migration twice, run the shipped claude_fleet_coordination_test.sql through psql and
exercise concurrency, then drop the throwaway database. nova_ops is never touched; the live
tests skip when no local PG/psql is available. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_claude_fleet_coordination_sql_7cat.py
"""
import os
import re
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path

import psycopg2

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
MIGRATION = SQL_DIR / "claude_fleet_coordination.sql"
SELFTEST = SQL_DIR / "claude_fleet_coordination_test.sql"
SQL = MIGRATION.read_text()
TEST_SQL = SELFTEST.read_text()
HOST = os.environ.get("NOVA_TEST_PGHOST", "localhost")
DBNAME = f"nova_coord_selftest_{os.getpid()}"
PSQL = shutil.which("psql") or "/opt/homebrew/bin/psql"

BASE_SCHEMA = """
CREATE TABLE claude_sessions (session_id text PRIMARY KEY, host text DEFAULT 'h',
                              started_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE claude_actions (id serial PRIMARY KEY, session_id text, ts timestamptz NOT NULL DEFAULT now(),
                             description text);
CREATE TABLE claude_queue (id serial PRIMARY KEY, session_id text NOT NULL,
                           created_at timestamptz NOT NULL DEFAULT now(),
                           updated_at timestamptz NOT NULL DEFAULT now(),
                           status text NOT NULL DEFAULT 'queued', priority int NOT NULL DEFAULT 5,
                           description text NOT NULL, context text, outcome text, completed_at timestamptz);
"""


def _admin():
    c = psycopg2.connect(host=HOST, dbname="postgres", connect_timeout=3)
    c.autocommit = True
    return c


def _pg_available():
    try:
        _admin().close()
        return Path(PSQL).exists()
    except Exception:
        return False


LIVE = _pg_available()


def _db():
    c = psycopg2.connect(host=HOST, dbname=DBNAME, connect_timeout=3)
    c.autocommit = True
    return c


def setUpModule():
    if not LIVE:
        return
    assert DBNAME.startswith("nova_coord_selftest_")
    a = _admin()
    a.cursor().execute(f'CREATE DATABASE "{DBNAME}"')
    a.close()
    c = _db()
    c.cursor().execute(BASE_SCHEMA)
    c.close()
    for _ in range(2):   # applying twice proves the migration is idempotent
        r = _psql_file(MIGRATION)
        assert r.returncode == 0, r.stderr


def tearDownModule():
    if not LIVE:
        return
    assert DBNAME.startswith("nova_coord_selftest_") and DBNAME != "nova_ops"
    a = _admin()
    a.cursor().execute(f'DROP DATABASE IF EXISTS "{DBNAME}" WITH (FORCE)')
    a.close()


def _psql_file(path):
    return subprocess.run([PSQL, "-h", HOST, "-d", DBNAME, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)],
                          capture_output=True, text=True, timeout=60)


def _task(cur, desc="t", prio=5):
    cur.execute("INSERT INTO claude_queue (session_id, description, priority) VALUES ('s', %s, %s) RETURNING id",
                (desc, prio))
    return cur.fetchone()[0]


class TestSecurity(unittest.TestCase):
    def test_no_public_grants_or_security_definer(self):
        self.assertNotRegex(SQL.upper(), r"GRANT\s+.*\s+TO\s+PUBLIC|SECURITY\s+DEFINER")

    def test_no_secrets(self):
        self.assertNotRegex(SQL.lower(), r"password|secret|token")

    def test_functions_take_parameters_not_dynamic_sql(self):
        self.assertNotRegex(SQL.upper(), r"\bEXECUTE\s+FORMAT|\bEXECUTE\s+'")

    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_only_holder_can_note_finish_or_unlock(self):
        c = _db(); cur = c.cursor()
        tid = _task(cur)
        cur.execute("SELECT count(*) FROM claude_claim('hA/s1', %s)", (tid,))
        cur.execute("SELECT claude_note(%s, 'evil/x', 'hijack'), claude_finish(%s, 'evil/x', 'done', 'x')", (tid, tid))
        self.assertEqual(cur.fetchone(), (False, False))
        cur.execute("SELECT claude_lock('repo:sec', 'hA/s1'), claude_unlock('repo:sec', 'evil/x')")
        self.assertEqual(cur.fetchone(), (True, False))
        c.close()

    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_injection_text_is_stored_as_data(self):
        c = _db(); cur = c.cursor()
        tid = _task(cur)
        cur.execute("SELECT count(*) FROM claude_claim('hA/s1', %s)", (tid,))
        evil = "x'); DELETE FROM claude_queue; --"
        cur.execute("SELECT claude_note(%s, 'hA/s1', %s)", (tid, evil))
        cur.execute("SELECT count(*) FROM claude_queue WHERE id = %s AND progress LIKE %s", (tid, f"%{evil}%"))
        self.assertEqual(cur.fetchone()[0], 1)
        c.close()


class TestPerformance(unittest.TestCase):
    def test_claim_and_reap_skip_locked_rows(self):
        self.assertEqual(SQL.count("FOR UPDATE SKIP LOCKED"), 2)
        self.assertIn("LIMIT 1", SQL)

    def test_claim_index_present(self):
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_claude_queue_claim", SQL)

    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_reap_of_many_expired_claims_is_fast(self):
        c = _db(); cur = c.cursor()
        cur.execute("INSERT INTO claude_queue (session_id, description, status, claimed_by, lease_until) "
                    "SELECT 's', 'bulk', 'in_progress', 'h/dead', now() - interval '1 hour' FROM generate_series(1, 3000)")
        t = time.perf_counter()
        cur.execute("SELECT count(*) FROM claude_reap()")
        n = cur.fetchone()[0]
        self.assertLess(time.perf_counter() - t, 5.0)
        self.assertGreaterEqual(n, 3000)
        c.close()


class TestRetry(unittest.TestCase):
    """Retry semantics for the fleet: a dead session's work is retried by another one."""

    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_expired_lease_reclaimable_without_reaper(self):
        c = _db(); cur = c.cursor()
        tid = _task(cur)
        cur.execute("SELECT count(*) FROM claude_claim('hA/s1', %s)", (tid,))
        cur.execute("UPDATE claude_queue SET lease_until = now() - interval '1 minute' WHERE id = %s", (tid,))
        cur.execute("SELECT claimed_by, progress FROM claude_claim('hB/s2', %s)", (tid,))
        who, progress = cur.fetchone()
        self.assertEqual(who, "hB/s2")
        self.assertIn("took over from hA/s1", progress)
        c.close()

    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_finish_queued_hands_task_back(self):
        c = _db(); cur = c.cursor()
        tid = _task(cur)
        cur.execute("SELECT count(*) FROM claude_claim('hA/s1', %s)", (tid,))
        cur.execute("SELECT claude_finish(%s, 'hA/s1', 'queued', 'blocked')", (tid,))
        cur.execute("SELECT status, claimed_by FROM claude_queue WHERE id = %s", (tid,))
        self.assertEqual(cur.fetchone(), ("queued", None))
        cur.execute("SELECT count(*) FROM claude_claim('hB/s2', %s)", (tid,))
        self.assertEqual(cur.fetchone()[0], 1)
        c.close()


class TestUnit(unittest.TestCase):
    def test_migration_is_transactional_and_idempotent_text(self):
        body = SQL.strip()
        self.assertTrue(body.startswith("BEGIN;") and body.endswith("COMMIT;"))
        self.assertNotRegex(SQL, r"CREATE TABLE (?!IF NOT EXISTS)")
        self.assertNotRegex(SQL, r"CREATE FUNCTION")   # always CREATE OR REPLACE
        self.assertIn("ADD COLUMN IF NOT EXISTS", SQL)

    def test_selftest_always_rolls_back(self):
        self.assertTrue(TEST_SQL.strip().endswith("ROLLBACK;"))
        self.assertNotIn("COMMIT", TEST_SQL)

    def test_every_function_defined(self):
        for fn in ("claude_claim", "claude_note", "claude_finish", "claude_lock", "claude_unlock",
                   "claude_heartbeat", "claude_reap"):
            self.assertRegex(SQL, rf"CREATE OR REPLACE FUNCTION {fn}\(")


class TestIntegration(unittest.TestCase):
    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_shipped_selftest_passes(self):
        r = _psql_file(SELFTEST)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all assertions passed", r.stderr + r.stdout)

    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_heartbeat_renews_only_that_sessions_claims(self):
        c = _db(); cur = c.cursor()
        a, b = _task(cur), _task(cur)
        cur.execute("SELECT count(*) FROM claude_claim('h/aaaa', %s)", (a,))
        cur.execute("SELECT count(*) FROM claude_claim('h/bbbb', %s)", (b,))
        cur.execute("UPDATE claude_queue SET lease_until = now() + interval '1 minute' WHERE id IN (%s, %s)", (a, b))
        cur.execute("SELECT claude_heartbeat('aaaa')")
        cur.execute("SELECT id, lease_until > now() + interval '10 minutes' FROM claude_queue WHERE id IN (%s, %s) ORDER BY id",
                    (a, b))
        self.assertEqual(dict(cur.fetchall()), {a: True, b: False})
        c.close()

    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_board_view_lists_live_session(self):
        c = _db(); cur = c.cursor()
        sid = "abcdef12-0000-0000-0000-000000000000"
        cur.execute("INSERT INTO claude_sessions (session_id, host) VALUES (%s, 'nodeX')", (sid,))
        cur.execute("INSERT INTO claude_actions (session_id, description) VALUES (%s, 'editing')", (sid,))
        tid = _task(cur, "board task")
        cur.execute("SELECT count(*) FROM claude_claim(%s, %s)", (f"nodeX/{sid}", tid))
        cur.execute("SELECT host, doing, claims FROM claude_board WHERE host = 'nodeX'")
        host, doing, claims = cur.fetchone()
        self.assertEqual(doing, "editing")
        self.assertIn(f"#{tid} board task", claims)
        c.close()


class TestFunctional(unittest.TestCase):
    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_concurrent_claims_never_double_assign(self):
        c = _db(); cur = c.cursor()
        cur.execute("DELETE FROM claude_queue WHERE status IN ('queued', 'pending')")
        ids = [_task(cur, f"c{i}", prio=-50) for i in range(5)]
        c.close()
        got, errs = [], []

        def worker(n):
            try:
                cc = _db(); k = cc.cursor()
                for _ in range(5):
                    k.execute("SELECT id FROM claude_claim(%s)", (f"h/w{n}",))
                    row = k.fetchone()
                    if row:
                        got.append(row[0])
                cc.close()
            except Exception as e:   # surfaced below, never swallowed
                errs.append(e)
        ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(sorted(got), sorted(ids))   # each task claimed exactly once

    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_live_claim_cannot_be_stolen_and_reap_ignores_live(self):
        c = _db(); cur = c.cursor()
        tid = _task(cur)
        cur.execute("SELECT count(*) FROM claude_claim('hA/s1', %s)", (tid,))
        cur.execute("SELECT count(*) FROM claude_claim('hB/s2', %s)", (tid,))
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("SELECT count(*) FROM claude_reap() r WHERE r.id = %s", (tid,))
        self.assertEqual(cur.fetchone()[0], 0)
        c.close()


class TestFrame(unittest.TestCase):
    def test_files_exist_and_balanced_dollar_quotes(self):
        self.assertTrue(MIGRATION.exists() and SELFTEST.exists())
        self.assertEqual(SQL.count("$$") % 2, 0)
        self.assertEqual(TEST_SQL.count("$$") % 2, 0)

    @unittest.skipUnless(LIVE, "local PostgreSQL not available")
    def test_migration_applied_cleanly(self):
        c = _db(); cur = c.cursor()
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname LIKE 'claude\\_%%'")
        self.assertGreaterEqual(cur.fetchone()[0], 7)
        c.close()


if __name__ == "__main__":
    unittest.main()
