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
        for n in ("old.m4a", "older.webm", "new.m4a"):
            (d / n).write_text("x")
        m.keep_only(d, d / "new.m4a")
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["new.m4a"])

    def test_latest_video_skips_live_and_upcoming(self):
        out = "a1\tis_upcoming\tsoon\nb2\tis_live\tnow\nc3\tnot_live\tdone\n"
        with patch.object(m, "_yt", return_value=types.SimpleNamespace(stdout=out, returncode=0)):
            self.assertEqual(m.latest_video("UCx"), ("c3", "done"))

    def test_transcription_is_local_only(self):
        self.assertIn("local_only=True", Path(m.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
