"""
test_nova_weather_homekit.py — All 7 test categories for nova_weather_homekit.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_weather_homekit.py"
_spec = importlib.util.spec_from_file_location("nova_weather_homekit", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_state = _mod.load_state
save_state = _mod.save_state
evaluate_rules = _mod.evaluate_rules
get_weather = _mod.get_weather
execute_scene = _mod.execute_scene


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_"]:
            self.assertNotIn(pat, src)
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_state_file_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.STATE_FILE))
    def test_homekit_api_is_localhost(self):
        self.assertTrue(_mod.HOMEKIT_URL.startswith("http://127.0.0.1"))
    def test_vector_url_is_localhost(self):
        self.assertTrue(_mod.VECTOR_URL.startswith("http://127.0.0.1"))
    def test_weather_uses_public_no_key_api(self):
        src = _SCRIPT.read_text()
        self.assertIn("wttr.in", src, "Should use wttr.in (no API key needed)")


class TestPerformance(unittest.TestCase):
    def test_evaluate_rules_fast(self):
        weather = {"temp_f": 75, "rain_chance": 10, "wind_mph": 5, "description": "Sunny", "max_f": 78, "min_f": 62}
        start = time.perf_counter()
        for _ in range(1000):
            evaluate_rules(weather)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)
    def test_load_state_fast_on_missing(self):
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/state.json")):
            start = time.perf_counter()
            state = load_state()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.01)
    def test_rules_list_not_empty(self):
        self.assertGreater(len(_mod.RULES), 0)


class TestRetry(unittest.TestCase):
    def test_get_weather_returns_none_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = get_weather()
        self.assertIsNone(result)
    def test_execute_scene_falls_back_to_shortcuts(self):
        with patch("urllib.request.urlopen", side_effect=OSError("homekit down")):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = execute_scene("Cool Down")
        self.assertTrue(result)
    def test_vector_remember_does_not_raise(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            _mod.vector_remember("Weather alert", {})
    def test_execute_scene_returns_false_on_all_failures(self):
        with patch("urllib.request.urlopen", side_effect=OSError("homekit down")):
            with patch("subprocess.run", side_effect=Exception("shortcuts failed")):
                result = execute_scene("Failed Scene")
        self.assertFalse(result)


class TestUnit(unittest.TestCase):
    def test_extreme_heat_rule_triggers(self):
        weather = {"temp_f": 98, "rain_chance": 0, "wind_mph": 0, "description": "Hot"}
        with patch.object(_mod, "HOUR", 12):
            triggered = evaluate_rules(weather)
        names = [r["name"] for r in triggered]
        self.assertIn("extreme_heat", names)

    def test_rain_rule_triggers_on_high_chance(self):
        weather = {"temp_f": 65, "rain_chance": 80, "wind_mph": 0, "description": "Rainy"}
        triggered = evaluate_rules(weather)
        names = [r["name"] for r in triggered]
        self.assertIn("rain_alert", names)

    def test_pleasant_weather_rule_triggers(self):
        weather = {"temp_f": 72, "rain_chance": 5, "wind_mph": 0, "description": "Sunny"}
        with patch.object(_mod, "HOUR", 10):
            triggered = evaluate_rules(weather)
        names = [r["name"] for r in triggered]
        self.assertIn("pleasant_weather", names)

    def test_cold_morning_rule_triggers(self):
        weather = {"temp_f": 45, "rain_chance": 0, "wind_mph": 0, "description": "Cold"}
        with patch.object(_mod, "HOUR", 7):
            triggered = evaluate_rules(weather)
        names = [r["name"] for r in triggered]
        self.assertIn("cold_morning", names)

    def test_load_state_resets_on_new_day(self):
        yesterday = "2026-01-01"
        state = {"date": yesterday, "triggered": {"extreme_heat": 999999}, "scenes_run": []}
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = Path(tmpdir) / "state.json"
            sf.write_text(json.dumps(state))
            with patch.object(_mod, "STATE_FILE", sf):
                with patch.object(_mod, "TODAY", "2026-01-02"):
                    loaded = load_state()
        self.assertEqual(loaded["triggered"], {})

    def test_save_and_load_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                state = {"date": "2026-01-01", "triggered": {"rain_alert": 1704067200}, "scenes_run": []}
                save_state(state)
                loaded = load_state()
        self.assertIn("rain_alert", loaded["triggered"])


class TestIntegration(unittest.TestCase):
    def test_main_skips_already_triggered_rules(self):
        """Rules already triggered within cooldown should not fire again."""
        current_time = time.time()
        state = {"date": _mod.TODAY, "triggered": {"rain_alert": current_time - 100}, "scenes_run": []}
        weather = {"temp_f": 65, "rain_chance": 80, "wind_mph": 0, "description": "Rain",
                   "max_f": 68, "min_f": 55, "feels_f": 63, "humidity": 85, "uv": 1}
        posts = []
        with patch.object(_mod, "get_weather", return_value=weather):
            with patch.object(_mod, "load_state", return_value=state):
                with patch.object(_mod, "save_state"):
                    with patch.object(_mod, "slack_post", side_effect=lambda t: posts.append(t)):
                        with patch.object(_mod, "vector_remember"):
                            _mod.main()
        # rain_alert within 6h cooldown → should NOT post again
        rain_posts = [p for p in posts if "Rain" in p or "rain" in p.lower()]
        self.assertEqual(len(rain_posts), 0)

    def test_main_handles_no_weather(self):
        """main() must exit gracefully if weather fetch fails."""
        with patch.object(_mod, "get_weather", return_value=None):
            _mod.main()  # Should not raise


class TestFunctional(unittest.TestCase):
    def test_main_status_mode_prints_weather(self):
        import io
        from contextlib import redirect_stdout
        mock_weather = {"temp_f": 75, "feels_f": 73, "max_f": 80, "min_f": 60,
                        "rain_chance": 0, "wind_mph": 5, "uv": 5, "description": "Sunny",
                        "temp_c": 24, "humidity": 40}
        with patch("sys.argv", ["nova_weather_homekit.py", "--status"]):
            with patch.object(_mod, "get_weather", return_value=mock_weather):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    import importlib
                    # Re-execute __main__ block
                    try:
                        exec(open(_SCRIPT).read(), {"__name__": "__main__",
                                                     "sys": sys,
                                                     "argparse": __import__("argparse"),
                                                     **vars(_mod)})
                    except SystemExit:
                        pass
    def test_wind_alert_triggers(self):
        weather = {"temp_f": 70, "rain_chance": 0, "wind_mph": 35, "description": "Windy"}
        triggered = evaluate_rules(weather)
        names = [r["name"] for r in triggered]
        self.assertIn("wind_alert", names)


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.RULES, list)
        self.assertIsInstance(_mod.STATE_FILE, Path)
        self.assertIsInstance(_mod.HOMEKIT_URL, str)
    def test_functions_exist(self):
        for fn in ("get_weather", "execute_scene", "check_open_contacts",
                   "evaluate_rules", "load_state", "save_state",
                   "vector_remember", "slack_post", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
