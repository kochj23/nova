#!/usr/bin/env python3
"""Tests for nova_gsm_analyze.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import socket
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
SCRIPT = SCRIPTS / "nova_gsm_analyze.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gsm = _load("gsm", SCRIPT)
HDR = bytes(16)                                                     # GSMTAP header (stripped by listen)
SI3 = bytes([0x49, 0x06, 0x1B, 0x12, 0x34, 0x13, 0x00, 0x62, 0x27, 0x19])     # MCC 310 MNC 260 LAC 0x2719 CI 0x1234
SI2 = bytes([0x49, 0x06, 0x1A])
CMC0 = bytes([0x06, 0x06, 0x35, 0x00])                              # A5/0
CMC1 = bytes([0x06, 0x06, 0x35, 0x01])                              # A5/1


class _Sock:
    """UDP socket stand-in: hands out the queued datagrams then times out."""
    def __init__(self, frames):
        self.frames = list(frames); self.bound = None; self.sent = []

    def setsockopt(self, *a): pass

    def bind(self, addr): self.bound = addr

    def settimeout(self, s): self.timeout = s

    def recvfrom(self, n):
        if not self.frames:
            raise socket.timeout()
        return self.frames.pop(0), ("127.0.0.1", 4729)

    def sendto(self, *a): self.sent.append(a)


def _cfg(exc=None):
    m = types.ModuleType("nova_config"); m.SLACK_BB = "C_BB"
    m.post_both = MagicMock(side_effect=exc) if exc else MagicMock(); m.notify_local = MagicMock()
    return m


def _listen(frames, cfg=None):
    s = _Sock([HDR + f for f in frames]); cfg = cfg or _cfg()
    out = io.StringIO()
    with patch.object(gsm.socket, "socket", MagicMock(return_value=s)), patch.dict(sys.modules, {"nova_config": cfg}), redirect_stdout(out):
        v = gsm.listen(1)
    return v, s, cfg, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_listener_is_receive_only(self):
        v, s, _, _ = _listen([SI3])
        self.assertEqual(s.bound, ("0.0.0.0", 4729))
        self.assertEqual(s.sent, [])
        self.assertNotIn("sendto", SRC)

    def test_alert_carries_only_cell_identity(self):
        cfg = _cfg()
        with patch.dict(sys.modules, {"nova_config": cfg}):
            gsm._alert({"cell": {"mcc": "001", "mnc": "01", "lac": 1, "ci": 2}, "tells": ["t1"], "verdict": "SUSPECT"})
        msg = cfg.post_both.call_args[0][0]
        self.assertIn("MCC 001 MNC 01 LAC 1 CID 2", msg); self.assertIn("• t1", msg)
        self.assertEqual(cfg.post_both.call_args[1], {"slack_channel": "C_BB"})
        cfg.notify_local.assert_called_once_with("IMSI-catcher confirmed", "MCC 001 MNC 01 LAC 1 CID 2", critical=True)


class TestPerformance(unittest.TestCase):
    def test_parse_10k_frames_under_bound(self):
        frames = [SI3, SI2, CMC0, CMC1, b"\x00"] * 2000
        t0 = time.perf_counter()
        findings = [gsm.parse_l3(f) for f in frames]
        v = gsm.analyze_session(findings)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(v["verdict"], "SUSPECT")


class TestRetry(unittest.TestCase):
    def test_alert_fails_open_when_slack_is_down(self):
        # RETRY GAP: _alert()/post_both — one attempt; a failing post is swallowed and the verdict is still printed
        v, s, cfg, out = _listen([SI3, CMC0], cfg=_cfg(exc=RuntimeError("slack down")))
        self.assertEqual(v["verdict"], "SUSPECT")
        self.assertEqual(cfg.post_both.call_count, 1)
        cfg.notify_local.assert_called_once()
        self.assertIn('"verdict": "SUSPECT"', out)

    def test_alert_without_nova_config_is_silent(self):
        with patch.dict(sys.modules, {"nova_config": None}):          # import fails -> return
            self.assertIsNone(gsm._alert({"cell": {}, "tells": ["x"], "verdict": "SUSPECT"}))

    def test_listener_timeout_ends_cleanly_with_a_clean_verdict(self):
        v, _, cfg, _ = _listen([])
        self.assertEqual(v, {"cell": {}, "tells": [], "verdict": "clean"})
        cfg.post_both.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_selftest_runs_clean(self):
        with redirect_stdout(io.StringIO()) as out:
            gsm.selftest()
        self.assertIn("SELFTEST OK", out.getvalue())

    def test_bcd_lai(self):
        self.assertEqual(gsm._bcd_lai(0x13, 0x00, 0x62), ("310", "260"))
        self.assertEqual(gsm._bcd_lai(0x13, 0xF0, 0x62), ("310", "26"))          # 2-digit MNC (filler nibble)

    def test_parse_edges(self):
        self.assertEqual(gsm.parse_l3(b""), {})
        self.assertEqual(gsm.parse_l3(b"\x00\x00"), {})
        self.assertEqual(gsm.parse_l3(bytes([0x49, 0x05, 0x1B]) + bytes(7)), {})            # MM protocol, not RR
        self.assertEqual(gsm.parse_l3(bytes([0x49, 0x06, 0x1B, 0x00])), {})                 # truncated SI3
        self.assertEqual(gsm.parse_l3(bytes([0x49, 0x06, 0x02]))["msg"], "SI2")
        r = gsm.parse_l3(bytes([0x06, 0x06, 0x35, 0x05]))                                    # SC=1 algo=2 -> A5/3
        self.assertEqual(r["cipher"], "A5/3"); self.assertNotIn("flag_weak_cipher", r); self.assertNotIn("flag_a5_0", r)
        self.assertEqual(gsm.parse_l3(bytes([0x06, 0x06, 0x35, 0x03]))["flag_weak_cipher"], "A5/2 (weak/export)")

    def test_analyze_session_edges(self):
        self.assertEqual(gsm.analyze_session([]), {"cell": {}, "tells": [], "verdict": "clean"})
        v = gsm.analyze_session([gsm.parse_l3(CMC0)])                                        # no SI3 at all
        self.assertEqual(v["cell"], {}); self.assertEqual(len(v["tells"]), 1)
        v = gsm.analyze_session([gsm.parse_l3(SI3)])                                         # SI3 but no neighbours
        self.assertIn("no neighbour-cell list", v["tells"][0]); self.assertEqual(v["verdict"], "SUSPECT")


class TestIntegration(unittest.TestCase):
    def test_listen_strips_the_gsmtap_header_and_drops_short_datagrams(self):
        v, _, _, _ = _listen([SI3, SI2, b"\x01"])                                            # 17-byte datagram ignored
        self.assertEqual(v["cell"], {"mcc": "310", "mnc": "260", "lac": 0x2719, "ci": 0x1234})
        self.assertEqual(v["verdict"], "clean")

    def test_us_mcc_set_matches_the_docstring(self):
        self.assertEqual(gsm.US_MCC, {"310", "311", "312", "313", "314", "315", "316"})
        self.assertEqual(gsm.A5[0], "A5/0 (NO ENCRYPTION)")


class TestFunctional(unittest.TestCase):
    def test_golden_path_dirtbox_session_alerts_critical(self):
        v, _, cfg, out = _listen([SI3, CMC0])
        self.assertEqual(v["verdict"], "SUSPECT")
        self.assertEqual(len(v["tells"]), 2)                                                 # null cipher + no neighbours
        msg = cfg.post_both.call_args[0][0]
        self.assertIn("IMSI-catcher CONFIRMED", msg); self.assertIn("MCC 310 MNC 260 LAC 10009 CID 4660", msg)
        self.assertEqual(json.loads(out)["verdict"], "SUSPECT")

    def test_clean_carrier_session_never_alerts(self):
        v, _, cfg, _ = _listen([SI3, SI2, bytes([0x06, 0x06, 0x35, 0x05])])
        self.assertEqual(v, {"cell": {"mcc": "310", "mnc": "260", "lac": 0x2719, "ci": 0x1234}, "tells": [], "verdict": "clean"})
        cfg.post_both.assert_not_called(); cfg.notify_local.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_selftest_and_help_exit_zero(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("SELFTEST OK", r.stdout)
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("--listen", r.stdout)
        r = subprocess.run([sys.executable, "-c", "import nova_gsm_analyze"], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
