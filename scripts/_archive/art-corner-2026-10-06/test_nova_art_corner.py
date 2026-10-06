#!/usr/bin/env python3
"""Tests for nova_art_corner.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_art_corner.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_art_corner_t", SCRIPTS / "nova_art_corner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ac = _load()
TMP = Path(tempfile.mkdtemp(prefix="artcorner_test_"))
ac.LOG_FILE = TMP / "art.log"
ac.CONTENT_ART = TMP / "content"
ac.IMAGES_ART = TMP / "images"
ac.HUGO_ROOT = TMP


class _Resp:
    def __init__(self, body): self.body = body
    def read(self): return self.body


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_key_comes_from_keychain(self):
        self.assertIn('"security", "find-generic-password"', SRC)
        with mock.patch.object(ac.subprocess, "run", return_value=mock.Mock(returncode=44, stdout="")):
            with self.assertRaises(RuntimeError):
                ac.get_openrouter_key()

    def test_pii_scrubbed(self):
        out = ac.scrub_pii("mail bob@example.com or nova@digitalnoise.net from " + str(Path.home()) + "/x")
        self.assertNotIn("bob@example.com", out)
        self.assertIn("nova@digitalnoise.net", out)
        self.assertNotIn(str(Path.home()) + "/", out)

    def test_sanitize_applies_privacy_gate_first(self):
        with mock.patch.object(ac.nova_config, "filter_private_memories", side_effect=lambda m: m[:1]) as f:
            out = ac.sanitize_memories([{"text": "a@b.com"}, {"text": "secret"}])
        f.assert_called_once()
        self.assertEqual(out, [{"text": "[redacted]"}])


class TestPerformance(unittest.TestCase):
    def test_scrub_10k(self):
        t0 = time.perf_counter()
        for _ in range(10_000):
            ac.scrub_emails("contact x@y.com now")
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_openrouter_has_no_retry(self):
        # RETRY GAP: call_openrouter — single urlopen; error propagates to main()'s FATAL handler
        calls = []
        def boom(*a, **k):
            calls.append(1); raise OSError("down")
        with mock.patch.object(ac, "get_openrouter_key", return_value="k"), \
                mock.patch("urllib.request.urlopen", boom):
            with self.assertRaises(OSError):
                ac.call_openrouter("s", "u")
        self.assertEqual(len(calls), 1)

    def test_memory_fetch_fails_open(self):
        # RETRY GAP: fetch_random_memories/fetch_themed_memories — single attempt, returns []
        with mock.patch("urllib.request.urlopen", side_effect=OSError("x")):
            self.assertEqual(ac.fetch_random_memories(), [])
            self.assertEqual(ac.fetch_themed_memories("q"), [])

    def test_all_candidates_failing_retries_pipeline_simplified_once(self):
        calls = []
        with mock.patch.object(ac, "fetch_unused", return_value=[{"id": i + 1, "text": "t"} for i in range(5)]), \
                mock.patch.object(ac, "sanitize_memories", side_effect=lambda m: m), \
                mock.patch.object(ac, "generate_candidates", side_effect=lambda p: calls.append(p) or []), \
                mock.patch.object(ac, "notify") as n:
            self.assertFalse(ac.run_pipeline(retry_simplified=True))
        self.assertEqual(len(calls), 1)
        n.assert_called_once()
        calls.clear()
        with mock.patch.object(ac, "fetch_unused", return_value=[{"id": i + 1, "text": "t"} for i in range(5)]), \
                mock.patch.object(ac, "sanitize_memories", side_effect=lambda m: m), \
                mock.patch.object(ac, "synthesize_visual_concept", return_value="c"), \
                mock.patch.object(ac, "write_image_prompt", return_value="p"), \
                mock.patch.object(ac, "generate_title", return_value="T"), \
                mock.patch.object(ac, "generate_candidates", side_effect=lambda p: calls.append(p) or []), \
                mock.patch.object(ac, "notify"):
            self.assertFalse(ac.run_pipeline())
        self.assertEqual(len(calls), 2)  # normal + simplified


class TestUnit(unittest.TestCase):
    def test_extract_memory_text(self):
        self.assertEqual(ac.extract_memory_text("hi"), "hi")
        self.assertEqual(ac.extract_memory_text({"content": "c"}), "c")
        self.assertEqual(len(ac.extract_memory_text({"text": "x" * 900})), 500)
        self.assertIn("k", ac.extract_memory_text({"metadata": {"k": 1}}))
        self.assertEqual(ac.extract_memory_text({}), "")

    def test_styles_cover_week(self):
        self.assertEqual(set(ac.DAILY_STYLES), set(range(7)))
        self.assertEqual(set(ac.DAILY_THEMES), set(range(7)))

    def test_pick_best_candidate(self):
        self.assertIsNone(ac.pick_best_candidate([]))
        a, b = TMP / "a.png", TMP / "b.png"
        a.write_bytes(b"1"); b.write_bytes(b"123456")
        self.assertEqual(ac.pick_best_candidate([a, b]), b)
        self.assertFalse(a.exists())

    def test_generate_title_cleans_quotes(self):
        with mock.patch.object(ac, "call_openrouter", return_value='"Quiet Harbor."'):
            self.assertEqual(ac.generate_title("c", ac.DAILY_STYLES[0]), "Quiet Harbor")


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("from nova_image_utils import generate_image", SRC)
        self.assertIn("from nova_unused_memories import fetch_unused, mark_used", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertNotIn("def generate_image", SRC)

    def test_concept_prompt_chain(self):
        seen = []
        with mock.patch.object(ac, "call_openrouter", side_effect=lambda s, u, max_tokens=0: seen.append(u) or "out"):
            c = ac.synthesize_visual_concept([{"text": "a lighthouse"}], ac.DAILY_STYLES[1])
            ac.write_image_prompt(c, ac.DAILY_STYLES[1])
        self.assertIn("a lighthouse", seen[0])
        self.assertIn(ac.DAILY_STYLES[1]["directive"], seen[1])


class TestFunctional(unittest.TestCase):
    def test_publish_to_hugo_writes_post_and_image(self):
        img = TMP / "src.png"; img.write_bytes(b"png")
        p = ac.publish_to_hugo("My Title", "Statement bob@example.com", ac.DAILY_STYLES[2], "prompt",
                               [{"text": "mem"}], img)
        txt = p.read_text()
        self.assertIn('title: "My Title"', txt)
        self.assertNotIn("bob@example.com", txt)
        self.assertTrue(any(ac.IMAGES_ART.glob("*my-title.png")))

    def test_pipeline_golden_path(self):
        img = TMP / "cand.png"; img.write_bytes(b"x" * 10)
        with mock.patch.object(ac, "fetch_unused", return_value=[{"id": i + 1, "text": "t"} for i in range(5)]), \
                mock.patch.object(ac, "sanitize_memories", side_effect=lambda m: m), \
                mock.patch.object(ac, "call_openrouter", return_value="llm text"), \
                mock.patch.object(ac, "generate_candidates", return_value=[img]), \
                mock.patch.object(ac, "git_push") as gp, \
                mock.patch.object(ac, "notify") as n, \
                mock.patch.object(ac, "mark_used", return_value=5) as mu:
            self.assertTrue(ac.run_pipeline())
        gp.assert_called_once(); n.assert_called_once()
        mu.assert_called_once_with([1, 2, 3, 4, 5])

    def test_too_few_memories_aborts(self):
        with mock.patch.object(ac, "fetch_unused", return_value=[]), \
                mock.patch.object(ac, "sanitize_memories", side_effect=lambda m: m), \
                mock.patch.object(ac, "notify") as n:
            self.assertFalse(ac.run_pipeline())
        self.assertEqual(n.call_args.kwargs["level"], "warning")

    def test_main_exits_1_on_crash(self):
        with mock.patch.object(ac, "run_pipeline", side_effect=RuntimeError("boom")), \
                mock.patch.object(ac, "notify") as n:
            with self.assertRaises(SystemExit) as cm:
                ac.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(n.call_args.kwargs["level"], "critical")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_art_corner"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("starting pipeline", r.stdout)


if __name__ == "__main__":
    unittest.main()
