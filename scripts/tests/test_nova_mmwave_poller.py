#!/usr/bin/env python3
"""Tests for nova_mmwave_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_mmwave_poller.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _load("mmwave_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="mmwave-test-"))
P.LOG_FILE = TMP / "mmwave.log"


class _Cur:
    def __init__(self): self.sql, self.params = [], []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sql.append(" ".join(sql.split())); self.params.append(params)


class _Conn:
    def __init__(self, cur=None, fail=False):
        self.cur = cur or _Cur(); self.commits = 0; self.closed = False; self.fail = fail
    def cursor(self):
        if self.fail:
            raise RuntimeError("db down")
        return self.cur
    def commit(self): self.commits += 1
    def close(self): self.closed = True


class _Headers(dict):
    pass


def _handler(path, body=b"", ctype="application/json", method="POST"):
    """A PresenceHandler with no socket: headers/rfile/wfile are plain objects."""
    h = P.PresenceHandler.__new__(P.PresenceHandler)
    h.path = path; h.headers = _Headers({"Content-Length": str(len(body)), "Content-Type": ctype})
    h.rfile = io.BytesIO(body); h.wfile = io.BytesIO(); h.codes = []
    h.send_response = lambda code: h.codes.append(code)
    h.send_header = lambda *a: None; h.end_headers = lambda: None
    return h


def _body(h):
    return json.loads(h.wfile.getvalue().decode())


class _Base(unittest.TestCase):
    def setUp(self):
        P._last_state.clear(); P._event_count = 0
        self.conn = _Conn()
        p = mock.patch("psycopg2.connect", return_value=self.conn); self.connect = p.start(); self.addCleanup(p.stop)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        P.write_presence_sync("office", True, 0.95, metadata={"x": 1})
        for sql, params in zip(self.conn.cur.sql, self.conn.cur.params):
            self.assertIn("%s", sql); self.assertIsInstance(params, tuple)
        self.assertEqual(self.conn.cur.params[0][0], "office")
        self.assertEqual(json.loads(self.conn.cur.params[0][2]), {"x": 1})

    def test_room_is_normalised_and_required(self):
        h = _handler("/presence", json.dumps({"room": "Living Room", "presence": "yes"}).encode())
        with mock.patch.object(P, "process_presence_update", return_value=True) as ppu:
            h.do_POST()
        self.assertEqual(ppu.call_args.args[0], "living_room")
        h = _handler("/presence", json.dumps({"presence": True}).encode()); h.do_POST()
        self.assertEqual(h.codes, [400]); self.assertIn("room", _body(h)["error"])


class TestPerformance(_Base):
    def test_10k_presence_updates_with_db_mocked(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            P.process_presence_update(f"room{i % 4}", bool(i & 1), source="perf")
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(P._last_state), 4)


class TestRetry(_Base):
    def test_db_write_errors_are_swallowed(self):
        # RETRY GAP: write_presence_sync()/write_observation_sync() — one attempt, error logged, no retry
        self.connect.return_value = _Conn(fail=True)
        P.write_presence_sync("office", True, 0.95)
        P.write_observation_sync("office", "enter")
        self.assertEqual(self.connect.call_count, 2)
        self.assertEqual(P._event_count, 0)
        self.assertIn("DB write error", P.LOG_FILE.read_text())

    def test_connect_failure_is_one_shot(self):
        self.connect.side_effect = OSError("no route")
        P.write_presence_sync("office", True, 0.95)
        self.assertEqual(self.connect.call_count, 1)

    def test_shortcut_poll_fails_open(self):
        # RETRY GAP: run_shortcut_poll() — FileNotFoundError/timeout/bad JSON all return False once
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("shortcuts")):
            self.assertFalse(P.run_shortcut_poll())
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "not json", "")):
            self.assertFalse(P.run_shortcut_poll())


class TestUnit(_Base):
    def test_write_presence_never_touches_presence_state(self):
        # presence_state has exactly one writer: nova_presence_engine (2026-10-08)
        P.write_presence_sync("office", False, 0.05)
        P.write_presence_sync("office", True, 0.95)
        self.assertEqual(len(self.conn.cur.sql), 2)
        self.assertTrue(all("INSERT INTO telemetry.presence" in q for q in self.conn.cur.sql))
        self.assertEqual((self.conn.commits, P._event_count, self.conn.closed), (2, 2, True))

    def test_process_presence_update_state_machine(self):
        with mock.patch.object(P.time, "time", return_value=1000.0):
            self.assertTrue(P.process_presence_update("office", True, {"z1": True}, "webhook"))
            self.assertIn("INSERT INTO shared_observations", self.conn.cur.sql[-1])
            self.assertEqual(self.conn.cur.params[-1], ("room_office", "mmWave: enter detected in office"))
            n = len(self.conn.cur.sql)
            self.assertFalse(P.process_presence_update("office", True, source="webhook"))   # unchanged, <60 s
            self.assertEqual(len(self.conn.cur.sql), n)
        with mock.patch.object(P.time, "time", return_value=1061.0):
            self.assertFalse(P.process_presence_update("office", True))                     # heartbeat: write, no observation
            self.assertEqual(len(self.conn.cur.sql), n + 1)                                   # telemetry row only
            self.assertTrue(P.process_presence_update("office", False))                     # leave
            self.assertEqual(self.conn.cur.params[-1][1], "mmWave: leave detected in office")
        self.assertEqual(P._last_state["office"]["presence"], False)

    def test_do_post_form_and_errors(self):
        h = _handler("/presence", b"room=patio&occupied=1", "application/x-www-form-urlencoded"); h.do_POST()
        self.assertEqual(h.codes, [200]); self.assertEqual(_body(h), {"ok": True, "room": "patio", "presence": True, "changed": True})
        h = _handler("/presence", b"{not json"); h.do_POST()
        self.assertEqual(h.codes, [400]); self.assertEqual(_body(h)["error"], "Invalid JSON")
        h = _handler("/other", b"{}"); h.do_POST()
        self.assertEqual(h.codes, [404])
        for v, want in (("detected", True), ("off", False), (0, False), (True, True)):
            h = _handler("/presence", json.dumps({"room": "x", "presence": v}).encode()); h.do_POST()
            self.assertEqual(_body(h)["presence"], want, v)

    def test_do_get(self):
        P.process_presence_update("office", True)
        h = _handler("/health"); h.do_GET()
        b = _body(h)
        self.assertEqual((h.codes, b["status"], b["occupied"], b["rooms_tracked"]), ([200], "ok", ["office"], 1))
        self.assertEqual(b["version"], P.VERSION)
        h = _handler("/state"); h.do_GET(); self.assertIn("office", _body(h))
        h = _handler("/nope"); h.do_GET(); self.assertEqual(h.codes, [404])

    def test_run_shortcut_poll_success(self):
        cp = subprocess.CompletedProcess([], 0, json.dumps({"Living Room": True, "office": False}), "")
        with mock.patch("subprocess.run", return_value=cp) as run:
            self.assertTrue(P.run_shortcut_poll())
        self.assertEqual(run.call_args.args[0], ["shortcuts", "run", "Nova FP2 Presence"])
        self.assertEqual(P._last_state["living_room"]["presence"], True)
        self.assertEqual(P._last_state["living_room"]["zones"], {})


class TestIntegration(_Base):
    def test_webhook_feeds_the_same_tables_the_zigbee_bridge_mirrors(self):
        h = _handler("/presence", json.dumps({"room": "office", "presence": True, "source": "homekit"}).encode()); h.do_POST()
        tables = [re.search(r"INSERT INTO ([\w.]+)", s).group(1) for s in self.conn.cur.sql]
        self.assertEqual(tables, ["telemetry.presence", "shared_observations"])
        self.assertEqual(self.conn.cur.params[0][1], 0.95)
        self.assertEqual(json.loads(self.conn.cur.params[0][2])["source"], "homekit")

    def test_rooms_match_fp2_sensor_map(self):
        self.assertEqual({s["room"] for s in P.FP2_SENSORS.values()}, {"office", "living_room", "master_bedroom", "patio"})
        self.assertEqual(P.LISTEN_PORT, 8089)


class TestFunctional(_Base):
    def test_webhook_golden_path(self):
        h = _handler("/presence", json.dumps({"room": "Master Bedroom", "presence": True}).encode()); h.do_POST()
        self.assertEqual(h.codes, [200])
        self.assertEqual(_body(h), {"ok": True, "room": "master_bedroom", "presence": True, "changed": True})
        self.assertEqual(self.conn.commits, 2)
        self.assertEqual(P._event_count, 1)

    def test_webhook_error_path(self):
        h = _handler("/presence", b"\xff\xfe"); h.do_POST()
        self.assertEqual(h.codes[0] >= 400, True)
        self.assertEqual(self.connect.call_count, 0)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_mmwave_poller"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
