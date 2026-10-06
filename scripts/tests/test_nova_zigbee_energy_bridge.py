#!/usr/bin/env python3
"""Tests for nova_zigbee_energy_bridge.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_zigbee_energy_bridge.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zb = _load("zigbee_bridge_under_test", SCRIPT)


class _Cur:
    def __init__(self, fail=None):
        self.fail, self.sql, self.params = fail, [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)
        if self.fail:
            raise self.fail


class _Conn:
    def __init__(self, cur):
        self.cur, self.closed, self.autocommit = cur, 0, False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = 1


def _msg(topic, payload):
    return types.SimpleNamespace(topic=topic, payload=payload if isinstance(payload, bytes) else json.dumps(payload).encode())


class _Base(unittest.TestCase):
    def setUp(self):
        zb._conn = None; zb._last.clear()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", zb.DSN)
        self.assertEqual((zb.MQTT_HOST, zb.MQTT_PORT), ("127.0.0.1", 1883))

    def test_sql_is_parameterized_and_only_writes_telemetry_energy(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"telemetry.energy"})
        cur = _Cur()
        with patch.object(zb.psycopg2, "connect", return_value=_Conn(cur)):
            zb.on_message(None, None, _msg("zigbee2mqtt/plug'); DROP TABLE telemetry.energy; --", {"power": 1, "voltage": 120}))
        self.assertNotIn("DROP", cur.sql[0]); self.assertEqual(cur.params[0][0], "plug'); DROP TABLE telemetry.energy; --")

    def test_bridge_and_subtopic_messages_never_reach_the_database(self):
        pg = MagicMock()
        with patch.object(zb.psycopg2, "connect", pg):
            for t in ("zigbee2mqtt/bridge/state", "zigbee2mqtt/bridge", "zigbee2mqtt/plug/availability", "zigbee2mqtt/plug/get"):
                zb.on_message(None, None, _msg(t, {"power": 5, "voltage": 120}))
        pg.assert_not_called()


class TestPerformance(_Base):
    def test_10k_distinct_plug_readings_are_inserted_quickly(self):
        cur = _Cur()
        t0 = time.perf_counter()
        with patch.object(zb.psycopg2, "connect", return_value=_Conn(cur)) as pg:
            for i in range(10_000):
                zb.on_message(None, None, _msg(f"zigbee2mqtt/plug{i}", {"power": i, "voltage": 120.1, "current": 0.5, "energy": 1.5, "state": "ON"}))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(cur.sql), 10_000)
        self.assertEqual(pg.call_count, 1)                       # one pooled connection, not one per reading


class TestRetry(_Base):
    def test_broken_connection_is_dropped_and_the_insert_retried_once_on_a_fresh_one(self):
        bad, good = _Cur(fail=RuntimeError("server closed the connection")), _Cur()
        conns = [_Conn(bad), _Conn(good)]
        with patch.object(zb.psycopg2, "connect", side_effect=conns) as pg:
            zb.on_message(None, None, _msg("zigbee2mqtt/plug", {"power": 7, "voltage": 121}))
        self.assertEqual(pg.call_count, 2)
        self.assertEqual(conns[0].closed, 1)                       # the broken one was closed, not leaked
        self.assertEqual(len(good.sql), 1); self.assertEqual(good.params[0][2], 7.0)
        self.assertIs(zb._conn, conns[1])

    def test_second_failure_is_logged_and_never_raises(self):
        out = io.StringIO()
        with patch.object(zb.psycopg2, "connect", side_effect=[_Conn(_Cur(fail=RuntimeError("a"))), _Conn(_Cur(fail=RuntimeError("b")))]), \
             redirect_stdout(out):
            zb.on_message(None, None, _msg("zigbee2mqtt/plug", {"power": 7, "voltage": 121}))
        self.assertIn("write error for plug (reconnect failed): b", out.getvalue())
        self.assertIsNone(zb._conn)                                 # next reading reconnects from scratch

    def test_main_retries_the_initial_pg_connect_with_backoff(self):
        client = MagicMock()
        with patch.object(zb.psycopg2, "connect", side_effect=[OSError("1"), OSError("2"), _Conn(_Cur())]) as pg, \
             patch.object(zb.time, "sleep") as sl, patch.object(zb.mqtt, "Client", return_value=client), \
             redirect_stdout(io.StringIO()):
            zb.main()
        self.assertEqual(pg.call_count, 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [2, 2])
        client.loop_forever.assert_called_once()


class TestUnit(_Base):
    def test_float_coercion(self):
        self.assertEqual(zb._f("12.5"), 12.5); self.assertEqual(zb._f(3), 3.0)
        self.assertIsNone(zb._f(None)); self.assertIsNone(zb._f("n/a")); self.assertIsNone(zb._f({}))

    def test_junk_and_non_metering_payloads_are_ignored(self):
        pg = MagicMock()
        with patch.object(zb.psycopg2, "connect", pg):
            zb.on_message(None, None, _msg("zigbee2mqtt/plug", b"not json"))
            zb.on_message(None, None, _msg("zigbee2mqtt/plug", [1, 2]))
            zb.on_message(None, None, _msg("zigbee2mqtt/sensor", {"temperature": 21.5, "voltage": 3.0}))
            zb.on_message(None, None, _msg("zigbee2mqtt/bulb", {"power": 9}))
        pg.assert_not_called()

    def test_per_device_throttle(self):
        cur = _Cur()
        with patch.object(zb.psycopg2, "connect", return_value=_Conn(cur)):
            zb.on_message(None, None, _msg("zigbee2mqtt/a", {"power": 1, "voltage": 120}))
            zb.on_message(None, None, _msg("zigbee2mqtt/a", {"power": 2, "voltage": 120}))   # within MIN_INTERVAL_S
            zb.on_message(None, None, _msg("zigbee2mqtt/b", {"power": 3, "voltage": 120}))   # other device not throttled
            zb._last["a"] -= zb.MIN_INTERVAL_S + 1
            zb.on_message(None, None, _msg("zigbee2mqtt/a", {"power": 4, "voltage": 120}))
        self.assertEqual([p[2] for p in cur.params], [1.0, 3.0, 4.0])

    def test_drop_conn_is_idempotent(self):
        zb._drop_conn(); zb._drop_conn()
        self.assertIsNone(zb._conn)
        c = _Conn(_Cur()); zb._conn = c; zb._drop_conn()
        self.assertEqual(c.closed, 1); self.assertIsNone(zb._conn)


class TestIntegration(_Base):
    def test_row_shape_written_to_telemetry_energy(self):
        cur = _Cur()
        with patch.object(zb.psycopg2, "connect", return_value=_Conn(cur)) as pg:
            zb.on_message(None, None, _msg("zigbee2mqtt/Office Plug", {"power": "12.5", "voltage": 119.8, "current": "0.1", "energy": 42, "state": "off"}))
            zb._last.clear()
            zb.on_message(None, None, _msg("zigbee2mqtt/Office Plug", {"power": 0, "voltage": 120, "state": "ON"}))
            zb._last.clear()
            zb.on_message(None, None, _msg("zigbee2mqtt/Office Plug", {"power": 0, "voltage": 120}))
        self.assertTrue(pg.return_value.autocommit)
        self.assertIn("INSERT INTO telemetry.energy (ts, device_id, device_name, watts, volts, amps, kwh_total, on_state) VALUES (now(), %s, %s, %s, %s, %s, %s, %s)", cur.sql[0])
        self.assertEqual(cur.params[0], ("Office Plug", "Office Plug", 12.5, 119.8, 0.1, 42.0, False))
        self.assertEqual(cur.params[1][3:], (120.0, None, None, True))
        self.assertIsNone(cur.params[2][-1])                        # no state key -> NULL, not False

    def test_main_resubscribes_on_every_reconnect(self):
        client = MagicMock()
        with patch.object(zb.psycopg2, "connect", return_value=_Conn(_Cur())), \
             patch.object(zb.mqtt, "Client", return_value=client), redirect_stdout(io.StringIO()) as out:
            zb.main()
            client.on_connect(client, None, None, 0)        # simulate a (re)connect while stdout is still captured
        self.assertIs(client.on_message, zb.on_message)
        client.subscribe.assert_called_once_with("zigbee2mqtt/+")
        client.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=60)
        self.assertIn("(re)connected rc=0, subscribed zigbee2mqtt/+", out.getvalue())


class TestFunctional(_Base):
    def test_golden_path_connects_to_the_local_broker_and_streams(self):
        client = MagicMock()
        with patch.object(zb.psycopg2, "connect", return_value=_Conn(_Cur())) as pg, \
             patch.object(zb.mqtt, "Client", return_value=client), redirect_stdout(io.StringIO()) as out:
            zb.main()
        pg.assert_called_once_with(zb.DSN)
        client.connect.assert_called_once_with("127.0.0.1", 1883, 60)
        client.loop_forever.assert_called_once()
        self.assertIn("streaming Zigbee plug power -> telemetry.energy", out.getvalue())

    def test_error_path_broker_down_escapes_after_pg_is_ready(self):
        client = MagicMock(); client.connect.side_effect = ConnectionRefusedError("no broker")
        with patch.object(zb.psycopg2, "connect", return_value=_Conn(_Cur())), \
             patch.object(zb.mqtt, "Client", return_value=client), redirect_stdout(io.StringIO()):
            with self.assertRaises(ConnectionRefusedError):
                zb.main()
        client.loop_forever.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_starts_the_loop(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_zigbee_energy_bridge"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
