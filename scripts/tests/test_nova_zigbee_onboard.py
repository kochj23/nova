#!/usr/bin/env python3
"""Tests for nova_zigbee_onboard.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_zigbee_onboard.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(sys, "argv", ["nova_zigbee_onboard.py"]):      # the module reads argv at import
        spec.loader.exec_module(mod)
    return mod


zb = _load("zb", SCRIPT)


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic; self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


class _Client:
    """paho stand-in: records publishes, fires on_connect when the loop starts, never opens a socket."""
    def __init__(self, *a, **k):
        self.published = []; self.on_connect = None; self.on_message = None; self.subs = []; self.connected = None

    def connect(self, host, port, keepalive):
        self.connected = (host, port)

    def subscribe(self, topic):
        self.subs.append(topic)

    def publish(self, topic, payload):
        self.published.append((topic, json.loads(payload)))

    def loop_start(self):
        if self.on_connect:
            self.on_connect(self)

    def loop_stop(self):
        pass


class _Clock:
    """Deterministic time for main(): every sleep advances the clock by its argument."""
    def __init__(self):
        self.now = 1_000_000.0; self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s); self.now += s

    def strftime(self, fmt):
        return "00:00:00"


def _reset():
    zb.KNOWN.clear(); zb.joined["done"] = False


def _event(ieee, typ="device_interview", status="successful", defn=None, friendly=None):
    data = {"ieee_address": ieee, "status": status, "friendly_name": friendly}
    if defn is not None:
        data["definition"] = defn
    return _Msg("zigbee2mqtt/bridge/event", {"type": typ, "data": data})


TEMP_DEF = {"vendor": "SONOFF", "model": "SNZB-02", "description": "Temp & humidity", "exposes": [{"property": "temperature"}]}


def _main(hours=0.05, client=None, clock=None):
    c = client or _Client(); clk = clock or _Clock()
    out = io.StringIO()
    with patch.object(zb.mqtt, "Client", lambda *a, **k: c), patch.object(zb, "time", clk), patch.object(zb, "HOURS", hours), \
         patch.object(zb.nova_config, "post_both", MagicMock()) as post, redirect_stdout(out):
        zb.main()
    return c, clk, post, out.getvalue()


class TestSecurity(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertEqual((zb.MQTT_HOST, zb.MQTT_PORT), ("127.0.0.1", 1883))

    def test_rename_payload_is_json_encoded_so_a_hostile_name_cannot_inject_topics(self):
        c = _Client()
        with patch.object(zb, "TARGET", 'x", "to": "coordinator'), patch.object(zb.nova_config, "post_both", MagicMock()), redirect_stdout(io.StringIO()):
            zb.on_message(c, None, _event("0x1", defn=TEMP_DEF, friendly="0x1"))
        topic, payload = c.published[-1]
        self.assertEqual(topic, "zigbee2mqtt/bridge/request/device/rename")
        self.assertEqual(payload, {"from": "0x1", "to": 'x", "to": "coordinator'})

    def test_every_publish_stays_under_the_bridge_request_namespace(self):
        c, _, _, _ = _main()
        for topic, _ in c.published:
            self.assertTrue(topic.startswith("zigbee2mqtt/bridge/request/"), topic)

    def test_network_is_closed_on_exit(self):
        c, _, _, _ = _main()
        self.assertEqual(c.published[-1], ("zigbee2mqtt/bridge/request/permit_join", {"time": 0}))


class TestPerformance(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_on_message_10k_known_device_events_under_bound(self):
        zb.KNOWN.update(f"0x{i:016x}" for i in range(10_000))
        c = _Client()
        t0 = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            for i in range(10_000):
                zb.on_message(c, None, _event(f"0x{i:016x}"))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(c.published, [])

    def test_exposes_temp_scans_a_wide_definition_fast(self):
        defn = {"exposes": [{"type": "composite", "features": [{"property": f"p{i}"} for i in range(10_000)]}]}
        t0 = time.perf_counter()
        self.assertFalse(zb._exposes_temp(defn))
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_slack_failure_fails_open(self):
        # RETRY GAP: slack()/nova_config.post_both — one attempt; a failing post is logged and onboarding completes anyway
        post = MagicMock(side_effect=RuntimeError("slack down"))
        with patch.object(zb.nova_config, "post_both", post), redirect_stdout(io.StringIO()) as out:
            zb.slack("hi")
        self.assertEqual(post.call_count, 1)
        self.assertIn("slack post failed: slack down", out.getvalue())

    def test_mqtt_connect_failure_leaves_the_network_closed(self):
        # RETRY GAP: main()/mqtt connect — one attempt; the error escapes BEFORE permit_join is ever published,
        # so the safe default (network stays closed) holds and launchd can simply re-run the one-shot.
        c = _Client(); c.connect = MagicMock(side_effect=ConnectionRefusedError("1883"))
        with patch.object(zb.mqtt, "Client", lambda *a, **k: c), patch.object(zb, "time", _Clock()), patch.object(zb.nova_config, "post_both", MagicMock()), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(ConnectionRefusedError):
                zb.main()
        self.assertEqual(c.published, [])

    def test_malformed_event_is_ignored(self):
        c = _Client()
        with redirect_stdout(io.StringIO()):
            zb.on_message(c, None, _Msg("zigbee2mqtt/bridge/event", b"{not json"))
        self.assertEqual(c.published, [])
        self.assertFalse(zb.joined["done"])


class TestUnit(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_exposes_temp_variants(self):
        self.assertTrue(zb._exposes_temp(TEMP_DEF))
        self.assertTrue(zb._exposes_temp({"exposes": [{"type": "climate", "features": [{"property": "temperature"}]}]}))
        self.assertFalse(zb._exposes_temp({"exposes": [{"property": "humidity"}, "junk", {"features": ["x"]}]}))
        self.assertFalse(zb._exposes_temp(None))
        self.assertFalse(zb._exposes_temp({}))

    def test_baseline_seeds_known_without_the_coordinator(self):
        c = _Client()
        with redirect_stdout(io.StringIO()) as out:
            zb.on_message(c, None, _Msg("zigbee2mqtt/bridge/devices", [{"type": "Coordinator", "ieee_address": "0xc"}, {"type": "Router", "ieee_address": "0xr"}, {"type": "EndDevice", "ieee_address": "0xe"}]))
        self.assertEqual(zb.KNOWN, {"0xr", "0xe"})
        self.assertIn("baseline: 2 known devices", out.getvalue())

    def test_known_and_unfinished_interviews_do_not_rename(self):
        zb.KNOWN.add("0xold")
        c = _Client()
        with redirect_stdout(io.StringIO()) as out:
            zb.on_message(c, None, _event("0xold", defn=TEMP_DEF))
            zb.on_message(c, None, _event("0xnew", status="started"))
            zb.on_message(c, None, _Msg("zigbee2mqtt/bridge/event", {"type": "device_leave", "data": {"ieee_address": "0xnew"}}))
            zb.on_message(c, None, _Msg("zigbee2mqtt/bridge/state", {"state": "online"}))
        self.assertEqual(c.published, [])
        self.assertIn("interview started for 0xnew", out.getvalue())
        self.assertFalse(zb.joined["done"])

    def test_defaults_and_reissue_margin(self):
        self.assertEqual((zb.TARGET, zb.HOURS), ("garage_temp", 6.0))
        self.assertLess(zb.REISSUE_EVERY, 254)
        c = _Client(); zb.reissue(c)
        self.assertEqual(c.published, [("zigbee2mqtt/bridge/request/permit_join", {"time": 254})])


class TestIntegration(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_new_device_is_renamed_once_and_announced_on_the_feed(self):
        c = _Client()
        with patch.object(zb.nova_config, "post_both", MagicMock()) as post, redirect_stdout(io.StringIO()) as out:
            zb.on_message(c, None, _event("0xabc", defn=TEMP_DEF, friendly="0xabc"))
            zb.on_message(c, None, _event("0xdef", defn=TEMP_DEF, friendly="0xdef"))    # second device: ignored, job done
        self.assertEqual(c.published, [("zigbee2mqtt/bridge/request/device/rename", {"from": "0xabc", "to": "garage_temp"})])
        post.assert_called_once()
        msg, kw = post.call_args[0][0], post.call_args[1]
        self.assertEqual(kw, {"slack_channel": zb.nova_config.SLACK_FEED, "discord_channel": None})
        self.assertIn("*garage_temp*", msg); self.assertIn("SONOFF SNZB-02", msg); self.assertIn("Temperature: yes", msg)
        self.assertIn("renamed 0xabc -> garage_temp", out.getvalue())

    def test_on_connect_subscribes_and_opens_the_network(self):
        c = _Client()
        with redirect_stdout(io.StringIO()):
            zb.on_connect(c)
        self.assertEqual(c.subs, ["zigbee2mqtt/bridge/devices", "zigbee2mqtt/bridge/event"])
        self.assertEqual(c.published, [("zigbee2mqtt/bridge/request/permit_join", {"time": 254})])


class TestFunctional(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_golden_path_keeps_reissuing_until_the_deadline_then_closes(self):
        c, clk, post, out = _main(hours=600 / 3600)                          # 10-minute window
        self.assertEqual(c.connected, ("127.0.0.1", 1883))
        opens = [p for p in c.published if p == ("zigbee2mqtt/bridge/request/permit_join", {"time": 254})]
        self.assertEqual(len(opens), 3)                                       # on_connect + re-issues at 230s and 460s
        self.assertEqual(c.published[-1], ("zigbee2mqtt/bridge/request/permit_join", {"time": 0}))
        self.assertIn("deadline reached — network closed", out)
        self.assertEqual(out.count("permit_join re-issued"), 2)
        post.assert_not_called()                                              # nothing joined: nothing announced

    def test_error_path_loop_crash_still_closes_the_network(self):
        clk = _Clock(); ticks = {"n": 0}

        def sleep(s):
            ticks["n"] += 1
            if ticks["n"] == 3:                                                   # third loop tick: Ctrl-C
                raise KeyboardInterrupt
            clk.now += s
        clk.sleep = sleep
        c = _Client()
        with self.assertRaises(KeyboardInterrupt):
            _main(hours=1, client=c, clock=clk)
        self.assertEqual(c.published[-1], ("zigbee2mqtt/bridge/request/permit_join", {"time": 0}))


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_zigbee_onboard"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
