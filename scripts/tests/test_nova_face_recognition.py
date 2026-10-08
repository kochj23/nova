#!/usr/bin/env python3
"""Tests for nova_face_recognition.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_face_recognition.py"
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()

import nova_config  # noqa: E402


def _load():
    # the module reads the Slack token at import: keep that off the Keychain
    with patch.object(nova_config, "slack_bot_token", return_value="xoxb-test"):
        spec = importlib.util.spec_from_file_location("nfr_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


fr = _load()
# stub every outbound side effect at module load; state lives in a tempdir
fr.slack_post = MagicMock()
fr.slack_upload_image = MagicMock(return_value=True)
fr.vector_remember = MagicMock()
_ROOT = Path(_TMP.name)
fr.STATE_FILE = _ROOT / "state" / "face.json"
fr.UNKNOWN_DIR = _ROOT / "unknown"
fr.KNOWN_DIR = _ROOT / "known"
fr.CAMERA_FRAMES = _ROOT / "frames"


def _resp(obj):
    m = MagicMock()
    m.__enter__.return_value.read.return_value = json.dumps(obj).encode()
    return m


def _face(name="Jordan", conf=0.9, unknown=False):
    return {"name": name, "confidence": conf, "unknown": unknown,
            "bounding_box": {"top": 10, "right": 50, "bottom": 50, "left": 10}}


class _Base(unittest.TestCase):
    def setUp(self):
        for m in (fr.slack_post, fr.slack_upload_image, fr.vector_remember):
            m.reset_mock()
        if fr.STATE_FILE.exists():
            fr.STATE_FILE.unlink()
        fr.CAMERA_FRAMES.mkdir(parents=True, exist_ok=True)
        self.frame = fr.CAMERA_FRAMES / "front_door_latest.jpg"
        self.frame.write_bytes(b"jpg")
        self.addCleanup(lambda: self.frame.unlink(missing_ok=True))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("SLACK_TOKEN = nova_config.slack_bot_token()", SRC)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        cur = MagicMock(); cur.fetchone.side_effect = [None, None]
        conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
        evil = "x'); DELETE FROM face_people;--"
        with patch.object(fr, "_get_pg", return_value=conn):
            self.assertEqual(fr.update_presence(evil, "Front Door", 90), "arrived")
        for c in cur.execute.call_args_list:
            self.assertNotIn("DELETE", c[0][0])
        conn.close.assert_called_once()

    def test_weak_match_never_named(self):
        self.assertGreaterEqual(fr.MIN_NAME_CONFIDENCE, 65)


class TestPerformance(unittest.TestCase):
    def test_scene_veto_10k_captions(self):
        caps = ["a car in the driveway with no people present", "a man carrying a box"] * 5000
        t0 = time.perf_counter()
        n = sum(fr._scene_has_no_people(c) for c in caps)
        self.assertEqual(n, 5000)
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def setUp(self):
        self.img = _ROOT / "crop.jpg"; self.img.write_bytes(b"x")

    def test_vision_gate_retries_then_answers(self):
        with patch.object(fr.urllib.request, "urlopen",
                          side_effect=[OSError("busy"), _resp({"response": ""}), _resp({"response": "NO"})]) as u, \
             patch("time.sleep") as sl:
            self.assertFalse(fr.looks_like_person(str(self.img)))
        self.assertEqual(u.call_count, 3)
        self.assertEqual(sl.call_count, 2)

    def test_vision_gate_fails_open_after_three(self):
        with patch.object(fr.urllib.request, "urlopen", side_effect=OSError("down")) as u, patch("time.sleep"):
            self.assertTrue(fr.looks_like_person(str(self.img)))
        self.assertEqual(u.call_count, 3)
        self.assertTrue(fr.looks_like_person(str(_ROOT / "missing.jpg")))   # unreadable -> fail-open

    def test_describe_scene_fails_open(self):
        # describe_scene() — 3 attempts (2 s / 4 s), then None (logged)
        with patch.object(fr.urllib.request, "urlopen", side_effect=OSError("down")), patch.object(fr.time, "sleep"):
            self.assertIsNone(fr.describe_scene(str(self.img)))


class TestUnit(unittest.TestCase):
    def test_scene_has_no_people(self):
        self.assertTrue(fr._scene_has_no_people("Nobody is visible"))
        self.assertFalse(fr._scene_has_no_people(""))
        self.assertFalse(fr._scene_has_no_people(None))
        self.assertFalse(fr._scene_has_no_people("two people walking a dog"))

    def test_context_crop_pads_and_upscales(self):
        from PIL import Image
        src = _ROOT / "frame.jpg"
        Image.new("RGB", (400, 300), "white").save(src)
        out = fr.make_context_crop(str(src), {"top": 100, "right": 140, "bottom": 140, "left": 100},
                                   str(_ROOT / "c" / "crop.jpg"))
        self.assertIsNotNone(out)
        self.assertGreaterEqual(max(Image.open(out).size), 320)
        self.assertIsNone(fr.make_context_crop(str(_ROOT / "nope.jpg"), {"top": 0, "right": 1, "bottom": 1, "left": 0},
                                               str(_ROOT / "x.jpg")))

    def test_state_roundtrip_and_corrupt(self):
        fr.save_state({"last_seen": {"a": 1}, "unknown_alerts": {}})
        self.assertEqual(fr.load_state()["last_seen"], {"a": 1})
        fr.STATE_FILE.write_text("{bad")
        self.assertEqual(fr.load_state(), {"last_seen": {}, "unknown_alerts": {}})


class TestIntegration(_Base):
    def test_weak_match_demoted_to_unknown_with_vision_gate(self):
        sam = MagicMock(); sam.identify.return_value = {"face_count": 1, "faces": [_face("Dave", 0.5)]}
        with patch.object(fr, "_load_sam_faces", return_value=sam), \
             patch.object(fr, "make_context_crop", return_value=None), \
             patch.object(fr, "looks_like_person", return_value=True):
            dets = fr.scan_cameras()
        self.assertEqual(dets[0]["type"], "unknown")
        self.assertEqual(dets[0]["weak_match"], "Dave 50%")

    def test_glare_rejected_and_cooldown_respected(self):
        sam = MagicMock(); sam.identify.return_value = {"face_count": 1, "faces": [_face(unknown=True)]}
        with patch.object(fr, "_load_sam_faces", return_value=sam), \
             patch.object(fr, "make_context_crop", return_value=None), \
             patch.object(fr, "looks_like_person", return_value=False):
            self.assertEqual(fr.scan_cameras(), [])
        sam.identify.return_value = {"face_count": 1, "faces": [_face("Jordan", 0.9)]}
        with patch.object(fr, "_load_sam_faces", return_value=sam), patch.object(fr, "make_context_crop", return_value=None):
            self.assertEqual(len(fr.scan_cameras()), 1)
            self.assertEqual(fr.scan_cameras(), [])          # PERSON_COOLDOWN


class TestFunctional(_Base):
    def test_main_golden_path_arrival_and_departure(self):
        dets = [{"type": "known", "name": "Jordan", "camera": "Front Door", "confidence": 90, "crop_path": None}]
        with patch.object(fr, "volumes_ready", return_value=True), \
             patch.object(fr, "scan_cameras", return_value=dets), \
             patch.object(fr, "update_presence", return_value="arrived"), \
             patch.object(fr, "mark_departed", return_value=["Amy"]):
            fr.main()
        posts = [c[0][0] for c in fr.slack_post.call_args_list]
        self.assertTrue(any("Jordan arrived home" in p for p in posts))
        self.assertTrue(any("Amy left home" in p for p in posts))
        kinds = [c[0][1]["type"] for c in fr.vector_remember.call_args_list]
        self.assertEqual(kinds, ["face_known", "presence_arrived", "presence_departed"])

    def test_unknown_suppressed_when_scene_says_no_people(self):
        dets = [{"type": "unknown", "camera": "Alley North", "crop_path": None, "frame_path": str(self.frame)}]
        with patch.object(fr, "describe_scene", return_value="A parked car, no people present."):
            fr.post_detections(dets)
        fr.slack_post.assert_not_called()
        fr.slack_upload_image.assert_not_called()

    def test_missing_sam_faces_skips_run(self):
        with patch.object(fr, "SAM_FACES_DIR", _ROOT / "absent"), patch.object(fr, "scan_cameras") as sc:
            fr.main()
        sc.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        code = ("import sys, runpy, nova_config; nova_config.slack_bot_token = lambda: ''; "
                f"sys.argv = ['x', '--help']; runpy.run_path({str(SCRIPT)!r}, run_name='__main__')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--who-is-home", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertNotIn("\nmain()", SRC)


if __name__ == "__main__":
    unittest.main()
