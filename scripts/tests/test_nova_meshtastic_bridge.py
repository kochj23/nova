#!/usr/bin/env python3
"""Tests for nova_meshtastic_bridge.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_meshtastic_bridge.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="mesh-bridge-test-"))


def _stub_modules():
    mesh = types.ModuleType("meshtastic"); serial = types.ModuleType("meshtastic.serial_interface")
    serial.SerialInterface = MagicMock(side_effect=OSError("offline: serial stubbed"))
    mesh.serial_interface = serial
    pubsub = types.ModuleType("pubsub"); pubsub.pub = types.SimpleNamespace(subscribe=MagicMock())
    return {"meshtastic": mesh, "meshtastic.serial_interface": serial, "pubsub": pubsub}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()):
        spec.loader.exec_module(mod)
    return mod


mb = _load("mesh_bridge_under_test", SCRIPT)
# Module-level stubs: no serial port, no PG, no HTTP server, log in a tempdir.
mb.LOG_FILE = TMP / "meshtastic_bridge.log"
mb.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")))
mb.HTTPServer = MagicMock(side_effect=AssertionError("HTTPServer must be patched per test"))


def _iface(info=None, nodes=None, send_exc=None):
    i = MagicMock()
    i.getMyNodeInfo.return_value = info if info is not None else {"user": {"longName": "Nova T114"},
                                                                  "deviceMetrics": {"batteryLevel": 87, "voltage": 4.1, "uptimeSeconds": 99}}
    i.nodes = nodes or {}
    i.sendText = MagicMock(side_effect=send_exc)
    return i


class _Cur:
    def __init__(self): self.sql, self.params, self.closed = [], [], False
    def execute(self, sql, params=None): self.sql.append(" ".join(sql.split())); self.params.append(params)
    def close(self): self.closed = True


def _pg(cur):
    conn = types.SimpleNamespace(cursor=lambda: cur, close=MagicMock(), autocommit=False)
    return patch.object(mb.psycopg2, "connect", MagicMock(return_value=conn)), conn


def _handler(method, path, body=None):
    """Drive Handler.do_GET/do_POST without a socket; returns (status, parsed json body)."""
    h = mb.Handler.__new__(mb.Handler)
    h.path, h.request_version, h.client_address, h.command = path, "HTTP/1.1", ("127.0.0.1", 0), method
    h.requestline, h.close_connection = f"{method} {path} HTTP/1.1", True
    raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
    getattr(h, "do_" + method)()
    out = h.wfile.getvalue()
    status = int(out.split(b" ", 2)[1])
    return status, json.loads(out.split(b"\r\n\r\n", 1)[1])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", mb.DSN)

    def test_observation_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        cur = _Cur(); p, conn = _pg(cur)
        evil = "x'); DROP TABLE shared_observations; --"
        with p:
            mb.record_observation(evil, evil, metadata={"k": evil})
        self.assertNotIn("DROP", cur.sql[0])
        self.assertEqual(cur.params[0][:2], (evil, evil))
        self.assertEqual(json.loads(cur.params[0][3]), {"k": evil})
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"shared_observations"})

    def test_send_truncates_to_200_chars_and_rejects_empty_or_bad_json(self):
        with patch.object(mb, "_iface", _iface()) as i:
            status, body = _handler("POST", "/send", {"text": "  " + "x" * 500})
            self.assertEqual((status, body), (200, {"sent": True}))
            self.assertEqual(len(i.sendText.call_args[0][0]), 200)
            self.assertEqual(_handler("POST", "/send", {"text": "   "}), (400, {"error": "text required"}))
            self.assertEqual(_handler("POST", "/send", b"{not json"), (400, {"error": "bad json"}))
            self.assertEqual(_handler("POST", "/other", {"text": "x"})[0], 404)

    def test_nodes_endpoint_is_read_only_projection(self):
        nodes = {"!abc": {"user": {"longName": "L", "shortName": "S", "hwModel": "T114", "secret": "nope"},
                          "position": {"latitude": 1.0, "longitude": 2.0}, "deviceMetrics": {"batteryLevel": 50}, "snr": 7.5, "hopsAway": 1, "lastHeard": 5}}
        with patch.object(mb, "_iface", _iface(nodes=nodes)):
            status, body = _handler("GET", "/nodes")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(set(body["nodes"][0]), {"id", "longName", "shortName", "hwModel", "snr", "hopsAway", "lastHeard", "batteryLevel", "latitude", "longitude"})


class TestPerformance(unittest.TestCase):
    def test_on_receive_fast_on_10k_non_text_packets(self):
        pk = {"decoded": {"portnum": "TELEMETRY_APP"}, "fromId": "!abc"}
        with patch.object(mb, "record_observation", MagicMock()) as ro:
            t0 = time.perf_counter()
            for _ in range(10_000):
                mb.on_receive(pk, None)
            self.assertLess(time.perf_counter() - t0, 1.0)
        ro.assert_not_called()                                   # telemetry/position never hit PG

    def test_nodes_projection_fast_on_10k_nodes(self):
        nodes = {f"!{i:08x}": {"user": {"longName": f"n{i}"}, "position": {}, "deviceMetrics": {}} for i in range(10_000)}
        with patch.object(mb, "_iface", _iface(nodes=nodes)):
            t0 = time.perf_counter()
            status, body = _handler("GET", "/nodes")
            self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(body["count"], 10_000)


class TestRetry(unittest.TestCase):
    def test_record_observation_fails_open(self):
        # RETRY GAP: record_observation/psycopg2.connect — one attempt; failure is logged, never raised
        with patch.object(mb.psycopg2, "connect", MagicMock(side_effect=OSError("pg down"))), redirect_stdout(io.StringIO()) as out:
            mb.record_observation("s", "o")
        self.assertIn("failed to record observation: pg down", out.getvalue())

    def test_send_failures_count_toward_the_watchdog_and_a_clean_send_resets(self):
        # RETRY GAP: do_POST /send — one sendText attempt per request; failures feed the watchdog counter instead
        with patch.object(mb, "_iface", _iface(send_exc=RuntimeError("timeout"))), patch.object(mb, "_send_timeouts", 0), redirect_stdout(io.StringIO()):
            for n in (1, 2):
                status, body = _handler("POST", "/send", {"text": "hi"})
                self.assertEqual((status, body["error"], mb._send_timeouts), (500, "timeout", n))
            mb._iface.sendText = MagicMock()
            self.assertEqual(_handler("POST", "/send", {"text": "hi"})[0], 200)
            self.assertEqual(mb._send_timeouts, 0)

    def test_watchdog_reconnects_on_stale_rx_and_exits_when_reconnect_fails(self):
        class _Stop(Exception):
            pass
        ro = MagicMock()
        with patch.object(mb.time, "sleep", MagicMock(side_effect=[None, _Stop()])), patch.object(mb, "record_observation", ro), \
             patch.object(mb, "_last_rx", time.time() - mb.RX_TIMEOUT_SEC - 5), patch.object(mb, "reconnect", MagicMock()) as rc, \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(_Stop):
                mb.watchdog()
        rc.assert_called_once()
        self.assertEqual(ro.call_args_list[-1][0], ("watchdog", "reconnect succeeded", "info"))
        with patch.object(mb.time, "sleep", MagicMock(side_effect=[None, _Stop()])), patch.object(mb, "record_observation", ro), \
             patch.object(mb, "_send_timeouts", mb.MAX_SEND_TIMEOUTS), patch.object(mb, "reconnect", MagicMock(side_effect=OSError("no port"))), \
             patch.object(mb.os, "_exit", MagicMock()) as ex, redirect_stdout(io.StringIO()):
            with self.assertRaises(_Stop):
                mb.watchdog()
        ex.assert_called_once_with(1)
        self.assertEqual(ro.call_args_list[-1][1], {"severity": "critical"})


class TestUnit(unittest.TestCase):
    def test_log_writes_file_and_survives_unwritable_path(self):
        with redirect_stdout(io.StringIO()) as out:
            mb.log("hello")
        self.assertIn("] hello", out.getvalue())
        self.assertTrue(mb.LOG_FILE.read_text().rstrip().endswith("] hello"))
        with patch.object(mb, "LOG_FILE", TMP / "missing-dir" / "x.log"), redirect_stdout(io.StringIO()):
            mb.log("still fine")

    def test_current_device_prefers_configured_then_glob(self):
        with patch.object(mb.os.path, "exists", lambda p: p == mb.DEVICE):
            self.assertEqual(mb.current_device(), mb.DEVICE)
        with patch.object(mb.os.path, "exists", lambda p: False), patch.object(mb.glob, "glob", lambda pat: ["/dev/cu.usbmodem9", "/dev/cu.usbmodem1"]):
            self.assertEqual(mb.current_device(), "/dev/cu.usbmodem1")
        with patch.object(mb.os.path, "exists", lambda p: False), patch.object(mb.glob, "glob", lambda pat: []):
            self.assertEqual(mb.current_device(), mb.DEVICE)

    def test_on_receive_stamps_rx_and_records_only_text(self):
        with patch.object(mb, "record_observation", MagicMock()) as ro, patch.object(mb, "_last_rx", 0.0), redirect_stdout(io.StringIO()):
            mb.on_receive({"decoded": {"portnum": "POSITION_APP"}, "from": 7}, None)
            self.assertGreater(mb._last_rx, 0.0)
            mb.on_receive({"decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "ping"}, "fromId": "!abc"}, None)
            mb.on_receive({}, None)
        ro.assert_called_once_with("message from !abc", "ping")

    def test_status_endpoint_states(self):
        with patch.object(mb, "_iface", None):
            status, body = _handler("GET", "/status")
            self.assertEqual((status, body["connected"]), (503, False))
            self.assertIn("seconds_since_last_rx", body)
            self.assertEqual(_handler("POST", "/send", {"text": "x"})[0], 503)
            self.assertEqual(_handler("GET", "/nodes")[0], 503)
        with patch.object(mb, "_iface", _iface()), patch.object(mb, "_last_rx", time.time() - 12):
            status, body = _handler("GET", "/status")
        self.assertEqual(status, 200)
        self.assertEqual((body["longName"], body["batteryLevel"], body["rx_timeout_seconds"]), ("Nova T114", 87, mb.RX_TIMEOUT_SEC))
        self.assertGreaterEqual(body["seconds_since_last_rx"], 11.5)
        bad = _iface(); bad.getMyNodeInfo.side_effect = RuntimeError("serial gone")
        with patch.object(mb, "_iface", bad):
            self.assertEqual(_handler("GET", "/status")[0], 500)
        self.assertEqual(_handler("GET", "/nope")[0], 404)


class TestIntegration(unittest.TestCase):
    def test_observations_land_in_shared_observations_as_mesh_category(self):
        cur = _Cur(); p, conn = _pg(cur)
        with p:
            mb.record_observation("message from !abc", "ping", metadata={"a": 1})
        self.assertIn("INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata) VALUES ('nova_meshtastic_bridge', 'mesh', %s, %s, %s, %s)", cur.sql[0])
        self.assertEqual(cur.params[0], ("message from !abc", "ping", "info", '{"a": 1}'))
        self.assertTrue(cur.closed); conn.close.assert_called_once()

    def test_connect_and_reconnect_use_the_resolved_device_and_subscribe_once(self):
        si = MagicMock(return_value=_iface())
        with patch.object(mb.meshtastic.serial_interface, "SerialInterface", si), patch.object(mb, "current_device", lambda: "/dev/cu.usbmodemX"), \
             patch.object(mb.pub, "subscribe", MagicMock()) as sub, redirect_stdout(io.StringIO()):
            mb.connect()
            sub.assert_called_once_with(mb.on_receive, "meshtastic.receive")
            old = mb._iface
            with patch.object(mb, "_send_timeouts", 5), patch.object(mb, "_last_rx", 0.0):
                mb.reconnect()
                self.assertEqual(mb._send_timeouts, 0); self.assertGreater(mb._last_rx, 0.0)
            old.close.assert_called_once()
        self.assertEqual(si.call_args_list, [((), {"devPath": "/dev/cu.usbmodemX"})] * 2)
        mb._iface = None

    def test_text_message_round_trip_from_radio_to_observation(self):
        cur = _Cur(); p, conn = _pg(cur)
        with p, redirect_stdout(io.StringIO()):
            mb.on_receive({"decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello mesh"}, "fromId": "!node1"}, None)
        self.assertEqual(cur.params[0][:2], ("message from !node1", "hello mesh"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_connects_starts_watchdog_and_serves(self):
        server = MagicMock()
        thread = MagicMock()
        with patch.object(mb.meshtastic.serial_interface, "SerialInterface", MagicMock(return_value=_iface())), \
             patch.object(mb, "current_device", lambda: "/dev/cu.usbmodemY"), patch.object(mb.pub, "subscribe", MagicMock()), \
             patch.object(mb, "HTTPServer", MagicMock(return_value=server)) as hs, patch.object(mb.threading, "Thread", MagicMock(return_value=thread)) as th, \
             redirect_stdout(io.StringIO()) as out:
            mb.main()
        hs.assert_called_once_with(("0.0.0.0", mb.PORT), mb.Handler)
        server.serve_forever.assert_called_once()
        self.assertEqual(th.call_args[1]["target"], mb.watchdog); self.assertTrue(th.call_args[1]["daemon"]); thread.start.assert_called_once()
        self.assertIn("Connected to /dev/cu.usbmodemY", out.getvalue()); self.assertIn(f"Listening on :{mb.PORT}", out.getvalue())
        mb._iface = None

    def test_missing_radio_fails_before_serving(self):
        with patch.object(mb.meshtastic.serial_interface, "SerialInterface", MagicMock(side_effect=OSError("no such port"))), \
             patch.object(mb, "current_device", lambda: "/dev/none"), patch.object(mb, "HTTPServer", MagicMock()) as hs:
            with self.assertRaises(OSError):
                mb.main()
        hs.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_opens_the_serial_port_or_serves(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        code = ("import sys, types\n"
                "m = types.ModuleType('meshtastic'); s = types.ModuleType('meshtastic.serial_interface')\n"
                "s.SerialInterface = lambda **k: (_ for _ in ()).throw(RuntimeError('serial opened at import'))\n"
                "m.serial_interface = s; sys.modules['meshtastic'] = m; sys.modules['meshtastic.serial_interface'] = s\n"
                "import nova_meshtastic_bridge as b\n"
                "print('IMPORT-OK', b.PORT, b._iface)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"IMPORT-OK {mb.PORT} None")


if __name__ == "__main__":
    unittest.main()
