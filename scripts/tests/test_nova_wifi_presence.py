#!/usr/bin/env python3
"""Tests for nova_wifi_presence.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import contextlib
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
SCRIPT = SCRIPTS / "nova_wifi_presence.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="wifi-presence-test-"))
_MISSING = object()


@contextlib.contextmanager
def _stub_modules(stubs):
    saved = {k: sys.modules.get(k, _MISSING) for k in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load():
    spec = importlib.util.spec_from_file_location("wifi_presence_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(subprocess, "run", side_effect=AssertionError("keychain at import")):
        spec.loader.exec_module(mod)
    return mod


wp = _load()


class _Cur:
    def __init__(self, owners=()):
        self.owners = list(owners); self.sql = []; self.many = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def executemany(self, sql, rows):
        self.many.append((" ".join(sql.split()), list(rows)))

    def fetchall(self):
        return self.owners


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.autocommit = False; self.closed = False; self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _sec(rc=0, out="unifi-key"):
    return MagicMock(return_value=types.SimpleNamespace(returncode=rc, stdout=out, stderr=""))


AP_OFFICE, AP_KITCHEN = "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"
DEVICES = [{"mac": AP_OFFICE, "type": "uap", "name": "Office U6 Enterprise"},
           {"mac": AP_KITCHEN, "type": "uap", "name": "Kitchen UAP-AC-Pro"},
           {"mac": "aa:bb:cc:00:00:09", "type": "usw", "name": "Switch"}]


def _fetch_for(devices, clients):
    def fetch(url, key):
        return devices if url == wp.DEV_URL else clients
    return fetch


def _run_main(owners, devices, clients, key="k", now=1_000_000.0):
    cur = _Cur(owners); conn = _Conn(cur)
    with patch.object(wp, "api_key", return_value=key), patch.object(wp.psycopg2, "connect", return_value=conn), \
         patch.object(wp, "fetch", side_effect=_fetch_for(devices, clients)), patch.object(wp.time, "time", return_value=now), \
         redirect_stdout(io.StringIO()) as out:
        rc = wp.main()
    return rc, cur, conn, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_key_read_via_argv_keychain(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)
        with patch.object(subprocess, "run", _sec()) as run:
            self.assertEqual(wp.api_key(), "unifi-key")
        self.assertEqual(run.call_args[0][0], ["security", "find-generic-password", "-a", "nova", "-s", "nova-unifi-api-key", "-w"])

    def test_sql_is_parameterized_and_the_key_is_never_logged(self):
        self.assertIsNone(re.search(r'execute(?:many)?\(\s*f"', SRC))
        evil = "jordan'); DROP TABLE telemetry.presence; --"
        owners = [("00:11:22:33:44:55", evil, "Phone")]
        clients = [{"mac": "00:11:22:33:44:55", "ap_mac": AP_OFFICE, "signal": -50, "last_seen": 1_000_000}]
        rc, cur, conn, out = _run_main(owners, DEVICES, clients, key="SUPER-SECRET-KEY")
        sql, rows = cur.many[0]
        self.assertNotIn("DROP", sql); self.assertEqual(rows[0][0], evil)
        self.assertNotIn("SUPER-SECRET-KEY", out)

    def test_controller_is_lan_only_and_api_key_header_is_used(self):
        self.assertTrue(wp.CONTROLLER.startswith("https://192.168.1."))
        with patch("urllib.request.urlopen") as u:
            u.return_value.__enter__.return_value.read.return_value = json.dumps({"data": [1]}).encode()
            self.assertEqual(wp.fetch(wp.STA_URL, "k1"), [1])
        self.assertEqual(u.call_args[0][0].get_header("X-api-key"), "k1")


class TestPerformance(unittest.TestCase):
    def test_10k_clients_resolve_fast(self):
        owners = [(f"00:00:00:{i//256:02x}:{i%256:02x}:00", f"p{i % 5}", "dev") for i in range(10_000)]
        clients = [{"mac": m, "ap_mac": AP_OFFICE, "signal": -40 - (i % 50), "last_seen": 1_000_000} for i, (m, _, _) in enumerate(owners)]
        t0 = time.perf_counter()
        rc, cur, conn, out = _run_main(owners, DEVICES, clients)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(cur.many[0][1]), 5)                   # one row per PERSON, strongest device wins


class TestRetry(unittest.TestCase):
    def test_api_key_falls_back_once_to_the_fleet_store_then_empty(self):
        # RETRY GAP: api_key — one `security` call, one nova_secrets lookup, then "" (main() returns 2)
        ns = types.ModuleType("nova_secrets"); ns.get_secret = MagicMock(return_value="")
        with patch.object(subprocess, "run", _sec(rc=1, out="")) as run, _stub_modules({"nova_secrets": ns}):
            self.assertEqual(wp.api_key(), "")
        self.assertEqual(run.call_count, 1); ns.get_secret.assert_called_once_with("nova-unifi-api-key")
        with patch.object(wp, "api_key", return_value=""), patch.object(wp.psycopg2, "connect") as c, redirect_stdout(io.StringIO()):
            self.assertEqual(wp.main(), 2)
        c.assert_not_called()

    def test_controller_fetch_is_one_shot_and_fails_loud(self):
        # RETRY GAP: fetch — a single urlopen; a controller error propagates (the scheduler records the failed run)
        cur = _Cur([("m", "jordan", "x")]); conn = _Conn(cur)
        with patch.object(wp, "api_key", return_value="k"), patch.object(wp.psycopg2, "connect", return_value=conn), \
             patch("urllib.request.urlopen", side_effect=OSError("unifi down")) as u:
            with self.assertRaises(OSError):
                wp.main()
        self.assertEqual(u.call_count, 1); self.assertEqual(cur.many, [])


class TestUnit(unittest.TestCase):
    def test_confidence_from_signal(self):
        self.assertEqual(wp.confidence_from_signal(None), 0.35)
        self.assertEqual(wp.confidence_from_signal(-40), 0.9)        # capped: never proof of a room
        self.assertEqual(wp.confidence_from_signal(-95), 0.25)       # floored
        self.assertEqual(wp.confidence_from_signal(-62), 0.51)

    def test_room_names_strip_hardware_models(self):
        clients = [{"mac": "m1", "ap_mac": AP_OFFICE, "signal": -50, "last_seen": 1_000_000},
                   {"mac": "m2", "ap_mac": AP_KITCHEN, "signal": -50, "last_seen": 1_000_000},
                   {"mac": "m3", "ap_mac": "zz", "signal": -50, "last_seen": 1_000_000}]
        rc, cur, conn, out = _run_main([("m1", "a", "d"), ("m2", "b", "d"), ("m3", "c", "d")], DEVICES, clients)
        rooms = {r[0]: r[1] for r in cur.many[0][1]}
        self.assertEqual(rooms, {"a": "office", "b": "kitchen", "c": "unknown"})

    def test_stale_wired_and_unowned_clients_are_ignored(self):
        clients = [{"mac": "m1", "ap_mac": AP_OFFICE, "signal": -50, "last_seen": 1_000_000 - wp.STALE_SECONDS - 1},
                   {"mac": "m2", "ap_mac": AP_OFFICE, "signal": -50, "is_wired": True},
                   {"mac": "m9", "ap_mac": AP_OFFICE, "signal": -50, "last_seen": 1_000_000}]
        rc, cur, conn, out = _run_main([("m1", "a", "d"), ("m2", "b", "d")], DEVICES, clients)
        self.assertEqual(cur.many, [])
        self.assertIn("no known devices present (2 wireless clients seen, 2 owned MACs known)", out)

    def test_no_owner_rows_short_circuits_before_the_controller(self):
        with patch.object(wp, "api_key", return_value="k"), patch.object(wp.psycopg2, "connect", return_value=_Conn(_Cur([]))), \
             patch.object(wp, "fetch") as f, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wp.main(), 0)
        f.assert_not_called(); self.assertIn("no device_owner rows yet", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_schema_and_owner_lookup(self):
        cur = _Cur([("AA:BB", "jordan", "iPhone")]); conn = _Conn(cur)
        wp.ensure_schema(conn)
        self.assertIn("CREATE TABLE IF NOT EXISTS telemetry.device_owner", cur.sql[0][0]); self.assertEqual(conn.commits, 1)
        self.assertEqual(wp.load_owners(conn), {"AA:BB": ("jordan", "iPhone")})
        self.assertEqual(cur.sql[1][0], "SELECT lower(mac), person, device_label FROM telemetry.device_owner")

    def test_presence_rows_use_the_fusion_tables_contract(self):
        clients = [{"mac": "M1", "ap_mac": AP_OFFICE.upper(), "signal": -45, "last_seen": 1_000_000}]
        rc, cur, conn, out = _run_main([("m1", "jordan", "Jordan iPhone")], DEVICES, clients)
        sql, rows = cur.many[0]
        self.assertTrue(sql.startswith("INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)"))
        person, room, conf, method, meta = rows[0]
        self.assertEqual((person, room, conf, method), ("jordan", "office", 0.82, "wifi_rssi"))
        self.assertEqual(json.loads(meta)["device"], "Jordan iPhone"); self.assertEqual(json.loads(meta)["source"], "unifi")
        self.assertTrue(conn.closed)


class TestFunctional(unittest.TestCase):
    def test_golden_path_strongest_device_per_person(self):
        owners = [("m1", "jordan", "iPhone"), ("m2", "jordan", "MacBook"), ("m3", "amy", "Pixel")]
        clients = [{"mac": "m1", "ap_mac": AP_KITCHEN, "signal": -70, "last_seen": 1_000_000},
                   {"mac": "m2", "ap_mac": AP_OFFICE, "signal": -42, "last_seen": 1_000_000},
                   {"mac": "m3", "ap_mac": AP_KITCHEN, "signal": -55, "last_seen": 1_000_000}]
        rc, cur, conn, out = _run_main(owners, DEVICES, clients)
        self.assertEqual(rc, 0)
        rows = {r[0]: r for r in cur.many[0][1]}
        self.assertEqual(rows["jordan"][1], "office"); self.assertEqual(rows["jordan"][2], 0.87)
        self.assertEqual(rows["amy"][1], "kitchen")
        self.assertIn("presence: jordan@office(0.87), amy@kitchen(0.64)", out)

    def test_seed_maps_only_unambiguous_personal_devices(self):
        cur = _Cur(); conn = _Conn(cur)
        clients = [{"mac": "A1", "name": "Jordan's iPhone"}, {"mac": "A2", "hostname": "amy-macbook"},
                   {"mac": "A3", "name": "Dylan Pixel 8"}, {"mac": "A4", "name": "Exterior Camera jordan-view"},
                   {"mac": "A5", "name": "Jordan wired", "is_wired": True}, {"mac": "A6", "name": "Roku"}]
        with patch.object(wp, "api_key", return_value="k"), patch.object(wp.psycopg2, "connect", return_value=conn), \
             patch.object(wp, "fetch", return_value=clients), redirect_stdout(io.StringIO()) as out:
            wp.seed()
        ins = [p for s, p in cur.sql if s.startswith("INSERT INTO telemetry.device_owner")]
        self.assertEqual(ins, [("a1", "jordan", "Jordan's iPhone", "phone"), ("a2", "amy", "amy-macbook", "computer"),
                               ("a3", "dylan", "Dylan Pixel 8", "phone")])
        self.assertIn("seeded 3 device->person mappings", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main_or_seed(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(seed() if "--seed" in sys.argv else main())', SRC)
        boot = ("import sys, unittest.mock as um, psycopg2, subprocess, urllib.request, runpy; "
                "psycopg2.connect = um.MagicMock(side_effect=AssertionError('pg at import')); "
                "subprocess.run = um.MagicMock(side_effect=AssertionError('keychain at import')); "
                "urllib.request.urlopen = um.MagicMock(side_effect=AssertionError('net at import')); "
                "runpy.run_path(sys.argv[1], run_name='imported'); print('IMPORT_OK')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK")


if __name__ == "__main__":
    unittest.main()
