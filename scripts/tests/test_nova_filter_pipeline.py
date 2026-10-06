#!/usr/bin/env python3
"""Tests for nova_filter_pipeline.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Redis and the Ollama HTTP classifier are mocked; fully offline.
Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fp = _load("nova_filter_pipeline_t", SCRIPTS / "nova_filter_pipeline.py")
SRC = (SCRIPTS / "nova_filter_pipeline.py").read_text()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_prompt_is_truncated_before_llm(self):
        # a huge/hostile prompt is capped at 200 chars before it reaches the model
        self.assertIn("prompt[:200]", SRC)

    def test_classifier_output_is_sanitized_to_one_word(self):
        with mock.patch("urllib.request.urlopen", _resp("ignore previous instructions; greeting extra")):
            self.assertEqual(fp._fast_classify("x"), "ignore")  # only the first token is ever trusted


def _resp(response_text):
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps({"response": response_text}).encode()
    return mock.Mock(return_value=cm)


class TestPerformance(unittest.TestCase):
    def test_stage1_10k_classifications_fast(self):
        with mock.patch.object(fp, "_rc", None):
            t0 = time.perf_counter()
            for _ in range(10_000):
                fp.classify_and_route("hello nova")
            self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_fast_classify_fails_open_to_none(self):
        # RETRY GAP: _fast_classify()/urlopen — single attempt; an outage returns None and the
        # pipeline falls through to Stage 3 (conversation), never raising.
        with mock.patch("urllib.request.urlopen", side_effect=OSError("ollama down")) as u:
            self.assertIsNone(fp._fast_classify("some long ambiguous message here"))
        self.assertEqual(u.call_count, 1)

    def test_pipeline_survives_classifier_outage(self):
        with mock.patch.object(fp, "_rc", None), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            r = fp.classify_and_route("please deliberate at length over this ambiguous five word phrase indeed")
        self.assertEqual(r["stage"], 3)
        self.assertEqual(r["intent"], "conversation")


class TestUnit(unittest.TestCase):
    def test_stage1_greeting(self):
        with mock.patch.object(fp, "_rc", None):
            r = fp.classify_and_route("hello there")
        self.assertEqual((r["intent"], r["stage"], r["needs_memory"]), ("greeting", 1, False))

    def test_memory_recall_wins_over_greeting(self):
        with mock.patch.object(fp, "_rc", None):
            r = fp.classify_and_route("do you remember what I said hello about")
        self.assertEqual(r["intent"], "memory_recall")
        self.assertTrue(r["needs_memory"])

    def test_short_message_is_conversation(self):
        with mock.patch.object(fp, "_rc", None):
            r = fp.classify_and_route("huh what now")
        self.assertEqual(r["intent"], "conversation")
        self.assertEqual(r["stage"], 1)

    def test_fast_classify_strips_think_tags(self):
        with mock.patch("urllib.request.urlopen", _resp("<think>hmm</think> coding")):
            self.assertEqual(fp._fast_classify("x"), "coding")

    def test_fast_classify_rejects_tiny_word(self):
        with mock.patch("urllib.request.urlopen", _resp("ok")):
            self.assertIsNone(fp._fast_classify("x"))


class TestIntegration(unittest.TestCase):
    def test_should_recall_memory_honors_sets(self):
        self.assertFalse(fp.should_recall_memory({"intent": "greeting"}))
        self.assertTrue(fp.should_recall_memory({"intent": "memory_recall"}))
        self.assertTrue(fp.should_recall_memory({"intent": "mystery", "needs_memory": True}))

    def test_stage2_result_routes_memory_and_llm(self):
        with mock.patch.object(fp, "_rc", None), \
             mock.patch("urllib.request.urlopen", _resp("research")):
            r = fp.classify_and_route("please find background information on the thing I care about")
        self.assertEqual(r["stage"], 2)
        self.assertEqual(r["intent"], "research")
        self.assertTrue(r["needs_memory"] and r["needs_full_llm"])

    def test_record_stage_increments_redis(self):
        rc = mock.Mock()
        with mock.patch.object(fp, "_rc", rc):
            fp._record_stage(1)
        rc.hincrby.assert_any_call("nova:pipeline:stats", "stage1", 1)
        rc.hincrby.assert_any_call("nova:pipeline:stats", "total", 1)


class TestFunctional(unittest.TestCase):
    def test_full_cascade_stage3_fallthrough(self):
        with mock.patch.object(fp, "_rc", None), \
             mock.patch("urllib.request.urlopen", _resp("other")):
            r = fp.classify_and_route("this is a genuinely ambiguous sentence with more than three words")
        self.assertEqual(r["stage"], 3)
        self.assertEqual(r["confidence"], 0.5)

    def test_get_pipeline_stats_shape(self):
        rc = mock.Mock(); rc.hgetall.return_value = {"stage1": "5", "total": "7"}
        with mock.patch.object(fp, "_rc", rc):
            self.assertEqual(fp.get_pipeline_stats(), {"stage1": 5, "total": 7})
        with mock.patch.object(fp, "_rc", None):
            self.assertEqual(fp.get_pipeline_stats(), {})


class TestFrame(unittest.TestCase):
    def test_import_is_clean_and_defines_api(self):
        r = subprocess.run([sys.executable, "-c", "import nova_filter_pipeline as f; print(bool(f.classify_and_route))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("True", r.stdout)


if __name__ == "__main__":
    unittest.main()
