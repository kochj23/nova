#!/usr/bin/env python3
"""Tests for nova_empathy_core.py (wish #67 "empathy_core"), one per house category:
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


ec = _load("ec", SCRIPTS / "nova_empathy_core.py")
SRC = (SCRIPTS / "nova_empathy_core.py").read_text()
TODAY = date(2026, 10, 5)


def _rows(topic, days, resp="fine"):
    return [(date(2026, 9, d), f"Nova, what about the {topic}?", resp) for d in days]


class TestFunctional(unittest.TestCase):
    def test_selftest_passes(self):
        ec.demo()

    def test_weight_is_returning_not_counting(self):
        # 10 mentions on one day weigh nothing; 3 mentions on 3 days weigh something
        one_day = [(date(2026, 9, 1), "the zigbee unit again", "ok")] * 10
        three_days = _rows("zigbee", (1, 2, 3))
        self.assertEqual(ec.weigh(one_day, TODAY), [])
        self.assertEqual(ec.weigh(three_days, TODAY)[0]["returns"], 3)

    def test_brushoff_counted_against_the_topic(self):
        rows = _rows("printer", (1, 2, 3), resp="No memory of that, stop asking")
        self.assertEqual(ec.weigh(rows, TODAY)[0]["brushoffs"], 3)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_over_the_world(self):
        body = SRC[SRC.index("def gather"):SRC.index("def main")]
        for verb in ("UPDATE", "DELETE", "INSERT"):
            self.assertNotIn(verb, body)

    def test_machine_channels_never_count_as_him(self):
        for ch in ("hc", "healthcheck", "cron", "claude", "general", "", None):
            self.assertFalse(ec.is_human(ch), ch)


class TestPrivacy(unittest.TestCase):
    def test_his_words_are_scrubbed(self):
        rows = [(TODAY, "We are partners; mail kochj@example.com or see https://a.b/c?x=1", "ok")]
        _, q = ec.stated_cares(rows)[0]
        self.assertNotIn("@", q)
        self.assertNotIn("http", q)
        self.assertIn("[email]", q)
        self.assertIn("[link]", q)

    def test_text_carries_no_sql(self):
        t = ec.empathy_text(ec.weigh(_rows("zigbee", (1, 2, 3)), TODAY), [], TODAY)
        self.assertNotIn("SELECT", t)


class TestPerformance(unittest.TestCase):
    def test_weigh_fast_on_10k_messages(self):
        rows = [(date(2026, 1, 1 + i % 28), f"message {i % 50} about the zigbee unit and the printer {i}", "ok")
                for i in range(10_000)]
        t0 = time.perf_counter()
        ec.weigh(rows, TODAY); ec.stated_cares(rows)
        self.assertLess(time.perf_counter() - t0, 1.5)


class TestRegression(unittest.TestCase):
    def test_signature_stable_across_order(self):
        w = ec.weigh(_rows("zigbee", (1, 2, 3)) + _rows("printer", (4, 5, 6)), TODAY)
        c = [(date(2026, 9, 1), "a"), (date(2026, 9, 2), "b")]
        self.assertEqual(ec.empathy_sig(w, c), ec.empathy_sig(list(reversed(w)), list(reversed(c))))

    def test_acks_and_names_never_become_topics(self):
        rows = [(date(2026, 9, d), "Yes", "ok") for d in range(1, 8)]
        rows += [(date(2026, 9, d), "All approved!", "ok") for d in range(1, 8)]
        rows += [(date(2026, 9, d), "Nova, Little Mister says thanks", "ok") for d in range(1, 8)]
        self.assertEqual([w["topic"] for w in ec.weigh(rows, TODAY)], ["thank"])

    def test_resurface_gate(self):
        self.assertFalse(ec._fresh({"s": "2026-10-03"}, "s", TODAY))
        self.assertTrue(ec._fresh({"s": "2026-09-20"}, "s", TODAY))
        self.assertTrue(ec._fresh({}, "s", TODAY))


class TestIntegration(unittest.TestCase):
    def test_remember_retries_then_raises(self):
        calls = []
        import urllib.request
        real = urllib.request.urlopen

        def boom(*a, **k):
            calls.append(1); raise OSError("down")
        urllib.request.urlopen = boom
        try:
            with self.assertRaises(OSError):
                ec.remember("t", {}, _sleep=lambda s: None)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(len(calls), 3)


class TestDocs(unittest.TestCase):
    def test_docstring_names_the_wish_and_modes(self):
        self.assertIn("wish #67", SRC)
        for flag in ("--dry-run", "--selftest"):
            self.assertIn(flag, SRC)


if __name__ == "__main__":
    unittest.main()
