#!/usr/bin/env python3
"""Tests for nova_camera_presence_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
YOLO (get_model), psycopg2 and signal handlers are mocked; frames live in a tempdir; LOG_FILE is
redirected to that tempdir; the daemon loop is stopped by the mocked sleep."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_camera_presence_poller.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_camera_presence_poller_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cp = _load()


def _box(cls, conf):
    return SimpleNamespace(cls=[cls], conf=[conf])


def _model(boxes):
    return MagicMock(return_value=[SimpleNamespace(boxes=boxes)])


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = [patch.object(cp, "LOG_FILE", self.tmp / "log.log"), patch.object(cp, "FRAME_DIR", self.tmp),
                  patch.object(cp, "_prev_state", {}), patch("builtins.print")]
        for x in self.p:
            x.start()
        self.conn = MagicMock()
        self.cur = self.conn.cursor.return_value.__enter__.return_value
        cp._conn = self.conn
        self.conn.closed = False

    def tearDown(self):
        for x in self.p:
            x.stop()
        cp._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def frame(self, name, age=0):
        f = self.tmp / name
        f.write_bytes(b"jpg")
        t = time.time() - age
        os.utime(f, (t, t))
        return f

    def sqls(self):
        return [c[0][0] for c in self.cur.execute.call_args_list]


class TestSecurity(_Base):
    def test_no_credentials_or_image_storage(self):
        self.assertIsNone(re.search(r"password\s*=", SRC, re.I))
        self.assertNotIn(".save(", SRC)
        self.assertNotIn("imwrite", SRC)

    def test_only_allowlisted_cameras_processed(self):
        self.frame("exterior_driveway_latest.jpg")
        with patch.object(cp, "detect_persons") as d:
            cp.poll_cameras()
        d.assert_not_called()
        self.cur.execute.assert_not_called()


class TestPerformance(_Base):
    def test_10k_detections_parse_fast(self):
        boxes = [_box(i % 3, 0.5) for i in range(10_000)]
        with patch.object(cp, "get_model", return_value=_model(boxes)):
            t0 = time.perf_counter()
            n, conf = cp.detect_persons("x.jpg")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual((n, conf), (3334, 0.5))


class TestRetry(_Base):
    def test_detection_error_per_camera_is_logged_not_fatal(self):
        # RETRY GAP: poll_cameras()/detect_persons — single try per frame per cycle; next poll retries
        self.frame("interior_kitchen_alley_latest.jpg")
        self.frame("interior_front_door_latest.jpg")
        with patch.object(cp, "detect_persons", side_effect=lambda f: (_ for _ in ()).throw(RuntimeError("yolo")) if "kitchen" in str(f) else (1, 0.8)) as d:
            cp.poll_cameras()
        self.assertEqual(d.call_count, 2)
        self.assertIn("Detection error", (self.tmp / "log.log").read_text())
        self.assertEqual(self.cur.execute.call_args_list[0][0][1][0], "hall")

    def test_get_db_reconnects_when_closed(self):
        self.conn.closed = True
        fresh = MagicMock()
        with patch.object(cp.psycopg2, "connect", return_value=fresh) as c:
            self.assertIs(cp.get_db(), fresh)
        c.assert_called_once_with(cp.DB_DSN)


class TestUnit(_Base):
    def test_no_person_is_zero(self):
        with patch.object(cp, "get_model", return_value=_model([_box(2, 0.9)])):
            self.assertEqual(cp.detect_persons("x"), (0, 0.0))

    def test_stale_and_missing_frames_skipped(self):
        self.frame("interior_kitchen_alley_latest.jpg", age=1000)
        with patch.object(cp, "detect_persons") as d:
            cp.poll_cameras()
        d.assert_not_called()

    def test_confidence_capped_at_095(self):
        self.frame("interior_kitchen_alley_latest.jpg")
        with patch.object(cp, "detect_persons", return_value=(2, 0.99)):
            cp.poll_cameras()
        self.assertEqual(self.cur.execute.call_args_list[0][0][1][1], 0.95)


class TestIntegration(_Base):
    def test_rooms_take_max_confidence_across_cameras(self):
        self.frame("interior_living_room_latest.jpg")
        self.frame("interior_lr_front_latest.jpg")
        with patch.object(cp, "detect_persons", side_effect=[(1, 0.5), (1, 0.7)]):
            cp.poll_cameras()
        presence = [c for c in self.cur.execute.call_args_list if "telemetry.presence" in c[0][0]]
        self.assertEqual(len(presence), 1)
        self.assertAlmostEqual(presence[0][0][1][1], 0.77)

    def test_state_change_writes_observation_heartbeat_does_not(self):
        self.frame("interior_kitchen_alley_latest.jpg")
        with patch.object(cp, "detect_persons", return_value=(1, 0.6)):
            cp.poll_cameras()
            self.assertTrue(any("shared_observations" in s for s in self.sqls()))
            self.cur.execute.reset_mock()
            cp.poll_cameras()                     # same state, within 5 min -> nothing
        self.assertEqual(self.sqls(), [])


class TestFunctional(_Base):
    def test_main_runs_one_cycle_and_stops(self):
        def stop(_):
            cp._shutdown = True
        with patch.object(cp.signal, "signal") as sig, patch.object(cp, "get_model") as gm, \
             patch.object(cp, "poll_cameras", side_effect=RuntimeError("boom")) as pc, \
             patch.object(cp.time, "sleep", side_effect=stop):
            try:
                cp.main()
            finally:
                cp._shutdown = False
        self.assertEqual(sig.call_count, 2)
        gm.assert_called_once()
        pc.assert_called_once()
        log = (self.tmp / "log.log").read_text()
        self.assertIn("Poll error: boom", log)
        self.assertIn("Shutdown complete", log)

    def test_signal_handler_sets_shutdown(self):
        try:
            cp._handle_signal(15, None)
            self.assertTrue(cp._shutdown)
        finally:
            cp._shutdown = False


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_camera_presence_poller"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
