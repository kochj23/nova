#!/usr/bin/env python3
"""Tests for nova_homekit_sensors.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
SCRIPT = SCRIPTS / "nova_homekit_sensors.py"
SRC = SCRIPT.read_text()
try:
    import psycopg2  # noqa: F401
except ImportError:
    pass


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hs = _load("hk_sensors_under_test", SCRIPT)


def _acc(name, room, **chars):
    types_ = {"temp": "Current Temperature", "hum": "Current Relative Humidity", "lux": "Current Light Level",
              "voc": "Volatile Organic Compound Density", "aqi": "Air Quality"}
    cs = [{"type": types_[k], "value": v} for k, v in chars.items()]
    return {"name": name, "room": room, "services": [{"characteristics": cs}]}


def _payload(accs):
    """fetch() demands > 1000 bytes, so pad small payloads with an inert accessory."""
    raw = json.dumps(accs)
    if len(raw) <= 1000:
        accs = accs + [{"name": "pad", "room": "Pad", "services": [{"characteristics": [{"type": "x" * 1100, "value": None}]}]}]
    return json.dumps(accs).encode()


def _resp(data):
    r = MagicMock(); r.__enter__.return_value.read.return_value = data; r.__exit__.return_value = False
    return r


class _Cur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self):
        self.cur = _Cur(); self.autocommit = False; self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _run(accs):
    conn = _Conn()
    with patch.object(hs.urllib.request, "urlopen", return_value=_resp(_payload(accs))), \
         patch.object(hs, "psycopg2", types.SimpleNamespace(connect=MagicMock(return_value=conn))), \
         redirect_stdout(io.StringIO()) as out:
        rc = hs.main()
    return rc, conn, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", hs.DSN)
        self.assertTrue(hs.HOMEKIT_URL.startswith("http://127.0.0.1:"))

    def test_sql_is_parameterized_and_writes_only_telemetry(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"telemetry.climate", "telemetry.air_quality"})
        evil = "Attic'); DROP TABLE telemetry.climate; --"
        rc, conn, _ = _run([_acc("s", evil, temp=20)])
        sql, params = conn.cur.ran("telemetry.climate")[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[0], hs.slug(evil))


class TestPerformance(unittest.TestCase):
    def test_collect_and_slug_fast_on_10k_accessories(self):
        accs = [_acc(f"s{i}", f"Room {i}", temp=20 + i % 5, hum=40, lux=100, voc=5, aqi=2) for i in range(10_000)]
        t0 = time.perf_counter()
        for a in accs:
            hs.slug(a["room"]); hs.collect(a)
        self.assertLess(time.perf_counter() - t0, 0.5)

    def test_main_hot_path_10k_accessories_under_2s(self):
        accs = [_acc(f"s{i}", f"Room {i}", temp=21, voc=3) for i in range(10_000)]
        t0 = time.perf_counter()
        rc, conn, out = _run(accs)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(conn.cur.sql), 20_000)


class TestRetry(unittest.TestCase):
    def test_fetch_retries_with_backoff_and_succeeds_on_third_try(self):
        good = _payload([_acc("s", "Kitchen", temp=22)])
        u = MagicMock(side_effect=[OSError("refused"), OSError("timeout"), _resp(good)])
        with patch.object(hs.urllib.request, "urlopen", u), patch("time.sleep") as sl, redirect_stdout(io.StringIO()) as out:
            data = hs.fetch()
        self.assertEqual(u.call_count, 3)
        self.assertEqual(sl.call_args_list, [((4,),), ((4,),)])
        self.assertEqual(data[0]["room"], "Kitchen")
        self.assertIn("fetch 1 failed: refused", out.getvalue()); self.assertIn("fetch 2 failed: timeout", out.getvalue())

    def test_fetch_gives_up_after_retries_and_main_skips_without_pg(self):
        u = MagicMock(side_effect=OSError("down"))
        connect = MagicMock()
        with patch.object(hs.urllib.request, "urlopen", u), patch("time.sleep") as sl, \
             patch.object(hs, "psycopg2", types.SimpleNamespace(connect=connect)), redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(hs.fetch(retries=3))
            rc = hs.main()
        self.assertEqual(u.call_count, 3 + 6)
        self.assertEqual(rc, 1)
        connect.assert_not_called()
        self.assertIn("HomeKit unreachable — skipping", out.getvalue())

    def test_tiny_body_is_treated_as_a_failed_attempt(self):
        u = MagicMock(side_effect=[_resp(b"[]"), _resp(_payload([_acc("s", "Garage", lux=5)]))])
        with patch.object(hs.urllib.request, "urlopen", u), patch("time.sleep"):
            data = hs.fetch()
        self.assertEqual(u.call_count, 2); self.assertEqual(data[0]["room"], "Garage")


class TestUnit(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(hs.slug("Office"), "server_rack")          # the rack-mounted "Office" accessory
        self.assertEqual(hs.slug("WTF"), "server_rack")
        self.assertEqual(hs.slug("Dylan’s Room"), "dylans_room")
        self.assertEqual(hs.slug("Guest Suite"), "guest_suite")
        self.assertEqual(hs.slug("Nora’s Den"), "noras_den")
        self.assertEqual(hs.slug(None), "unknown"); self.assertEqual(hs.slug(""), "unknown")

    def test_c_to_f(self):
        self.assertEqual(hs.c_to_f(0), 32.0); self.assertEqual(hs.c_to_f("100"), 212.0)
        self.assertEqual(hs.c_to_f(21.5), 70.7)
        self.assertIsNone(hs.c_to_f(None)); self.assertIsNone(hs.c_to_f("warm"))

    def test_collect_edges(self):
        self.assertEqual(hs.collect({}), {})
        self.assertEqual(hs.collect({"services": None}), {})
        self.assertEqual(hs.collect({"services": [{"characteristics": None}]}), {})
        self.assertEqual(hs.collect(_acc("s", "r", temp=None, hum=55.55, aqi=3)), {"humidity": 55.6, "aqi": 3.0})
        self.assertEqual(hs.collect(_acc("s", "r", temp=20, lux=12.34, voc=7.77)), {"temp_f": 68.0, "lux": 12.3, "voc": 7.8})
        self.assertEqual(hs.collect({"services": [{"characteristics": [{"type": "Battery Level", "value": 50}]}]}), {})


class TestIntegration(unittest.TestCase):
    def test_climate_and_air_quality_rows_route_to_the_right_tables(self):
        rc, conn, out = _run([_acc("Eve Room", "WTF", temp=25, hum=40, voc=120, aqi=2), _acc("Motion", "Garage", lux=3)])
        self.assertEqual(rc, 0)
        cl = conn.cur.ran("telemetry.climate"); aq = conn.cur.ran("telemetry.air_quality")
        self.assertEqual(len(cl), 2); self.assertEqual(len(aq), 1)
        self.assertTrue(cl[0][0].startswith("INSERT INTO telemetry.climate (ts, room, source, temp_f, humidity, light_lux) VALUES (now(), %s, %s, %s, %s, %s)"))
        self.assertEqual(cl[0][1], ("server_rack", "homekit", 77.0, 40.0, None))
        self.assertEqual(cl[1][1], ("garage", "homekit", None, None, 3.0))
        self.assertEqual(aq[0][1], ("homekit", "server_rack", 120.0, 2.0))
        self.assertIn("server_rack/Eve Room:", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_with_autocommit_and_reports_counts(self):
        rc, conn, out = _run([_acc("Thermo", "Kitchen", temp=20, hum=50)])
        self.assertEqual(rc, 0)
        self.assertTrue(conn.autocommit); self.assertTrue(conn.closed)
        self.assertIn("[hk-sensors] wrote 1 climate + 0 air_quality rows.", out)

    def test_accessories_without_sensor_values_are_skipped(self):
        rc, conn, out = _run([{"name": "Switch", "room": "Hall", "services": [{"characteristics": [{"type": "On", "value": True}]}]}])
        self.assertEqual(rc, 0)
        self.assertEqual([s for s in conn.cur.sql if "INSERT" in s[0]], [])
        self.assertIn("wrote 0 climate + 0 air_quality rows", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no argparse: --help would poll HomeKit (with 6x4s retries) and write to PG, so the smoke is an import
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_homekit_sensors"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
