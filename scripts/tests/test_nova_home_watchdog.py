#!/usr/bin/env python3
"""Tests for nova_home_watchdog.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The module reads the Slack token from Keychain at import, so it is loaded with
nova_config.slack_bot_token stubbed; Slack/memory urlopen, curl/Shortcuts subprocess and the
state file are mocked/redirected for the whole file."""
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
PATH = SCRIPTS / "nova_home_watchdog.py"
SRC = PATH.read_text()

import nova_config  # noqa: E402

FAKE_TOKEN = "xoxb-test-not-real"
_REAL_RUN = subprocess.run   # the file-wide stub patches subprocess.run itself; Frame needs the real one


def _load():
    spec = importlib.util.spec_from_file_location("home_watchdog_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(nova_config, "slack_bot_token", return_value=FAKE_TOKEN) as tok:
        spec.loader.exec_module(mod)
    mod._tok_calls = tok.call_count
    return mod


hw = _load()
_TD = tempfile.TemporaryDirectory()
_PATCHES = []


def setUpModule():
    for p in (patch.object(hw, "STATE_FILE", Path(_TD.name) / "state.json"),
              patch.object(hw.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(hw.subprocess, "run", side_effect=OSError("no subprocess in tests"))):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _acc(name, svc, char, value, uuid=None):
    return {"name": name, "room": "Hall", "uuid": uuid or name,
            "services": [{"type": svc, "characteristics": [{"type": char, "value": value}]}]}


def _ok():
    r = MagicMock(); r.__enter__.return_value = r
    return r


def _cp(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("xoxb-", SRC)

    def test_token_from_keychain_helper_in_header(self):
        self.assertEqual(hw._tok_calls, 1)
        with patch.object(hw.urllib.request, "urlopen", return_value=_ok()) as u, redirect_stdout(io.StringIO()):
            hw.slack_alert("hi")
        req = u.call_args.args[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer " + FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, req.full_url)

    def test_subprocess_argv_lists(self):
        self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_analyze_10k_accessories_fast(self):
        accs = [_acc(f"s{i}", "ContactSensor", "contact", i % 2) for i in range(10_000)]
        t0 = time.perf_counter()
        alerts, st = hw.analyze_accessories(accs, {})
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(st), 5_000)


class TestRetry(unittest.TestCase):
    def test_api_down_falls_back_to_shortcut(self):
        good = json.dumps([_acc("x", "s", "c", 0)])
        with patch.object(hw.subprocess, "run", side_effect=[_cp(7, ""), _cp(0, good)]) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(len(hw.get_accessories()), 1)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0], [str(hw.HOMEKIT_SCRIPT)])

    def test_both_sources_fail_open(self):
        # RETRY GAP: get_accessories() — API once, Shortcut once, then [] (cron re-runs in 20 min)
        with patch.object(hw.subprocess, "run", side_effect=[OSError("x"), subprocess.TimeoutExpired("s", 30)]), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hw.get_accessories(), [])
        self.assertIn("timed out", out.getvalue())

    def test_slack_and_memory_errors_swallowed(self):
        with redirect_stdout(io.StringIO()) as out:
            hw.slack_alert("x")
            hw.vector_remember("x")
        self.assertIn("Slack error", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_sleep_hours_boundaries(self):
        for h, want in ((23, True), (2, True), (5, True), (6, False), (12, False), (22, False)):
            with patch.object(hw, "HOUR", h):
                self.assertEqual(hw.is_sleep_hours(), want, h)

    def test_contact_open_alerts_once_after_10_min_and_clears(self):
        st = {"contact_Door": {"first_open": time.time() - 15 * 60}}
        alerts, st = hw.analyze_accessories([_acc("Door", "ContactSensor", "contactstate", 1)], st)
        self.assertEqual(len(alerts), 1)
        self.assertIn("open for 15 minutes", alerts[0])
        alerts, st = hw.analyze_accessories([_acc("Door", "ContactSensor", "contactstate", 1)], st)
        self.assertEqual(alerts, [])
        alerts, st = hw.analyze_accessories([_acc("Door", "ContactSensor", "contactstate", 0)], st)
        self.assertNotIn("contact_Door", st)

    def test_temperature_rules(self):
        hot = hw.analyze_accessories([_acc("Attic", "TempSensor", "currenttemperature", 32)], {})[0]
        self.assertIn("90°F", hot[0])
        self.assertEqual(hw.analyze_accessories([_acc("Bulb", "Light", "colortemperature", 5)], {})[0], [])
        self.assertEqual(hw.analyze_accessories([_acc("B", "Light", "temperature", 370)], {})[0], [])
        self.assertEqual(hw.analyze_accessories([_acc("B", "S", "temperature", "n/a")], {})[0], [])
        self.assertEqual(hw.analyze_accessories([_acc("B", "S", "temperature", 21)], {})[0], [])


class TestIntegration(unittest.TestCase):
    def test_sleep_motion_alerts_and_remembers(self):
        with patch.object(hw, "HOUR", 2), patch.object(hw, "vector_remember") as vr:
            alerts, st = hw.analyze_accessories([_acc("Yard", "MotionSensor", "motiondetected", True)], {})
            again, _ = hw.analyze_accessories([_acc("Yard", "MotionSensor", "motiondetected", True)], st)
        self.assertEqual((len(alerts), again), (1, []))          # 30-min cooldown
        self.assertEqual(vr.call_args.args[1]["type"], "security_event")
        with patch.object(hw, "HOUR", 14):
            self.assertEqual(hw.analyze_accessories([_acc("Yard", "MotionSensor", "m", True)], {})[0], [])

    def test_memory_payload_source(self):
        with patch.object(hw.urllib.request, "urlopen", return_value=_ok()) as u:
            hw.vector_remember("t", {"k": 1})
        self.assertEqual(json.loads(u.call_args.args[0].data)["source"], "homekit")


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_and_saves_state(self):
        accs = [_acc("Attic", "TempSensor", "currenttemperature", 35)]
        with patch.object(hw, "get_accessories", return_value=accs), patch.object(hw, "slack_alert") as sa, \
                patch.object(hw, "vector_remember") as vr, redirect_stdout(io.StringIO()) as out:
            hw.main()
        self.assertIn("Nova Home Alert", sa.call_args.args[0])
        self.assertEqual(vr.call_args.args[1]["type"], "home_alert")
        self.assertIn("temp_Attic_alert", json.loads(hw.STATE_FILE.read_text()))
        self.assertIn("Sent 1 alert", out.getvalue())

    def test_no_accessories_is_quiet(self):
        with patch.object(hw, "get_accessories", return_value=[]), patch.object(hw, "slack_alert") as sa, \
                patch.object(hw, "save_state") as ss, redirect_stdout(io.StringIO()):
            hw.main()
        sa.assert_not_called(); ss.assert_not_called()

    def test_corrupt_state_loads_empty(self):
        hw.STATE_FILE.write_text("{bad")
        self.assertEqual(hw.load_state(), {})


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import nova_config; nova_config.slack_bot_token = lambda: ''; "
                "import nova_home_watchdog as m; print('OK', callable(m.main))")
        r = _REAL_RUN([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "OK True")


if __name__ == "__main__":
    unittest.main()
