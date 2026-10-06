#!/usr/bin/env python3
"""Tests for nova_ha_metrics.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The Keychain (_keychain), HA REST (urlopen), asyncpg.create_pool and signal handlers are mocked;
LOG_FILE is redirected to a tempdir. No real token is read and nothing is written to PG."""
import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_ha_metrics.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_ha_metrics_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hm = _load()
TMP = Path(tempfile.mkdtemp(prefix="hametrics_"))
hm.LOG_FILE = TMP / "ha.log"
hm.print = lambda *a, **k: None     # module-local print stub (log() echoes to stdout)
hm._keychain = MagicMock(return_value=None)   # never touch the real Keychain


def _resp(body):
    m = MagicMock()
    m.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
    return m


def _st(eid, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


STATES = [
    _st("sensor.office_temp", "21.5", device_class="temperature", unit_of_measurement="°C", friendly_name="Office"),
    _st("binary_sensor.door", "on", device_class="door"),
    _st("light.lamp", "on", brightness=128),
    _st("switch.fan", "off"),
    _st("sensor.watts", "42", unit_of_measurement="W"),
    _st("sensor.text_only", "hello"),
    _st("sensor.battery", "unavailable", device_class="battery"),
]


class _Reset(unittest.TestCase):
    def setUp(self):
        hm._access_token, hm._token_expires, hm._area_map, hm._area_refreshed, hm._pool = None, 0, {}, 0, None


class TestSecurity(_Reset):
    def test_no_hardcoded_token(self):
        self.assertIsNone(re.search(r"(token|password|secret)\s*=\s*['\"][A-Za-z0-9._-]{16,}['\"]", SRC, re.I))
        self.assertIn('_keychain("nova-hass-refresh-token")', SRC)

    def test_sql_uses_positional_params(self):
        self.assertIn("VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)", SRC)
        self.assertIsNone(re.search(r"execute(many)?\(\s*f[\"']", SRC))

    def test_ha_is_loopback_and_bearer_header(self):
        self.assertTrue(hm.HA_URL.startswith("http://127.0.0.1"))
        hm._access_token, hm._token_expires = "tok", time.time() + 60
        with patch.object(hm.urllib.request, "urlopen", return_value=_resp([])) as u:
            hm.ha_get_states()
        self.assertEqual(u.call_args[0][0].get_header("Authorization"), "Bearer tok")


class TestPerformance(_Reset):
    def test_extract_10k_states_fast(self):
        states = [_st(f"sensor.s{i}", str(i), device_class="power", unit_of_measurement="W") for i in range(10_000)]
        t0 = time.perf_counter()
        rows = hm.extract_rows(states)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(rows), 10_000)


class TestRetry(_Reset):
    def test_token_failure_fails_open_and_is_cached_on_success(self):
        # RETRY GAP: get_ha_token() — one refresh attempt per call; the 60s poll loop is the retry
        with patch.object(hm, "_keychain", return_value="refresh"), \
             patch.object(hm.urllib.request, "urlopen", side_effect=[OSError("x"), _resp({"access_token": "A"})]) as u:
            self.assertIsNone(hm.get_ha_token())
            self.assertEqual(hm.get_ha_token(), "A")
            self.assertEqual(hm.get_ha_token(), "A")    # cached
        self.assertEqual(u.call_count, 2)

    def test_area_refresh_failure_backs_off(self):
        hm._access_token, hm._token_expires = "tok", time.time() + 60
        with patch.object(hm.urllib.request, "urlopen", side_effect=OSError("down")):
            hm.refresh_area_map(STATES)
        self.assertGreater(hm._area_refreshed, 0)
        self.assertEqual(hm._area_map, {})

    def test_cycle_error_keeps_loop_alive(self):
        async def run():
            calls = []

            async def boom():
                calls.append(1)
                hm._shutdown = True
                raise RuntimeError("pg")
            with patch.object(hm, "collect_once", side_effect=boom), patch.object(hm.asyncio, "sleep", AsyncMock()):
                await hm.poll_loop()
            return calls
        try:
            self.assertEqual(asyncio.run(run()), [1])
        finally:
            hm._shutdown = False
        self.assertIn("Collection cycle error: pg", hm.LOG_FILE.read_text())


class TestUnit(_Reset):
    def test_to_numeric_and_binary_flag(self):
        self.assertEqual(hm.to_numeric("3.5"), 3.5)
        self.assertIsNone(hm.to_numeric(None))
        self.assertIsNone(hm.to_numeric("on"))
        self.assertEqual((hm.binary_flag(" ON "), hm.binary_flag("locked"), hm.binary_flag("weird")), (1.0, 0.0, None))

    def test_extract_rows_shapes(self):
        rows = {r[0]: r for r in hm.extract_rows(STATES)}
        self.assertNotIn("sensor.text_only", rows)
        self.assertNotIn("sensor.battery", rows)
        self.assertEqual(rows["sensor.office_temp"][4], 21.5)
        self.assertEqual(rows["binary_sensor.door"][4:6], (1.0, "on"))
        self.assertEqual(rows["light.lamp"][4:7], (128.0, "on", "brightness"))
        self.assertEqual(rows["switch.fan"][4:6], (0.0, "off"))
        self.assertEqual(rows["sensor.watts"][6], "W")

    def test_malformed_entity_skipped(self):
        self.assertEqual(hm.extract_rows([{"state": "1"}, _st("switch.x", "on")])[0][0], "switch.x")


class TestIntegration(_Reset):
    def test_area_map_feeds_rows(self):
        hm._access_token, hm._token_expires = "tok", time.time() + 60
        body = b"switch.fan\tGarage\nlight.lamp\tNone\nbad-line\n"
        with patch.object(hm.urllib.request, "urlopen", return_value=_resp(body)):
            hm.refresh_area_map(STATES)
        self.assertEqual(hm._area_map, {"switch.fan": "Garage"})
        rows = {r[0]: r for r in hm.extract_rows(STATES)}
        self.assertEqual(rows["switch.fan"][7], "Garage")
        self.assertIsNone(rows["light.lamp"][7])

    def test_write_rows_uses_pool(self):
        conn = MagicMock(); conn.executemany = AsyncMock()
        pool = MagicMock(); pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(hm.asyncpg, "create_pool", AsyncMock(return_value=pool)) as cp:
            n = asyncio.run(hm.write_rows([("e", "f", "d", None, 1.0, None, "u", None)]))
            self.assertEqual(asyncio.run(hm.write_rows([])), 0)
        self.assertEqual(n, 1)
        self.assertEqual(cp.call_args[0][0], hm.DB_DSN)
        sql, recs = conn.executemany.call_args[0]
        self.assertIn("telemetry.ha_sensors", sql)
        self.assertEqual(len(recs[0]), 9)


class TestFunctional(_Reset):
    def test_collect_once_golden_path(self):
        with patch.object(hm, "ha_get_states", return_value=STATES), \
             patch.object(hm, "refresh_area_map") as ram, patch.object(hm, "write_rows", AsyncMock(return_value=5)) as wr:
            self.assertEqual(asyncio.run(hm.collect_once()), 5)
        ram.assert_called_once()
        self.assertEqual(len(wr.call_args[0][0]), 5)

    def test_collect_once_no_states(self):
        with patch.object(hm, "ha_get_states", return_value=None), patch.object(hm, "write_rows", AsyncMock()) as wr:
            self.assertEqual(asyncio.run(hm.collect_once()), 0)
        wr.assert_not_called()

    def test_main_installs_handlers_and_closes_pool(self):
        pool = MagicMock(); pool.close = AsyncMock()
        hm._pool = pool
        with patch.object(hm, "signal") as sig, patch.object(hm, "get_ha_token", return_value=None), \
             patch.object(hm, "poll_loop", AsyncMock()):
            asyncio.run(hm.main())
        self.assertEqual(sig.signal.call_count, 2)
        pool.close.assert_awaited_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ha_metrics"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
