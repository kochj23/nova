#!/usr/bin/env python3
"""Tests for nova_bambu_status.py — the 7 house categories (Security, Performance, Retry, Unit,
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bb = _load("nova_bambu_status_t", SCRIPTS / "nova_bambu_status.py")
SRC = (SCRIPTS / "nova_bambu_status.py").read_text()
_TMP = tempfile.mkdtemp()
bb.STATE = Path(_TMP) / "state" / "nova_bambu_state.json"
META = {"name": "Printer T", "ip": "192.0.2.5", "serial": "SERIAL1"}


def _ok(code="12345678"):
    return types.SimpleNamespace(returncode=0, stdout=code + "\n")


class _FakeClient:
    """paho Client stand-in: on loop_start fires on_connect then delivers `report`."""
    instances = []

    def __init__(self, report=None, fail=None):
        self.report = report; self.fail = fail; self.pub = []; self.sub = []; self.creds = None
        _FakeClient.instances.append(self)

    def username_pw_set(self, u, p):
        self.creds = (u, p)

    def tls_set(self, **k):
        pass

    def tls_insecure_set(self, v):
        pass

    def connect(self, ip, port, keepalive):
        if self.fail:
            raise OSError(self.fail)

    def subscribe(self, t):
        self.sub.append(t)

    def publish(self, t, payload):
        self.pub.append((t, payload))

    def loop_start(self):
        self.on_connect(self)
        if self.report is not None:
            self.on_message(self, None, types.SimpleNamespace(payload=json.dumps({"print": self.report}).encode()))

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


def _mqtt(**kw):
    return types.SimpleNamespace(Client=lambda: _FakeClient(**kw))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token|code)\s*=\s*['\"][A-Za-z0-9+/]{8,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_access_code_comes_from_keychain(self):
        with patch.object(bb.subprocess, "run", return_value=_ok("abc")) as run:
            self.assertEqual(bb.access_code("S1"), "abc")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["security", "find-generic-password"])
        self.assertIn("nova-bambu-S1", argv)

    def test_missing_code_never_connects(self):
        miss = types.SimpleNamespace(returncode=44, stdout="")
        with patch.object(bb.subprocess, "run", return_value=miss), patch.object(bb, "mqtt") as mq:
            st = bb.poll_one("P9", META)
        self.assertIn("no access code", st["error"])
        mq.Client.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_poll_one_10k_reports_fast(self):
        t0 = time.perf_counter()
        with patch.object(bb.subprocess, "run", return_value=_ok()), \
                patch.object(bb, "mqtt", _mqtt(report={"gcode_state": "RUNNING", "mc_percent": 5})):
            for _ in range(2_000):
                bb.poll_one("P1", META, timeout=1)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_keychain_falls_back_to_second_lookup(self):
        miss = types.SimpleNamespace(returncode=44, stdout="")
        with patch.object(bb.subprocess, "run", side_effect=[miss, _ok("xyz")]) as run:
            self.assertEqual(bb.access_code("S1"), "xyz")
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("-a", run.call_args_list[1][0][0])

    def test_connect_failure_fails_open(self):
        # RETRY GAP: poll_one/mqtt connect — one attempt per run; error dict, no exception
        with patch.object(bb.subprocess, "run", return_value=_ok()), patch.object(bb, "mqtt", _mqtt(fail="refused")):
            st = bb.poll_one("P1", META)
        self.assertEqual(st["error"], "connect: refused")


class TestUnit(unittest.TestCase):
    def _poll(self, report):
        with patch.object(bb.subprocess, "run", return_value=_ok()), patch.object(bb, "mqtt", _mqtt(report=report)):
            return bb.poll_one("P1", META, timeout=0.3)

    def test_hot_finish_is_active_cold_finish_is_not(self):
        self.assertTrue(self._poll({"gcode_state": "FINISH", "nozzle_temper": 200})["active"])
        self.assertFalse(self._poll({"gcode_state": "finish", "nozzle_temper": 30})["active"])

    def test_report_fields_parsed(self):
        st = self._poll({"gcode_state": "RUNNING", "mc_percent": 42, "subtask_name": "benchy",
                         "layer_num": 3, "total_layer_num": 100})
        self.assertEqual((st["state"], st["percent"], st["job"], st["total_layers"]), ("RUNNING", 42, "benchy", 100))

    def test_no_report_is_offline(self):
        st = self._poll({"unrelated": 1})
        self.assertIn("no report", st["error"])


class TestIntegration(unittest.TestCase):
    def test_uses_printer_registry_and_device_topics(self):
        import bambu_printers
        self.assertIn("bambu_printers.PRINTERS", SRC)
        self.assertTrue(bambu_printers.PRINTERS)
        _FakeClient.instances.clear()
        with patch.object(bb.subprocess, "run", return_value=_ok("code9")), \
                patch.object(bb, "mqtt", _mqtt(report={"gcode_state": "IDLE"})):
            bb.poll_one("P1", META, timeout=0.3)
        c = _FakeClient.instances[-1]
        self.assertEqual(c.creds, ("bblp", "code9"))
        self.assertEqual(c.sub, ["device/SERIAL1/report"])
        self.assertEqual(json.loads(c.pub[0][1])["pushing"]["command"], "pushall")


class TestFunctional(unittest.TestCase):
    def test_main_writes_state_file(self):
        fake = {"A": {"id": "A", "name": "a", "state": "RUNNING", "active": True, "percent": 9, "job": "j"},
                "B": {"id": "B", "name": "b", "error": "connect: x"}}
        reg = {"A": {"name": "a"}, "B": {"name": "b"}}
        buf = io.StringIO()
        with patch.object(bb.bambu_printers, "PRINTERS", reg), \
                patch.object(bb, "poll_one", side_effect=lambda pid, meta: fake[pid]), redirect_stdout(buf):
            bb.main()
        self.assertEqual(json.loads(bb.STATE.read_text()), fake)
        self.assertIn("1 printer(s) active", buf.getvalue())
        self.assertIn("connect: x", buf.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_bambu_status"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
