#!/usr/bin/env python3
"""Tests for nova_monthly_wrap.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
HUGO_ROOT is a tempdir; the LLM, image generation, Hugo publish, git push and Slack are mocked."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_monthly_wrap.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_monthly_wrap_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mw = _load()
for _n in ("call_openrouter", "get_image_prompt", "generate_image", "publish_hugo", "git_push", "notify_slack", "log"):
    setattr(mw, _n, MagicMock())   # module-level stubs: nothing outbound even if a test forgets


def _article(d, section, name, title, body="Body text here"):
    p = d / "content" / section
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_text(f'---\ntitle: "{title}"\ndate: x\n---\n{body}\n')


class _Run:
    def __init__(self, llm="# My Wrap 🎉\n\nbody", img="/tmp/c.webp", pub=True):
        self.llm, self.img, self.pub = llm, img, pub

    def __enter__(self):
        self.root = Path(tempfile.mkdtemp())
        self.st = ExitStack()
        p = lambda n, **kw: self.st.enter_context(patch.object(mw, n, **kw))
        p("HUGO_ROOT", new=self.root)
        self.call = p("call_openrouter", return_value=self.llm)
        p("get_image_prompt", return_value="ip")
        self.gen = p("generate_image", return_value=self.img)
        self.publish = p("publish_hugo", return_value=self.pub)
        self.push = p("git_push")
        self.slack = p("notify_slack")
        self.log = p("log")
        self.voice = self.st.enter_context(patch.object(mw.nova_voice, "system_prompt", return_value="HOUSE"))
        return self

    def __exit__(self, *a):
        self.st.close()
        shutil.rmtree(self.root, ignore_errors=True)


class TestSecurity(unittest.TestCase):
    def test_no_credentials(self):
        self.assertIsNone(re.search(r"(password|api[_-]?key|token|secret)\s*=\s*['\"]", SRC, re.I))

    def test_unknown_section_cannot_path_traverse(self):
        with _Run() as r, patch.object(sys, "argv", ["x", "../../etc"]):
            mw.main()
        r.call.assert_not_called()
        self.assertTrue(any("Unknown section" in c[0][0] for c in r.log.call_args_list))

    def test_title_symbols_stripped(self):
        with _Run(llm="# Wrap <script>alert(1)</script> 🎉\nbody") as r:
            _article(r.root, "rando", "2026-05-01-a.md", "A")
            mw.generate_wrap("rando", mw.SECTIONS["rando"])
        self.assertNotIn("<", r.publish.call_args.kwargs["title"])


class TestPerformance(unittest.TestCase):
    def test_many_articles_read_fast(self):
        with _Run() as r:
            for i in range(500):
                _article(r.root, "art", f"2026-05-{i:04d}.md", f"T{i}", "x" * 2000)
            t0 = time.perf_counter()
            arts = mw.get_may_articles("art")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(arts), 500)
        self.assertTrue(all(len(a["preview"]) <= 600 for a in arts))


class TestRetry(unittest.TestCase):
    def test_llm_failure_skips_publish(self):
        # RETRY GAP: generate_wrap()/call_openrouter — one call; empty -> False, nothing published
        with _Run(llm=None) as r:
            _article(r.root, "art", "2026-05-01.md", "A")
            self.assertFalse(mw.generate_wrap("art", mw.SECTIONS["art"]))
        self.assertEqual(r.call.call_count, 1)
        r.publish.assert_not_called()

    def test_section_exception_does_not_stop_others(self):
        with _Run() as r, patch.object(mw, "generate_wrap", side_effect=[RuntimeError("x"), True]), \
             patch.object(sys, "argv", ["x", "art", "rando"]):
            mw.main()
        r.push.assert_called_once()
        self.assertTrue(any("1/2 sections" in c[0][0] for c in r.log.call_args_list))


class TestUnit(unittest.TestCase):
    def test_get_may_articles_filters_month_and_parses(self):
        with _Run() as r:
            _article(r.root, "art", "2026-05-02-x.md", "May Piece", "hello")
            _article(r.root, "art", "2026-06-01-x.md", "June Piece")
            (r.root / "content/art/2026-05-03-nofm.md").write_text("no frontmatter")
            arts = mw.get_may_articles("art")
        self.assertEqual([a["title"] for a in arts], ["May Piece", "2026-05-03-nofm"])
        self.assertEqual(arts[0]["preview"], "hello")
        self.assertEqual(arts[1]["preview"], "")

    def test_no_articles_returns_false(self):
        with _Run() as r:
            self.assertFalse(mw.generate_wrap("art", mw.SECTIONS["art"]))
        r.call.assert_not_called()

    def test_title_fallback_when_no_heading(self):
        with _Run(llm="plain body") as r:
            _article(r.root, "after-dark", "2026-05-01.md", "A")
            mw.generate_wrap("after-dark", mw.SECTIONS["after-dark"])
        self.assertEqual(r.publish.call_args.kwargs["title"], "Monthly Wrap: After Dark — May 2026")


class TestIntegration(unittest.TestCase):
    def test_voice_routing_house_vs_column(self):
        with _Run() as r:
            _article(r.root, "rando", "2026-05-01.md", "A")
            _article(r.root, "dreams", "2026-05-01.md", "B")
            mw.generate_wrap("rando", mw.SECTIONS["rando"])
            self.assertEqual(r.call.call_args[0][0], "HOUSE")
            mw.generate_wrap("dreams", mw.SECTIONS["dreams"])
            self.assertIn("COLUMN VOICE", r.call.call_args[0][0])
        self.assertEqual(r.voice.call_count, 1)

    def test_helpers_come_from_nova_journal(self):
        self.assertIn("from nova_journal import", SRC)
        self.assertNotIn("def call_openrouter", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_notifies_and_pushes_once(self):
        with _Run() as r, patch.object(sys, "argv", ["x", "art"]):
            _article(r.root, "art", "2026-05-01.md", "Sunset Study")
            mw.main()
        kw = r.publish.call_args.kwargs
        self.assertEqual((kw["section"], kw["emoji"], kw["image_path"]), ("art", "🎨", "/tmp/c.webp"))
        self.assertIn("monthly-wrap", kw["tags"])
        self.assertIn('"Sunset Study"', r.call.call_args[0][1])
        r.slack.assert_called_once()
        r.push.assert_called_once_with("monthly-wrap", "Monthly Wrap — May 2026")

    def test_publish_failure_no_slack(self):
        with _Run(pub=False) as r:
            _article(r.root, "art", "2026-05-01.md", "A")
            self.assertFalse(mw.generate_wrap("art", mw.SECTIONS["art"]))
        r.slack.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_monthly_wrap"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Monthly Wrap Generator", r.stdout)


if __name__ == "__main__":
    unittest.main()
