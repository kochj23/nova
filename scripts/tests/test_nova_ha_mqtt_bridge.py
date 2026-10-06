#!/usr/bin/env python3
"""Tests for nova_ha_mqtt_bridge.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_ha_mqtt_bridge.py"
SRC = SCRIPT.read_text()


def _mqtt_stub():
    pkg = types.ModuleType("paho"); mq = types.ModuleType("paho.mqtt"); cl = types.ModuleType("paho.mqtt.client")
    cl.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    cl.Client = MagicMock()
    pkg.mqtt = mq; mq.client = cl
    return {"paho": pkg, "paho.mqtt": mq, "paho.mqtt.client": cl}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _mqtt_stub()):              # the daemon binds `mqtt` at import; restored after
        spec.loader.exec_module(mod)
    return mod


hb = _load("ha_mqtt_under_test", SCRIPT)


class _Stop(Exception):
    """Raised from the mocked sleep to leave main()'s forever loop after one pass."""


def _run(answers, sleep_exc=_Stop()):
    """One pass of main(): `answers` maps a SQL substring -> psql stdout; everything else answers ''."""
    client = MagicMock()
    hb.mqtt.Client = MagicMock(return_value=client)

    def run(argv, **kw):
        sql = argv[-1]
        for frag, out in answers.items():
            if frag in sql:
                return types.SimpleNamespace(stdout=out, returncode=0)
        return types.SimpleNamespace(stdout="", returncode=0)
    client.sleep_mock = MagicMock(side_effect=sleep_exc)          # kept on the client so tests can inspect it after the patch ends
    with patch.object(hb.subprocess, "run", side_effect=run) as sp, patch.object(hb.time, "sleep", client.sleep_mock), \
         redirect_stdout(io.StringIO()) as out:
        try:
            hb.main()
        except _Stop:
            pass
    pubs = [(c.args[0], c.args[1], c.kwargs.get("retain", False)) for c in client.publish.call_args_list]
    return client, pubs, out.getvalue(), sp


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_local_broker(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertEqual((hb.MQTT_HOST, hb.MQTT_PORT), ("127.0.0.1", 1883))
        self.assertNotIn("password", " ".join(hb.PG))

    def test_psql_is_argv_no_shell_and_sql_is_read_only(self):
        self.assertNotIn("shell=True", SRC)
        with patch.object(hb.subprocess, "run", return_value=types.SimpleNamespace(stdout="")) as sp:
            hb.q("SELECT 1")
        argv = sp.call_args[0][0]
        self.assertIsInstance(argv, list); self.assertEqual(argv[0], "psql"); self.assertEqual(argv[-1], "SELECT 1")
        self.assertFalse(sp.call_args[1].get("shell", False))
        for row in hb.STATIC:
            self.assertTrue(row[2].lstrip().upper().startswith("SELECT"), row[0])
        for row in hb.DYNAMIC:
            self.assertTrue(row[2].lstrip().upper().startswith("SELECT"), row[0])
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE \w+ SET|DELETE FROM|DROP )", SRC))

    def test_dynamic_keys_are_slugified_in_sql_so_topics_stay_clean(self):
        for prefix, _, sql, *_ in hb.DYNAMIC:
            self.assertIn("regexp_replace(lower(", sql, prefix)
            self.assertIn("'[^a-z0-9]+','_','g'", sql, prefix)


class TestPerformance(unittest.TestCase):
    def test_discovery_payloads_10k_under_1s(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            hb.discovery_payload(f"plug_{i}", "Plug", "W", "power", "Energy", "mdi:power")
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_one_pass_with_10k_dynamic_rows_under_2s(self):
        rows = "\n".join(f"dev_{i}\t{i}" for i in range(10_000))
        t0 = time.perf_counter()
        client, pubs, out, _ = _run({"energy_readings WHERE ts>now()-interval '15 min' AND watts": rows})
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(sum(1 for t, _, r in pubs if t.startswith("nova/plug_") and not r), 10_000)


class TestRetry(unittest.TestCase):
    def test_query_failure_fails_open_to_empty_and_the_pass_continues(self):
        # RETRY GAP: q()/subprocess.run(psql) — one attempt; an exception or timeout returns [] and the sensor
        # simply isn't published this round (HA marks it unavailable after EXPIRE_AFTER).
        with patch.object(hb.subprocess, "run", side_effect=subprocess.TimeoutExpired("psql", 20)) as sp, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hb.q("SELECT 1"), [])
        self.assertEqual(sp.call_count, 1)
        self.assertIn("query error:", out.getvalue())
        client = MagicMock(); hb.mqtt.Client = MagicMock(return_value=client)
        with patch.object(hb.subprocess, "run", side_effect=OSError("no psql")), patch.object(hb.time, "sleep", MagicMock(side_effect=_Stop())), \
             redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(_Stop):
                hb.main()
        self.assertEqual(len(client.publish.call_args_list), len(hb.STATIC))      # discovery only, no states
        self.assertIn(f"published states ({len(hb.STATIC)} static + 0 dynamic entities)", out.getvalue())

    def test_broker_connect_failure_is_one_shot(self):
        # RETRY GAP: main()/mqtt connect — no retry; launchd restarts the daemon
        client = MagicMock(); client.connect.side_effect = ConnectionRefusedError("1883 closed")
        hb.mqtt.Client = MagicMock(return_value=client)
        with self.assertRaises(ConnectionRefusedError):
            hb.main()
        self.assertEqual(client.connect.call_count, 1)
        client.publish.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_topics(self):
        self.assertEqual(hb.disc_topic("wx_temp"), "homeassistant/sensor/nova/wx_temp/config")
        self.assertEqual(hb.state_topic("wx_temp"), "nova/wx_temp/state")

    def test_discovery_payload_shape(self):
        p = json.loads(hb.discovery_payload("wx_temp", "Outdoor Temperature", "°F", "temperature", "Weather", "mdi:x"))
        self.assertEqual(p["unique_id"], "nova_wx_temp"); self.assertEqual(p["state_topic"], "nova/wx_temp/state")
        self.assertEqual(p["expire_after"], hb.EXPIRE_AFTER); self.assertTrue(p["force_update"])
        self.assertEqual(p["device"], {"identifiers": ["nova_weather"], "name": "Nova Weather", "manufacturer": "Nova", "model": "telemetry-bridge"})
        self.assertEqual((p["unit_of_measurement"], p["state_class"], p["device_class"], p["icon"]), ("°F", "measurement", "temperature", "mdi:x"))
        bare = json.loads(hb.discovery_payload("presence_jordan_room", "Jordan Room", None, None, "Presence"))
        for k in ("unit_of_measurement", "state_class", "device_class", "icon"):
            self.assertNotIn(k, bare)
        self.assertEqual(bare["device"]["identifiers"], ["nova_presence"])

    def test_q_parses_tab_rows_and_skips_blanks(self):
        with patch.object(hb.subprocess, "run", return_value=types.SimpleNamespace(stdout="a\t1\n\n  \nb\t2\n")):
            self.assertEqual(hb.q("SELECT x"), [["a", "1"], ["b", "2"]])
        with patch.object(hb.subprocess, "run", return_value=types.SimpleNamespace(stdout="")):
            self.assertEqual(hb.q("SELECT x"), [])

    def test_sensor_tables_are_well_formed_and_unique(self):
        uids = [r[0] for r in hb.STATIC]
        self.assertEqual(len(uids), len(set(uids)))
        self.assertTrue(all(len(r) == 7 for r in hb.STATIC)); self.assertTrue(all(len(r) == 6 for r in hb.DYNAMIC))
        self.assertGreater(hb.EXPIRE_AFTER, hb.PUBLISH_INTERVAL)


class TestIntegration(unittest.TestCase):
    def test_static_values_flow_from_psql_to_state_topics(self):
        client, pubs, out, sp = _run({"FROM telemetry.weather ORDER BY ts DESC LIMIT 1": "71.3", "presence_state WHERE person='jordan' ORDER BY last_confirmed": "office"})
        states = {t: v for t, v, r in pubs if not r}
        self.assertEqual(states["nova/wx_temp/state"], "71.3")
        self.assertEqual(states["nova/presence_jordan_room/state"], "office")
        self.assertNotIn("nova/wan_latency/state", states)                     # empty answer -> no publish
        self.assertEqual(sp.call_args_list[0][0][0][:len(hb.PG)], hb.PG)
        self.assertEqual(hb.PG[:7], ["psql", "-h", "localhost", "-U", "kochj", "-d", "nova_ops"])

    def test_dynamic_rows_discover_once_then_publish_state(self):
        client, pubs, out, _ = _run({"FROM telemetry.climate WHERE ts>now()-interval '30 min' AND temp_f": "living_room\t72.5\nserver_rack\t94.0\n\t1\nonlykey"})
        disc = [(t, json.loads(v)) for t, v, r in pubs if r and t.startswith("homeassistant/sensor/nova/room_temp_")]
        self.assertEqual([t for t, _ in disc], ["homeassistant/sensor/nova/room_temp_living_room/config", "homeassistant/sensor/nova/room_temp_server_rack/config"])
        self.assertEqual(disc[0][1]["name"], "Living Room Temp"); self.assertEqual(disc[0][1]["device"]["name"], "Nova Climate")
        states = {t: v for t, v, r in pubs if not r and t.startswith("nova/room_temp_")}
        self.assertEqual(states, {"nova/room_temp_living_room/state": "72.5", "nova/room_temp_server_rack/state": "94.0"})
        self.assertIn("published states (%d static + 2 dynamic entities)" % len(hb.STATIC), out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_connects_publishes_discovery_then_sleeps(self):
        client, pubs, out, _ = _run({})
        hb.mqtt.Client.assert_called_once_with(2, client_id="nova-ha-mqtt-bridge")
        client.connect.assert_called_once_with("127.0.0.1", 1883, 60)
        client.loop_start.assert_called_once()
        disc = [(t, r) for t, v, r in pubs if t.endswith("/config")]
        self.assertEqual(len(disc), len(hb.STATIC)); self.assertTrue(all(r for _, r in disc))
        self.assertEqual(disc[0][0], "homeassistant/sensor/nova/wx_temp/config")
        self.assertIn(f"published discovery for {len(hb.STATIC)} static sensors", out)
        client.sleep_mock.assert_called_once_with(hb.PUBLISH_INTERVAL)

    def test_blank_scalar_is_never_published(self):
        client, pubs, out, _ = _run({"wan_quality": "\n"})
        self.assertFalse(any(t.startswith("nova/wan_") for t, _, r in pubs if not r))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no argparse: --help would connect to the broker and loop forever, so the smoke is an import
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ha_mqtt_bridge"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
