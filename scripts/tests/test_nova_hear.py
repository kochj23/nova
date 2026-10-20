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
    def note(self, r=FAKE, text="Loud, steady and euphoric. " * 8):
        seen = []
        with patch.object(h.subprocess, "run", side_effect=fake_download), patch.object(h, "hear", return_value=r), \
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


if __name__ == "__main__":
    unittest.main()
