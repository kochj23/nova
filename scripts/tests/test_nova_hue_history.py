#!/usr/bin/env python3
"""Tests for nova_hue_history.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_hue_history.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hh = _load("hue_under_test", SCRIPT)

LIGHTS = {
    "1": {"name": "Office Lamp", "type": "Extended color light",
          "state": {"on": True, "bri": 127, "ct": 370, "hue": 8000, "sat": 140, "colormode": "ct", "reachable": True}},
    "2": {"name": "Hall", "type": "Dimmable light",
          "state": {"on": False, "bri": 254, "reachable": False}},
}
GROUPS = {
    "1": {"type": "Room", "name": "Office", "lights": ["1"]},
    "2": {"type": "LightGroup", "name": "All", "lights": ["1", "2"]},     # not a Room/Zone: ignored
    "3": {"type": "Zone", "name": "Downstairs", "lights": ["1", "2"]},    # first Room/Zone wins for light 1
}


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _bridge(lights=LIGHTS, groups=GROUPS):
    def fake(url, timeout=None):
        if url.endswith("/lights"):
            return _Resp(lights)
        if url.endswith("/groups"):
            return _Resp(groups)
        raise AssertionError(url)
    return fake


class _Cur:
    def __init__(self): self.sql = []; self.rows = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sql.append(sql)
    def executemany(self, sql, rows): self.sql.append(sql); self.rows = list(rows)


def _conn(cur):
    return types.SimpleNamespace(cursor=lambda: cur, close=lambda: None, autocommit=False)


def _keychain(stdout="abcdef0123456789", rc=0):
    return lambda cmd, **kw: types.SimpleNamespace(stdout=stdout, returncode=rc)


def _run_main(lights=LIGHTS, groups=GROUPS, urlopen=None):
    cur = _Cur(); connects = []

    def connect(dsn):
        connects.append(dsn); return _conn(cur)
    with patch.object(hh.subprocess, "run", _keychain()), patch.object(hh.psycopg2, "connect", connect), \
         patch("urllib.request.urlopen", urlopen or _bridge(lights, groups)), redirect_stdout(io.StringIO()) as out:
        rc = hh.main()
    return rc, cur, connects, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", hh.DSN)

    def test_api_key_comes_from_keychain_then_fleet_store(self):
        self.assertIn("find-generic-password", SRC)
        self.assertIn("nova_secrets.get_secret", SRC)
        with patch.object(hh.subprocess, "run", _keychain("  k3y-from-keychain \n")):
            self.assertEqual(hh.api_key(), "k3y-from-keychain")

    def test_insert_is_parameterized_and_only_touches_the_history_table(self):
        self.assertIsNone(re.search(r'execute(many)?\(\s*f"', SRC))
        writes = {m.group(0) for m in re.finditer(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+[\w.]+", SRC)}
        self.assertEqual(writes, {"INSERT INTO telemetry.hue_light_history"})
        evil = {"9": {"name": "x'); DROP TABLE telemetry.hue_light_history; --", "type": "t", "state": {"on": True}}}
        rc, cur, _, _ = _run_main(lights=evil)
        self.assertIn("VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", cur.sql[-1])
        self.assertEqual(cur.rows[0][1], evil["9"]["name"])      # passed as a bound value, never spliced into SQL


class TestPerformance(unittest.TestCase):
    def test_10k_lights_build_rows_fast(self):
        many = {str(i): {"name": f"L{i}", "type": "Color light" if i % 2 else "strip",
                         "state": {"on": bool(i % 3), "bri": i % 255, "ct": 153 + i % 300, "reachable": True}} for i in range(10_000)}
        t0 = time.perf_counter()
        rc, cur, _, _ = _run_main(lights=many, groups={})
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(cur.rows), 10_000)
        t0 = time.perf_counter()
        for i in range(10_000):
            hh.rated_watts("Extended color light")
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def test_keychain_miss_falls_back_to_fleet_secret_store(self):
        # two Keychain spellings x two arg forms are tried before the PG store: that is the retry ladder
        tried = []

        def miss(cmd, **kw):
            tried.append(cmd); return types.SimpleNamespace(stdout="", returncode=44)
        ns = types.ModuleType("nova_secrets"); ns.get_secret = lambda name: f"fleet:{name}"
        with patch.object(hh.subprocess, "run", miss), patch.dict(sys.modules, {"nova_secrets": ns}):
            self.assertEqual(hh.api_key(), "fleet:nova-hue-api-key")
        self.assertEqual(len(tried), 4)

    def test_no_key_anywhere_fails_closed_before_any_bridge_or_db_call(self):
        ns = types.ModuleType("nova_secrets"); ns.get_secret = lambda name: (_ for _ in ()).throw(KeyError(name))
        with patch.object(hh.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())), \
             patch.dict(sys.modules, {"nova_secrets": ns}), patch("urllib.request.urlopen") as u, \
             patch.object(hh.psycopg2, "connect") as pg:
            with self.assertRaises(RuntimeError):
                hh.main()
        u.assert_not_called(); pg.assert_not_called()

    def test_bridge_fetch_is_one_shot_and_never_writes_partial_data(self):
        # RETRY GAP: get()/urlopen — a single attempt; failure propagates out of main() and PG is never opened
        calls = []

        def dead(url, timeout=None):
            calls.append(url); raise OSError("bridge unreachable")
        with patch.object(hh.subprocess, "run", _keychain()), patch.object(hh.psycopg2, "connect") as pg, \
             patch("urllib.request.urlopen", dead):
            with self.assertRaises(OSError):
                hh.main()
        self.assertEqual(len(calls), 1)
        pg.assert_not_called()

    def test_room_map_fails_open(self):
        with patch("urllib.request.urlopen", lambda u, timeout=None: (_ for _ in ()).throw(OSError("x"))):
            self.assertEqual(hh.room_map("k"), {})


class TestUnit(unittest.TestCase):
    def test_rated_watts_by_type(self):
        self.assertEqual(hh.rated_watts("Hue lightstrip plus"), 20.0)
        self.assertEqual(hh.rated_watts("Gradient signe"), 20.0)
        self.assertEqual(hh.rated_watts("Extended color light"), 10.0)
        self.assertEqual(hh.rated_watts("Color temperature light"), 10.0)
        self.assertEqual(hh.rated_watts("Dimmable white"), 9.0)
        self.assertEqual(hh.rated_watts("Dimmable light"), 8.0)
        self.assertEqual(hh.rated_watts(None), 8.0)

    def test_get_builds_the_bridge_url(self):
        seen = []
        with patch("urllib.request.urlopen", lambda u, timeout=None: seen.append(u) or _Resp({"a": 1})):
            self.assertEqual(hh.get("KEY", "lights"), {"a": 1})
        self.assertEqual(seen, [f"http://{hh.BRIDGE}/api/KEY/lights"])

    def test_row_math(self):
        rc, cur, _, _ = _run_main()
        on, off = cur.rows
        # (lid, name, room, on, bri, bri_pct, hue, sat, ct, kelvin, colormode, reachable, est)
        self.assertEqual(on[:6], ("1", "Office Lamp", "Office", True, 127, 50.0))
        self.assertEqual(on[8:10], (370, 2703))
        self.assertEqual(on[12], 5.0)                       # 10W * 127/254
        self.assertEqual(off[3:6], (False, 254, 100.0))
        self.assertEqual(off[9], None)
        self.assertEqual(off[12], 0.0)                      # off draws nothing
        self.assertIs(off[11], False)

    def test_missing_bri_and_ct_are_null_not_zero(self):
        rc, cur, _, _ = _run_main(lights={"5": {"name": "n", "type": "t", "state": {"on": True}}}, groups={})
        row = cur.rows[0]
        self.assertEqual((row[4], row[5], row[8], row[9], row[2]), (None, None, None, None, None))
        self.assertEqual(row[12], 0.0)


class TestIntegration(unittest.TestCase):
    def test_room_map_uses_rooms_and_zones_first_match_wins(self):
        with patch("urllib.request.urlopen", _bridge()):
            self.assertEqual(hh.room_map("k"), {"1": "Office", "2": "Downstairs"})

    def test_main_composes_key_bridge_rooms_and_schema(self):
        rc, cur, connects, _ = _run_main()
        self.assertEqual(connects, [hh.DSN])
        self.assertIn("CREATE TABLE IF NOT EXISTS telemetry.hue_light_history", cur.sql[0])
        self.assertIn("idx_huehist_name_ts", cur.sql[0])
        self.assertTrue(cur.sql[1].startswith("INSERT INTO telemetry.hue_light_history (light_id,name,room,is_on,bri,bri_pct,hue,sat,ct_mired,ct_kelvin,colormode,reachable,est_watts)"))
        self.assertEqual([r[2] for r in cur.rows], ["Office", "Downstairs"])


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_every_light_and_reports(self):
        rc, cur, _, out = _run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(cur.rows), 2)
        self.assertEqual(out.strip(), "[hue-history] wrote 2 lights (1 on)")

    def test_no_lights_still_creates_schema_and_exits_zero(self):
        rc, cur, _, out = _run_main(lights={}, groups={})
        self.assertEqual(rc, 0)
        self.assertEqual(cur.rows, [])
        self.assertIn("wrote 0 lights (0 on)", out)

    def test_bridge_error_path_raises_without_db_write(self):
        with patch.object(hh.subprocess, "run", _keychain()), patch.object(hh.psycopg2, "connect") as pg, \
             patch("urllib.request.urlopen", lambda u, timeout=None: (_ for _ in ()).throw(OSError("timeout"))):
            with self.assertRaises(OSError):
                hh.main()
        pg.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_polls_the_bridge(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_hue_history; print(nova_hue_history.BRIDGE)"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "192.168.1.152")

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
