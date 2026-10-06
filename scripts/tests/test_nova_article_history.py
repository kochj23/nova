#!/usr/bin/env python3
"""Tests for nova_article_history.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_article_history.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ah = _load("ah", SCRIPT)


def _article(d, title, description=None, body="First sentence here. Second one.", prefix=True):
    fm = f'---\ntitle: "{title}"\ndate: {d:%Y-%m-%d}T08:00:00-07:00\n'
    if description:
        fm += f"description: {description}\n"
    name = (f"{d:%Y-%m-%d}-" if prefix else "") + re.sub(r"\W+", "-", title.lower()) + ".md"
    return name, fm + "---\n\n" + body + "\n"


class _Section:
    """A temp Hugo content dir; `ah._content_dir` is pointed at it while the context is open."""
    def __init__(self, articles):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "content" / "operations"
        self.dir.mkdir(parents=True)
        for name, text in articles:
            (self.dir / name).write_text(text, encoding="utf-8")
        self._p = patch.object(ah, "_content_dir", lambda section: self.dir if section == "operations" else None)

    def __enter__(self):
        self._p.start(); return self

    def __exit__(self, *a):
        self._p.stop(); self.tmp.cleanup(); return False


def _days_ago(n):
    return datetime.now() - timedelta(days=n)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_no_outbound_calls(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        for mod in ("subprocess", "urllib", "requests", "psycopg2", "socket"):
            self.assertNotIn(f"import {mod}", SRC)

    def test_never_walks_the_whole_home_tree(self):
        code = "\n".join(l for l in SRC.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn('glob("**', code)
        self.assertNotIn("rglob", code)
        self.assertIn('d.glob("*.md")', code)                # one flat glob of the section dir only
        with patch.object(ah.Path, "home", return_value=Path(tempfile.gettempdir()) / "no-such-home-xyz"):
            self.assertIsNone(ah._content_dir("operations"))

    def test_frontmatter_injection_cannot_spoof_title_or_escape_the_block(self):
        text = '---\ntitle: "real"\ntitle: "spoof"\n---\n---\ntitle: "after-block"\n'
        fm = ah._parse_frontmatter(text)
        self.assertEqual(fm["title"], "real")             # first key wins, nothing after the block is read
        self.assertEqual(ah._parse_frontmatter("title: x\n---\n"), {})
        with _Section([_article(_days_ago(1), "*bold* `code` _it_")]):
            out = ah.recent_articles_context("operations")
        self.assertIn('"bold code it"', out)             # markdown noise stripped from titles


class TestPerformance(unittest.TestCase):
    def test_parsers_on_10k_documents(self):
        name, text = _article(_days_ago(2), "Perf", body="word " * 400)
        t0 = time.perf_counter()
        for _ in range(10_000):
            ah._parse_frontmatter(text); ah._first_sentence(text); ah._article_date(Path(name), {})
        self.assertLess(time.perf_counter() - t0, 3.0)

    def test_context_window_is_capped_at_max_items(self):
        arts = [_article(_days_ago(1), f"Article {i}") for i in range(120)]
        with _Section(arts):
            out = ah.recent_articles_context("operations", max_items=40)
        self.assertEqual(out.count("\n- "), 40)


class TestRetry(unittest.TestCase):
    def test_unreadable_article_is_skipped_not_fatal(self):
        # RETRY GAP: recent_articles_context/read_text — an OSError on one file is skipped once, never retried
        good = _article(_days_ago(1), "Good one")
        bad = _article(_days_ago(1), "Bad one")
        real = Path.read_text

        def flaky(self, *a, **k):
            if self.name == bad[0]:
                raise OSError("EIO")
            return real(self, *a, **k)
        with _Section([good, bad]), patch.object(ah.Path, "read_text", flaky):
            out = ah.recent_articles_context("operations")
        self.assertIn("Good one", out)
        self.assertNotIn("Bad one", out)

    def test_missing_section_returns_empty_string(self):
        with _Section([]):
            self.assertEqual(ah.recent_articles_context("nonexistent"), "")
            self.assertEqual(ah.recent_articles_context("operations"), "")


class TestUnit(unittest.TestCase):
    def test_parse_frontmatter_edges(self):
        self.assertEqual(ah._parse_frontmatter(""), {})
        self.assertEqual(ah._parse_frontmatter("no frontmatter"), {})
        self.assertEqual(ah._parse_frontmatter("---\ntitle: never closed\n"), {})
        fm = ah._parse_frontmatter("---\nTitle: 'Quoted'\ndate: 2026-01-02\n  junk line\n---\n")
        self.assertEqual(fm, {"title": "Quoted", "date": "2026-01-02"})

    def test_first_sentence_edges(self):
        self.assertEqual(ah._first_sentence(""), "")
        self.assertEqual(ah._first_sentence("---\ntitle: x\n---\n"), "")
        self.assertEqual(ah._first_sentence("# Head\n\n> Short. Rest"), "Head Short. Rest")   # dot too early (<40): whole cut kept
        long = "A" * 60 + ". " + "B" * 200
        s = ah._first_sentence("---\nt: x\n---\n" + long)
        self.assertEqual(s, "A" * 60 + ".")
        self.assertLessEqual(len(ah._first_sentence("C" * 500)), 140)

    def test_article_date_prefers_filename_then_frontmatter(self):
        self.assertEqual(ah._article_date(Path("2026-03-04-x.md"), {"date": "2020-01-01"}), datetime(2026, 3, 4))
        self.assertEqual(ah._article_date(Path("x.md"), {"date": "2026-03-04T10:00:00"}), datetime(2026, 3, 4))
        self.assertEqual(ah._article_date(Path("2026-13-40-x.md"), {"date": "2026-03-04"}), datetime(2026, 3, 4))
        self.assertIsNone(ah._article_date(Path("x.md"), {"date": "2026-99-99"}))
        self.assertIsNone(ah._article_date(Path("x.md"), {}))

    def test_content_dir_finds_the_home_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "nova-journal" / "content" / "opinions").mkdir(parents=True)
            with patch.object(ah.Path, "home", return_value=Path(tmp)):
                self.assertEqual(ah._content_dir("opinions"), Path(tmp) / "nova-journal" / "content" / "opinions")
                self.assertIsNone(ah._content_dir("fishbowl"))


class TestIntegration(unittest.TestCase):
    def test_window_sorting_and_gist_source(self):
        arts = [_article(_days_ago(1), "Newest", description="desc wins"),
                _article(_days_ago(5), "Middle", body="Body sentence that is long enough to be a gist. More."),
                _article(_days_ago(20), "Too old"),
                _article(_days_ago(3), "No prefix", prefix=False)]
        with _Section(arts):
            out = ah.recent_articles_context("operations", days=14)
        lines = [l for l in out.splitlines() if l.startswith("- ")]
        self.assertEqual([l.split('"')[1] for l in lines], ["Newest", "No prefix", "Middle"])
        self.assertIn("— desc wins", lines[0])
        self.assertIn("— Body sentence that is long enough to be a gist.", lines[2])
        self.assertNotIn("Too old", out)

    def test_prompt_block_instructs_against_rehashing(self):
        with _Section([_article(_days_ago(1), "X")]):
            out = ah.recent_articles_context("operations", days=7)
        self.assertTrue(out.startswith("YOUR LAST 7 DAYS IN THIS COLUMN"))
        self.assertIn("Do NOT rehash", out)
        self.assertTrue(out.endswith("\n"))

    def test_untitled_article_falls_back_to_stem(self):
        name = f"{_days_ago(1):%Y-%m-%d}-untitled-thing.md"
        with _Section([(name, "no frontmatter at all\n")]):
            out = ah.recent_articles_context("operations")
        self.assertIn(f'"{name[:-3]}"', out)


class TestFunctional(unittest.TestCase):
    def test_cli_prints_context_for_a_populated_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "nova-journal" / "content" / "operations"; d.mkdir(parents=True)
            name, text = _article(_days_ago(2), "Golden path"); (d / name).write_text(text)
            r = subprocess.run([sys.executable, str(SCRIPT), "operations"], capture_output=True, text=True, timeout=30,
                               env={**os.environ, "HOME": tmp, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"Golden path"', r.stdout)

    def test_cli_error_path_unknown_section(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "no-such-section-zz"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "(no articles in the last 14 days for section 'no-such-section-zz')")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_the_cli(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_article_history"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
