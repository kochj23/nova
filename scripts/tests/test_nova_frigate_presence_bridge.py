#!/usr/bin/env python3
"""Tests for nova_frigate_presence_bridge.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fb = _load("nova_frigate_presence_bridge_t", SCRIPTS / "nova_frigate_presence_bridge.py")
fb.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=RuntimeError("offline")))   # refuse PG at load
SRC = (SCRIPTS / "nova_frigate_presence_bridge.py").read_text()


def _msg(cam="garage", label="person", typ="new", score=0.91):
    return types.SimpleNamespace(payload=json.dumps(
        {"type": typ, "after": {"camera": cam, "label": label, "top_score": score}}).encode())


def _fake_conn():
    conn = MagicMock(); conn.closed = False
    return conn, conn.cursor.return_value.__enter__.return_value


class _Base(unittest.TestCase):
    def setUp(self):
        fb._last.clear(); fb._conn = None


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", fb.DSN)

    def test_insert_is_parameterized_and_hostile_camera_ignored(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        conn, cur = _fake_conn(); fb._conn = conn
        fb.on_message(None, None, _msg(cam="x'); --"))
        cur.execute.assert_not_called()              # unknown camera -> never written
        self.assertIn("%s", SRC[SRC.index("INSERT INTO telemetry.presence"):][:200])


class TestPerformance(_Base):
    def test_throttle_absorbs_10k_update_events(self):
        conn, cur = _fake_conn(); fb._conn = conn
        m = _msg(typ="update")
        t0 = time.perf_counter()
        for _ in range(10_000):
            fb.on_message(None, None, m)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(cur.execute.call_count, 1)


class TestRetry(_Base):
    def test_main_retries_db_then_subscribes(self):
        conn, _ = _fake_conn()
        connect = MagicMock(side_effect=[RuntimeError("a"), RuntimeError("b"), conn])
        client = MagicMock()
        fake_mqtt = types.SimpleNamespace(Client=MagicMock(return_value=client),
                                          CallbackAPIVersion=types.SimpleNamespace(VERSION2=2))
        with patch.object(fb.psycopg2, "connect", connect), patch.object(fb, "mqtt", fake_mqtt), \
                patch.object(fb.time, "sleep") as sl, redirect_stdout(io.StringIO()):
            fb.main()
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(sl.call_count, 2)
        client.subscribe.assert_called_once_with(fb.EVENTS_TOPIC)
        client.loop_forever.assert_called_once()

    def test_write_failure_reconnects_once_then_succeeds(self):
        bad, bad_cur = _fake_conn(); bad_cur.execute.side_effect = RuntimeError("server gone")
        good, good_cur = _fake_conn()
        fb._conn = bad
        with patch.object(fb.psycopg2, "connect", return_value=good) as c:
            fb.on_message(None, None, _msg())
        self.assertEqual(c.call_count, 1)
        self.assertEqual(good_cur.execute.call_count, 1)

    def test_double_failure_fails_open(self):
        with patch.object(fb.psycopg2, "connect", side_effect=RuntimeError("down")), \
                redirect_stdout(io.StringIO()) as out:
            fb.on_message(None, None, _msg())
        self.assertIn("reconnect failed", out.getvalue())
        self.assertIsNone(fb._conn)


class TestUnit(_Base):
    def test_ignored_events(self):
        conn, cur = _fake_conn(); fb._conn = conn
        fb.on_message(None, None, types.SimpleNamespace(payload=b"not json"))
        fb.on_message(None, None, _msg(typ="end"))
        fb.on_message(None, None, _msg(label="dog"))
        fb.on_message(None, None, _msg(cam="unknown_cam"))
        cur.execute.assert_not_called()

    def test_vehicle_vs_person_methods(self):
        conn, cur = _fake_conn(); fb._conn = conn
        fb.on_message(None, None, _msg(cam="carport", label="truck"))
        fb.on_message(None, None, _msg(cam="interior_front_door", label="person", score=None))
        p1 = cur.execute.call_args_list[0][0][1]; p2 = cur.execute.call_args_list[1][0][1]
        self.assertEqual(p1[:4], ("vehicle", "carport", 0.91, "vehicle_vision"))
        self.assertEqual(p2[:4], ("unknown", "kitchen", 0.7, "camera_vision"))

    def test_drop_conn_tolerates_close_error(self):
        c = MagicMock(); c.closed = False; c.close.side_effect = RuntimeError("x")
        fb._conn = c
        fb._drop_conn()
        self.assertIsNone(fb._conn)


class TestIntegration(_Base):
    def test_writes_rows_the_presence_engine_reads(self):
        conn, cur = _fake_conn(); fb._conn = conn
        fb.on_message(None, None, _msg(cam="back_patio"))
        sql, params = cur.execute.call_args[0]
        self.assertIn("telemetry.presence", sql)
        self.assertEqual(json.loads(params[4]), {"source": "frigate", "camera": "back_patio", "label": "person"})
        eng = (SCRIPTS / "nova_presence_engine.py")
        if eng.exists():
            self.assertIn("camera_vision", eng.read_text())


class TestFunctional(_Base):
    def test_new_event_then_throttle_then_other_label(self):
        conn, cur = _fake_conn(); fb._conn = conn
        fb.on_message(None, None, _msg())
        fb.on_message(None, None, _msg())                       # throttled
        fb.on_message(None, None, _msg(label="car"))            # different key
        self.assertEqual(cur.execute.call_count, 2)
        fb._last[("garage", "person")] -= fb.MIN_INTERVAL_S + 1
        fb.on_message(None, None, _msg())                       # throttle expired
        self.assertEqual(cur.execute.call_count, 3)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_frigate_presence_bridge"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
