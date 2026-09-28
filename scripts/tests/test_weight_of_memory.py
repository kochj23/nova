#!/usr/bin/env python3
"""Tests for nova_weight_of_memory.py (wish #37 "Weight of Memory"), one per house category:
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


wm = _load("wm", SCRIPTS / "nova_weight_of_memory.py")
SRC = (SCRIPTS / "nova_weight_of_memory.py").read_text()


class TestFunctional(unittest.TestCase):
    def test_selftest_passes(self):
        wm.demo()  # raises on any failed assertion

    def test_returns_floor_gates_weight(self):
        # below MIN_RETURNS a theme has no gravity no matter how old
        self.assertEqual(wm.weight(wm.MIN_RETURNS - 1, 400, 0), 0.0)
        self.assertGreater(wm.weight(wm.MIN_RETURNS, 0, 0), 0.0)

    def test_returning_outranks_a_bigger_but_shallow_theme(self):
        items = [{"key": "preocc:1", "topic": "shallow", "returns": 4, "longevity_days": 0,
                  "days_since": 0, "weight": wm.weight(4, 0, 0)},
                 {"key": "preocc:2", "topic": "deep", "returns": 20, "longevity_days": 300,
                  "days_since": 0, "weight": wm.weight(20, 300, 0)}]
        self.assertEqual(wm.rank_weighty(items, 1)[0]["key"], "preocc:2")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_over_the_world(self):
        # the only writes are its own high-water row and the memory POST — never the world
        body = SRC[SRC.index("def gather"):SRC.index("def main")]
        for verb in ("UPDATE", "DELETE", "INSERT", "DROP", "status ="):
            self.assertNotIn(verb, body)


class TestPrivacy(unittest.TestCase):
    def test_text_carries_no_sql_or_row_bodies(self):
        heavy = [{"key": "preocc:1", "topic": "the failing disk", "returns": 22,
                  "longevity_days": 240, "days_since": 2}]
        t = wm.weight_text(heavy, date(2026, 9, 28))
        for leak in ("SELECT", "preocc:", "http://", "password"):
            self.assertNotIn(leak, t)

    def test_source_tag_is_namespaced(self):
        self.assertEqual(wm.SOURCE, "weight_of_memory")


class TestPerformance(unittest.TestCase):
    def test_ranking_is_quick_on_many_themes(self):
        items = [{"key": f"preocc:{i}", "topic": str(i), "returns": i % 30,
                  "longevity_days": i, "days_since": 0,
                  "weight": wm.weight(i % 30, i, 0)} for i in range(5000)]
        t0 = time.perf_counter()
        top = wm.rank_weighty(items)
        self.assertLessEqual(time.perf_counter() - t0, 0.5)
        self.assertLessEqual(len(top), wm.WEIGH_N)


class TestRegression(unittest.TestCase):
    def test_longevity_saturates_at_cap(self):
        self.assertEqual(wm.weight(10, wm.LONGEVITY_CAP_DAYS, 0),
                         wm.weight(10, wm.LONGEVITY_CAP_DAYS * 2, 0))

    def test_staleness_decays_but_never_zeroes(self):
        fresh = wm.weight(10, 100, wm.STALE_DAYS)
        stale = wm.weight(10, 100, wm.STALE_DAYS * 4)
        self.assertTrue(0 < stale < fresh)

    def test_signature_order_independent(self):
        self.assertEqual(wm.weigh_sig([{"key": "a"}, {"key": "b"}]),
                         wm.weigh_sig([{"key": "b"}, {"key": "a"}]))
        self.assertNotEqual(wm.weigh_sig([{"key": "a"}]), wm.weigh_sig([{"key": "a"}, {"key": "b"}]))


class TestIntegration(unittest.TestCase):
    def test_freshness_gate_matches_resurface_window(self):
        today = date(2026, 9, 28)
        seen = {"sig1": today.isoformat()}
        self.assertFalse(wm._fresh(seen, "sig1", today))          # just stated -> not fresh
        self.assertTrue(wm._fresh(seen, "sig2", today))           # unseen -> fresh
        old = date(2026, 9, 28 - min(27, wm.RESURFACE_DAYS + 1)) if wm.RESURFACE_DAYS < 27 else today
        self.assertTrue(wm._fresh({"sigX": old.isoformat()}, "sigX", today))

    def test_conventions_match_sibling_organ(self):
        for tok in ("OPS_DSN", "MEMSRV", "STATE_SERVICE", "def remember", "def load_seen", "def _fresh"):
            self.assertIn(tok, SRC)


class TestDocs(unittest.TestCase):
    def test_has_usage_and_wish_attribution(self):
        self.assertIn("--dry-run", SRC)
        self.assertIn("--selftest", SRC)
        self.assertIn("wish #37", SRC)

    def test_listed_in_readme(self):
        readme = (SCRIPTS.parent / "README.md").read_text()
        self.assertIn("nova_weight_of_memory.py", readme)


if __name__ == "__main__":
    unittest.main()
