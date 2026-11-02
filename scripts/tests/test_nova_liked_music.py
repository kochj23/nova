"""Tests for nova_liked_music.py and the Nova Speaks song-clip mode — pure logic only."""
import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


lm = _load("nova_liked_music_t", "nova_liked_music.py")
ns = _load("nova_speaks_t", "nova_speaks.py")


class TestMusicFilter(unittest.TestCase):
    def test_podcast_theme_and_episodes_rejected(self):
        for t in ("The Daily Show Podcast Theme Song", "Episode 42 — interview with Bob", "Intro Music for My Show"):
            self.assertTrue(lm.hard_reject({"duration": 200, "title": t}), t)

    def test_real_songs_and_sets_pass(self):
        self.assertIsNone(lm.hard_reject({"duration": 242, "title": "Talk Talk - It's My Life (Official Video)"}))
        self.assertIsNone(lm.hard_reject({"duration": 3600, "title": "DnB Allstars Live Set"}))

    def test_length_bounds(self):
        self.assertTrue(lm.hard_reject({"duration": 20, "title": "x"}))
        self.assertTrue(lm.hard_reject({"duration": 5 * 3600, "title": "x"}))


class TestMusicSignals(unittest.TestCase):
    def test_lyric_video_is_music(self):   # 2026-11-02: model called Roxy Music's lyric video "talking"
        self.assertTrue(lm.plainly_music({"title": "Roxy Music - More Than This (Lyrics)", "channel": "Mica Medusa"}))

    def test_short_dj_track_is_a_song(self):   # DJ ÆDIDIAS - REAL FRIENDS, 4:16
        self.assertEqual(lm.kind_for({"duration": 256}, "dj_set"), "song")


class TestAnalysis(unittest.TestCase):
    def test_key_estimate(self):
        import numpy as np
        self.assertEqual(lm.estimate_key(lm.MAJ), "C major")
        self.assertEqual(lm.estimate_key(list(np.roll(lm.MIN, 9))), "A minor")

    def test_candidates_spaced_and_in_bounds(self):
        cs = lm.candidates(240, [{"start_time": 53, "value": 1.0}], [0.2] * 30 + [0.9] * 90, [60, 120])
        self.assertTrue(cs)
        self.assertTrue(all(3 <= s <= 240 - lm.CLIP_S - 2 for s in cs))
        self.assertTrue(all(b - a >= lm.CLIP_S + 5 for a, b in zip(cs, cs[1:])))

    def test_heatmap_start_artifact_ignored(self):
        # YouTube's heatmap always peaks at 0:00 (autoplay); that is not "the good bit"
        cs = lm.candidates(240, [{"start_time": 0, "value": 1.0}], [0.5] * 120, [])
        self.assertNotIn(0, cs)


class TestCompose(unittest.TestCase):
    def test_article_has_cued_embeds_the_renderer_can_parse(self):
        t = {"id": "cFH5JgyZK1I", "title": "Talk Talk - It's My Life", "artist": "Talk Talk", "track": "It's My Life",
             "kind": "song", "duration": 242, "tempo": 117, "key": "D major",
             "liked_at": datetime(2026, 11, 1, 21, 5, tzinfo=timezone.utc),
             "text": {"analysis": ["A paragraph long enough to count as analysis of the song here."],
                      "highlights": [{"start": 51, "why": "The chorus lands."}], "one_line": "Timeless."}}
        body, desc = lm.compose([t], "in the last 24 hours")
        self.assertIn('{{< youtube id="cFH5JgyZK1I" start="51" end="66"', body)
        self.assertIn("Little Mister", desc)
        chapters = ns.article_to_script("---\ntitle: x\n---\n\n" + body, None)
        clips = [p for _, ps in chapters for p in ps if p.startswith(ns.CLIP)]
        self.assertEqual(len(clips), 1)
        self.assertEqual(ns.parse_clip(clips[0])[:3], ("cFH5JgyZK1I", 51, 66))


class TestSpeaksClipMode(unittest.TestCase):
    def test_clip_runs_keep_order_and_split_chapters(self):
        ch = [("A", ["p1", f"{ns.CLIP} abcdefghijk 1 16 T", "p2"])]
        self.assertEqual(ns.clip_runs(ch), [("A", ["p1"]), (f"{ns.CLIP} abcdefghijk 1 16 T", []), ("A", ["p2"])])

    def test_plain_articles_have_no_clips(self):
        ch = ns.article_to_script("---\ntitle: x\n---\n\n## H\n\nJust words here, nothing cued.", None)
        self.assertFalse(any(p.startswith(ns.CLIP) for _, ps in ch for p in ps))

    def test_clip_file_lives_next_to_review_dir(self):
        self.assertEqual(ns.clip_file("abcdefghijk", 5, 20).parent.name, "clips")


if __name__ == "__main__":
    unittest.main()
