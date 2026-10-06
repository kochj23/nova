#!/usr/bin/env python3
"""Tests for nova_tag_extractor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Ollama is mocked at urlopen. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_tag_extractor.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("tagx", SCRIPTS / "nova_tag_extractor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tx = _load()


class _Resp:
    def __init__(self, text):
        self.text = text

    def read(self):
        return json.dumps({"response": self.text}).encode()


ARTICLE = ("Kubernetes clusters and kubernetes operators: why kubernetes scheduling beats bespoke "
           "scheduling scripts. Operators reconcile state; scheduling decides placement.")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_llm_stays_local_and_names_never_tags(self):
        self.assertTrue(tx.OLLAMA_URL.startswith("http://127.0.0.1"))
        tags = tx.extract_tags("Nova and Jordan", "jordan nova jordan nova " * 20 + ARTICLE, n=6)
        self.assertNotIn("jordan", tags)
        self.assertNotIn("nova", tags)

    def test_markup_and_urls_stripped(self):
        tags = tx._extract_keyword_tags("x", "see https://evil.example/payload <script>alert</script> " * 5, "", 5)
        self.assertFalse(any("http" in t or "<" in t or "evil" in t for t in tags))


class TestPerformance(unittest.TestCase):
    def test_keyword_extraction_on_large_content_fast(self):
        big = " ".join(f"word{i % 300}" for i in range(10_000))
        t0 = time.perf_counter()
        for _ in range(50):
            tx._extract_keyword_tags("t", big, "essays", 5)
        self.assertLess(time.perf_counter() - t0, 2.0)          # only the first 2000 chars are scanned


class TestRetry(unittest.TestCase):
    def test_llm_failure_falls_back_to_seeds(self):
        # RETRY GAP: _extract_llm_tags — one Ollama call; failure returns the category seeds
        with patch.object(tx.urllib.request, "urlopen", side_effect=OSError("ollama down")) as uo:
            self.assertEqual(tx._extract_llm_tags("t", "c", "dreams", 5), ["dream", "memory", "subconscious"])
            self.assertEqual(tx._extract_llm_tags("t", "c", "unknown", 5), ["journal"])
        self.assertEqual(uo.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_seeds_lead_and_n_respected(self):
        tags = tx.extract_tags("Ops", ARTICLE, category="tech-today", n=5)
        self.assertEqual(tags[:3], ["technology", "AI", "infrastructure"])
        self.assertEqual(len(tags), 5)
        self.assertIn("kubernetes", tags)

    def test_empty_input_uses_llm_path(self):
        with patch.object(tx, "_extract_llm_tags", return_value=["x"]) as llm:
            self.assertEqual(tx.extract_tags("", "", "", 5), ["x"])
        llm.assert_called_once()

    def test_llm_output_cleaned(self):
        with patch.object(tx.urllib.request, "urlopen",
                          return_value=_Resp('Sure! ["Machine Learning", "the", "GPU Clusters", ""] done')):
            self.assertEqual(tx._extract_llm_tags("t", "c", "essays", 5), ["machine-learning", "gpu-clusters"])


class TestIntegration(unittest.TestCase):
    def test_llm_request_shape(self):
        seen = {}

        def uo(req, timeout=None):
            seen["url"] = req.full_url; seen["body"] = json.loads(req.data); return _Resp('["a"]')

        with patch.object(tx.urllib.request, "urlopen", side_effect=uo):
            tx._extract_llm_tags("Title", "x" * 2000, "art", 4)
        self.assertEqual(seen["url"], tx.OLLAMA_URL)
        self.assertEqual(seen["body"]["model"], tx.MODEL)
        self.assertIn("Extract exactly 4", seen["body"]["prompt"])
        self.assertLess(len(seen["body"]["prompt"]), 1200)


class TestFunctional(unittest.TestCase):
    def test_journal_post_gets_tags_without_llm(self):
        with patch.object(tx.urllib.request, "urlopen") as uo:
            tags = tx.extract_tags("Scheduling at Scale", ARTICLE, category="opinions")
        uo.assert_not_called()
        self.assertEqual(len(tags), 5)
        self.assertTrue(all(t == t.strip() and t for t in tags))

    def test_sparse_post_falls_to_llm(self):
        with patch.object(tx.urllib.request, "urlopen", return_value=_Resp('["haiku", "autumn", "rain"]')):
            self.assertEqual(tx.extract_tags("Hi", "a b", "", 5), ["haiku", "autumn", "rain"])


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_tag_extractor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
