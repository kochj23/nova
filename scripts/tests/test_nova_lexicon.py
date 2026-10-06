#!/usr/bin/env python3
"""Tests for nova_lexicon.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
psycopg2 (the Ferengi rules table) is mocked via _conn; the CLI is exercised in-process."""
import importlib.util
import os
import random
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_lexicon.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_lexicon_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lx = _load()


def _conn(matched=(48, "The bigger the smile, the sharper the knife."), fallback=(1, "Once you have their money...")):
    c = MagicMock()
    cur = c.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [matched, fallback]
    return c, cur


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_parameterized_topic(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]", SRC, re.I))
        c, cur = _conn()
        evil = "x'); " + "DR" + "OP TABLE ferengi_rules;--"
        lx.ferengi_rule(evil, conn=c)
        sql, params = cur.execute.call_args_list[0][0]
        self.assertNotIn(evil, sql)
        self.assertEqual(params, (evil,))

    def test_topic_length_capped(self):
        c, cur = _conn()
        lx.ferengi_rule("z" * 5000, conn=c)
        self.assertEqual(len(cur.execute.call_args_list[0][0][1][0]), 400)

    def test_emergency_and_unknown_sections_never_seasoned(self):
        with patch.object(lx, "ferengi_rule") as fr:
            for bad in ("", "breaking", "emergency", "wat"):
                self.assertEqual(lx.seasoning(bad, "brush fire evacuation"), "")
        fr.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_10k_seasonings_fast(self):
        with patch.object(lx, "ferengi_rule", return_value=(9, "Opportunity plus instinct equals profit.")):
            t0 = time.perf_counter()
            for i in range(10_000):
                lx.seasoning("operations", f"topic {i}")
        self.assertLess(time.perf_counter() - t0, 5.0)


class TestRetry(unittest.TestCase):
    def test_db_unreachable_fails_open(self):
        # RETRY GAP: ferengi_rule()/_conn — one connect; failure -> None, seasoning still renders
        with patch.object(lx, "_conn", side_effect=RuntimeError("pg down")) as c:
            self.assertIsNone(lx.ferengi_rule("profit"))
            out = lx.seasoning("essays", "profit")
        self.assertEqual(c.call_count, 2)
        self.assertNotIn("RULE OF ACQUISITION", out)
        self.assertIn("LIBERALLY", out)

    def test_own_connection_closed_even_on_error(self):
        c, cur = _conn()
        cur.execute.side_effect = RuntimeError("query failed")
        with patch.object(lx, "_conn", return_value=c):
            self.assertIsNone(lx.ferengi_rule("x"))
        c.close.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_relevant_rule_then_random_fallback(self):
        c, cur = _conn(matched=None)
        self.assertEqual(lx.ferengi_rule("nothing matches", conn=c)[0], 1)
        self.assertIn("ORDER BY random()", cur.execute.call_args_list[-1][0][0])
        c2, cur2 = _conn()
        lx.ferengi_rule("   ", conn=c2)                     # blank topic skips the FTS query
        self.assertEqual(cur2.execute.call_count, 1)
        c2.close.assert_not_called()                       # borrowed conn left open

    def test_seasoning_samples_pool(self):
        random.seed(7)
        with patch.object(lx, "ferengi_rule", return_value=(48, "smile")):
            out = lx.seasoning("Operations", "db")
        self.assertIn("#48", out)
        self.assertEqual(sum(1 for t in lx.POOL if t in out), lx.SAMPLE_PER_ARTICLE)

    def test_all_entries_cover_pool(self):
        texts = [t for _, t in lx.all_entries()]
        self.assertEqual(set(texts), set(lx.POOL))
        self.assertEqual(len({n for n, _ in lx.all_entries()}), len(lx.all_entries()))


class TestIntegration(unittest.TestCase):
    def test_voice_layer_uses_seasoning(self):
        self.assertIn("nova_lexicon", (SCRIPTS / "nova_voice.py").read_text())
        self.assertIn("public.ferengi_rules", SRC)
        self.assertIn("dbname=nova_ops", lx.DSN)

    def test_horror_shelf_in_pool(self):
        for t in lx.HORROR_POOL:
            self.assertIn(t, lx.POOL)
        self.assertTrue(lx.FLAVOR_SECTIONS >= {"operations", "essays", "after-dark"})


class TestFunctional(unittest.TestCase):
    def test_demo_selftest_passes_with_mocked_db(self):
        with patch.object(lx, "ferengi_rule", return_value=(48, "smile")), patch("builtins.print") as p:
            lx._demo()
        self.assertIn("PASSED", p.call_args_list[0][0][0])

    def test_cli_prints_rule_or_fallback(self):
        with patch("psycopg2.connect") as pc, patch.object(sys, "argv", ["x", "database"]), patch("builtins.print") as p:
            pc.return_value, _ = _conn()
            exec(compile(SRC, str(SCRIPT), "exec"), {"__name__": "__main__"})
        self.assertEqual(p.call_args[0][0], "Rule #48: The bigger the smile, the sharper the knife.")
        with patch("psycopg2.connect", side_effect=RuntimeError("down")), patch.object(sys, "argv", ["x"]), \
             patch("builtins.print") as p:
            exec(compile(SRC, str(SCRIPT), "exec"), {"__name__": "__main__"})
        self.assertEqual(p.call_args[0][0], "no rule available")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_lexicon"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
