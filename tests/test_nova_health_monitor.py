"""
test_nova_health_monitor.py — All 7 test categories for nova_health_monitor.py
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
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.JORDAN_DM = "D0AMPB3F4T0"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_health_monitor.py"
_spec = importlib.util.spec_from_file_location("nova_health_monitor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

summarize_readings = _mod.summarize_readings
check_alerts = _mod.check_alerts
load_state = _mod.load_state
save_state = _mod.save_state
ALERT_THRESHOLDS = _mod.ALERT_THRESHOLDS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "password ="]:
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

    def test_health_data_goes_to_dm_not_channel(self):
        """Health alerts must go to DM (JORDAN_DM), not public Slack channel."""
        src = _SCRIPT.read_text()
        self.assertIn("JORDAN_DM", src, "Health alerts should go to DM")

    def test_health_source_labeled_private(self):
        """Vector memory source must be 'apple_health' to keep data identifiable."""
        src = _SCRIPT.read_text()
        self.assertIn("apple_health", src, "Health data must be labeled as apple_health in memory")

    def test_icloud_health_path_uses_home(self):
        """iCloud health path must use Path.home(), not hardcoded /Users/..."""
        self.assertTrue(str(_mod.ICLOUD_HEALTH).startswith(str(Path.home())))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_summarize_readings_fast(self):
        readings = {
            "heart_rate": [{"value": 70 + i, "unit": "BPM", "date": "2026-01-01"} for i in range(100)]
        }
        start = time.perf_counter()
        summarize_readings(readings)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_check_alerts_fast(self):
        readings = {
            "heart_rate": [{"value": 75, "unit": "BPM"}],
            "blood_pressure_sys": [{"value": 120, "unit": "mmHg"}],
        }
        start = time.perf_counter()
        for _ in range(1000):
            check_alerts(readings)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_alert_thresholds_bounded(self):
        """All thresholds must have both high and low bounds."""
        for key, thresholds in ALERT_THRESHOLDS.items():
            self.assertIn("high", thresholds, f"{key} missing 'high' threshold")
            self.assertIn("low", thresholds, f"{key} missing 'low' threshold")
            self.assertGreater(thresholds["high"], thresholds["low"],
                               f"{key}: high must be > low")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_vector_remember_silently_fails(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                _mod.vector_remember("test health data", {})
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")

    def test_vector_remember_async_silently_fails(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                _mod.vector_remember_async("test", {})
            except Exception as e:
                self.fail(f"vector_remember_async raised: {e}")

    def test_load_state_returns_defaults_on_missing(self):
        with patch.object(_mod.STATE_FILE, "exists", return_value=False):
            state = load_state()
        self.assertIn("last_ingest", state)
        self.assertIn("last_alert_date", state)

    def test_load_state_handles_corrupt(self):
        with patch.object(_mod.STATE_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="{BAD"):
                state = load_state()
        self.assertIn("last_ingest", state)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_summarize_single_reading(self):
        readings = {"heart_rate": [{"value": 72, "unit": "BPM", "date": "2026-01-01"}]}
        summaries = summarize_readings(readings)
        self.assertEqual(len(summaries), 1)
        self.assertIn("72", summaries[0])

    def test_summarize_multiple_readings(self):
        readings = {
            "heart_rate": [
                {"value": 70, "unit": "BPM", "date": "2026-01-01"},
                {"value": 80, "unit": "BPM", "date": "2026-01-01"},
            ]
        }
        summaries = summarize_readings(readings)
        self.assertEqual(len(summaries), 1)
        self.assertIn("avg", summaries[0])

    def test_summarize_sleep(self):
        readings = {
            "sleep": [
                {"stage": "deep", "duration_min": 90, "start": "2026-01-01"},
                {"stage": "rem", "duration_min": 60, "start": "2026-01-01"},
                {"stage": "core", "duration_min": 180, "start": "2026-01-01"},
                {"stage": "awake", "duration_min": 20, "start": "2026-01-01"},
            ]
        }
        summaries = summarize_readings(readings)
        self.assertEqual(len(summaries), 1)
        self.assertIn("Sleep", summaries[0])

    def test_check_alerts_normal_readings(self):
        readings = {
            "heart_rate": [{"value": 75, "unit": "BPM"}],
        }
        alerts = check_alerts(readings)
        self.assertEqual(len(alerts), 0)

    def test_check_alerts_high_heart_rate(self):
        readings = {
            "heart_rate": [{"value": 130, "unit": "BPM"}],  # > 120 threshold
        }
        alerts = check_alerts(readings)
        self.assertEqual(len(alerts), 1)
        self.assertIn("HIGH", alerts[0])

    def test_check_alerts_low_blood_oxygen(self):
        readings = {
            "blood_oxygen": [{"value": 90, "unit": "%"}],  # < 92 threshold
        }
        alerts = check_alerts(readings)
        self.assertEqual(len(alerts), 1)
        self.assertIn("LOW", alerts[0])

    def test_check_alerts_normal_bp(self):
        readings = {
            "blood_pressure_sys": [{"value": 120, "unit": "mmHg"}],
            "blood_pressure_dia": [{"value": 80, "unit": "mmHg"}],
        }
        alerts = check_alerts(readings)
        self.assertEqual(len(alerts), 0)

    def test_summarize_empty_readings(self):
        summaries = summarize_readings({})
        self.assertEqual(summaries, [])

    def test_summarize_skips_empty_entries(self):
        readings = {"heart_rate": []}
        summaries = summarize_readings(readings)
        self.assertEqual(summaries, [])


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_ingest_sends_alert_on_out_of_range(self):
        """ingest() should send DM alert when readings exceed thresholds."""
        health_data = {
            "period_hours": 24,
            "start": "2026-01-01",
            "end": "2026-01-02",
            "readings": {
                "heart_rate": [{"value": 130, "unit": "BPM", "date": "2026-01-02"}],
            }
        }
        dm_calls = []
        with patch.object(_mod, "read_health_data", return_value=health_data):
            with patch.object(_mod, "vector_remember"):
                with patch.object(_mod, "vector_remember_async"):
                    with patch.object(_mod, "slack_dm", side_effect=lambda m: dm_calls.append(m)):
                        with patch.object(_mod, "load_state",
                                          return_value={"last_ingest": "", "last_alert_date": ""}):
                            with patch.object(_mod, "save_state"):
                                _mod.ingest()
        self.assertGreater(len(dm_calls), 0, "Alert should fire for out-of-range heart rate")

    def test_ingest_no_duplicate_alert_same_day(self):
        """ingest() should not send a second DM alert on the same day."""
        from datetime import date
        today = date.today().isoformat()
        health_data = {
            "period_hours": 24,
            "start": today,
            "end": today,
            "readings": {
                "heart_rate": [{"value": 130, "unit": "BPM", "date": today}],
            }
        }
        dm_calls = []
        with patch.object(_mod, "read_health_data", return_value=health_data):
            with patch.object(_mod, "vector_remember"):
                with patch.object(_mod, "vector_remember_async"):
                    with patch.object(_mod, "slack_dm", side_effect=lambda m: dm_calls.append(m)):
                        with patch.object(_mod, "load_state",
                                          return_value={"last_ingest": "", "last_alert_date": today}):
                            with patch.object(_mod, "save_state"):
                                _mod.ingest()
        self.assertEqual(len(dm_calls), 0, "Should not send duplicate alert same day")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_ingest_returns_on_no_data(self):
        """ingest() should return gracefully when no health data is available."""
        with patch.object(_mod, "read_health_data", return_value=None):
            try:
                _mod.ingest()
            except Exception as e:
                self.fail(f"ingest() raised with no data: {e}")

    def test_ingest_calls_vector_remember(self):
        """ingest() should store health summary in vector memory."""
        from datetime import date
        today = date.today().isoformat()
        health_data = {
            "period_hours": 24,
            "start": today,
            "end": today,
            "readings": {
                "heart_rate": [{"value": 72, "unit": "BPM", "date": today}],
            }
        }
        remember_calls = []
        with patch.object(_mod, "read_health_data", return_value=health_data):
            with patch.object(_mod, "vector_remember",
                              side_effect=lambda t, m=None: remember_calls.append(t)):
                with patch.object(_mod, "vector_remember_async"):
                    with patch.object(_mod, "slack_dm"):
                        with patch.object(_mod, "load_state",
                                          return_value={"last_ingest": "", "last_alert_date": ""}):
                            with patch.object(_mod, "save_state"):
                                _mod.ingest()
        self.assertGreater(len(remember_calls), 0, "Health summary should be stored in vector memory")

    def test_state_file_path_is_json(self):
        self.assertTrue(str(_mod.STATE_FILE).endswith(".json"))


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

    def test_module_has_ingest(self):
        self.assertTrue(callable(_mod.ingest))

    def test_alert_thresholds_not_empty(self):
        self.assertGreater(len(ALERT_THRESHOLDS), 0)

    def test_state_file_under_home(self):
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
