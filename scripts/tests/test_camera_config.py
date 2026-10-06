#!/usr/bin/env python3
"""Tests for camera_config.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

camera_config.py is gitignored (it carries stream tokens); the whole file skips when absent."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
PATH = SCRIPTS / "camera_config.py"
HAVE = PATH.exists()


def _load():
    spec = importlib.util.spec_from_file_location("camera_config_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cc = _load() if HAVE else None
SRC = PATH.read_text() if HAVE else ""


def _emit():
    buf = io.StringIO()
    with patch("subprocess.check_output", return_value=b"FAKECODE\n") as co, redirect_stdout(buf):
        cc.emit_frigate()
    return buf.getvalue(), co


@unittest.skipUnless(HAVE, "camera_config.py is gitignored and absent")
class TestSecurity(unittest.TestCase):
    def test_file_is_gitignored(self):
        r = subprocess.run(["git", "check-ignore", "-q", str(PATH)], cwd=str(SCRIPTS), timeout=30)
        self.assertEqual(r.returncode, 0, "camera_config.py holds stream tokens and must stay gitignored")

    def test_bambu_access_code_never_stored(self):
        for cam in cc.BAMBU_CAMERAS.values():
            self.assertEqual(set(cam), {"ip", "serial"})
        self.assertNotRegex(SRC, r"bblp:[A-Za-z0-9]{4,}@")

    def test_access_code_comes_from_keychain(self):
        with patch("subprocess.check_output", return_value=b"SEKRIT\n") as co:
            url = cc._bambu_rtspx({"ip": "10.0.0.5", "serial": "SER1"})
        argv = co.call_args[0][0]
        self.assertEqual(argv[:2], ["security", "find-generic-password"])
        self.assertIn("nova-bambu-SER1", argv)
        self.assertEqual(url, "rtspx://bblp:SEKRIT@10.0.0.5:322/streaming/live/1")


@unittest.skipUnless(HAVE, "camera_config.py is gitignored and absent")
class TestPerformance(unittest.TestCase):
    def test_rtspx_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            cc._rtspx(f"rtsps://10.0.0.1:7441/tok{i}?enableSrtp")
        self.assertLess(time.perf_counter() - t0, 1.0)


@unittest.skipUnless(HAVE, "camera_config.py is gitignored and absent")
class TestRetry(unittest.TestCase):
    def test_keychain_failure_is_one_shot_and_raises(self):
        # RETRY GAP: _bambu_rtspx — a single Keychain lookup; a missing item raises (generator is hand-run,
        # so failing loudly beats emitting a config with a blank access code)
        err = subprocess.CalledProcessError(44, "security")
        with patch("subprocess.check_output", side_effect=err) as co:
            with self.assertRaises(subprocess.CalledProcessError):
                cc._bambu_rtspx({"ip": "1.2.3.4", "serial": "X"})
        self.assertEqual(co.call_count, 1)


@unittest.skipUnless(HAVE, "camera_config.py is gitignored and absent")
class TestUnit(unittest.TestCase):
    def test_rtspx_rewrites_scheme_and_drops_srtp(self):
        self.assertEqual(cc._rtspx("rtsps://1.2.3.4:7441/abc?enableSrtp"), "rtspx://1.2.3.4:7441/abc")
        self.assertEqual(cc._rtspx("rtsps://h/x"), "rtspx://h/x")
        self.assertEqual(cc._rtspx(""), "")

    def test_all_urls_well_formed(self):
        for d in (cc.CAMERAS, cc.SUBSTREAMS):
            for name, url in d.items():
                self.assertRegex(name, r"^[a-z0-9_]+$")
                self.assertTrue(url.startswith("rtsps://") and url.endswith("?enableSrtp"), name)

    def test_substreams_only_for_known_cameras(self):
        self.assertTrue(set(cc.SUBSTREAMS) <= set(cc.CAMERAS))
        self.assertTrue(set(cc.DETECT_DISABLED) <= set(cc.CAMERAS))


@unittest.skipUnless(HAVE, "camera_config.py is gitignored and absent")
class TestIntegration(unittest.TestCase):
    def test_check_composes_emit_and_passes(self):
        buf = io.StringIO()
        with patch("subprocess.check_output", return_value=b"C0DE\n"), redirect_stdout(buf):
            cc._check()
        self.assertIn("chamber cams record-only", buf.getvalue())

    def test_detect_uses_substream_when_present(self):
        cfg, _ = _emit()
        name = sorted(cc.SUBSTREAMS)[0]
        self.assertIn(f"rtsp://127.0.0.1:8554/{name}_sub, input_args: preset-rtsp-restream, roles: [detect]", cfg)


@unittest.skipUnless(HAVE, "camera_config.py is gitignored and absent")
class TestFunctional(unittest.TestCase):
    def test_emit_frigate_golden_path(self):
        cfg, co = _emit()
        self.assertTrue(cfg.startswith("go2rtc:\n  streams:"))
        self.assertIn("\ncameras:\n", cfg)
        self.assertEqual(co.call_count, len(cc.BAMBU_CAMERAS))
        self.assertEqual(cfg.count("roles: [record]"), len(cc.CAMERAS) + len(cc.BAMBU_CAMERAS))
        self.assertNotIn("enableSrtp", cfg)

    def test_detect_disabled_camera_is_record_only(self):
        name = sorted(cc.CAMERAS)[0]
        base, _ = _emit()
        with patch.object(cc, "DETECT_DISABLED", {name}):
            cfg, _ = _emit()
        self.assertEqual(cfg.count("roles: [detect]"), base.count("roles: [detect]") - 1)
        self.assertNotIn(f"8554/{name}, input_args: preset-rtsp-restream, roles: [detect]", cfg)


@unittest.skipUnless(HAVE, "camera_config.py is gitignored and absent")
class TestFrame(unittest.TestCase):
    def test_default_invocation_exits_zero_without_keychain(self):
        r = subprocess.run([sys.executable, str(PATH)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--emit-frigate", r.stdout)

    def test_import_has_main_guard(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
