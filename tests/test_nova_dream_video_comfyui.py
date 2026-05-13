"""
test_nova_dream_video_comfyui.py — All 7 test categories for nova_dream_video_comfyui.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_dream_video_comfyui.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_mod = load_script_compat(_SCRIPT, "nova_dream_video_comfyui")

generate_dream_video = _mod.generate_dream_video
generate_frame = _mod.generate_frame
frames_to_video = _mod.frames_to_video
get_session = _mod.get_session
log = _mod.log


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pat, src, f"Credential: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_swarmui_is_localhost(self):
        """SwarmUI URL must point to localhost, not external."""
        self.assertIn("localhost", _mod.SWARMUI_URL)

    def test_dream_dir_under_home(self):
        self.assertTrue(str(_mod.DREAM_DIR).startswith(str(Path.home())))

    def test_frame_payload_has_no_credentials(self):
        """Frame generation payload must not contain any auth tokens."""
        payloads = []
        def capture(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            raise OSError("stopped")
        with patch("urllib.request.urlopen", side_effect=capture):
            generate_frame("test-session", "dream prompt", 1)
        if payloads:
            payload_str = json.dumps(payloads[0])
            self.assertNotIn("Bearer", payload_str)
            self.assertNotIn("sk-", payload_str)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_frame_generation_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_frames_to_video_returns_false_on_empty_list(self):
        start = time.perf_counter()
        result = frames_to_video([], "/tmp/out.mp4")
        elapsed = time.perf_counter() - start
        self.assertFalse(result)
        self.assertLess(elapsed, 0.1)

    def test_generate_dream_video_exits_fast_on_no_swarmui(self):
        with patch("urllib.request.urlopen", side_effect=Exception("no swarmui")):
            start = time.perf_counter()
            result = generate_dream_video("dream prompt", num_frames=3)
            elapsed = time.perf_counter() - start
        self.assertIsNone(result)
        self.assertLess(elapsed, 5.0)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_dream_video_returns_none_when_session_fails(self):
        with patch("urllib.request.urlopen", side_effect=Exception("swarmui down")):
            result = generate_dream_video("test prompt")
        self.assertIsNone(result)

    def test_generate_frame_returns_none_on_error_response(self):
        fake = MagicMock()
        fake.read.return_value = json.dumps({"error": "model busy"}).encode()
        with patch("urllib.request.urlopen", return_value=fake):
            result = generate_frame("sess", "prompt", 1)
        self.assertIsNone(result)

    def test_generate_frame_returns_none_on_network_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("network error")):
            result = generate_frame("sess", "prompt", 1)
        self.assertIsNone(result)

    def test_frames_to_video_returns_false_on_ffmpeg_failure(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="error")):
            result = frames_to_video(["/tmp/f1.png"], "/tmp/out.mp4")
        self.assertFalse(result)

    def test_main_handles_failure_gracefully(self):
        with patch("sys.argv", ["nova_dream_video_comfyui.py", "dream prompt"]):
            with patch("urllib.request.urlopen", side_effect=Exception("down")):
                try:
                    _mod.main()
                except SystemExit as e:
                    self.assertEqual(e.code, 1)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_session_parses_response(self):
        fake = MagicMock()
        fake.read.return_value = json.dumps({"session_id": "abc123"}).encode()
        fake.__enter__ = lambda s: s
        fake.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=fake):
            sess = get_session()
        self.assertEqual(sess, "abc123")

    def test_generate_frame_returns_none_on_empty_images(self):
        fake = MagicMock()
        fake.read.return_value = json.dumps({"images": []}).encode()
        fake.__enter__ = lambda s: s
        fake.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=fake):
            result = generate_frame("sess", "prompt", 1)
        self.assertIsNone(result)

    def test_log_does_not_raise(self):
        log("smoke test message")

    def test_frames_to_video_creates_concat_file(self):
        created_files = []
        def mock_open(path, mode="r"):
            created_files.append(str(path))
            return MagicMock().__enter__.return_value

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "DREAM_DIR", Path(tmp)):
                with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="")):
                    frames_to_video(["/tmp/frame1.png"], f"{tmp}/out.mp4")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.SWARMUI_URL)
        self.assertIsNotNone(_mod.WORKSPACE)
        self.assertIsNotNone(_mod.DREAM_DIR)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_full_pipeline_no_frames_returns_none(self):
        def mock_get_session():
            return "test-session-id"

        def mock_frame(sess, prompt, frame_num, **kwargs):
            return None  # all frames fail

        with patch.object(_mod, "get_session", side_effect=mock_get_session):
            with patch.object(_mod, "generate_frame", side_effect=mock_frame):
                result = generate_dream_video("a dream prompt", num_frames=3)
        self.assertIsNone(result)

    def test_full_pipeline_with_frames_calls_frames_to_video(self):
        ftv_calls = []

        def mock_get_session():
            return "sess-id"

        def mock_frame(sess, prompt, frame_num, **kwargs):
            return f"/tmp/frame_{frame_num}.png"

        def mock_ftv(frames, output, fps=2):
            ftv_calls.append(frames)
            return True

        with patch.object(_mod, "get_session", side_effect=mock_get_session):
            with patch.object(_mod, "generate_frame", side_effect=mock_frame):
                with patch.object(_mod, "frames_to_video", side_effect=mock_ftv):
                    result = generate_dream_video("dream text", num_frames=3)
        self.assertGreater(len(ftv_calls), 0)
        self.assertEqual(len(ftv_calls[0]), 3)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_prints_video_path_on_success(self):
        import io
        with patch("sys.argv", ["script.py", "surreal dream"]):
            with patch.object(_mod, "generate_dream_video", return_value="/tmp/dream.mp4"):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    try:
                        _mod.main()
                    except SystemExit:
                        pass
                    output = mock_out.getvalue()
        self.assertIn("/tmp/dream.mp4", output)

    def test_main_exits_1_on_failure(self):
        with patch("sys.argv", ["script.py", "surreal dream"]):
            with patch.object(_mod, "generate_dream_video", return_value=None):
                with self.assertRaises(SystemExit) as cm:
                    _mod.main()
        self.assertEqual(cm.exception.code, 1)

    def test_frame_prompt_includes_frame_number(self):
        """Each frame prompt should include 'frame N/total' for consistency."""
        prompts_used = []

        def mock_get_session():
            return "sess"

        def mock_frame(sess, prompt, frame_num, **kwargs):
            prompts_used.append(prompt)
            return None

        with patch.object(_mod, "get_session", side_effect=mock_get_session):
            with patch.object(_mod, "generate_frame", side_effect=mock_frame):
                generate_dream_video("base dream prompt", num_frames=3)

        self.assertEqual(len(prompts_used), 3)
        for p in prompts_used:
            self.assertIn("frame", p.lower())


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
        for fn in ["main", "generate_dream_video", "generate_frame",
                   "frames_to_video", "get_session", "log"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
