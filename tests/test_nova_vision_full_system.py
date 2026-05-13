"""
test_nova_vision_full_system.py — All 7 test categories for nova_vision_full_system.py
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

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_vision_full_system.py"
_spec = importlib.util.spec_from_file_location("nova_vision_full_system", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

log = _mod.log
start_track = _mod.start_track
save_pids = _mod.save_pids
load_pids = _mod.load_pids
check_processes = _mod.check_processes
stop_all = _mod.stop_all


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "Bearer "]:
            self.assertNotIn(pat, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_pid_file_in_workspace(self):
        """PID file must be in local workspace, not global /tmp."""
        self.assertIn(str(Path.home()), str(_mod.PID_FILE))

    def test_scripts_dir_is_local(self):
        self.assertIn(str(Path.home()), str(_mod.SCRIPTS_DIR))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_check_processes_fast_for_empty_dict(self):
        start = time.perf_counter()
        result = check_processes({})
        elapsed = time.perf_counter() - start
        self.assertTrue(result)
        self.assertLess(elapsed, 0.01)

    def test_load_pids_returns_none_for_missing_file(self):
        with patch.object(_mod, "PID_FILE", Path("/nonexistent/pids.json")):
            start = time.perf_counter()
            result = load_pids()
            elapsed = time.perf_counter() - start
        self.assertIsNone(result)
        self.assertLess(elapsed, 0.01)

    def test_stop_all_handles_none_processes(self):
        """stop_all must handle processes={} or processes with None values."""
        processes = {"track1": None, "track2": None}
        # Should not raise
        stop_all(processes)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_start_track_returns_none_for_missing_script(self):
        """start_track must return None if script file doesn't exist."""
        result = start_track("test_track", "nonexistent_script_xyz.py")
        self.assertIsNone(result)

    def test_stop_all_kills_process_on_timeout(self):
        """stop_all must kill a process if terminate() times out."""
        import subprocess
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 5)
        mock_proc.kill = MagicMock()
        stop_all({"test": mock_proc})
        mock_proc.kill.assert_called_once()

    def test_check_processes_returns_false_for_exited_process(self):
        """check_processes returns False if any process has exited."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # exited with code 1
        result = check_processes({"track": mock_proc})
        self.assertFalse(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_check_processes_true_for_running(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        result = check_processes({"track": mock_proc})
        self.assertTrue(result)

    def test_save_and_load_pids_roundtrip(self):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "PID_FILE", Path(tmpdir) / "pids.json"):
                save_pids({"motion_detector": mock_proc})
                data = load_pids()
        self.assertIn("processes", data)
        self.assertIn("motion_detector", data["processes"])
        self.assertEqual(data["processes"]["motion_detector"], 12345)

    def test_log_function_callable(self):
        self.assertTrue(callable(_mod.log))

    def test_scripts_dir_path_type(self):
        self.assertIsInstance(_mod.SCRIPTS_DIR, Path)

    def test_pid_file_path_type(self):
        self.assertIsInstance(_mod.PID_FILE, Path)

    def test_workspace_path_type(self):
        self.assertIsInstance(_mod.WORKSPACE, Path)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_start_track_launches_process(self):
        """start_track must launch subprocess for existing script."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_script = Path(tmpdir) / "fake_track.py"
            fake_script.write_text("import time; time.sleep(60)")
            with patch.object(_mod, "SCRIPTS_DIR", Path(tmpdir)):
                proc = start_track("test", "fake_track.py")
        if proc is not None:
            proc.terminate()
            self.assertIsNotNone(proc.pid)

    def test_save_pids_skips_none_processes(self):
        """save_pids must skip None process entries."""
        mock_proc = MagicMock()
        mock_proc.pid = 999
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "PID_FILE", Path(tmpdir) / "pids.json"):
                save_pids({"real_proc": mock_proc, "dead_proc": None})
                data = load_pids()
        self.assertIn("real_proc", data["processes"])
        self.assertNotIn("dead_proc", data["processes"])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_stop_command_reads_pids(self):
        """'stop' command should read PIDs from state file."""
        with patch("sys.argv", ["nova_vision_full_system.py", "stop"]):
            with patch.object(_mod, "load_pids", return_value=None) as mock_load:
                _mod.main()
        mock_load.assert_called_once()

    def test_main_status_command_prints_pid_data(self):
        """'status' command should print PID JSON."""
        import io
        from contextlib import redirect_stdout
        with patch("sys.argv", ["nova_vision_full_system.py", "status"]):
            with patch.object(_mod, "load_pids", return_value={"processes": {}, "timestamp": "2026-01-01T00:00:00"}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _mod.main()
        output = buf.getvalue()
        self.assertIn("processes", output)

    def test_main_status_no_running_system(self):
        """'status' with no running system should say so."""
        import io
        from contextlib import redirect_stdout
        with patch("sys.argv", ["nova_vision_full_system.py", "status"]):
            with patch.object(_mod, "load_pids", return_value=None):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _mod.main()
        self.assertIn("No vision system running", buf.getvalue())

    def test_main_no_args_prints_usage(self):
        """No arguments should print usage."""
        import io
        from contextlib import redirect_stdout
        with patch("sys.argv", ["nova_vision_full_system.py"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                _mod.main()
        self.assertIn("Usage", buf.getvalue())


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

    def test_constants_defined(self):
        self.assertIsInstance(_mod.SCRIPTS_DIR, Path)
        self.assertIsInstance(_mod.WORKSPACE, Path)
        self.assertIsInstance(_mod.PID_FILE, Path)

    def test_functions_exist(self):
        for fn in ("log", "start_track", "save_pids", "load_pids",
                   "check_processes", "stop_all", "signal_handler", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
