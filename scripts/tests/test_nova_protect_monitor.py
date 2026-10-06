#!/usr/bin/env python3
"""Tests for nova_protect_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The UNVR, Keychain, Ollama vision, face/pet recognition, Slack, the
memory server and PG are all mocked; state and snapshots go to a tempdir.
Written by Jordan Koch (via Claude)."""
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
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_protect_monitor.py").read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("protectmon", SCRIPTS / "nova_protect_monitor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pm = _load()
pm.log = MagicMock()
pm.STATE_FILE = Path(_TMP.name) / "state.json"
pm.SNAPSHOT_DIR = Path(_TMP.name) / "snaps"
_REAL = {n: getattr(pm, n) for n in ("slack_post", "slack_upload_image", "vector_remember")}
# every outbound side effect is stubbed at load; individual tests opt back in to the real function
pm.slack_post = MagicMock()
pm.slack_upload_image = MagicMock(return_value=True)
pm.vector_remember = MagicMock()
pm.shared_observe = MagicMock()
pm.handle_package_detection = MagicMock()

EXT = {"id": "c1", "name": "Exterior - Front Middle", "state": "CONNECTED"}
INT = {"id": "c9", "name": "Interior - Living Room", "state": "CONNECTED"}


class _Client:
    def __init__(self, events, cams=(EXT, INT)):
        self.events = events; self.cams = list(cams); self._logged_in = True; self._csrf_token = "t"
        self.thumbs = []

    def get_bootstrap(self):
        return {"cameras": self.cams}

    def get_events(self, since_ms=None, limit=30):
        return self.events


def _ev(cam="c1", ts=None, types=("person",), etype="smartDetectZone", eid="e1"):
    return {"camera": cam, "start": ts or int(time.time() * 1000), "smartDetectTypes": list(types),
            "type": etype, "id": eid}


def _reset():
    for m in (pm.slack_post, pm.slack_upload_image, pm.vector_remember, pm.shared_observe, pm.handle_package_detection):
        m.reset_mock()
    pm.slack_upload_image.return_value = True


class TestSecurity(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_password_from_keychain(self):
        with patch.object(pm.subprocess, "run", return_value=SimpleNamespace(stdout="pw\n")) as r:
            self.assertEqual(pm._get_password(), "pw")
        self.assertEqual(r.call_args[0][0][:2], ["security", "find-generic-password"])
        self.assertIn("nova-unifi-protect-api", r.call_args[0][0])

    def test_interior_cameras_never_touched(self):
        client = _Client([_ev(cam="c9", eid="int1")])
        with patch.object(pm, "_get_event_thumbnail") as th:
            pm.check_motion_events(client, {"last_event_ts": 1})
        th.assert_not_called()
        pm.slack_post.assert_not_called()
        pm.vector_remember.assert_not_called()
        self.assertFalse(pm._is_exterior(INT))


class TestPerformance(unittest.TestCase):
    def test_classifiers_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            pm._observation_severity(["person"] if i % 2 else ["animal"], "motion", hour=i % 24)
            pm._is_exterior({"name": f"Exterior {i}"})
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_401_reauths_once_then_succeeds(self):
        c = pm.ProtectClient(); c._logged_in = True
        err = urllib.error.HTTPError("u", 401, "expired", {}, None)
        ok = SimpleNamespace(read=lambda: b'{"cameras": []}')
        def relogin():
            c._logged_in = True
            return True
        with patch.object(c._opener, "open", side_effect=[err, ok]) as op, \
             patch.object(c, "login", side_effect=relogin) as login:
            self.assertEqual(c.get_bootstrap(), {"cameras": []})
        self.assertEqual(op.call_count, 2)
        login.assert_called_once()

    def test_repeated_401_does_not_loop(self):
        c = pm.ProtectClient(); c._logged_in = True
        err = urllib.error.HTTPError("u", 401, "expired", {}, None)
        with patch.object(c._opener, "open", side_effect=[err, err, err]) as op, \
             patch.object(c, "login", return_value=True):
            self.assertIsNone(c.get_events())
        self.assertEqual(op.call_count, 2)

    def test_helpers_fail_open(self):
        # RETRY GAP: vision / Slack / memory writes are single attempts; failures return None silently
        with patch.object(pm.urllib.request, "urlopen", side_effect=OSError("down")), \
             patch.object(pm.nova_config, "slack_bot_token", return_value="xoxb-x"):
            self.assertIsNone(pm._vision_identify(__file__))
            self.assertIsNone(_REAL["slack_post"]("hi"))
            self.assertIsNone(_REAL["vector_remember"]("hi"))


class TestUnit(unittest.TestCase):
    def test_severity_matrix(self):
        self.assertEqual(pm._observation_severity([], "ring", hour=12), "critical")
        self.assertEqual(pm._observation_severity(["person"], "x", hour=2), "critical")
        self.assertEqual(pm._observation_severity(["person"], "x", hour=14), "warning")
        self.assertEqual(pm._observation_severity(["animal"], "x", hour=14), "warning")
        self.assertEqual(pm._observation_severity(["package"], "x", hour=23), "info")

    def test_state_roundtrip_and_corrupt(self):
        pm.save_state({"last_event_ts": 5, "camera_status": {}})
        self.assertEqual(pm.load_state()["last_event_ts"], 5)
        pm.STATE_FILE.write_text("{bad")
        self.assertEqual(pm.load_state(), {"last_event_ts": 0, "camera_status": {}})

    def test_vehicle_presence_debounced_and_parameterized(self):
        pm._last_vehicle_presence.clear()
        conn = MagicMock()
        with patch("psycopg2.connect", return_value=conn):
            pm._feed_vehicle_presence("Exterior - Front Middle", ["vehicle"])
            pm._feed_vehicle_presence("Exterior - Front Middle", ["vehicle"])   # within 5 min
            pm._feed_vehicle_presence("Exterior - Unknown", ["vehicle"])
        cur = conn.cursor.return_value.__enter__.return_value
        self.assertEqual(cur.execute.call_count, 1)
        self.assertEqual(cur.execute.call_args[0][1][:2], ("jordan", "garage"))


class TestIntegration(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_health_transition_posts_to_photos_channel(self):
        client = _Client([], cams=[dict(EXT, state="DISCONNECTED"), INT])
        state = {"camera_status": {"c1": {"state": "CONNECTED"}}}
        ext = pm.check_camera_health(client, state)
        self.assertEqual([c["id"] for c in ext], ["c1"])
        self.assertIn("went OFFLINE", pm.slack_post.call_args[0][0])
        self.assertNotIn("c9", state["camera_status"])
        self.assertEqual(pm.SLACK_NOTIFY, pm.nova_config.SLACK_PHOTOS)

    def test_shared_observation_payload(self):
        pm._post_shared_observation("Cam", {"person"}, "smartDetectZone", "e1", vision_desc="A man at the door")
        kw = pm.shared_observe.call_args[1]
        self.assertEqual((kw["category"], kw["subject"]), ("security/camera", "Cam"))
        self.assertIn("A man at the door", kw["observation"])


class TestFunctional(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_person_event_uploads_thumbnail_and_remembers(self):
        client = _Client([_ev(types=("person", "vehicle"))])
        state = {"last_event_ts": 1}
        with patch.object(pm, "_get_event_thumbnail", return_value=True), \
             patch.object(pm, "_vision_identify", return_value="A man in a red jacket walking a dog."), \
             patch.object(pm, "_face_recognize", return_value=("Abundio (91%)", [])), \
             patch.object(pm, "_feed_vehicle_presence") as fvp:
            pm.check_motion_events(client, state)
        fvp.assert_called_once()
        comment = pm.slack_upload_image.call_args[1]["comment"]
        self.assertIn("person detected", comment)
        self.assertNotIn("vehicle detected", comment)
        self.assertIn("Abundio", comment)
        pm.slack_post.assert_not_called()
        self.assertIn("Vision: A man", pm.vector_remember.call_args[0][0])
        self.assertGreater(state["last_event_ts"], 1)

    def test_vehicle_only_vision_skips_alert(self):
        client = _Client([_ev(types=("person",))])
        with patch.object(pm, "_get_event_thumbnail", return_value=True), \
             patch.object(pm, "_vision_identify", return_value="A parked sedan and a truck."), \
             patch.object(pm, "_feed_vehicle_presence"):
            pm.check_motion_events(client, {"last_event_ts": 1})
        pm.slack_upload_image.assert_not_called()
        pm.slack_post.assert_not_called()

    def test_main_login_failure_saves_nothing(self):
        if pm.STATE_FILE.exists():
            pm.STATE_FILE.unlink()
        with patch.object(pm.ProtectClient, "login", return_value=False), \
             patch.object(pm, "check_camera_health") as ch:
            pm.main()
        ch.assert_not_called()
        self.assertFalse(pm.STATE_FILE.exists())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # main() logs into the UNVR, so the smoke is an import only (isolated HOME keeps logs untouched)
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_protect_monitor"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
