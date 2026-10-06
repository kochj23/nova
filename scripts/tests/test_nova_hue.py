#!/usr/bin/env python3
"""Tests for nova_hue.py — the 7 house categories (Security, Performance, Retry, Unit, Integration,
Functional, Frame). The bridge (hue_get/hue_put), Keychain, psql and notifications are all mocked;
no light ever changes, no port is bound, no daemon thread is started.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_hue.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_hue_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hue = _load()
GROUPS = {"1": {"type": "Room", "name": "Kitchen", "lights": ["1", "2"], "state": {"any_on": True, "all_on": False},
                "action": {"on": True, "bri": 127}},
          "2": {"type": "Room", "name": "Office", "lights": ["3"], "state": {"any_on": False}, "action": {}},
          "3": {"type": "Zone", "name": "Carport", "lights": [], "state": {}, "action": {}},
          "9": {"type": "LightGroup", "name": "ignored"}}
SCENES = {"s1": {"name": "Relax", "group": "1"}, "s2": {"name": "Bright", "group": "1"}}
PROBE = "x'); SELECT pg_sleep(9);--"


def _bridge(sensors=None):
    data = {"groups": GROUPS, "scenes": SCENES, "sensors": sensors or {},
            "lights": {"1": {"name": "L1", "state": {"on": True, "bri": 10}}, "2": {"name": "L2", "state": {}}}}
    return lambda path: data.get(path)


class _Base(unittest.TestCase):
    def setUp(self):
        self.ps = [patch.object(hue, "hue_get", side_effect=_bridge()), patch.object(hue, "hue_put", return_value=[{}]),
                   patch.object(hue, "insert_observation"), patch.object(hue.nova_config, "notify_local"),
                   patch.object(hue.urllib.request, "urlopen", side_effect=OSError("offline"))]
        self.get, self.put, self.obs, self.local, _ = [p.start() for p in self.ps]

    def tearDown(self):
        for p in self.ps:
            p.stop()


class _Handler(hue.HueAPIHandler):
    def __init__(self, method, path, body=None):  # no socket: drive do_GET/do_POST directly
        self.path = path
        raw = json.dumps(body).encode() if body is not None else b""
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile, self.wfile = io.BytesIO(raw), io.BytesIO()
        self.status = None
        getattr(self, "do_" + method)()

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue())


class TestSecurity(_Base):
    def test_no_hardcoded_api_key(self):
        self.assertNotRegex(SRC, r"(?i)(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9-]{16,}['\"]")
        self.assertIn('"nova-hue-api-key"', SRC)

    def test_api_key_from_env_or_keychain(self):
        with patch.object(hue, "_api_key_cache", None), patch.dict(os.environ, {"NOVA_HUE_API_KEY": " k1 "}):
            self.assertEqual(hue.get_api_key(), "k1")
        env = {k: v for k, v in os.environ.items() if k != "NOVA_HUE_API_KEY"}
        with patch.object(hue, "_api_key_cache", None), patch.dict(os.environ, env, clear=True), \
             patch.object(hue.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "kc-key\n", "")) as run:
            self.assertEqual(hue.get_api_key(), "kc-key")
        self.assertEqual(run.call_args[0][0][:2], ["security", "find-generic-password"])

    def test_observation_sql_escapes_quotes(self):
        self.ps[2].stop()
        try:
            with patch.object(hue.subprocess, "run") as run:
                hue.insert_observation("nova_hue", "security", PROBE, "it's here", metadata={"sensor": "Jordan's Porch"})
        finally:
            self.obs = self.ps[2].start()
        sql = run.call_args[0][0][-1]
        self.assertIn("'x''); SELECT pg_sleep(9);--'", sql)
        self.assertIn("Jordan''s Porch", sql)
        self.assertEqual(sql.count("'") % 2, 0)

    def test_unknown_command_changes_nothing(self):
        self.assertIn("don't understand", hue.execute_command("launch the rockets"))
        self.put.assert_not_called()


class TestPerformance(_Base):
    def test_sensor_parse_10k(self):
        sensors = {str(i): {"type": "ZLLTemperature", "name": f"t{i}", "state": {"temperature": 2150},
                            "config": {"reachable": True}} for i in range(10_000)}
        self.get.side_effect = _bridge(sensors)
        t0 = time.perf_counter()
        out = hue.get_all_sensors()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(out["5"]["temperature"], 21.5)


class TestRetry(unittest.TestCase):
    def test_bridge_failure_fails_open(self):
        # RETRY GAP: hue_get/hue_put — one request each; None on failure, fetchers return {}
        with patch.object(hue, "_api_key_cache", "k"), \
             patch.object(hue.urllib.request, "urlopen", side_effect=OSError("bridge down")) as u, \
             patch("sys.stderr", new_callable=io.StringIO):
            self.assertIsNone(hue.hue_put("groups/1/action", {"on": False}))
            self.assertEqual(hue.get_all_lights(), {})
        self.assertEqual(u.call_count, 2)


class TestUnit(_Base):
    def test_command_parsing(self):
        self.assertEqual(hue.execute_command("dim kitchen to 50%"), "Dimmed Kitchen to 50%.")
        self.assertEqual(self.put.call_args[0], ("groups/1/action", {"on": True, "bri": 127}))
        self.assertEqual(hue.execute_command("set office to blue"), "Set Office to blue.")
        self.assertEqual(self.put.call_args[0][1]["hue"], 46920)
        self.assertEqual(hue.execute_command("activate relax in kitchen"), "Activated 'Relax' in Kitchen.")
        self.assertEqual(hue.execute_command("office off"), "Turned off Office.")
        self.assertEqual(hue.execute_command("turn on garage"), "Room 'garage' not found.")

    def test_status_and_rooms(self):
        self.assertEqual(sorted(hue.get_all_rooms()), ["1", "2", "3"])
        self.assertIn("Kitchen (brightness: 50%)", hue.execute_command("status"))

    def test_find_outdoor_sensors_fallback(self):
        s = {"a": {"name": "Hall motion", "type": "ZLLPresence"}, "b": {"name": "Hall temp", "type": "ZLLTemperature"}}
        out = hue.find_outdoor_sensors(s)
        self.assertEqual((out["motion"]["name"], out["lightlevel"]), ("Hall motion", None))


class TestIntegration(_Base):
    def test_hue_url_uses_bridge_and_key(self):
        with patch.object(hue, "_api_key_cache", "KEY"):
            self.assertEqual(hue.hue_url("lights"), f"http://{hue.HUE_BRIDGE}/api/KEY/lights")

    def test_all_off_targets_every_room_only(self):
        self.assertEqual(hue.execute_command("all off"), "All lights turned off.")
        self.assertEqual(sorted(c[0][0] for c in self.put.call_args_list),
                         ["groups/1/action", "groups/2/action", "groups/3/action"])


class TestFunctional(_Base):
    def test_http_command_and_status(self):
        h = _Handler("POST", "/command", {"text": "kitchen on"})
        self.assertEqual((h.status, h.json()), (200, {"result": "Turned on Kitchen."}))
        h = _Handler("GET", "/status")
        self.assertEqual((h.json()["rooms_on"], h.json()["lights_on"]), (1, 1))
        self.assertEqual(_Handler("POST", "/command", {}).status, 400)
        self.assertEqual(_Handler("GET", "/nope").status, 404)

    def test_room_scene_name_resolved(self):
        h = _Handler("POST", "/rooms/1/action", {"scene": "bright"})
        self.assertEqual(h.json()["applied"], {"scene": "s2"})
        self.put.return_value = None
        self.assertEqual(_Handler("POST", "/lights/1/state", {"on": True}).status, 500)

    def test_night_motion_triggers_carport_once_per_poll(self):
        sensors = {"m": {"type": "ZLLPresence", "name": "Outdoor motion", "state": {"presence": True}, "config": {}}}
        self.get.side_effect = _bridge(sensors)
        with patch.object(hue.automation, "is_night", return_value=True), \
             patch.object(hue, "turn_on_carport_temporarily") as carport, \
             patch.object(hue.time, "sleep", side_effect=StopIteration), redirect_stdout(io.StringIO()):
            with self.assertRaises(StopIteration):
                hue.sensor_monitor_loop()
        carport.assert_called_once()
        self.assertEqual(self.obs.call_args[0][1:3], ("security", "outdoor_motion"))
        self.assertTrue(self.local.call_args.kwargs["critical"])


class TestFrame(unittest.TestCase):
    def test_import_never_starts_server(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_hue; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
