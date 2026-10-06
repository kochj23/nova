#!/usr/bin/env python3
"""Tests for nova_journal_guard.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import ast
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_journal_guard.py"
SRC = SCRIPT.read_text()

import nova_journal_guard as jg  # noqa: E402  (import-clean: pure regex module)

REAL = "The scheduler vanished on Tuesday and nobody noticed until the dashboards went quiet. " * 12


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_system_prompt_leakage_is_blocked(self):
        for opener in ("[System Instructions] be nice. ", "As an AI language model I think. ",
                       "I don't have real-time access to that. "):
            ok, why = jg.is_publishable("Real Title", opener + REAL)
            self.assertFalse(ok, opener)
            self.assertIn("leakage", why)

    def test_hostile_input_never_raises(self):
        for t, b in ((None, None), ("\x00" * 50, "‮" * 500), ("🛡️", "🛡️ " * 200)):
            ok, why = jg.is_publishable(t, b)
            self.assertFalse(ok); self.assertIsInstance(why, str)


class TestPerformance(unittest.TestCase):
    def test_10k_checks_are_bounded(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            jg.is_publishable(f"Title {i}", REAL)
        self.assertLess(time.perf_counter() - t0, 15.0)

    def test_huge_body_only_scans_opening(self):
        t0 = time.perf_counter()
        ok, _ = jg.is_publishable("Big", REAL + ("filler words here " * 200_000))
        self.assertTrue(ok)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_pure_gate_has_no_external_calls(self):
        # RETRY GAP: none applicable — the gate is pure regex; no I/O to retry. Prove it imports only `re`
        tree = ast.parse(SRC)
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(mods, {"re"})


class TestUnit(unittest.TestCase):
    def test_bad_titles(self):
        for t in ("Introduction", "", "TITLE", "🛡️ I'm ready to fetch", "Sure, here it is", "Understood"):
            self.assertFalse(jg.is_publishable(t, REAL)[0], t)

    def test_short_body_rejected_with_count(self):
        ok, why = jg.is_publishable("Fine", "only a few words")
        self.assertFalse(ok); self.assertIn("(4 words)", why)

    def test_meta_opener_rejected(self):
        self.assertFalse(jg.is_publishable("T", "Here's the expanded article. " + REAL)[0])
        self.assertFalse(jg.is_publishable("T", "I can see your article and " + REAL)[0])

    def test_refusal_shape_and_body_markers(self):
        shape = "Little Mister, you asked for a formal essay but this is " + REAL
        self.assertIn("refusal shape", jg.is_publishable("Essay", shape)[1])
        dispatch = "I need the actual article details to write this responsibly. " + REAL
        self.assertFalse(jg.is_publishable("LA County dispatch", dispatch)[0])

    def test_marker_buried_past_opening_is_allowed(self):
        buried = REAL * 2 + " Someone wrote: you handed me a grocery list of Wikipedia excerpts."
        self.assertEqual(jg.is_publishable("Weekly digest", buried), (True, "ok"))

    def test_dramatic_real_opening_is_allowed(self):
        self.assertTrue(jg.is_publishable("Let me walk you through this nightmare", REAL)[0])


class TestIntegration(unittest.TestCase):
    def test_publishers_import_the_single_choke_point(self):
        callers = [p.name for p in SCRIPTS.glob("*.py")
                   if p.name != SCRIPT.name and "is_publishable" in p.read_text(errors="ignore")
                   and "nova_journal_guard" in p.read_text(errors="ignore")]
        for must in ("nova_journal.py", "nova_daily_essay.py"):
            self.assertIn(must, callers)
        for c in callers:
            self.assertNotIn("_REFUSAL_BODY = [", (SCRIPTS / c).read_text(errors="ignore"))   # not re-implemented

    def test_all_patterns_compile(self):
        for rx in jg._REFUSAL_BODY + jg._META + [a for pair in jg._REFUSAL_SHAPE for a in pair]:
            re.compile(rx)


class TestFunctional(unittest.TestCase):
    def test_incident_bodies_blocked_real_article_passes(self):
        incident = ("BREAKING security alert", "I'm ready to fetch the article, but I need the direct URL. " * 10)
        self.assertFalse(jg.is_publishable(*incident)[0])
        self.assertEqual(jg.is_publishable("Burbank Hits 96 Degrees", REAL), (True, "ok"))


class TestFrame(unittest.TestCase):
    def test_selfcheck_exits_zero_and_reports_ok(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "OK")
        self.assertNotIn("LEAKED", r.stdout); self.assertNotIn("FALSE-BLOCK", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_journal_guard"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
