#!/usr/bin/env python3
"""Tests for nova_memory_quality_filter.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_memory_quality_filter.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nmqf", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qf = _load()
GOOD = "This is a proper memory entry about how mushrooms grow in forest environments with adequate moisture and shade."


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_no_io(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        for mod in ("urllib", "requests", "psycopg2", "subprocess", "socket"):
            self.assertNotIn(f"import {mod}", SRC)

    def test_pathological_input_is_bounded(self):
        # a regex-DoS shaped payload must still return quickly
        t0 = time.perf_counter()
        qf.passes_quality("[" * 5000 + "a" * 5000)
        qf.passes_quality("ab" * 5000)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestPerformance(unittest.TestCase):
    def test_10k_entries_batch(self):
        entries = [{"text": GOOD + f" #{i}", "source": "s"} if i % 2 else "hi" for i in range(10_000)]
        t0 = time.perf_counter()
        passed, rejected = qf.filter_batch(entries)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual((len(passed), len(rejected)), (5_000, 5_000))


class TestRetry(unittest.TestCase):
    def test_pure_module_has_no_external_calls(self):
        # RETRY GAP: none applicable — the filter is pure CPU; garbage input fails closed (reject), never raises
        for junk in (None, "", "   ", "\x00" * 80):
            ok, reason = qf.passes_quality(junk)
            self.assertFalse(ok)
            self.assertTrue(reason)
        self.assertEqual(qf.classify_quality(None), ("reject", "empty"))


class TestUnit(unittest.TestCase):
    def test_each_rule(self):
        cases = {
            "hi": "too_short",
            "[Psilocybin mushroom chemistry]\nChemistry and properties": "bracket_header_only",
            "== Cast ==\nSome actor name goes here for this film role ok": "wiki_markup_fragment",
            "Mark Anthony. Mark Anthony. Mark Anthony. Mark Anthony. Mark Anthony.": "repetitive_loop",
            "[a] [b] [c] [d] [e] and some words but not that many really here": "bracket_list_no_context",
            "1234567890 !!!! ---- ==== 1234567890 ..... 99999 ###### $$$$ ab": "low_info_density",
        }
        for text, want in cases.items():
            ok, reason = qf.passes_quality(text)
            self.assertFalse(ok, text)
            self.assertTrue(reason.startswith(want), (text, reason))
        self.assertEqual(qf.passes_quality(GOOD), (True, "ok"))

    def test_single_phrase(self):
        self.assertEqual(qf.passes_quality("Supercalifragilisticexpialidocious_is_a_word ok sure")[1], "single_phrase")

    def test_classify_reference_routing(self):
        v, r = qf.classify_quality("== Further reading ==\nAllison, Graham (1999). Essence of Decision. ISBN 978-0-321-01349-1.")
        self.assertEqual(v, "reference"); self.assertIn("further reading", r)
        self.assertEqual(qf.classify_quality("== Notes ==")[0], "reject")      # too short even as reference
        self.assertEqual(qf.classify_quality(GOOD), ("allow", "ok"))


class TestIntegration(unittest.TestCase):
    def test_classify_agrees_with_passes_quality_off_reference(self):
        for text in (GOOD, "hi", "Mark Anthony. Mark Anthony. Mark Anthony. Mark Anthony. Mark Anthony."):
            ok, reason = qf.passes_quality(text)
            v, r = qf.classify_quality(text)
            self.assertEqual(v == "allow", ok)
            self.assertEqual(r, reason)

    def test_filter_batch_accepts_dicts_and_strings(self):
        passed, rejected = qf.filter_batch([{"text": GOOD}, GOOD, {"text": ""}])
        self.assertEqual(len(passed), 2)
        self.assertEqual(rejected[0]["reason"], "empty")


class TestFunctional(unittest.TestCase):
    def test_memory_write_path_routing(self):
        batch = [GOOD, "== See also ==\nGerman idealism\nNeocriticism\nNorth American Kant Society\nList of publications", "hi"]
        self.assertEqual([qf.classify_quality(t)[0] for t in batch], ["allow", "reference", "reject"])

    def test_error_path_non_dict_objects(self):
        passed, rejected = qf.filter_batch([12345, None])
        self.assertEqual(passed, [])
        self.assertEqual(len(rejected), 2)


class TestFrame(unittest.TestCase):
    def test_selftest_block_all_pass(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PASS", r.stdout)
        self.assertNotIn("FAIL", r.stdout)

    def test_import_is_silent(self):
        code = "import sys; sys.path.insert(0, sys.argv[1]); import nova_memory_quality_filter"
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
