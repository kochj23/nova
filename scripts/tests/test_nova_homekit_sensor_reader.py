#!/usr/bin/env python3
"""Tests for nova_homekit_sensor_reader.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_homekit_sensor_reader.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="hk-test-"))

import psycopg2  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hk = _load("homekit_under_test", SCRIPT)


def _home_with_prefs(payload, name="h"):
    home = TMP / name
    (home / "Library/Preferences").mkdir(parents=True, exist_ok=True)
    (home / "Library/Preferences/com.apple.homed.plist").write_bytes(b"bplist00")
    return home


def _plutil(payload, rc=0):
    def run(cmd, capture_output=False, text=False, timeout=None):
        return types.SimpleNamespace(returncode=rc, stdout=json.dumps(payload), stderr="")
    return run


def _conn(rows):
    cur = MagicMock(); cur.fetchall.return_value = rows
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", SRC)

    def test_plutil_is_argv_and_sql_is_static(self):
        self.assertNotIn("shell=True", SRC)
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        conn, cur = _conn([])
        with patch("psycopg2.connect", return_value=conn):
            hk.read_via_shortcuts_output()
        sql = cur.execute.call_args[0][0]
        self.assertIn("FROM telemetry.bluetooth", sql)
        self.assertNotIn("%s", sql)        # fixed query, no runtime values at all
        self.assertEqual(len(cur.execute.call_args[0]), 1)


class TestPerformance(unittest.TestCase):
    def test_cache_scan_10k_keys(self):
        payload = {f"key{i}": i for i in range(10_000)}
        payload.update({f"RoomTemperature{i}": 70 + i for i in range(50)})
        home = _home_with_prefs(payload, "perf")
        with patch("pathlib.Path.home", return_value=home), patch.object(hk.subprocess, "run", _plutil(payload)):
            t0 = time.perf_counter()
            out = hk.read_via_homekit_cache()
            self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(out), 50)


class TestRetry(unittest.TestCase):
    def test_cache_read_fails_open(self):
        # RETRY GAP: read_via_homekit_cache()/plutil — one subprocess; any failure yields [] not an exception
        home = _home_with_prefs({}, "retry")
        boom = MagicMock(side_effect=OSError("plutil missing"))
        with patch("pathlib.Path.home", return_value=home), patch.object(hk.subprocess, "run", boom):
            self.assertEqual(hk.read_via_homekit_cache(), [])
        self.assertEqual(boom.call_count, 1)

    def test_pg_outage_fails_open_as_error_record(self):
        # RETRY GAP: read_via_shortcuts_output()/psycopg2.connect — one attempt, error surfaced in the JSON payload
        pc = MagicMock(side_effect=RuntimeError("pg down"))
        with patch("psycopg2.connect", pc):
            self.assertEqual(hk.read_via_shortcuts_output(), [{"error": "pg down"}])
        self.assertEqual(pc.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_placeholder_readers(self):
        self.assertIsNone(hk.read_via_home_cli())
        self.assertEqual(hk.read_via_system_profiler(), [])

    def test_cache_filters_temperature_and_humidity_keys_case_insensitively(self):
        payload = {"LivingRoomTemperature": 71, "hallHUMIDITY": 40, "lastSync": 1}
        home = _home_with_prefs(payload, "unit")
        with patch("pathlib.Path.home", return_value=home), patch.object(hk.subprocess, "run", _plutil(payload)):
            out = hk.read_via_homekit_cache()
        self.assertEqual(out, [{"key": "LivingRoomTemperature", "value": 71}, {"key": "hallHUMIDITY", "value": 40}])

    def test_cache_edges(self):
        with patch("pathlib.Path.home", return_value=TMP / "nope"):
            self.assertEqual(hk.read_via_homekit_cache(), [])            # no prefs file
        home = _home_with_prefs({}, "rc")
        with patch("pathlib.Path.home", return_value=home), patch.object(hk.subprocess, "run", _plutil({"temperature": 1}, rc=1)):
            self.assertEqual(hk.read_via_homekit_cache(), [])            # plutil failed -> nothing fabricated


class TestIntegration(unittest.TestCase):
    def test_shortcuts_reader_shapes_homepod_rows(self):
        rows = [("HomePod (Kitchen)", -55, None, "homepod", {}), (None, -70, None, "homepod", {})]
        conn, cur = _conn(rows)
        with patch("psycopg2.connect", return_value=conn):
            out = hk.read_via_shortcuts_output()
        self.assertEqual([o["room"] for o in out], ["Kitchen", "unknown"])
        self.assertEqual(out[0]["type"], "presence"); self.assertEqual(out[0]["unit"], "dBm"); self.assertEqual(out[0]["value"], -55)
        self.assertIn("device_type = 'homepod'", cur.execute.call_args[0][0])
        self.assertTrue(conn.close.called and cur.close.called)


class TestFunctional(unittest.TestCase):
    def test_fallback_chain_cache_then_pg(self):
        """Mirror the __main__ block: cache first, PG presence data only if the cache is empty."""
        home = _home_with_prefs({"x": 1}, "func")
        conn, cur = _conn([("HomePod (Office)", -60, None, "homepod", {})])
        with patch("pathlib.Path.home", return_value=home), patch.object(hk.subprocess, "run", _plutil({"x": 1})), \
             patch("psycopg2.connect", return_value=conn) as pc:
            result = hk.read_via_homekit_cache() or hk.read_via_shortcuts_output()
        self.assertEqual(result[0]["room"], "Office")
        self.assertEqual(pc.call_count, 1)
        self.assertIn("note", result[0])
        json.dumps(result)                                             # the stdout contract: serialisable

    def test_cache_hit_skips_pg(self):
        payload = {"BedroomTemperature": 68}
        home = _home_with_prefs(payload, "func2")
        with patch("pathlib.Path.home", return_value=home), patch.object(hk.subprocess, "run", _plutil(payload)), \
             patch("psycopg2.connect", side_effect=AssertionError("PG touched")) as pc:
            result = hk.read_via_homekit_cache() or hk.read_via_shortcuts_output()
        self.assertEqual(result, [{"key": "BedroomTemperature", "value": 68}])
        self.assertEqual(pc.call_count, 0)


class TestFrame(unittest.TestCase):
    def test_script_prints_json_offline(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys, runpy; from unittest.mock import MagicMock; import psycopg2\n"
                "psycopg2.connect = MagicMock(side_effect=RuntimeError('offline'))\n"
                "import nova_homekit_sensor_reader\n"
                "runpy.run_path(%r, run_name='__main__')\n" % str(SCRIPT))
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP / "frame")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [{"error": "offline"}])


if __name__ == "__main__":
    unittest.main()
