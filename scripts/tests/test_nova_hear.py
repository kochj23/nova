"""nova_hear.describe_track: the '🎧 What it sounds like' note, with YouTube, audio analysis and the writer mocked."""
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nova_hear as h  # noqa: E402

URL = "https://www.youtube.com/watch?v=vZexE7wAvBE"
FAKE = {"duration_s": 254.9, "tempo": {"bpm": 86.1, "half": 43.1, "double": 172.3, "candidates": [86.1, 172.3]},
        "key": {"key": "D# major", "runner_up": "D# minor", "margin": 0.016, "correlation": 0.39},
        "loudness": {"integrated_lufs": -7.8}, "sections": [],
        "clap": {"speech_fraction": 0.0, "vocals": [{"label": "rap vocals", "prob": 0.95}], "genre": []}}


def fake_download(cmd, **k):
    out = re.sub(r"%\(ext\)s$", "wav", cmd[cmd.index("-o") + 1])
    Path(out).write_bytes(b"RIFF")
    return type("P", (), {"stdout": "", "stderr": ""})()


class TestDescribeTrack(unittest.TestCase):
    def note(self, r=FAKE, text="Loud, steady and euphoric. " * 8, vocal=None):
        seen = []
        with patch.object(h.subprocess, "run", side_effect=fake_download), patch.object(h, "hear", return_value=r), \
             patch.object(h, "vocal_guess", return_value=vocal), \
             patch("nova_journal.call_openrouter", side_effect=lambda s, u, **k: seen.append(u) or text):
            return h.describe_track(URL, log=lambda m: None), seen

    def test_note_links_the_analyzed_youtube_video(self):
        note, _ = self.note()
        self.assertTrue(note.startswith("**🎧 What it sounds like:** "))
        self.assertIn(f"[Listen on YouTube]({URL})", note)
        self.assertRegex(note, r"\((https://(www\.)?youtube\.com/watch\?v=|https://youtu\.be/)[\w-]+\)")

    def test_writer_never_sees_vocal_type_and_weak_key_is_ambiguous(self):
        _, seen = self.note()
        self.assertNotIn("rap vocals", seen[0])
        self.assertIn('"key": "ambiguous"', seen[0])

    def test_talk_heavy_audio_gets_no_note(self):
        note, _ = self.note(r={**FAKE, "clap": {**FAKE["clap"], "speech_fraction": 0.56}})
        self.assertIsNone(note)

    def test_writer_out_of_bounds_gets_no_note(self):
        note, _ = self.note(text="Too short.")
        self.assertIsNone(note)


    def test_listening_model_vocal_call_is_labeled_a_guess(self):
        note, _ = self.note(vocal={"lead_vocal": True, "gender": "female", "style": "melodic singing"})
        self.assertIn("*Vocal guess (a listening model, not measured): female, melodic singing lead vocal.*", note)
        self.assertTrue(note.rstrip().endswith(f"[Listen on YouTube]({URL})"))

    def test_listen_service_unreachable_omits_the_vocal_line(self):
        note, _ = self.note(vocal=None)
        self.assertNotIn("Vocal guess", note)
        self.assertIn("[Listen on YouTube]", note)

    def test_vocal_guess_returns_none_when_the_studio_is_down(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            self.assertIsNone(h.vocal_guess(URL, log=lambda m: None))


SEARCH = [  # what a flat ytsearch for "Kosheen Hide U" returns (2026-10-19), trimmed
    {"id": "lcJVf1zN_Fs", "title": "Kosheen ‘Hide U’ | The Making Of A Dance Anthem", "channel": "DJ Mag and Kosheen", "duration": 592},
    {"id": "ASac36m_sWk", "title": "Kosheen - Hide U (Tinlicker Remix) [Official Music Video]", "channel": "Armada Music TV", "duration": 229},
    {"id": "qvmM3Vbg0xM", "title": "Kosheen - Hide U", "channel": "O Porta Luz", "duration": 253},
    {"id": "vZexE7wAvBE", "title": "Kosheen - Hide U (2001) - Official Video (HD)", "channel": "KosheenAtMoksha", "duration": 255},
]
OFFICIAL = "https://www.youtube.com/watch?v=vZexE7wAvBE"


class TestFindOfficial(unittest.TestCase):
    def find(self, results, artist="Kosheen", title="Hide U"):
        return h.find_official(artist, title, log=lambda m: None, search=lambda q: results)

    def test_picks_the_artists_own_upload_over_making_of_remix_and_fan_reupload(self):
        self.assertEqual(self.find(SEARCH), OFFICIAL)

    def test_topic_channel_beats_artist_channel(self):
        topic = {"id": "TOPIC123456", "title": "Hide U", "channel": "Kosheen - Topic", "duration": 240}
        self.assertEqual(self.find(SEARCH + [topic]), "https://www.youtube.com/watch?v=TOPIC123456")

    def test_no_verified_match_returns_none(self):
        self.assertIsNone(self.find([v for v in SEARCH if v["id"] != "vZexE7wAvBE"]))   # only fan/remix/doc left
        self.assertIsNone(self.find([{**SEARCH[3], "duration": 30}]))                   # a 30 s clip is not the track

    def test_reaction_link_is_swapped_for_the_official_upload(self):
        talky = {**FAKE, "clap": {**FAKE["clap"], "speech_fraction": 0.56}}
        analyzed = []
        def hear(path):
            analyzed.append(path)
            return talky if len(analyzed) == 1 else FAKE
        with patch.object(h.subprocess, "run", side_effect=fake_download), patch.object(h, "hear", side_effect=hear), \
             patch.object(h, "vocal_guess", return_value=None), patch.object(h, "_yt_search", return_value=SEARCH), \
             patch("nova_journal.call_openrouter", return_value="Loud, steady and euphoric. " * 8):
            note = h.describe_track("https://www.youtube.com/watch?v=SjS3miaTvlE", log=lambda m: None,
                                    song=lambda: ("Kosheen", "Hide U"))
        self.assertEqual(len(analyzed), 2)
        self.assertIn(f"[Listen on YouTube]({OFFICIAL})", note)
        self.assertNotIn("SjS3miaTvlE", note)

    def test_reaction_without_a_named_song_still_skips(self):
        talky = {**FAKE, "clap": {**FAKE["clap"], "speech_fraction": 0.56}}
        with patch.object(h.subprocess, "run", side_effect=fake_download), patch.object(h, "hear", return_value=talky):
            self.assertIsNone(h.describe_track(URL, log=lambda m: None, song=lambda: None))


if __name__ == "__main__":
    unittest.main()
