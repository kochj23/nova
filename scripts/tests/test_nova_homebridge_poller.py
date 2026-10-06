#!/usr/bin/env python3
"""Tests for nova_homebridge_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The module configures logging, creates its log dir and installs SIGTERM/SIGINT handlers at import,
so it is loaded with Path.home -> tempdir and logging.basicConfig / signal.signal stubbed.
hb_login() reads the password via nova_secrets.vault_secret (1Password `op` / PG mirror); that is stubbed for the
whole module (it was the real `op read` + PG fallback that made this file slow)."""
import importlib.util
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_homebridge_poller.py"
SRC = PATH.read_text()
_TD = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("homebridge_poller_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", return_value=Path(_TD.name)), patch.object(logging, "basicConfig"), \
            patch.object(signal, "signal"):
        spec.loader.exec_module(mod)
    return mod


hb = _load()
import nova_secrets  # noqa: E402  (import-clean; hb_login imports vault_secret from it at call time)
FAKE_HB_PW = "hb-" + "vault-fixture"
VAULT = MagicMock(return_value=FAKE_HB_PW)
_PATCHES = []


def setUpModule():
    for p in (patch.object(hb.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(hb.psycopg2, "connect", side_effect=AssertionError("unmocked PG")),
              patch.object(nova_secrets, "vault_secret", VAULT)):
        p.start(); _PATCHES.append(p)
    hb.log.disabled = True


def tearDownModule():
    hb.log.disabled = False
    while _PATCHES:
        _PATCHES.pop().stop()


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode()
    return r


def _acc(kind, name, desc, value):
    return {"type": kind, "serviceName": name, "serviceCharacteristics": [{"description": desc, "value": value}]}


class _State:
    def setUp(self):
        hb._token, hb._prev_motion, hb._conn = None, {}, None
        self.cur = MagicMock()
        self.conn = MagicMock(closed=False); self.conn.cursor.return_value = self.cur


class TestSecurity(_State, unittest.TestCase):
    def test_no_long_hardcoded_secrets(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertEqual(hb.HB_URL, "http://192.168.1.10:8581")

    def test_no_admin_password_literal_in_source(self):
        # Fixed 2026-10-06: the Homebridge factory default admin/admin password is gone from source.
        self.assertFalse(hasattr(hb, "HB_PASS"))
        self.assertIsNone(re.search(r"HB_PASS\s*=", SRC))
        self.assertIsNone(re.search(r"[\"']password[\"']\s*:\s*[\"']", SRC))   # no literal password value
        self.assertNotIn("admin/admin", SRC)
        self.assertEqual(hb.HB_PASS_ITEM, "nova-homebridge-password")

    def test_login_posts_vault_password(self):
        VAULT.reset_mock()
        with patch.object(hb.urllib.request, "urlopen", return_value=_resp({"access_token": "tk"})) as u:
            self.assertTrue(hb.hb_login())
        VAULT.assert_called_once_with("nova-homebridge-password")
        req = u.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/api/auth/login"))
        self.assertEqual(json.loads(req.data), {"username": hb.HB_USER, "password": FAKE_HB_PW})
        self.assertEqual(hb._token, "tk")

    def test_login_fails_closed_when_vault_unavailable(self):
        with patch.object(nova_secrets, "vault_secret", side_effect=KeyError("nova-homebridge-password")), \
                patch.object(hb.urllib.request, "urlopen") as u:
            self.assertFalse(hb.hb_login())
            self.assertIsNone(hb.hb_get("/api/accessories"))
        u.assert_not_called()
        self.assertIsNone(hb._token)

    def test_inserts_parameterized(self):
        hb._token = "t"
        evil = "Cam'); SELECT pg_sleep(9);--"
        with patch.object(hb.urllib.request, "urlopen", return_value=_resp([_acc("MotionSensor", evil, "Motion Detected", 1)])), \
                patch.object(hb, "get_db", return_value=self.conn):
            hb.poll_cycle()
        sql, params = self.cur.execute.call_args.args
        self.assertNotIn(evil, sql)
        self.assertEqual(params[0], f"Motion detected: {evil}")

    def test_bearer_token_in_header_not_url(self):
        hb._token = "tok123"
        with patch.object(hb.urllib.request, "urlopen", return_value=_resp([])) as u:
            hb.hb_get("/api/accessories")
        req = u.call_args.args[0]
        self.assertNotIn("tok123", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer tok123")


class TestPerformance(_State, unittest.TestCase):
    def test_poll_10k_accessories_fast(self):
        hb._token = "t"
        accs = [_acc("MotionSensor" if i % 2 else "TemperatureSensor", f"d{i}",
                     "Motion Detected" if i % 2 else "Current Temperature", i % 3) for i in range(10_000)]
        with patch.object(hb.urllib.request, "urlopen", return_value=_resp(accs)), \
                patch.object(hb, "get_db", return_value=self.conn):
            t0 = time.perf_counter()
            hb.poll_cycle()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertGreater(self.cur.execute.call_count, 5000)


class TestRetry(_State, unittest.TestCase):
    def test_401_relogs_in_once_then_succeeds(self):
        hb._token = "stale"
        e401 = urllib.error.HTTPError("u", 401, "unauth", {}, None)
        seq = [e401, _resp({"access_token": "fresh"}), _resp([{"ok": 1}])]
        with patch.object(hb.urllib.request, "urlopen", side_effect=seq) as u:
            self.assertEqual(hb.hb_get("/api/accessories"), [{"ok": 1}])
        self.assertEqual(u.call_count, 3)
        self.assertEqual(hb._token, "fresh")

    def test_network_error_fails_open(self):
        # RETRY GAP: hb_get() — non-401 errors are not retried within a cycle (the 10s poll loop is the retry)
        hb._token = "t"
        with patch.object(hb.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertIsNone(hb.hb_get("/x"))
        self.assertEqual(u.call_count, 1)

    def test_login_failure_returns_none(self):
        with patch.object(hb.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertFalse(hb.hb_login())
            self.assertIsNone(hb.hb_get("/x"))


class TestUnit(_State, unittest.TestCase):
    def test_motion_only_on_rising_edge(self):
        hb._token = "t"
        seq = [[_acc("MotionSensor", "Door", "Motion Detected", v)] for v in (1, 1, 0, 1)]
        with patch.object(hb, "get_db", return_value=self.conn):
            for accs in seq:
                with patch.object(hb.urllib.request, "urlopen", return_value=_resp(accs)):
                    hb.poll_cycle()
        self.assertEqual(self.cur.execute.call_count, 2)

    def test_empty_accessories_never_touches_db(self):
        hb._token = "t"
        with patch.object(hb.urllib.request, "urlopen", return_value=_resp([])), patch.object(hb, "get_db") as g:
            hb.poll_cycle()
        g.assert_not_called()


class TestIntegration(_State, unittest.TestCase):
    def test_temperature_goes_to_nova_meta_with_metric_name(self):
        hb._token = "t"
        with patch.object(hb.urllib.request, "urlopen",
                          return_value=_resp([_acc("TemperatureSensor", "NVR Core", "Current Temperature", 51.5)])), \
                patch.object(hb, "get_db", return_value=self.conn):
            hb.poll_cycle()
        sql, params = self.cur.execute.call_args.args
        self.assertIn("INSERT INTO telemetry.nova_meta", sql)
        self.assertEqual(params[:2], ("nvr_temp_nvr_core", 51.5))
        self.assertEqual(json.loads(params[2])["source"], "homebridge")

    def test_get_db_reuses_open_connection(self):
        with patch.object(hb.psycopg2, "connect", return_value=self.conn) as c:
            self.assertIs(hb.get_db(), hb.get_db())
        self.assertEqual(c.call_count, 1)


class TestFunctional(_State, unittest.TestCase):
    def test_main_loop_runs_until_shutdown_and_resets_conn_on_error(self):
        calls = []

        def cycle():
            calls.append(1)
            hb._conn = "stale"
            if len(calls) == 2:
                hb._shutdown = True
            raise RuntimeError("pg gone")
        with patch.object(hb, "poll_cycle", side_effect=cycle), patch.object(hb.time, "sleep") as sl:
            try:
                hb.main()
            finally:
                hb._shutdown = False
        self.assertEqual(len(calls), 2)
        self.assertIsNone(hb._conn)
        self.assertEqual(sl.call_count, hb.POLL_INTERVAL)

    def test_motion_golden_path_writes_observation(self):
        hb._token = "t"
        with patch.object(hb.urllib.request, "urlopen",
                          return_value=_resp([_acc("MotionSensor", "Driveway", "Motion Detected", True)])), \
                patch.object(hb, "get_db", return_value=self.conn):
            hb.poll_cycle()
        sql, params = self.cur.execute.call_args.args
        self.assertIn("INSERT INTO shared_observations", sql)
        self.assertEqual(json.loads(params[1])["camera"], "Driveway")
        self.cur.close.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_homebridge_poller"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "HOME": home, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)
        self.assertNotIn("starting", r.stderr)


if __name__ == "__main__":
    unittest.main()
