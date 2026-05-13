"""
test_nova_dead_mans_switch.py — All 7 test categories for nova_dead_mans_switch.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_dead_mans_switch.py"
_spec = importlib.util.spec_from_file_location("nova_dead_mans_switch", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_scheduler_tasks = _mod.get_scheduler_tasks
task_ran_today = _mod.task_ran_today
run_script = _mod.run_script
DELIVERIES = _mod.DELIVERIES


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
        """Scheduler API must be localhost — never remote."""
        self.assertIn("127.0.0.1", _mod.SCHEDULER_API,
                      "Scheduler API must be localhost")

    def test_scripts_path_uses_dunder_file(self):
        """SCRIPTS path should be relative to __file__, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("__file__", src, "Scripts path should use __file__")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_get_scheduler_tasks_has_timeout(self):
        """get_scheduler_tasks must use a timeout on urlopen."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src, "urlopen calls must have a timeout")

    def test_task_ran_today_fast(self):
        import time
        tasks = {"morning_brief": {"last_run": time.time(), "last_exit_code": 0}}
        start = time.perf_counter()
        for _ in range(1000):
            task_ran_today(tasks, "morning_brief")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_deliveries_list_bounded(self):
        """DELIVERIES should not be excessively long."""
        self.assertLessEqual(len(DELIVERIES), 50)

    def test_run_script_has_timeout(self):
        """run_script must pass timeout= to subprocess.run."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src, "run_script must use subprocess timeout")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_scheduler_tasks_returns_empty_on_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = get_scheduler_tasks()
        self.assertEqual(result, {})

    def test_run_script_returns_false_on_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("python3", 120)):
            result = run_script(Path("/tmp/nonexistent.py"))
        self.assertFalse(result)

    def test_run_script_returns_false_on_exception(self):
        with patch("subprocess.run", side_effect=Exception("crash")):
            result = run_script(Path("/tmp/nonexistent.py"))
        self.assertFalse(result)

    def test_main_skips_when_scheduler_unreachable(self):
        """main() must gracefully handle scheduler being down."""
        with patch.object(_mod, "get_scheduler_tasks", return_value={}):
            with patch.object(_mod, "slack_post") as mock_slack:
                _mod.main()
        mock_slack.assert_not_called()


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_task_ran_today_success(self):
        tasks = {
            "morning_brief": {
                "last_run": time.time() - 3600,  # 1 hour ago = today
                "last_exit_code": 0
            }
        }
        self.assertTrue(task_ran_today(tasks, "morning_brief"))

    def test_task_ran_today_failed(self):
        tasks = {
            "morning_brief": {
                "last_run": time.time() - 3600,
                "last_exit_code": 1  # failed
            }
        }
        self.assertFalse(task_ran_today(tasks, "morning_brief"))

    def test_task_ran_today_never_ran(self):
        tasks = {"morning_brief": {"last_run": 0}}
        self.assertFalse(task_ran_today(tasks, "morning_brief"))

    def test_task_ran_today_missing_key(self):
        tasks = {}
        self.assertFalse(task_ran_today(tasks, "nonexistent"))

    def test_task_ran_today_yesterday(self):
        yesterday_ts = (datetime.now() - timedelta(days=1)).timestamp()
        tasks = {"old_task": {"last_run": yesterday_ts, "last_exit_code": 0}}
        self.assertFalse(task_ran_today(tasks, "old_task"))

    def test_run_script_returns_true_on_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_script(Path("/tmp/fake.py"))
        self.assertTrue(result)

    def test_run_script_returns_false_on_nonzero(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = run_script(Path("/tmp/fake.py"))
        self.assertFalse(result)

    def test_deliveries_have_required_fields(self):
        for entry in DELIVERIES:
            task_id, script_path, min_hour, label = entry
            self.assertIsInstance(task_id, str)
            self.assertIsInstance(script_path, Path)
            self.assertIsInstance(min_hour, int)
            self.assertIsInstance(label, str)
            self.assertGreaterEqual(min_hour, 0)
            self.assertLessEqual(min_hour, 23)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_alerts_on_missed_delivery(self):
        """When a task is missing, main() should run it and post to Slack."""
        tasks = {
            "morning_brief": {"last_run": 0, "last_exit_code": -1},
            "mail_deliver_am": {"last_run": 0, "last_exit_code": -1},
            "mail_deliver_pm": {"last_run": 0, "last_exit_code": -1},
        }
        slack_calls = []

        with patch.object(_mod, "get_scheduler_tasks", return_value=tasks):
            with patch.object(_mod, "run_script", return_value=True):
                with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
                    with patch.object(_mod, "NOW_HOUR", 23):  # past all min_hours
                        _mod.main()

        self.assertGreater(len(slack_calls), 0, "Expected Slack alert for missed deliveries")
        msg = slack_calls[0]
        self.assertIn("Missed", msg)

    def test_main_no_alert_when_all_ran(self):
        """When all tasks ran today successfully, no alert should be sent."""
        now_ts = time.time()
        tasks = {
            "morning_brief": {"last_run": now_ts - 3600, "last_exit_code": 0},
            "mail_deliver_am": {"last_run": now_ts - 1800, "last_exit_code": 0},
            "mail_deliver_pm": {"last_run": now_ts - 900, "last_exit_code": 0},
        }
        slack_calls = []

        with patch.object(_mod, "get_scheduler_tasks", return_value=tasks):
            with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
                with patch.object(_mod, "NOW_HOUR", 23):
                    _mod.main()

        self.assertEqual(len(slack_calls), 0, "No alert when all deliveries confirmed")

    def test_main_skips_delivery_too_early(self):
        """Deliveries should be skipped if it's too early."""
        tasks = {
            "morning_brief": {"last_run": 0, "last_exit_code": -1},
            "mail_deliver_am": {"last_run": 0, "last_exit_code": -1},
            "mail_deliver_pm": {"last_run": 0, "last_exit_code": -1},
        }
        run_calls = []

        with patch.object(_mod, "get_scheduler_tasks", return_value=tasks):
            with patch.object(_mod, "run_script", side_effect=lambda p: run_calls.append(p) or True):
                with patch.object(_mod, "slack_post"):
                    with patch.object(_mod, "NOW_HOUR", 1):  # 1am — before any min_hour
                        _mod.main()

        self.assertEqual(len(run_calls), 0, "No scripts should run when it's too early")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_slack_shows_checkmark_on_success(self):
        tasks = {
            "morning_brief": {"last_run": 0, "last_exit_code": -1},
            "mail_deliver_am": {"last_run": time.time(), "last_exit_code": 0},
            "mail_deliver_pm": {"last_run": time.time(), "last_exit_code": 0},
        }
        slack_calls = []

        with patch.object(_mod, "get_scheduler_tasks", return_value=tasks):
            with patch.object(_mod, "run_script", return_value=True):
                with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
                    with patch.object(_mod, "NOW_HOUR", 23):
                        _mod.main()

        self.assertGreater(len(slack_calls), 0)
        msg = slack_calls[0]
        self.assertIn("✅", msg, "Successful recovery should show ✅")

    def test_slack_shows_x_on_run_failure(self):
        tasks = {"morning_brief": {"last_run": 0, "last_exit_code": -1},
                 "mail_deliver_am": {"last_run": time.time(), "last_exit_code": 0},
                 "mail_deliver_pm": {"last_run": time.time(), "last_exit_code": 0},
                 }
        slack_calls = []

        with patch.object(_mod, "get_scheduler_tasks", return_value=tasks):
            with patch.object(_mod, "run_script", return_value=False):  # script fails
                with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
                    with patch.object(_mod, "NOW_HOUR", 23):
                        _mod.main()

        self.assertGreater(len(slack_calls), 0)
        msg = slack_calls[0]
        self.assertIn("❌", msg, "Failed run should show ❌")


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

    def test_deliveries_not_empty(self):
        self.assertGreater(len(DELIVERIES), 0)

    def test_scheduler_api_is_http(self):
        self.assertTrue(_mod.SCHEDULER_API.startswith("http"))

    def test_module_has_main(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
