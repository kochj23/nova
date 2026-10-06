#!/usr/bin/env python3
"""Tests for nova_face_integration.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_face_integration.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_face_int_test_"))


def _load():
    spec = importlib.util.spec_from_file_location("nfaceint", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.FACES_DIR = TMP / "faces"; mod.UNKNOWN_DIR = TMP / "faces/unknown"; mod.CAMERA_FRAMES = TMP / "frames"
    mod.remember = MagicMock(return_value="id")
    return mod


fi = _load()
SAM_OUT = "loading model...\n" + json.dumps({"face_count": 2, "faces": [
    {"name": "Jordan", "confidence": 0.93, "unknown": False, "position_desc": "left"},
    {"name": "Unknown", "confidence": 0.41, "unknown": True, "position_desc": "right"}]})


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_enroll_name_never_reaches_a_shell(self):
        # regression: name was interpolated into a shell=True string (`--name "{name}"`)
        evil = 'Bob"; touch /tmp/pwned; echo "'
        with patch.object(fi.subprocess, "run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run, \
             redirect_stdout(io.StringIO()):
            fi.enroll_person(evil, "/tmp/x.jpg")
        cmd, kw = run.call_args[0][0], run.call_args.kwargs
        self.assertIsInstance(cmd, list)
        self.assertFalse(kw["shell"])
        self.assertEqual(cmd[cmd.index("--name") + 1], evil)

    def test_identify_path_is_argv(self):
        with patch.object(fi.subprocess, "run", return_value=MagicMock(returncode=1, stdout="", stderr="")) as run:
            fi.identify_faces("/tmp/a b; ls.jpg")
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertIn("/tmp/a b; ls.jpg", run.call_args[0][0])


class TestPerformance(unittest.TestCase):
    def test_many_faces_in_one_frame(self):
        frame = TMP / "big.jpg"; frame.write_bytes(b"x")
        faces = [{"name": f"p{i}", "confidence": 0.9, "unknown": i % 2 == 0} for i in range(10_000)]
        t0 = time.perf_counter()
        with patch.object(fi, "identify_faces", return_value={"face_count": 10_000, "faces": faces}), \
             redirect_stdout(io.StringIO()):
            ev = fi.process_camera_frame("garage", str(frame))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(ev), 10_000)


class TestRetry(unittest.TestCase):
    def test_remember_fails_open(self):
        # RETRY GAP: remember — one POST to the memory server; failure returns None
        fresh = importlib.util.module_from_spec(importlib.util.spec_from_file_location("nfi2", SCRIPT))
        importlib.util.spec_from_file_location("nfi2", SCRIPT).loader.exec_module(fresh)
        with patch.object(fresh.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertIsNone(fresh.remember("x"))
        self.assertEqual(uo.call_count, 1)

    def test_run_command_timeout_and_error(self):
        with patch.object(fi.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertEqual(fi.run_command(["x"]), (124, "", "Timeout"))
        with patch.object(fi.subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(fi.run_command(["x"])[0], 1)


class TestUnit(unittest.TestCase):
    def test_identify_parses_json_after_noise(self):
        with patch.object(fi, "run_command", return_value=(0, SAM_OUT, "")):
            self.assertEqual(fi.identify_faces("/x")["face_count"], 2)
        with patch.object(fi, "run_command", return_value=(0, "no json here", "")):
            self.assertIsNone(fi.identify_faces("/x"))
        with patch.object(fi, "run_command", return_value=(1, "", "err")):
            self.assertIsNone(fi.identify_faces("/x"))

    def test_missing_frame_and_no_faces(self):
        self.assertEqual(fi.process_camera_frame("c", str(TMP / "missing.jpg")), [])
        frame = TMP / "f.jpg"; frame.write_bytes(b"x")
        with patch.object(fi, "identify_faces", return_value={"face_count": 0}):
            self.assertEqual(fi.process_camera_frame("c", str(frame)), [])

    def test_enroll_failure_returns_false(self):
        fi.remember.reset_mock()
        with patch.object(fi, "run_command", return_value=(1, "", "no face")), redirect_stdout(io.StringIO()):
            self.assertFalse(fi.enroll_person("A", "/x"))
        fi.remember.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_frame_to_events_and_memories(self):
        fi.remember.reset_mock()
        frame = TMP / "front_door_latest.jpg"; frame.write_bytes(b"x")
        with patch.object(fi, "run_command", return_value=(0, SAM_OUT, "")), redirect_stdout(io.StringIO()):
            ev = fi.process_camera_frame("front_door", str(frame))
        self.assertEqual([e["status"] for e in ev], ["known", "unknown_detected"])
        texts = [c[0][0] for c in fi.remember.call_args_list]
        self.assertTrue(texts[0].startswith("Jordan spotted at front_door"))
        self.assertIn("Awaiting identification", texts[1])
        self.assertEqual(fi.MEMORY_URL.split(":")[-1], "18790")


class TestFunctional(unittest.TestCase):
    def test_main_scans_latest_frames(self):
        fi.CAMERA_FRAMES.mkdir(parents=True, exist_ok=True)
        (fi.CAMERA_FRAMES / "garage_latest.jpg").write_bytes(b"x")
        with patch.object(fi, "identify_faces", return_value={"face_count": 1, "faces": [{"name": "A", "confidence": 1}]}) as idf, \
             redirect_stdout(io.StringIO()) as out:
            fi.main()
        self.assertEqual(idf.call_count, 1)
        self.assertIn("1 known, 0 unknown", out.getvalue())
        self.assertTrue(fi.UNKNOWN_DIR.exists())

    def test_main_without_frames_dir(self):
        saved = fi.CAMERA_FRAMES; fi.CAMERA_FRAMES = TMP / "nope"
        try:
            with patch.object(fi, "identify_faces") as idf, redirect_stdout(io.StringIO()) as out:
                fi.main()
        finally:
            fi.CAMERA_FRAMES = saved
        idf.assert_not_called()
        self.assertIn("No camera frames directory", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_smoke(self):
        # no --help/--selftest: running it scans real camera frames, so the frame check is the import
        code = "import sys; sys.path.insert(0, sys.argv[1]); import nova_face_integration as m; print(callable(m.main))"
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch("subprocess.run", side_effect=AssertionError("import must not run sam-faces")):
            _load()


if __name__ == "__main__":
    unittest.main()
