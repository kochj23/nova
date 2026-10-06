#!/usr/bin/env python3
"""Tests for nova_camera_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_camera_monitor.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="cam-test-"))

CAMS = {"front": "rtsp://user:s3cretpw@192.168.1.50/stream", "garage": "rtsp://user:s3cretpw@192.168.1.51/stream"}


def _runner(behaviour):
    """behaviour(name) -> returncode | Exception instance to raise. Records every argv."""
    calls = []

    def run(cmd, capture_output=False, timeout=None, **kw):
        calls.append(cmd)
        name = Path(cmd[-1]).name.replace("_latest.jpg", "")
        b = behaviour(name)
        if isinstance(b, BaseException):
            raise b
        return types.SimpleNamespace(returncode=b, stdout=b"", stderr=b"")
    run.calls = calls
    return run


def _exec(cameras, behaviour=lambda n: 0, home=None):
    """The script runs at import: load it fresh under a stubbed camera_config + fake ffmpeg."""
    cfg = types.ModuleType("camera_config"); cfg.CAMERAS = cameras
    spec = importlib.util.spec_from_file_location("cam_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    run = _runner(behaviour)
    out = io.StringIO()
    with patch.dict(sys.modules, {"camera_config": cfg}), patch.dict(os.environ, {"HOME": str(home or TMP)}), \
         patch("subprocess.run", run), redirect_stdout(out):
        spec.loader.exec_module(mod)
    return mod, out.getvalue(), run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_config_is_gitignored(self):
        pat = re.compile(r"(rtsp://\S+:\S+@|password|secret|token)\s*=?\s*['\"][^'\"]{8,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("from camera_config import CAMERAS", SRC)
        ignore = (SCRIPTS.parent / ".gitignore").read_text()
        self.assertIn("scripts/camera_config.py", ignore)

    def test_ffmpeg_is_argv_not_shell_and_urls_never_printed(self):
        self.assertNotIn("shell=True", SRC)
        mod, out, run = _exec(CAMS, lambda n: 1)
        self.assertTrue(all(c[0] == "/opt/homebrew/bin/ffmpeg" for c in run.calls))
        self.assertNotIn("s3cretpw", out)          # a failing camera is reported by name, not by URL


class TestPerformance(unittest.TestCase):
    def test_10k_cameras_under_two_seconds(self):
        cams = {f"cam{i}": f"rtsp://10.0.0.{i % 250}/s" for i in range(10_000)}
        t0 = time.perf_counter()
        mod, out, run = _exec(cams, lambda n: 0)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(run.calls), 10_000)
        self.assertIn("10000/10000 online", out)


class TestRetry(unittest.TestCase):
    def test_timeout_and_errors_fail_open_per_camera(self):
        # RETRY GAP: subprocess.run(ffmpeg) — one grab per camera per run; a stuck camera is 'timeout', not a crash
        def beh(n):
            return {"front": subprocess.TimeoutExpired(["ffmpeg"], 10), "garage": OSError("no ffmpeg")}[n]
        mod, out, run = _exec(CAMS, beh)
        self.assertEqual(mod.status, {"front": "timeout", "garage": "error: no ffmpeg"})
        self.assertEqual(len(run.calls), 2)      # exactly one attempt each, no retry
        self.assertIn("0/2 online", out)


class TestUnit(unittest.TestCase):
    def test_status_mapping_and_output_path(self):
        mod, out, run = _exec(CAMS, lambda n: 0 if n == "front" else 2)
        self.assertEqual(mod.status, {"front": "ok", "garage": "error"})
        self.assertEqual(mod.success_count, 1)
        self.assertTrue(run.calls[0][-1].endswith("/camera_frames/front_latest.jpg"))
        self.assertEqual(mod.FFMPEG, "/opt/homebrew/bin/ffmpeg")

    def test_missing_camera_config_exits_1(self):
        spec = importlib.util.spec_from_file_location("cam_missing_cfg", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"camera_config": None}), patch.dict(os.environ, {"HOME": str(TMP)}), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                spec.loader.exec_module(mod)
        self.assertEqual(cm.exception.code, 1)


class TestIntegration(unittest.TestCase):
    def test_storage_dir_is_created_under_home_and_ffmpeg_args_are_single_frame_tcp(self):
        home = TMP / "home-int"
        mod, out, run = _exec(CAMS, lambda n: 0, home=home)
        self.assertTrue((home / ".openclaw/workspace/camera_frames").is_dir())
        cmd = run.calls[0]
        self.assertEqual(cmd[1:5], ["-rtsp_transport", "tcp", "-i", CAMS["front"]])
        self.assertIn("-frames:v", cmd); self.assertEqual(cmd[cmd.index("-frames:v") + 1], "1")
        self.assertEqual(cmd[-2], "-y")
        self.assertEqual(mod.storage_dir, str(home / ".openclaw/workspace/camera_frames"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_all_online(self):
        mod, out, run = _exec(CAMS, lambda n: 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^\[\d{4}-\d{2}-\d{2}T.*\] Camera monitor: 2/2 online$")

    def test_error_path_lists_only_bad_cameras(self):
        mod, out, run = _exec({**CAMS, "side": "rtsp://x/s"}, lambda n: {"front": 0, "garage": 1}.get(n, subprocess.TimeoutExpired("f", 10)))
        lines = out.strip().splitlines()
        self.assertIn("1/3 online", lines[0])
        self.assertEqual(sorted(l.strip() for l in lines[1:]), ["garage: error", "side: timeout"])


class TestFrame(unittest.TestCase):
    def test_script_runs_offline_with_empty_camera_set(self):
        code = ("import sys, types, runpy\n"
                "cfg = types.ModuleType('camera_config'); cfg.CAMERAS = {}; sys.modules['camera_config'] = cfg\n"
                "import subprocess\n"
                "subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError('ffmpeg spawned'))\n"
                "runpy.run_path(%r, run_name='__main__')\n" % str(SCRIPT))
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP / "frame")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Camera monitor: 0/0 online", r.stdout)
        # top-level script by design (launchd one-shot): no main() to import-guard, so the smoke proves
        # the only side effects are the frame directory + the summary line
        self.assertNotIn("def main", SRC)


if __name__ == "__main__":
    unittest.main()
