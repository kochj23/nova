#!/usr/bin/env python3
"""Tests for nova_zigbee_plug_onboard.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_zigbee_plug_onboard.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    cfg = types.ModuleType("nova_config")
    cfg.SLACK_FEED = "C_FEED"
    cfg.post_both = MagicMock()
    return {"nova_config": cfg}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # argv is parsed at import (prefix/count/hours); nova_config is bound at import — both restored after
    with patch.dict(sys.modules, _stub_modules()), patch.object(sys, "argv", ["nova_zigbee_plug_onboard.py"]):
        spec.loader.exec_module(mod)
    return mod


zb = _load("zigbee_onboard_under_test", SCRIPT)
DEFAULTS = (zb.PREFIX, zb.COUNT, zb.HOURS)          # captured at import, before any test mutates them


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def _plug(ieee, name=None, model="TS011F", nested=False):
    exp = [{"type": "composite", "features": [{"property": "power"}]}] if nested else [{"property": "power"}]
    return {"ieee_address": ieee, "friendly_name": name, "definition": {"model": model, "exposes": exp}}


def _bulb(ieee):
    return {"ieee_address": ieee, "friendly_name": "bulb", "definition": {"model": "LCA001", "exposes": [{"property": "brightness"}]}}


def _reset(count=2, prefix="laundry_plug"):
    zb.known = set(); zb.assigned.clear()
    zb.COUNT, zb.PREFIX = count, prefix
    zb.nova_config.post_both = MagicMock()


def _devices(c, devs):
    with redirect_stdout(io.StringIO()):
        zb.on_message(c, None, _Msg("zigbee2mqtt/bridge/devices", devs))


def _published(c, topic):
    return [json.loads(a[0][1]) for a in c.publish.call_args_list if a[0][0] == topic]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("username_pw_set", SRC)                  # broker is loopback, no creds in source
        self.assertEqual((zb.MQTT_HOST, zb.MQTT_PORT), ("127.0.0.1", 1883))

    def test_rename_payload_is_json_encoded_not_interpolated(self):
        _reset()
        c = MagicMock()
        _devices(c, [_plug("0xaa")])
        evil = 'bad"name,"to":"pwned'
        _devices(c, [_plug("0xaa"), _plug("0xbb", name=evil)])
        renames = _published(c, "zigbee2mqtt/bridge/request/device/rename")
        self.assertEqual(renames, [{"from": evil, "to": "laundry_plug_1"}])   # the quote survived as data, not structure

    def test_slack_never_raises_and_uses_feed_channel(self):
        _reset()
        zb.nova_config.post_both = MagicMock(side_effect=RuntimeError("slack 500"))
        with redirect_stdout(io.StringIO()) as out:
            zb.slack("hello")
        self.assertIn("slack: slack 500", out.getvalue())


class TestPerformance(unittest.TestCase):
    def test_is_plug_and_baseline_fast_on_10k_devices(self):
        devs = [_plug(f"0x{i:04x}", nested=bool(i % 2)) if i % 3 else _bulb(f"0x{i:04x}") for i in range(10_000)]
        t0 = time.perf_counter()
        n = sum(1 for d in devs if zb.is_plug(d["definition"]))
        _reset(); _devices(MagicMock(), devs)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(n, 6_666)
        self.assertEqual(len(zb.known), 10_000)


class TestRetry(unittest.TestCase):
    def test_slack_failure_is_swallowed(self):
        # RETRY GAP: slack() — one attempt; a post_both failure is logged and onboarding continues
        _reset()
        zb.nova_config.post_both = MagicMock(side_effect=OSError("down"))
        c = MagicMock()
        _devices(c, [_plug("0xaa")])
        _devices(c, [_plug("0xaa"), _plug("0xbb")])
        self.assertEqual(zb.assigned, {"0xbb": "laundry_plug_1"})     # rename happened despite the slack failure
        self.assertEqual(zb.nova_config.post_both.call_count, 1)

    def test_bad_payload_and_foreign_topic_are_ignored(self):
        # RETRY GAP: on_message — malformed JSON is dropped silently; no exception reaches paho's loop thread
        _reset()
        c = MagicMock()
        zb.on_message(c, None, _Msg("zigbee2mqtt/bridge/devices", b"{not json"))
        zb.on_message(c, None, _Msg("zigbee2mqtt/bridge/event", [{"ieee_address": "0x1"}]))
        self.assertEqual(zb.known, set()); c.publish.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_is_plug_edges(self):
        self.assertFalse(zb.is_plug(None)); self.assertFalse(zb.is_plug({}))
        self.assertFalse(zb.is_plug({"exposes": ["power", 3]}))
        self.assertTrue(zb.is_plug({"exposes": [{"property": "power"}]}))
        self.assertTrue(zb.is_plug({"exposes": [{"features": [{"property": "power"}]}]}))
        self.assertFalse(zb.is_plug({"exposes": [{"features": ["power"]}]}))

    def test_baseline_then_rename_cap_and_skip_rules(self):
        _reset(count=1, prefix="dryer")
        c = MagicMock()
        _devices(c, [_plug("0xaa"), {"friendly_name": "no-ieee"}])
        self.assertEqual(zb.known, {"0xaa"}); c.publish.assert_not_called()
        _devices(c, [_plug("0xaa"), _bulb("0xbb"), {"ieee_address": "0xcc"}, _plug("0xdd", nested=True), _plug("0xee")])
        self.assertEqual(zb.assigned, {"0xdd": "dryer_1"})               # bulb + uninterviewed skipped, cap of 1 honored
        self.assertEqual(_published(c, "zigbee2mqtt/bridge/request/device/rename"), [{"from": "0xdd", "to": "dryer_1"}])
        _devices(c, [_plug("0xaa"), _plug("0xdd", name="dryer_1")])
        self.assertEqual(c.publish.call_count, 1)                          # already-assigned device is not renamed twice

    def test_reissue_and_log_format(self):
        c = MagicMock()
        zb.reissue(c)
        c.publish.assert_called_once_with("zigbee2mqtt/bridge/request/permit_join", json.dumps({"time": 254}))
        with redirect_stdout(io.StringIO()) as out:
            zb.log("hi")
        self.assertRegex(out.getvalue(), r"^\[plug-onboard\] \d\d:\d\d:\d\d hi\n$")


class TestIntegration(unittest.TestCase):
    def test_on_connect_subscribes_opens_and_announces(self):
        _reset()
        c = MagicMock()
        with redirect_stdout(io.StringIO()):
            zb.on_connect(c, None, None, 0)
        self.assertEqual([a[0][0] for a in c.subscribe.call_args_list], ["zigbee2mqtt/bridge/devices", "zigbee2mqtt/bridge/event"])
        self.assertEqual(_published(c, "zigbee2mqtt/bridge/request/permit_join"), [{"time": 254}])
        zb.nova_config.post_both.assert_called_once()
        self.assertEqual(zb.nova_config.post_both.call_args[1], {"slack_channel": "C_FEED", "discord_channel": None})
        self.assertIn("up to 2 laundry plugs", zb.nova_config.post_both.call_args[0][0])

    def test_defaults_and_reissue_cadence_fit_z2m_cap(self):
        self.assertEqual(DEFAULTS, ("laundry_plug", 2, 2.0))
        self.assertLess(zb.REISSUE_EVERY, 254)
        self.assertIn("permit_join", SRC)


def _run_main(assigned_after=None, ticks=None):
    """Drive main() with a fake mqtt client, no real sleeping, and a scripted clock."""
    clock = iter(ticks or [0.0, 0.0, 10_000.0])
    client = MagicMock()

    def sleep(_s):
        if assigned_after:
            zb.assigned.update(assigned_after)
    with patch.object(zb.mqtt, "Client", return_value=client), patch.object(zb.time, "sleep", sleep), \
         patch.object(zb.time, "time", lambda: next(clock)), redirect_stdout(io.StringIO()) as out:
        zb.main()
    return client, out.getvalue()


class TestFunctional(unittest.TestCase):
    def test_golden_path_onboards_two_and_closes_network(self):
        _reset()
        client, out = _run_main(assigned_after={"0x1": "laundry_plug_1", "0x2": "laundry_plug_2"},
                                ticks=[0.0, 0.0, 100.0, 200.0, 300.0])
        client.connect.assert_called_once_with("127.0.0.1", 1883, 60)
        client.loop_start.assert_called_once(); client.loop_stop.assert_called_once()
        self.assertEqual(_published(client, "zigbee2mqtt/bridge/request/permit_join"), [{"time": 254}, {"time": 0}])
        self.assertIn("2/2 onboarded; network closed", out)
        final = zb.nova_config.post_both.call_args[0][0]
        self.assertIn("2/2 adopted (laundry_plug_1, laundry_plug_2)", final)

    def test_deadline_with_nothing_paired_still_closes_network(self):
        _reset()
        client, out = _run_main(ticks=[0.0, 10_000.0, 10_000.0])
        self.assertEqual(_published(client, "zigbee2mqtt/bridge/request/permit_join"), [{"time": 0}])
        self.assertIn("0/2 onboarded", out)
        self.assertTrue(zb.nova_config.post_both.call_args[0][0].endswith("0/2 adopted. Network closed."))


class TestFrame(unittest.TestCase):
    def test_import_never_opens_the_network(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        code = ("import sys, types\n"
                "cfg = types.ModuleType('nova_config'); cfg.SLACK_FEED='C'; cfg.post_both=lambda *a, **k: None\n"
                "sys.modules['nova_config'] = cfg\n"
                "import paho.mqtt.client as m\n"
                "m.Client = lambda *a, **k: (_ for _ in ()).throw(AssertionError('main ran at import'))\n"
                "sys.argv = ['x', 'dryer', '3', '0.5']\n"
                "import nova_zigbee_plug_onboard as z\n"
                "print(z.PREFIX, z.COUNT, z.HOURS)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "dryer 3 0.5")


if __name__ == "__main__":
    unittest.main()
