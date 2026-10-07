#!/usr/bin/env python3
"""Tests for nova_yt_subs_audio.py — coverage filter, latest-only retention, live skipping.
Written by Jordan Koch (via Claude)."""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"nova_ingest": types.ModuleType("nova_ingest")}):
    import nova_yt_subs_audio as m


class TestSubsAudio(unittest.TestCase):
    def test_covered_channels_are_skipped_by_handle_or_id(self):
        src = 'url "https://www.youtube.com/@TimWrite" and id UCabcdefghijklmnopqrstuv'
        handles, ids = m.covered_keys(src)
        subs = [("UCabcdefghijklmnopqrstuv", "@x", "by id"), ("UC" + "z" * 22, "@timwrite", "by handle"),
                ("UC" + "y" * 22, "@new", "uncovered")]
        self.assertEqual([s[2] for s in m.uncovered(subs, handles, ids)], ["uncovered"])

    def test_keep_only_latest_audio(self):
        d = Path(tempfile.mkdtemp())
        for n in ("old.m4a", "older.webm", "new.m4a", ".smbdeleteAAA1", "next.m4a.part"):
            (d / n).write_text("x")
        m.keep_only(d, d / "new.m4a")
        self.assertEqual(sorted(p.name for p in d.iterdir()), [".smbdeleteAAA1", "new.m4a", "next.m4a.part"])

    def test_latest_video_skips_live_and_upcoming(self):
        out = "a1\tis_upcoming\tsoon\nb2\tis_live\tnow\nc3\tnot_live\tdone\n"
        with patch.object(m, "_yt", return_value=types.SimpleNamespace(stdout=out, returncode=0)):
            self.assertEqual(m.latest_video("UCx"), ("c3", "done"))

    def test_transcription_is_local_only(self):
        self.assertIn("local_only=True", Path(m.__file__).read_text())

    def test_channel_folder_is_readable_and_renames_legacy_id_folder(self):
        d = Path(tempfile.mkdtemp())
        with patch.object(m, "AUDIO_DIR", d):
            (d / "UCabc").mkdir()
            (d / "UCabc" / "x.m4a").write_text("1")
            f = m.channel_folder("UCabc", 'Hoovies: Garage / "Cars"?')
        self.assertEqual(f.name, "Hoovies Garage Cars [UCabc]")
        self.assertTrue((f / "x.m4a").exists())
        self.assertFalse((d / "UCabc").exists())

    def test_classify_maps_label_after_thinking_and_falls_back(self):
        def fake(answer):
            return types.SimpleNamespace(read=lambda: ('{"message": {"content": %s}}' % __import__("json").dumps(answer)).encode())
        with patch("urllib.request.urlopen", return_value=fake("hmm... watches </think>\n\nHorology")):
            self.assertEqual(m.classify("Chisholm Hunter", "Omega Bond Seamasters"), "horology")
        with patch("urllib.request.urlopen", return_value=fake("other")):
            self.assertEqual(m.classify("x", "y"), m.FALLBACK_VECTOR)
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(m.classify("x", "y"), m.FALLBACK_VECTOR)


if __name__ == "__main__":
    unittest.main()
