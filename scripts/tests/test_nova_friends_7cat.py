#!/usr/bin/env python3
"""7-category tests for nova_friends.py — Cars and Friends / Tech and Friends (Security, Performance,
Retry, Unit, Integration, Functional, Frame). PostgreSQL, the Claude CLI (call_openrouter), image
generation, Hugo publish, git push and Slack are all mocked — nothing is published.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_friends_7cat.py
"""
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_friends as m  # noqa: E402
import nova_watches_and_friends as w  # noqa: E402

SRC = (SCRIPTS / "nova_friends.py").read_text()
D = datetime(2026, 10, 19, 9, 0)


def vrow(text, vid, chan="Rob Dahm", title="T", **extra):
    return (text, D, {"video_id": vid, "channel": chan, "title": title, "pipeline": "yt_subs_audio", **extra})


def arow(text, url, feed="Ars Technica", title="A"):
    return (text, D, {"url": url, "feed": feed, "title": title, "type": "gov_rss"})


class Cur:
    """Answers by query shape: the channel query gets videos, the article query gets articles."""

    def __init__(self, videos, arts):
        self.videos, self.arts, self.calls, self._r = videos, arts, [], []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._r = self.arts if "gov_rss" in sql else self.videos

    def fetchall(self):
        return self._r


def run_main(argv=("--subject", "cars"), videos=None, arts=None, llm=None, publish=True):
    videos = [vrow("a rear wing that lifts the nose", "v1")] if videos is None else videos
    arts = [arow("new kernel release", "https://a/1")] if arts is None else arts
    cur = Cur(videos, arts)
    conn = MagicMock(); conn.cursor.return_value = cur
    nj = MagicMock()
    nj.today_str.return_value = "2026-10-19"
    nj.call_openrouter.side_effect = llm or (lambda *a, **k: "TITLE: Big Day Out\n## News\n" + "word " * 3000)
    nj.publish_hugo.return_value = publish
    nj.git_push.return_value = "ok"
    with patch.object(sys, "argv", ["x", *argv]), patch.object(m, "_connect", return_value=conn), \
            patch.object(m, "nj", nj), patch.object(m.nova_voice, "system_prompt", side_effect=lambda c: c):
        rc = m.main()
    return rc, nj, cur


class TestSecurity(unittest.TestCase):
    def test_queries_parameterised(self):
        # the only f-string SQL interpolates the constant NORM_SQL, never data
        for q in re.findall(r'execute\(f"""(.*?)"""', SRC, re.S):
            self.assertNotRegex(re.sub(r"\{NORM_SQL\}", "", q), r"\{")
        _, _, cur = run_main(["--subject", "tech", "--dry-run", "--days", "3"])
        self.assertTrue(all(p[0] == 3 for _, p in cur.calls))

    def test_banned_inputs_dropped(self):
        cur = Cur([vrow("my day at Disney work", "v1"), vrow("ok text", "v2")],
                  [arow("OpenClaw gateway notes", "u1"), arow("fine", "u2")])
        vids, arts = m.fetch(cur, m.SUBJECTS["tech"], 1)
        self.assertEqual([v["text"] for v in vids], ["ok text"])
        self.assertEqual([a["text"] for a in arts], ["fine"])

    def test_draft_with_private_detail_fails_closed(self):
        for leak in ("the box at 192.168.1.6", "back at Disney", "OpenClaw runs it", "/Users/someone/x"):
            rc, nj, _ = run_main(llm=lambda *a, leak=leak, **k: f"TITLE: Big Day Out\n{leak}\n" + "w " * 3000)
            self.assertEqual(rc, 1, leak)
            nj.publish_hugo.assert_not_called()

    def test_prompt_carries_public_journal_rules(self):
        for s in ("Never invent prices", "never mention Jordan's employer", "'OpenClaw'", "no sexual content",
                  "no IP addresses", "household"):
            self.assertIn(s, SRC)

    def test_no_secrets_or_home_paths(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]|password\s*=|sk-or-")


class TestPerformance(unittest.TestCase):
    def test_source_block_bounded(self):
        cur = Cur([vrow("x" * 5000, f"v{i}") for i in range(200)], [arow("y" * 5000, f"u{i}") for i in range(200)])
        vids, arts = m.fetch(cur, m.SUBJECTS["tech"], 1)
        self.assertEqual((len(vids), len(arts)), (m.VID_MAX, m.ART_MAX))
        self.assertLess(len(w.digest(vids)) + len(w.digest(arts)), 70_000)   # under the claude -p hang size

    def test_fetch_large_input_fast(self):
        cur = Cur([vrow("chunk " * 50, f"v{i % 500}") for i in range(20000)], [])
        t = time.perf_counter()
        m.fetch(cur, m.SUBJECTS["cars"], 1)
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_llm_calls_have_timeouts(self):
        _, nj, _ = run_main(["--subject", "cars", "--dry-run"])
        self.assertTrue(all(c.kwargs.get("timeout") for c in nj.call_openrouter.call_args_list))


class TestRetry(unittest.TestCase):
    def test_pg_connect_is_the_retrying_watches_helper(self):
        self.assertIs(m._connect, w._connect)

    def test_news_llm_failure_aborts_not_silent(self):
        rc, nj, _ = run_main(llm=lambda *a, **k: None)
        self.assertEqual(rc, 1)
        nj.publish_hugo.assert_not_called()

    def test_image_failure_non_fatal(self):
        cur = Cur([vrow("t", "v1")], [])
        conn = MagicMock(); conn.cursor.return_value = cur
        nj = MagicMock(); nj.today_str.return_value = "d"; nj.publish_hugo.return_value = True
        nj.call_openrouter.side_effect = lambda *a, **k: "TITLE: Long day here\n" + "w " * 3000
        nj.generate_image.side_effect = RuntimeError("SD down")
        with patch.object(sys, "argv", ["x", "--subject", "cars"]), patch.object(m, "_connect", return_value=conn), \
                patch.object(m, "nj", nj), patch.object(m.nova_voice, "system_prompt", side_effect=lambda c: c):
            self.assertEqual(m.main(), 0)
        self.assertIsNone(nj.publish_hugo.call_args.kwargs["image_path"])


class TestUnit(unittest.TestCase):
    def test_norm_matches_curly_apostrophes_case_and_spaces(self):
        self.assertEqual(m.norm("Tom’s  Guide"), m.norm("tom's guide"))
        self.assertEqual(m.norm('Computer History Archives Project  ("CHAP")'),
                         m.norm('Computer History Archives Project ("CHAP")'))

    def test_channel_lists_disjoint_and_jordans_decisions(self):
        cars, tech = {m.norm(c) for c in m.CARS_CHANNELS}, {m.norm(c) for c in m.TECH_CHANNELS}
        self.assertFalse(cars & tech)
        for c in ("Mark Rober", "Real Engineering", "Veritasium", "Honest HiFi", "Dave's Garage"):
            self.assertIn(m.norm(c), tech)
        for c in ("AMain Hobbies", "1:10 Rod Shop", "TeamAssociatedRC", "Hotrods & Homestead", "CarAudioFabrication"):
            self.assertIn(m.norm(c), cars)
        self.assertNotIn(m.norm("Parkflyers RC"), cars | tech)

    def test_tech_feed_filter_drops_space_and_forum(self):
        ex = m.TECH_FEED_EXCLUDE
        for f in ("NASA Breaking News", "Astronomy Magazine", "EarthSky", "HA Community Latest"):
            self.assertTrue(ex.search(f), f)
        for f in ("Ars Technica", "LWN.net", "9to5Mac", "Lobste.rs", "HA Core Releases"):
            self.assertFalse(ex.search(f), f)

    def test_downloaded_episode_grouped_and_listed_unlinked(self):
        rows = [("[x] part one", D, {"show": "Rob Dahm", "title": "Wing", "source_file": "/v/youtube/Rob Dahm/a.mp4"}),
                ("part two", D, {"show": "Rob Dahm", "title": "Wing", "source_file": "/v/youtube/Rob Dahm/a.mp4"})]
        vids, _ = m.fetch(Cur(rows, []), m.SUBJECTS["cars"], 1)
        self.assertEqual(len(vids), 1)
        self.assertIsNone(vids[0]["url"])
        self.assertEqual(vids[0]["channel"], "Rob Dahm")
        self.assertIn("- Rob Dahm — Wing (2026-10-19)", m.source_list(vids))

    def test_source_list_empty(self):
        self.assertEqual(m.source_list([]), "- (none)")


class TestIntegration(unittest.TestCase):
    def test_cars_query_uses_normalised_channel_list_no_articles(self):
        _, _, cur = run_main(["--subject", "cars", "--dry-run"])
        self.assertEqual(len(cur.calls), 1)             # cars has no article vectors
        self.assertIn(m.norm("Jay Leno's Garage"), cur.calls[0][1][1])

    def test_tech_reads_article_vectors(self):
        _, _, cur = run_main(["--subject", "tech", "--dry-run"])
        self.assertEqual(cur.calls[1][1][1], list(m.SUBJECTS["tech"]["article_vectors"]))

    def test_sources_appended_and_no_fishbowl(self):
        with patch("builtins.print") as pr:
            run_main(["--subject", "tech", "--dry-run"])
        body = pr.call_args.args[0]
        self.assertIn("## Sources\n\n### Videos", body)
        self.assertIn("### Articles\n\n- [Ars Technica — A](https://a/1)", body)
        self.assertNotIn("Fishbowl", body)

    def test_short_issue_gets_deeper_cuts_call(self):
        calls = []

        def llm(system, user, **k):
            calls.append(system)
            return "TITLE: Short day\nshort" if len(calls) == 1 else "## Deeper Cuts\nmore"
        with patch("builtins.print"):
            run_main(["--subject", "cars", "--dry-run"], llm=llm)
        self.assertEqual(len(calls), 2)
        self.assertIn("Deeper Cuts", calls[1])


class TestFunctional(unittest.TestCase):
    def test_golden_cars_publishes_to_cars(self):
        rc, nj, _ = run_main()
        self.assertEqual(rc, 0)
        a = nj.publish_hugo.call_args.args
        self.assertTrue(a[0].startswith("Cars and Friends: Big Day Out"))
        self.assertEqual(a[2], "cars")
        self.assertIn("daily", a[3])
        nj.git_push.assert_called_once()
        self.assertEqual(nj.git_push.call_args.args[0], "cars")

    def test_golden_tech_publishes_to_tech(self):
        rc, nj, _ = run_main(["--subject", "tech"])
        self.assertEqual(rc, 0)
        self.assertEqual(nj.publish_hugo.call_args.args[2], "tech")
        self.assertTrue(nj.publish_hugo.call_args.args[0].startswith("Tech and Friends: "))

    def test_no_news_skips_quietly(self):
        rc, nj, _ = run_main(videos=[], arts=[])
        self.assertEqual(rc, 0)
        nj.call_openrouter.assert_not_called()

    def test_quality_guard_reject_returns_1_without_push(self):
        rc, nj, _ = run_main(publish=False)
        self.assertEqual(rc, 1)
        nj.git_push.assert_not_called()

    def test_dry_run_never_publishes(self):
        with patch("builtins.print"):
            rc, nj, _ = run_main(["--subject", "cars", "--dry-run"])
        self.assertEqual(rc, 0)
        nj.publish_hugo.assert_not_called()

    def test_weekly_title_fallback(self):
        rc, nj, _ = run_main(["--subject", "tech", "--days", "7"], llm=lambda *a, **k: "no title\n" + "w " * 6000)
        self.assertEqual(nj.publish_hugo.call_args.args[0], "Tech and Friends — Week Ending 2026-10-19")


class TestFrame(unittest.TestCase):
    def test_module_shape(self):
        for n in ("main", "fetch", "source_list", "norm"):
            self.assertTrue(callable(getattr(m, n)))
        self.assertEqual(set(m.SUBJECTS), {"cars", "tech"})

    def test_watches_script_untouched_by_the_sibling(self):
        self.assertEqual(w.SECTION, "watches")
        self.assertNotIn("subject", (SCRIPTS / "nova_watches_and_friends.py").read_text())

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_friends.py"), "--help"],
                           capture_output=True, text=True, timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--subject", r.stdout)


if __name__ == "__main__":
    unittest.main()
