"""
test_nova_home_watchdog.py — All 7 test categories for nova_home_watchdog.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.slack_bot_token.return_value = "xoxb-fake"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_home_watchdog.py"
_spec = importlib.util.spec_from_file_location("nova_home_watchdog", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

analyze_accessories = _mod.analyze_accessories
load_state = _mod.load_state
save_state = _mod.save_state
is_sleep_hours = _mod.is_sleep_hours


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "AKIA", "password ="]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)

    def test_slack_token_from_nova_config(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova_config.slack_bot_token()", src)

    def test_vector_url_is_localhost(self):
        self.assertIn("127.0.0.1", _mod.VECTOR_URL)

    def test_state_file_under_home(self):
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))

    def test_no_color_temperature_false_alerts(self):
        """Should not alert on Hue bulb color temperature (mired values 140-500)."""
        accessories = [{
            "name": "Hue Lamp",
            "room": "Living Room",
            "uuid": "hue_001",
            "services": [{
                "type": "Lightbulb",
                "characteristics": [
                    {"type": "colortemperature", "value": 370},  # mired — should be ignored
                ]
            }]
        }]
        alerts, _ = analyze_accessories(accessories, {})
        temp_alerts = [a for a in alerts if "Hue Lamp" in a and "°F" in a]
        self.assertEqual(len(temp_alerts), 0, "Color temperature should not trigger temp alerts")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_analyze_accessories_fast(self):
        accessories = [
            {
                "name": f"Sensor {i}", "room": "Room", "uuid": f"acc_{i}",
                "services": [{"type": "ContactSensor",
                               "characteristics": [{"type": "contactsensorstate", "value": 0}]}]
            }
            for i in range(50)
        ]
        start = time.perf_counter()
        analyze_accessories(accessories, {})
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_sleep_hours_check_fast(self):
        start = time.perf_counter()
        for _ in range(10000):
            is_sleep_hours()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_vector_remember_silently_fails(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                _mod.vector_remember("test motion event", {})
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")

    def test_slack_alert_silently_fails(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                _mod.slack_alert("test alert")
            except Exception as e:
                self.fail(f"slack_alert raised: {e}")

    def test_get_accessories_falls_back_on_curl_failure(self):
        """get_accessories should fall back to Shortcuts CLI if curl fails."""
        with patch("subprocess.run") as mock_run:
            # First call (curl) fails, second (shortcut) returns empty
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout="", stderr="connection refused"),
                MagicMock(returncode=1, stdout="", stderr="shortcut failed"),
            ]
            result = _mod.get_accessories()
        self.assertIsInstance(result, list)

    def test_load_state_returns_empty_dict_on_missing(self):
        with patch.object(_mod.STATE_FILE, "exists", return_value=False):
            state = load_state()
        self.assertIsInstance(state, dict)

    def test_load_state_handles_corrupt_json(self):
        with patch.object(_mod.STATE_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="{BAD"):
                state = load_state()
        self.assertIsInstance(state, dict)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_contact_sensor_open_alert_after_10_min(self):
        """A door open for >10 min should trigger an alert."""
        accessories = [{
            "name": "Front Door", "room": "Entry", "uuid": "door_001",
            "services": [{"type": "ContactSensor",
                          "characteristics": [{"type": "contactsensorstate", "value": 1}]}]
        }]
        # State shows door was opened 15 minutes ago, no alert yet
        state = {"contact_door_001": {"first_open": time.time() - 15 * 60}}
        with patch.object(_mod, "is_sleep_hours", return_value=False):
            alerts, new_state = analyze_accessories(accessories, state)
        self.assertGreater(len(alerts), 0, "Should alert when door open >10 min")
        self.assertIn("Front Door", alerts[0])

    def test_contact_sensor_open_no_alert_under_10_min(self):
        """A door open for <10 min should NOT trigger an alert."""
        accessories = [{
            "name": "Front Door", "room": "Entry", "uuid": "door_001",
            "services": [{"type": "ContactSensor",
                          "characteristics": [{"type": "contactsensorstate", "value": 1}]}]
        }]
        state = {}  # First time seeing it open
        with patch.object(_mod, "is_sleep_hours", return_value=False):
            alerts, _ = analyze_accessories(accessories, state)
        door_alerts = [a for a in alerts if "Front Door" in a]
        self.assertEqual(len(door_alerts), 0, "Should not alert under 10 min")

    def test_temperature_alert_hot(self):
        """Room temperature above 85°F should alert."""
        accessories = [{
            "name": "Garage Sensor", "room": "Garage", "uuid": "temp_001",
            "services": [{"type": "TemperatureSensor",
                          "characteristics": [{"type": "currenttemperature", "value": 35.0}]}]
            # 35°C = 95°F > 85°F threshold
        }]
        alerts, _ = analyze_accessories(accessories, {})
        temp_alerts = [a for a in alerts if "Garage Sensor" in a]
        self.assertGreater(len(temp_alerts), 0, "Should alert on high temperature")

    def test_temperature_normal_no_alert(self):
        """Normal room temperature (20°C = 68°F) should not alert."""
        accessories = [{
            "name": "Living Room", "room": "Living", "uuid": "temp_002",
            "services": [{"type": "TemperatureSensor",
                          "characteristics": [{"type": "currenttemperature", "value": 20.0}]}]
        }]
        alerts, _ = analyze_accessories(accessories, {})
        self.assertEqual(len(alerts), 0)

    def test_motion_during_sleep_hours_alerts(self):
        """Motion detection during sleep hours should alert."""
        accessories = [{
            "name": "Hallway Motion", "room": "Hallway", "uuid": "motion_001",
            "services": [{"type": "MotionSensor",
                          "characteristics": [{"type": "motiondetected", "value": True}]}]
        }]
        with patch.object(_mod, "is_sleep_hours", return_value=True):
            with patch.object(_mod, "vector_remember"):
                alerts, _ = analyze_accessories(accessories, {})
        self.assertGreater(len(alerts), 0, "Motion during sleep should alert")

    def test_motion_during_day_no_alert(self):
        """Motion during waking hours should not alert."""
        accessories = [{
            "name": "Living Room Motion", "room": "Living", "uuid": "motion_002",
            "services": [{"type": "MotionSensor",
                          "characteristics": [{"type": "motiondetected", "value": True}]}]
        }]
        with patch.object(_mod, "is_sleep_hours", return_value=False):
            alerts, _ = analyze_accessories(accessories, {})
        self.assertEqual(len(alerts), 0, "Motion during day should not alert")

    def test_contact_closed_clears_state(self):
        """When door closes, its state entry should be removed."""
        accessories = [{
            "name": "Back Door", "room": "Kitchen", "uuid": "door_002",
            "services": [{"type": "ContactSensor",
                          "characteristics": [{"type": "contactsensorstate", "value": 0}]}]
        }]
        state = {"contact_door_002": {"first_open": time.time() - 20 * 60}}
        _, new_state = analyze_accessories(accessories, state)
        self.assertNotIn("contact_door_002", new_state, "Closed door should clear state")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_no_crash_on_no_accessories(self):
        with patch.object(_mod, "get_accessories", return_value=[]):
            try:
                _mod.main()
            except Exception as e:
                self.fail(f"main() raised with no accessories: {e}")

    def test_main_posts_alert_on_open_door(self):
        accessories = [{
            "name": "Front Door", "room": "Entry", "uuid": "door_test",
            "services": [{"type": "ContactSensor",
                          "characteristics": [{"type": "contactsensorstate", "value": 1}]}]
        }]
        # Door has been open for 15 minutes
        prev_state = {"contact_door_test": {"first_open": time.time() - 15 * 60}}
        slack_calls = []
        with patch.object(_mod, "get_accessories", return_value=accessories):
            with patch.object(_mod, "load_state", return_value=prev_state):
                with patch.object(_mod, "save_state"):
                    with patch.object(_mod, "slack_alert",
                                      side_effect=lambda m: slack_calls.append(m)):
                        with patch.object(_mod, "vector_remember"):
                            _mod.main()
        self.assertGreater(len(slack_calls), 0)
        self.assertIn("Front Door", slack_calls[0])

    def test_main_saves_state_after_run(self):
        accessories = [{
            "name": "Test Sensor", "room": "Test", "uuid": "test_001",
            "services": []
        }]
        save_calls = []
        with patch.object(_mod, "get_accessories", return_value=accessories):
            with patch.object(_mod, "load_state", return_value={}):
                with patch.object(_mod, "save_state",
                                  side_effect=lambda s: save_calls.append(s)):
                    with patch.object(_mod, "slack_alert"):
                        with patch.object(_mod, "vector_remember"):
                            _mod.main()
        self.assertEqual(len(save_calls), 1, "State should be saved once per run")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_motion_cooldown_prevents_spam(self):
        """Motion alerts should have a 30-minute cooldown."""
        accessories = [{
            "name": "Hallway", "room": "Hall", "uuid": "motion_cool",
            "services": [{"type": "MotionSensor",
                          "characteristics": [{"type": "motiondetected", "value": True}]}]
        }]
        # State shows alert was just sent
        state = {"motion_motion_cool": time.time() - 5 * 60}  # 5 min ago
        with patch.object(_mod, "is_sleep_hours", return_value=True):
            with patch.object(_mod, "vector_remember"):
                alerts, _ = analyze_accessories(accessories, state)
        self.assertEqual(len(alerts), 0, "Motion cooldown should prevent repeat alerts")

    def test_temp_alert_cooldown(self):
        """Temperature alerts should have a 1-hour cooldown."""
        accessories = [{
            "name": "Garage", "room": "Garage", "uuid": "temp_cool",
            "services": [{"type": "TemperatureSensor",
                          "characteristics": [{"type": "currenttemperature", "value": 35.0}]}]
        }]
        # Alert was sent 30 min ago
        state = {"temp_temp_cool_alert": time.time() - 30 * 60}
        alerts, _ = analyze_accessories(accessories, state)
        self.assertEqual(len(alerts), 0, "Temp alert should respect 1-hour cooldown")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_script_is_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_module_has_main(self):
        self.assertTrue(callable(_mod.main))

    def test_sleep_hours_defined(self):
        self.assertIsNotNone(_mod.SLEEP_HOURS)
        self.assertEqual(len(_mod.SLEEP_HOURS), 2)

    def test_state_file_is_json(self):
        self.assertTrue(str(_mod.STATE_FILE).endswith(".json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
