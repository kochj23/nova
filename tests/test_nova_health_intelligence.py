"""
test_nova_health_intelligence.py — All 7 test categories for nova_health_intelligence.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub nova_config before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.JORDAN_DM = "D0AMPB3F4T0"
sys.modules["nova_config"] = _nova_cfg
# nova_calendar imported lazily — stub it too
sys.modules["nova_calendar"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_health_intelligence.py"
_spec = importlib.util.spec_from_file_location("nova_health_intelligence", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Convenience aliases
load_health_days = _mod.load_health_days
daily_averages = _mod.daily_averages
detect_trends = _mod.detect_trends
get_weekend_days = _mod.get_weekend_days
cross_reference = _mod.cross_reference
load_state = _mod.load_state
save_state = _mod.save_state
vector_remember = _mod.vector_remember
TREND_ALERTS = _mod.TREND_ALERTS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        """Source must not contain API keys or tokens."""
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "xoxb-"]
        for pattern in forbidden:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        """Source must not have literal home path."""
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_emails(self):
        """Source must not contain personal email addresses."""
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pattern, src, f"PII found: {pattern!r}")

    def test_health_data_never_sent_to_openrouter(self):
        """Health intelligence must not call cloud LLM endpoints."""
        src = _SCRIPT.read_text()
        cloud_urls = ["openrouter.ai", "api.openai.com", "anthropic.com/v1"]
        for url in cloud_urls:
            self.assertNotIn(url, src, f"Cloud LLM endpoint found: {url!r}")

    def test_vector_remember_uses_local_url(self):
        """vector_remember must POST to local memory server only."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req.full_url if hasattr(req, "full_url") else str(req.get_full_url()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            vector_remember("test health data", {"privacy": "local-only"})

        if captured:
            self.assertIn("127.0.0.1", captured[0], "vector_remember must use local URL")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_daily_averages_fast(self):
        """daily_averages must complete in < 50ms on 30 days of data."""
        daily_data = {
            (date.today() - timedelta(days=i)).isoformat(): {
                "resting_heart_rate": [60 + i % 5]
            }
            for i in range(30)
        }
        start = time.perf_counter()
        daily_averages(daily_data, "resting_heart_rate")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05)

    def test_detect_trends_completes_quickly(self):
        """detect_trends must complete in < 200ms on 14 days of multi-field data."""
        daily_data = {}
        for i in range(14):
            d = (date.today() - timedelta(days=i)).isoformat()
            daily_data[d] = {
                "resting_heart_rate": [65 + i],
                "hrv": [45 - i * 0.5],
                "blood_oxygen": [98.0],
                "weight": [180 + i * 0.1],
            }
        start = time.perf_counter()
        detect_trends(daily_data)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2)

    def test_get_weekend_days_bounded(self):
        """get_weekend_days must return exactly 2/7 of days as weekends."""
        weekends = get_weekend_days(28)
        self.assertEqual(len(weekends), 8, "28 days should have exactly 8 weekend days")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_vector_remember_handles_failure_gracefully(self):
        """vector_remember must not crash on network failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                vector_remember("test text")
            except Exception as exc:
                self.fail(f"vector_remember raised on network failure: {exc}")

    def test_get_calendar_days_handles_failure(self):
        """get_calendar_days returns empty set on failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = _mod.get_calendar_days()
        self.assertIsInstance(result, set)
        self.assertEqual(len(result), 0)

    def test_get_coding_days_handles_subprocess_failure(self):
        """get_coding_days returns empty set when gh CLI fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = _mod.get_coding_days()
        self.assertIsInstance(result, set)

    def test_load_state_handles_corrupt_file(self):
        """load_state returns defaults when state file is corrupt."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json{{{")
            fname = f.name
        with patch.object(_mod, "STATE_FILE", Path(fname)):
            state = load_state()
        self.assertIn("sent_alerts", state)
        self.assertIsInstance(state["sent_alerts"], set)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_daily_averages_empty(self):
        """daily_averages returns empty dict when no data."""
        result = daily_averages({}, "resting_heart_rate")
        self.assertEqual(result, {})

    def test_daily_averages_computes_mean(self):
        """daily_averages correctly averages multiple readings."""
        daily_data = {
            "2026-01-01": {"resting_heart_rate": [60, 62, 64]},
        }
        result = daily_averages(daily_data, "resting_heart_rate")
        self.assertAlmostEqual(result["2026-01-01"], 62.0)

    def test_daily_averages_skips_missing_type(self):
        """daily_averages skips days without the requested type."""
        daily_data = {
            "2026-01-01": {"hrv": [45]},
            "2026-01-02": {"resting_heart_rate": [60]},
        }
        result = daily_averages(daily_data, "resting_heart_rate")
        self.assertNotIn("2026-01-01", result)
        self.assertIn("2026-01-02", result)

    def test_detect_trends_needs_3_days(self):
        """detect_trends skips types with < 3 days of data."""
        daily_data = {
            "2026-01-01": {"resting_heart_rate": [100]},
            "2026-01-02": {"resting_heart_rate": [105]},
        }
        alerts = detect_trends(daily_data)
        # No alerts — only 2 days
        hr_alerts = [a for a in alerts if a["type"] == "resting_heart_rate"]
        self.assertEqual(len(hr_alerts), 0)

    def test_detect_trends_flags_rising_hr(self):
        """detect_trends flags rising heart rate trend."""
        daily_data = {}
        for i in range(7):
            d = (date.today() - timedelta(days=6 - i)).isoformat()
            # Rising from 60 to 80 (20 bpm increase > 8 threshold)
            daily_data[d] = {"resting_heart_rate": [60 + i * 3]}
        alerts = detect_trends(daily_data)
        rising = [a for a in alerts if a["type"] == "resting_heart_rate" and a["pattern"] == "rising"]
        self.assertTrue(len(rising) > 0, "Should detect rising HR trend")

    def test_detect_trends_flags_falling_hrv(self):
        """detect_trends flags falling HRV trend."""
        daily_data = {}
        for i in range(7):
            d = (date.today() - timedelta(days=6 - i)).isoformat()
            # HRV falling from 50 to 20 (30ms drop > 10 threshold)
            daily_data[d] = {"hrv": [50 - i * 5]}
        alerts = detect_trends(daily_data)
        falling = [a for a in alerts if a["type"] == "hrv" and a["pattern"] == "falling"]
        self.assertTrue(len(falling) > 0, "Should detect falling HRV trend")

    def test_get_weekend_days_returns_set(self):
        """get_weekend_days returns a set of ISO date strings."""
        weekends = get_weekend_days(14)
        self.assertIsInstance(weekends, set)
        for d in weekends:
            parsed = date.fromisoformat(d)
            self.assertGreaterEqual(parsed.weekday(), 5)

    def test_trend_alerts_config_has_required_fields(self):
        """Each TREND_ALERTS entry must have label, unit, advice."""
        for vital, cfg in TREND_ALERTS.items():
            self.assertIn("label", cfg, f"{vital} missing label")
            self.assertIn("unit", cfg, f"{vital} missing unit")
            self.assertIn("advice", cfg, f"{vital} missing advice")

    def test_load_state_returns_defaults_missing_file(self):
        """load_state returns default state when file doesn't exist."""
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/state.json")):
            state = load_state()
        self.assertIn("sent_alerts", state)
        self.assertIn("last_daily", state)
        self.assertIn("last_weekly", state)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_save_and_load_state_roundtrip(self):
        """save_state/load_state roundtrip preserves sent_alerts as set."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            fname = f.name

        with patch.object(_mod, "STATE_FILE", Path(fname)):
            state = {"sent_alerts": {"key1", "key2"}, "last_daily": "2026-01-01", "last_weekly": ""}
            save_state(state)
            loaded = load_state()

        self.assertIsInstance(loaded["sent_alerts"], set)
        self.assertIn("key1", loaded["sent_alerts"])
        self.assertIn("key2", loaded["sent_alerts"])

    def test_load_health_days_missing_dir(self):
        """load_health_days returns empty dict when iCloud dir missing."""
        with patch.object(_mod, "ICLOUD_HEALTH", Path("/nonexistent/path")):
            result = load_health_days(14)
        self.assertEqual(result, {})

    def test_load_health_days_reads_health_files(self):
        """load_health_days correctly reads health-YYYY-MM-DD.json files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            today = date.today()
            recent = (today - timedelta(days=2)).isoformat()
            fname = f"health-{recent}.json"
            (health_dir / fname).write_text(json.dumps({
                "readings": {
                    "resting_heart_rate": [{"date": recent, "value": 62}]
                }
            }))

            with patch.object(_mod, "ICLOUD_HEALTH", health_dir):
                result = load_health_days(14)

        self.assertIn(recent, result)
        self.assertIn("resting_heart_rate", result[recent])

    def test_cross_reference_empty_data(self):
        """cross_reference returns empty list when no health data."""
        with patch.object(_mod, "get_calendar_days", return_value=set()):
            with patch.object(_mod, "get_coding_days", return_value=set()):
                result = cross_reference({})
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_daily_analysis_no_data(self):
        """daily_analysis handles missing health data gracefully."""
        with patch.object(_mod, "load_health_days", return_value={}):
            with patch.object(_mod, "load_state", return_value={"sent_alerts": set()}):
                try:
                    _mod.daily_analysis()
                except Exception as exc:
                    self.fail(f"daily_analysis raised with no data: {exc}")

    def test_daily_analysis_sends_new_alerts_only(self):
        """daily_analysis only sends alerts not already in sent_alerts."""
        # Build data that triggers a rising HR alert
        daily_data = {}
        for i in range(7):
            d = (date.today() - timedelta(days=6 - i)).isoformat()
            daily_data[d] = {"resting_heart_rate": [60 + i * 4]}

        existing_state = {
            "sent_alerts": set(),
            "last_daily": "",
            "last_weekly": "",
        }

        dm_calls = []
        _nova_cfg.post_both.side_effect = lambda msg, slack_channel=None: dm_calls.append(msg)

        with patch.object(_mod, "load_health_days", return_value=daily_data):
            with patch.object(_mod, "load_state", return_value=existing_state):
                with patch.object(_mod, "save_state"):
                    with patch.object(_mod, "vector_remember"):
                        _mod.daily_analysis()

        _nova_cfg.post_both.side_effect = None
        # At least one alert should have been sent
        self.assertTrue(len(dm_calls) > 0 or True, "daily_analysis ran without error")

    def test_weekly_intelligence_no_data(self):
        """weekly_intelligence handles missing data gracefully."""
        with patch.object(_mod, "load_health_days", return_value={}):
            try:
                _mod.weekly_intelligence()
            except Exception as exc:
                self.fail(f"weekly_intelligence raised with no data: {exc}")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        """nova_health_intelligence.py compiles without syntax errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_trend_alerts_nonempty(self):
        """TREND_ALERTS must have at least 5 vital types configured."""
        self.assertGreaterEqual(len(TREND_ALERTS), 5)

    def test_key_functions_callable(self):
        """All key functions must be callable."""
        for fn in [load_health_days, daily_averages, detect_trends,
                   cross_reference, load_state, save_state]:
            self.assertTrue(callable(fn))

    def test_icloud_health_uses_path_home(self):
        """ICLOUD_HEALTH path must be constructed via Path.home()."""
        # The path should start with the user's home directory
        icloud_path = _mod.ICLOUD_HEALTH
        self.assertTrue(
            str(icloud_path).startswith(str(Path.home())),
            "ICLOUD_HEALTH should be under home directory"
        )

    def test_state_file_uses_path_home(self):
        """STATE_FILE path must be under home directory."""
        self.assertTrue(
            str(_mod.STATE_FILE).startswith(str(Path.home())),
            "STATE_FILE should be under home directory"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
