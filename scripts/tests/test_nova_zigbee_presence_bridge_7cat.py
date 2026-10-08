#!/usr/bin/env python3
"""7-category tests for nova_zigbee_presence_bridge.py (2026-10-08: telemetry.presence only, never
presence_state; PG writes and MQTT connect retried with backoff). Complements
test_nova_zigbee_presence_bridge.py. PG, MQTT, notify and sleep are mocked. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_zigbee_presence_bridge.py"
SRC = SCRIPT.read_text()

_spec = importlib.util.spec_from_file_location("zigbee_7cat", SCRIPT)
zb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zb)
zb.notify = MagicMock()
zb.print = lambda *a, **k: None


class Conn:
    def __init__(self, fail=0):
        self.sql, self.closed, self.autocommit, self.fail = [], False, False, fail

    def cursor(self):
        if self.fail:
            self.fail -= 1
            raise zb.psycopg2.OperationalError("server closed the connection")
        conn = self

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None): conn.sql.append((sql, params))
        return Cur()

    def close(self): self.closed = True


def msg(device, payload):
    return SimpleNamespace(topic=f"zigbee2mqtt/{device}", payload=json.dumps(payload).encode())


class _Base(unittest.TestCase):
    def setUp(self):
        zb._last_present.clear(); zb._last_notified.clear(); zb.notify.reset_mock()
        self.conns = []

        def connect(dsn):
            c = Conn()
            self.conns.append(c)
            return c
        zb._conn = None
        for p in (patch.object(zb.psycopg2, "connect", side_effect=connect), patch.object(zb.time, "sleep")):
            m = p.start()
            self.addCleanup(p.stop)
        self.sleep = m

    def all_sql(self):
        return [s for c in self.conns for s in c.sql]


class TestSecurity(_Base):
    def test_never_writes_presence_state(self):
        zb.on_message(None, None, msg("office_presence", {"presence": True, "temperature": 20}))
        joined = " ".join(s for s, _ in self.all_sql())
        self.assertNotIn("presence_state", joined)
        self.assertIn("telemetry.presence", joined)

    def test_topic_outside_allowlist_is_ignored(self):
        zb.on_message(None, None, msg("../office_presence", {"presence": True}))
        self.assertEqual(self.conns, [])

    def test_notify_payload_carries_room_only(self):
        zb.on_message(None, None, msg("office_presence", {"presence": True, "target_distance": 1.2}))
        kw = zb.notify.call_args.kwargs
        self.assertEqual(kw["meta"], {"room": "office"})
        self.assertEqual(kw["level"], "info")


class TestPerformance(_Base):
    def test_reuses_one_connection_for_many_messages(self):
        t0 = time.perf_counter()
        for i in range(5000):
            zb.on_message(None, None, msg("patio_presence", {"presence": bool(i % 2), "humidity": 40}))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(self.conns), 1)       # no reconnect per message


class TestRetry(_Base):
    def test_dropped_connection_reconnects_with_backoff(self):
        zb._conn = Conn(fail=1)
        zb.on_message(None, None, msg("office_presence", {"presence": True}))
        self.assertEqual(len(self.conns), 1)        # reconnected once
        self.assertEqual(len(self.conns[0].sql), 1)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [0.5])

    def test_gives_up_after_write_attempts(self):
        with patch.object(zb.psycopg2, "connect", side_effect=zb.psycopg2.OperationalError("down")) as c:
            zb.on_message(None, None, msg("office_presence", {"presence": True}))
        self.assertEqual(c.call_count, zb.WRITE_ATTEMPTS)
        self.assertEqual([x.args[0] for x in self.sleep.call_args_list], [0.5, 1.0])
        zb.notify.assert_not_called()               # no ping for a reading that never landed

    def test_mqtt_connect_retried(self):
        client = MagicMock()
        client.connect.side_effect = [ConnectionRefusedError("broker starting"), None]
        with patch.object(zb.mqtt, "Client", return_value=client):
            zb.main()
        self.assertEqual(client.connect.call_count, 2)
        client.loop_forever.assert_called_once()

    def test_mqtt_connect_raises_after_three(self):
        client = MagicMock()
        client.connect.side_effect = ConnectionRefusedError("no broker")
        with patch.object(zb.mqtt, "Client", return_value=client), self.assertRaises(ConnectionRefusedError):
            zb.main()
        self.assertEqual(client.connect.call_count, 3)
        client.loop_forever.assert_not_called()


class TestUnit(_Base):
    def test_with_retry_returns_value(self):
        self.assertEqual(zb._with_retry(lambda x: x * 2, 21), 42)

    def test_absent_presence_writes_zero_confidence(self):
        zb.on_message(None, None, msg("office_presence", {"presence": False}))
        self.assertEqual(self.all_sql()[0][1][1], 0.0)


class TestIntegration(_Base):
    def test_rows_use_method_the_engine_reads(self):
        zb.on_message(None, None, msg("living_room_presence", {"presence": True}))
        sql, params = self.all_sql()[0]
        self.assertIn("'mmwave'", sql)
        self.assertEqual(params[0], "living_room")
        self.assertIn("living_room", (SCRIPTS / "nova_presence_engine.py").read_text())


class TestFunctional(_Base):
    def test_arrival_golden_path(self):
        zb.on_message(None, None, msg("dylans_room_presence", {"presence": True, "temperature": 22, "illuminance": 5}))
        tables = [s.split("INTO ")[1].split()[0] for s, _ in self.all_sql()]
        self.assertEqual(tables, ["telemetry.presence", "telemetry.climate", "telemetry.climate"])  # fp300 + legacy zigbee feed
        zb.notify.assert_called_once()

    def test_pg_outage_drops_message_without_crashing(self):
        with patch.object(zb.psycopg2, "connect", side_effect=zb.psycopg2.OperationalError("down")):
            self.assertIsNone(zb.on_message(None, None, msg("office_presence", {"presence": True})))


class TestFrame(unittest.TestCase):
    def test_imports_without_connecting(self):
        code = ("import importlib.util as u;"
                f"s=u.spec_from_file_location('z', {str(SCRIPT)!r}); m=u.module_from_spec(s);"
                "s.loader.exec_module(m); assert m._conn is None and callable(m.main); print(len(m.ZIGBEE_PRESENCE))")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           cwd=str(SCRIPTS), env={**os.environ})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "5")


# ── 2026-10-08: ZHA disabled, zigbee2mqtt sole coordinator owner again ─────────────────────────────
# The SLZB-06U (.23:6638) takes one TCP client; ZHA (.6) and zigbee2mqtt (.2) were both configured for it
# and stole it from each other ~10x/day, and ZHA has no quirk exposing FP300 (lumi.sensor_occupy.agl8)
# occupancy -> FP300 presence was dark since 2026-07-30. The bridge now also writes the legacy
# source='zigbee' climate feed nova_climate_poller produced from ZHA, and garage climate (no presence).
class TestOwnershipCutover(_Base):
    # Security: a climate-only device can never assert presence
    def test_security_garage_never_writes_presence(self):
        zb.on_message(None, None, msg("garage_presence", {"presence": True, "temperature": 30}))
        joined = " ".join(s for s, _ in self.all_sql())
        self.assertNotIn("telemetry.presence", joined)
        zb.notify.assert_not_called()

    # Performance: one message -> at most 3 statements
    def test_performance_statement_count_bounded(self):
        zb.on_message(None, None, msg("office_presence", {"presence": True, "temperature": 20, "humidity": 40}))
        self.assertLessEqual(len(self.all_sql()), 3)

    # Retry: the dual climate write is retried as one unit (reconnects, then both rows land)
    def test_retry_dual_climate_write(self):
        first = {"n": 0}

        def flaky(dsn):
            first["n"] += 1
            c = Conn(fail=1 if first["n"] == 1 else 0)
            self.conns.append(c)
            return c
        with patch.object(zb.psycopg2, "connect", side_effect=flaky):
            zb.on_message(None, None, msg("office_presence", {"humidity": 41}))
        sources = [s.split("'")[1] for s, _ in self.all_sql() if "telemetry.climate" in s]
        self.assertEqual(sources, ["fp300", "zigbee"])
        self.assertGreaterEqual(self.sleep.call_count, 1)

    # Unit: both feeds carry the right room label
    def test_unit_room_labels(self):
        zb.on_message(None, None, msg("living_room_presence", {"humidity": 50}))
        rows = [(s.split("'")[1], p[0]) for s, p in self.all_sql()]
        self.assertEqual(rows, [("fp300", "living_room"), ("zigbee", "living_room_presence")])

    # Integration: the bridge subscribes to every FP300 including climate-only ones
    def test_integration_subscribes_all_six(self):
        client = MagicMock()
        with patch.object(zb.mqtt, "Client", return_value=client), patch.object(zb, "_db"):
            client.loop_forever.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                zb.main()
        topics = sorted(c.args[0] for c in client.subscribe.call_args_list)
        self.assertEqual(len(topics), 6)
        self.assertIn("zigbee2mqtt/garage_presence", topics)

    # Functional: garage climate lands only in the legacy zigbee feed
    def test_functional_garage_climate_only(self):
        zb.on_message(None, None, msg("garage_presence", {"temperature": 30, "humidity": 20}))
        rows = [(s.split("'")[1], p[0]) for s, p in self.all_sql()]
        self.assertEqual(rows, [("zigbee", "garage_presence")])

    # Frame: maps are disjoint and sane
    def test_frame_maps_disjoint(self):
        self.assertFalse(set(zb.ZIGBEE_PRESENCE) & set(zb.ZIGBEE_CLIMATE_ONLY))
        self.assertTrue(all(k.endswith("_presence") for k in list(zb.ZIGBEE_PRESENCE) + list(zb.ZIGBEE_CLIMATE_ONLY)))


if __name__ == "__main__":
    unittest.main()
