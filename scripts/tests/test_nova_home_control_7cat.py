#!/usr/bin/env python3
"""nova_home_control.py — 7-category gap tests (Security, Performance, Retry, Unit, Integration,
Functional, Frame) for the Proteus guard on _shortcuts_run, the kill switch, and retry/backoff on every
device call (Bose SOAP, Onkyo eISCP connect, Shortcuts, weather PG). Written by Jordan Koch (via Claude).

No device is ever contacted: urlopen, socket.socket, subprocess.run, psycopg2.connect and time.sleep
are mocked in setUp for every test; the kill switch is patched explicitly."""
import importlib.util
import os
import socket
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
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_home_control.py"
import nova_safety_guards as guards  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("nova_home_control_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load()


class _Base(unittest.TestCase):
    def setUp(self):
        self.sock = MagicMock()
        self.sock.recv.side_effect = socket.timeout()
        self.ps = [patch.object(hc.socket, "socket", return_value=self.sock),
                   patch.object(hc.urllib.request, "urlopen"),
                   patch("subprocess.run", return_value=SimpleNamespace(returncode=0)),
                   patch.object(psycopg2, "connect", side_effect=psycopg2.OperationalError("no pg in tests")),
                   patch.object(hc.time, "sleep"),
                   patch.object(guards, "kill_engaged", return_value=False)]
        self.mock_sock_cls, self.urlopen, self.run, self.pg, self.sleep, self.kill = [p.start() for p in self.ps]
        resp = MagicMock()
        resp.__enter__.return_value.read.return_value = b"<ok/>"
        self.urlopen.return_value = resp

    def tearDown(self):
        for p in self.ps:
            p.stop()


# ── Security ────────────────────────────────────────────────────────────────────
class TestSecurity(_Base):
    def test_locking_shortcut_refused_never_spawned(self):
        for name in ("Lock Up", "Close Garage Door", "Arm Alarm"):
            self.assertFalse(hc.scenes._shortcuts_run(name), name)
        self.run.assert_not_called()

    def test_kill_switch_blocks_shortcuts(self):
        self.kill.return_value = True
        self.assertFalse(hc.scenes._shortcuts_run("Dim Living Room"))
        self.run.assert_not_called()

    def test_guards_unavailable_fails_closed(self):
        with patch.dict(sys.modules, {"nova_safety_guards": None}):
            self.assertFalse(hc.scenes._shortcuts_run("Dim Living Room"))
        self.run.assert_not_called()

    def test_shortcut_runs_as_argv_not_shell(self):
        hc.scenes._shortcuts_run("Dim; rm -rf ~")
        self.assertEqual(self.run.call_args[0][0], ["shortcuts", "run", "Dim; rm -rf ~"])
        self.assertNotIn("shell", self.run.call_args.kwargs)

    def test_http_error_not_retried(self):
        # a device that answered (HTTP 500) is not hammered
        self.urlopen.side_effect = urllib.error.HTTPError("u", 500, "err", {}, None)
        with self.assertRaises(ConnectionError):
            hc.bose.mute("bedroom")
        self.assertEqual(self.urlopen.call_count, 1)


# ── Performance ─────────────────────────────────────────────────────────────────
class TestPerformance(_Base):
    def test_backoff_bounded_per_call(self):
        self.urlopen.side_effect = urllib.error.URLError("down")
        with self.assertRaises(ConnectionError):
            hc.bose.mute("bedroom")
        self.assertLessEqual(sum(c[0][0] for c in self.sleep.call_args_list), 2.0)

    def test_goodnight_with_everything_down_bounded_attempts(self):
        self.urlopen.side_effect = urllib.error.URLError("down")
        self.sock.connect.side_effect = OSError("down")
        self.run.return_value = SimpleNamespace(returncode=1)
        t0 = time.perf_counter()
        hc.scenes.goodnight()
        self.assertLess(time.perf_counter() - t0, 2.0)
        n_calls = len(hc.BOSE_DEVICES) + len(hc.ONKYO_DEVICES) + 1 + 1   # bose, onkyo, zone2, shortcut
        self.assertLessEqual(self.urlopen.call_count + self.sock.connect.call_count + self.run.call_count,
                             n_calls * hc.RETRY_ATTEMPTS)


# ── Retry ───────────────────────────────────────────────────────────────────────
class TestRetry(_Base):
    def test_bose_recovers_on_second_attempt(self):
        ok = self.urlopen.return_value
        self.urlopen.side_effect = [urllib.error.URLError("blip"), ok]
        hc.bose.mute("bedroom")
        self.assertEqual(self.urlopen.call_count, 2)
        self.assertEqual([c[0][0] for c in self.sleep.call_args_list], [0.5])

    def test_onkyo_connect_retried_command_sent_once(self):
        self.sock.connect.side_effect = [OSError("refused"), OSError("refused"), None]
        hc.onkyo.power_off("office")
        self.assertEqual(self.sock.connect.call_count, 3)
        self.assertEqual(self.sock.sendall.call_count, 1)
        self.assertEqual([c[0][0] for c in self.sleep.call_args_list], [0.5, 1.0])

    def test_onkyo_gives_up_with_attempt_count(self):
        self.sock.connect.side_effect = OSError("refused")
        with self.assertRaisesRegex(ConnectionError, "after 3 attempts"):
            hc.onkyo.power_off("office")
        self.sock.sendall.assert_not_called()

    def test_shortcuts_retry_nonzero_and_timeout(self):
        self.run.side_effect = [subprocess.TimeoutExpired("s", 10), SimpleNamespace(returncode=1),
                                SimpleNamespace(returncode=0)]
        self.assertTrue(hc.scenes._shortcuts_run("Dim Living Room"))
        self.assertEqual(self.run.call_count, 3)

    def test_shortcuts_persistent_failure_not_silent(self):
        self.run.return_value = SimpleNamespace(returncode=1)
        with patch("builtins.print") as pr:
            self.assertFalse(hc.scenes._shortcuts_run("Dim Living Room"))
        self.assertEqual(self.run.call_count, 3)
        self.assertTrue(any("after 3 attempts" in str(c) for c in pr.call_args_list))

    def test_weather_pg_retried_then_error_dict(self):
        out = hc.weather.get_current()
        self.assertEqual(self.pg.call_count, 3)
        self.assertIn("Weather query failed", out["error"])


# ── Unit ────────────────────────────────────────────────────────────────────────
class TestUnit(_Base):
    def test_retry_constants(self):
        self.assertEqual(hc.RETRY_ATTEMPTS, 3)
        self.assertGreater(hc.RETRY_BACKOFF_S, 0)

    def test_every_scene_shortcut_passes_guard(self):
        import re
        names = re.findall(r'_shortcuts_run\("([^"]+)"\)', SCRIPT.read_text())
        self.assertTrue(names)
        for n in names:
            self.assertTrue(guards.physical_guard(f"shortcut {n}")[0], n)


# ── Integration ─────────────────────────────────────────────────────────────────
class TestIntegration(_Base):
    def test_scene_continues_after_device_retries_exhausted(self):
        self.sock.connect.side_effect = OSError("down")
        out = hc.scenes.movie_mode()
        self.assertIn("onkyo_error", out["results"])
        self.assertTrue(out["results"]["lights"])
        self.assertEqual(self.run.call_args[0][0], ["shortcuts", "run", "Dim Living Room"])

    def test_kill_switch_scene_holds_lights(self):
        self.kill.return_value = True
        out = hc.scenes.work()
        self.assertFalse(out["results"]["lights"])
        self.run.assert_not_called()


# ── Functional ──────────────────────────────────────────────────────────────────
class TestFunctional(_Base):
    def _main(self, *argv):
        with patch.object(sys, "argv", ["x", *argv]), patch("builtins.print"):
            try:
                hc.main()
                return 0
            except SystemExit as e:
                return e.code

    def test_cli_scene_golden_path_with_transient_failures(self):
        self.urlopen.side_effect = [urllib.error.URLError("blip")] + [self.urlopen.return_value] * 20
        self.assertEqual(self._main("scene", "party"), 0)
        self.assertTrue(self.run.called)

    def test_cli_scene_lights_fail_reports_false_not_crash(self):
        self.run.return_value = SimpleNamespace(returncode=1)
        out = hc.scenes.away()
        self.assertFalse(out["results"]["lights"])


# ── Frame ───────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_import_in_fresh_interpreter(self):
        r = subprocess.run([sys.executable, "-c",
                            "import nova_home_control as h; assert h.RETRY_ATTEMPTS == 3 and callable(h.main)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
