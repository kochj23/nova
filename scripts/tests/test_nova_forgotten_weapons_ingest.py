#!/usr/bin/env python3
"""Tests for nova_forgotten_weapons_ingest.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude).

ffmpeg/whisper (subprocess.run), the memory server (urlopen), PG, the media registry and Slack are
mocked for the whole file; the video dir, work dir, state and log files live in a tempdir."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_forgotten_weapons_ingest.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("fw_ingest_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fw = _load()
_TD = tempfile.TemporaryDirectory()
_REAL_RUN = subprocess.run
_PATCHES = []
_VOCAB = ("rifle bolt arsenal prototype cartridge magazine trigger barrel receiver carbine military trial "
          "factory designer patent caliber sight stock action gas piston recoil ammunition soldier army").split()


def _speech(n_words, seed=1):
    """Varied, non-repeating prose (the repetition/trash filters reject copy-pasted text)."""
    import random
    rnd = random.Random(seed)
    return " ".join(rnd.choice(_VOCAB) for _ in range(n_words))


TALK = ("This rifle was developed in nineteen twelve by a small arsenal and only a few hundred were made "
        "before the program was cancelled because the bolt design was too expensive to produce at scale. ")


def setUpModule():
    d = Path(_TD.name)
    (d / "videos").mkdir()
    for p in (patch.object(fw, "FW_DIR", d / "videos"), patch.object(fw, "WORK_DIR", d / "work"),
              patch.object(fw, "STATE_FILE", d / "state.json"), patch.object(fw, "LOG_FILE", d / "fw.log"),
              patch.object(fw, "registry", MagicMock()),
              patch.object(fw.subprocess, "run", side_effect=AssertionError("unmocked ffmpeg/whisper")),
              patch.object(fw.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(psycopg2, "connect", side_effect=OSError("no PG in tests")),
              patch.object(fw.nova_config, "post_both")):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _ok(obj=None):
    r = MagicMock(); r.__enter__.return_value = r
    r.read.return_value = json.dumps(obj or {}).encode()
    return r


def _fake_tools(transcript):
    """subprocess.run stand-in: ffmpeg writes a wav, whisper writes the transcript txt."""
    def run(cmd, **kw):
        if cmd[0] == fw.FFMPEG_BIN:
            Path(cmd[-1]).write_bytes(b"\0" * 2000)
        elif cmd[0] == fw.WHISPER_BIN and transcript is not None:
            out_dir, name = cmd[cmd.index("--output-dir") + 1], cmd[cmd.index("--output-name") + 1]
            Path(out_dir, f"{name}.txt").write_text(transcript)
        return MagicMock(returncode=0)
    return run


class _Fresh(unittest.TestCase):
    def setUp(self):
        fw.registry.reset_mock(); fw.registry.is_done.return_value = False


class TestSecurity(_Fresh):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_memories_local_only_and_sql_parameterized(self):
        with patch.object(fw.urllib.request, "urlopen", return_value=_ok()) as u:
            fw.remember("text", {"k": 1})
        self.assertEqual(json.loads(u.call_args.args[0].data)["privacy"], "local-only")
        self.assertIn("metadata->>'source_file' = %s", SRC)

    def test_tool_invocations_are_argv_lists(self):
        self.assertNotIn("shell=True", SRC)
        evil = Path(_TD.name) / "videos" / "x; echo pwned.mp4"
        with patch.object(fw.subprocess, "run", side_effect=_fake_tools(None)) as run:
            fw.extract_audio(evil, Path(_TD.name) / "o.wav")
        self.assertIn(str(evil), run.call_args.args[0])


class TestPerformance(_Fresh):
    def test_chunk_4k_words_under_bound(self):
        # FINDING: the back-referencing repeat regex costs ~0.1s per 400-word chunk, so the bound is per
        # realistic episode (~4k words / 10 chunks), not 10k items. Whisper dominates the pipeline anyway.
        t0 = time.perf_counter()
        chunks = fw.chunk_text(_speech(4_000))
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(chunks), 10)


class TestRetry(_Fresh):
    def test_remember_fails_open(self):
        # RETRY GAP: remember() — one POST per chunk; failure returns False and the chunk is simply not counted
        self.assertFalse(fw.remember("t", {}))
        self.assertIsNone(fw.recall_fw_memory())

    def test_whisper_timeout_returns_none(self):
        with patch.object(fw.subprocess, "run", side_effect=subprocess.TimeoutExpired("w", 1)), \
                redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(fw.transcribe(Path("x.wav"), "stem"))
        self.assertIn("Whisper timeout", out.getvalue())

    def test_audio_failure_marks_status(self):
        v = Path(_TD.name) / "videos" / "S01E0002 - Broken.mp4"
        with patch.object(fw.subprocess, "run", side_effect=OSError("no ffmpeg")), redirect_stdout(io.StringIO()):
            self.assertFalse(fw.process_video(v, {"done": {}}, None))
        fw.registry.mark_status.assert_called_once_with(str(v), "audio_failed")


class TestUnit(_Fresh):
    def test_trash_detection(self):
        self.assertTrue(fw.is_trash_chunk("too short"))
        self.assertTrue(fw.is_trash_chunk("♪ " + "words " * 20))
        self.assertTrue(fw.is_trash_chunk("[music] " * 30))
        self.assertFalse(fw.is_trash_chunk(TALK))

    def test_truncate_and_sort(self):
        self.assertEqual(fw.truncate_at_boundary("abc"), "abc")
        cut = fw.truncate_at_boundary("word " * 1000)
        self.assertLessEqual(len(cut), 2000)
        self.assertFalse(cut.endswith(" "))
        d = fw.FW_DIR
        for n in ("S02E0001 - B.mp4", "S01E0010 - A.mkv", "notes.txt", "S01E0002 - C.MP4"):
            (d / n).write_text("x")
        try:
            self.assertEqual([p.name for p in fw.find_videos()], ["S01E0002 - C.MP4", "S01E0010 - A.mkv", "S02E0001 - B.mp4"])
        finally:
            for p in d.iterdir():
                p.unlink()


class TestIntegration(_Fresh):
    def test_registry_dedup_short_circuits(self):
        fw.registry.is_done.return_value = True
        state = {"done": {}}
        with patch.object(fw.subprocess, "run") as run:
            self.assertFalse(fw.process_video(Path("/v/S01E0001 - A.mp4"), state, None))
        run.assert_not_called()
        self.assertEqual(state["done"]["/v/S01E0001 - A.mp4"]["status"], "registry_done")

    def test_existing_memories_mark_done_without_transcribing(self):
        cur = MagicMock(); cur.fetchone.return_value = (7,)
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(psycopg2, "connect", return_value=conn), patch.object(fw.subprocess, "run") as run, \
                redirect_stdout(io.StringIO()):
            self.assertTrue(fw.process_video(Path("/v/S01E0001 - A.mp4"), {"done": {}}, None))
        run.assert_not_called()
        fw.registry.mark_ingested.assert_called_once_with("/v/S01E0001 - A.mp4", 7, "military_history")


class TestFunctional(_Fresh):
    def test_golden_path_ingests_and_notifies(self):
        v = Path(_TD.name) / "videos" / "S01E0003 - The Rare Rifle.mp4"
        state = {"done": {}}
        with patch.object(fw.subprocess, "run", side_effect=_fake_tools(_speech(1200))), \
                patch.object(fw.urllib.request, "urlopen", side_effect=lambda req, timeout: _ok({"memories": []})) as u, \
                patch.object(fw.nova_config, "post_both") as post, redirect_stdout(io.StringIO()):
            self.assertTrue(fw.process_video(v, state, "S01E0004 - Next One"))
        posts = [json.loads(c.args[0].data) for c in u.call_args_list if c.args[0].full_url == fw.MEMORY_URL]
        self.assertEqual(len(posts), state["done"][str(v)]["chunks"])
        self.assertEqual(posts[0]["metadata"]["title"], "The Rare Rifle")
        msg = post.call_args.args[0]
        self.assertIn("*Next:* _Next One_", msg)
        self.assertFalse(list(fw.WORK_DIR.glob("*.wav")))          # temp audio cleaned up

    def test_main_nothing_pending(self):
        fw.save_state({"done": {}})
        with patch.object(fw, "find_videos", return_value=[]), patch.object(fw.nova_config, "post_both") as post, \
                redirect_stdout(io.StringIO()):
            fw.main()
        self.assertIn("All 0 videos already in memory", post.call_args.args[0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = _REAL_RUN([sys.executable, "-c", "import nova_forgotten_weapons_ingest"], cwd=str(SCRIPTS),
                      capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
