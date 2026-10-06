#!/usr/bin/env python3
"""Tests for nova_weekly_media_wrap.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import datetime as dt
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_weekly_media_wrap.py"
SRC = SCRIPT.read_text()


def _stubs():
    nj = types.ModuleType("nova_journal")
    nj.log = MagicMock(); nj.call_openrouter = MagicMock(return_value="TITLE: Couch Potato Protocol\n\nI watched things.")
    nj.today_str = MagicMock(return_value="2026-10-05"); nj.get_image_prompt = MagicMock(return_value="prompt")
    nj.generate_image = MagicMock(return_value="/tmp/img.webp"); nj.publish_hugo = MagicMock(return_value=True)
    nj.git_push = MagicMock(); nj.notify_slack = MagicMock()
    nv = types.ModuleType("nova_voice"); nv.system_prompt = MagicMock(side_effect=lambda ctx: "SYS:" + ctx)
    ah = types.ModuleType("nova_article_history"); ah.recent_articles_context = MagicMock(return_value="RECENT: prior wrap")
    return {"nova_journal": nj, "nova_voice": nv, "nova_article_history": ah}


_STUBS = _stubs()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _STUBS):      # journal/LLM/git/Slack stubbed at import; restored after
        spec.loader.exec_module(mod)
    return mod


mw = _load("mw_mod", SCRIPT)


class _Cur:
    """Answers the fixed query sequence: shows, recordings, (bcast, local), (total,), then one snippet per show."""
    def __init__(self, shows, recordings=(), news=(0, 0), total=0, snippets=None):
        self.shows, self.recordings, self.news, self.total = list(shows), list(recordings), news, total
        self.snippets = snippets or {}; self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params)); self._last = (sql, params)

    def fetchall(self):
        return self.shows if "tv_transcript" in self._last[0] else self.recordings

    def fetchone(self):
        sql, params = self._last
        if "FILTER" in sql:
            return self.news
        if "SELECT count(*) FROM memories" in sql:
            return (self.total,)
        return self.snippets.get(params[1])


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.autocommit = False

    def cursor(self):
        return self.cur


def _show(name="Last Week Tonight", eps=2, chunks=40):
    return (name, eps, chunks, "youtube", dt.date(2026, 10, 3))


def _reset():
    nj = mw.nj
    for fn in ("log", "call_openrouter", "get_image_prompt", "generate_image", "publish_hugo", "git_push", "notify_slack"):
        getattr(nj, fn).reset_mock()
    nj.call_openrouter.return_value = "TITLE: Couch Potato Protocol\n\nI watched things."
    nj.publish_hugo.return_value = True; nj.generate_image.side_effect = None
    mw.nova_voice.system_prompt.reset_mock()
    _STUBS["nova_article_history"].recent_articles_context.return_value = "RECENT: prior wrap"


def _main(cur):
    _reset()
    with patch.object(mw.psycopg2, "connect", return_value=_Conn(cur)), patch.dict(sys.modules, {"nova_article_history": _STUBS["nova_article_history"]}):
        return mw.main()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", mw.MEM_DSN)

    def test_sql_is_parameterized_and_read_only(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))
        cur = _Cur([_show("x'; select 1; --")], snippets={"x'; select 1; --": ("snip", 1)})
        _main(cur)
        for sql, params in cur.sql:
            self.assertNotIn("select 1", sql)
        self.assertEqual(cur.sql[-1][1], ("7 days", "x'; select 1; --"))

    def test_snippets_are_truncated_before_the_llm_sees_them(self):
        cur = _Cur([_show()], snippets={"Last Week Tonight": ("z" * 5000, 7)})
        _main(cur)
        user = mw.nj.call_openrouter.call_args[0][1]
        self.assertIn("z" * 400, user); self.assertNotIn("z" * 401, user)


class TestPerformance(unittest.TestCase):
    def test_10k_shows_render_under_bound(self):
        shows = [_show(f"show {i}", 1, 3) for i in range(10_000)]
        cur = _Cur(shows, total=30_000)
        t0 = time.perf_counter()
        rc = _main(cur)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(rc, 0)
        self.assertEqual(sum(1 for s, _ in cur.sql if "ORDER BY created_at DESC LIMIT 1" in s), 14)   # snippet cap


class TestRetry(unittest.TestCase):
    def test_llm_empty_aborts_without_publishing(self):
        # RETRY GAP: main()/nj.call_openrouter — one attempt; empty reply → rc=1 (weekly task reruns next Sunday)
        _reset(); mw.nj.call_openrouter.return_value = ""
        with patch.object(mw.psycopg2, "connect", return_value=_Conn(_Cur([_show()]))), \
             patch.dict(sys.modules, {"nova_article_history": _STUBS["nova_article_history"]}):
            self.assertEqual(mw.main(), 1)
        self.assertEqual(mw.nj.call_openrouter.call_count, 1)
        mw.nj.publish_hugo.assert_not_called()

    def test_image_failure_non_fatal(self):
        _reset(); mw.nj.generate_image.side_effect = RuntimeError("sd down")
        with patch.object(mw.psycopg2, "connect", return_value=_Conn(_Cur([_show()]))), \
             patch.dict(sys.modules, {"nova_article_history": _STUBS["nova_article_history"]}):
            self.assertEqual(mw.main(), 0)
        self.assertIsNone(mw.nj.publish_hugo.call_args[1]["image_path"])

    def test_pg_failure_propagates(self):
        # RETRY GAP: main()/psycopg2.connect — single attempt, exception escapes to the scheduler
        _reset()
        with patch.object(mw.psycopg2, "connect", side_effect=OSError("pg down")):
            with self.assertRaises(OSError):
                mw.main()
        mw.nj.call_openrouter.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_nothing_ingested_skips(self):
        rc = _main(_Cur([], []))
        self.assertEqual(rc, 0)
        mw.nj.call_openrouter.assert_not_called()
        self.assertIn("nothing media-shaped", mw.nj.log.call_args[0][0])

    def test_degenerate_title_falls_back(self):
        for raw in ("TITLE: TV\n\nbody", "TITLE: wow wow wow wow wow wow wow wow\n\nbody", "no title\nbody"):
            _reset(); mw.nj.call_openrouter.return_value = raw
            with patch.object(mw.psycopg2, "connect", return_value=_Conn(_Cur([_show()]))), \
                 patch.dict(sys.modules, {"nova_article_history": _STUBS["nova_article_history"]}):
                mw.main()
            self.assertEqual(mw.nj.publish_hugo.call_args[0][0], "What I Watched So You Didn't Have To — 2026-10-05", raw)

    def test_good_title_kept_and_quotes_stripped(self):
        _reset(); mw.nj.call_openrouter.return_value = 'title: "Seven Days of Screens"\n\nbody'
        with patch.object(mw.psycopg2, "connect", return_value=_Conn(_Cur([_show()]))), \
             patch.dict(sys.modules, {"nova_article_history": _STUBS["nova_article_history"]}):
            mw.main()
        self.assertEqual(mw.nj.publish_hugo.call_args[0][0], "Seven Days of Screens")

    def test_stats_footer_is_deterministic(self):
        cur = _Cur([_show("A", 2, 10), _show("B", 1, 5)], [("KTLA", 3), ("KCAL", 1)], news=(4, 9), total=25)
        _main(cur)
        body = mw.nj.publish_hugo.call_args[0][1]
        self.assertTrue(body.startswith("I watched things."))
        self.assertIn("- Shows ingested: **2** (3 episodes, 15 transcript chunks)", body)
        self.assertIn("- OTA recordings: **4** across 2 channels", body)
        self.assertIn("- News: **4** broadcasts, **9** local-news items", body)
        self.assertIn("- Total media memories stored this week: **25**", body)


class TestIntegration(unittest.TestCase):
    def test_reads_nova_memories_metadata_types_and_cites_memory_ids(self):
        cur = _Cur([_show("A"), _show("B")], snippets={"A": ("sa", 11), "B": ("sb", 22)})
        _main(cur)
        self.assertIn("nova_memories", mw.MEM_DSN)
        self.assertIn("metadata->>'type'='tv_transcript'", cur.sql[0][0])
        self.assertIn("metadata->>'type'='full_episode'", cur.sql[1][0])
        self.assertEqual(cur.sql[0][1], ("7 days",))
        self.assertEqual(mw.nj.publish_hugo.call_args[1]["cited_memory_ids"], [11, 22])

    def test_article_history_context_is_appended_to_prompt(self):
        _main(_Cur([_show()]))
        system, user = mw.nj.call_openrouter.call_args[0]
        self.assertTrue(system.startswith("SYS:Write this week's SUNDAY MEDIA WRAP-UP"))
        self.assertTrue(user.endswith("RECENT: prior wrap"))
        self.assertIn("- Last Week Tonight: 2 episode(s), 40 transcript chunk(s), memory vector 'youtube', last ingested 2026-10-03", user)
        self.assertEqual(mw.nj.call_openrouter.call_args[1], {"max_tokens": 3200, "temperature": 0.9})


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_pushes_notifies(self):
        cur = _Cur([_show()], [("KTLA", 2)], news=(1, 2), total=9, snippets={"Last Week Tonight": ("snippet", 5)})
        self.assertEqual(_main(cur), 0)
        args, kw = mw.nj.publish_hugo.call_args
        self.assertEqual(args[0], "Couch Potato Protocol"); self.assertEqual(args[2], "operations")
        self.assertIn("youtube", args[3]); self.assertEqual(kw["emoji"], "📺"); self.assertEqual(kw["image_path"], "/tmp/img.webp")
        mw.nj.git_push.assert_called_once_with("operations", "Couch Potato Protocol")
        mw.nj.notify_slack.assert_called_once_with("operations", "📺 Couch Potato Protocol", "Nova's weekly media ingest wrap-up.")
        self.assertIn("PUBLISHED: Couch Potato Protocol", mw.nj.log.call_args[0][0])

    def test_quality_guard_rejection(self):
        _reset(); mw.nj.publish_hugo.return_value = False
        with patch.object(mw.psycopg2, "connect", return_value=_Conn(_Cur([_show()]))), \
             patch.dict(sys.modules, {"nova_article_history": _STUBS["nova_article_history"]}):
            self.assertEqual(mw.main(), 1)
        mw.nj.git_push.assert_not_called(); mw.nj.notify_slack.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        snippet = ("import sys, types; from unittest.mock import MagicMock\n"
                   "for n in ('nova_journal', 'nova_voice'): sys.modules[n] = types.ModuleType(n)\n"
                   "import psycopg2; psycopg2.connect = MagicMock(side_effect=AssertionError('pg at import'))\n"
                   "import nova_weekly_media_wrap as m; assert m.WINDOW == '7 days'")
        r = subprocess.run([sys.executable, "-c", snippet], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
