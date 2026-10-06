#!/usr/bin/env python3
"""Tests for nova_climate_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import datetime
import importlib.util
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import unittest
import urllib.request  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2          # noqa: F401
import psycopg2.extras   # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_climate_poller.py"
SRC = SCRIPT.read_text()


def _load():
    """Import with the log file and the process-wide SIGTERM/SIGINT hooks neutralised."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    spec = importlib.util.spec_from_file_location("nclimate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    try:
        with patch("logging.basicConfig"), patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
             patch("psycopg2.connect", side_effect=RuntimeError("offline")):
            spec.loader.exec_module(mod)
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    mod.log = logging.getLogger("nclimate.test"); mod.log.addHandler(logging.NullHandler()); mod.log.propagate = False
    return mod


cp = _load()


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


class _Cur:
    def __init__(self, row=None):
        self.row = row; self.sql = []; self.params = []

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sql.append(sql); self.params.append(params)
    def fetchone(self): return self.row


class _Conn:
    def __init__(self, row=None, fail_insert=False):
        self.cur = _Cur(row); self.fail_insert = fail_insert; self.commits = 0; self.rollbacks = 0; self.closed = False

    def cursor(self, **kw):
        if self.fail_insert and self.commits >= 1:
            raise psycopg2.OperationalError("lost")
        return self.cur

    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed = True


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_hue_key_comes_from_keychain(self):
        self.assertIn('nova_config._keychain("nova-hue-api-key"', SRC)
        with patch.object(cp.nova_config, "_keychain", return_value="k") as kc:
            self.assertEqual(cp._get_hue_username(), "k")
        self.assertEqual(kc.call_args[0][0], "nova-hue-api-key")

    def test_insert_is_parameterized(self):
        conn = _Conn()
        cp.insert_readings(conn, [{"room": "x'); DROP TABLE t;--", "metric": "temp_f", "value": 70, "source": "s"}])
        self.assertIn("%s", conn.cur.sql[0])
        self.assertEqual(conn.cur.params[0][0], "x'); DROP TABLE t;--")
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')


class TestPerformance(unittest.TestCase):
    def test_normalize_and_group_10k(self):
        readings = [{"room": cp._normalize_room(f"Room {i % 50} HomePod mini"), "metric": "temp_f",
                     "value": 70.0, "source": "s"} for i in range(10_000)]
        conn = _Conn()
        t0 = time.perf_counter()
        cp.insert_readings(conn, readings)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(conn.cur.sql), 50)          # one row per room+source


class TestRetry(unittest.TestCase):
    def test_http_get_fails_open(self):
        # RETRY GAP: _http_get — one GET per cycle; failure returns None (next 2-min cycle is the retry)
        with patch.object(cp.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertIsNone(cp._http_get("http://127.0.0.1:1/x"))
        self.assertEqual(uo.call_count, 1)

    def test_insert_failure_reconnects_once(self):
        bad = _Conn(fail_insert=True)
        good = _Conn()
        bad.commits = 1                                   # schema check already committed
        with patch.object(cp, "_get_hue_username", return_value=""), patch.object(cp, "_get_ha_token", return_value=None), \
             patch.object(cp, "ensure_schema"), patch.object(cp, "poll_weather_station", return_value=[]), \
             patch.object(cp, "poll_homepod_sensors", return_value=[{"room": "r", "metric": "temp_f", "value": 1, "source": "s"}]), \
             patch.object(cp.psycopg2, "connect", side_effect=[bad, good]) as connect, \
             patch.object(cp.time, "sleep", side_effect=lambda s: setattr(cp, "_shutdown", True)):
            cp._shutdown = False
            cp.main()
        cp._shutdown = False
        self.assertEqual(connect.call_count, 2)
        self.assertTrue(bad.closed)

    def test_zha_fetch_failure_returns_empty(self):
        with patch.object(cp.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(cp.poll_zha_climate("tok"), [])
        self.assertEqual(cp.poll_zha_climate(None), [])


class TestUnit(unittest.TestCase):
    def test_normalize_room(self):
        self.assertEqual(cp._normalize_room("Living Room HomePod"), "living_room")
        self.assertEqual(cp._normalize_room("Office Hue Motion Sensor"), "office")
        self.assertEqual(cp._normalize_room("  "), "unknown")

    def test_poll_hue_converts_units(self):
        sensors = {"1": {"type": "ZLLTemperature", "name": "Patio Hue motion", "state": {"temperature": 2143}},
                   "2": {"type": "ZLLLightLevel", "name": "Patio", "state": {"lightlevel": 10001}},
                   "3": {"type": "ZLLPresence", "name": "Patio", "state": {"presence": True}}, "4": "junk"}
        with patch.object(cp, "_http_get", return_value=sensors):
            r = {x["metric"]: x["value"] for x in cp.poll_hue("1.2.3.4", "u")}
        self.assertEqual(r, {"temperature_c": 21.43, "light_lux": 10.0, "presence": 1.0})

    def test_weather_station_ignores_stale_rows(self):
        old = {"temp_f": 60, "humidity": 50, "ts": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)}
        self.assertEqual(cp.poll_weather_station(_Conn(old)), [])
        fresh = {**old, "ts": datetime.datetime.now(datetime.timezone.utc)}
        self.assertEqual(len(cp.poll_weather_station(_Conn(fresh))), 2)


class TestIntegration(unittest.TestCase):
    def test_zha_emits_both_legacy_feeds(self):
        t, h, _ = cp.ZHA_FP300["garage"]
        t2, h2, _ = cp.ZHA_FP300["office"]
        states = [{"entity_id": t, "state": "71.5"}, {"entity_id": h, "state": "unavailable"},
                  {"entity_id": t2, "state": "70"}, {"entity_id": h2, "state": "40"}]
        with patch.object(cp.urllib.request, "urlopen", return_value=_resp(states)):
            r = cp.poll_zha_climate("tok")
        feeds = {(x["room"], x["source"], x["metric"]) for x in r}
        self.assertIn(("garage_presence", "zigbee", "temp_f"), feeds)
        self.assertNotIn(("garage", "fp300", "temp_f"), feeds)            # garage never in the fp300 feed
        self.assertIn(("office", "fp300", "humidity"), feeds)
        self.assertNotIn(("garage_presence", "zigbee", "humidity"), feeds)

    def test_writes_telemetry_climate_table(self):
        conn = _Conn()
        cp.insert_readings(conn, [{"room": "r", "metric": "presence", "value": 1.0, "source": "hue_bridge"},
                                  {"room": "r", "metric": "humidity", "value": 41.7, "source": "hue_bridge"}])
        self.assertIn("INSERT INTO telemetry.climate", conn.cur.sql[0])
        self.assertEqual(conn.cur.params[0][2:], (None, 41, None, True))


class TestFunctional(unittest.TestCase):
    def test_one_cycle_inserts_and_exits_on_shutdown(self):
        conn = _Conn()
        with patch.object(cp, "_get_hue_username", return_value="u"), patch.object(cp, "_discover_hue_bridge", return_value="1.2.3.4"), \
             patch.object(cp, "poll_hue", return_value=[{"room": "patio", "metric": "light_lux", "value": 5, "source": "hue_bridge"}]), \
             patch.object(cp, "_get_ha_token", return_value=None), patch.object(cp, "poll_weather_station", return_value=[]), \
             patch.object(cp, "poll_homepod_sensors", return_value=[]), \
             patch.object(cp.psycopg2, "connect", return_value=conn), \
             patch.object(cp.time, "sleep", side_effect=lambda s: setattr(cp, "_shutdown", True)):
            cp._shutdown = False
            cp.main()
        cp._shutdown = False
        inserts = [p for s, p in zip(conn.cur.sql, conn.cur.params) if "INSERT" in s]
        self.assertEqual(inserts, [("patio", "hue_bridge", None, None, 5, None)])
        self.assertTrue(conn.closed)

    def test_pg_down_at_start_exits_1(self):
        with patch.object(cp, "_get_hue_username", return_value=""), patch.object(cp, "_get_ha_token", return_value=None), \
             patch.object(cp.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")):
            with self.assertRaises(SystemExit) as cm:
                cp.main()
        self.assertEqual(cm.exception.code, 1)


class TestFrame(unittest.TestCase):
    def test_import_smoke_offline(self):
        # No --help/--selftest: running the script starts the daemon loop, so smoke the import instead.
        code = ("import logging, sys; logging.basicConfig = lambda **k: None; sys.path.insert(0, sys.argv[1]); "
                "import nova_climate_poller as m; assert callable(m.main) and m._shutdown is False; print('ok')")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")

    def test_import_never_runs_main_or_keeps_signal_hooks(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        before = signal.getsignal(signal.SIGINT)
        _load()
        self.assertIs(signal.getsignal(signal.SIGINT), before)


if __name__ == "__main__":
    unittest.main()
