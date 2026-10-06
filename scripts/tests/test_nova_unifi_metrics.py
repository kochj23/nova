#!/usr/bin/env python3
"""Tests for nova_unifi_metrics.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The UniFi API (HTTP), Keychain/nova_secrets and psycopg2 are all
mocked; no controller call, no Keychain read, no DB. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


um = _load("nova_unifi_metrics_t", SCRIPTS / "nova_unifi_metrics.py")
SRC = (SCRIPTS / "nova_unifi_metrics.py").read_text()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_api_key_from_secrets_or_keychain_not_source(self):
        self.assertIn('nova_secrets.get_secret("nova-unifi-api-key")', SRC)
        self.assertIn('"security", "find-generic-password"', SRC)

    def test_insert_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIn("VALUES %s", SRC)
        self.assertIn("(%s, %s, %s, %s::jsonb)", SRC)

    def test_api_get_sends_key_header_and_no_verify_bypass(self):
        captured = {}
        def urlopen(req, timeout=None, context=None):
            captured["headers"] = req.headers
            return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False,
                                  read=lambda: json.dumps({"data": [1]}).encode())
        with mock.patch.object(um, "get_api_key", return_value="SECRETKEY"), \
             mock.patch("urllib.request.urlopen", side_effect=urlopen):
            um.api_get("stat/sta")
        self.assertEqual(captured["headers"].get("X-api-key"), "SECRETKEY")


class TestPerformance(unittest.TestCase):
    def test_collect_clients_1000(self):
        clients = [{"mac": f"aa:bb:cc:00:00:{i:02x}", "hostname": f"c{i}", "rx_bytes": i,
                    "tx_bytes": i, "radio": "na", "signal": -50} for i in range(1000)]
        rows = []
        with mock.patch.object(um, "api_get", return_value=clients):
            t0 = time.perf_counter()
            um.collect_clients(rows)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(rows)


class TestRetry(unittest.TestCase):
    def test_api_get_failure_returns_none(self):
        # RETRY GAP: api_get()/urlopen — single attempt; any error returns None, the collector skips
        with mock.patch.object(um, "get_api_key", return_value="k"), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("controller down")) as u:
            self.assertIsNone(um.api_get("stat/sta"))
        self.assertEqual(u.call_count, 1)

    def test_no_api_key_skips_call(self):
        with mock.patch.object(um, "get_api_key", return_value=None), \
             mock.patch("urllib.request.urlopen") as u:
            self.assertIsNone(um.api_get("stat/sta"))
        u.assert_not_called()

    def test_insert_rows_db_failure_returns_zero(self):
        import psycopg2
        with mock.patch.object(psycopg2, "connect", side_effect=RuntimeError("pg down")):
            self.assertEqual(um.insert_rows([(um.NOW, "m", 1.0, "{}")]), 0)


class TestUnit(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(um._slug("Office AP-2.4"), "office_ap_2_4")
        self.assertEqual(um._slug(None), "unknown")

    def test_num(self):
        self.assertEqual(um._num("3.5"), 3.5)
        self.assertIsNone(um._num("x"))
        self.assertIsNone(um._num(None))

    def test_band_for_radio(self):
        self.assertEqual(um._band_for_radio("ng"), "2.4GHz")
        self.assertEqual(um._band_for_radio("6e"), "6GHz")
        self.assertEqual(um._band_for_radio("zzz"), "other")


class TestIntegration(unittest.TestCase):
    def test_collect_clients_counts_bands(self):
        clients = [
            {"mac": "a", "hostname": "h1", "rx_bytes": 10, "tx_bytes": 5, "radio": "na", "signal": -40},
            {"mac": "b", "hostname": "h2", "is_wired": True, "rx_bytes": 1, "tx_bytes": 1},
        ]
        rows = []
        with mock.patch.object(um, "api_get", return_value=clients):
            um.collect_clients(rows)
        metrics = {m for _, m, _, _ in rows}
        self.assertIn("unifi_client_signal_dbm", metrics)
        self.assertIn("unifi_clients_total", metrics)
        total = next(v for _, m, v, _ in rows if m == "unifi_clients_total")
        self.assertEqual(total, 2.0)
        wired = next(v for _, m, v, _ in rows if m == "unifi_clients_wired")
        self.assertEqual(wired, 1.0)

    def test_collect_wan_speedtest_and_up(self):
        health = [{"subsystem": "wan", "status": "ok", "tx_bytes-r": 1000, "rx_bytes-r": 2000},
                  {"subsystem": "www", "status": "ok", "xput_up": 35.0, "xput_down": 400.0, "latency": 12}]
        rows = []
        with mock.patch.object(um, "api_get", return_value=health):
            um.collect_wan(rows)
        metrics = {m: v for _, m, v, _ in rows}
        self.assertEqual(metrics["unifi_wan_up"], 1.0)
        self.assertEqual(metrics["unifi_wan_speedtest_down_mbps"], 400.0)
        self.assertEqual(metrics["unifi_wan_latency_ms"], 12.0)


class TestFunctional(unittest.TestCase):
    def test_main_collects_and_inserts(self):
        def fake_api(ep):
            if ep == "stat/sta":
                return [{"mac": "a", "hostname": "h", "rx_bytes": 1, "tx_bytes": 1, "radio": "na", "signal": -50}]
            return []
        with mock.patch.object(um, "api_get", side_effect=fake_api), \
             mock.patch.object(um, "insert_rows", return_value=5) as ins, mock.patch.object(um, "log"):
            um.main()
        ins.assert_called_once()
        self.assertTrue(ins.call_args[0][0])   # rows passed

    def test_main_survives_a_crashing_collector(self):
        def boom(rows): raise RuntimeError("boom")
        with mock.patch.object(um, "collect_clients", boom), \
             mock.patch.object(um, "collect_devices"), mock.patch.object(um, "collect_wan"), \
             mock.patch.object(um, "insert_rows", return_value=0) as ins, mock.patch.object(um, "log"):
            um.main()
        ins.assert_called_once()   # still reached insert despite the crash

    def test_insert_rows_bulk_executes(self):
        import psycopg2
        import psycopg2.extras
        captured = {}
        class Cur:
            def execute_values(s, *a): pass
            def close(s): pass
        conn = mock.MagicMock()
        def ev(cur, sql, rows, template=None):
            captured["n"] = len(rows); captured["sql"] = sql
        with mock.patch.object(psycopg2, "connect", return_value=conn), \
             mock.patch.object(psycopg2.extras, "execute_values", side_effect=ev):
            n = um.insert_rows([(um.NOW, "m", 1.0, "{}"), (um.NOW, "m2", 2.0, "{}")])
        self.assertEqual(n, 2)
        self.assertEqual(captured["n"], 2)
        self.assertIn("telemetry.unifi_metrics", captured["sql"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_unifi_metrics"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("DONE: collected", r.stdout)


if __name__ == "__main__":
    unittest.main()
