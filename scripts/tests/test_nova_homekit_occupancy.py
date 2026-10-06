#!/usr/bin/env python3
"""Tests for nova_homekit_occupancy.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_homekit_occupancy.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oc = _load("homekit_occ_t", SCRIPT)
SRC = SCRIPT.read_text()
# Stub outbound paths at load: memory server, HomeKit query subprocess; quiet log; temp script path.
URLOPEN = MagicMock()
oc.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=URLOPEN))
oc.subprocess = MagicMock()
oc.subprocess.run.side_effect = RuntimeError("subprocess.run not mocked in test")
oc.log = lambda m: None
_TMP = tempfile.TemporaryDirectory()
oc.HOMEKIT_SCRIPT = Path(_TMP.name) / "nova_homekit_query.sh"


def _at(hour):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2000, 1, 3, hour, 0)
    return patch.object(oc, "datetime", _DT)


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda *a: False
    return r


ACC = [{"room": "kitchen", "type": "Motion Sensor", "state": "detected"},
       {"room": "hall", "type": "Door", "state": "open", "name": "Front"},
       {"room": "den", "type": "Thermostat", "state": "81°F", "reachable": False}]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_stub_vehicle_data_is_never_persisted(self):
        URLOPEN.reset_mock()
        with _at(12):
            m = oc.build_occupancy_map([], oc.check_vehicle_presence())
        self.assertTrue(m["vehicle_stub"])
        oc.analyze_occupancy_pattern(m)
        URLOPEN.assert_not_called()

    def test_homekit_query_is_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_occupancy_map_10k_accessories(self):
        acc = [{"room": f"r{i % 50}", "type": "Motion" if i % 2 else "Door", "state": "open", "name": f"n{i}"}
               for i in range(10_000)]
        t0 = time.perf_counter()
        with _at(12):
            m = oc.build_occupancy_map(acc, {"home": True})
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(m["rooms"]), 50)


class TestRetry(unittest.TestCase):
    def test_remember_fails_open(self):
        # RETRY GAP: remember — one POST to the memory server; failure returns None, never raises
        URLOPEN.reset_mock(); URLOPEN.side_effect = OSError("down")
        try:
            self.assertIsNone(oc.remember("x"))
        finally:
            URLOPEN.side_effect = None
        self.assertEqual(URLOPEN.call_count, 1)

    def test_homekit_failures_return_empty(self):
        # RETRY GAP: get_homekit_accessories — one query per tick; missing script, rc!=0, bad JSON all -> []
        self.assertEqual(oc.get_homekit_accessories(), [])          # script absent
        oc.HOMEKIT_SCRIPT.write_text("#!/bin/bash\n")
        try:
            for ret in (SimpleNamespace(returncode=1, stdout="", stderr="e"), SimpleNamespace(returncode=0, stdout="{x", stderr=""),
                        SimpleNamespace(returncode=0, stdout='{"a":1}', stderr="")):
                with patch.object(oc.subprocess, "run", return_value=ret):
                    self.assertEqual(oc.get_homekit_accessories(), [])
        finally:
            oc.HOMEKIT_SCRIPT.unlink()

    def test_loop_survives_errors_and_stops_on_interrupt(self):
        sleeps = MagicMock(side_effect=[None, KeyboardInterrupt()])
        with patch.object(oc, "get_homekit_accessories", side_effect=[RuntimeError("boom"), []]), \
                patch.object(oc.time, "sleep", sleeps), patch.object(oc, "analyze_occupancy_pattern"):
            oc.occupancy_monitor_loop()
        self.assertEqual(sleeps.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_room_inference_and_anomalies(self):
        with _at(12):
            m = oc.build_occupancy_map(ACC, {"home": True})
        self.assertTrue(m["rooms"]["kitchen"]["occupied"])
        self.assertEqual(m["rooms"]["hall"]["doors_open"], ["Front"])
        self.assertEqual(m["rooms"]["den"]["temperature"], 81.0)
        self.assertIn("den: Temperature high (81.0°F)", m["anomalies"])
        self.assertNotIn("Motion in kitchen during sleep hours", m["anomalies"])
        self.assertAlmostEqual(m["confidence"], 2 / 3)

    def test_sleep_hours_motion_and_empty(self):
        with _at(23):
            m = oc.build_occupancy_map(ACC[:1], {})
        self.assertIn("Motion in kitchen during sleep hours", m["anomalies"])
        self.assertFalse(m["home_occupied"])
        with _at(12):
            self.assertEqual(oc.build_occupancy_map([], {})["confidence"], 0.0)


class TestIntegration(unittest.TestCase):
    def test_remember_posts_to_memory_server(self):
        URLOPEN.reset_mock(); URLOPEN.return_value = _resp({"id": 42})
        self.assertEqual(oc.remember("hello", source="occupancy"), 42)
        req = URLOPEN.call_args.args[0]
        self.assertEqual(req.full_url, f"{oc.MEMORY_URL}/remember")
        self.assertEqual(json.loads(req.data), {"text": "hello", "source": "occupancy"})

    def test_state_composes_query_vehicle_and_map(self):
        with patch.object(oc, "get_homekit_accessories", return_value=ACC), _at(12):
            s = oc.get_occupancy_state()
        self.assertEqual(set(s["rooms"]), {"kitchen", "hall", "den"})
        self.assertTrue(s["vehicle_stub"])


class TestFunctional(unittest.TestCase):
    def test_real_vehicle_data_persists_state_and_anomalies(self):
        URLOPEN.reset_mock(); URLOPEN.return_value = _resp({"id": 1})
        with _at(12):
            m = oc.build_occupancy_map(ACC, {"home": True, "stub": False})
        oc.analyze_occupancy_pattern(m)
        texts = [json.loads(c.args[0].data)["text"] for c in URLOPEN.call_args_list]
        self.assertTrue(any(t.startswith("Occupancy anomaly:") for t in texts))
        self.assertTrue(texts[-1].startswith("Occupancy state: home=True"))

    def test_status_cli_prints_json(self):
        with patch.object(sys, "argv", ["x", "status"]), patch.object(oc, "get_homekit_accessories", return_value=[]), \
                redirect_stdout(io.StringIO()) as out:
            oc.main()
        self.assertEqual(json.loads(out.getvalue())["rooms"], {})


class TestFrame(unittest.TestCase):
    def test_status_exits_zero_offline(self):
        code = ("import sys,runpy,subprocess,urllib.request;"
                "subprocess.run=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "urllib.request.urlopen=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                f"sys.argv=[{str(SCRIPT)!r},'status'];runpy.run_path(sys.argv[0],run_name='__main__')")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"home_occupied"', r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
