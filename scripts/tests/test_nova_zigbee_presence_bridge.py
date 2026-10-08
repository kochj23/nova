#!/usr/bin/env python3
"""Tests for nova_zigbee_presence_bridge.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). PG, MQTT and the notifier are mocked; no broker is contacted and
loop_forever is never entered. Written by Jordan Koch (via Claude)."""
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
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_zigbee_presence_bridge.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("zigbee", SCRIPTS / "nova_zigbee_presence_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zb = _load()
zb.notify = MagicMock()             # never post from a test
zb.print = lambda *a, **k: None


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))


class _Conn:
    def __init__(self):
        self.sql = []; self.closed = False; self.autocommit = False

    def cursor(self):
        return _Cur(self)


def _msg(device, payload):
    return SimpleNamespace(topic=f"zigbee2mqtt/{device}",
                           payload=(json.dumps(payload) if not isinstance(payload, bytes) else payload).encode()
                           if not isinstance(payload, bytes) else payload)


def _reset():
    zb._last_present.clear(); zb._last_notified.clear(); zb.notify.reset_mock()
    zb._conn = _Conn()


class TestSecurity(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertEqual(zb.MQTT_HOST, "127.0.0.1")

    def test_unknown_devices_ignored_and_values_bound(self):
        zb.on_message(None, None, _msg("garage_door'; --", {"presence": True}))
        self.assertEqual(zb._conn.sql, [])
        zb.on_message(None, None, _msg("office_presence", {"presence": True, "target_distance": "1'); --"}))
        sql, params = zb._conn.sql[0]
        self.assertNotIn("1');", sql)
        self.assertIn("1'); --", params[2])
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))


class TestPerformance(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_10k_messages_fast_and_pings_rate_limited(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            zb.on_message(None, None, _msg("office_presence", {"presence": bool(i % 2), "temperature": 21}))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(zb.notify.call_count, 1)        # one ping per room per cooldown


class TestRetry(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_main_waits_for_pg_then_subscribes(self):
        zb._conn = None
        attempts = []

        def connect(dsn):
            attempts.append(dsn)
            if len(attempts) < 3:
                raise zb.psycopg2.OperationalError("starting up")
            return _Conn()

        client = MagicMock()
        with patch.object(zb.psycopg2, "connect", side_effect=connect), \
             patch.object(zb.time, "sleep") as sl, patch.object(zb.mqtt, "Client", return_value=client):
            zb.main()
        self.assertEqual(len(attempts), 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [2, 2])
        client.connect.assert_called_once_with("127.0.0.1", 1883, 60)
        client.loop_forever.assert_called_once()

    def test_write_error_fails_open(self):
        class Boom(_Conn):
            def cursor(self):
                raise RuntimeError("pg gone")
        zb._conn = Boom()
        self.assertIsNone(zb.on_message(None, None, _msg("office_presence", {"presence": True})))


class TestUnit(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_c_to_f(self):
        self.assertEqual(zb._c_to_f(0), 32.0)
        self.assertEqual(zb._c_to_f("21.5"), 70.7)
        self.assertIsNone(zb._c_to_f(None))
        self.assertIsNone(zb._c_to_f("warm"))

    def test_notify_only_on_arrival_edge(self):
        zb.maybe_notify_presence("office", False)
        zb.maybe_notify_presence("office", True)
        zb.maybe_notify_presence("office", True)
        self.assertEqual(zb.notify.call_count, 1)
        self.assertEqual(zb.notify.call_args[1]["dedup_key"], "presence:office")
        zb._last_notified["office"] = 0
        zb.maybe_notify_presence("office", False); zb.maybe_notify_presence("office", True)
        self.assertEqual(zb.notify.call_count, 2)

    def test_bad_payload_ignored(self):
        zb.on_message(None, None, SimpleNamespace(topic="zigbee2mqtt/office_presence", payload=b"{nope"))
        self.assertEqual(zb._conn.sql, [])


class TestIntegration(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_presence_mirrors_mmwave_tables(self):
        zb.write_presence("office", True, {"pir_detection": True})
        [(s1, p1)] = zb._conn.sql                           # presence_state is the engine's alone
        self.assertIn("INSERT INTO telemetry.presence", s1)
        self.assertIn("'mmwave'", s1)
        self.assertEqual(p1[:2], ("office", zb.PRESENCE_CONFIDENCE))
        zb._conn.sql.clear()
        zb.write_presence("office", False, {})
        self.assertEqual(len(zb._conn.sql), 1)
        self.assertEqual(zb._conn.sql[0][1][1], 0.0)


class TestFunctional(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_message_writes_presence_climate_and_pings(self):
        zb.on_message(None, None, _msg("patio_presence", {"presence": True, "temperature": 20, "humidity": 40,
                                                          "illuminance": 300}))
        tables = [s.split("INTO ")[1].split(" ")[0] for s, _ in zb._conn.sql]
        self.assertEqual(tables, ["telemetry.presence", "telemetry.climate"])
        self.assertEqual(zb._conn.sql[-1][1], ("patio", 68.0, 40, 300, True))
        self.assertIn("Patio", zb.notify.call_args[0][0])

    def test_climate_only_message(self):
        zb.on_message(None, None, _msg("office_presence", {"battery": 90}))
        self.assertEqual(zb._conn.sql, [])
        zb.on_message(None, None, _msg("office_presence", {"humidity": 55}))
        self.assertEqual(zb._conn.sql[0][1], ("office", None, 55, None, None))
        zb.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # main() connects to PG + the MQTT broker and loops forever, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_zigbee_presence_bridge"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
