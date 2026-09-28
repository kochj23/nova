#!/usr/bin/env python3
"""Tests for nova_proactive_digest.py silence gate + feeds (2026-09-28 fix).
Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
SRC = (SCRIPTS / "nova_proactive_digest.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("pd", SCRIPTS / "nova_proactive_digest.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


class TestSilenceGate(unittest.TestCase):
    def test_bold_and_dressed_nothing_is_silence(self):          # regression: 5 mornings of "**Nothing**"
        pd = _load()
        for y in ("NOTHING", "**NOTHING**", "**Nothing.**", '"Nothing"', "_nothing_", "Nothing!\nmore"):
            self.assertTrue(pd.is_silence(y), y)

    def test_real_note_is_not_silence(self):                     # functional
        pd = _load()
        for n in ("Here's what I noticed", "**Nothing is wrong with the NAS**, but", "Nothing much, except the probe"):
            self.assertFalse(pd.is_silence(n), n)


class TestFeeds(unittest.TestCase):
    def test_reads_live_incident_table(self):                    # integration: legacy public.incidents had no opened_at
        self.assertIn("FROM telemetry.incidents", SRC)
        self.assertNotIn('FROM incidents "', SRC)

    def test_organ_noticings_are_candidates(self):
        for src in ("attention_focus", "pattern_sense", "human_insight"):
            self.assertIn(f'"{src}"', SRC)

    def test_no_stale_mini_ip_and_no_secrets(self):              # security / regression
        self.assertNotIn("192.168.1.251", SRC)
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_docstring_keeps_silence_as_valid(self):             # docs: the contract did not change
        self.assertIn("silence is a valid outcome", SRC)


if __name__ == "__main__":
    unittest.main()
