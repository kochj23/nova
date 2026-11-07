#!/usr/bin/env python3
"""Tests for nova_liked_learned.py — the pure helpers (captions, clip moments, topic trend, article composition)
and the "no non-music likes, no article" rule. Offline: the DB, yt-dlp and LLM are stubbed.

    python3 -m pytest tests/test_liked_learned.py -q
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import nova_liked_learned as L  # noqa: E402


class TestCaptions(unittest.TestCase):
    def test_json3_events_joined_and_music_dropped(self):
        d = {"events": [{"tStartMs": 61500, "segs": [{"utf8": "drunk "}, {"utf8": "feel\n"}]},
                        {"tStartMs": 70000, "segs": [{"utf8": "[Music]"}]}, {"tStartMs": 80000}]}
        self.assertEqual(L.parse_json3(json.dumps(d)), [{"start": 61, "text": "drunk feel"}])

    def test_bad_json_is_empty(self):
        self.assertEqual(L.parse_json3("<html>"), [])

    def test_digest_thins_long_transcripts(self):
        caps = [{"start": i * 5, "text": "word " * 10} for i in range(2000)]
        out = L.transcript_digest(caps, 3000)
        self.assertLess(len(out), 3600)
        self.assertIn("[0:00]", out)


class TestMoments(unittest.TestCase):
    def test_heatmap_peak_first_and_spaced(self):
        ms = L.moments(900, [{"start_time": 400, "value": 1.0}])
        self.assertIn(398, ms)
        self.assertTrue(all(b - a >= L.CLIP_S + 5 for a, b in zip(ms, ms[1:])))
        self.assertTrue(all(3 <= s <= 900 - L.CLIP_S - 2 for s in ms))

    def test_tiny_video_is_one_clip_from_zero(self):
        self.assertEqual(L.moments(12, []), [0])

    def test_words_at_window(self):
        caps = [{"start": 10, "text": "a"}, {"start": 26, "text": "b"}, {"start": 40, "text": "c"}]
        self.assertEqual(L.words_at(caps, 10, 26), "a b")


class TestTrend(unittest.TestCase):
    def test_new_growing_constant(self):
        today = [["woodworking", "ai"], ["ai", "cars"]]
        prior = [["ai"]] * 3 + [["cars"]] * 12 + [["food"]] * 5
        t = L.trend(today, prior)
        self.assertEqual(t["new"], ["woodworking"])
        self.assertIn("ai", t["growing"])
        self.assertIn("cars", t["constant"])
        self.assertNotIn("cars", t["growing"])

    def test_exactly_double_share_is_not_growing(self):
        t = L.trend([["cars"]], [["cars"]] * 10 + [["ai"]] * 10)
        self.assertEqual((t["growing"], t["constant"]), ([], ["cars"]))

    def test_empty_prior_everything_new(self):
        self.assertEqual(L.trend([["a"], ["b"]], [])["new"], ["a", "b"])

    def test_norm_topic(self):
        self.assertEqual(L.norm_topic("  Home  Lab!! "), "home lab")


class TestCompose(unittest.TestCase):
    def setUp(self):
        self.v = {"id": "abcdefghijk", "title": 'A "quoted" <title>', "channel": "Chan", "duration": 125, "liked_at": None,
                  "topics": ["music theory"],
                  "text": {"what_it_is": "A lesson on swing.", "takeaways": ["Late snares feel loose."], "why_him": "He plays.",
                           "highlights": [{"start": 30, "why": "The demo."}]}}
        self.tr = L.trend([["music theory"]], [])

    def test_embeds_clip_shortcode_and_escapes(self):
        body, desc = L.compose([self.v], [], {"headline": "Rhythm", "paragraphs": ["He keeps coming back to rhythm and feel."]},
                               self.tr, "in the last 24 hours")
        self.assertIn('{{< youtube id="abcdefghijk" start="30" end="45"', body)
        self.assertNotIn("<title>", body)
        self.assertIn("**New this month:** music theory", body)
        self.assertIn("Little Mister liked 1 video ", body)
        self.assertTrue(desc.startswith("Rhythm"))

    def test_extras_listed(self):
        body, _ = L.compose([self.v], [{"video_id": "zzzzzzzzzzz", "title": "Other", "channel": "C"}], {}, self.tr, "today")
        self.assertIn("## Also liked", body)
        self.assertIn("watch?v=zzzzzzzzzzz", body)

    def test_selftest_passes(self):
        with mock.patch("builtins.print"):
            self.assertEqual(L.selftest(), 0)


class TestScreen(unittest.TestCase):
    """Music never lands here, even when the music job called it "not a track to review"."""

    def test_title_rules(self):
        self.assertFalse(L.listen_rule({"title": "How to Play Music With a 'Drunk' Feel", "channel": "Adam Neely"}))
        self.assertTrue(L.listen_rule({"title": "Theme Song To Cex", "channel": "Cex - Topic"}))
        self.assertIsNone(L.listen_rule({"title": "The Screamers - Eva Braun", "channel": "miodlipowy"}))

    def test_screen_uses_stored_answer_and_skips_music_kinds(self):
        rows = [{"video_id": "a", "kind": "song", "listenable": None},
                {"video_id": "b", "kind": "not_music", "listenable": True},
                {"video_id": "c", "kind": "not_music", "listenable": False}]
        cur = mock.MagicMock()
        with mock.patch.object(L.nlm, "ytdlp") as y:
            self.assertEqual([r["video_id"] for r in L.screen(cur, rows)], ["c"])
            y.assert_not_called()

    def test_screen_asks_once_and_stores(self):
        cur = mock.MagicMock()
        meta = {"title": "Ren - Vincent's Tale", "channel": "Ren", "duration": 480}
        with mock.patch.object(L.nlm, "ytdlp", return_value=json.dumps(meta)), \
             mock.patch.object(L.nlm, "llm_json", return_value={"music": True, "reason": "a song's short film"}):
            self.assertEqual(L.screen(cur, [{"video_id": "r", "kind": None, "listenable": None}]), [])
        self.assertIn('"listenable": true', cur.execute.call_args[0][1][0])

    def test_scrub_and_category_topics(self):
        self.assertEqual(L.scrub(["good vibes only"]), ["good feel only"])
        self.assertEqual(L.norm_topic("People & Blogs"), "")


class TestNoLikesNoArticle(unittest.TestCase):
    """Jordan's rule: no non-music likes -> no article, nothing published."""

    def _conn(self, rows):
        cur = mock.MagicMock()
        cur.fetchall.return_value = rows
        cur.__enter__.return_value = cur
        conn = mock.MagicMock()
        conn.cursor.return_value = cur
        return conn

    def test_only_music_likes_publishes_nothing(self):
        rows = [("vid00000001", "A Song", "Band", 200, None, "song", None),
                ("vid00000002", "A Live Tape", "Fan", 300, None, "not_music", True)]
        with mock.patch.object(L.nlm, "_conn", return_value=self._conn(rows)), \
             mock.patch.object(L, "video") as video, mock.patch.dict(sys.modules, {"nova_journal": None}):
            self.assertEqual(L.run(24, False, 8), 0)
            video.assert_not_called()

    def test_no_likes_at_all_publishes_nothing(self):
        with mock.patch.object(L.nlm, "_conn", return_value=self._conn([])), mock.patch.object(L, "video") as video:
            self.assertEqual(L.run(24, False, 8), 0)
            video.assert_not_called()


if __name__ == "__main__":
    unittest.main()
