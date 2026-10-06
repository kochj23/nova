#!/usr/bin/env python3
"""Tests for nova_dream_movie.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_dream_movie.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_dream_movie_test_"))
DREAM = ("I walked through a library made of water and every book was humming softly. "
         "The ceiling opened into a sky full of slow amber lanterns drifting upward. "
         "A staircase spiraled down into a quiet city where nobody had a face at all. "
         "I found my old phone ringing inside a glass bell jar on the pavement. "
         "The streets folded like paper and I was suddenly standing on the roof again. "
         "Someone handed me a key that was warm like it had just been held for hours. "
         "Then the whole city exhaled and I woke with the taste of rain in my mouth.")


def _load():
    import nova_config
    spec = importlib.util.spec_from_file_location("ndreammovie", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(nova_config, "slack_bot_token", return_value="xoxb-test"), patch("pathlib.Path.mkdir"), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.MOVIE_DIR = TMP
    return mod


dm = _load()


def _router(result):
    m = types.ModuleType("nova_intent_router"); m.query_local = MagicMock(return_value=result)
    return m


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bp]-\d")
        self.assertIn("SLACK_TOKEN   = nova_config.slack_bot_token()", SRC)

    def test_ffmpeg_is_argv_and_image_path_is_not_shell_parsed(self):
        self.assertNotIn("shell=True", SRC)
        with patch.object(dm.subprocess, "run", return_value=MagicMock(returncode=0)) as run, redirect_stdout(io.StringIO()):
            dm.build_scene_clip("/tmp/a; touch pwned.png", {"camera": "push_in", "duration": 2}, 1)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("/tmp/a; touch pwned.png", cmd)


class TestPerformance(unittest.TestCase):
    def test_fallback_scenes_on_huge_dream(self):
        t0 = time.perf_counter()
        scenes = dm._fallback_scenes(DREAM * 1500)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(scenes), 7)


class TestRetry(unittest.TestCase):
    def test_llm_failure_falls_back_to_scenes(self):
        # RETRY GAP: extract_scenes/query_local — one LLM call; failure falls back to sentence split
        router = _router({"success": False, "error": "timeout"})
        with patch.dict(sys.modules, {"nova_intent_router": router}), redirect_stdout(io.StringIO()):
            scenes = dm.extract_scenes(DREAM)
        self.assertEqual(router.query_local.call_count, 1)
        self.assertEqual(scenes[0]["title"], "Scene 1")

    def test_slack_upload_fails_open(self):
        # RETRY GAP: post_movie_to_slack — one getUploadURL attempt; failure returns False
        movie = TMP / "m.mp4"; movie.write_bytes(b"x")
        with patch.object(dm.urllib.request, "urlopen", side_effect=OSError("down")) as uo, redirect_stdout(io.StringIO()):
            self.assertFalse(dm.post_movie_to_slack(str(movie), "t"))
        self.assertEqual(uo.call_count, 1)

    def test_keyframe_failure_returns_none(self):
        with patch.object(dm.urllib.request, "urlopen", side_effect=OSError("down")), redirect_stdout(io.StringIO()):
            self.assertIsNone(dm.generate_keyframe("s", {"visual": "v", "mood": "m"}, 1))


class TestUnit(unittest.TestCase):
    def test_extract_scenes_strips_think_and_fences(self):
        scenes = [{"visual": f"v{i}", "camera": "static", "title": "t", "mood": "m"} for i in range(9)]
        resp = "<think>hmm</think>Sure!\n```json\n" + json.dumps({"scenes": scenes}) + "\n```"
        with patch.dict(sys.modules, {"nova_intent_router": _router({"success": True, "response": resp})}), \
             redirect_stdout(io.StringIO()):
            out = dm.extract_scenes(DREAM)
        self.assertEqual(len(out), 8)                         # capped at 8
        self.assertEqual(out[0]["visual"], "v0")

    def test_extract_scenes_too_few_falls_back(self):
        resp = json.dumps({"scenes": [{"visual": "only one"}]})
        with patch.dict(sys.modules, {"nova_intent_router": _router({"success": True, "response": resp})}), \
             redirect_stdout(io.StringIO()):
            self.assertTrue(dm.extract_scenes(DREAM)[0]["title"].startswith("Scene"))

    def test_fallback_on_short_text(self):
        self.assertEqual(dm._fallback_scenes("tiny."), [])

    def test_unknown_camera_uses_static(self):
        with patch.object(dm.subprocess, "run", return_value=MagicMock(returncode=0)) as run, redirect_stdout(io.StringIO()):
            dm.build_scene_clip("i.png", {"camera": "warp_drive"}, 2)
        self.assertIn("z='1.05'", " ".join(run.call_args[0][0]))

    def test_annotations_resolve(self):
        # regression: generate_keyframe used Optional without importing it
        sig = inspect.signature(dm.generate_keyframe)          # raised NameError before the fix
        self.assertIn("prev_image", sig.parameters)


class TestIntegration(unittest.TestCase):
    def test_assemble_chains_cumulative_xfade_offsets(self):
        probe = MagicMock(returncode=0, stdout=json.dumps({"streams": [{"duration": "5.0"}]}))
        out = TMP / "movie.mp4"
        def run(cmd, **kw):
            if cmd[0] == "ffmpeg":
                out.write_bytes(b"x" * 10)
            return probe
        with patch.object(dm.subprocess, "run", side_effect=run) as r, redirect_stdout(io.StringIO()):
            self.assertTrue(dm.assemble_movie(["a", "b", "c"], str(out), dissolve_frames=24))
        fc = r.call_args_list[-1][0][0]
        fc = fc[fc.index("-filter_complex") + 1]
        self.assertIn("offset=4.000[v1]", fc)
        self.assertIn("offset=8.000[vout]", fc)

    def test_assemble_empty(self):
        self.assertFalse(dm.assemble_movie([], "x"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_builds_and_posts(self):
        scenes = [{"visual": f"v{i}", "camera": "push_in", "mood": "calm", "duration": 4} for i in range(3)]
        with patch.object(dm, "ltx_available", return_value=False), patch.object(dm, "extract_scenes", return_value=scenes), \
             patch.object(dm, "swarmui_session", return_value="sess"), \
             patch.object(dm, "generate_keyframe", side_effect=lambda s, sc, n, p=None: f"/img{n}.png"), \
             patch.object(dm, "build_scene_clip", side_effect=lambda i, s, n: str(TMP / f"clip{n}.mp4")), \
             patch.object(dm, "assemble_movie", return_value=True) as asm, \
             patch.object(dm, "post_movie_to_slack", return_value=True) as post, redirect_stdout(io.StringIO()):
            path = dm.generate_dream_movie(DREAM)
        self.assertTrue(path.startswith(str(TMP)))
        self.assertEqual(len(asm.call_args[0][0]), 3)
        self.assertTrue(post.call_args[0][1].startswith("Dream Movie — "))

    def test_swarmui_down_returns_none_without_posting(self):
        with patch.object(dm, "ltx_available", return_value=False), \
             patch.object(dm, "extract_scenes", return_value=[{"visual": "v"}]), \
             patch.object(dm, "swarmui_session", side_effect=OSError("refused")), \
             patch.object(dm, "post_movie_to_slack") as post, redirect_stdout(io.StringIO()):
            self.assertIsNone(dm.generate_dream_movie(DREAM))
        post.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_smoke_offline(self):
        # No --help: any argv is treated as dream text and would start the pipeline, so smoke the import
        # in a throwaway HOME with the Keychain lookup stubbed.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import nova_config; "
                "nova_config.slack_bot_token = lambda: ''; import nova_dream_movie as m; "
                "assert 'push_in' in m.CAMERA_MOVES; print('ok')")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": tempfile.mkdtemp()})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(sys, "argv", ["x", "a dream"]), patch.object(dm, "generate_dream_movie") as g:
            _load()
        g.assert_not_called()


if __name__ == "__main__":
    unittest.main()
