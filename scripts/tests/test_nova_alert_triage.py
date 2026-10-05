#!/usr/bin/env python3
"""Tests for the evidence stage of nova_alert_triage.py (apply_evidence + annotation shape).
Pure functions only — triage() itself needs PG and an LLM and is exercised by the notifier.
Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tri", SCRIPTS / "nova_alert_triage.py")
tri = importlib.util.module_from_spec(spec); spec.loader.exec_module(tri)
SRC = (SCRIPTS / "nova_alert_triage.py").read_text()
FAULT = {"verdict": "detector_fault", "note": "queried name ends in .net"}


class TestApplyEvidence(unittest.TestCase):
    def test_fault_on_warning_suppresses(self):
        v, conf, dec, reason = tri.apply_evidence(FAULT, hard=False, level="warning")
        self.assertEqual((v, dec), ("detector_fault", "suppress"))
        self.assertGreaterEqual(conf, 0.9)
        self.assertIn(".net", reason)

    def test_safety_contract_hard_and_critical_still_page(self):
        self.assertIsNone(tri.apply_evidence(FAULT, hard=True, level="warning"))
        self.assertIsNone(tri.apply_evidence(FAULT, hard=False, level="critical"))

    def test_supported_or_missing_evidence_changes_nothing(self):
        self.assertIsNone(tri.apply_evidence({"verdict": "supported"}, False, "warning"))
        self.assertIsNone(tri.apply_evidence(None, False, "warning"))
        self.assertIsNone(tri.apply_evidence("junk", False, "warning"))

    def test_verdict_registered_and_prompt_carries_evidence(self):
        self.assertIn("detector_fault", tri._VERDICTS)
        self.assertIn("EVIDENCE:", SRC)
        self.assertIn('"next_action"', SRC)
        self.assertIn("🛠 Do:", SRC)

    def test_evidence_runs_before_the_llm(self):
        self.assertLess(SRC.index("nova_evidence_check.check("), SRC.index("raw = llm("))


if __name__ == "__main__":
    unittest.main()
