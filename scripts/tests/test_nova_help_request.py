#!/usr/bin/env python3
"""Tests for nova_help_request.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_help_request.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hr = _load("help_request_t", SCRIPT)
SRC = SCRIPT.read_text()
hr.subprocess = MagicMock()   # no psql / redis-cli from a test
hr.subprocess.run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")


class _PG:
    """Fake psycopg2 module recording every statement; fetchone answers from `existing`."""
    def __init__(self, existing=None):
        self.existing = existing; self.stmts = []

    def connect(self, dsn):
        pg = self

        class C:
            autocommit = False

            def cursor(self):
                return SimpleNamespace(execute=lambda s, p=(): pg.stmts.append((s, p)), fetchone=lambda: pg.existing)

            def close(self):
                pass
        return C()


def _with_pg(pg):
    return patch.dict(sys.modules, {"psycopg2": pg})


def _published():
    return [c.args[0] for c in hr.subprocess.run.call_args_list if c.args[0][0] == "redis-cli"]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_param_sql(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", hr.PG_DSN)
        self.assertIsNone(re.search(r'_pg_(execute|fetchone)\(\s*f["\']', SRC))

    def test_hostile_description_is_a_bound_param(self):
        pg = _PG()
        evil = "x'); SELECT pg_sleep(9); --"
        hr.subprocess.run.reset_mock()
        with _with_pg(pg):
            hr.request_help("code_bug", evil)
        sql, params = pg.stmts[-1]
        self.assertNotIn("pg_sleep", sql)
        self.assertEqual(params[3], evil)

    def test_psql_fallback_escapes_quotes(self):
        hr.subprocess.run.reset_mock()
        with patch.dict(sys.modules, {"psycopg2": None, "psycopg": None}):
            self.assertTrue(hr._pg_execute("INSERT INTO t VALUES (%s, %s, %s)", ("o'brien", None, 3)))
        q = hr.subprocess.run.call_args.args[0][-1]
        self.assertEqual(q, "INSERT INTO t VALUES ('o''brien', NULL, 3)")


class TestPerformance(unittest.TestCase):
    def test_10k_requests_hot_path(self):
        pg = _PG()
        t0 = time.perf_counter()
        with _with_pg(pg), patch.object(hr, "_redis_publish"):
            for i in range(10_000):
                hr.request_help("performance", f"d{i}", {"k": i})
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(pg.stmts), 20_000)


class TestRetry(unittest.TestCase):
    def test_redis_publish_fails_open(self):
        # RETRY GAP: _redis_publish — one redis-cli attempt, failures swallowed (fire-and-forget)
        with patch.object(hr.subprocess, "run", side_effect=OSError("no redis-cli")) as r:
            hr._redis_publish("c", {"a": 1})
        self.assertEqual(r.call_count, 1)

    def test_pg_error_is_not_retried(self):
        # RETRY GAP: _pg_execute — one connect; a non-ImportError escapes to the caller (no backoff)
        pg = MagicMock(); pg.connect.side_effect = OSError("pg down")
        with _with_pg(pg):
            with self.assertRaises(OSError):
                hr._pg_execute("SELECT 1")
        self.assertEqual(pg.connect.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_priority_mapping(self):
        pg = _PG()
        with _with_pg(pg), patch.object(hr, "_redis_publish"):
            hr.request_help("feature_request", "a"); hr.request_help("weird", "b")
        prios = [p[2] for s, p in pg.stmts if s.startswith("INSERT")]
        self.assertEqual(prios, [4, hr.DEFAULT_PRIORITY])

    def test_fetchone_returns_none_without_drivers(self):
        with patch.dict(sys.modules, {"psycopg2": None, "psycopg": None}):
            self.assertIsNone(hr._pg_fetchone("SELECT 1"))

    def test_failure_wrapper_truncates_error(self):
        with patch.object(hr, "request_help") as rh:
            hr.request_help_for_failure("t1", "/x.py", "E" * 2000, 4, last_success="yesterday")
        cat, desc, ctx = rh.call_args.args
        self.assertEqual(cat, "code_bug")
        self.assertIn("4 consecutive failures", desc)
        self.assertEqual(len(ctx["error"]), 500)
        self.assertEqual(ctx["last_success"], "yesterday")
        with patch.object(hr, "request_help") as rh:
            hr.request_help_for_failure("t1", "/x.py", "", 1)
        self.assertEqual(rh.call_args.args[2]["error"], "no error captured")


class TestIntegration(unittest.TestCase):
    def test_queue_row_then_redis_notification(self):
        pg = _PG()
        hr.subprocess.run.reset_mock()
        with _with_pg(pg):
            hr.request_help("config_issue", "D" * 300, {"file": "a.py"})
        sql, params = pg.stmts[-1]
        self.assertIn("INSERT INTO claude_queue", sql)
        self.assertEqual(params[:3], (hr.BRIDGE_SESSION_ID, "queued", 2))
        self.assertEqual(json.loads(params[4])["from"], "nova-help-request")
        cmd = _published()[0]
        self.assertEqual(cmd[5:7], ["PUBLISH", "nova:to_claude"])
        self.assertEqual(len(json.loads(cmd[7])["description"]), 200)


class TestFunctional(unittest.TestCase):
    def test_duplicate_is_not_requeued(self):
        pg = _PG(existing=(1,))
        hr.subprocess.run.reset_mock()
        with _with_pg(pg):
            hr.request_help("code_bug", "same")
        self.assertFalse(any(s.startswith("INSERT") for s, _ in pg.stmts))
        self.assertEqual(_published(), [])

    def test_cli_golden_and_bad_json(self):
        pg = _PG()
        with _with_pg(pg), patch.object(hr, "_redis_publish"), \
                patch.object(sys, "argv", ["x", "performance", "slow", "--context", '{"ms": 5}']), \
                redirect_stdout(io.StringIO()) as out:
            hr.main()
        self.assertIn("priority=3", out.getvalue())
        with patch.object(sys, "argv", ["x", "code_bug", "d", "--context", "{bad"]), \
                patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                hr.main()
        self.assertEqual(e.exception.code, 1)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("code_bug", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_help_request"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
