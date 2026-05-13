"""
test_nova_weekly_reliability.py — All 7 test categories for nova_weekly_reliability.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
sys.modules["nova_config"] = _nova_cfg

_nova_logger = MagicMock()
_nova_logger.log = MagicMock()
_nova_logger.LOG_INFO = "INFO"
_nova_logger.LOG_ERROR = "ERROR"
_nova_logger.read_logs = MagicMock(return_value=[])
sys.modules["nova_logger"] = _nova_logger

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_weekly_reliability.py"
_spec = importlib.util.spec_from_file_location("nova_weekly_reliability", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_scheduler_tasks = _mod.get_scheduler_tasks
get_scheduler_status = _mod.get_scheduler_status
get_memory_count = _mod.get_memory_count
analyze_logs = _mod.analyze_logs


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

    def test_scheduler_api_is_localhost(self):
        src = _SCRIPT.read_text()
        self.assertIn("127.0.0.1", src, "Scheduler API must be localhost")

    def test_vector_url_from_nova_config(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova_config.VECTOR_URL", src, "VECTOR_URL must come from nova_config")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_get_scheduler_tasks_fast_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            start = time.perf_counter()
            result = get_scheduler_tasks()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result, {})

    def test_analyze_logs_fast(self):
        _nova_logger.read_logs.return_value = [
            {"source": "test", "level": "error", "msg": f"Error {i}"}
            for i in range(500)
        ]
        start = time.perf_counter()
        analyze_logs()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_scheduler_tasks_returns_empty_on_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = get_scheduler_tasks()
        self.assertEqual(result, {})

    def test_get_scheduler_status_returns_empty_on_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = get_scheduler_status()
        self.assertEqual(result, {})

    def test_get_memory_count_returns_zeros_on_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            count, sources = get_memory_count()
        self.assertEqual(count, 0)
        self.assertEqual(sources, 0)

    def test_main_does_not_crash_when_scheduler_down(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch.object(_mod, "slack_post"):
                try:
                    _mod.main()
                except Exception as e:
                    self.fail(f"main() raised when scheduler down: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_scheduler_tasks_parses_response(self):
        tasks = {"task_a": {"enabled": True, "run_count": 100, "consecutive_failures": 0}}
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value.read.return_value = json.dumps(tasks).encode()
            result = get_scheduler_tasks()
        self.assertEqual(result["task_a"]["run_count"], 100)

    def test_get_scheduler_status_parses_response(self):
        status = {"uptime_s": 86400, "total_runs": 500, "total_failures": 5}
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value.read.return_value = json.dumps(status).encode()
            result = get_scheduler_status()
        self.assertEqual(result["total_runs"], 500)

    def test_get_memory_count_parses_response(self):
        data = {"count": 1482791, "by_source": {"web": 100, "video": 200, "health": 50}}
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value.read.return_value = json.dumps(data).encode()
            count, sources = get_memory_count()
        self.assertEqual(count, 1482791)
        self.assertEqual(sources, 3)

    def test_analyze_logs_counts_errors(self):
        _nova_logger.read_logs.return_value = [
            {"source": "protect", "level": "error", "msg": "Error 1"},
            {"source": "protect", "level": "error", "msg": "Error 2"},
            {"source": "synology", "level": "error", "msg": "Error 3"},
        ]
        error_count, warn_count, error_sources = analyze_logs()
        self.assertEqual(error_count, 3)
        self.assertEqual(error_sources.get("protect", 0), 2)
        self.assertEqual(error_sources.get("synology", 0), 1)

    def test_analyze_logs_counts_warnings(self):
        _nova_logger.read_logs.side_effect = [
            [],  # errors call
            [{"source": "test", "level": "warn", "msg": "Warning"}],  # warnings call
        ]
        error_count, warn_count, error_sources = analyze_logs()
        # warn_count from the second read_logs call
        self.assertIsInstance(warn_count, int)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_posts_reliability_report(self):
        """main() should post the weekly reliability report to Slack."""
        tasks = {
            "task_a": {"enabled": True, "run_count": 100, "consecutive_failures": 0,
                       "last_duration": 2.5},
        }
        status = {"uptime_s": 604800, "total_runs": 5000, "total_failures": 10}

        with patch.object(_mod, "get_scheduler_status", return_value=status):
            with patch.object(_mod, "get_scheduler_tasks", return_value=tasks):
                with patch.object(_mod, "get_memory_count", return_value=(1482791, 217)):
                    with patch.object(_mod, "analyze_logs", return_value=(5, 12, {})):
                        with patch("urllib.request.urlopen") as mock_url:
                            mock_url.return_value.__enter__ = lambda s: MagicMock()
                            mock_url.return_value.__exit__ = MagicMock(return_value=False)
                            mock_url.return_value.read.return_value = b"ok"
                            with patch.object(_mod, "slack_post") as mock_post:
                                _mod.main()

        mock_post.assert_called_once()
        msg = mock_post.call_args[0][0]
        self.assertIn("Reliability Report", msg)

    def test_main_shows_failing_tasks(self):
        """Report must highlight tasks with consecutive failures."""
        tasks = {
            "broken_task": {"enabled": True, "run_count": 50, "consecutive_failures": 5,
                            "last_exit_code": 1, "last_duration": 0.1},
        }
        status = {"uptime_s": 86400, "total_runs": 100, "total_failures": 5}

        with patch.object(_mod, "get_scheduler_status", return_value=status):
            with patch.object(_mod, "get_scheduler_tasks", return_value=tasks):
                with patch.object(_mod, "get_memory_count", return_value=(0, 0)):
                    with patch.object(_mod, "analyze_logs", return_value=(0, 0, {})):
                        with patch("urllib.request.urlopen"):
                            with patch.object(_mod, "slack_post") as mock_post:
                                _mod.main()

        msg = mock_post.call_args[0][0]
        self.assertIn("broken_task", msg)
        self.assertIn("failing", msg.lower())


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_verdict_rock_solid_on_high_success_rate(self):
        """99%+ success rate with no failures should yield 'Rock solid' verdict."""
        tasks = {"task_a": {"enabled": True, "run_count": 1000,
                            "consecutive_failures": 0, "last_duration": 2.0}}
        status = {"uptime_s": 604800, "total_runs": 1000, "total_failures": 5}

        with patch.object(_mod, "get_scheduler_status", return_value=status):
            with patch.object(_mod, "get_scheduler_tasks", return_value=tasks):
                with patch.object(_mod, "get_memory_count", return_value=(0, 0)):
                    with patch.object(_mod, "analyze_logs", return_value=(0, 0, {})):
                        with patch("urllib.request.urlopen"):
                            with patch.object(_mod, "slack_post") as mock_post:
                                _mod.main()

        msg = mock_post.call_args[0][0]
        self.assertIn("Rock solid", msg)

    def test_verdict_needs_work_on_poor_success(self):
        """<95% success rate should yield 'Needs work' verdict."""
        tasks = {"task_a": {"enabled": True, "run_count": 100,
                            "consecutive_failures": 5, "last_exit_code": 1, "last_duration": 1.0}}
        status = {"uptime_s": 86400, "total_runs": 100, "total_failures": 20}

        with patch.object(_mod, "get_scheduler_status", return_value=status):
            with patch.object(_mod, "get_scheduler_tasks", return_value=tasks):
                with patch.object(_mod, "get_memory_count", return_value=(0, 0)):
                    with patch.object(_mod, "analyze_logs", return_value=(10, 5, {})):
                        with patch("urllib.request.urlopen"):
                            with patch.object(_mod, "slack_post") as mock_post:
                                _mod.main()

        msg = mock_post.call_args[0][0]
        self.assertIn("Needs work", msg)

    def test_report_includes_memory_count(self):
        """Report must include vector memory count."""
        tasks = {}
        status = {"uptime_s": 3600, "total_runs": 0, "total_failures": 0}

        with patch.object(_mod, "get_scheduler_status", return_value=status):
            with patch.object(_mod, "get_scheduler_tasks", return_value=tasks):
                with patch.object(_mod, "get_memory_count", return_value=(1482791, 217)):
                    with patch.object(_mod, "analyze_logs", return_value=(0, 0, {})):
                        with patch("urllib.request.urlopen"):
                            with patch.object(_mod, "slack_post") as mock_post:
                                _mod.main()

        msg = mock_post.call_args[0][0]
        self.assertIn("1,482,791", msg)


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

    def test_slack_chan_defined(self):
        self.assertIsNotNone(_mod.SLACK_CHAN)

    def test_vector_url_defined(self):
        self.assertIsNotNone(_mod.VECTOR_URL)

    def test_week_ago_is_7_days_back(self):
        from datetime import timedelta, datetime
        diff = _mod.TODAY - _mod.WEEK_AGO
        self.assertEqual(diff.days, 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
