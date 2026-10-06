#!/usr/bin/env python3
"""Tests for nova_journal_weekly_summary.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ws = _load("nova_journal_weekly_summary_t", SCRIPTS / "nova_journal_weekly_summary.py")
_TMP = Path(tempfile.mkdtemp())
ws.CONTENT_ROOT = _TMP / "content"
ws.LOG_FILE = _TMP / "weekly.log"
ws.STATE_FILE = _TMP / "state.json"
# every publish side effect stubbed at load
ws.nova_notify = MagicMock()
ws.publish_hugo = MagicMock()
ws.git_push = MagicMock()
ws.generate_image = MagicMock(return_value=None)
ws.call_openrouter = MagicMock(return_value="recap " * 100)
ws.system_prompt = lambda ctx="", **k: "VOICE\n" + ctx      # real one reads PG
SRC = (SCRIPTS / "nova_journal_weekly_summary.py").read_text()


def _art(section, name, d, title="A piece", tags='["x"]', body="Body text here."):
    p = ws.CONTENT_ROOT / section / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\ntitle: "{title}"\ndate: {d.isoformat()}T09:00:00-07:00\ntags: {tags}\n---\n{body}\n')
    return p


def _reset():
    shutil.rmtree(ws.CONTENT_ROOT, ignore_errors=True)
    ws.STATE_FILE.unlink(missing_ok=True)
    for m in (ws.nova_notify, ws.publish_hugo, ws.git_push, ws.generate_image, ws.call_openrouter):
        m.reset_mock(side_effect=True)
    ws.call_openrouter.return_value = "recap " * 100
    ws.generate_image.return_value = None


def _q():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_slug_is_url_safe(self):
        s = ws._slug('../../"etc" <script>passwd</script>')
        self.assertRegex(s, r"^[a-z0-9-]+$")
        self.assertLessEqual(len(ws._slug("x" * 500)), 60)

    def test_dry_run_never_publishes(self):
        _reset()
        today = date.today()
        _art("tech", "a.md", today); _art("tech", "b.md", today)
        with _q() as out:
            ws.run(dry_run=True)
        self.assertIn("WOULD-SUMMARIZE", out.getvalue())
        for m in (ws.publish_hugo, ws.git_push, ws.nova_notify, ws.call_openrouter):
            m.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_week_summary_check_10k(self):
        arts = [{"title": f"Piece {i}", "tags": ["a", "b"]} for i in range(10_000)]
        t0 = time.perf_counter()
        self.assertFalse(any(ws._is_weekly_summary(a) for a in arts))
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_short_llm_output_is_rejected(self):
        # RETRY GAP: generate_summary/call_openrouter — one attempt; next weekly run retries (not marked seen)
        ws.call_openrouter.return_value = "too short"
        today = date.today()
        with _q():
            self.assertIsNone(ws.generate_summary("tech", [], today, today))
        self.assertEqual(ws.call_openrouter.call_count, 1)

    def test_publish_failure_leaves_week_unseen(self):
        today = date.today()
        _art("tech", "a.md", today); _art("tech", "b.md", today)
        ws.publish_hugo.side_effect = RuntimeError("hugo broke")
        with _q():
            ws.run()
        self.assertFalse(ws.STATE_FILE.exists())
        ws.git_push.assert_not_called()

    def test_image_failure_non_fatal(self):
        ws.generate_image.side_effect = RuntimeError("no gpu")
        today = date.today()
        arts = [{"title": "t", "date": today, "body": "b", "name": "n"}]
        with _q():
            self.assertTrue(ws.summarize_section("tech", arts, today, today, {}, False))
        self.assertIsNone(ws.publish_hugo.call_args[1]["image_path"])


class TestUnit(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_parse_article_dates_and_skips(self):
        p = _art("s", "x.md", date(2026, 3, 4), title="Hi", tags='["Weekly-Summary", "a"]')
        a = ws._parse_article(p)
        self.assertEqual((a["date"], a["title"]), (date(2026, 3, 4), "Hi"))
        self.assertTrue(ws._is_weekly_summary(a))
        bare = ws.CONTENT_ROOT / "s" / "2026-01-02-bare.md"; bare.write_text("no front matter")
        self.assertEqual(ws._parse_article(bare)["date"], date(2026, 1, 2))
        nodate = ws.CONTENT_ROOT / "s" / "nodate.md"; nodate.write_text("x")
        self.assertIsNone(ws._parse_article(nodate))
        self.assertIsNone(ws._parse_article(ws.CONTENT_ROOT / "s" / "_index.md"))

    def test_is_weekly_summary_title_with_emoji(self):
        self.assertTrue(ws._is_weekly_summary({"title": "\U0001f4c5 This Week in Tech", "tags": []}))

    def test_date_range_label(self):
        self.assertEqual(ws._date_range_label(date(2026, 3, 1), date(2026, 3, 7)), "March 1–7, 2026")
        self.assertEqual(ws._date_range_label(date(2025, 12, 29), date(2026, 1, 4)), "Dec 29, 2025 – Jan 04, 2026")

    def test_list_sections_skips_meta(self):
        for d in ("tech", "about", "_hidden", "operations"):
            (ws.CONTENT_ROOT / d).mkdir(parents=True)
        self.assertEqual(ws.list_sections(), ["operations", "tech"])


class TestIntegration(unittest.TestCase):
    def test_reuses_journal_pipeline(self):
        self.assertIn("from nova_journal import publish_hugo, git_push, call_openrouter, generate_image", SRC)
        self.assertIn("from nova_voice import system_prompt", SRC)

    def test_echoed_title_line_dropped(self):
        _reset()
        s, e = date(2026, 3, 1), date(2026, 3, 7)
        ws.call_openrouter.return_value = "# This Week in Tech: March 1–7, 2026\n" + "real body " * 50
        with _q():
            title, body = ws.generate_summary("tech", [{"title": "A", "date": s, "body": "b"}], s, e)
        self.assertEqual(title, "This Week in Tech: March 1–7, 2026")
        self.assertTrue(body.startswith("real body"))


class TestFunctional(unittest.TestCase):
    def test_run_publishes_qualifying_sections_once(self):
        _reset()
        today = date.today()
        _art("tech", "a.md", today); _art("tech", "b.md", today - timedelta(days=2))
        _art("tech", "old.md", today - timedelta(days=30))
        _art("lonely", "a.md", today)
        with _q():
            ws.run()
            ws.run()                                         # same week: deduped by state
        self.assertEqual(ws.publish_hugo.call_count, 1)
        args = ws.publish_hugo.call_args[0]
        self.assertEqual((args[2], args[3]), ("tech", ["tech", "weekly-summary"]))
        ws.git_push.assert_called_once()
        self.assertIn("Weekly Summary (tech)", ws.nova_notify.call_args[0][0])
        self.assertEqual(len(json.loads(ws.STATE_FILE.read_text())["seen"]), 1)


class TestFrame(unittest.TestCase):
    def test_dry_run_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, str(SCRIPTS / "nova_journal_weekly_summary.py"), "dry-run"],
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY-RUN", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_journal_weekly_summary"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
