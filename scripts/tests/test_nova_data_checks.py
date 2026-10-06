#!/usr/bin/env python3
"""Tests for nova_data_checks.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dc = _load("nova_data_checks_t", SCRIPTS / "nova_data_checks.py")
SRC = (SCRIPTS / "nova_data_checks.py").read_text()


def _pg(value=None, exc=None):
    """psycopg2.connect stub whose cursor returns `value` from fetchone (or raises `exc`)."""
    cur = MagicMock()
    cur.fetchone.return_value = value
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    return MagicMock(side_effect=exc) if exc else MagicMock(return_value=conn), cur


class _Base(unittest.TestCase):
    def setUp(self):
        dc._last_run.clear()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", SRC)

    def test_sql_is_read_only_and_interpolates_only_constants(self):
        self.assertNotRegex(SRC, r"\b(INSERT INTO|UPDATE \w+ SET|DELETE FROM)\b")
        # the one f-string query interpolates the module constant INGEST_WINDOW, never input
        self.assertEqual(re.findall(r"interval '\{(\w+)\}'", SRC), ["INGEST_WINDOW"])


class TestPerformance(_Base):
    def test_10k_run_one_fast(self):
        chk = dc.Check("x", 0, "warning", lambda: (True, ""), None)
        t0 = time.perf_counter()
        for _ in range(10_000):
            dc._run_one(chk)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_backup_scan_many_files_fast(self):
        with tempfile.TemporaryDirectory() as td:
            for i in range(500):
                (Path(td) / f"b{i}.sql").write_text("x")
            with patch.object(dc, "BACKUP_DIRS", [td]):
                t0 = time.perf_counter()
                self.assertEqual(dc._check_backup(), (True, ""))
                self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(_Base):
    def test_pg_error_is_fail_closed_single_attempt(self):
        # RETRY GAP: _scalar/psycopg2.connect — one attempt by design (a DB error IS the signal); never raises
        connect, _ = _pg(exc=RuntimeError("pgvector missing"))
        with patch.object(dc.psycopg2, "connect", connect):
            ok, msg = dc._check_vector()
        self.assertEqual(connect.call_count, 1)
        self.assertFalse(ok)
        self.assertIn("VECTOR STORE DOWN", msg)

    def test_http_and_socket_failures_fail_closed(self):
        # RETRY GAP: _check_memory_server/_check_mqtt — one attempt each; not-ok message, no exception
        with patch.object(dc.urllib.request, "urlopen", side_effect=OSError("refused")) as uo, \
                patch.object(dc.socket, "create_connection", side_effect=OSError("timeout")) as cc:
            self.assertFalse(dc._check_memory_server()[0])
            self.assertIn("unreachable", dc._check_mqtt()[1])
        self.assertEqual((uo.call_count, cc.call_count), (1, 1))


class TestUnit(_Base):
    def test_email_freshness_thresholds(self):
        for value, ok, frag in [((None,), False, "no email_archive"), ((dc.EMAIL_MAX_AGE_S + 7200,), False, "STALE"),
                                ((60,), True, "")]:
            connect, _ = _pg(value)
            with patch.object(dc.psycopg2, "connect", connect):
                r = dc._check_email()
            self.assertEqual(r[0], ok)
            self.assertIn(frag, r[1])

    def test_ingest_and_energy(self):
        for fn, value, ok in [(dc._check_ingest, (0,), False), (dc._check_ingest, (5,), True),
                              (dc._check_energy, (None,), False), (dc._check_energy, (60,), True),
                              (dc._check_energy, (dc.ENERGY_MAX_AGE_S + 1,), False)]:
            connect, _ = _pg(value)
            with patch.object(dc.psycopg2, "connect", connect):
                self.assertEqual(fn()[0], ok, (fn.__name__, value))

    def test_article_cadence(self):
        fresh = subprocess.CompletedProcess([], 0, stdout=str(int(time.time()) - 60), stderr="")
        old = subprocess.CompletedProcess([], 0, stdout=str(int(time.time()) - 200_000), stderr="")
        bad = subprocess.CompletedProcess([], 128, stdout="", stderr="not a repo")
        with patch.object(dc.subprocess, "run", side_effect=[fresh, old, bad]):
            self.assertTrue(dc._check_article()[0])
            self.assertIn("journal quiet", dc._check_article()[1])
            self.assertIn("git log failed", dc._check_article()[1])

    def test_backup_missing_and_stale(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(dc, "BACKUP_DIRS", [td]):
                self.assertIn("no DB backups", dc._check_backup()[1])
                f = Path(td) / "old.sql"
                f.write_text("x")
                os.utime(f, (time.time() - 3 * 86400,) * 2)
                self.assertIn("STALE", dc._check_backup()[1])

    def test_run_one_catches_crashing_check(self):
        r = dc._run_one(dc.Check("boom", 0, "critical", lambda: 1 / 0, None))
        self.assertEqual((r.ok, r.severity), (False, "critical"))
        self.assertIn("raised", r.message)


class TestIntegration(_Base):
    def test_checks_target_the_right_databases(self):
        connect, cur = _pg((1,))
        with patch.object(dc.psycopg2, "connect", connect):
            dc._check_vector()
            dc._check_energy()
        dsns = [c.args[0] for c in connect.call_args_list]
        self.assertIn("dbname=nova_memories", dsns[0])
        self.assertIn("dbname=nova_ops", dsns[1])
        self.assertIn("energy_readings", cur.execute.call_args_list[1].args[0])

    def test_memory_server_health_parse(self):
        resp = MagicMock()
        resp.__enter__.return_value.read.return_value = b'{"status": "degraded"}'
        with patch.object(dc.urllib.request, "urlopen", return_value=resp):
            self.assertIn("unhealthy", dc._check_memory_server()[1])


class TestFunctional(_Base):
    def _fake_checks(self, vector_ok):
        calls = []

        def mk(cid, ok):
            def fn():
                calls.append(cid)
                return ok, "" if ok else f"{cid} bad"
            return fn
        checks = [dc.Check("vector_liveness", 300, "critical", mk("vector_liveness", vector_ok), None),
                  dc.Check("email_freshness", 3600, "warning", mk("email_freshness", True), "vector_liveness"),
                  dc.Check("mqtt_broker", 300, "warning", mk("mqtt_broker", True), None)]
        return checks, calls

    def test_dependency_skip_and_interval_gating(self):
        checks, calls = self._fake_checks(vector_ok=False)
        with patch.object(dc, "CHECKS", checks):
            res = dc.run_checks(force=True)
            self.assertEqual([r.id for r in res], ["vector_liveness", "mqtt_broker"])
            self.assertNotIn("email_freshness", calls)
            # next sweep: ran checks are not due yet; the skipped dependent gets its turn
            self.assertEqual([r.id for r in dc.run_due_checks()], ["email_freshness"])
            self.assertEqual(dc.run_due_checks(), [])

    def test_all_green_pass(self):
        checks, _ = self._fake_checks(vector_ok=True)
        with patch.object(dc, "CHECKS", checks):
            self.assertTrue(all(r.ok for r in dc.run_due_checks()))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_checks(self):
        # the __main__ block runs every live check (PG/MQTT/HTTP), so the smoke is a plain import
        r = subprocess.run([sys.executable, "-c", "import nova_data_checks as m; print(len(m.CHECKS))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "8")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
