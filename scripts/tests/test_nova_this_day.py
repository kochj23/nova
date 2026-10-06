#!/usr/bin/env python3
"""Tests for nova_this_day.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
notify and the structured logger are stubbed on the loaded module; Wikipedia and the memory server
(urlopen) are mocked; MEMORY_DIR is a tempdir. Nothing is posted or remembered."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_this_day.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="thisday_"))


def _load():
    spec = importlib.util.spec_from_file_location("nova_this_day_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.notify = MagicMock()
    mod.log = MagicMock()
    mod.MEMORY_DIR = TMP / "memory"
    return mod


td = _load()
WIKI = {"events": [{"year": 1969, "text": "First astronaut walks on the moon in a space mission"},
                   {"year": None, "text": "something"}, {"year": 1900, "text": "A treaty is signed"}],
        "births": [{"year": 1950, "text": "A famous musician"}], "deaths": [{"year": 2000, "text": "A king"}]}


def _resp(obj):
    m = MagicMock()
    m.read.return_value = json.dumps(obj).encode()
    m.__enter__.return_value = m
    return m


class _Urls:
    def __init__(self, wiki=WIKI, mems=()):
        self.wiki, self.mems, self.calls = wiki, list(mems), []

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        self.calls.append(req)
        if "wikimedia" in url:
            if self.wiki is None:
                raise OSError("wiki down")
            return _resp(self.wiki)
        if "/search?" in url or "/recall?" in url:
            return _resp({"memories": self.mems})
        return _resp({})

    def posts(self):
        return [json.loads(r.data) for r in self.calls if not isinstance(r, str) and r.data]


class _Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(td.MEMORY_DIR, ignore_errors=True)
        td.notify.reset_mock()


class TestSecurity(_Base):
    def test_no_credentials_and_query_is_url_encoded(self):
        self.assertIsNone(re.search(r"(password|api[_-]?key|token)\s*=\s*['\"]", SRC, re.I))
        u = _Urls()
        with patch.object(td.urllib.request, "urlopen", side_effect=u):
            td.vector_search("a&b=c d")
        self.assertIn("q=a%26b%3Dc%20d", u.calls[0])

    def test_private_memories_filtered(self):
        mems = [{"text": "2010 public", "source": "music"}, {"text": "2011 secret", "source": "work_email"}]
        with patch.object(td.urllib.request, "urlopen", side_effect=_Urls(mems=mems)), \
             patch.object(td.nova_config, "filter_private_memories", side_effect=lambda m: m[:1]) as f:
            self.assertEqual(len(td.vector_recall("q")), 1)
        f.assert_called_once()


class TestPerformance(_Base):
    def test_rank_10k_events_fast(self):
        items = [{"year": i, "text": f"war and discovery number {i} " * (i % 5)} for i in range(10_000)]
        t0 = time.perf_counter()
        best = td.pick_best(items, 6, score_fn=td.score_event)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(best), 6)


class TestRetry(_Base):
    def test_all_fetchers_fail_open(self):
        # RETRY GAP: fetch_on_this_day/vector_recall/vector_search/vector_remember — one try, safe default
        with patch.object(td.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertIsNone(td.fetch_on_this_day(1, 2))
            self.assertEqual(td.vector_recall("q"), [])
            self.assertEqual(td.vector_search("q"), [])
            td.vector_remember("t")
        self.assertEqual(u.call_count, 4)

    def test_wikipedia_down_still_posts_personal_section(self):
        with patch.object(td.urllib.request, "urlopen", side_effect=_Urls(wiki=None)):
            td.main()
        body = td.notify.call_args.kwargs["body"]
        self.assertIn("Wikipedia was unavailable", body)
        self.assertIn("Some dates are quiet", body)


class TestUnit(_Base):
    def test_score_event(self):
        self.assertGreater(td.score_event({"year": 1, "text": "war"}), td.score_event({"text": "war"}))
        self.assertEqual(td.score_event({}), 0)

    def test_clean_memory_text_email(self):
        t = td._clean_memory_text("Date: x From: Bob Subject: Lunch plans", "email_archive")
        self.assertEqual(t, "Email: Lunch plans (from Bob)")
        self.assertTrue(td._clean_memory_text("y" * 400, "music").endswith("..."))

    def test_find_memories_buckets_and_dedups(self):
        mems = [{"text": "In 2015 we went to Japan", "source": "imessage", "score": 0.5}] * 3 + \
               [{"text": "No year here", "source": "x"}]
        with patch.object(td, "vector_search", return_value=mems), patch.object(td, "vector_recall", return_value=[]):
            out = td.find_memories_for_date(5, 6, "May 06", 2026)
        self.assertEqual(list(out), [2015])
        self.assertEqual(len(out[2015]), 1)

    def test_format_personal_labels(self):
        out = td.format_personal_slack({2025: [{"text": "t", "source": "music"}]}, "May 06", 2026)
        self.assertIn("1 year ago", out)
        self.assertIn(":notes:", out)


class TestIntegration(_Base):
    def test_slack_post_uses_notify_bus(self):
        td.slack_post("*:calendar: On This Day*\nbody")
        args, kw = td.notify.call_args
        self.assertEqual(args[0], "On This Day")
        self.assertEqual((kw["category"], kw["dedup_key"]), ("calendar", "this-day-digest"))

    def test_append_to_memory_once(self):
        td.append_to_memory("## On This Day in History -- X\n", "2026-01-01")
        td.append_to_memory("## On This Day in History -- X\n", "2026-01-01")
        self.assertEqual((td.MEMORY_DIR / "2026-01-01.md").read_text().count("On This Day in History"), 1)


class TestFunctional(_Base):
    def test_golden_path(self):
        u = _Urls(mems=[{"text": "2019 concert night", "source": "music", "score": 1}])
        with patch.object(td.urllib.request, "urlopen", side_effect=u):
            td.main()
        body = td.notify.call_args.kwargs["body"]
        self.assertIn("*1969* — First astronaut", body)
        self.assertIn("2019", body)
        (mf,) = list(td.MEMORY_DIR.glob("*.md"))
        text = mf.read_text()
        self.assertIn("On This Day in History", text)
        self.assertIn("This Day in Your Life", text)
        sources = [p["source"] for p in u.posts()]
        self.assertEqual(sources.count("history"), 4)          # 3 events + 1 birth
        self.assertIn("dream", sources)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_this_day"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
