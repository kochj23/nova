#!/usr/bin/env python3
"""Tests for nova_thermal_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_thermal_monitor.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="thermal-test-"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tm = _load("thermal_under_test", SCRIPT)
tm.STATE = str(TMP / "state" / "thermal_monitor.json")      # never touch ~/.openclaw/state

HEALTHY = {"192.168.1.86": "65000", "kochj@192.168.1.11": "48000", "root@192.168.1.1": "61.5",
           "root@192.168.1.69": "CPU 52\nFANS 1200 1150 900\nDRV 38 41 40 39",
           "192.168.1.7": "3", "192.168.1.190": "7"}


def _ssh(table):
    calls = []

    def ssh(host, cmd, timeout=10):
        calls.append(host)
        return table.get(host, "")
    ssh.calls = calls
    return ssh


def _run_main(table, now, slack=None, state=None):
    Path(tm.STATE).parent.mkdir(parents=True, exist_ok=True)
    if state is not None:
        Path(tm.STATE).write_text(json.dumps(state))
    elif Path(tm.STATE).exists():
        Path(tm.STATE).unlink()
    slack = slack or MagicMock()
    out = io.StringIO()
    with patch.object(tm, "ssh", _ssh(table)), patch.object(tm, "slack", slack), \
         patch.object(tm.time, "time", lambda: now), redirect_stdout(out):
        tm.main()
    return slack, json.loads(Path(tm.STATE).read_text()), out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"](xox|[A-Za-z0-9+/]{20,})", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"security","find-generic-password"', SRC)     # Slack token from Keychain

    def test_ssh_and_curl_are_argv_batchmode(self):
        self.assertNotIn("shell=True", SRC)
        run = MagicMock(return_value=types.SimpleNamespace(stdout="12\n"))
        with patch.object(tm.subprocess, "run", run):
            self.assertEqual(tm.ssh("root@192.168.1.69", "cat /x; echo $(id)"), "12")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertEqual(argv[-1], "cat /x; echo $(id)")               # remote command is ONE argv element

    def test_slack_without_token_sends_nothing(self):
        run = MagicMock(return_value=types.SimpleNamespace(stdout=""))
        with patch.object(tm.subprocess, "run", run), redirect_stdout(io.StringIO()) as out:
            tm.slack("hi")
        self.assertEqual(run.call_count, 1)                             # keychain lookup only, no curl
        self.assertIn("no token", out.getvalue())


class TestPerformance(unittest.TestCase):
    def test_evaluate_10k_readings(self):
        readings = [{"core2_cpu": 60 + (i % 40), "unas_cpu": 50, "synology": 40, "udm_cpu": 55, "drive_max": 30 + (i % 30),
                     "unas_fans": [1000, 1000, 1000], "unas_fans_spinning": 3, "repl_tv_7": i % 700, "repl_mini_190": 5}
                    for i in range(10_000)]
        t0 = time.perf_counter()
        n = sum(len(tm.evaluate(r)) for r in readings)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertGreater(n, 0)


class TestRetry(unittest.TestCase):
    def test_ssh_fails_open_to_empty_and_readings_become_none(self):
        # RETRY GAP: ssh()/subprocess.run — one attempt per host; failure yields "" and a None reading, never a crash
        run = MagicMock(side_effect=subprocess.TimeoutExpired("ssh", 10))
        with patch.object(tm.subprocess, "run", run):
            r = tm.read_all()
        self.assertEqual(run.call_count, 6)                             # six hosts, one attempt each
        self.assertIsNone(r["core2_cpu"]); self.assertIsNone(r["unas_cpu"]); self.assertEqual(r["unas_fans"], [])
        self.assertIsNone(r["repl_tv_7"])

    def test_slack_post_failure_is_swallowed(self):
        # RETRY GAP: slack()/curl — single POST; an exception is printed, not raised
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[0])
            if cmd[0] == "curl":
                raise OSError("curl exploded")
            return types.SimpleNamespace(stdout="xoxb-test")
        with patch.object(tm.subprocess, "run", run), redirect_stdout(io.StringIO()) as out:
            tm.slack("alert")
        self.assertEqual(calls, ["security", "curl"])
        self.assertIn("slack err", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_evaluate_thresholds(self):
        base = {"unas_fans": [1000, 1000, 1000], "unas_fans_spinning": 3, "repl_tv_7": 1, "repl_mini_190": 1}
        self.assertEqual(tm.evaluate({**base, "core2_cpu": 93.0}), [])
        a = dict(tm.evaluate({**base, "core2_cpu": 94.0, "drive_max": 59}))
        self.assertEqual(a["core2_cpu"], "core2 CPU at 94°C (limit 93°C)")
        self.assertIn("drive_max", a)
        self.assertEqual(tm.evaluate({**base, "core2_cpu": None}), [])      # unreachable != hot

    def test_evaluate_fans_and_replicas(self):
        a = dict(tm.evaluate({"unas_fans": [600, 0, 0], "unas_fans_spinning": 1, "repl_tv_7": None, "repl_mini_190": 700}))
        self.assertIn("only 1 spinning fan", a["unas_fans"])
        self.assertIn("DOWN or not streaming", a["repl_tv_7"])
        self.assertIn("replay lag 700s", a["repl_mini_190"])
        self.assertNotIn("unas_fans", dict(tm.evaluate({"unas_fans": [], "unas_fans_spinning": 0, "repl_tv_7": 1, "repl_mini_190": 1})))

    def test_read_all_parsing(self):
        with patch.object(tm, "ssh", _ssh({**HEALTHY, "root@192.168.1.1": "garbage", "192.168.1.190": "-1"})):
            r = tm.read_all()
        self.assertEqual((r["core2_cpu"], r["synology"], r["unas_cpu"]), (65.0, 48.0, 52))
        self.assertEqual(r["unas_fans"], [1200, 1150, 900]); self.assertEqual(r["unas_fans_spinning"], 3)
        self.assertEqual(r["drive_max"], 41)
        self.assertIsNone(r["udm_cpu"])          # non-numeric -> None
        self.assertIsNone(r["repl_mini_190"])    # -1 sentinel = not streaming
        self.assertEqual(r["repl_tv_7"], 3)

    def test_state_helpers(self):
        self.assertEqual(tm.load_state(), {}) if not Path(tm.STATE).exists() else None
        tm.save_state({"last": {"x": 1}})
        self.assertEqual(tm.load_state(), {"last": {"x": 1}})


class TestIntegration(unittest.TestCase):
    def test_alert_then_debounce_then_recover(self):
        hot = {**HEALTHY, "192.168.1.86": "96000"}
        slack, st, out = _run_main(hot, now=1000)
        self.assertEqual(slack.call_count, 1)
        self.assertIn(":rotating_light: *Thermal alert* — core2 CPU at 96°C", slack.call_args[0][0])
        self.assertEqual(st["last"], {"core2_cpu": 1000})
        slack, st, out = _run_main(hot, now=1000 + tm.REALERT_SEC - 1, state=st)
        self.assertEqual(slack.call_count, 0)                            # still hot, inside the re-alert window
        slack, st, out = _run_main(hot, now=1000 + tm.REALERT_SEC + 1, state=st)
        self.assertEqual(slack.call_count, 1)                            # re-warn after 30 min
        slack, st, out = _run_main(HEALTHY, now=5000, state=st)
        self.assertIn(":white_check_mark: *Recovered* — core2_cpu", slack.call_args[0][0])
        self.assertEqual(st["last"], {})

    def test_channel_and_state_shape(self):
        self.assertEqual(tm.SLACK_CHANNEL, "#nova-notifications")
        slack, st, out = _run_main(HEALTHY, now=42)
        self.assertEqual(set(st), {"last", "snap", "ts"}); self.assertEqual(st["ts"], 42)


class TestFunctional(unittest.TestCase):
    def test_golden_path_quiet_closet(self):
        slack, st, out = _run_main(HEALTHY, now=7)
        self.assertEqual(slack.call_count, 0)
        self.assertEqual(out.strip(), "core2 65.0°C | UNAS 52°C fans[1200, 1150, 900] drives[38, 41, 40, 39] | Synology 48.0°C | UDM 61.5°C")

    def test_error_path_unreachable_replicas_page_once(self):
        table = {k: v for k, v in HEALTHY.items() if not k.startswith("192.168.1.7") and k != "192.168.1.190"}
        slack, st, out = _run_main(table, now=7)
        self.assertEqual(slack.call_count, 2)
        self.assertEqual(sorted(st["last"]), ["repl_mini_190", "repl_tv_7"])
        self.assertIn("UNAS 52°C", slack.call_args[0][0])                 # snapshot appended to every alert


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import subprocess\nsubprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError('ssh spawned'))\n"
                "import nova_thermal_monitor; print('ok', nova_thermal_monitor.REALERT_SEC)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok 1800")


if __name__ == "__main__":
    unittest.main()
