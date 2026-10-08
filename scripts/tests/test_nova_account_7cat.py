#!/usr/bin/env python3
"""7-category gap tests for nova_account.py — the ledger-query and HEAD-probe retries added here
(the base suite marked both 'RETRY GAP'). PG and HTTP are mocked; the organ stays read-only.
Base suite: test_nova_account.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_account_7cat.py
"""
import importlib.util
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_account.py"
spec = importlib.util.spec_from_file_location("account_7cat", SCRIPT)
acct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acct)


def conn(rows):
    cur = MagicMock(); cur.fetchall.return_value = rows
    c = MagicMock(); c.cursor.return_value = cur
    return c, cur


class _Base(unittest.TestCase):
    def setUp(self):
        p = patch("time.sleep"); self.sleep = p.start(); self.addCleanup(p.stop)


class TestSecurity(_Base):
    def test_query_args_bound(self):
        c, cur = conn([])
        with patch.object(acct.psycopg2, "connect", return_value=c):
            acct._q(acct.OPS, "SELECT 1 WHERE x=%s", ("'; DROP --",))
        self.assertEqual(cur.execute.call_args.args, ("SELECT 1 WHERE x=%s", ("'; DROP --",)))

    def test_error_text_capped(self):
        with patch.object(acct.psycopg2, "connect", side_effect=OSError("x" * 1000)):
            self.assertLess(len(acct._q(acct.OPS, "SELECT 1")["error"]), 200)


class TestPerformance(_Base):
    def test_bounded_attempts_and_timeouts(self):
        with patch.object(acct.psycopg2, "connect", side_effect=OSError("down")) as c:
            acct._q(acct.OPS, "SELECT 1")
        self.assertEqual(c.call_count, 2)
        self.assertEqual(c.call_args.kwargs["connect_timeout"], 5)
        with patch.object(acct.urllib.request, "urlopen", side_effect=OSError("x")) as u:
            acct._http("https://x/")
        self.assertEqual(u.call_count, 2)
        self.assertEqual(u.call_args.kwargs["timeout"], 10)


class TestRetry(_Base):
    def test_query_recovers_after_blip(self):
        c, _ = conn([{"n": 1}])
        with patch.object(acct.psycopg2, "connect", side_effect=[OSError("failover"), c]):
            self.assertEqual(acct._q(acct.OPS, "SELECT 1"), [{"n": 1}])
        self.sleep.assert_called_once_with(1)

    def test_head_recovers_after_blip(self):
        ok = MagicMock(status=200)
        with patch.object(acct.urllib.request, "urlopen", side_effect=[OSError("reset"), ok]):
            self.assertEqual(acct._http("https://x/"), 200)

    def test_http_error_is_an_answer_not_retried(self):
        with patch.object(acct.urllib.request, "urlopen",
                          side_effect=urllib.error.HTTPError("u", 404, "nf", {}, None)) as u:
            self.assertEqual(acct._http("https://x/"), 404)
        self.assertEqual(u.call_count, 1)


class TestUnit(_Base):
    def test_unreachable_reported_never_guessed(self):
        with patch.object(acct.psycopg2, "connect", side_effect=OSError("pg down")):
            self.assertEqual(acct._q(acct.OPS, "SELECT 1"), {"error": "OSError: pg down"})


class TestIntegration(_Base):
    def test_learned_tolerates_unreachable_memories(self):
        with patch.object(acct.psycopg2, "connect", side_effect=OSError("down")):
            out = acct.learned(acct._d("2026-10-08"))
        self.assertIsInstance(out, dict)


class TestFunctional(_Base):
    def test_cli_help(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60,
                           cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
