#!/usr/bin/env python3
"""Tests for nova_package_clairvoyance.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
Protect thumbnails, Slack upload, nova_notify, nova_logger and the memory server are mocked at module load;
tracker/state/snapshot paths live in a tempdir."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_package_clairvoyance.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="clairvoyance-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("package_clairvoyance_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pc = _load()
# module-level stubs: no Protect, no Slack upload, no notify bus, no log file; paths in a tempdir
pc.TRACKING_FILE = TMP / "package_tracker.json"
pc.STATE_FILE = TMP / "clairvoyance.json"
pc.SNAPSHOT_DIR = TMP / "snaps"
pc.notify = MagicMock()
pc.log = MagicMock()
pc._get_event_thumbnail = MagicMock(return_value=False)
pc.slack_upload_image = MagicMock(return_value=False)

TRACK = {"packages": {
    "a": {"carrier": "UPS", "subject": "New keyboard", "tracking": "1Z", "status": "Out for delivery"},
    "b": {"carrier": "USPS", "subject": "Old thing", "status": "Delivered"},
    "c": {"carrier": "FedEx", "subject": "Cancelled", "status": "cancelled"}}}


def _reset(track=TRACK):
    for f in (pc.STATE_FILE, pc.TRACKING_FILE):
        if f.exists():
            f.unlink()
    if track is not None:
        pc.TRACKING_FILE.write_text(json.dumps(track))
    for m in (pc.notify, pc._get_event_thumbnail, pc.slack_upload_image):
        m.reset_mock()
    pc._get_event_thumbnail.return_value = False
    pc.slack_upload_image.return_value = False


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_only_exterior_cameras_are_mapped(self):
        # interior cameras are a hard redline (nova_protect_monitor.INTERIOR_PREFIX — NEVER access)
        import nova_protect_monitor as npm
        for cam in pc.CAMERA_LOCATIONS:
            self.assertFalse(cam.startswith(npm.INTERIOR_PREFIX), cam)
            self.assertTrue(cam.startswith(("Exterior", "External")), cam)

    def test_thumbnail_deleted_after_upload(self):
        _reset()
        def fake_thumb(client, eid, path):
            Path(path).write_bytes(b"jpg"); return True
        pc._get_event_thumbnail.side_effect = fake_thumb
        pc.slack_upload_image.return_value = True
        try:
            with patch.object(urllib.request, "urlopen"):
                pc.handle_package_detection("Exterior - Front Door Left", "ev1", client=object())
        finally:
            pc._get_event_thumbnail.side_effect = None
        self.assertEqual(list(pc.SNAPSHOT_DIR.glob("*.jpg")), [])
        pc.notify.assert_not_called()                          # the upload carried the alert


class TestPerformance(unittest.TestCase):
    def test_load_10k_packages(self):
        big = {"packages": {str(i): {"carrier": "UPS", "subject": f"p{i}", "status": "In transit"} for i in range(10_000)}}
        _reset(big)
        t0 = time.perf_counter()
        active = pc.load_active_packages()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(active), 10_000)
        with patch.object(urllib.request, "urlopen"):
            pc.handle_package_detection("Exterior - Garbage", None)
        self.assertEqual(pc.notify.call_args[1]["body"].count(":truck:"), 5)   # alert lists at most 5


class TestRetry(unittest.TestCase):
    def test_memory_failure_still_alerts_and_saves(self):
        # RETRY GAP: handle_package_detection memory POST — one attempt, swallowed; alert + state still happen
        _reset()
        with patch.object(urllib.request, "urlopen", side_effect=OSError("mem down")) as m:
            pc.handle_package_detection("External - Carport", "e2")
        self.assertEqual(m.call_count, 1)
        pc.notify.assert_called_once()
        self.assertIn("External - Carport", json.loads(pc.STATE_FILE.read_text())["last_package_events"])

    def test_upload_failure_falls_back_to_notify(self):
        _reset()
        pc._get_event_thumbnail.return_value = True
        with patch.object(urllib.request, "urlopen"):
            pc.handle_package_detection("Exterior - Front Right", "e3", client=object())
        pc.slack_upload_image.assert_called_once()
        pc.notify.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_active_filter(self):
        _reset()
        self.assertEqual([p["key"] for p in pc.load_active_packages()], ["a"])
        _reset(track=None)
        self.assertEqual(pc.load_active_packages(), [])
        pc.TRACKING_FILE.write_text("{bad")
        self.assertEqual(pc.load_active_packages(), [])

    def test_state_roundtrip_and_default(self):
        _reset()
        self.assertEqual(pc.load_state(), {"last_package_events": {}})
        pc.save_state({"last_package_events": {"x": 1}})
        self.assertEqual(pc.load_state()["last_package_events"], {"x": 1})

    def test_slack_post_splits_title(self):
        pc.notify.reset_mock()
        pc.slack_post(":package: *Package Detected!*\n  Camera: X")
        self.assertEqual(pc.notify.call_args[0][0], ":package: *Package Detected!*")
        self.assertEqual(pc.notify.call_args[1]["category"], "package")


class TestIntegration(unittest.TestCase):
    def test_reuses_protect_monitor_helpers(self):
        self.assertIn("from nova_protect_monitor import ProtectClient, _get_event_thumbnail, slack_upload_image", SRC)
        _reset()
        with patch.object(urllib.request, "urlopen") as m:
            pc.handle_package_detection("Exterior - Patio Couch", None)
        body = json.loads(m.call_args[0][0].data)
        self.assertEqual(body["source"], "security")
        self.assertEqual(body["metadata"]["location"], "back patio")


class TestFunctional(unittest.TestCase):
    def test_detection_alert_lists_active_deliveries(self):
        _reset()
        with patch.object(urllib.request, "urlopen"):
            pc.handle_package_detection("Exterior - Front Door Left", "e9")
        body = pc.notify.call_args[1]["body"]
        self.assertIn("Location: front door", body)
        self.assertIn("[UPS] New keyboard — _Out for delivery_", body)
        self.assertNotIn("Old thing", body)
        ev = json.loads(pc.STATE_FILE.read_text())["last_package_events"]["Exterior - Front Door Left"]
        self.assertEqual((ev["event_id"], ev["active_packages"]), ("e9", 1))

    def test_untracked_delivery(self):
        _reset(track={"packages": {}})
        with patch.object(urllib.request, "urlopen"):
            pc.handle_package_detection("Unknown Cam", None)
        self.assertIn("untracked delivery", pc.notify.call_args[1]["body"])


class TestFrame(unittest.TestCase):
    def test_standalone_lists_packages_and_exits_zero(self):
        home = Path(tempfile.mkdtemp(prefix="clairvoyance-home-"))
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("active packages", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(pc.handle_package_detection))


if __name__ == "__main__":
    unittest.main()
