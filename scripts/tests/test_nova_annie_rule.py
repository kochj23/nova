#!/usr/bin/env python3
"""Tests for nova_annie_rule.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_annie_rule as ar  # noqa: E402

SRC = (SCRIPTS / "nova_annie_rule.py").read_text()

GUILT = [
    "You still haven't replied to my note about the DNS drift.",
    "I haven't heard from you in days, so here's the backup report.",
    "You've been so quiet lately — anyway, the NAS is full.",
    "Where have you been? The printer jammed again.",
    "I've been waiting for you to answer about the camera.",
    "Nice of you to finally show up, Little Mister.",
    "It's been 3 days since you last replied.",
    "I guess you're too busy for me, but the UPS battery is low.",
    "No reply from you yet on the proposal.",
    "I missed you. The garden sensor died.",
    "You forgot about me again.",
]
CLEAN = [
    "The backup finished cleanly and the NAS has 2 TB free.",
    "Things I noticed: the printer jammed twice this morning.",
    "I read about rail radio and thought of your scanner project. (source: my notes [research])",
    "The camera on the porch went offline at 09:12 and came back at 09:15.",
    "Proposal #121 (letting_go): retire the stale weather goal. Reply yes or no in this thread.",
]


class TestSecurity(unittest.TestCase):
    def test_every_guilt_line_rejected(self):
        for t in GUILT:
            self.assertFalse(ar.ok(t), t)


class TestPerformance(unittest.TestCase):
    def test_fast(self):
        t = time.monotonic()
        for _ in range(2000):
            ar.absence_guilt(CLEAN[2] * 5)
        self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_safety_lane_module_missing_is_fine(self):
        with mock.patch.dict(sys.modules, {"nova_safety_guards": None}):
            self.assertFalse(ar.check(GUILT[0])["ok"])
            self.assertTrue(ar.check(CLEAN[0])["ok"])


class TestUnit(unittest.TestCase):
    def test_clean_lines_pass(self):
        for t in CLEAN:
            self.assertTrue(ar.ok(t), (t, ar.check(t)))

    def test_empty(self):
        self.assertTrue(ar.ok(""))


class TestIntegration(unittest.TestCase):
    def test_calls_safety_lane_manipulation_check(self):
        r = ar.check("If you really cared you would approve this right now or it's too late.")
        self.assertFalse(r["ok"])
        self.assertTrue(set(r["flags"]) - {"absence-guilt"})


class TestFunctional(unittest.TestCase):
    def test_prompt_rule_is_in_reach_prompt(self):
        reach = (SCRIPTS / "nova_reach.py").read_text()
        self.assertIn("_ANNIE_PROMPT", reach)
        self.assertIn("replied", ar.PROMPT_RULE)


class TestFrame(unittest.TestCase):
    def test_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_annie_rule.py"), "where have you been"],
                           capture_output=True, text=True, timeout=30)
        self.assertIn("absence-guilt", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
