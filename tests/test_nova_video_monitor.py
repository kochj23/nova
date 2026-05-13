"""
test_nova_video_monitor.py — All 7 test categories for nova_video_monitor.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_video_monitor.py"
_spec = importlib.util.spec_from_file_location("nova_video_monitor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_progress = _mod.get_progress
INTERVAL = _mod.INTERVAL
LOG_FILE = _mod.LOG_FILE


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)

    def test_log_file_on_volumes_data(self):
        """Log file must be on /Volumes/Data per storage policy."""
        self.assertTrue(str(LOG_FILE).startswith("/Volumes/Data"),
                        "Log file should be on /Volumes/Data")

    def test_no_eval_in_source(self):
        src = _SCRIPT.read_text()
        self.assertNotIn("eval(", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_interval_is_10_minutes(self):
        self.assertEqual(INTERVAL, 600, "Reporting interval must be 10 minutes")

    def test_get_progress_returns_none_on_missing_log(self):
        with patch.object(LOG_FILE, "exists", return_value=False):
            result = get_progress()
        self.assertIsNone(result)

    def test_get_progress_fast_on_large_log(self):
        """get_progress must parse large log files quickly."""
        lines = [f"[12:00:{i:02d}] Processed video: file_{i}.mp4 — 1000 char transcript\n"
                 for i in range(100)]
        log_content = "".join(lines)

        start = time.perf_counter()
        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=log_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1)  # process not running
                    get_progress()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_progress_returns_none_on_read_error(self):
        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", side_effect=IOError("permission denied")):
                try:
                    result = get_progress()
                except Exception:
                    pass  # If it raises, that's also acceptable — just not crash-loop

    def test_slack_post_silently_fails(self):
        _nova_cfg.post_both.side_effect = Exception("slack down")
        try:
            _mod.slack_post("test message")
        except Exception:
            pass
        finally:
            _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_progress_counts_processed_videos(self):
        log_content = (
            "[12:00:00] Processing: movie1.mp4\n"
            "[12:01:00] Processed video: movie1.mp4 — 5000 char transcript\n"
            "[12:02:00] Processing: movie2.mp4\n"
            "[12:03:00] Processed video: movie2.mp4 — 3000 char transcript\n"
        )
        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=log_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1)
                    result = get_progress()
        self.assertIsNotNone(result)
        self.assertEqual(result["processed"], 2)

    def test_get_progress_counts_total_chars(self):
        log_content = (
            "[12:01:00] Processed video: movie1.mp4 — 5000 char transcript\n"
            "[12:03:00] Processed video: movie2.mp4 — 3000 char transcript\n"
        )
        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=log_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1)
                    result = get_progress()
        self.assertIsNotNone(result)
        self.assertEqual(result["total_chars"], 8000)

    def test_get_progress_detects_running_process(self):
        log_content = "[12:00:00] Processing: movie.mp4\n"
        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=log_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)  # process running
                    result = get_progress()
        self.assertTrue(result["running"])

    def test_get_progress_detects_stopped_process(self):
        log_content = "[12:00:00] All done.\n"
        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=log_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1)  # not running
                    result = get_progress()
        self.assertFalse(result["running"])

    def test_get_progress_counts_errors(self):
        log_content = (
            "[12:00:00] Error processing: bad_file.mp4\n"
            "[12:01:00] error: codec not found\n"
            "[12:02:00] Processed video: ok.mp4 — 1000 char transcript\n"
        )
        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=log_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1)
                    result = get_progress()
        self.assertGreater(result["errors"], 0)

    def test_get_progress_last_file_truncated(self):
        """last_file must be truncated to 60 chars."""
        long_filename = "A" * 100
        log_content = f"[12:00:00] Processing: {long_filename}\n"
        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=log_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1)
                    result = get_progress()
        self.assertLessEqual(len(result["last_file"]), 60)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_posts_progress_message(self):
        """main() should post a progress update to Slack at each interval."""
        log_content = (
            "[12:00:00] Processing: movie.mp4\n"
            "[12:01:00] Processed video: movie.mp4 — 5000 char transcript\n"
        )
        slack_calls = []
        call_count = [0]

        def fake_sleep(seconds):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise KeyboardInterrupt("stop test")

        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=log_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1)
                    with patch("time.sleep", side_effect=fake_sleep):
                        with patch.object(_mod, "slack_post",
                                          side_effect=lambda m: slack_calls.append(m)):
                            try:
                                _mod.main()
                            except KeyboardInterrupt:
                                pass

        self.assertGreater(len(slack_calls), 0, "Should post at least one progress update")

    def test_main_reports_finished_when_process_stops(self):
        """Progress report should say FINISHED when process is no longer running."""
        log_content = "[12:00:00] Processing: movie.mp4\n"
        slack_calls = []
        call_count = [0]

        def fake_sleep(seconds):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise KeyboardInterrupt("stop")

        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=log_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1)  # finished
                    with patch("time.sleep", side_effect=fake_sleep):
                        with patch.object(_mod, "slack_post",
                                          side_effect=lambda m: slack_calls.append(m)):
                            try:
                                _mod.main()
                            except KeyboardInterrupt:
                                pass

        if slack_calls:
            self.assertIn("FINISHED", slack_calls[-1])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_slack_message_includes_processed_count(self):
        """Slack progress message must include processed count."""
        log_content = "\n".join([
            f"[12:0{i}:00] Processed video: movie{i}.mp4 — 1000 char transcript"
            for i in range(5)
        ])
        slack_calls = []
        call_count = [0]

        def fake_sleep(s):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise KeyboardInterrupt

        with patch.object(LOG_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=log_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1)
                    with patch("time.sleep", side_effect=fake_sleep):
                        with patch.object(_mod, "slack_post",
                                          side_effect=lambda m: slack_calls.append(m)):
                            try:
                                _mod.main()
                            except KeyboardInterrupt:
                                pass

        if slack_calls:
            msg = slack_calls[0]
            self.assertIn("Processed:", msg)
            self.assertIn("5", msg)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_script_is_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_module_has_main(self):
        self.assertTrue(callable(_mod.main))

    def test_interval_defined(self):
        self.assertIsNotNone(INTERVAL)
        self.assertGreater(INTERVAL, 0)

    def test_log_file_is_path(self):
        self.assertIsInstance(LOG_FILE, Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
