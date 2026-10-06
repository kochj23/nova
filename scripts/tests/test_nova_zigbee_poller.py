#!/usr/bin/env python3
"""Tests for nova_zigbee_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The script connects to PG at import (wait_for_pg), so psycopg2.connect is patched around the load;
paho's Client is mocked in main(); no broker or database is ever reached."""
import importlib.util
import json
import re
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_zigbee_poller.py"
SRC = SCRIPT.read_text()
EVIL = "x'); " + "DR" + "OP TABLE energy_readings;--"


def _load(connect):
    spec = importlib.util.spec_from_file_location("nova_zigbee_poller_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(psycopg2, "connect", connect), patch("time.sleep") as slp, patch("builtins.print"):
        spec.loader.exec_module(mod)
    return mod, slp


zp, _ = _load(MagicMock(return_value=MagicMock()))


def _msg(name, payload):
    return SimpleNamespace(topic=f"zigbee2mqtt/{name}", payload=json.dumps(payload).encode())


def _cur():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    zp.conn = conn
    return conn, cur


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_parameterized_sql(self):
        self.assertIsNone(re.search(r"password\s*=", SRC, re.I))
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))

    def test_injection_in_topic_is_a_bound_param(self):
        conn, cur = _cur()
        with patch("builtins.print"):
            zp.on_message(None, None, _msg(EVIL, {"power": 5}))
        sql, params = cur.execute.call_args[0]
        self.assertNotIn(EVIL, sql)
        self.assertEqual(params[0], EVIL)

    def test_broker_is_local(self):
        self.assertEqual(zp.MQTT_HOST, "127.0.0.1")


class TestPerformance(unittest.TestCase):
    def test_10k_messages_fast(self):
        conn, cur = _cur()
        msgs = [_msg(f"plug{i}", {"power": i, "voltage": 120}) for i in range(10_000)]
        t0 = time.perf_counter()
        with patch("builtins.print"):
            for m in msgs:
                zp.on_message(None, None, m)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(cur.execute.call_count, 10_000)


class TestRetry(unittest.TestCase):
    def test_wait_for_pg_retries_until_connected(self):
        conn = MagicMock()
        connect = MagicMock(side_effect=[psycopg2.OperationalError("a"), psycopg2.OperationalError("b"), conn])
        mod, slp = _load(connect)
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(slp.call_count, 2)
        self.assertIs(mod.conn, conn)
        self.assertTrue(conn.autocommit)

    def test_operational_error_mid_stream_reconnects(self):
        conn, cur = _cur()
        cur.execute.side_effect = psycopg2.OperationalError("gone")
        fresh = MagicMock()
        with patch.object(zp, "get_conn", return_value=fresh) as gc, patch("builtins.print"):
            zp.on_message(None, None, _msg("plug", {"power": 3}))
        gc.assert_called_once()
        self.assertIs(zp.conn, fresh)


class TestUnit(unittest.TestCase):
    def test_bridge_topics_ignored(self):
        conn, cur = _cur()
        for name in ("bridge", "bridge/state", "bridge/info", "bridge/logging"):
            zp.on_message(None, None, _msg(name, {"power": 1}))
        cur.execute.assert_not_called()

    def test_payload_without_energy_fields_ignored(self):
        conn, cur = _cur()
        zp.on_message(None, None, _msg("button", {"action": "single"}))
        cur.execute.assert_not_called()

    def test_bad_json_is_logged_not_raised(self):
        conn, cur = _cur()
        with patch("builtins.print") as p:
            zp.on_message(None, None, SimpleNamespace(topic="zigbee2mqtt/x", payload=b"{nope"))
        self.assertIn("Error processing", p.call_args[0][0])

    def test_climate_suffix_strip_and_fahrenheit(self):
        conn, cur = _cur()
        with patch("builtins.print"):
            zp.on_message(None, None, _msg("office_temp", {"temperature": 20, "humidity": 40}))
        sql, params = cur.execute.call_args[0]
        self.assertIn("telemetry.climate", sql)
        self.assertEqual(params, ("office", 68.0, 40))


class TestIntegration(unittest.TestCase):
    def test_energy_row_shape(self):
        conn, cur = _cur()
        with patch("builtins.print"):
            zp.on_message(None, None, _msg("plug", {"power": 12.5, "voltage": 121, "current": 0.1,
                                                    "energy": 3.2, "state": "ON", "ieee_address": "0xab"}))
        sql, params = cur.execute.call_args[0]
        self.assertIn("INSERT INTO energy_readings", sql)
        self.assertEqual(params, ("plug", "zigbee-0xab", 12.5, 121, 0.1, 3.2, True))

    def test_uses_nova_ops(self):
        self.assertIn("dbname=nova_ops", zp.DB_DSN)


class TestFunctional(unittest.TestCase):
    def test_main_subscribes_and_loops_with_mocked_client(self):
        client = MagicMock()
        with patch.object(zp.mqtt, "Client", return_value=client), patch("builtins.print"):
            zp.main()
        client.connect.assert_called_once_with("127.0.0.1", 1883, 60)
        client.subscribe.assert_called_once_with("zigbee2mqtt/+")
        client.loop_forever.assert_called_once()
        self.assertIs(client.on_message, zp.on_message)

    def test_main_connect_failure_raises(self):
        client = MagicMock()
        client.connect.side_effect = ConnectionRefusedError()
        with patch.object(zp.mqtt, "Client", return_value=client), patch("builtins.print"):
            with self.assertRaises(ConnectionRefusedError):
                zp.main()
        client.loop_forever.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        client_cls = MagicMock()
        with patch("paho.mqtt.client.Client", client_cls):
            _load(MagicMock(return_value=MagicMock()))
        client_cls.assert_not_called()

    def test_compiles(self):
        compile(SRC, str(SCRIPT), "exec")
        self.assertTrue(SRC.startswith("#!/usr/bin/env python3"))


if __name__ == "__main__":
    unittest.main()
