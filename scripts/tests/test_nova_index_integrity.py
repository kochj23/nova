#!/usr/bin/env python3
"""Tests for nova_index_integrity.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). psycopg2 and the Slack/Discord alert are mocked.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_index_integrity.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("idxint", SCRIPTS / "nova_index_integrity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ii = _load()


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))
        if sql.startswith("SELECT bt_index_check") and params[0] in self.conn.corrupt:
            raise RuntimeError(f"index \"{params[0]}\" lacks a main relation tuple\nDETAIL: more")

    def fetchall(self):
        return self.conn.idxs


class _Conn:
    def __init__(self, idxs=(), corrupt=()):
        self.idxs = list(idxs); self.corrupt = set(corrupt); self.sql = []; self.autocommit = False; self.closed = False

    def cursor(self):
        return _Cur(self)

    def close(self):
        self.closed = True


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", SRC)

    def test_index_name_is_bound_and_size_is_int(self):
        conn = _Conn(idxs=[("public.x'; --", "x")])
        with patch("psycopg2.connect", return_value=conn):
            ii.sweep("nova_ops", 300, False)
        check = [(s, p) for s, p in conn.sql if "bt_index_check" in s][0]
        self.assertEqual(check[1], ("public.x'; --",))
        # the only interpolated value in the catalog query is --max-mb, which argparse forces to int
        self.assertIn("type=int", SRC)
        with patch.object(sys, "argv", ["x", "--max-mb", "1; DELETE"]), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                ii.main()


class TestPerformance(unittest.TestCase):
    def test_sweep_10k_indexes_fast_and_large_ones_skipped(self):
        conn = _Conn(idxs=[(f"public.i{i}", f"i{i}") for i in range(10_000)])
        t0 = time.perf_counter()
        with patch("psycopg2.connect", return_value=conn):
            bad, n = ii.sweep("nova_ops", 300, False)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual((bad, n), ([], 10_000))
        self.assertIn("< 300 * 1024 * 1024", conn.sql[1][0])
        with patch("psycopg2.connect", return_value=_Conn()) as c:
            ii.sweep("nova_ops", 300, True)
        self.assertNotIn("pg_relation_size(c.oid) <", c.return_value.sql[1][0])


class TestRetry(unittest.TestCase):
    def test_connect_failure_fails_open_per_db(self):
        # RETRY GAP: sweep()/psycopg2.connect — one attempt per DB; failure is reported, not raised
        calls = []

        def boom(*a, **k):
            calls.append(a); raise RuntimeError("could not connect to server\n")

        with patch("psycopg2.connect", side_effect=boom):
            bad, n = ii.sweep("nova_media", 300, False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(n, 0)
        self.assertEqual(bad[0][0], "__connect__")
        self.assertIn("could not connect", bad[0][1])

    def test_alert_failure_does_not_crash(self):
        with patch.object(ii, "sweep", return_value=([("k", "bad")], 1)), \
             patch("nova_config.post_both", side_effect=RuntimeError("slack down")), \
             patch.object(sys, "argv", ["x", "--quiet"]), redirect_stdout(io.StringIO()), \
             redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ii.main(), 1)
        self.assertIn("alert failed", err.getvalue())


class TestUnit(unittest.TestCase):
    def test_corrupt_index_collected_with_first_line_only(self):
        conn = _Conn(idxs=[("public.a", "a"), ("public.b", "b")], corrupt={"public.b"})
        with patch("psycopg2.connect", return_value=conn):
            bad, n = ii.sweep("nova_ops", 300, False)
        self.assertEqual(n, 2)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0][0], "b")
        self.assertNotIn("DETAIL", bad[0][1])
        self.assertTrue(conn.closed)

    def test_amcheck_create_failure_tolerated(self):
        class C(_Conn):
            def cursor(self):
                cur = _Cur(self); orig = cur.execute

                def ex(sql, params=None):
                    if sql.startswith("CREATE EXTENSION"):
                        raise RuntimeError("permission denied")
                    return orig(sql, params)
                cur.execute = ex
                return cur
        with patch("psycopg2.connect", return_value=C(idxs=[("public.a", "a")])):
            self.assertEqual(ii.sweep("nova", 300, False), ([], 1))


class TestIntegration(unittest.TestCase):
    def test_heapallindexed_and_all_dbs(self):
        conn = _Conn(idxs=[("public.a", "a")])
        with patch("psycopg2.connect", return_value=conn) as c:
            ii.sweep("nova_ops", 300, False)
        self.assertIn("heapallindexed => true", conn.sql[-1][0])
        self.assertIn("dbname=nova_ops", c.call_args[0][0])
        self.assertEqual(ii.DBS, ["nova_ops", "nova_memories", "nova_media", "nova"])

    def test_alert_uses_shared_post_both_to_alerts(self):
        self.assertIn("nova_config.post_both(msg, slack_channel=nova_config.SLACK_ALERTS)", SRC)


class TestFunctional(unittest.TestCase):
    def test_clean_run_returns_zero_without_alert(self):
        with patch.object(ii, "sweep", return_value=([], 7)), patch("nova_config.post_both") as pb, \
             patch.object(sys, "argv", ["x"]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ii.main(), 0)
        pb.assert_not_called()
        self.assertIn("28 unique/PK indexes verified, 0 corrupt", out.getvalue())

    def test_corruption_alerts_once(self):
        def sw(db, mb, full):
            return ([("claude_memories_name_key", "lacks tuple")], 3) if db == "nova_ops" else ([], 3)
        with patch.object(ii, "sweep", side_effect=sw), patch("nova_config.post_both") as pb, \
             patch.object(sys, "argv", ["x", "--quiet"]), redirect_stdout(io.StringIO()):
            self.assertEqual(ii.main(), 1)
        pb.assert_called_once()
        self.assertIn("claude_memories_name_key", pb.call_args[0][0])
        self.assertIn("REINDEX INDEX CONCURRENTLY", pb.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_index_integrity.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--full", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_index_integrity"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
