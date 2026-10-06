#!/usr/bin/env python3
"""Tests for nova_awn_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_awn_poller.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="awn-test-"))

import psycopg2  # noqa: E402  real C extension stays resident; connect is patched per test


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


awn = _load("awn_under_test", SCRIPT)


def _cfg(app="app-key", api="api-key"):
    cfg = types.ModuleType("nova_config")
    cfg.calls = []
    def _keychain(service, account="nova", required=True):
        cfg.calls.append((service, required))
        return {"nova-ambient-app-key": app, "nova-ambient-api-key": api}.get(service, "")
    cfg._keychain = _keychain
    return cfg


class _Cur:
    def __init__(self):
        self.sql, self.params = [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params)


class _Conn:
    def __init__(self):
        self.cur, self.closed, self.autocommit = _Cur(), False, False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _resp(payload):
    r = MagicMock(); r.read.return_value = json.dumps(payload).encode(); return r


DEVICES = [
    {"macAddress": "aa", "lastData": {"pm25_in_aqin": "7.5", "pm10_in_aqin": 9, "co2_in_aqin": 612,
                                      "aqi_pm25_aqin": 31, "pm_in_temp_aqin": 71.2,
                                      "pm_in_humidity_aqin": 44, "batt_co2": 1}},
    {"macAddress": "bb", "lastData": {"tempf": 70.0}},          # no AQIN attached -> skipped
]


def _run_main(devices=DEVICES, cfg=None, urlopen=None, connect=None):
    conn = _Conn()
    out = io.StringIO()
    uo = urlopen or MagicMock(return_value=_resp(devices))
    pc = connect or MagicMock(return_value=conn)
    with patch.dict(sys.modules, {"nova_config": cfg or _cfg()}), \
         patch.object(awn.urllib.request, "urlopen", uo), \
         patch.object(awn.psycopg2, "connect", pc), redirect_stdout(out):
        rc = awn.main()
    return rc, conn, out.getvalue(), uo, pc


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|app[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", awn.DSN)

    def test_keys_come_from_keychain_not_source(self):
        self.assertIn("_keychain(", SRC)
        self.assertIn("nova-ambient-api-key", SRC)
        rc, conn, out, uo, pc = _run_main(cfg=_cfg(app="a&b=c", api="k y"))
        url = uo.call_args[0][0].full_url
        self.assertIn("applicationKey=a%26b%3Dc", url)     # urlencoded, never raw-interpolated
        self.assertIn("apiKey=k+y", url)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        rc, conn, *_ = _run_main()
        sql, params = conn.cur.sql[0], conn.cur.params[0]
        self.assertEqual(sql.count("%s"), 8)
        self.assertEqual(len(params), 8)
        self.assertIn("INSERT INTO telemetry.air_quality", sql)


class TestPerformance(unittest.TestCase):
    def test_float_coercion_10k(self):
        rows = [{"pm25_in_aqin": str(i / 10)} if i % 3 else {"pm25_in_aqin": "junk", "alt": i} for i in range(10_000)]
        t0 = time.perf_counter()
        vals = [awn._f(r, "pm25_in_aqin", "alt") for r in rows]
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(len(vals), 10_000)
        self.assertIsNone(vals[0]) if "alt" not in rows[0] else self.assertEqual(vals[0], 0.0)


class TestRetry(unittest.TestCase):
    def test_fetch_failure_fails_open_without_touching_pg(self):
        # RETRY GAP: main()/urllib.request.urlopen — a single attempt; failure prints and returns 1
        uo = MagicMock(side_effect=OSError("dns down"))
        rc, conn, out, uo, pc = _run_main(urlopen=uo)
        self.assertEqual(rc, 1)
        self.assertEqual(uo.call_count, 1)
        self.assertIn("fetch failed: OSError", out)
        self.assertEqual(pc.call_count, 0)

    def test_pg_outage_is_one_shot_and_escapes(self):
        # RETRY GAP: main()/psycopg2.connect — no retry and no guard; the exception propagates to launchd
        pc = MagicMock(side_effect=RuntimeError("pg down"))
        with self.assertRaises(RuntimeError):
            _run_main(connect=pc)
        self.assertEqual(pc.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_f_edges(self):
        self.assertIsNone(awn._f({}, "x"))
        self.assertIsNone(awn._f({"x": None}, "x"))
        self.assertIsNone(awn._f({"x": "abc"}, "x"))
        self.assertEqual(awn._f({"x": "abc", "y": "2.5"}, "x", "y"), 2.5)   # falls through to the next key
        self.assertEqual(awn._f({"x": 0}, "x"), 0.0)                        # zero is a reading, not missing
        self.assertEqual(awn._f({"x": "1e2"}, "x"), 100.0)

    def test_missing_keys_return_1_before_any_io(self):
        rc, conn, out, uo, pc = _run_main(cfg=_cfg(app="", api="k"))
        self.assertEqual((rc, uo.call_count, pc.call_count), (1, 0, 0))
        self.assertIn("missing AWN keys", out)


class TestIntegration(unittest.TestCase):
    def test_kc_uses_shared_keychain_helper_cron_safe(self):
        cfg = _cfg()
        with patch.dict(sys.modules, {"nova_config": cfg}):
            self.assertEqual(awn._kc("nova-ambient-app-key"), "app-key")
        self.assertEqual(cfg.calls, [("nova-ambient-app-key", False)])   # required=False: never sys.exit from cron

    def test_targets_ops_db_and_air_quality_table(self):
        self.assertIn("dbname=nova_ops", awn.DSN)
        self.assertEqual(awn.API_URL, "https://api.ambientweather.net/v1/devices")
        rc, conn, *_ = _run_main()
        self.assertTrue(conn.autocommit)
        self.assertEqual(conn.cur.params[0], (7.5, 9.0, 612.0, 31.0, None, 71.2, 44.0, True))


class TestFunctional(unittest.TestCase):
    def test_golden_path_stores_one_reading(self):
        rc, conn, out, uo, pc = _run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(conn.cur.sql), 1)        # device without AQIN is skipped
        self.assertTrue(conn.closed)
        self.assertIn("stored 1 air-quality reading", out)
        self.assertIn("pm2.5=7.5", out)

    def test_battery_flag_and_empty_device_list(self):
        dev = [{"lastData": {"pm25_in_aqin": 3, "batt_co2": "0"}}]
        rc, conn, *_ = _run_main(devices=dev)
        self.assertIs(conn.cur.params[0][-1], False)
        dev = [{"lastData": {"co2_in_aqin": 500}}]
        rc, conn, *_ = _run_main(devices=dev)
        self.assertIsNone(conn.cur.params[0][-1])
        rc, conn, out, *_ = _run_main(devices=[])
        self.assertEqual((rc, conn.cur.sql), (0, []))
        self.assertIn("stored 0", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys; from unittest.mock import MagicMock; import psycopg2\n"
                "psycopg2.connect = MagicMock(side_effect=AssertionError('PG touched'))\n"
                "import nova_awn_poller; print('imported', callable(nova_awn_poller.main))")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "imported True")


if __name__ == "__main__":
    unittest.main()
