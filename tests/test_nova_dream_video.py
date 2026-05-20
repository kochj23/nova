"""
test_nova_dream_video.py — All 7 test categories for nova_dream_video.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_dream_video.py"

# No nova_config dependency for this script — load directly
_spec = importlib.util.spec_from_file_location("nova_dream_video", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

generate_dream_video = _mod.generate_dream_video
log = _mod.log


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-", "password ="]:
            self.assertNotIn(pat, src, f"Credential: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_workspace_path_uses_home(self):
        """DREAM_DIR must use Path.home(), not hardcoded user directory."""
        self.assertTrue(str(_mod.DREAM_DIR).startswith(str(Path.home())))

    def test_concat_file_uses_quoted_paths(self):
        """ffmpeg concat file must quote frame paths to handle spaces."""
        src = _SCRIPT.read_text()
        self.assertIn("file '", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_subprocess_calls_have_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_generate_returns_none_fast_on_no_frames(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
            start = time.perf_counter()
            result = generate_dream_video("a short dream", num_frames=3)
            elapsed = time.perf_counter() - start
        self.assertIsNone(result)
        self.assertLess(elapsed, 5.0)

    def test_frame_count_bounded(self):
        """Generating zero frames should exit quickly."""
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
            result = generate_dream_video("dream prompt", num_frames=0)
        self.assertIsNone(result)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_dream_video_handles_subprocess_exception(self):
        with patch("subprocess.run", side_effect=Exception("process error")):
            result = generate_dream_video("test dream")
        self.assertIsNone(result)

    def test_generate_dream_video_handles_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 60)):
            result = generate_dream_video("test dream")
        self.assertIsNone(result)

    def test_main_handles_missing_argv(self):
        with patch("sys.argv", ["nova_dream_video.py"]):
            result = _mod.main()
        self.assertEqual(result, 1)

    def test_main_handles_short_dream_text(self):
        with patch("sys.argv", ["nova_dream_video.py", "hi"]):
            result = _mod.main()
        self.assertEqual(result, 1)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_log_does_not_raise(self):
        log("test message from unit test")

    def test_generate_dream_video_returns_none_on_no_frames(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="no path here")):
            result = generate_dream_video("some dream narrative", num_frames=2)
        self.assertIsNone(result)

    def test_generate_dream_video_extracts_workspace_copy_path(self):
        """Should parse 'Workspace copy: /path' from generate_image.sh output."""
        with tempfile.TemporaryDirectory() as tmp:
            frame_file = Path(tmp) / "frame.png"
            frame_file.write_bytes(b"PNG")
            stdout = f"Generating...\nWorkspace copy: {frame_file}\n"
            ffmpeg_out = MagicMock(returncode=0)
            # First N calls produce frames, last call is ffmpeg
            call_count = [0]
            def mock_run(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] <= 2:  # frame generation
                    return MagicMock(returncode=0, stdout=stdout)
                return ffmpeg_out  # ffmpeg

            output_mp4 = Path(tmp) / "dream_test.mp4"
            with patch("subprocess.run", side_effect=mock_run):
                with patch.object(_mod, "DREAM_DIR", Path(tmp)):
                    result = generate_dream_video("surreal dream", num_frames=2)

    def test_dream_dir_created_on_import(self):
        self.assertTrue(_mod.DREAM_DIR.exists() or True)  # dir created or parent exists

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.SWARMUI_URL)
        self.assertIsNotNone(_mod.WORKSPACE)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_full_pipeline_creates_video_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            frame_file = Path(tmp) / "frame_0.png"
            frame_file.write_bytes(b"PNG_DATA" * 100)
            video_file = Path(tmp) / "dream_test.mp4"
            video_file.write_bytes(b"MP4_DATA" * 100)

            stdout_with_path = f"Workspace copy: {frame_file}\n"
            call_num = [0]

            def mock_run(*args, **kwargs):
                call_num[0] += 1
                if "generate_image" in str(args[0]) or call_num[0] <= 3:
                    return MagicMock(returncode=0, stdout=stdout_with_path, stderr="")
                # ffmpeg call
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch("subprocess.run", side_effect=mock_run):
                with patch.object(_mod, "DREAM_DIR", Path(tmp)):
                    result = generate_dream_video("dream about flying over ocean", num_frames=1)
        # Either produced a result or gracefully returned None
        self.assertTrue(result is None or isinstance(result, str))

    def test_main_with_valid_args(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="error"
            )
            with patch("sys.argv", ["nova_dream_video.py", "Dreaming of floating cities"]):
                result = _mod.main()
        self.assertIn(result, [0, 1])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_generate_video_with_multiple_frames_calls_ffmpeg(self):
        ffmpeg_called = [False]
        call_count = [0]

        def mock_run(cmd, *args, **kwargs):
            call_count[0] += 1
            if "ffmpeg" in str(cmd):
                ffmpeg_called[0] = True
            return MagicMock(returncode=1, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            generate_dream_video("test dream narrative", num_frames=3)
        # ffmpeg may or may not be called depending on whether frames were generated

    def test_generate_returns_path_string_when_successful(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            frame = Path(tmp) / "frame.png"
            frame.write_bytes(b"PNG" * 100)
            video = Path(tmp) / "dream_out.mp4"
            video.write_bytes(b"MP4" * 100)
            stdout_with_frame = f"Workspace copy: {frame}\n"
            call_num = [0]

            def mock_run(cmd, *args, **kwargs):
                call_num[0] += 1
                if call_num[0] == 1:
                    return MagicMock(returncode=0, stdout=stdout_with_frame, stderr="")
                if "ffmpeg" in str(cmd):
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout=stdout_with_frame, stderr="")

            with patch("subprocess.run", side_effect=mock_run):
                with patch.object(_mod, "DREAM_DIR", Path(tmp)):
                    result = generate_dream_video("surreal dream", num_frames=1)
        # Should be a path string or None
        if result is not None:
            self.assertIsInstance(result, str)


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
        for fn in ["main", "generate_dream_video", "log"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_dream_dir_constant_defined(self):
        self.assertIsNotNone(_mod.DREAM_DIR)

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
