#!/usr/bin/env python3
"""Tests for nova_home_control.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
This module drives real speakers, receivers and lights, so EVERY outbound path is mocked in setUp for
every test: Bose SOAP (urlopen), Onkyo eISCP (socket.socket), HomeKit Shortcuts (subprocess.run),
PG (psycopg2.connect) and time.sleep. No device is ever contacted."""
import importlib.util
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_home_control.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_home_control_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load()
NS_RC = "urn:schemas-upnp-org:service:RenderingControl:1"
VOL_XML = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
           f'<u:GetVolumeResponse xmlns:u="{NS_RC}"><CurrentVolume>33</CurrentVolume></u:GetVolumeResponse>'
           f'</s:Body></s:Envelope>')
STATE_XML = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
             '<u:GetTransportInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
             '<CurrentTransportState>PLAYING</CurrentTransportState></u:GetTransportInfoResponse></s:Body></s:Envelope>')


def _soap(body):
    m = MagicMock()
    m.__enter__.return_value.read.return_value = body.encode()
    return m


def _eiscp_reply(cmd):
    return hc.onkyo._build_eiscp(cmd)[:-1] + b"\x1a"


class _Base(unittest.TestCase):
    def setUp(self):
        self.sock = MagicMock()
        self.sock.recv.side_effect = socket.timeout()
        self.ps = [patch.object(hc.socket, "socket", return_value=self.sock),
                   patch.object(hc.urllib.request, "urlopen", return_value=_soap(VOL_XML)),
                   patch("subprocess.run", return_value=SimpleNamespace(returncode=0)),
                   patch.object(psycopg2, "connect", side_effect=RuntimeError("no pg in tests")),
                   patch.object(hc.time, "sleep")]
        self.mock_sock_cls, self.urlopen, self.run, self.pg, self.sleep = [p.start() for p in self.ps]

    def tearDown(self):
        for p in self.ps:
            p.stop()

    def sent(self):
        return [c[0][0] for c in self.sock.sendall.call_args_list]


class TestSecurity(_Base):
    def test_no_credentials_and_parameterized_scene_log(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]", SRC, re.I))
        self.assertIn('"VALUES (now(), %s)"', SRC)

    def test_unknown_devices_and_inputs_rejected_before_any_io(self):
        with self.assertRaises(ValueError):
            hc.bose.set_volume("garage; rm", 10)
        with self.assertRaises(ValueError):
            hc.onkyo.set_input("living_room", "EVIL\rPWR00")
        with self.assertRaises(ValueError):
            hc.onkyo.zone2_power_on("office")          # no zone 2 on that model
        self.mock_sock_cls.assert_not_called()
        self.urlopen.assert_not_called()

    def test_volumes_clamped(self):
        self.assertEqual(hc.bose.set_volume("kitchen", 999)["level"], 100)
        self.assertEqual(hc.onkyo.set_volume("office", -5)["level"], 0)
        self.assertIn(b"!1MVL00\r", self.sent()[-1])


class TestPerformance(_Base):
    def test_10k_packets_build_and_parse_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            self.assertEqual(hc.onkyo._parse_eiscp(_eiscp_reply(f"MVL{i % 80:02X}")), f"MVL{i % 80:02X}")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def test_device_errors_surface_as_connection_errors_single_attempt(self):
        # RETRY GAP: bose._soap_request / onkyo._send_command — one attempt; error -> ConnectionError
        self.urlopen.side_effect = urllib.error.URLError("no route")
        with self.assertRaises(ConnectionError):
            hc.bose.mute("bedroom")
        self.assertEqual(self.urlopen.call_count, 1)
        self.sock.connect.side_effect = OSError("refused")
        with self.assertRaises(ConnectionError):
            hc.onkyo.power_on("office")
        self.sock.close.assert_called_once()

    def test_status_and_scenes_fail_open(self):
        self.urlopen.side_effect = urllib.error.URLError("down")
        self.sock.connect.side_effect = OSError("down")
        st = hc.get_all_status()
        self.assertTrue(all("error" in v for v in st["bose"].values()))
        self.assertTrue(all("error" in v["power"] for v in st["onkyo"].values()))
        out = hc.scenes.goodnight()                      # PG + devices down, lights still attempted
        self.assertIn("bose_error", out["results"])
        self.assertTrue(out["results"]["lights"])

    def test_weather_db_down(self):
        self.assertIn("Weather query failed", hc.weather.get_current()["error"])


class TestUnit(_Base):
    def test_eiscp_packet_layout(self):
        pkt = hc.onkyo._build_eiscp("PWR01")
        self.assertEqual(pkt[:4], b"ISCP")
        self.assertEqual(struct.unpack(">I", pkt[4:8])[0], 16)
        self.assertEqual(struct.unpack(">I", pkt[8:12])[0], len(b"!1PWR01\r"))
        self.assertEqual(pkt[16:], b"!1PWR01\r")
        self.assertEqual(hc.onkyo._parse_eiscp(b"short"), "")

    def test_query_and_reverse_lookup(self):
        self.sock.recv.side_effect = [_eiscp_reply("SLI" + hc.ONKYO_INPUTS["NET"])]
        self.assertEqual(hc.onkyo.get_input("office")["input_name"], "NET")
        self.sock.recv.side_effect = [_eiscp_reply("MVL28")]
        self.assertEqual(hc.onkyo.get_volume("office")["volume"], 40)
        self.sock.recv.side_effect = [_eiscp_reply("MVLzz")]
        self.assertEqual(hc.onkyo.get_volume("office")["volume"], -1)

    def test_bose_xml_parsing(self):
        self.assertEqual(hc.bose.get_volume("kitchen")["volume"], 33)
        self.urlopen.side_effect = [_soap(STATE_XML), _soap(VOL_XML)]
        st = hc.bose.status("kitchen")
        self.assertEqual((st["transport_state"], st["volume"]), ("PLAYING", 33))   # bug fix 2026-10-05

    def test_format_helpers(self):
        self.assertEqual(hc._format_result("x"), "x")
        txt = hc._format_status({"bose": {"b": {"error": "e"}}, "onkyo": {"o": {"name": "O", "model": "M", "power": "on",
                                                                             "volume": 5, "input": "NET"}}})
        self.assertIn("b: ERROR - e", txt)
        self.assertIn("vol=5, input=NET", txt)


class TestIntegration(_Base):
    def test_soap_envelope_and_headers(self):
        hc.bose.set_volume("bedroom", 30)
        req = self.urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "http://192.168.1.25:8091/RenderingControl")
        self.assertIn(b"<DesiredVolume>30</DesiredVolume>", req.data)
        self.assertEqual(req.get_header("Soapaction"), f'"{NS_RC}#SetVolume"')

    def test_all_fans_out_to_every_soundbar(self):
        out = hc.bose.stop("all")
        self.assertEqual(set(out), set(hc.BOSE_DEVICES))
        self.assertEqual(self.urlopen.call_count, len(hc.BOSE_DEVICES))

    def test_scene_logs_activation_and_runs_shortcut(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        self.pg.side_effect = None
        self.pg.return_value = conn
        out = hc.scenes.movie_mode()
        self.assertEqual(cur.execute.call_args[0][1], ("movie_mode",))
        self.assertEqual(self.run.call_args[0][0], ["shortcuts", "run", "Dim Living Room"])
        self.assertEqual(out["results"]["input"]["input"], "STRM BOX")
        self.assertEqual([p[18:-1] for p in self.sent()], [b"PWR01", b"SLI" + hc.ONKYO_INPUTS["STRM BOX"].encode(),
                                                          b"MVL28", b"LMD" + hc.ONKYO_MODES["surround"].encode()])


class TestFunctional(_Base):
    def _main(self, *argv):
        out = []
        with patch.object(sys, "argv", ["x", *argv]), patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            try:
                hc.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, "\n".join(out)

    def test_cli_onkyo_zone2_volume(self):
        code, out = self._main("onkyo", "living_room", "zone2", "volume", "25")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["level"], 25)
        self.assertIn(b"!1ZVL19\r", self.sent()[-1])

    def test_cli_errors_exit_1(self):
        self.assertEqual(self._main("bose", "nowhere", "mute")[0], 1)
        self.assertEqual(self._main("scene", "rave")[0], 1)
        self.assertEqual(self._main("toaster")[0], 1)
        self.assertEqual(self._main("weather")[0], 1)
        self.urlopen.assert_not_called()

    def test_cli_scene_goodnight(self):
        code, out = self._main("scene", "night")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["scene"], "goodnight")
        self.assertIn(b"!1ZPW00\r", self.sent()[-1])


class TestFrame(unittest.TestCase):
    def test_usage_exits_zero_without_touching_devices(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Nova Home Control", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_home_control"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
