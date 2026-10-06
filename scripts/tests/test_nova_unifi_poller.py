#!/usr/bin/env python3
"""Tests for nova_unifi_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The module installs signal handlers and a file log at import, so it is loaded with Path.home() pointed
at a tempdir, logging.basicConfig stubbed, and the previous SIGINT/SIGTERM handlers restored afterwards.
Keychain (subprocess.run), the controller (_opener.open) and psycopg2 are mocked."""
import importlib.util
import json
import logging
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_unifi_poller.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="unifi_"))


def _load():
    spec = importlib.util.spec_from_file_location("nova_unifi_poller_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        with patch.object(pathlib.Path, "home", return_value=TMP), patch.object(logging, "basicConfig"):
            spec.loader.exec_module(mod)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    mod.log = logging.getLogger("unifi_poller_test")
    mod.log.addHandler(logging.NullHandler())
    mod.log.propagate = False
    return mod


up = _load()
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _resp(obj):
    m = MagicMock()
    m.read.return_value = json.dumps(obj).encode()
    return m


def _pg():
    conn = MagicMock()
    return conn


class _Reset(unittest.TestCase):
    def setUp(self):
        up._previous_poll, up._api_key, up._shutdown = {}, None, False


class TestSecurity(_Reset):
    def test_api_key_from_keychain_not_source(self):
        self.assertIsNone(re.search(r"(api[_-]?key|password)\s*=\s*['\"][A-Za-z0-9_-]{12,}['\"]", SRC, re.I))
        with patch.object(up.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="KEY\n")) as run:
            self.assertEqual(up._get_unifi_api_key(), "KEY")
        self.assertEqual(run.call_args[0][0][:2], ["security", "find-generic-password"])

    def test_missing_key_exits_never_continues_unauthenticated(self):
        with patch.object(up.subprocess, "run", return_value=SimpleNamespace(returncode=44, stdout="")):
            with self.assertRaises(SystemExit):
                up._get_unifi_api_key()

    def test_key_sent_as_header_and_import_restored_signals(self):
        up._api_key = "KEY"
        with patch.object(up._opener, "open", return_value=_resp({"data": []})) as o:
            up._unifi_get(up.CLIENTS_URL)
        self.assertEqual(o.call_args[0][0].get_header("X-api-key"), "KEY")
        self.assertIsNot(signal.getsignal(signal.SIGINT), up._handle_signal)


class TestPerformance(_Reset):
    def test_10k_clients_build_rows_fast(self):
        clients = [{"mac": f"AA:{i:06d}", "rx_bytes": i, "tx_bytes": i} for i in range(10_000)]
        with patch.object(up, "_get_db", return_value=_pg()), patch("psycopg2.extras.execute_values") as ev:
            t0 = time.perf_counter()
            n = up._insert_client_stats(clients, NOW)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(n, 10_000)
        self.assertEqual(len(ev.call_args[0][2]), 10_000)


class TestRetry(_Reset):
    def test_fetch_relogins_once_then_succeeds(self):
        with patch.object(up, "_unifi_get", side_effect=[None, {"data": [{"mac": "a"}]}]) as g, \
             patch.object(up, "_unifi_login", return_value=True) as li:
            self.assertEqual(up._fetch_clients(), [{"mac": "a"}])
        self.assertEqual(g.call_count, 2)
        li.assert_called_once()

    def test_fetch_gives_up_after_second_failure(self):
        with patch.object(up, "_unifi_get", return_value=None) as g, patch.object(up, "_unifi_login", return_value=True):
            self.assertIsNone(up._fetch_devices())
        self.assertEqual(g.call_count, 2)

    def test_http_and_db_errors_fail_open(self):
        err = urllib.error.HTTPError("u", 401, "unauth", {}, None)
        with patch.object(up._opener, "open", side_effect=err):
            self.assertIsNone(up._unifi_get(up.CLIENTS_URL))
        with patch.object(up, "_get_db", side_effect=RuntimeError("pg down")):
            self.assertEqual(up._insert_ap_metrics([{"type": "usw", "name": "s"}], NOW), 0)


class TestUnit(_Reset):
    def test_deltas_and_counter_reset(self):
        with patch.object(up, "_get_db", return_value=_pg()), patch("psycopg2.extras.execute_values") as ev:
            up._insert_client_stats([{"mac": "AA", "rx_bytes": 100, "tx_bytes": 50}], NOW)
            self.assertEqual(ev.call_args[0][2][0][4:6], (100, 50))
            up._insert_client_stats([{"mac": "aa", "rx_bytes": 160, "tx_bytes": 20}], NOW)
        self.assertEqual(ev.call_args[0][2][0][4:6], (60, 20))     # rx delta, tx reset -> raw

    def test_client_without_mac_skipped_and_hostname_fallback(self):
        with patch.object(up, "_get_db", return_value=_pg()), patch("psycopg2.extras.execute_values") as ev:
            self.assertEqual(up._insert_client_stats([{"ip": "1.2.3.4"}], NOW), 0)
            up._insert_client_stats([{"mac": "bb", "oui": "Apple"}], NOW)
        self.assertEqual(ev.call_args[0][2][0][2], "Apple")

    def test_ap_metric_names(self):
        devs = [{"type": "uap", "name": "Living Room-AP", "system-stats": {"cpu": "5", "mem": "40"}, "num_sta": 3,
                 "radio_table_stats": [{"name": "wifi0", "num_sta": 2, "satisfaction": 98}]},
                {"type": "udm", "name": "GW", "sys_stats": {"cpu": 1}}, {"type": "other"}]
        with patch.object(up, "_get_db", return_value=_pg()), patch("psycopg2.extras.execute_values") as ev:
            self.assertEqual(up._insert_ap_metrics(devs, NOW), 6)
        names = [r[1] for r in ev.call_args[0][2]]
        self.assertIn("unifi_ap_living_room_ap_cpu", names)
        self.assertIn("unifi_ap_living_room_ap_wifi0_satisfaction", names)
        self.assertIn("unifi_gw_gw_cpu", names)


class TestIntegration(_Reset):
    def test_tables_and_parameterized_templates(self):
        with patch.object(up, "_get_db", return_value=_pg()), patch("psycopg2.extras.execute_values") as ev:
            up._insert_client_stats([{"mac": "a"}], NOW)
            self.assertIn("telemetry.network", ev.call_args[0][1])
            self.assertEqual(ev.call_args.kwargs["template"].count("%s"), 13)
            up._insert_ap_metrics([{"type": "usw", "name": "s"}], NOW)
        self.assertIn("telemetry.nova_meta", ev.call_args[0][1])
        self.assertIn("dbname=nova_ops", up.DB_DSN)


class TestFunctional(_Reset):
    def test_poll_once_golden_path(self):
        with patch.object(up, "_fetch_clients", return_value=[{"mac": "a", "is_wired": True}, {"mac": "b"}]), \
             patch.object(up, "_fetch_devices", return_value=[{"type": "usw", "name": "s"}]), \
             patch.object(up, "_insert_client_stats", return_value=2) as ic, \
             patch.object(up, "_insert_ap_metrics", return_value=1) as ia, \
             patch.object(up.log, "info") as info:
            up._poll_once()
        ic.assert_called_once(); ia.assert_called_once()
        self.assertTrue(any("1 wireless, 1 wired" in c[0][0] for c in info.call_args_list))

    def test_main_loop_stops_on_shutdown_and_survives_errors(self):
        def stop(_):
            up._shutdown = True
        with patch.object(up, "_unifi_login", return_value=False), \
             patch.object(up, "_poll_once", side_effect=RuntimeError("boom")) as po, \
             patch.object(up.time, "sleep", side_effect=stop):
            up.main()
        po.assert_called_once()
        up._handle_signal(15, None)
        self.assertTrue(up._shutdown)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home, "PYTHONPATH": str(SCRIPTS)}
            r = subprocess.run([sys.executable, "-c", "import nova_unifi_poller"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("starting", r.stdout)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
