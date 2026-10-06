#!/usr/bin/env python3
"""Tests for nova_zigbee_lqi_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
mosquitto_sub (subprocess.run) and psycopg2.connect are mocked; nothing touches MQTT or PG."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_zigbee_lqi_poller.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_zigbee_lqi_poller_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lq = _load()
DEVICES = json.dumps([
    {"friendly_name": "Coordinator", "type": "Coordinator", "ieee_address": "0x0"},
    {"friendly_name": "lamp", "type": "Router", "ieee_address": "0x1"},
    {"friendly_name": "door", "type": "EndDevice", "ieee_address": "0x2"},
])
STATES = "\n".join([
    'zigbee2mqtt/lamp {"linkquality": 120, "state": "ON"}',
    'zigbee2mqtt/door {"linkquality": 60}',
    'zigbee2mqtt/bridge {"linkquality": 1}',
    'zigbee2mqtt/lamp/set {"linkquality": 9}',
    'zigbee2mqtt/ghost not-json',
    'garbage-line-without-space',
])


def _sub(dev_out=DEVICES, state_out=STATES):
    def run(args, **kw):
        out = dev_out if "zigbee2mqtt/bridge/devices" in args else state_out
        return SimpleNamespace(stdout=out, returncode=0, stderr="")
    return run


def _pg():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    return conn, cur


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_parameterized_insert(self):
        self.assertIsNone(re.search(r"password\s*=", SRC, re.I))
        self.assertIn("VALUES (%s, %s, %s, %s, %s)", SRC)
        self.assertNotIn('execute(f"', SRC)

    def test_broker_is_local_only(self):
        self.assertEqual(lq.MQTT[lq.MQTT.index("-h") + 1], "127.0.0.1")


class TestPerformance(unittest.TestCase):
    def test_parse_10k_state_lines_fast(self):
        big = "\n".join(f'zigbee2mqtt/d{i} {{"linkquality": {i % 255}}}' for i in range(10_000))
        t0 = time.perf_counter()
        with patch.object(lq.subprocess, "run", side_effect=_sub(state_out=big)):
            res = lq.get_lqi()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(res), 10_000)


class TestRetry(unittest.TestCase):
    def test_bad_roster_fails_open(self):
        # RETRY GAP: get_devices()/_sub — mosquitto_sub is called once; unparseable output -> {}
        with patch.object(lq.subprocess, "run", side_effect=_sub(dev_out="")) as run:
            self.assertEqual(lq.get_devices(), {})
        self.assertEqual(run.call_count, 1)

    def test_pg_failure_propagates_after_one_attempt(self):
        # RETRY GAP: main()/psycopg2.connect — single attempt; launchd re-runs next cycle
        with patch.object(lq.subprocess, "run", side_effect=_sub()), \
             patch.object(lq.psycopg2, "connect", side_effect=lq.psycopg2.OperationalError("down")) as c:
            with self.assertRaises(lq.psycopg2.OperationalError):
                lq.main()
        self.assertEqual(c.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_get_devices_drops_coordinator(self):
        with patch.object(lq.subprocess, "run", side_effect=_sub()):
            d = lq.get_devices()
        self.assertEqual(set(d), {"lamp", "door"})
        self.assertEqual(d["lamp"], {"ieee": "0x1", "type": "Router"})

    def test_get_lqi_filters_bridge_subtopics_and_junk(self):
        with patch.object(lq.subprocess, "run", side_effect=_sub()):
            self.assertEqual(lq.get_lqi(), {"lamp": 120, "door": 60})

    def test_get_lqi_empty(self):
        with patch.object(lq.subprocess, "run", side_effect=_sub(state_out="")):
            self.assertEqual(lq.get_lqi(), {})


class TestIntegration(unittest.TestCase):
    def test_writes_to_telemetry_zigbee_link(self):
        self.assertIn("INSERT INTO telemetry.zigbee_link", SRC)
        self.assertIn("dbname=nova_ops", lq.DSN)

    def test_unknown_device_gets_null_ieee(self):
        conn, cur = _pg()
        with patch.object(lq.subprocess, "run", side_effect=_sub(dev_out="[]")), \
             patch.object(lq.psycopg2, "connect", return_value=conn), patch("builtins.print"):
            lq.main()
        rows = cur.executemany.call_args[0][1]
        self.assertTrue(all(r[2] is None and r[3] is None for r in rows))


class TestFunctional(unittest.TestCase):
    def test_main_writes_one_row_per_device(self):
        conn, cur = _pg()
        with patch.object(lq.subprocess, "run", side_effect=_sub()), \
             patch.object(lq.psycopg2, "connect", return_value=conn), patch("builtins.print") as p:
            self.assertEqual(lq.main(), 0)
        rows = cur.executemany.call_args[0][1]
        self.assertEqual(sorted((r[1], r[2], r[4]) for r in rows), [("door", "0x2", 60), ("lamp", "0x1", 120)])
        conn.close.assert_called_once()
        self.assertIn("avg LQI 90", p.call_args[0][0])

    def test_no_data_skips_pg(self):
        with patch.object(lq.subprocess, "run", side_effect=_sub(state_out="")), \
             patch.object(lq.psycopg2, "connect") as c, patch("builtins.print"):
            self.assertEqual(lq.main(), 0)
        c.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_zigbee_lqi_poller"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
