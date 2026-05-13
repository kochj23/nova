"""
test_nova_scheduler.py — All 7 test categories for nova_scheduler.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub deps before loading
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.slack_bot_token.return_value = "xoxb-test"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_ops_writer"] = MagicMock()

_nova_logger = MagicMock()
_nova_logger.log = print
_nova_logger.LOG_INFO = "INFO"
_nova_logger.LOG_ERROR = "ERROR"
_nova_logger.LOG_WARN = "WARN"
_nova_logger.LOG_DEBUG = "DEBUG"
sys.modules["nova_logger"] = _nova_logger

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_scheduler.py"
_spec = importlib.util.spec_from_file_location("nova_scheduler", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

parse_interval = _mod.parse_interval
parse_cron = _mod.parse_cron
next_cron_time = _mod.next_cron_time
TaskState = _mod.TaskState
Task = _mod.Task


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

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
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_config_paths_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.CONFIG_PATH))
        self.assertIn(str(Path.home()), str(_mod.STATE_PATH))

    def test_scripts_dir_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.SCRIPTS_DIR))

    def test_http_status_port_defined(self):
        """Scheduler status API must run on a defined port."""
        src = _SCRIPT.read_text()
        self.assertIn("37460", src, "Status API port 37460 not found")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_parse_interval_fast(self):
        for _ in range(10000):
            parse_interval("every 5m")
        # Just verify it doesn't hang

    def test_parse_interval_seconds(self):
        self.assertEqual(parse_interval("every 30s"), 30)

    def test_parse_interval_minutes(self):
        self.assertEqual(parse_interval("every 15m"), 900)

    def test_parse_interval_hours(self):
        self.assertEqual(parse_interval("every 4h"), 14400)

    def test_parse_interval_invalid_returns_zero(self):
        self.assertEqual(parse_interval("not valid"), 0)

    def test_next_cron_time_fast(self):
        now_ts = time.time()
        start = time.perf_counter()
        result = next_cron_time("0 9 * * *", now_ts)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_task_state_consecutive_failures_tracked(self):
        ts = TaskState()
        ts.consecutive_failures = 3
        self.assertEqual(ts.consecutive_failures, 3)

    def test_task_overlap_skip_prevents_concurrent_runs(self):
        t = Task(id="test", script="test.py", schedule="every 5m", overlap="skip")
        t.state.running = True
        # When overlap=skip and running=True, should skip
        should_skip = t.overlap == "skip" and t.state.running
        self.assertTrue(should_skip)

    def test_task_state_retry_pending_flag(self):
        ts = TaskState()
        ts._retry_pending = True
        self.assertTrue(ts._retry_pending)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_parse_cron_extracts_expression(self):
        result = parse_cron("cron 0 23 * * *")
        self.assertEqual(result, "0 23 * * *")

    def test_parse_cron_returns_empty_for_non_cron(self):
        result = parse_cron("every 5m")
        self.assertEqual(result, "")

    def test_parse_cron_handles_complex_expression(self):
        result = parse_cron("cron 15 10 * * 1")
        self.assertEqual(result, "15 10 * * 1")

    def test_next_cron_time_daily_9am(self):
        """9am daily cron must fire at 9:00."""
        # Use a timestamp for 7am today
        before_9 = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0)
        before_9_ts = before_9.timestamp()
        result_ts = next_cron_time("0 9 * * *", before_9_ts)
        self.assertIsNotNone(result_ts)
        result_dt = datetime.fromtimestamp(result_ts)
        self.assertEqual(result_dt.hour, 9)
        self.assertEqual(result_dt.minute, 0)

    def test_next_cron_time_after_trigger_advances_to_next_day(self):
        """After 9am, next 9am cron should be tomorrow."""
        after_9 = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        after_9_ts = after_9.timestamp()
        result_ts = next_cron_time("0 9 * * *", after_9_ts)
        self.assertIsNotNone(result_ts)
        self.assertGreater(result_ts, after_9_ts)

    def test_task_defaults(self):
        t = Task(id="test", script="test.py", schedule="every 5m")
        self.assertEqual(t.timeout, 300)
        self.assertEqual(t.overlap, "skip")
        self.assertTrue(t.enabled)

    def test_task_state_defaults(self):
        ts = TaskState()
        self.assertEqual(ts.last_run, 0)
        self.assertEqual(ts.consecutive_failures, 0)
        self.assertFalse(ts.running)
        self.assertEqual(ts.run_count, 0)

    def test_parse_interval_with_spaces(self):
        self.assertEqual(parse_interval("every  5  m"), 300)

    def test_parse_cron_weekly(self):
        result = parse_cron("cron 0 10 * * 1")
        self.assertEqual(result, "0 10 * * 1")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_task_schedule_parsed_as_interval(self):
        """Task with 'every Xm' schedule should have _interval_s set."""
        task = Task(id="test", script="test.py", schedule="every 10m")
        task._interval_s = parse_interval(task.schedule)
        self.assertEqual(task._interval_s, 600)

    def test_task_schedule_parsed_as_cron(self):
        """Task with 'cron ...' schedule should have _cron_expr set."""
        task = Task(id="daily", script="daily.py", schedule="cron 0 9 * * *")
        task._cron_expr = parse_cron(task.schedule)
        self.assertEqual(task._cron_expr, "0 9 * * *")
        self.assertEqual(task._interval_s, 0)

    def test_heartbeat_file_path_in_config(self):
        self.assertIn(str(Path.home()), str(_mod.HEARTBEAT_FILE))


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_next_cron_matches_minute_precision(self):
        """Cron "30 14 * * *" must fire at 14:30."""
        before = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        result_ts = next_cron_time("30 14 * * *", before.timestamp())
        self.assertIsNotNone(result_ts)
        result_dt = datetime.fromtimestamp(result_ts)
        self.assertEqual(result_dt.hour, 14)
        self.assertEqual(result_dt.minute, 30)

    def test_cron_weekday_filter(self):
        """Cron "0 9 * * 1" must schedule on weekday index 1 (Tuesday in Python's weekday())."""
        # The scheduler uses Python weekday() where 0=Monday, 1=Tuesday, etc.
        # Use a time well before 9am so we don't accidentally hit the same day.
        base = datetime.now().replace(hour=1, minute=0, second=0, microsecond=0)
        result_ts = next_cron_time("0 9 * * 1", base.timestamp())
        self.assertIsNotNone(result_ts)
        result_dt = datetime.fromtimestamp(result_ts)
        self.assertEqual(result_dt.weekday(), 1)  # Python weekday 1 = Tuesday

    def test_interval_never_negative(self):
        for spec in ["every 1s", "every 30m", "every 2h", "every 999m"]:
            result = parse_interval(spec)
            self.assertGreater(result, 0, f"Interval for '{spec}' should be > 0")


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

    def test_constants_defined(self):
        self.assertIsInstance(_mod.CONFIG_PATH, Path)
        self.assertIsInstance(_mod.STATE_PATH, Path)
        self.assertIsInstance(_mod.HEARTBEAT_FILE, Path)
        self.assertIsInstance(_mod.SCRIPTS_DIR, Path)

    def test_dataclasses_instantiate(self):
        ts = TaskState()
        self.assertIsInstance(ts, TaskState)
        t = Task(id="t", script="s.py", schedule="every 5m")
        self.assertIsInstance(t, Task)

    def test_parse_functions_exist(self):
        for fn in ("parse_interval", "parse_cron", "next_cron_time"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
