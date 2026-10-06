#!/usr/bin/env python3
"""Tests for nova_bambu_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import runpy
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_bambu_watch.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nbw_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bw = _load()
# stub every outbound side effect at module load: Slack notify, ntfy push, memory store, Keychain
bw.notify = MagicMock()
bw.code = lambda serial: "12345678"
bw._ntfy_topic = lambda: ""


def _printer(key="P1"):
    with patch.object(bw.mqtt, "Client", return_value=MagicMock()):
        return bw.Printer(key, {"name": f"Printer {key[-1]}", "ip": "10.0.0.9", "serial": "SER" + key})


def _msg(d):
    return SimpleNamespace(payload=json.dumps(d).encode())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token|access_code)\s*=\s*['\"][A-Za-z0-9+/]{8,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_access_code_comes_from_keychain(self):
        self.assertIn('"security", "find-generic-password"', SRC)
        self.assertIn("nova-bambu-", SRC)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        self.assertEqual(SRC.count("VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"), 2)


class TestPerformance(unittest.TestCase):
    def test_10k_mqtt_deltas_fast(self):
        p = _printer()
        t0 = time.perf_counter()
        for i in range(10_000):
            p._on_message(None, None, _msg({"print": {"mc_percent": i % 100, "gcode_state": "RUNNING"}}))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(p.state["mc_percent"], 99)


class TestRetry(unittest.TestCase):
    def setUp(self):
        bw._pg_conn = None

    def test_pg_reconnects_after_failure(self):
        # _pg() self-heals: a failed connect returns None, the next tick reconnects
        good = MagicMock(closed=False)
        import psycopg2
        with patch.object(psycopg2, "connect", side_effect=[Exception("down"), Exception("down"), good]) as c:
            self.assertIsNone(bw._pg())
            self.assertIsNone(bw._pg())
            self.assertIs(bw._pg(), good)
        self.assertEqual(c.call_count, 3)
        bw._pg_conn = None

    def test_memory_and_push_fail_open(self):
        # RETRY GAP: remember()/push() — single urlopen attempt, errors logged never raised
        with patch.object(bw.urllib.request, "urlopen", side_effect=OSError("net")) as u:
            bw.remember("x")
            with patch.object(bw, "_ntfy_topic", return_value="topic"):
                bw.push("t", "m")
        self.assertEqual(u.call_count, 2)

    def test_notify_failure_never_raises(self):
        with patch.object(bw, "notify", side_effect=RuntimeError("pg")):
            bw._alert("info", "t", "b", "k")


class TestUnit(unittest.TestCase):
    def setUp(self):
        bw.notify.reset_mock()

    def test_fmt_age_and_busy(self):
        self.assertEqual(bw._fmt_age(30), "30s")
        self.assertEqual(bw._fmt_age(600), "10m")
        self.assertEqual(bw._fmt_age(7200), "2h")
        self.assertEqual(bw._fmt_age(3 * 86400), "3d")
        self.assertTrue(bw.is_busy("PAUSE"))
        self.assertFalse(bw.is_busy(None))
        self.assertTrue(bw._is_filament_runout(0, 0x07008011))
        self.assertFalse(bw._is_filament_runout(0, 0x1))

    def test_status_line_offline_live_stale(self):
        p = _printer()
        self.assertIn("OFFLINE", p.status_line())
        p.state = {"gcode_state": "RUNNING", "subtask_name": "benchy", "mc_percent": 42,
                   "nozzle_temper": 220, "bed_temper": 60}
        p.connected, p.last_report_ts = True, time.time()
        self.assertIn("42%", p.status_line())
        p.last_report_ts = time.time() - bw.STALE_AFTER_S - 5
        self.assertIn("OFFLINE", p.status_line())

    def test_bad_payload_ignored_and_first_report_silent(self):
        p = _printer()
        p._on_message(None, None, SimpleNamespace(payload=b"not json"))
        p._on_message(None, None, _msg({"info": {}}))
        self.assertEqual(p.state, {})
        p._on_message(None, None, _msg({"print": {"gcode_state": "RUNNING"}}))
        bw.notify.assert_not_called()          # initial sync is not a transition

    def test_transitions_alert(self):
        p = _printer()
        p._on_message(None, None, _msg({"print": {"gcode_state": "RUNNING", "subtask_name": "j"}}))
        p._on_message(None, None, _msg({"print": {"gcode_state": "FAILED", "mc_percent": 50}}))
        self.assertEqual(bw.notify.call_args.kwargs["level"], "critical")
        p._on_message(None, None, _msg({"print": {"hms": [{"attr": 1, "code": 0x07008011}]}}))
        self.assertIn("Filament runout", bw.notify.call_args[0][0])


class TestIntegration(unittest.TestCase):
    def test_pg_sample_writes_live_and_offline_rows(self):
        cur = MagicMock()
        conn = MagicMock(closed=False)
        conn.cursor.return_value.__enter__.return_value = cur
        live, off, never = _printer("P1"), _printer("P2"), _printer("P3")
        live.state = {"gcode_state": "RUNNING", "stg_cur": 0}
        live.connected, live.last_report_ts = True, time.time()
        off.last_report_ts = time.time() - 9999
        with patch.object(bw, "_pg", return_value=conn):
            bw.pg_sample([live, off, never])
        rows = [c[0][1] for c in cur.execute.call_args_list]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][2:5], ("RUNNING", True, "printing"))
        self.assertEqual(rows[1][2], "OFFLINE")
        self.assertIn("bambu_telemetry", cur.execute.call_args[0][0])

    def test_digest_goes_to_memory_with_source(self):
        with patch.object(bw.urllib.request, "urlopen") as u:
            lines = bw.write_digest([_printer()], blocking=True)
        req = u.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/remember"))
        self.assertEqual(json.loads(req.data)["source"], "bambu")
        self.assertEqual(len(lines), 1)


class TestFunctional(unittest.TestCase):
    def test_controls_publish_expected_commands(self):
        p = _printer()
        p.pause(); p.speed("sport"); p.light(False)
        sent = [json.loads(c[0][1]) for c in p.client.publish.call_args_list]
        self.assertEqual(sent[0]["print"]["command"], "pause")
        self.assertEqual(sent[1]["print"]["param"], "3")
        self.assertEqual(sent[2]["system"]["led_mode"], "off")

    def test_upload_missing_file_refused(self):
        p = _printer()
        ok, m = p.upload_and_print("/nonexistent/x.3mf")
        self.assertFalse(ok)
        self.assertIn("file not found", m)
        p.client.publish.assert_not_called()

    def _run_main(self, argv, frame=None):
        client = MagicMock()
        if frame:
            client.loop_start.side_effect = lambda: client.on_message(None, None, _msg({"print": frame}))
        with patch("paho.mqtt.client.Client", return_value=client), \
             patch("subprocess.check_output", return_value=b"12345678"), \
             patch("time.sleep"), patch.object(sys, "argv", ["nova_bambu_watch.py"] + argv):
            try:
                runpy.run_path(str(SCRIPT), run_name="__main__")
            except SystemExit as e:
                return client, e.code
        return client, 0

    def test_offline_printer_refuses_command(self):
        client, rc = self._run_main(["stop", "P1"])
        self.assertEqual(rc, 1)
        client.publish.assert_not_called()

    def test_calibrate_refuses_busy_printer(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            client, rc = self._run_main(["calibrate", "P1"], frame={"gcode_state": "RUNNING"})
        self.assertIn("refusing to calibrate", buf.getvalue())
        self.assertFalse(any("calibration" in str(c) for c in client.publish.call_args_list))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("calibrate", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_bambu_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
