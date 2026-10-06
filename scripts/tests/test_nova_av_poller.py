#!/usr/bin/env python3
"""Tests for nova_av_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch("logging.basicConfig"):            # no FileHandler on the real av_poller.log
        spec.loader.exec_module(mod)
    return mod


av = _load("nova_av_poller_t", SCRIPTS / "nova_av_poller.py")
SRC = (SCRIPTS / "nova_av_poller.py").read_text()
EVIL = "x' OR '1'='1"


def _frame(payload):
    """Build a receiver-side eISCP frame for e.g. 'PWR01'."""
    data = f"!1{payload}\x1a\r\n".encode()
    return b"ISCP" + (16).to_bytes(4, "big") + len(data).to_bytes(4, "big") + b"\x01\x00\x00\x00" + data


class _Sock:
    def __init__(self, chunks):
        self.chunks = list(chunks); self.sent = []

    def sendall(self, b):
        self.sent.append(b)

    def recv(self, n):
        if not self.chunks:
            raise socket.timeout()
        return self.chunks.pop(0)

    def close(self):
        pass


def _poller():
    with patch.object(av, "get_db", return_value=MagicMock()), patch.object(av, "ensure_tables"):
        return av.AVPoller()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", av.DB_DSN)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        conn = MagicMock(); cur = conn.cursor.return_value.__enter__.return_value
        av.record_state(conn, EVIL, "main", {"power": "on"})
        sql, params = cur.execute.call_args[0]
        self.assertNotIn(EVIL, sql)
        self.assertEqual(params[0], EVIL)


class TestPerformance(unittest.TestCase):
    def test_frame_roundtrip_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            f = av.eiscp_build_frame(f"MVL{i % 100:02X}")
            self.assertTrue(av.eiscp_parse_response(f).startswith("MVL"))
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_failed_write_triggers_db_reconnect(self):
        p = _poller()
        new_db = MagicMock()
        with patch.object(av, "record_state", side_effect=RuntimeError("conn lost")), \
                patch.object(av, "get_db", return_value=new_db) as g:
            p._record("Bose Kitchen", "main", "k", {"power": "on"})
        self.assertEqual(g.call_count, 1)
        self.assertIs(p.db, new_db)
        self.assertNotIn("k", p.last_write_times)

    def test_onkyo_unreachable_fails_open(self):
        # RETRY GAP: OnkyoConnection.connect — one connect per poll; next poll interval is the retry
        conn = av.OnkyoConnection("192.0.2.1", 60128, "t")
        with patch.object(av.socket, "socket", side_effect=OSError("no route")):
            st = av.query_onkyo(conn, "main")
        self.assertEqual(st["power"], "unreachable")

    def test_soap_failure_returns_none(self):
        # RETRY GAP: soap_request — one urlopen per action; query_bose reports unreachable
        with patch.object(av.urllib.request, "urlopen", side_effect=OSError("x")) as m:
            st = av.query_bose(av.BOSE_DEVICES[0])
        self.assertEqual(st["power"], "unreachable")
        self.assertEqual(m.call_count, 4)


class TestUnit(unittest.TestCase):
    def test_build_frame_layout(self):
        f = av.eiscp_build_frame("PWRQSTN")
        self.assertEqual(f[:4], b"ISCP")
        self.assertEqual(int.from_bytes(f[8:12], "big"), len(b"!1PWRQSTN\r"))
        self.assertTrue(f.endswith(b"!1PWRQSTN\r"))

    def test_parse_rejects_garbage(self):
        self.assertIsNone(av.eiscp_parse_response(b"short"))
        self.assertIsNone(av.eiscp_parse_response(b"XXXX" + b"\x00" * 20))
        self.assertEqual(av.eiscp_parse_response(_frame("PWR01")), "PWR01")

    def test_read_response_skips_noise_and_other_prefixes(self):
        c = av.OnkyoConnection("h", 1, "t")
        c.sock = _Sock([b"junk" + _frame("NLSxx") + _frame("PWR00")])
        self.assertEqual(c._read_response("PWR", timeout=0.5), "PWR00")

    def test_poll_interval_by_power(self):
        p = _poller()
        self.assertEqual(p.get_poll_interval("x"), av.POLL_ACTIVE_SEC)
        p.last_states["x"] = {"power": "standby"}
        self.assertEqual(p.get_poll_interval("x"), av.POLL_STANDBY_SEC)


class TestIntegration(unittest.TestCase):
    def test_query_onkyo_zone2_uses_zone_commands(self):
        conn = MagicMock(); conn.is_connected.return_value = True
        answers = {"ZPWQSTN": "ZPW01", "ZVLQSTN": "ZVL1A", "ZMTQSTN": "ZMT01", "ZSLQSTN": "ZSL2B"}
        conn.send_command.side_effect = lambda c: answers.get(c)
        st = av.query_onkyo(conn, "zone2")
        self.assertEqual(st, {"power": "on", "volume": 26, "muted": True, "input_source": "2B", "listening_mode": None})

    def test_power_on_feeds_presence_table(self):
        conn = MagicMock(); cur = conn.cursor.return_value.__enter__.return_value
        av.record_power_event(conn, "Onkyo TX-NR696", "main", "standby", "on")
        sqls = [c[0][0] for c in cur.execute.call_args_list]
        self.assertIn("telemetry.device_power_events", sqls[0])
        self.assertIn("telemetry.presence", sqls[1])
        self.assertEqual(cur.execute.call_args_list[1][0][1][:2], ("living_room", 0.7))


class TestFunctional(unittest.TestCase):
    def test_bose_poll_records_state_and_transition(self):
        p = _poller()
        p.last_states["Bose Kitchen/main"] = {"power": "standby"}
        st = {"power": "on", "volume": 20}
        with patch.object(av, "query_bose", return_value=st), patch.object(av, "record_state") as rs, \
                patch.object(av, "record_power_event") as rpe:
            p.poll_bose(av.BOSE_DEVICES[2])
        rpe.assert_called_once_with(p.db, "Bose Kitchen", "main", "standby", "on")
        rs.assert_called_once_with(p.db, "Bose Kitchen", "main", st)
        self.assertEqual(p.last_states["Bose Kitchen/main"], st)

    def test_unreachable_records_heartbeat_not_power_event(self):
        p = _poller()
        with patch.object(av, "query_bose", side_effect=RuntimeError("boom")), \
                patch.object(av, "record_state") as rs, patch.object(av, "record_power_event") as rpe:
            p.poll_bose(av.BOSE_DEVICES[0])           # first sighting: heartbeat due
            p.last_poll_times.clear()
            p.poll_bose(av.BOSE_DEVICES[0])           # heartbeat not yet due -> silent
        self.assertEqual(rs.call_count, 1)
        rpe.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_av_poller"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
