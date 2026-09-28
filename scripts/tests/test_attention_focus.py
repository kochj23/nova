#!/usr/bin/env python3
"""Tests for nova_attention_focus.py (wish #36 "Attention Focus"), one per house category:
functional, security, privacy, performance, regression, integration, docs.
Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import time
import unittest
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


af = _load("af", SCRIPTS / "nova_attention_focus.py")
SRC = (SCRIPTS / "nova_attention_focus.py").read_text()


class TestFunctional(unittest.TestCase):
    def test_selftest_passes(self):
        af.demo()  # raises on any failed assertion

    def test_live_critical_outranks_neglected_goal(self):
        items = [{"key": "goal:1", "label": "g", "score": af.score_goal(200, 7)},
                 {"key": "incident:1", "label": "i", "score": af.score_incident("critical", 0, False)}]
        self.assertEqual(af.rank_focus(items, 1)[0]["key"], "incident:1")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_over_the_world(self):
        # the only writes are its own high-water row and the memory POST
        body = SRC[SRC.index("def gather"):SRC.index("def main")]
        for verb in ("UPDATE", "DELETE", "INSERT", "acked_at ="):
            self.assertNotIn(verb, body)


class TestPrivacy(unittest.TestCase):
    def test_focus_text_carries_no_sql_or_emails(self):
        focus = [{"key": "incident:1", "label": "open critical on nova-core: thing (1d, un-acked)"}]
        hold = [{"topic": "horology", "returns": 22}]
        t = af.focus_text(focus, hold, date(2026, 9, 28))
        self.assertNotIn("SELECT", t)
        self.assertNotIn("@", t)


class TestPerformance(unittest.TestCase):
    def test_rank_and_hold_fast_on_10k(self):
        items = [{"key": f"k{i}", "label": "x", "score": (i % 100) / 100} for i in range(10_000)]
        pre = [{"key": f"p{i}", "topic": "t", "returns": i % 30, "days_since": i % 20} for i in range(10_000)]
        t0 = time.perf_counter()
        f = af.rank_focus(items)
        af.hold_set(pre, {x["key"] for x in f})
        self.assertLess(time.perf_counter() - t0, 0.2)


class TestRegression(unittest.TestCase):
    def test_signature_stable_across_order(self):
        a = [{"key": "x"}, {"key": "y"}]
        self.assertEqual(af.focus_sig(a), af.focus_sig(list(reversed(a))))

    def test_resurface_gate(self):
        today = date(2026, 9, 28)
        seen = {"s": "2026-09-27"}
        self.assertFalse(af._fresh(seen, "s", today))
        self.assertTrue(af._fresh({"s": "2026-09-20"}, "s", today))
        self.assertTrue(af._fresh({}, "s", today))


class TestIntegration(unittest.TestCase):
    def test_remember_retries_then_raises(self):
        calls = []
        orig = af.json.dumps
        import urllib.request
        real = urllib.request.urlopen

        def boom(*a, **k):
            calls.append(1); raise OSError("down")
        urllib.request.urlopen = boom
        try:
            with self.assertRaises(OSError):
                af.remember("t", {}, _sleep=lambda s: None)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(len(calls), 3)
        self.assertIs(af.json.dumps, orig)


class TestDocs(unittest.TestCase):
    def test_docstring_names_the_wish_and_modes(self):
        self.assertIn("wish #36", SRC)
        for flag in ("--dry-run", "--selftest"):
            self.assertIn(flag, SRC)


if __name__ == "__main__":
    unittest.main()
