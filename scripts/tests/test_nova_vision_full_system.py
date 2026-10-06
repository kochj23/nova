#!/usr/bin/env python3
"""Tests for nova_vision_full_system.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Popen/os.kill/os.chmod/subprocess.run are mocked and the
scripts dir + PID file live in a tempdir: no process is started, signalled or chmod-ed.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_vision_full_system.py"
SRC = SCRIPT.read_text()
TRACKS = ("nova_motion_detector_live.py", "nova_homekit_occupancy.py", "nova_claude_vision_analyzer.py")


def _load():
    spec = importlib.util.spec_from_file_location("nova_vision_full_system_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vf = _load()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        for name in TRACKS:
            (t / name).write_text("print('x')\n")
        self.pidfile = t / "pids.json"
        self.ps = [patch.object(vf, "SCRIPTS_DIR", t), patch.object(vf, "PID_FILE", self.pidfile),
                   patch.object(vf.os, "chmod"), patch.object(vf.os, "kill"),
                   patch.object(vf.subprocess, "Popen"), patch.object(vf.subprocess, "run"),
                   patch.object(vf.signal, "signal")]
        started = [p.start() for p in self.ps]
        self.chmod, self.kill, self.popen, self.run = started[2:6]
        self._r = redirect_stdout(io.StringIO())
        self.out = self._r.__enter__()

    def tearDown(self):
        self._r.__exit__(None, None, None)
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()

    def main(self, *argv):
        with patch.object(sys, "argv", ["x", *argv]):
            vf.main()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")
        self.assertNotIn("shell=True", SRC)

    def test_missing_script_never_started_or_chmodded(self):
        self.assertIsNone(vf.start_track("evil", "../../bin/not_there.py"))
        self.popen.assert_not_called()
        self.chmod.assert_not_called()

    def test_stop_only_signals_recorded_pids_with_sigterm(self):
        self.pidfile.write_text(json.dumps({"processes": {"motion": 111, "hk": 222}}))
        self.main("stop")
        self.assertEqual([c[0] for c in self.kill.call_args_list], [(111, signal.SIGTERM), (222, signal.SIGTERM)])


class TestPerformance(unittest.TestCase):
    def test_check_many_processes(self):
        procs = {f"p{i}": MagicMock(poll=MagicMock(return_value=None)) for i in range(10_000)}
        t0 = time.perf_counter()
        self.assertTrue(vf.check_processes(procs))
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(_Base):
    def test_stop_tolerates_missing_and_failing_pids(self):
        # RETRY GAP: stop — one SIGTERM per pid, no retry; ProcessLookupError/other errors are logged
        self.pidfile.write_text(json.dumps({"processes": {"gone": 1, "denied": 2}}))
        self.kill.side_effect = [ProcessLookupError(), PermissionError("nope")]
        self.main("stop")
        self.assertIn("gone not running", self.out.getvalue())
        self.assertIn("Error stopping denied", self.out.getvalue())

    def test_stop_all_kills_after_terminate_timeout(self):
        p = MagicMock()
        p.wait.side_effect = subprocess.TimeoutExpired("x", 5)
        vf.stop_all({"a": p, "b": None})
        p.terminate.assert_called_once()
        p.kill.assert_called_once()


class TestUnit(_Base):
    def test_load_pids_edges(self):
        self.assertIsNone(vf.load_pids())
        self.pidfile.write_text("{bad")
        self.assertIsNone(vf.load_pids())

    def test_check_processes_detects_exit(self):
        self.assertTrue(vf.check_processes({"a": None}))
        self.assertFalse(vf.check_processes({"a": MagicMock(poll=MagicMock(return_value=1), pid=9)}))


class TestIntegration(_Base):
    def test_start_track_popen_and_pid_file(self):
        self.popen.return_value = MagicMock(pid=4242)
        proc = vf.start_track("Motion", TRACKS[0], ["--x"])
        self.assertEqual(self.popen.call_args[0][0], ["python3", str(vf.SCRIPTS_DIR / TRACKS[0]), "--x"])
        vf.save_pids({"motion": proc, "dead": None})
        self.assertEqual(json.loads(self.pidfile.read_text())["processes"], {"motion": 4242})


class TestFunctional(_Base):
    def test_start_runs_three_tracks_then_stops_when_one_dies(self):
        procs = [MagicMock(pid=i, poll=MagicMock(return_value=None if i else 1)) for i in range(3)]
        self.popen.side_effect = procs
        with patch.object(vf.time, "sleep"):
            self.main("start")
        self.assertEqual(self.popen.call_count, 3)
        self.assertEqual(len(json.loads(self.pidfile.read_text())["processes"]), 3)
        for p in procs:
            p.terminate.assert_called_once()
        self.assertIn("Vision system stopped", self.out.getvalue())

    def test_reports_and_unknown_command(self):
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout="occupied: 2\n", stderr="")
        self.main("occupancy")
        self.assertIn("occupied: 2", self.out.getvalue())
        self.main("daily-report")
        self.assertEqual(self.run.call_args[0][0][-1], "daily")
        self.main("bogus")
        self.assertIn("Usage:", self.out.getvalue())


class TestFrame(unittest.TestCase):
    def test_no_args_prints_usage(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Usage: nova_vision_full_system.py", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(vf.subprocess, "Popen") as p:
            _load()
        p.assert_not_called()


if __name__ == "__main__":
    unittest.main()
