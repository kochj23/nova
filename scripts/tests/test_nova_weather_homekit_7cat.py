#!/usr/bin/env python3
"""7-category gap tests for nova_weather_homekit.py — the wttr.in retry added here (the base suite
marked it 'RETRY GAP'). wttr.in, HomeKit and notify are mocked. Base suite:
test_nova_weather_homekit.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_weather_homekit_7cat.py
"""
import importlib.util
import io
import json
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_weather_homekit.py"
spec = importlib.util.spec_from_file_location("weather_homekit_7cat", PATH)
wh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wh)

WTTR = {"current_condition": [{"temp_C": "20", "temp_F": "68", "FeelsLikeF": "68", "humidity": "40",
                               "windspeedMiles": "5", "weatherDesc": [{"value": "Sunny"}], "uvIndex": "3"}],
        "weather": [{"maxtempF": "75", "mintempF": "55", "hourly": []}]}


def resp(obj=WTTR):
    r = MagicMock(); r.__enter__.return_value = r
    r.read.return_value = json.dumps(obj).encode()
    return r


class _Quiet(unittest.TestCase):
    def setUp(self):
        r = redirect_stdout(io.StringIO()); self.out = r.__enter__(); self.addCleanup(r.__exit__, None, None, None)
        p = patch.object(wh.time, "sleep"); self.sleep = p.start(); self.addCleanup(p.stop)


class TestSecurity(_Quiet):
    def test_fixed_https_endpoint_no_user_input(self):
        with patch.object(wh.urllib.request, "urlopen", return_value=resp()) as u:
            wh.get_weather()
        self.assertEqual(u.call_args.args[0].full_url, "https://wttr.in/burbank,ca?format=j1")


class TestPerformance(_Quiet):
    def test_timeout_and_bounded_retry(self):
        with patch.object(wh.urllib.request, "urlopen", side_effect=OSError("x")) as u:
            wh.get_weather()
        self.assertEqual(u.call_count, 3)
        self.assertTrue(all(c.kwargs["timeout"] == 10 for c in u.call_args_list))
        self.assertLessEqual(sum(c.args[0] for c in self.sleep.call_args_list), 10)


class TestRetry(_Quiet):
    def test_recovers_on_second_attempt(self):
        with patch.object(wh.urllib.request, "urlopen", side_effect=[OSError("503"), resp()]):
            w = wh.get_weather()
        self.assertIsNotNone(w)
        self.sleep.assert_called_once_with(3)

    def test_bad_json_retried(self):
        bad = MagicMock(); bad.__enter__.return_value = bad; bad.read.return_value = b"<html>rate limited"
        with patch.object(wh.urllib.request, "urlopen", side_effect=[bad, resp()]):
            self.assertIsNotNone(wh.get_weather())

    def test_final_failure_logged(self):
        with patch.object(wh.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertIsNone(wh.get_weather())
        self.assertIn("Weather fetch error", self.out.getvalue())


class TestUnit(_Quiet):
    def test_parsed_fields(self):
        with patch.object(wh.urllib.request, "urlopen", return_value=resp()):
            w = wh.get_weather()
        self.assertIsInstance(w, dict)


class TestIntegration(_Quiet):
    def test_parse_after_retry_matches_first_try(self):
        with patch.object(wh.urllib.request, "urlopen", return_value=resp()):
            a = wh.get_weather()
        with patch.object(wh.urllib.request, "urlopen", side_effect=[OSError("x"), resp()]):
            b = wh.get_weather()
        self.assertEqual(a, b)


class TestFunctional(_Quiet):
    def test_two_failures_then_success_yields_weather(self):
        with patch.object(wh.urllib.request, "urlopen", side_effect=[OSError("a"), OSError("b"), resp()]):
            self.assertIsNotNone(wh.get_weather())
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [3, 6])


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(PATH)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(callable(wh.main) and callable(wh.get_weather))


if __name__ == "__main__":
    unittest.main()
