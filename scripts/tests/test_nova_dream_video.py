#!/usr/bin/env python3
"""Tests for nova_dream_video.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_dream_video.py"
SRC = SCRIPT.read_text()
_HOME = tempfile.TemporaryDirectory()
(Path(_HOME.name) / ".openclaw/workspace").mkdir(parents=True)


def _load():
    """Load with Path.home() pointed at a temp dir so the import-time mkdir never touches the real workspace."""
    spec = importlib.util.spec_from_file_location("nova_dream_video_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", return_value=Path(_HOME.name)):
        spec.loader.exec_module(mod)
    return mod


dv = _load()


def tearDownModule():
    _HOME.cleanup()


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


def _main(argv, run_ret=None):
    with patch.object(sys, "argv", argv), patch.object(dv.subprocess, "run", return_value=run_ret or _cp(1)) as run, \
         redirect_stdout(io.StringIO()) as out:
        rc = dv.main()
    return rc, run, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_prompt_passed_as_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        evil = "a dream; touch /x $(whoami) `id`"
        _, run, _ = _main(["x", evil])
        self.assertEqual(run.call_args[0][0][1], evil)

    def test_writes_stay_under_workspace(self):
        self.assertTrue(str(dv.DREAM_DIR).startswith(_HOME.name))


class TestPerformance(unittest.TestCase):
    def test_frame_parsing_bounded(self):
        out = "\n".join(f"noise line {i}" for i in range(10_000)) + "\nWorkspace copy: /tmp/f.png\n"
        with patch.object(dv.subprocess, "run", side_effect=[_cp(0, out)] * 3 + [_cp(1)]), \
             redirect_stdout(io.StringIO()):
            t0 = time.perf_counter()
            dv.generate_dream_video("p", num_frames=3)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_failed_frames_are_skipped_not_retried(self):
        # RETRY GAP: generate_dream_video — each frame tried once; no frames -> None
        with patch.object(dv.subprocess, "run", return_value=_cp(1)) as run, redirect_stdout(io.StringIO()):
            self.assertIsNone(dv.generate_dream_video("p", num_frames=4))
        self.assertEqual(run.call_count, 4)

    def test_timeout_fails_open(self):
        with patch.object(dv.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 60)), \
             redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(dv.generate_dream_video("p", num_frames=1))
        self.assertIn("timeout", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_usage_and_short_text(self):
        self.assertEqual(_main(["x"])[0], 1)
        rc, run, _ = _main(["x", "short"])
        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_log_format(self):
        with redirect_stdout(io.StringIO()) as out:
            dv.log("hi")
        self.assertTrue(out.getvalue().startswith("[nova_dream_video "))


class TestIntegration(unittest.TestCase):
    def test_uses_generate_image_sh_and_ffmpeg_concat(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"mp4")
                return _cp(0)
            return _cp(0, f"Workspace copy: /tmp/frame{len(calls)}.png\n")
        with patch.object(dv.subprocess, "run", side_effect=fake_run), redirect_stdout(io.StringIO()):
            path = dv.generate_dream_video("ocean of glass", num_frames=2)
        self.assertTrue(calls[0][0].endswith("generate_image.sh"))
        self.assertIn("ocean of glass (frame 1)", calls[0][1])
        self.assertEqual(calls[-1][:3], ["ffmpeg", "-y", "-f"])
        concat = Path(calls[-1][calls[-1].index("-i") + 1]).read_text()
        self.assertEqual(concat.count("duration 2"), 2)
        self.assertTrue(path.endswith(".mp4"))


class TestFunctional(unittest.TestCase):
    def test_main_golden_path_prints_image(self):
        rc, run, out = _main(["x", "a long enough dream narrative"], _cp(0, "Workspace copy: /tmp/d.png\n"))
        self.assertEqual(rc, 0)
        self.assertIn("/tmp/d.png", out)
        self.assertEqual(run.call_args[0][0][2:], ["1024", "576", "20"])

    def test_main_generation_failure(self):
        rc, _, out = _main(["x", "a long enough dream narrative"], _cp(0, "nothing useful"))
        self.assertEqual(rc, 1)
        self.assertIn("failed", out)


class TestFrame(unittest.TestCase):
    def _run(self, args):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".openclaw/workspace").mkdir(parents=True)
            return subprocess.run([sys.executable] + args, cwd=str(SCRIPTS), capture_output=True, text=True,
                                  timeout=30, env={**os.environ, "HOME": td, "NOVA_TEST_QUIET": "1"})

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = self._run(["-c", "import nova_dream_video; print('ok')"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")

    def test_no_args_prints_usage(self):
        r = self._run([str(SCRIPT)])
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage", r.stdout)


if __name__ == "__main__":
    unittest.main()
