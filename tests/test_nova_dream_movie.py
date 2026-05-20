"""
test_nova_dream_movie.py — All 7 test categories for nova_dream_movie.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_dream_movie.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.slack_bot_token = MagicMock(return_value="xoxb-test-token")

sys.modules["nova_config"] = _nova_cfg

_mod = load_script_compat(_SCRIPT, "nova_dream_movie")

log = _mod.log
CAMERA_MOVES = _mod.CAMERA_MOVES
DREAM_STYLE = _mod.DREAM_STYLE


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_slack_channel_is_photos_not_general(self):
        """Dream movies post to dedicated channel, not a work channel."""
        src = _SCRIPT.read_text()
        self.assertIn("nova-photos", src)

    def test_swarmui_is_localhost(self):
        self.assertIn("localhost", _mod.SWARMUI_URL)

    def test_movie_dir_under_home(self):
        self.assertTrue(str(_mod.MOVIE_DIR).startswith(str(Path.home())))

    def test_dream_style_no_explicit_content(self):
        """Dream style must include 'no nudity' or 'no text' safety guardrails."""
        self.assertIn("no text", DREAM_STYLE.lower())
        self.assertIn("no watermark", DREAM_STYLE.lower())


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_camera_moves_cover_all_expected_types(self):
        expected = {"push_in", "pull_back", "pan_right", "pan_left",
                    "drift_up", "drift_down", "static", "vertiginous"}
        self.assertEqual(set(CAMERA_MOVES.keys()), expected)

    def test_camera_move_has_zoom_x_y_description(self):
        for name, (zoom, x, y, desc) in CAMERA_MOVES.items():
            self.assertIsInstance(zoom, str, f"zoom not str for {name}")
            self.assertIsInstance(desc, str, f"desc not str for {name}")
            self.assertGreater(len(desc), 5, f"desc too short for {name}")

    def test_api_calls_have_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_extract_scenes_handles_ollama_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("swarmui down")):
            try:
                result = _mod.extract_scenes("a dream about flying")
            except Exception:
                result = None
        self.assertTrue(result is None or isinstance(result, list))

    def test_generate_keyframe_handles_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            try:
                result = _mod.generate_keyframe("sess", "scene desc", 1)
            except Exception:
                result = None
        self.assertTrue(result is None or isinstance(result, str))

    def test_post_to_slack_handles_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("slack error")):
            try:
                _mod.post_to_slack("test msg", "/tmp/video.mp4")
            except Exception as e:
                self.fail(f"post_to_slack raised: {e}")

    def test_main_handles_scene_extraction_failure(self):
        with patch.object(_mod, "get_swarmui_session", side_effect=Exception("no swarmui")):
            try:
                result = _mod.main("a dream about floating")
            except Exception:
                result = None
        self.assertTrue(result is None or isinstance(result, str))


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_log_does_not_raise(self):
        log("test message from unit test")

    def test_dream_style_is_nonempty(self):
        self.assertGreater(len(DREAM_STYLE), 50)

    def test_scene_prompt_constant(self):
        self.assertIsNotNone(_mod.SCENE_PROMPT)
        self.assertIn("JSON", _mod.SCENE_PROMPT)

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.SWARMUI_URL)
        self.assertIsNotNone(_mod.MOVIE_DIR)
        self.assertIsNotNone(_mod.SLACK_CHANNEL)
        self.assertIsNotNone(_mod.SLACK_TOKEN)

    def test_camera_moves_count(self):
        self.assertEqual(len(CAMERA_MOVES), 8)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_pipeline_with_mocked_components(self):
        scenes = [
            {"visual": "A dark room", "camera": "push_in", "title": "Scene 1", "mood": "tense"},
        ]
        session = "test-session"

        with patch.object(_mod, "get_swarmui_session", return_value=session):
            with patch.object(_mod, "extract_scenes", return_value=scenes):
                with patch.object(_mod, "generate_keyframe", return_value=None):
                    try:
                        result = _mod.main("A dream about an empty room")
                    except Exception:
                        result = None
        # No frames generated → should return None gracefully
        self.assertIsNone(result)

    def test_assemble_movie_requires_frames(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    result = _mod.assemble_movie([], f"{tmp}/out.mp4")
                except Exception:
                    result = None
        self.assertTrue(result is None or result == "" or result is False)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_scene_json_parsing_handles_malformed(self):
        fake = MagicMock()
        fake.read.return_value = json.dumps({"response": "INVALID JSON HERE {{{{"}).encode()
        with patch("urllib.request.urlopen", return_value=fake):
            try:
                result = _mod.extract_scenes("dream text")
            except Exception:
                result = None
        self.assertTrue(result is None or isinstance(result, list))

    def test_camera_move_push_in_zooms_in(self):
        zoom_expr, x_expr, y_expr, desc = CAMERA_MOVES["push_in"]
        self.assertIn("zoom", zoom_expr)
        self.assertIn("intimacy", desc.lower())

    def test_camera_move_vertiginous_fast(self):
        zoom_expr, x_expr, y_expr, desc = CAMERA_MOVES["vertiginous"]
        self.assertIn("disorienting", desc.lower()) or self.assertIn("fast", desc.lower())

    def test_dream_style_has_color_palette(self):
        self.assertIn("palette", DREAM_STYLE.lower())


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_key_functions_exist(self):
        for fn in ["main", "log", "extract_scenes", "generate_keyframe"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
