#!/usr/bin/env python3
"""Tests for nova_lutron.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_lutron.py"
SRC = SCRIPT.read_text()
TMP_HOME = Path(tempfile.mkdtemp(prefix="nova_lutron_test_"))   # LOG_FILE / STATE_FILE / CERT_DIR land here


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    root = logging.getLogger()
    before = list(root.handlers)
    with patch("pathlib.Path.home", return_value=TMP_HOME):        # redirect every ~ path at import
        spec.loader.exec_module(mod)
    for h in root.handlers:                                        # don't leak the daemon's handlers into the session
        if h not in before:
            root.removeHandler(h); h.close()
    return mod


nl = _load("nl", SCRIPT)


def _reset(connected=True, states=None):
    nl._bridge_connected = connected
    nl._device_states.clear()
    nl._device_states.update(states or {})
    nl._bridge = None


def _bridge(devices=None):
    b = MagicMock()
    b.turn_on = AsyncMock(); b.turn_off = AsyncMock(); b.set_value = AsyncMock()
    b.get_devices = MagicMock(return_value=devices if devices is not None else {})
    return b


def _handler(method, path, body=None):
    """Build a LutronHTTPHandler without a socket and run the verb; returns (status, json)."""
    h = nl.LutronHTTPHandler.__new__(nl.LutronHTTPHandler)
    h.request_version = "HTTP/1.1"; h.command = method; h.path = path; h.client_address = ("127.0.0.1", 1)
    h.requestline = f"{method} {path} HTTP/1.1"
    raw = json.dumps(body).encode() if body is not None else b""
    h.headers = Message(); h.headers["Content-Length"] = str(len(raw))
    h.rfile = io.BytesIO(raw); h.wfile = io.BytesIO()
    getattr(h, f"do_{method}")()
    out = h.wfile.getvalue().decode()
    status = int(out.split(" ", 2)[1])
    payload = json.loads(out.split("\r\n\r\n", 1)[1]) if "\r\n\r\n" in out and out.split("\r\n\r\n", 1)[1] else None
    return status, payload


class _LoopThread:
    """A real event loop in a background thread, so run_coroutine_threadsafe works like in the daemon."""
    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.t = threading.Thread(target=self.loop.run_forever, daemon=True); self.t.start()
        self._old = nl._loop; nl._loop = self.loop
        return self.loop

    def __exit__(self, *a):
        nl._loop = self._old
        self.loop.call_soon_threadsafe(self.loop.stop); self.t.join(timeout=5); self.loop.close()
        return False


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_tls_material_from_cert_dir(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        for f in (nl.KEY_FILE, nl.CERT_FILE, nl.CA_FILE):
            self.assertTrue(f.startswith(str(nl.CERT_DIR)))
        self.assertNotIn("BEGIN", SRC)                              # no inline PEM

    def test_api_binds_loopback_only(self):
        self.assertEqual(nl.HTTP_HOST, "127.0.0.1")
        self.assertIn("HTTPServer((HTTP_HOST, HTTP_PORT)", SRC)

    def test_unknown_zone_and_bad_json_are_rejected(self):
        _reset()
        status, body = _handler("POST", "/devices/99/set", {"on": True})
        self.assertEqual((status, body), (404, {"error": "Unknown zone: 99"}))
        status, body = _handler("POST", "/devices/abc/set", {"on": True})
        self.assertEqual(status, 404)
        h = nl.LutronHTTPHandler.__new__(nl.LutronHTTPHandler)
        h.headers = Message(); h.headers["Content-Length"] = "5"; h.rfile = io.BytesIO(b"{bad}")
        self.assertEqual(h._read_body(), {})

    def test_levels_are_clamped_to_0_100(self):
        self.assertEqual(nl.parse_command("kitchen 150%")[0]["level"], 100)
        self.assertEqual(nl.parse_command("kitchen dim 0")[0]["level"], 0)
        _reset(); nl._bridge = _bridge()
        with _LoopThread():
            status, body = _handler("POST", "/devices/2/set", {"level": 999})
        self.assertEqual((status, body["response"]), (200, "Set Kitchen Main Lights to 100%"))
        nl._bridge.set_value.assert_awaited_once_with("2", 100)


class TestPerformance(unittest.TestCase):
    def test_parser_fast_on_10k_commands(self):
        cmds = ["kitchen 50%", "living room off", "all on", "status", "porch", "patio 20 percent", "garage on"]
        t0 = time.perf_counter()
        n = sum(len(nl.parse_command(cmds[i % len(cmds)])) for i in range(10_000))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertGreater(n, 10_000)


class TestRetry(unittest.TestCase):
    def test_bridge_loop_reconnects_after_two_failures(self):
        _reset(connected=False)
        good = _bridge({"1": {"zone": "1", "current_state": 40}})
        good.connect = AsyncMock(); good.add_subscriber = MagicMock()
        sb = types.ModuleType("pylutron_caseta.smartbridge")
        sb.Smartbridge = MagicMock()
        sb.Smartbridge.create_tls = MagicMock(side_effect=[OSError("refused"), OSError("tls"), good])
        pkg = types.ModuleType("pylutron_caseta"); pkg.smartbridge = sb
        sleeps = []

        class _Stop(BaseException):
            pass

        async def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) == 3:
                raise _Stop()
        with patch.dict(sys.modules, {"pylutron_caseta": pkg, "pylutron_caseta.smartbridge": sb}), \
             patch.object(nl.asyncio, "sleep", fake_sleep):
            with self.assertRaises(_Stop):
                asyncio.run(nl.bridge_loop())
        self.assertEqual(sb.Smartbridge.create_tls.call_count, 3)
        self.assertEqual(sleeps, [15, 15, 30])                      # two reconnect backoffs, then the keep-alive tick
        self.assertTrue(nl._bridge_connected)
        self.assertEqual(nl._device_states[1], {"level": 40})
        good.add_subscriber.assert_called_once()
        self.assertEqual(sb.Smartbridge.create_tls.call_args[1]["keyfile"], nl.KEY_FILE)
        self.assertTrue(json.loads(nl.STATE_FILE.read_text())["bridge_connected"])

    def test_http_set_is_one_shot_and_fails_closed_with_500(self):
        # RETRY GAP: LutronHTTPHandler.do_POST — a failing bridge call is tried once and surfaced as a 500 JSON error
        _reset(); nl._bridge = _bridge(); nl._bridge.turn_on = AsyncMock(side_effect=RuntimeError("bridge gone"))
        with _LoopThread():
            status, body = _handler("POST", "/devices/3/set", {"on": True})
        self.assertEqual((status, body), (500, {"error": "bridge gone"}))
        self.assertEqual(nl._bridge.turn_on.await_count, 1)

    def test_callback_errors_never_escape(self):
        # RETRY GAP: on_device_changed()/update_all_states() — bridge errors are logged, state left as-is
        _reset(states={2: {"level": 10}}); nl._bridge = _bridge(); nl._bridge.get_devices.side_effect = RuntimeError("boom")
        asyncio.run(nl.on_device_changed("2")); asyncio.run(nl.update_all_states())
        self.assertEqual(nl._device_states, {2: {"level": 10}})


class TestUnit(unittest.TestCase):
    def test_parse_command_global_and_status(self):
        self.assertEqual(len(nl.parse_command("ALL ON")), len(nl.DEVICE_MAP))
        self.assertTrue(all(a["action"] == "off" and a["level"] == 0 for a in nl.parse_command("lights off")))
        self.assertEqual(nl.parse_command("what's on"), [{"action": "status"}])
        self.assertEqual(nl.parse_command(""), [])
        self.assertEqual(nl.parse_command("garage lights on"), [])

    def test_parse_command_rooms_and_levels(self):
        self.assertEqual(nl.parse_command("kitchen 50%"), [{"zone": 2, "action": "set", "level": 50}])
        self.assertEqual(nl.parse_command("dim 75 livingroom"),
                         [{"zone": 1, "action": "set", "level": 75}, {"zone": 4, "action": "set", "level": 75}])
        self.assertEqual(nl.parse_command("front porch off"), [{"zone": 3, "action": "off", "level": 0}])
        self.assertEqual(nl.parse_command("patio"), [{"zone": 5, "action": "on", "level": 100}])
        self.assertEqual(nl.parse_command("outside patio 20 percent")[0]["level"], 20)

    def test_status_text(self):
        _reset(connected=False)
        self.assertEqual(nl.get_status_text(), "Bridge disconnected")
        _reset()
        self.assertEqual(nl.get_status_text(), "All lights are off.")
        _reset(states={2: {"level": 35}, 3: {"level": 100}})
        self.assertEqual(nl.get_status_text(), "2 lights on:\n  Kitchen Main Lights: 35%\n  Front Porch: ON")
        _reset(states={5: {"level": 100}})
        self.assertTrue(nl.get_status_text().startswith("1 light on:"))

    def test_write_state_shape(self):
        _reset(states={1: {"level": 60}})
        nl.write_state()
        d = json.loads(nl.STATE_FILE.read_text())
        self.assertEqual((d["devices_on"], d["total_devices"], d["bridge_connected"]), (1, 5, True))
        self.assertEqual([x["zone"] for x in d["devices"]], [1, 2, 3, 4, 5])
        self.assertEqual(d["devices"][0]["level"], 60)
        self.assertTrue(str(nl.STATE_FILE).startswith(str(TMP_HOME)))
        self.assertTrue(str(nl.LOG_FILE).startswith(str(TMP_HOME)))


class TestIntegration(unittest.TestCase):
    def test_execute_command_drives_the_bridge(self):
        _reset(); nl._bridge = _bridge()
        out = asyncio.run(nl.execute_command("living room 40%"))
        self.assertEqual(out, "Living Room Main Lights 1: set to 40%\nLiving Room Main Lights 2: set to 40%")
        self.assertEqual([c.args for c in nl._bridge.set_value.await_args_list], [("1", 40), ("4", 40)])
        self.assertEqual(asyncio.run(nl.execute_command("porch off")), "Front Porch: turned off")
        nl._bridge.turn_off.assert_awaited_once_with("3")
        self.assertEqual(asyncio.run(nl.execute_command("xyz")), "Could not understand command: 'xyz'")
        _reset(connected=False)
        self.assertEqual(asyncio.run(nl.execute_command("all on")), "Error: Bridge not connected")

    def test_device_change_updates_state_file(self):
        _reset(); nl._bridge = _bridge({"7": {"zone": "2", "current_state": 80}})
        asyncio.run(nl.on_device_changed("7"))
        self.assertEqual(nl._device_states[2], {"level": 80})
        self.assertEqual(json.loads(nl.STATE_FILE.read_text())["devices_on"], 1)

    def test_update_all_states_skips_unknown_and_unparseable_zones(self):
        _reset(); nl._bridge = _bridge({"a": {"zone": "x", "current_state": 1}, "b": {"zone": "42", "current_state": 1},
                                        "c": {"zone": "5", "current_state": 100}, "d": {"current_state": 100}})
        asyncio.run(nl.update_all_states())
        self.assertEqual(nl._device_states, {5: {"level": 100}})


class TestFunctional(unittest.TestCase):
    def test_http_golden_path(self):
        _reset(states={2: {"level": 50}}); nl._bridge = _bridge()
        status, h = _handler("GET", "/health")
        self.assertEqual((status, h["status"], h["devices_tracked"]), (200, "ok", 5))
        status, d = _handler("GET", "/devices")
        self.assertEqual([x["on"] for x in d["devices"]], [False, True, False, False, False])
        status, s = _handler("GET", "/status")
        self.assertEqual((s["devices_on"], s["devices_off"], s["active_rooms"]), (1, 4, ["kitchen"]))
        with _LoopThread():
            status, c = _handler("POST", "/command", {"command": "kitchen off"})
            self.assertEqual((status, c["response"]), (200, "Kitchen Main Lights: turned off"))
            status, c = _handler("POST", "/devices/4/set", {"on": False})
            self.assertEqual(c["response"], "Turned off Living Room Main Lights 2")
            status, c = _handler("POST", "/devices/4/set", {})
            self.assertEqual(c["response"], "No action specified (need 'level' or 'on')")
        self.assertEqual([c.args for c in nl._bridge.turn_off.await_args_list], [("2",), ("4",)])

    def test_http_error_paths(self):
        _reset(); nl._bridge = _bridge()
        self.assertEqual(_handler("GET", "/nope")[0], 404)
        self.assertEqual(_handler("POST", "/nope")[0], 404)
        self.assertEqual(_handler("POST", "/command", {"command": ""}), (400, {"error": "No command specified"}))
        old = nl._loop; nl._loop = None
        try:
            self.assertEqual(_handler("POST", "/command", {"command": "all on"})[0], 503)
            self.assertEqual(_handler("POST", "/devices/1/set", {"on": True})[0], 503)
        finally:
            nl._loop = old
        _reset(connected=False)
        self.assertEqual(_handler("GET", "/health")[1]["status"], "disconnected")


class TestFrame(unittest.TestCase):
    def test_import_never_starts_the_daemon(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_lutron"], cwd=str(SCRIPTS), capture_output=True,
                               text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "")
            self.assertTrue((Path(home) / ".openclaw" / "logs").is_dir())       # the daemon's log dir, in the sandbox home


if __name__ == "__main__":
    unittest.main()
