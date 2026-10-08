#!/usr/bin/env python3
"""7-category tests for nova_watches_and_friends.py (Security, Performance, Retry, Unit, Integration,
Functional, Frame). PostgreSQL, the Claude CLI (call_openrouter), image generation, Hugo publish,
git push and Slack are all mocked — nothing is published. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_watches_and_friends_7cat.py
"""
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_watches_and_friends as m  # noqa: E402

SRC = (SCRIPTS / "nova_watches_and_friends.py").read_text()
D = datetime(2026, 10, 7, 9, 0)


def row(text, vid, chan="Chan", title="T", **extra):
    return (text, D, {"video_id": vid, "channel": chan, "title": title, **extra})


class Cur:
    """One cursor serving both connections: answers by query shape."""

    def __init__(self, news, fish, dossiers=()):
        self.news, self.fish, self.dossiers, self.calls, self._r = news, fish, list(dossiers), [], []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "FROM yt_ingest_seen" in sql:
            self._r = [("v1",)]
        elif "fishbowl_people" in sql:
            self._r = self.dossiers
        elif "source = 'fishbowl'" in sql:
            self._r = self.fish
        elif "FROM memories" in sql:
            self._r = self.news

    def fetchall(self):
        return self._r


def run_main(argv=(), news=None, fish=None, llm=None, publish=True):
    news = [row("[Chan] the new Speedmaster", "v1")] if news is None else news
    fish = [row("[F] drama", "f1", chan="Fish")] if fish is None else fish
    cur = Cur(news, fish, [("Bob", "chanA", "summary")])
    conn = MagicMock(); conn.cursor.return_value = cur
    nj = MagicMock()
    nj.today_str.return_value = "2026-10-08"
    nj.call_openrouter.side_effect = llm or (lambda *a, **k: "TITLE: Big Week\n## News\n" + "word " * 3000)
    nj.publish_hugo.return_value = publish
    nj.git_push.return_value = "ok"
    with patch.object(sys, "argv", ["x", *argv]), patch.object(m, "_connect", return_value=conn), \
            patch.object(m, "nj", nj), patch.object(m.nova_voice, "system_prompt", side_effect=lambda c: c):
        rc = m.main()
    return rc, nj, cur


class TestSecurity(unittest.TestCase):
    def test_queries_parameterised(self):
        self.assertNotRegex(SRC, r'execute\(f["\']')
        _, _, cur = run_main(["--dry-run", "--days", "3"])
        mem = [p for s, p in cur.calls if "FROM memories" in s]
        self.assertTrue(all(p[0] == 3 for p in mem))

    def test_title_link_text_cannot_break_markdown_link(self):
        out = m.sources([{"url": "https://y/1", "channel": "C", "title": "a]](javascript:x)", "date": "d"}])
        self.assertNotIn("]]", out.split("](")[0])

    def test_no_secrets_or_home_paths(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]|password\s*=|sk-or-")

    def test_prompts_forbid_invention(self):
        self.assertIn("Never invent prices", SRC)


class TestPerformance(unittest.TestCase):
    def test_digest_bounded_by_video_and_char_caps(self):
        rows = [row("x" * 5000, f"v{i}") for i in range(200)]
        vids = m.group_videos(rows, m.NEWS_PER_VIDEO, m.NEWS_MAX_VIDEOS)
        self.assertEqual(len(vids), m.NEWS_MAX_VIDEOS)
        self.assertLess(len(m.digest(vids)), 70_000)   # stays under the size that hangs claude -p

    def test_grouping_large_input_fast(self):
        rows = [row("chunk " * 50, f"v{i % 500}") for i in range(20000)]
        t = time.perf_counter()
        m.group_videos(rows, 1600, 36)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_llm_calls_have_timeouts(self):
        _, nj, _ = run_main(["--dry-run"])
        self.assertTrue(all(c.kwargs.get("timeout") for c in nj.call_openrouter.call_args_list))


class TestRetry(unittest.TestCase):
    def test_pg_connect_retried_with_backoff(self):
        conn = MagicMock()
        with patch.object(m.psycopg2, "connect", side_effect=[psycopg2.OperationalError("x"), conn]), \
                patch.object(m.time, "sleep") as sl, patch.object(m, "log"):
            self.assertIs(m._connect("dsn"), conn)
        sl.assert_called_once_with(5)

    def test_pg_connect_gives_up_loudly(self):
        with patch.object(m.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")) as c, \
                patch.object(m.time, "sleep"), patch.object(m, "log") as lg, self.assertRaises(psycopg2.OperationalError):
            m._connect("dsn")
        self.assertEqual(c.call_count, 3)
        self.assertEqual(lg.call_count, 2)

    def test_llm_retry_lives_in_call_openrouter(self):
        src = (SCRIPTS / "nova_journal.py").read_text()
        self.assertIn("_attempts = 3", src)

    def test_news_llm_failure_aborts_not_silent(self):
        rc, nj, _ = run_main(llm=lambda *a, **k: None)
        self.assertEqual(rc, 1)
        nj.publish_hugo.assert_not_called()

    def test_image_failure_non_fatal(self):
        nj_holder = {}

        def llm(*a, **k):
            return "TITLE: Week\n" + "w " * 6000
        rc, nj, _ = run_main(llm=llm)
        self.assertEqual(rc, 0)
        nj_holder["nj"] = nj
        # now with the image backend failing
        cur = Cur([row("t", "v1")], [])
        conn = MagicMock(); conn.cursor.return_value = cur
        nj2 = MagicMock(); nj2.call_openrouter.side_effect = llm; nj2.today_str.return_value = "d"
        nj2.generate_image.side_effect = RuntimeError("SD down")
        nj2.publish_hugo.return_value = True
        with patch.object(sys, "argv", ["x"]), patch.object(m, "_connect", return_value=conn), \
                patch.object(m, "nj", nj2), patch.object(m.nova_voice, "system_prompt", side_effect=lambda c: c):
            self.assertEqual(m.main(), 0)
        self.assertIsNone(nj2.publish_hugo.call_args.kwargs["image_path"])


class TestUnit(unittest.TestCase):
    def test_group_videos_merges_chunks_and_strips_prefix(self):
        vids = m.group_videos([row("[Chan] a", "v1"), row("[Chan] b", "v1"), row("c", "v2")], 100, 5)
        self.assertEqual(vids[0]["text"], "a b")
        self.assertEqual(vids[0]["url"], "https://www.youtube.com/watch?v=v1")
        self.assertEqual(len(vids), 2)

    def test_group_videos_handles_missing_metadata(self):
        vids = m.group_videos([("text here", D, None)], 100, 5)
        self.assertEqual(vids[0]["channel"], "unknown")
        self.assertIsNone(vids[0]["url"])

    def test_sources_dedup_and_empty(self):
        v = {"url": "u", "channel": "c", "title": "t", "date": "d"}
        self.assertEqual(m.sources([v, dict(v)]).count("\n"), 0)
        self.assertEqual(m.sources([]), "- (none this week)")

    def test_split_title_and_words(self):
        self.assertEqual(m.split_title('TITLE: "Hi"\nbody'), ("Hi", "body"))
        self.assertEqual(m.split_title(None), (None, ""))
        self.assertEqual(m.words("a b, c"), 3)


class TestIntegration(unittest.TestCase):
    def test_fishbowl_section_nested_and_sources_appended(self):
        def llm(system, user, **k):
            if "Fishbowl / Hate Streams' section" in system:
                return "## Fishbowl stuff\nintro\n## Beef\nmore"
            return "TITLE: Week\n## News\n" + "w " * 6000
        rc, nj, _ = run_main(["--dry-run"], llm=llm)
        self.assertEqual(rc, 0)
        with patch("builtins.print") as pr:
            run_main(["--dry-run"], llm=llm)
        body = pr.call_args.args[0]
        self.assertIn("## Fishbowl / Hate Streams\n\nintro\n### Beef", body)
        self.assertIn("## Sources\n\n### Watch News", body)

    def test_short_issue_gets_deeper_cuts_call(self):
        calls = []

        def llm(system, user, **k):
            calls.append(system)
            return "TITLE: Short week\nshort" if len(calls) == 1 else "## Deeper Cuts\nmore"
        run_main(["--dry-run"], fish=[], llm=llm)
        self.assertEqual(len(calls), 2)
        self.assertIn("Deeper Cuts", calls[1])


class TestFunctional(unittest.TestCase):
    def test_golden_publishes_pushes_and_notifies(self):
        rc, nj, _ = run_main()
        self.assertEqual(rc, 0)
        title = nj.publish_hugo.call_args.args[0]
        self.assertTrue(title.startswith("Watches and Friends: Big Week"))
        nj.git_push.assert_called_once()
        self.assertEqual(nj.notify_slack.call_args.args[0], "fishbowl")

    def test_no_news_aborts(self):
        rc, nj, _ = run_main(news=[])
        self.assertEqual(rc, 1)
        nj.call_openrouter.assert_not_called()

    def test_quality_guard_reject_returns_1_without_push(self):
        rc, nj, _ = run_main(publish=False)
        self.assertEqual(rc, 1)
        nj.git_push.assert_not_called()

    def test_dry_run_never_publishes(self):
        rc, nj, _ = run_main(["--dry-run"])
        self.assertEqual(rc, 0)
        nj.publish_hugo.assert_not_called()
        nj.notify_slack.assert_not_called()

    def test_missing_title_falls_back_to_week(self):
        with patch("builtins.print"):
            rc, nj, _ = run_main(llm=lambda *a, **k: "no title\n" + "w " * 6000)
        self.assertEqual(nj.publish_hugo.call_args.args[0], "Watches and Friends — Week Ending 2026-10-08")


class TestFrame(unittest.TestCase):
    def test_module_shape(self):
        for n in ("main", "group_videos", "digest", "sources", "split_title", "_connect"):
            self.assertTrue(callable(getattr(m, n)))

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_watches_and_friends.py"), "--help"],
                           capture_output=True, text=True, timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--days", r.stdout)


if __name__ == "__main__":
    unittest.main()
