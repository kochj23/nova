#!/usr/bin/env python3
"""7-category gap tests for nova_lexicon.py — the 2026-10-06 Damien running bit (The Omen line,
occasional, exactly once, never on grim topics) and the ferengi_rule connect retry / loud
fail-open added here. PG is mocked. Base suite: test_nova_lexicon.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_lexicon_7cat.py
"""
import io
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_lexicon as lx  # noqa: E402


class TestSecurity(unittest.TestCase):
    def test_topic_bound_and_truncated(self):
        cur = MagicMock(); cur.fetchone.return_value = (1, "r")
        conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
        lx.ferengi_rule("x'; TRUNCATE ferengi_rules; --" + "a" * 2000, conn=conn)
        sql, params = cur.execute.call_args_list[0].args
        self.assertNotIn("TRUNCATE", sql)
        self.assertEqual(len(params[0]), 400)

    def test_damien_never_on_grim_topic_even_on_lowest_roll(self):
        for t in ("died in a crash", "Funeral for a friend", "MURDERED", "grief and mourning"):
            self.assertEqual(lx.damien_block(t, roll=0.0), "")


class TestPerformance(unittest.TestCase):
    def test_grim_check_fast_on_long_topic(self):
        t = time.perf_counter()
        for _ in range(2000):
            lx.damien_block("fleet uptime report " * 200, roll=0.5)
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_connect_timeout_set(self):
        with patch("psycopg2.connect", return_value=MagicMock()) as c:
            lx._conn()
        self.assertEqual(c.call_args.kwargs["connect_timeout"], 5)


class TestRetry(unittest.TestCase):
    def test_connect_retried_once(self):
        conn = MagicMock()
        with patch("psycopg2.connect", side_effect=[psycopg2.OperationalError("blip"), conn]) as c, \
                patch("time.sleep") as sl:
            self.assertIs(lx._conn(), conn)
        self.assertEqual(c.call_count, 2)
        sl.assert_called_once_with(1)

    def test_unreachable_is_logged_not_silent(self):
        err = io.StringIO()
        with patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")), patch("time.sleep"), \
                redirect_stderr(err):
            self.assertIsNone(lx.ferengi_rule("profit"))
        self.assertIn("ferengi_rule unavailable", err.getvalue())


class TestUnit(unittest.TestCase):
    def test_damien_rate_and_exactly_once(self):
        fired = [lx.damien_block("watch news", roll=i / 100) for i in range(100)]
        self.assertEqual(sum(1 for f in fired if f), int(lx.DAMIEN_P * 100))
        self.assertTrue(all(f.count(lx.DAMIEN_LINE) == 1 for f in fired if f))

    def test_film_spelling(self):
        self.assertEqual(lx.DAMIEN_LINE, "It's all for you, Damien!")


class TestIntegration(unittest.TestCase):
    def test_seasoning_includes_rule_and_survives_db_loss(self):
        with patch.object(lx, "ferengi_rule", return_value=(9, "Opportunity plus instinct equals profit.")):
            out = lx.seasoning("operations", "fleet profit")
        self.assertIn("Opportunity plus instinct", out)
        with patch.object(lx, "_conn", side_effect=psycopg2.OperationalError("down")), redirect_stderr(io.StringIO()):
            self.assertTrue(lx.seasoning("operations", "fleet profit"))


class TestFunctional(unittest.TestCase):
    def test_golden_and_grim_paths(self):
        with patch.object(lx, "ferengi_rule", return_value=None), patch.object(lx.random, "random", return_value=0.0):
            happy = lx.seasoning("operations", "new NAS arrived")
            grim = lx.seasoning("operations", "fatal crash on the 5")
        self.assertIn(lx.DAMIEN_LINE, happy)
        self.assertNotIn(lx.DAMIEN_LINE, grim)


class TestFrame(unittest.TestCase):
    def test_compiles_and_imports(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPTS / "nova_lexicon.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(callable(lx.seasoning) and callable(lx.damien_block))


if __name__ == "__main__":
    unittest.main()
