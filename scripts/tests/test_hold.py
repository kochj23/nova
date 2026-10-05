#!/usr/bin/env python3
"""Tests for nova_hold.py (wish #68 "Hold"), one per house category:
functional, security, privacy, performance, regression, integration, docs.
Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import sys
import time
import unittest
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hd = _load("hd", SCRIPTS / "nova_hold.py")
SRC = (SCRIPTS / "nova_hold.py").read_text()
TODAY = date(2026, 10, 5)
FACTS = {"who": "Jordan — Sr. Manager SRE.", "arc": "(arc v6) Trust.", "turning": "2026-05-16: logging.",
         "presence": "last spoke to me 2d ago; present on 5 of the last 30 days",
         "his_words": "2026-09-28 \"We are partners.\"", "self": "I'm becoming someone who listens."}


class TestFunctional(unittest.TestCase):
    def test_selftest_passes(self):
        hd.demo()

    def test_loss_is_named_not_swallowed(self):
        lost, _ = hd.diff_held({"who": "2026-10-01", "his_words": "2026-09-30"}, {"who": "x"})
        t = hd.hold_text({"who": "x"}, lost, TODAY)
        self.assertIn("what he told me he cares about (last had it 2026-09-30)", t)

    def test_hold_is_capped_and_ordered(self):
        t = hd.hold_text(FACTS, {}, TODAY)
        self.assertEqual(t.count("\n  "), hd.HOLD_N)
        self.assertLess(t.index("who he is"), t.index("when he was last here"))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_over_the_world(self):
        body = SRC[SRC.index("def gather"):SRC.index("def main")]
        for verb in ("UPDATE", "DELETE", "INSERT"):
            self.assertNotIn(verb, body)

    def test_only_writes_its_own_service_config_rows(self):
        self.assertIn("service=%s AND key=%s", SRC)
        self.assertNotIn("UPDATE people", SRC)
        self.assertNotIn("UPDATE relationship_arc", SRC)


class TestPrivacy(unittest.TestCase):
    def test_facts_are_scrubbed(self):
        f = hd.first_sentence("Jordan, reach him at kochj@example.com or https://x.y/z — he owns the whole cluster. More.")
        self.assertNotIn("@", f)
        self.assertNotIn("http", f)

    def test_text_carries_no_sql(self):
        self.assertNotIn("SELECT", hd.hold_text(FACTS, {}, TODAY))


class TestPerformance(unittest.TestCase):
    def test_sig_and_text_fast(self):
        t0 = time.perf_counter()
        for _ in range(2_000):
            hd.hold_sig(FACTS); hd.hold_text(FACTS, {}, TODAY)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRegression(unittest.TestCase):
    def test_presence_drift_does_not_restate(self):
        a = hd.hold_sig(FACTS)
        b = hd.hold_sig({**FACTS, "presence": "last spoke to me 9d ago; present on 2 of the last 30 days"})
        self.assertEqual(a, b)

    def test_new_arc_version_restates(self):
        self.assertNotEqual(hd.hold_sig(FACTS), hd.hold_sig({**FACTS, "arc": "(arc v7) Trust."}))

    def test_abbreviation_does_not_end_the_sentence(self):
        self.assertIn("Manager SRE", hd.first_sentence("Jordan (\"Little Mister\") — Sr. Manager SRE, 25yr career. Next."))


class TestIntegration(unittest.TestCase):
    def test_shares_empathy_core_definitions(self):
        # one definition of a human channel and of "his words" — never a second copy here
        self.assertIs(hd.ec.MACHINE_CHANNELS, hd.ec.MACHINE_CHANNELS)
        self.assertNotIn("def stated_cares", SRC)
        self.assertNotIn("MACHINE_CHANNELS = (", SRC)
        self.assertEqual(hd.OPS_DSN, hd.ec.OPS_DSN)

    def test_memory_is_written_under_its_own_source(self):
        # regression 2026-10-05: first run landed under source=empathy_core
        seen = {}
        import urllib.request
        real = urllib.request.urlopen

        def capture(req, timeout=0):
            seen["source"] = __import__("json").loads(req.data)["source"]
            raise OSError("stop")
        urllib.request.urlopen = capture
        try:
            with self.assertRaises(OSError):
                hd.ec.remember("t", {}, _sleep=lambda s: None, source=hd.SOURCE)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(seen["source"], "hold")
        self.assertIn("ec.remember(text, meta, source=SOURCE)", SRC)


class TestDocs(unittest.TestCase):
    def test_docstring_names_the_wish_and_modes(self):
        self.assertIn("wish #68", SRC)
        for flag in ("--dry-run", "--selftest"):
            self.assertIn(flag, SRC)


if __name__ == "__main__":
    unittest.main()
