#!/usr/bin/env python3
"""Tests for nova_zwave_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
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

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_zwave_poller.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="zwave-poller-test-"))
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


class _Cur:
    def __init__(self, fail=None):
        self.sql = []; self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.fail:
            raise self.fail
        self.sql.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self, cur=None):
        self.cur = cur or _Cur(); self.autocommit = False

    def cursor(self):
        return self.cur


def _mqtt_stub():
    paho = types.ModuleType("paho"); mq = types.ModuleType("paho.mqtt"); client = types.ModuleType("paho.mqtt.client")
    client.CallbackAPIVersion = types.SimpleNamespace(VERSION2="v2")
    client.Client = MagicMock()
    paho.mqtt = mq; mq.client = client
    return {"paho": paho, "paho.mqtt": mq, "paho.mqtt.client": client}


def _load():
    spec = importlib.util.spec_from_file_location("zwave_poller_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # the module connects to PG at import (wait_for_pg); hand it a stub connection
    with _stub_modules(_mqtt_stub()), patch.object(psycopg2, "connect", return_value=_Conn()):
        spec.loader.exec_module(mod)
    return mod


zp = _load()


def _msg(topic, payload):
    return types.SimpleNamespace(topic=topic, payload=payload if isinstance(payload, bytes) else json.dumps(payload).encode())


def _deliver(topic, payload, cur=None):
    cur = cur or _Cur()
    zp.conn = _Conn(cur)
    with redirect_stdout(io.StringIO()) as out:
        zp.on_message(None, None, _msg(topic, payload))
    return cur, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("subprocess", SRC); self.assertEqual(zp.MQTT_HOST, "127.0.0.1")   # local broker only

    def test_topic_fragments_are_bound_parameters(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        cur, _ = _deliver("zwave/x'); DROP TABLE energy_readings; --/50/0/value/65537", {"value": 3})
        sql, params = cur.sql[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[0], "x'); DROP TABLE energy_readings; --")

    def test_only_meter_classes_reach_the_database(self):
        cur, _ = _deliver("zwave/2/38/0/targetValue", {"value": 99})     # switch class, not a meter
        self.assertEqual(cur.sql, [])


class TestPerformance(unittest.TestCase):
    def test_10k_messages_fast(self):
        cur = _Cur(); zp.conn = _Conn(cur)
        t0 = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            for i in range(10_000):
                zp.on_message(None, None, _msg("zwave/2/50/0/value/65537", {"value": i}))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(cur.sql), 10_000)


class TestRetry(unittest.TestCase):
    def test_wait_for_pg_retries_with_a_fixed_backoff(self):
        with patch.object(psycopg2, "connect", side_effect=[psycopg2.OperationalError("x"), psycopg2.OperationalError("y"), _Conn()]) as c, \
             patch("time.sleep") as slp, redirect_stdout(io.StringIO()):
            conn = zp.wait_for_pg()
        self.assertEqual(c.call_count, 3)
        self.assertEqual(slp.call_args_list[0][0], (5,)); self.assertEqual(slp.call_count, 2)
        self.assertTrue(conn.autocommit)

    def test_lost_connection_during_insert_reconnects_once_and_never_raises(self):
        # RETRY GAP: on_message — an OperationalError triggers one reconnect attempt; the reading is dropped
        fresh = _Conn()
        with patch.object(psycopg2, "connect", return_value=fresh) as c:
            cur, out = _deliver("zwave/2/50/0/value/65537", {"value": 1}, cur=_Cur(fail=psycopg2.OperationalError("gone")))
        self.assertEqual(c.call_count, 1); self.assertIs(zp.conn, fresh)
        with patch.object(psycopg2, "connect", side_effect=psycopg2.OperationalError("still gone")):
            _deliver("zwave/2/50/0/value/65537", {"value": 1}, cur=_Cur(fail=psycopg2.OperationalError("gone")))


class TestUnit(unittest.TestCase):
    def test_short_and_unknown_topics_are_ignored(self):
        for topic in ("zwave/2", "zwave/2/50", "zwave/2/50/0/value/999999"):
            cur, _ = _deliver(topic, {"value": 1}); self.assertEqual(cur.sql, [], topic)

    def test_payload_forms(self):
        cur, _ = _deliver("zwave/2/50/0/value/65537", b"12.5")                  # raw bytes
        self.assertEqual(cur.sql[0][1][2], 12.5)
        cur, _ = _deliver("zwave/2/50/0/value/65537", {"value": None}); self.assertEqual(cur.sql, [])
        cur, _ = _deliver("zwave/2/50/0/value/65537", {"value": "abc"}); self.assertEqual(cur.sql, [])
        cur, _ = _deliver("zwave/2/50/0/value/65537", 7); self.assertEqual(cur.sql[0][1][2], 7.0)

    def test_metric_mapping_by_value_id_and_by_name(self):
        cases = {"66049": (None, None, None, 2.0), "66561": (None, 2.0, None, None), "66817": (None, None, 2.0, None),
                 "Electric_W_Consumed": (2.0, None, None, None), "voltage": (None, 2.0, None, None),
                 "current": (None, None, 2.0, None), "energy_kwh": (None, None, None, 2.0),
                 "watt_hours": (None, None, None, 2.0)}
        for vid, expect in cases.items():
            cur, _ = _deliver(f"zwave/2/Meter/0/{vid}", {"value": 2}, )
            self.assertEqual(cur.sql[0][1][2:6], expect, vid)

    def test_friendly_device_names(self):
        cur, out = _deliver("zwave/2/50/0/value/65537", {"value": 1})
        self.assertEqual(cur.sql[0][1][:2], ("kitchen_tv", "zwave-kitchen_tv"))
        self.assertIn("node-kitchen_tv: watts=1.0", out)
        cur, _ = _deliver("zwave/9/50/0/value/65537", {"value": 1})
        self.assertEqual(cur.sql[0][1][:2], ("9", "zwave-9"))


class TestIntegration(unittest.TestCase):
    def test_insert_targets_energy_readings_with_the_shared_columns(self):
        cur, _ = _deliver("zwave/2/50/0/value/65537", {"value": 40.5})
        sql, params = cur.sql[0]
        self.assertTrue(sql.startswith("INSERT INTO energy_readings (ts, device_name, device_id, watts, voltage, amperes, total_kwh, relay_on)"))
        self.assertEqual(params, ("kitchen_tv", "zwave-kitchen_tv", 40.5, None, None, None))

    def test_main_wires_the_v2_client_to_the_zwave_tree(self):
        client = MagicMock()
        with patch.object(zp.mqtt, "Client", return_value=client) as ctor, redirect_stdout(io.StringIO()):
            zp.main()
        ctor.assert_called_once_with("v2")
        self.assertIs(client.on_message, zp.on_message)
        client.connect.assert_called_once_with("127.0.0.1", 1883, 60)
        client.subscribe.assert_called_once_with("zwave/#"); client.loop_forever.assert_called_once()


class TestFunctional(unittest.TestCase):
    def test_golden_path_meter_reading(self):
        cur, out = _deliver("zwave/2/50/0/value/66049", {"value": 123.456, "time": 1})
        self.assertEqual(cur.sql[0][1], ("kitchen_tv", "zwave-kitchen_tv", None, None, None, 123.456))
        self.assertEqual(out.strip(), "[nova-zwave-poller] node-kitchen_tv: kwh=123.456")

    def test_generic_db_error_is_logged_not_raised(self):
        cur, out = _deliver("zwave/2/50/0/value/65537", {"value": 1}, cur=_Cur(fail=ValueError("bad row")))
        self.assertIn("[nova-zwave-poller] Error: bad row", out)


class TestFrame(unittest.TestCase):
    def test_import_connects_to_pg_only_through_the_mocked_driver_and_never_starts_mqtt(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        boot = ("import sys, types, unittest.mock as um, psycopg2, runpy; "
                "p = um.MagicMock(); p.mqtt.client.Client.side_effect = AssertionError('mqtt at import'); "
                "sys.modules['paho'] = p; sys.modules['paho.mqtt'] = p.mqtt; sys.modules['paho.mqtt.client'] = p.mqtt.client; "
                "psycopg2.connect = um.MagicMock(); runpy.run_path(sys.argv[1], run_name='imported'); "
                "print('IMPORT_OK', psycopg2.connect.call_count)")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK 1")


if __name__ == "__main__":
    unittest.main()
