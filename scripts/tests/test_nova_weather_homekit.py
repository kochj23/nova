#!/usr/bin/env python3
"""Tests for nova_weather_homekit.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

wttr.in/HomeKit (urlopen), the scene-shortcut subprocess and notify are mocked file-wide; the state
file is redirected to a tempdir. No HomeKit scene is ever executed for real."""
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
PATH = SCRIPTS / "nova_weather_homekit.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("weather_homekit_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wh = _load()
_TD = tempfile.TemporaryDirectory()
_REAL_RUN = subprocess.run
_PATCHES = []


def setUpModule():
    for p in (patch.object(wh, "STATE_FILE", Path(_TD.name) / "state.json"),
              patch.object(wh.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(wh.subprocess, "run", side_effect=AssertionError("unmocked scene shortcut")),
              patch.object(wh, "notify", side_effect=AssertionError("unmocked notify"))):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _resp(obj):
    r = MagicMock(); r.__enter__.return_value = r
    r.read.return_value = json.dumps(obj).encode()
    return r


WTTR = {"current_condition": [{"temp_C": "33", "temp_F": "92", "FeelsLikeF": "95", "humidity": "20",
                               "windspeedMiles": "5", "weatherDesc": [{"value": "Sunny"}], "uvIndex": "9"}],
        "weather": [{"maxtempF": "97", "mintempF": "70",
                     "hourly": [{"time": "0", "chanceofrain": "90"}, {"time": "1500", "chanceofrain": "30"},
                                {"time": "2100", "chanceofrain": "70"}]}]}
W = {"temp_f": 92, "description": "Sunny", "rain_chance": 70, "wind_mph": 5, "max_f": 97}
INJECT = "Night; echo pwned"


def _main(weather, hour=12, contacts=()):
    with patch.object(wh, "get_weather", return_value=weather), patch.object(wh, "HOUR", hour), \
            patch.object(wh, "check_open_contacts", return_value=list(contacts)), \
            patch.object(wh, "notify") as n, patch.object(wh, "vector_remember") as vr, \
            redirect_stdout(io.StringIO()) as out:
        wh.main()
    return n, vr, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_no_scene_auto_executes_by_default(self):
        self.assertTrue(all(r["scene"] is None for r in wh.RULES))
        wh.STATE_FILE.unlink(missing_ok=True)
        with patch.object(wh, "execute_scene") as ex:
            _main({**W, "temp_f": 99, "wind_mph": 40})
        ex.assert_not_called()

    def test_scene_name_passed_as_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        with patch.object(wh.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run, \
                redirect_stdout(io.StringIO()):
            self.assertTrue(wh.execute_scene(INJECT))
        self.assertEqual(run.call_args.args[0][1], INJECT)


class TestPerformance(unittest.TestCase):
    def test_evaluate_10k_fast(self):
        t0 = time.perf_counter()
        with patch.object(wh, "HOUR", 9):
            for i in range(10_000):
                wh.evaluate_rules({**W, "temp_f": 40 + i % 60})
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_scene_api_down_falls_back_to_shortcut_once(self):
        with patch.object(wh.subprocess, "run", return_value=types.SimpleNamespace(returncode=1)) as run, \
                redirect_stdout(io.StringIO()):
            self.assertFalse(wh.execute_scene("Cool"))
        self.assertEqual(run.call_count, 1)

    def test_weather_fetch_fails_open(self):
        # get_weather() — 3 wttr.in attempts (3 s / 6 s); then None and main skips the cycle
        with redirect_stdout(io.StringIO()) as out, patch.object(wh.time, "sleep"):
            self.assertIsNone(wh.get_weather())
            self.assertEqual(wh.check_open_contacts(), [])
        self.assertIn("Weather fetch error", out.getvalue())
        n, _, out = _main(None)
        n.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_get_weather_parses_and_rain_from_remaining_hours(self):
        with patch.object(wh.urllib.request, "urlopen", return_value=_resp(WTTR)), patch.object(wh, "HOUR", 14):
            w = wh.get_weather()
        self.assertEqual((w["temp_f"], w["max_f"], w["uv"], w["description"]), (92, 97, 9, "Sunny"))
        self.assertEqual(w["rain_chance"], 70)                 # midnight's 90% is in the past

    def test_rule_hour_windows(self):
        with patch.object(wh, "HOUR", 7):
            self.assertEqual([a["name"] for a in wh.evaluate_rules({"temp_f": 45, "description": ""})], ["cold_morning"])
        with patch.object(wh, "HOUR", 22):
            self.assertEqual(wh.evaluate_rules({"temp_f": 45, "description": ""}), [])

    def test_state_resets_daily(self):
        wh.STATE_FILE.write_text(json.dumps({"date": "1999-01-01", "triggered": {"x": 1}}))
        self.assertEqual(wh.load_state()["triggered"], {})
        wh.STATE_FILE.write_text("{bad")
        self.assertEqual(wh.load_state()["date"], wh.TODAY)


class TestIntegration(unittest.TestCase):
    def test_slack_post_routes_through_notify(self):
        with patch.object(wh, "notify") as n:
            wh.slack_post("*Weather Alert*\nline")
        self.assertEqual(n.call_args.args[0], "Weather Alert")
        self.assertEqual((n.call_args.kwargs["category"], n.call_args.kwargs["body"]), ("weather", "line"))

    def test_open_contacts_parsed(self):
        accs = {"accessories": [{"name": "Garage", "services": [{"characteristics": [{"type": "ContactSensorState", "value": 1}]}]},
                                {"name": "Door", "services": [{"characteristics": [{"type": "contact", "value": 0}]}]}]}
        with patch.object(wh.urllib.request, "urlopen", return_value=_resp(accs)):
            self.assertEqual(wh.check_open_contacts(), ["Garage"])


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_once_then_cools_down(self):
        wh.STATE_FILE.unlink(missing_ok=True)
        n, vr, out = _main(W, contacts=["Garage"])
        body = n.call_args.kwargs["body"]
        self.assertIn("Rain likely (70% chance)", body)
        self.assertIn("Hot day ahead (92F", body)
        self.assertIn("*Currently open:* Garage", body)
        self.assertEqual(vr.call_args.args[1]["type"], "weather_automation")
        self.assertEqual(set(json.loads(wh.STATE_FILE.read_text())["triggered"]), {"rain_alert", "hot_day"})
        n, _, out = _main(W)
        n.assert_not_called()
        self.assertIn("No new weather actions", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = _REAL_RUN([sys.executable, str(PATH), "--help"], capture_output=True, text=True, timeout=30,
                      env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--force", r.stdout)

    def test_import_never_runs_main(self):
        r = _REAL_RUN([sys.executable, "-c", "import nova_weather_homekit"], cwd=str(SCRIPTS),
                      capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
