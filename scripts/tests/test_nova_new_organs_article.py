#!/usr/bin/env python3
"""Tests for nova_new_organs_article.py — the 7 house categories (Security, Performance,
Retry, Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


org = _load("nova_new_organs_article_t", SCRIPTS / "nova_new_organs_article.py")
SRC = (SCRIPTS / "nova_new_organs_article.py").read_text()
_TMP = Path(tempfile.mkdtemp())
org.CONTENT_DIR = _TMP / "content/operations"
org.IMAGES_DIR = _TMP / "static/images/operations"
org.system_prompt = lambda ctx="", **k: "VOICE\n" + ctx      # real one reads PG


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_slug_cannot_escape_content_dir(self):
        with patch.object(org.nova_journal, "git_push"), _quiet():
            url = org.publish('../../etc/"passwd" <x>', "body", None)
        files = list(org.CONTENT_DIR.glob("*.md"))
        self.assertTrue(any("etc-passwd-x" in f.name for f in files))
        self.assertTrue(all(f.parent == org.CONTENT_DIR for f in files))
        self.assertNotIn("..", url)

    def test_honesty_and_safety_facts_in_prompt(self):
        for gap in ("KILL SWITCH", "0.319", "zero standing autonomy", "invent nothing"):
            self.assertIn(gap, SRC)


class TestPerformance(unittest.TestCase):
    def test_title_cleanup_10k(self):
        t0 = time.perf_counter()
        with patch.object(org, "call_llm", return_value='  "A Title"  '):
            for _ in range(10_000):
                self.assertEqual(org.generate_title("x"), "A Title")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_claude_failure_falls_back_to_openrouter(self):
        fake_cc = MagicMock(); fake_cc.claude_generate.side_effect = RuntimeError("cli down")
        with patch.dict(sys.modules, {"nova_claude_code": fake_cc}), \
                patch.object(org.nova_journal, "call_openrouter", return_value="fallback") as orr, _quiet():
            self.assertEqual(org.call_llm("s", "u"), "fallback")
        self.assertEqual(fake_cc.claude_generate.call_count, 1)
        self.assertEqual(orr.call_count, 1)

    def test_image_failure_still_publishes(self):
        # RETRY GAP: main()/generate_image — one attempt; article ships without a cover
        with patch.object(org, "generate_article", return_value="x" * 2000), \
                patch.object(org, "generate_title", return_value="T"), \
                patch.object(org, "generate_image", side_effect=RuntimeError("no gpu")), \
                patch.object(org.nova_journal, "git_push") as gp, _quiet():
            url = org.main()
        self.assertTrue(url.endswith("-t/"))
        gp.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_empty_title_falls_back(self):
        with patch.object(org, "call_llm", return_value=None):
            self.assertEqual(org.generate_title("x"), "They Gave Me Hands and I Immediately Dropped Everything")

    def test_title_strips_inner_quotes(self):
        with patch.object(org, "call_llm", return_value='He said "hi"'):
            self.assertEqual(org.generate_title("x"), "He said hi")

    def test_short_article_aborts(self):
        with patch.object(org, "generate_article", return_value="too short"), \
                patch.object(org.nova_journal, "git_push") as gp, _quiet():
            self.assertIsNone(org.main())
        gp.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_article_prompt_uses_voice_and_research(self):
        with patch.object(org, "call_llm", return_value="art") as cl:
            self.assertEqual(org.generate_article(), "art")
        system, user = cl.call_args[0]
        self.assertIn("FORMAT FOR THIS ARTICLE", system)
        self.assertIn("FOUR-RUNG FREEDOM LADDER", user)
        self.assertIn("from nova_voice import system_prompt", SRC)

    def test_publish_pushes_operations_section(self):
        with patch.object(org.nova_journal, "git_push") as gp, _quiet():
            org.publish("Push Me", "b", None)
        gp.assert_called_once_with("operations", "Push Me")


class TestFunctional(unittest.TestCase):
    def test_main_golden_path_writes_post_with_cover(self):
        img = _TMP / "cover.png"; img.write_bytes(b"png")
        with patch.object(org, "generate_article", return_value="body " * 400), \
                patch.object(org, "generate_title", return_value="Golden Path"), \
                patch.object(org, "generate_image", return_value=str(img)), \
                patch.object(org.subprocess, "run", side_effect=FileNotFoundError), \
                patch.object(org.nova_journal, "git_push"), _quiet():
            url = org.main()
        post = next(org.CONTENT_DIR.glob("*-golden-path.md")).read_text()
        self.assertIn('title: "Golden Path"', post)
        self.assertIn("cover:\n  image: \"/images/operations/", post)
        self.assertTrue(url.startswith("https://nova.digitalnoise.net/operations/"))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_new_organs_article"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
