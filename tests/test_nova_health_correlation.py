"""
test_nova_health_correlation.py — All 7 test categories for nova_health_correlation.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub nova_config before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_API = "https://slack.com/api"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_health_correlation.py"
_spec = importlib.util.spec_from_file_location("nova_health_correlation", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Convenience aliases
load_health_data = _mod.load_health_data
_recall = _mod._recall
_safe_avg = _mod._safe_avg
_classify_day = _mod._classify_day
correlate_sleep_vs_meetings = _mod.correlate_sleep_vs_meetings
correlate_hr_vs_meetings = _mod.correlate_hr_vs_meetings
correlate_hrv_weekday_weekend = _mod.correlate_hrv_weekday_weekend
correlate_steps_vs_coding = _mod.correlate_steps_vs_coding
correlate_energy_vs_events = _mod.correlate_energy_vs_events
compute_summaries = _mod.compute_summaries
store_insights = _mod.store_insights
generate_report = _mod.generate_report
HEALTH_FIELDS = _mod.HEALTH_FIELDS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        """Source must not contain API keys or passwords."""
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]
        for pattern in forbidden:
            self.assertNotIn(pattern, src, f"Credential pattern found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode a literal home path."""
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src, "Hardcoded home path found — use Path.home()")

    def test_no_pii_email_in_source(self):
        """Source must not contain personal email addresses."""
        src = _SCRIPT.read_text()
        _at = "@"
        pii_patterns = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney" + ".com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]
        for pattern in pii_patterns:
            self.assertNotIn(pattern, src, f"PII email found: {pattern!r}")

    def test_health_data_stored_with_local_only_privacy(self):
        """store_insights must set privacy=local-only in metadata payload."""
        captured_payloads = []

        def fake_urlopen(req, timeout=None):
            captured_payloads.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        correlations = [{"title": "Test", "finding": "test finding"}]
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store_insights(correlations, 7)

        self.assertTrue(len(captured_payloads) > 0, "store_insights made no request")
        payload = captured_payloads[0]
        self.assertEqual(
            payload["metadata"]["privacy"], "local-only",
            "Health data not tagged privacy=local-only"
        )

    def test_health_data_source_label_is_health_correlation(self):
        """store_insights must use source='health_correlation'."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store_insights([{"title": "X", "finding": "y"}], 7)

        self.assertEqual(captured[0]["source"], "health_correlation")

    def test_health_data_never_routed_to_cloud_llm(self):
        """store_insights must only POST to local vector memory URL (127.0.0.1)."""
        src = _SCRIPT.read_text()
        # No external LLM endpoints
        cloud_patterns = ["openrouter.ai", "api.openai.com", "anthropic.com/v1"]
        for pattern in cloud_patterns:
            self.assertNotIn(pattern, src, f"Cloud LLM endpoint found: {pattern!r}")
        # Must use local vector memory
        self.assertIn("127.0.0.1", src, "Local vector URL (127.0.0.1) not found")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_safe_avg_fast(self):
        """_safe_avg must compute average of 10k values in < 50ms."""
        values = list(range(10000))
        start = time.perf_counter()
        result = _safe_avg(values)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05, f"_safe_avg too slow: {elapsed:.3f}s")
        self.assertAlmostEqual(result, 4999.5, places=0)

    def test_correlate_sleep_no_unbounded_loop(self):
        """correlate_sleep_vs_meetings must complete in < 100ms on 100 days."""
        health = {
            (date.today() - timedelta(days=i)).isoformat(): {
                "sleep_hours": 7.0 + (i % 3) * 0.5
            }
            for i in range(100)
        }
        calendar = {
            (date.today() - timedelta(days=i)).isoformat(): i % 5
            for i in range(100)
        }
        start = time.perf_counter()
        correlate_sleep_vs_meetings(health, calendar)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"Correlation too slow: {elapsed:.3f}s")

    def test_compute_summaries_performance(self):
        """compute_summaries must complete in < 100ms on 30 days."""
        health = {}
        for i in range(30):
            d = (date.today() - timedelta(days=i)).isoformat()
            health[d] = {
                "sleep_hours": 7.0,
                "resting_heart_rate": 60,
                "hrv": 45,
                "steps": 8000,
                "active_energy": 500,
            }
        start = time.perf_counter()
        compute_summaries(health)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_vector_max_n_bounded(self):
        """VECTOR_MAX_N must be <= 50 to prevent API rejection."""
        self.assertLessEqual(_mod.VECTOR_MAX_N, 50)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_recall_handles_network_failure_gracefully(self):
        """_recall must return empty list on network failure (no crash)."""
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = _recall("test query", "calendar", n=10)
        self.assertEqual(result, [], "_recall should return [] on network failure")

    def test_recall_handles_json_error_gracefully(self):
        """_recall must return empty list on JSON decode error."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _recall("test", "calendar")
        self.assertEqual(result, [])

    def test_store_insights_handles_network_failure(self):
        """store_insights must not crash on urlopen failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("network down")):
            # Should not raise
            try:
                store_insights([{"title": "X", "finding": "y"}], 7)
            except Exception as exc:
                self.fail(f"store_insights raised on network failure: {exc}")

    def test_post_to_slack_handles_failure(self):
        """post_to_slack must return False (not crash) when post_both fails."""
        _nova_cfg.post_both.side_effect = Exception("Slack down")
        result = _mod.post_to_slack("test message")
        _nova_cfg.post_both.side_effect = None
        self.assertFalse(result, "post_to_slack should return False on failure")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_safe_avg_empty(self):
        self.assertEqual(_safe_avg([]), 0.0)

    def test_safe_avg_single(self):
        self.assertAlmostEqual(_safe_avg([5.0]), 5.0)

    def test_safe_avg_multiple(self):
        self.assertAlmostEqual(_safe_avg([2.0, 4.0, 6.0]), 4.0)

    def test_classify_day_weekend(self):
        """Saturday and Sunday are 'weekend'."""
        # Find a Saturday
        today = date.today()
        days_ahead = (5 - today.weekday()) % 7
        saturday = today + timedelta(days=days_ahead)
        self.assertEqual(_classify_day(saturday.isoformat()), "weekend")

    def test_classify_day_weekday(self):
        """Monday through Friday are 'weekday'."""
        today = date.today()
        # Find a Monday
        days_ahead = (0 - today.weekday()) % 7
        monday = today + timedelta(days=days_ahead if days_ahead else 7)
        self.assertEqual(_classify_day(monday.isoformat()), "weekday")

    def test_correlate_sleep_returns_none_on_insufficient_data(self):
        """Returns None when < 2 meeting days."""
        health = {"2026-01-01": {"sleep_hours": 7.0}}
        calendar = {"2026-01-01": 3}
        result = correlate_sleep_vs_meetings(health, calendar)
        self.assertIsNone(result)

    def test_correlate_sleep_returns_none_on_small_diff(self):
        """Returns None when sleep difference < 0.3h."""
        health = {
            "2026-01-01": {"sleep_hours": 7.0},
            "2026-01-02": {"sleep_hours": 7.1},
            "2026-01-03": {"sleep_hours": 7.0},
            "2026-01-04": {"sleep_hours": 7.1},
        }
        calendar = {"2026-01-01": 5, "2026-01-02": 5, "2026-01-03": 1, "2026-01-04": 1}
        result = correlate_sleep_vs_meetings(health, calendar)
        self.assertIsNone(result)

    def test_correlate_sleep_detects_significant_diff(self):
        """Returns result dict when sleep difference >= 0.3h."""
        health = {
            "2026-01-01": {"sleep_hours": 5.5},
            "2026-01-02": {"sleep_hours": 5.5},
            "2026-01-03": {"sleep_hours": 8.5},
            "2026-01-04": {"sleep_hours": 8.5},
        }
        calendar = {"2026-01-01": 8, "2026-01-02": 8, "2026-01-03": 1, "2026-01-04": 1}
        result = correlate_sleep_vs_meetings(health, calendar)
        self.assertIsNotNone(result)
        self.assertIn("finding", result)
        self.assertIn("title", result)

    def test_correlate_hr_meetings_returns_none_no_data(self):
        """Returns None when no HR data overlaps with meeting days."""
        result = correlate_hr_vs_meetings({}, {})
        self.assertIsNone(result)

    def test_correlate_hrv_weekend_weekday(self):
        """Detects HRV difference between weekend and weekday."""
        health = {}
        # Build a week with clear weekend/weekday HRV difference
        monday = date.today() - timedelta(days=date.today().weekday())
        for i in range(7):
            d = (monday + timedelta(days=i))
            is_wknd = d.weekday() >= 5
            health[d.isoformat()] = {"hrv": 60.0 if is_wknd else 40.0}
        result = correlate_hrv_weekday_weekend(health)
        self.assertIsNotNone(result)
        self.assertGreater(result["weekend_avg"], result["weekday_avg"])

    def test_correlate_steps_coding_returns_none_empty(self):
        """Returns None when no steps data."""
        result = correlate_steps_vs_coding({}, {})
        self.assertIsNone(result)

    def test_health_fields_defined(self):
        """HEALTH_FIELDS must contain expected metrics."""
        expected = {"sleep_hours", "resting_heart_rate", "hrv", "steps", "active_energy"}
        self.assertTrue(expected.issubset(set(HEALTH_FIELDS)))

    def test_load_health_data_missing_dir(self):
        """load_health_data returns empty dict when health dir doesn't exist."""
        with patch.object(_mod, "HEALTH_DIR", Path("/nonexistent/path/xyz")):
            result = load_health_data(7)
        self.assertEqual(result, {})


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_load_health_data_reads_json_files(self):
        """load_health_data correctly loads and filters JSON health files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            # Write a file within the last 7 days
            today = date.today()
            recent = (today - timedelta(days=2)).isoformat()
            old = (today - timedelta(days=40)).isoformat()

            (health_dir / f"{recent}.json").write_text(json.dumps({
                "sleep_hours": 7.5,
                "resting_heart_rate": 58,
                "hrv": 45,
            }))
            (health_dir / f"{old}.json").write_text(json.dumps({
                "sleep_hours": 6.0,
            }))

            with patch.object(_mod, "HEALTH_DIR", health_dir):
                result = load_health_data(7)

        self.assertIn(recent, result)
        self.assertNotIn(old, result)
        self.assertEqual(result[recent]["sleep_hours"], 7.5)

    def test_load_health_data_skips_latest_json(self):
        """load_health_data must skip the 'latest' file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            (health_dir / "latest.json").write_text(json.dumps({"sleep_hours": 8.0}))

            with patch.object(_mod, "HEALTH_DIR", health_dir):
                result = load_health_data(7)

        self.assertNotIn("latest", result)

    def test_compute_summaries_handles_all_fields(self):
        """compute_summaries produces output for each field with data."""
        health = {
            "2026-01-01": {"sleep_hours": 7.0, "steps": 8000, "hrv": 45},
            "2026-01-02": {"sleep_hours": 8.0, "steps": 10000, "hrv": 50},
        }
        summaries = compute_summaries(health)
        # Should have at least 3 summaries (sleep, steps, hrv)
        self.assertGreaterEqual(len(summaries), 3)
        # All should be strings
        for s in summaries:
            self.assertIsInstance(s, str)

    def test_recall_parses_list_response(self):
        """_recall handles list response from memory server."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([
            {"text": "meeting on 2026-01-01", "metadata": {"date": "2026-01-01"}}
        ]).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _recall("meeting", "calendar")
        self.assertEqual(len(result), 1)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_generate_report_no_data(self):
        """generate_report returns appropriate message when no health data."""
        with patch.object(_mod, "load_health_data", return_value={}):
            with patch.object(_mod, "get_calendar_events_by_date", return_value={}):
                with patch.object(_mod, "get_email_volume_by_date", return_value={}):
                    with patch.object(_mod, "get_coding_activity_by_date", return_value={}):
                        result = generate_report(7)
        # generate_report returns (text, correlations) tuple, but no-data path may
        # return just a string. Handle both cases.
        if isinstance(result, tuple):
            report_text, correlations = result
        else:
            report_text = result
            correlations = []
        self.assertIn("No health data", report_text)
        self.assertEqual(correlations, [])

    def test_generate_report_weekly_label(self):
        """generate_report includes 'Weekly' for 7-day lookback."""
        health = {
            "2026-01-01": {"sleep_hours": 7.0, "resting_heart_rate": 60}
        }
        with patch.object(_mod, "load_health_data", return_value=health):
            with patch.object(_mod, "get_calendar_events_by_date", return_value={}):
                with patch.object(_mod, "get_email_volume_by_date", return_value={}):
                    with patch.object(_mod, "get_coding_activity_by_date", return_value={}):
                        result, _ = generate_report(7)
        self.assertIn("Weekly", result)

    def test_generate_report_monthly_label(self):
        """generate_report includes 'Monthly' for 30-day lookback."""
        health = {"2026-01-01": {"sleep_hours": 7.0}}
        with patch.object(_mod, "load_health_data", return_value=health):
            with patch.object(_mod, "get_calendar_events_by_date", return_value={}):
                with patch.object(_mod, "get_email_volume_by_date", return_value={}):
                    with patch.object(_mod, "get_coding_activity_by_date", return_value={}):
                        result, _ = generate_report(30)
        self.assertIn("Monthly", result)

    def test_store_insights_empty_correlations(self):
        """store_insights does nothing when correlations list is empty."""
        call_count = [0]

        def should_not_be_called(*a, **kw):
            call_count[0] += 1

        with patch("urllib.request.urlopen", side_effect=should_not_be_called):
            store_insights([], 7)

        self.assertEqual(call_count[0], 0, "store_insights should not POST with empty correlations")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        """nova_health_correlation.py compiles without syntax errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_module_constants_present(self):
        """Critical constants must be defined."""
        self.assertIsInstance(_mod.HEALTH_FIELDS, list)
        self.assertGreater(len(_mod.HEALTH_FIELDS), 0)
        self.assertIsInstance(_mod.VECTOR_RECALL, str)
        self.assertIn("127.0.0.1", _mod.VECTOR_RECALL)
        self.assertIsInstance(_mod.VECTOR_MAX_N, int)

    def test_correlation_functions_callable(self):
        """All correlation functions must be callable."""
        for fn in [correlate_sleep_vs_meetings, correlate_hr_vs_meetings,
                   correlate_hrv_weekday_weekend, correlate_steps_vs_coding,
                   correlate_energy_vs_events]:
            self.assertTrue(callable(fn))

    def test_load_health_data_returns_dict(self):
        """load_health_data always returns a dict."""
        with patch.object(_mod, "HEALTH_DIR", Path("/nonexistent")):
            result = load_health_data(7)
        self.assertIsInstance(result, dict)

    def test_safe_avg_returns_numeric(self):
        """_safe_avg always returns a numeric type."""
        self.assertIsInstance(_safe_avg([]), float)
        # statistics.mean([1,2,3]) returns int on Python 3.9, float on 3.11+
        result = _safe_avg([1, 2, 3])
        self.assertIsInstance(result, (int, float))


if __name__ == "__main__":
    unittest.main(verbosity=2)
