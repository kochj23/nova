#!/usr/bin/env python3
"""Tests for nova_energy_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). psycopg2, all HTTP (_http_get) and aiohomekit are mocked; HOME is
redirected at load so the real ~/.openclaw/logs is never touched and no daemon loop is ever run to
completion. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

_TMP_HOME = tempfile.mkdtemp(prefix="energy-home-")
_OLD_HOME = os.environ.get("HOME")
os.environ["HOME"] = _TMP_HOME
_FAKE_CFG = types.SimpleNamespace(post_both=lambda *a, **k: None)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"nova_config": _FAKE_CFG}):
        spec.loader.exec_module(mod)
    return mod


ep = _load("nova_energy_poller_t", SCRIPTS / "nova_energy_poller.py")
if _OLD_HOME is not None:
    os.environ["HOME"] = _OLD_HOME
SRC = (SCRIPTS / "nova_energy_poller.py").read_text()


def _char(uuid, value, desc=""):
    return {"type": uuid, "value": value, "description": desc}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_insert_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIn("VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", SRC)


class TestPerformance(unittest.TestCase):
    def test_extract_reading_10k(self):
        chars = [_char(ep.EVE_CHAR_WATT, 42.0)]
        t0 = time.perf_counter()
        for i in range(10_000):
            ep._extract_eve_energy_reading("Eve", f"d{i}", chars)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_http_get_failure_returns_none(self):
        # RETRY GAP: _http_get()/urlopen — single attempt; failure returns None, callers try next source
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")) as u:
            self.assertIsNone(ep._http_get("http://x"))
        self.assertEqual(u.call_count, 1)

    def test_get_db_connection_failure_returns_none(self):
        with mock.patch.object(ep.psycopg2, "connect", side_effect=RuntimeError("pg down")):
            self.assertIsNone(ep.get_db_connection())

    def test_insert_readings_survives_db_error(self):
        class Cur:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, *a): raise RuntimeError("insert boom")
        class Conn:
            def cursor(s): return Cur()
            def rollback(s): s.rolled = True
        conn = Conn()
        self.assertEqual(ep.insert_readings(conn, [{"device_id": "d", "watts": 1}]), 0)


class TestUnit(unittest.TestCase):
    def test_extract_eve_reading_needs_watts(self):
        chars = [_char(ep.EVE_CHAR_VOLT, 120.0)]
        self.assertIsNone(ep._extract_eve_energy_reading("Eve", "d1", chars))
        chars = [_char(ep.EVE_CHAR_WATT, 55.5), _char(ep.EVE_CHAR_VOLT, 120.0)]
        r = ep._extract_eve_energy_reading("Eve Plug", "d1", chars)
        self.assertEqual(r["watts"], 55.5)
        self.assertEqual(r["volts"], 120.0)

    def test_extract_ignores_bad_values(self):
        chars = [_char(ep.EVE_CHAR_WATT, 10.0), _char(ep.EVE_CHAR_AMP, "not-a-number")]
        r = ep._extract_eve_energy_reading("Eve", "d", chars)
        self.assertEqual(r["watts"], 10.0)
        self.assertIsNone(r["amps"])

    def test_parse_homebridge_accessory(self):
        acc = {"serviceName": "Rack Plug", "aid": 7,
               "serviceCharacteristics": [{"type": ep.EVE_CHAR_WATT, "value": 88, "description": "Watts"}]}
        r = ep._parse_homebridge_accessory(acc)
        self.assertEqual(r["device_id"], "hb_7")
        self.assertEqual(r["watts"], 88.0)

    def test_state_roundtrip_and_merge(self):
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "eve.json"
            with mock.patch.object(ep, "STATE_FILE", sf), mock.patch.object(ep, "STATE_DIR", Path(td)):
                ep.save_known_devices([{"device_id": "a"}])
                self.assertEqual(ep.load_known_devices(), [{"device_id": "a"}])
                ep._merge_discovered_devices([{"device_id": "a"}, {"device_id": "b"}])
                ids = {d["device_id"] for d in ep.load_known_devices()}
        self.assertEqual(ids, {"a", "b"})


class TestIntegration(unittest.TestCase):
    def test_insert_readings_writes_each_row(self):
        rows = []
        class Cur:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, sql, params): rows.append(params)
        class Conn:
            def cursor(s): return Cur()
        n = ep.insert_readings(Conn(), [
            {"device_id": "d1", "device_name": "A", "watts": 1.0},
            {"device_id": "d2", "device_name": "B", "watts": 2.0},
        ])
        self.assertEqual(n, 2)
        self.assertEqual(rows[0][1], "d1")        # device_id bound
        self.assertEqual(rows[1][3], 2.0)         # watts bound

    def test_poll_all_sources_prefers_shortcuts(self):
        with mock.patch.object(ep, "poll_shortcuts_proxy", return_value=[{"device_id": "s", "watts": 1}]), \
             mock.patch.object(ep, "poll_homebridge") as hb:
            out = ep.poll_all_sources()
        self.assertEqual(out[0]["device_id"], "s")
        hb.assert_not_called()                    # short-circuits before homebridge

    def test_poll_all_sources_falls_through_to_homebridge(self):
        with mock.patch.object(ep, "poll_shortcuts_proxy", return_value=[]), \
             mock.patch.object(ep, "poll_aiohomekit", new=mock.AsyncMock(return_value=[])), \
             mock.patch.object(ep, "poll_homebridge", return_value=[{"device_id": "hb", "watts": 3}]):
            out = ep.poll_all_sources()
        self.assertEqual(out[0]["device_id"], "hb")


class TestFunctional(unittest.TestCase):
    def test_poll_homebridge_parses_accessories(self):
        data = [{"serviceName": "Plug", "aid": 1,
                 "serviceCharacteristics": [{"type": ep.EVE_CHAR_WATT, "value": 12, "description": "Consumption"}]}]
        with mock.patch.object(ep, "_http_get", return_value=data):
            out = ep.poll_homebridge()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["watts"], 12.0)

    def test_main_single_cycle_inserts_then_shuts_down(self):
        # drive exactly one loop iteration: known devices present, one reading, then shutdown
        conn = types.SimpleNamespace(closed=False, close=lambda: None)
        def sleep(_):
            ep._shutdown = True
        ep._shutdown = False
        with mock.patch.object(ep, "load_known_devices", return_value=[{"device_id": "a"}]), \
             mock.patch.object(ep, "get_db_connection", return_value=conn), \
             mock.patch.object(ep, "poll_all_sources", return_value=[{"device_id": "a", "watts": 5.0}]), \
             mock.patch.object(ep, "insert_readings", return_value=1) as ins, \
             mock.patch.object(ep, "run_discovery", return_value=[]), \
             mock.patch.object(ep.time, "sleep", side_effect=sleep):
            ep.main()
        ins.assert_called_once()
        ep._shutdown = False

    def test_main_no_devices_runs_discovery(self):
        conn = types.SimpleNamespace(closed=False, close=lambda: None)
        ep._shutdown = False
        def sleep(_):
            ep._shutdown = True
        with mock.patch.object(ep, "load_known_devices", return_value=[]), \
             mock.patch.object(ep, "run_discovery", return_value=[]) as disc, \
             mock.patch.object(ep, "_print_setup_guide"), \
             mock.patch.object(ep, "get_db_connection", return_value=conn), \
             mock.patch.object(ep, "poll_all_sources", return_value=[]), \
             mock.patch.object(ep.time, "sleep", side_effect=sleep):
            ep.main()
        disc.assert_called()
        ep._shutdown = False


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        home = tempfile.mkdtemp(prefix="energy-frame-")
        r = subprocess.run([sys.executable, "-c", "import nova_energy_poller"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": home, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")   # startup banner goes to the log file, not stdout


if __name__ == "__main__":
    unittest.main()
