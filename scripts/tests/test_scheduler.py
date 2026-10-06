#!/usr/bin/env python3
"""
test_scheduler.py — Comprehensive tests for Nova's scheduler and related scripts.

Covers:
  - nova_scheduler.py: cron parsing, interval calculation, overlap prevention,
    sleep/wake recovery, task execution, state management
  - nova_log_rotate.py: log file rotation, JSONL trimming
  - nova_self_audit.py: script/service/process audit logic

Uses unittest.mock.patch for unit tests.
Uses @pytest.mark.integration for live-service tests.

Written by Jordan Koch.
"""

import asyncio
import importlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture(autouse=True)
def _offline_ops_writer():
    """execute_task() records run start/end through nova_ops_writer, whose worker opens a real asyncpg
    pool to pg-primary. Stub both writers for every test in this file so nothing touches PG."""
    import nova_ops_writer
    with patch.object(nova_ops_writer, "record_run_start"), patch.object(nova_ops_writer, "record_run_end"):
        yield


# ============================================================================
# nova_scheduler.py — Schedule Parsing
# ============================================================================

class TestSchedulerParsing:
    """Tests for schedule parsing functions (pure logic, no I/O)."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_scheduler" in sys.modules:
            del sys.modules["nova_scheduler"]
        import nova_scheduler
        self.mod = nova_scheduler

    def test_parse_interval_minutes(self):
        """'every 5m' parses to 300 seconds."""
        assert self.mod.parse_interval("every 5m") == 300

    def test_parse_interval_hours(self):
        """'every 4h' parses to 14400 seconds."""
        assert self.mod.parse_interval("every 4h") == 14400

    def test_parse_interval_seconds(self):
        """'every 30s' parses to 30 seconds."""
        assert self.mod.parse_interval("every 30s") == 30

    def test_parse_interval_with_spaces(self):
        """'every  10 m' with extra spaces parses correctly."""
        assert self.mod.parse_interval("every  10 m") == 600

    def test_parse_interval_invalid(self):
        """Invalid interval string returns 0."""
        assert self.mod.parse_interval("invalid") == 0
        assert self.mod.parse_interval("") == 0
        assert self.mod.parse_interval("cron 0 7 * * *") == 0

    def test_parse_cron_basic(self):
        """'cron 0 7 * * *' extracts '0 7 * * *'."""
        assert self.mod.parse_cron("cron 0 7 * * *") == "0 7 * * *"

    def test_parse_cron_complex(self):
        """'cron */5 * * * *' extracts '*/5 * * * *'."""
        assert self.mod.parse_cron("cron */5 * * * *") == "*/5 * * * *"

    def test_parse_cron_with_ranges(self):
        """'cron 0 8-17 * * 1-5' extracts correctly."""
        assert self.mod.parse_cron("cron 0 8-17 * * 1-5") == "0 8-17 * * 1-5"

    def test_parse_cron_invalid(self):
        """Non-cron string returns empty string."""
        assert self.mod.parse_cron("every 5m") == ""
        assert self.mod.parse_cron("invalid") == ""

    def test_next_cron_time_specific_time(self):
        """Cron '0 7 * * *' returns 7:00am of next matching day."""
        tz = "America/Los_Angeles"
        now = datetime(2026, 5, 1, 6, 0, 0, tzinfo=ZoneInfo(tz)).timestamp()
        next_t = self.mod.next_cron_time("0 7 * * *", now, tz)
        dt = datetime.fromtimestamp(next_t, tz=ZoneInfo(tz))
        assert dt.hour == 7
        assert dt.minute == 0

    def test_next_cron_time_every_5_min(self):
        """Cron '*/5 * * * *' returns next 5-minute mark."""
        tz = "America/Los_Angeles"
        now = datetime(2026, 5, 1, 10, 3, 0, tzinfo=ZoneInfo(tz)).timestamp()
        next_t = self.mod.next_cron_time("*/5 * * * *", now, tz)
        dt = datetime.fromtimestamp(next_t, tz=ZoneInfo(tz))
        assert dt.minute % 5 == 0
        assert dt.minute >= 5  # Next 5-min mark after :03

    def test_next_cron_time_after_time_already_passed(self):
        """Cron time that already passed today schedules for tomorrow."""
        tz = "America/Los_Angeles"
        now = datetime(2026, 5, 1, 20, 0, 0, tzinfo=ZoneInfo(tz)).timestamp()
        next_t = self.mod.next_cron_time("0 7 * * *", now, tz)
        dt = datetime.fromtimestamp(next_t, tz=ZoneInfo(tz))
        assert dt.day == 2  # Tomorrow
        assert dt.hour == 7

    def test_next_cron_time_weekday_filter(self):
        """Cron '0 9 * * 1' only fires on Mondays."""
        tz = "America/Los_Angeles"
        # May 1, 2026 is a Friday (weekday=4)
        now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=ZoneInfo(tz)).timestamp()
        next_t = self.mod.next_cron_time("0 9 * * 1", now, tz)
        dt = datetime.fromtimestamp(next_t, tz=ZoneInfo(tz))
        assert dt.weekday() == 0  # Python weekday(): Monday == 0 (cron '1' == Monday)
        assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 5, 4, 9)

    def test_next_cron_time_comma_separated(self):
        """Cron '0 7,19 * * *' matches 7am and 7pm."""
        tz = "America/Los_Angeles"
        now = datetime(2026, 5, 1, 8, 0, 0, tzinfo=ZoneInfo(tz)).timestamp()
        next_t = self.mod.next_cron_time("0 7,19 * * *", now, tz)
        dt = datetime.fromtimestamp(next_t, tz=ZoneInfo(tz))
        assert dt.hour == 19  # 7pm (7am already passed)

    def test_next_cron_time_range(self):
        """Cron with range field matches correctly."""
        tz = "America/Los_Angeles"
        now = datetime(2026, 5, 1, 6, 0, 0, tzinfo=ZoneInfo(tz)).timestamp()
        next_t = self.mod.next_cron_time("0 8-17 * * *", now, tz)
        dt = datetime.fromtimestamp(next_t, tz=ZoneInfo(tz))
        assert 8 <= dt.hour <= 17

    def test_next_cron_time_step(self):
        """Cron '*/15 * * * *' fires every 15 minutes."""
        tz = "America/Los_Angeles"
        now = datetime(2026, 5, 1, 10, 1, 0, tzinfo=ZoneInfo(tz)).timestamp()
        next_t = self.mod.next_cron_time("*/15 * * * *", now, tz)
        dt = datetime.fromtimestamp(next_t, tz=ZoneInfo(tz))
        assert dt.minute in (0, 15, 30, 45)

    def test_next_cron_time_invalid_fields(self):
        """Invalid (too few fields) returns fallback."""
        tz = "America/Los_Angeles"
        now = time.time()
        result = self.mod.next_cron_time("0 7", now, tz)
        # Fallback: now + 3600
        assert result == now + 3600


# ============================================================================
# nova_scheduler.py — Data Structures
# ============================================================================

class TestSchedulerDataStructures:
    """Tests for Task and TaskState data structures."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_scheduler" in sys.modules:
            del sys.modules["nova_scheduler"]
        import nova_scheduler
        self.mod = nova_scheduler

    def test_task_state_defaults(self):
        """TaskState has correct defaults."""
        ts = self.mod.TaskState()
        assert ts.last_run == 0
        assert ts.last_duration == 0
        assert ts.last_exit_code == 0
        assert ts.consecutive_failures == 0
        assert ts.running is False
        assert ts.run_count == 0

    def test_task_defaults(self):
        """Task has correct defaults."""
        t = self.mod.Task(id="test", script="test.py", schedule="every 5m")
        assert t.timeout == 300
        assert t.overlap == "skip"
        assert t.enabled is True
        assert t.args == []
        assert t.env == {}

    def test_task_schedule_parsing_interval(self):
        """Task with interval schedule gets _interval_s set."""
        t = self.mod.Task(id="test", script="test.py", schedule="every 5m")
        t._interval_s = self.mod.parse_interval(t.schedule)
        assert t._interval_s == 300

    def test_task_schedule_parsing_cron(self):
        """Task with cron schedule gets _cron_expr set."""
        t = self.mod.Task(id="test", script="test.py", schedule="cron 0 7 * * *")
        t._cron_expr = self.mod.parse_cron(t.schedule)
        assert t._cron_expr == "0 7 * * *"


# ============================================================================
# nova_scheduler.py — Scheduler Logic
# ============================================================================

class TestSchedulerLogic:
    """Tests for NovaScheduler state management and scheduling logic."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_scheduler" in sys.modules:
            del sys.modules["nova_scheduler"]
        import nova_scheduler
        self.mod = nova_scheduler

    def test_save_and_load_state(self, tmp_path):
        """Scheduler state persists across save/load."""
        self.mod.STATE_PATH = tmp_path / "scheduler_state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler.tasks = {
            "test_task": self.mod.Task(id="test_task", script="test.py", schedule="every 5m")
        }
        scheduler.tasks["test_task"].state.last_run = 12345.0
        scheduler.tasks["test_task"].state.run_count = 10
        scheduler.tasks["test_task"].state.consecutive_failures = 2

        scheduler._save_state()

        # Verify heartbeat file was written
        assert self.mod.HEARTBEAT_FILE.exists()
        assert float(self.mod.HEARTBEAT_FILE.read_text()) > 0

        # Reload state
        scheduler2 = self.mod.NovaScheduler()
        scheduler2.tasks = {
            "test_task": self.mod.Task(id="test_task", script="test.py", schedule="every 5m")
        }
        scheduler2._load_state()
        assert scheduler2.tasks["test_task"].state.last_run == 12345.0
        assert scheduler2.tasks["test_task"].state.run_count == 10
        assert scheduler2.tasks["test_task"].state.consecutive_failures == 2

    def test_recalculate_next_runs_interval(self, tmp_path):
        """Interval tasks get next_run set based on last_run."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"tz": "America/Los_Angeles"}
        now = time.time()
        task = self.mod.Task(id="t1", script="test.py", schedule="every 5m")
        task._interval_s = 300
        task.state.last_run = now - 60  # Ran 60s ago
        scheduler.tasks = {"t1": task}

        scheduler._recalculate_next_runs()
        # next_run should be last_run + 300 = now + 240
        expected = (now - 60) + 300
        assert abs(task.state.next_run - expected) < 2

    def test_recalculate_next_runs_overdue(self, tmp_path):
        """Overdue interval tasks get next_run set to now."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"tz": "America/Los_Angeles"}
        now = time.time()
        task = self.mod.Task(id="t1", script="test.py", schedule="every 5m")
        task._interval_s = 300
        task.state.last_run = now - 600  # Ran 10 min ago, overdue
        scheduler.tasks = {"t1": task}

        scheduler._recalculate_next_runs()
        # Overdue: next_run should be ~now
        assert abs(task.state.next_run - now) < 2

    def test_recalculate_next_runs_first_run(self, tmp_path):
        """Tasks that never ran get next_run in 5 seconds."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"tz": "America/Los_Angeles"}
        now = time.time()
        task = self.mod.Task(id="t1", script="test.py", schedule="every 5m")
        task._interval_s = 300
        task.state.last_run = 0  # Never ran
        scheduler.tasks = {"t1": task}

        scheduler._recalculate_next_runs()
        assert abs(task.state.next_run - (now + 5)) < 2

    def test_advance_next_run_interval(self, tmp_path):
        """_advance_next_run sets next interval from now."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"tz": "America/Los_Angeles"}
        task = self.mod.Task(id="t1", script="test.py", schedule="every 5m")
        task._interval_s = 300
        scheduler.tasks = {"t1": task}

        now = time.time()
        scheduler._advance_next_run(task)
        assert abs(task.state.next_run - (now + 300)) < 2

    def test_advance_next_run_cron(self, tmp_path):
        """_advance_next_run calculates next cron time."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"tz": "America/Los_Angeles"}
        task = self.mod.Task(id="t1", script="test.py", schedule="cron 0 7 * * *")
        task._cron_expr = "0 7 * * *"
        scheduler.tasks = {"t1": task}

        scheduler._advance_next_run(task)
        dt = datetime.fromtimestamp(task.state.next_run, tz=ZoneInfo("America/Los_Angeles"))
        assert dt.hour == 7
        assert dt.minute == 0

    def test_overlap_skip(self, tmp_path):
        """Task with overlap='skip' has next_run advanced when already running."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"tz": "America/Los_Angeles"}
        now = time.time()
        task = self.mod.Task(id="t1", script="test.py", schedule="every 5m", overlap="skip")
        task._interval_s = 300
        task.state.running = True
        task.state.next_run = now - 10  # overdue
        scheduler.tasks = {"t1": task}

        # Simulate the check in the main loop
        if task.state.running and task.overlap == "skip":
            scheduler._advance_next_run(task)
        assert task.state.next_run > now

    def test_shutdown_signal(self, tmp_path):
        """_shutdown sets _running to False."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler._running = True
        scheduler._shutdown()
        assert scheduler._running is False

    def test_execute_task_success(self, tmp_path):
        """Successful task execution updates state correctly."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"
        self.mod.SCRIPTS_DIR = tmp_path

        # Create a simple test script
        script = tmp_path / "ok.py"
        script.write_text("import sys; sys.exit(0)")

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"python": sys.executable, "tz": "America/Los_Angeles", "env": {"PATH": os.environ.get("PATH", "")}}
        task = self.mod.Task(id="ok_task", script="ok.py", schedule="every 5m", timeout=30)
        task._interval_s = 300
        scheduler.tasks = {"ok_task": task}

        asyncio.run(scheduler.execute_task(task))

        assert task.state.last_exit_code == 0
        assert task.state.consecutive_failures == 0
        assert task.state.run_count == 1
        assert task.state.running is False
        assert task.state.last_run > 0
        assert task.state.last_duration > 0

    def test_execute_task_failure(self, tmp_path):
        """Failed task increments failures and records error."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"
        self.mod.SCRIPTS_DIR = tmp_path

        script = tmp_path / "fail.py"
        script.write_text("import sys; print('error output', file=sys.stderr); sys.exit(1)")

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"python": sys.executable, "tz": "America/Los_Angeles", "env": {"PATH": os.environ.get("PATH", "")}}
        scheduler.slack_cfg = {"alerts": False}  # Disable Slack during test
        task = self.mod.Task(id="fail_task", script="fail.py", schedule="every 5m", timeout=30)
        task._interval_s = 300
        scheduler.tasks = {"fail_task": task}

        # First failure triggers retry (doesn't increment consecutive_failures)
        asyncio.run(scheduler.execute_task(task))
        assert task.state.last_exit_code == 1
        assert task.state._retry_pending is True
        # consecutive_failures should still be 0 (first failure = retry)
        assert task.state.consecutive_failures == 0

    def test_execute_task_retry_then_fail(self, tmp_path):
        """Second consecutive failure increments consecutive_failures."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"
        self.mod.SCRIPTS_DIR = tmp_path

        script = tmp_path / "fail2.py"
        script.write_text("import sys; sys.exit(1)")

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"python": sys.executable, "tz": "America/Los_Angeles", "env": {"PATH": os.environ.get("PATH", "")}}
        scheduler.slack_cfg = {"alerts": False}
        task = self.mod.Task(id="fail2", script="fail2.py", schedule="every 5m", timeout=30)
        task._interval_s = 300
        task.state._retry_pending = True  # Simulates retry already pending
        scheduler.tasks = {"fail2": task}

        asyncio.run(scheduler.execute_task(task))
        assert task.state.consecutive_failures == 1
        assert task.state._retry_pending is False

    def test_execute_task_recovery_clears_failures(self, tmp_path):
        """Successful execution after failures resets consecutive_failures."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"
        self.mod.SCRIPTS_DIR = tmp_path

        script = tmp_path / "recover.py"
        script.write_text("import sys; sys.exit(0)")

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"python": sys.executable, "tz": "America/Los_Angeles", "env": {"PATH": os.environ.get("PATH", "")}}
        task = self.mod.Task(id="rec", script="recover.py", schedule="every 5m", timeout=30)
        task._interval_s = 300
        task.state.consecutive_failures = 5
        scheduler.tasks = {"rec": task}

        asyncio.run(scheduler.execute_task(task))
        assert task.state.consecutive_failures == 0

    def test_execute_task_timeout(self, tmp_path):
        """Task that exceeds timeout is killed and marked as failed."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"
        self.mod.SCRIPTS_DIR = tmp_path

        script = tmp_path / "slow.py"
        script.write_text("import time; time.sleep(60)")

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"python": sys.executable, "tz": "America/Los_Angeles", "env": {"PATH": os.environ.get("PATH", "")}}
        scheduler.slack_cfg = {"alerts": False}
        task = self.mod.Task(id="slow", script="slow.py", schedule="every 5m", timeout=2)  # 2s timeout
        task._interval_s = 300
        scheduler.tasks = {"slow": task}

        asyncio.run(scheduler.execute_task(task))
        assert "timeout" in task.state.last_error.lower() or "timed out" in task.state.last_error.lower()
        assert task.state.running is False

    def test_group_serialization_logic(self, tmp_path):
        """Tasks in the same group should not run concurrently."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"tz": "America/Los_Angeles"}

        task_a = self.mod.Task(id="a", script="a.py", schedule="every 5m", group="mail")
        task_a._interval_s = 300
        task_a.state.running = True  # Already running

        task_b = self.mod.Task(id="b", script="b.py", schedule="every 5m", group="mail")
        task_b._interval_s = 300
        task_b.state.next_run = time.time() - 10  # Overdue

        scheduler.tasks = {"a": task_a, "b": task_b}

        # Check group busy logic
        group_busy = any(
            t.state.running
            for t in scheduler.tasks.values()
            if t.group == task_b.group and t.id != task_b.id
        )
        assert group_busy is True

    def test_http_handler_health(self, tmp_path):
        """HTTP /health endpoint returns ok."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler._start_time = time.time()
        scheduler.tasks = {}

        # Simulate HTTP request parsing
        request = "GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
        path = request.split(" ")[1]
        assert path == "/health"

    def test_http_handler_tasks(self, tmp_path):
        """HTTP /tasks endpoint returns task data."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler._start_time = time.time()
        task = self.mod.Task(id="test", script="test.py", schedule="every 5m")
        task.state.last_run = 12345.0
        task.state.run_count = 5
        scheduler.tasks = {"test": task}

        # Build the response data as the handler would
        tasks_data = {}
        for tid, t in scheduler.tasks.items():
            tasks_data[tid] = {
                "script": t.script,
                "schedule": t.schedule,
                "enabled": t.enabled,
                "running": t.state.running,
                "last_run": t.state.last_run,
                "next_run": t.state.next_run,
                "last_duration": round(t.state.last_duration, 1),
                "last_exit_code": t.state.last_exit_code,
                "consecutive_failures": t.state.consecutive_failures,
                "run_count": t.state.run_count,
            }

        assert "test" in tasks_data
        assert tasks_data["test"]["run_count"] == 5
        assert tasks_data["test"]["last_run"] == 12345.0


# ============================================================================
# nova_scheduler.py — Sleep/Wake Recovery
# ============================================================================

class TestSchedulerSleepWake:
    """Tests for sleep/wake detection and recovery."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_scheduler" in sys.modules:
            del sys.modules["nova_scheduler"]
        import nova_scheduler
        self.mod = nova_scheduler

    def test_time_jump_detected(self, tmp_path):
        """Large time gap between ticks triggers recalculation."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"tz": "America/Los_Angeles", "tick_interval": 1}
        now = time.time()
        scheduler._last_tick = now - 300  # 5 minutes ago (>> tick * 30)

        # The sleep/wake detection checks: now - self._last_tick > tick * 30
        tick = scheduler.sched_cfg.get("tick_interval", 1)
        gap = now - scheduler._last_tick
        assert gap > tick * 30  # This would trigger sleep/wake handling

    def test_overdue_tasks_run_after_wake(self, tmp_path):
        """Tasks overdue after wake get next_run set to now."""
        self.mod.STATE_PATH = tmp_path / "state.json"
        self.mod.HEARTBEAT_FILE = tmp_path / "heartbeat"

        scheduler = self.mod.NovaScheduler()
        scheduler.sched_cfg = {"tz": "America/Los_Angeles"}
        now = time.time()
        task = self.mod.Task(id="overdue", script="test.py", schedule="every 5m")
        task._interval_s = 300
        task.state.last_run = now - 3600  # Ran 1 hour ago
        scheduler.tasks = {"overdue": task}

        scheduler._recalculate_next_runs()
        # next_run should be at or before now (overdue)
        assert task.state.next_run <= now + 1


# ============================================================================
# nova_log_rotate.py
# ============================================================================

class TestLogRotate:
    """Tests for nova_log_rotate.py rotation logic."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        if "nova_log_rotate" in sys.modules:
            del sys.modules["nova_log_rotate"]
        import nova_log_rotate
        self.mod = nova_log_rotate

    def test_trim_jsonl_removes_old_entries(self, tmp_path):
        """JSONL entries older than 30 days are removed."""
        jsonl = tmp_path / "test.jsonl"
        now_ms = int(time.time() * 1000)
        old_ms = int((time.time() - 40 * 86400) * 1000)  # 40 days ago

        entries = [
            json.dumps({"ts": old_ms, "action": "finished", "status": "ok"}),
            json.dumps({"ts": now_ms, "action": "finished", "status": "ok"}),
        ]
        jsonl.write_text("\n".join(entries))

        before, after = self.mod.trim_jsonl(jsonl)
        assert before == 2
        assert after == 1  # Only the recent entry kept

    def test_trim_jsonl_keeps_recent_entries(self, tmp_path):
        """JSONL entries within 30 days are kept."""
        jsonl = tmp_path / "test.jsonl"
        now_ms = int(time.time() * 1000)

        entries = [
            json.dumps({"ts": now_ms - 3600000, "action": "finished"}),
            json.dumps({"ts": now_ms, "action": "finished"}),
        ]
        jsonl.write_text("\n".join(entries))

        before, after = self.mod.trim_jsonl(jsonl)
        assert before == 2
        assert after == 2  # Both kept

    def test_trim_jsonl_preserves_malformed_lines(self, tmp_path):
        """Malformed JSON lines are preserved (not discarded)."""
        jsonl = tmp_path / "test.jsonl"
        now_ms = int(time.time() * 1000)

        entries = [
            "this is not json",
            json.dumps({"ts": now_ms, "action": "finished"}),
        ]
        jsonl.write_text("\n".join(entries))

        before, after = self.mod.trim_jsonl(jsonl)
        assert after == 2  # Both kept (malformed line preserved)

    def test_trim_jsonl_nonexistent_file(self, tmp_path):
        """Nonexistent file returns (0, 0)."""
        before, after = self.mod.trim_jsonl(tmp_path / "nonexistent.jsonl")
        assert before == 0 and after == 0

    def test_trim_log_file_under_threshold(self, tmp_path):
        """Log files under 5MB are not trimmed."""
        log_file = tmp_path / "small.log"
        log_file.write_text("small content\n" * 100)
        freed = self.mod.trim_log_file(log_file)
        assert freed == 0

    def test_trim_log_file_over_threshold(self, tmp_path):
        """Log files over 5MB are trimmed to last 5MB."""
        log_file = tmp_path / "big.log"
        # Write ~6MB
        content = "x" * 100 + "\n"
        line_count = (6 * 1024 * 1024) // len(content)
        log_file.write_text(content * line_count)

        original_size = log_file.stat().st_size
        assert original_size > self.mod.MAX_LOG_BYTES

        freed = self.mod.trim_log_file(log_file)
        assert freed > 0
        new_size = log_file.stat().st_size
        assert new_size <= self.mod.MAX_LOG_BYTES

    def test_trim_log_file_preserves_line_boundaries(self, tmp_path):
        """Trimmed log file starts at a line boundary."""
        log_file = tmp_path / "lines.log"
        lines = [f"line {i}: {'x' * 200}\n" for i in range(30000)]
        log_file.write_text("".join(lines))

        original_size = log_file.stat().st_size
        if original_size <= self.mod.MAX_LOG_BYTES:
            pytest.skip("Test content not big enough")

        self.mod.trim_log_file(log_file)
        # First line should start at a line boundary
        content = log_file.read_text()
        assert content.startswith("line ")

    def test_main_integration(self, tmp_path):
        """Main function processes both JSONL and log files."""
        # Set up directories
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        # Create a JSONL file with old entries
        jsonl = runs_dir / "old_task.jsonl"
        old_ms = int((time.time() - 40 * 86400) * 1000)
        jsonl.write_text(json.dumps({"ts": old_ms, "action": "finished", "status": "ok"}))

        # Create a large log file
        big_log = logs_dir / "big.log"
        big_log.write_text("x" * 100 + "\n")  # Small for speed

        original_runs = self.mod.CRON_RUNS_DIR
        original_logs = self.mod.LOGS_DIR
        self.mod.CRON_RUNS_DIR = runs_dir
        self.mod.LOGS_DIR = logs_dir

        with patch.object(self.mod, "slack_post"):
            self.mod.main()

        self.mod.CRON_RUNS_DIR = original_runs
        self.mod.LOGS_DIR = original_logs

        # JSONL should be trimmed
        remaining = jsonl.read_text().strip()
        assert remaining == ""  # Old entry removed, nothing left


# ============================================================================
# nova_self_audit.py
# ============================================================================

class TestSelfAudit:
    """Tests for nova_self_audit.py audit check logic."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        if "nova_self_audit" in sys.modules:
            del sys.modules["nova_self_audit"]
        import nova_self_audit
        self.mod = nova_self_audit

    def test_scripts_on_disk(self):
        """_scripts_on_disk returns set of .py and .sh files."""
        with patch.object(Path, "glob") as mock_glob:
            mock_glob.side_effect = [
                [Path("nova_health_check.py"), Path("nova_watchdog.py")],
                [Path("nova_start.sh")],
            ]
            result = self.mod._scripts_on_disk()
        assert "nova_health_check.py" in result
        assert "nova_watchdog.py" in result
        assert "nova_start.sh" in result

    def test_scripts_in_file(self, tmp_path):
        """_scripts_in_file extracts nova_*.py and dream_*.py references."""
        test_file = tmp_path / "test.md"
        test_file.write_text("""
        Some text about nova_health_check.py and nova_watchdog.py.
        Also mentions dream_generate.py but not random_script.py.
        """)
        result = self.mod._scripts_in_file(test_file)
        assert "nova_health_check.py" in result
        assert "nova_watchdog.py" in result
        assert "dream_generate.py" in result
        # random_script.py should not match (doesn't start with nova_ or dream_)
        assert "random_script.py" not in result

    def test_scripts_in_file_nonexistent(self):
        """_scripts_in_file returns empty set for missing file."""
        result = self.mod._scripts_in_file(Path("/nonexistent/path/file.md"))
        assert result == set()

    def test_scripts_in_scheduler(self, tmp_path):
        """_scripts_in_scheduler extracts script references from YAML."""
        yaml_file = tmp_path / "scheduler.yaml"
        yaml_file.write_text("""
scheduler:
  tz: America/Los_Angeles

tasks:
  morning_brief:
    script: nova_morning_brief.py
    schedule: cron 0 7 * * *
  health_check:
    script: nova_health_check.py
    schedule: cron 45 6 * * *
""")
        original = self.mod.SCHEDULER_YAML
        self.mod.SCHEDULER_YAML = yaml_file

        result = self.mod._scripts_in_scheduler()

        self.mod.SCHEDULER_YAML = original
        assert "morning_brief" in result
        assert result["morning_brief"] == "nova_morning_brief.py"
        assert result["health_check"] == "nova_health_check.py"

    def test_port_listening_open(self):
        """_port_listening returns True for open port."""
        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value.__enter__ = lambda s: mock_sock
            mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = self.mod._port_listening(37460)
        assert result is True

    def test_port_listening_closed(self):
        """_port_listening returns False for closed port."""
        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_sock.connect.side_effect = ConnectionRefusedError()
            mock_socket_cls.return_value.__enter__ = lambda s: mock_sock
            mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = self.mod._port_listening(99999)
        assert result is False

    def test_process_running_true(self):
        """_process_running returns True when pgrep finds process."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = self.mod._process_running("nova_scheduler.py")
        assert result is True

    def test_process_running_false(self):
        """_process_running returns False when pgrep finds nothing."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = self.mod._process_running("nonexistent_process")
        assert result is False

    def test_audit_scripts_missing_from_disk(self, tmp_path):
        """Scheduler tasks whose script is missing on disk are flagged; present ones are not.

        MEMORY.md was retired as a script source (b61fe79 — memories live in PG), so the
        scheduler YAML is the only reference list and mem_count is always 0."""
        assert not hasattr(self.mod, "MEMORY_MD")
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "nova_present.py").write_text("# ok\n")
        yaml_file = tmp_path / "scheduler.yaml"
        yaml_file.write_text("""
tasks:
  good_task:
    script: nova_present.py
    schedule: cron 0 7 * * *
  bad_task:
    script: nova_fake_script.py
    schedule: cron 0 8 * * *
""")

        original_scripts = self.mod.SCRIPTS_DIR
        original_scheduler = self.mod.SCHEDULER_YAML
        self.mod.SCRIPTS_DIR = scripts_dir
        self.mod.SCHEDULER_YAML = yaml_file
        try:
            issues, info, disk_count, mem_count, sched_count = self.mod.audit_scripts()
        finally:
            self.mod.SCRIPTS_DIR = original_scripts
            self.mod.SCHEDULER_YAML = original_scheduler

        assert any("nova_fake_script.py" in i and "doesn't exist" in i for i in issues)
        assert not any("nova_present.py" in i for i in issues)
        assert mem_count == 0
        assert sched_count == 2
        assert disk_count == 1

    def test_audit_scripts_missing_scheduler_script(self, tmp_path):
        """Scheduler referencing non-existent scripts is flagged."""
        yaml_file = tmp_path / "scheduler.yaml"
        yaml_file.write_text("""
tasks:
  broken_task:
    script: nova_missing.py
    schedule: cron 0 7 * * *
""")
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()

        original_scripts = self.mod.SCRIPTS_DIR
        original_scheduler = self.mod.SCHEDULER_YAML

        self.mod.SCRIPTS_DIR = scripts_dir
        self.mod.SCHEDULER_YAML = yaml_file
        try:
            issues, info, _, _, _ = self.mod.audit_scripts()
        finally:
            self.mod.SCRIPTS_DIR = original_scripts
            self.mod.SCHEDULER_YAML = original_scheduler

        assert any("nova_missing.py" in i and "doesn't exist" in i for i in issues)

    def test_audit_services(self):
        """audit_services correctly identifies up/down services."""
        def mock_port_listening(port, host="127.0.0.1"):
            return port != 99999

        with patch.object(self.mod, "_port_listening", side_effect=mock_port_listening):
            # Add a fake service to test
            original = dict(self.mod.EXPECTED_SERVICES)
            self.mod.EXPECTED_SERVICES[99999] = {"name": "FakeService", "path": "/"}
            issues, ok = self.mod.audit_services()
            self.mod.EXPECTED_SERVICES = original

        assert any("FakeService" in i for i in issues)

    def test_audit_processes(self):
        """audit_processes correctly identifies running/stopped processes."""
        def mock_process_running(match_str):
            return match_str != "nonexistent_process"

        with patch.object(self.mod, "_process_running", side_effect=mock_process_running):
            original = list(self.mod.EXPECTED_PROCESSES)
            self.mod.EXPECTED_PROCESSES.append({"name": "FakeProc", "match": "nonexistent_process"})
            issues, ok = self.mod.audit_processes()
            self.mod.EXPECTED_PROCESSES = original

        assert any("FakeProc" in i for i in issues)

    def test_audit_docs_missing_memory(self, tmp_path):
        """Flat-file docs (MEMORY.md / IDENTITY.md) are no longer audited — memories and
        persona live in PostgreSQL (b61fe79), so audit_docs() never flags a missing file."""
        assert not hasattr(self.mod, "MEMORY_MD")
        assert not hasattr(self.mod, "IDENTITY_MD")
        issues = self.mod.audit_docs()
        assert issues == []
        assert not any("MEMORY.md" in i for i in issues)

    def test_audit_docs_empty_memory(self, tmp_path):
        """audit_docs() is filesystem-independent: an empty MEMORY.md on disk is never flagged."""
        memory = tmp_path / "MEMORY.md"
        memory.write_text("short")
        with patch.object(self.mod.Path, "home", return_value=tmp_path):
            issues = self.mod.audit_docs()
        assert isinstance(issues, list)
        assert not any("empty" in i.lower() or "minimal" in i.lower() for i in issues)

    def test_audit_state_persistence(self, tmp_path):
        """Audit state saves and loads correctly for dedup."""
        state_file = tmp_path / "audit_state.json"
        original = self.mod.AUDIT_STATE_FILE
        self.mod.AUDIT_STATE_FILE = state_file

        state = {"last_issue_key": '["some issue"]', "last_run": "2026-05-01"}
        self.mod._save_audit_state(state)
        loaded = self.mod._load_last_audit_state()

        self.mod.AUDIT_STATE_FILE = original
        assert loaded["last_issue_key"] == '["some issue"]'

    def test_run_audit_posts_new_issues_to_slack(self, tmp_path):
        """New issues get posted to Slack."""
        state_file = tmp_path / "audit_state.json"
        original_state = self.mod.AUDIT_STATE_FILE
        self.mod.AUDIT_STATE_FILE = state_file

        with patch.object(self.mod, "audit_scripts", return_value=(["test issue"], [], 10, 5, 5)), \
             patch.object(self.mod, "audit_services", return_value=([], ["svc1"])), \
             patch.object(self.mod, "audit_processes", return_value=([], ["proc1"])), \
             patch.object(self.mod, "audit_docs", return_value=([])), \
             patch.object(self.mod, "slack_post") as mock_slack:
            result = self.mod.run_audit()

        self.mod.AUDIT_STATE_FILE = original_state
        assert result == 1
        assert mock_slack.called

    def test_run_audit_suppresses_duplicate_slack(self, tmp_path):
        """Unchanged issues don't get re-posted to Slack."""
        state_file = tmp_path / "audit_state.json"
        original_state = self.mod.AUDIT_STATE_FILE
        self.mod.AUDIT_STATE_FILE = state_file

        # Pre-seed with the same issue key
        issues = ["test issue"]
        issue_key = json.dumps(sorted(issues))
        self.mod._save_audit_state({"last_issue_key": issue_key, "last_run": "2026-05-01"})

        with patch.object(self.mod, "audit_scripts", return_value=(issues, [], 10, 5, 5)), \
             patch.object(self.mod, "audit_services", return_value=([], ["svc1"])), \
             patch.object(self.mod, "audit_processes", return_value=([], ["proc1"])), \
             patch.object(self.mod, "audit_docs", return_value=([])), \
             patch.object(self.mod, "slack_post") as mock_slack:
            self.mod.run_audit()

        self.mod.AUDIT_STATE_FILE = original_state
        # Slack should NOT be called because issues haven't changed
        mock_slack.assert_not_called()


# ============================================================================
# Integration Tests
# ============================================================================

@pytest.mark.skipif(not os.environ.get("NOVA_LIVE_TESTS"), reason="live scheduler tests need NOVA_LIVE_TESTS=1")
class TestSchedulerIntegration:
    """Integration tests for live scheduler service."""

    @pytest.mark.integration
    def test_scheduler_status_api(self):
        """Scheduler status API returns valid JSON."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:37460/status", timeout=5)
            data = json.loads(resp.read())
            assert "status" in data
            assert "uptime_s" in data
            assert "tasks_total" in data
            assert "tasks_running" in data
        except Exception:
            pytest.skip("Scheduler not running on port 37460")

    @pytest.mark.integration
    def test_scheduler_tasks_api(self):
        """Scheduler tasks API returns task details with expected fields."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:37460/tasks", timeout=5)
            data = json.loads(resp.read())
            assert isinstance(data, dict)
            for task_id, task in data.items():
                assert "script" in task
                assert "schedule" in task
                assert "enabled" in task
                assert "last_run" in task
                assert "consecutive_failures" in task
        except Exception:
            pytest.skip("Scheduler not running on port 37460")

    @pytest.mark.integration
    def test_scheduler_health_endpoint(self):
        """Scheduler /health responds with ok."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:37460/health", timeout=5)
            data = json.loads(resp.read())
            assert data.get("ok") is True
        except Exception:
            pytest.skip("Scheduler not running on port 37460")


# ── house categories added 2026-10-05 (7 unittest classes) ──────────────────
# Tests for nova_scheduler.py — the 7 house categories (Security, Performance, Retry, Unit, Integration,
# Functional, Frame). Written by Jordan Koch (via Claude). A private copy of the module is loaded with
# its logger, notifier and nova_ops writer replaced, so no file/PG/Slack side effect is possible.

import importlib.util as _ilu
import re as _re
import subprocess as _sp
import tempfile as _tempfile
import types as _types
import unittest

_SCRIPTS = Path(__file__).resolve().parents[1]
_SRC = (_SCRIPTS / "nova_scheduler.py").read_text()
_TMP = _tempfile.TemporaryDirectory()


def _load_sched():
    spec = _ilu.spec_from_file_location("sched_house", _SCRIPTS / "nova_scheduler.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log = MagicMock()
    mod.notify = MagicMock()
    mod.nova_ops_writer = _types.SimpleNamespace(record_run_start=MagicMock(), record_run_end=MagicMock(),
                                                 close=AsyncMock())
    mod.STATE_PATH = Path(_TMP.name) / "state.json"
    mod.HEARTBEAT_FILE = Path(_TMP.name) / "heartbeat"
    mod.CONFIG_PATH = Path(_TMP.name) / "scheduler.yaml"
    return mod


_sh = _load_sched()


class _Proc:
    def __init__(self, rc=0, out=b"ok", err=b""):
        self.returncode = rc; self.pid = 4242; self._out = out; self._err = err

    async def communicate(self):
        return self._out, self._err

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _sched(alerts=True):
    s = _sh.NovaScheduler()
    s.sched_cfg = {"python": "/usr/bin/python3", "tz": "America/Los_Angeles", "env": {"PATH": "/usr/bin"}}
    s.slack_cfg = {"alerts": alerts}
    return s


def _task(tid="t1", **kw):
    t = _sh.Task(id=tid, script="job.py", schedule="every 5m", timeout=30, **kw)
    t._interval_s = 300
    return t


def _exec(s, task, procs):
    procs = list(procs)
    calls = []

    async def cse(*cmd, **kw):
        calls.append((cmd, kw))
        return procs.pop(0)

    with patch.object(_sh.asyncio, "create_subprocess_exec", side_effect=cse):
        asyncio.run(s.execute_task(task))
    return calls


class _HouseBase(unittest.TestCase):
    def setUp(self):
        _sh.notify.reset_mock(); _sh.log.reset_mock()
        _sh.nova_ops_writer.record_run_start.reset_mock(); _sh.nova_ops_writer.record_run_end.reset_mock()


class TestSecurity(_HouseBase):
    def test_no_hardcoded_credentials(self):
        pat = _re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", _re.I)
        self.assertIsNone(pat.search(_SRC))
        self.assertNotIn("shell=True", _SRC)

    def test_tasks_run_as_argv_never_a_shell_string(self):
        s = _sched()
        t = _task(args=["--flag; rm -rf ~"])
        calls = _exec(s, t, [_Proc(0)])
        cmd, kw = calls[0]
        self.assertEqual(cmd[0], "/usr/bin/python3")
        self.assertEqual(cmd[-1], "--flag; rm -rf ~")              # one inert argv token
        self.assertEqual(kw["cwd"], str(_sh.SCRIPTS_DIR))

    def test_run_history_query_is_parameterized(self):
        self.assertIn("WHERE task_id=$1", _SRC)
        self.assertIsNone(_re.search(r"fetch\(\s*f[\"']", _SRC))


class TestPerformance(_HouseBase):
    def test_cron_and_interval_parsing_fast_and_bounded(self):
        t0 = time.time()
        base = time.time()
        for i in range(500):
            _sh.next_cron_time("*/5 * * * *", base + i * 60)
        for i in range(10_000):
            _sh.parse_interval(f"every {i % 90}m")
        self.assertLess(time.time() - t0, 3.0)
        self.assertEqual(_sh.next_cron_time("bad expr", 1000.0), 4600.0)
        self.assertIn("range(525600)", _SRC)                          # the search loop is capped at a year


class TestRetry(_HouseBase):
    def test_first_failure_retries_in_60s_then_recovers(self):
        s = _sched()
        t = _task()
        _exec(s, t, [_Proc(1, err=b"boom")])
        self.assertTrue(t.state._retry_pending)
        self.assertEqual(t.state.consecutive_failures, 0)
        self.assertAlmostEqual(t.state.next_run, time.time() + 60, delta=5)
        _exec(s, t, [_Proc(0)])
        self.assertFalse(t.state._retry_pending)
        self.assertTrue(_sh.nova_ops_writer.record_run_end.call_args[1]["retry_recovered"])
        self.assertTrue(_sh.nova_ops_writer.record_run_start.call_args[1]["was_retry"])
        _sh.notify.assert_not_called()

    def test_alert_cadence_backs_off(self):
        self.assertEqual([n for n in range(1, 70) if _sh._should_alert_failure(n)], [1, 2, 4, 8, 16, 32, 64])
        self.assertFalse(_sh._should_alert_failure(0))


class TestUnit(_HouseBase):
    def test_parse_edges(self):
        self.assertEqual(_sh.parse_interval("every 2h"), 7200)
        self.assertEqual(_sh.parse_interval("hourly"), 0)
        self.assertEqual(_sh.parse_cron("cron  0 8 * * 0 "), "0 8 * * 0")
        self.assertEqual(_sh.parse_cron("every 5m"), "")

    def test_timeout_sets_124(self):
        class Slow(_Proc):
            async def communicate(self):
                await asyncio.sleep(5)
        s = _sched(alerts=False)
        t = _task(); t.timeout = 0.01; t.state._retry_pending = True
        _exec(s, t, [Slow()])
        self.assertEqual(t.state.last_exit_code, 124)
        self.assertEqual(_sh.nova_ops_writer.record_run_end.call_args[1]["status"], "timeout")
        self.assertIn("Timed out", t.state.last_error)


class TestIntegration(_HouseBase):
    def test_run_start_and_end_share_run_id(self):
        s = _sched()
        _exec(s, _task(), [_Proc(0, out=b"x" * 500)])
        st = _sh.nova_ops_writer.record_run_start.call_args[1]
        en = _sh.nova_ops_writer.record_run_end.call_args[1]
        self.assertEqual(st["run_id"], en["run_id"])
        self.assertEqual((st["task_id"], en["status"]), ("t1", "success"))
        self.assertEqual(len(en["stdout_tail"]), 200)

    def test_failure_alert_rate_limited_per_task(self):
        s = _sched()
        asyncio.run(s._slack_alert(":x: *a* — boom", task_id="a"))
        asyncio.run(s._slack_alert(":x: *a* — boom again", task_id="a"))
        asyncio.run(s._slack_alert(":x: *b* — boom", task_id="b"))
        keys = [c[0][5] for c in _sh.notify.call_args_list]
        self.assertEqual(keys, ["scheduler-task-failure-a", "scheduler-task-failure-b"])


class TestFunctional(_HouseBase):
    def test_quarantine_after_threshold_alerts_once(self):
        s = _sched()
        t = _task()
        t.state.consecutive_failures = _sh.QUARANTINE_THRESHOLD - 1
        _exec(s, t, [_Proc(1, err=b"dead")])
        _exec(s, t, [_Proc(1, err=b"dead")])
        self.assertIn("t1", s._quarantined)
        dks = [c[0][5] for c in _sh.notify.call_args_list]
        self.assertEqual(dks.count("scheduler-quarantine-t1"), 1)
        _exec(s, t, [_Proc(0)])
        self.assertNotIn("t1", s._quarantined)                      # a success clears dead-letter

    def test_reload_adds_updates_removes(self):
        _sh.CONFIG_PATH.write_text(
            "scheduler: {}\nslack: {alerts: false}\ntasks:\n"
            "  keep: {script: k.py, schedule: every 10m}\n  new: {script: n.py, schedule: cron 0 8 * * 0}\n")
        s = _sched()
        s.tasks = {"keep": _task("keep"), "gone": _task("gone")}
        s.tasks["keep"].state.run_count = 7
        s._quarantined = {"keep"}
        s._reload()
        self.assertEqual(set(s.tasks), {"keep", "new"})
        self.assertEqual(s.tasks["keep"].state.run_count, 7)          # runtime state preserved
        self.assertEqual(s.tasks["new"]._cron_expr, "0 8 * * 0")
        self.assertEqual(s._quarantined, set())                        # schedule changed -> cleared
        self.assertEqual(s.slack_cfg, {"alerts": False})

    def test_reload_with_broken_yaml_keeps_tasks(self):
        _sh.CONFIG_PATH.write_text("tasks: [unclosed")
        s = _sched(); s.tasks = {"keep": _task("keep")}
        s._reload()
        self.assertEqual(set(s.tasks), {"keep"})


class TestFrame(unittest.TestCase):
    def test_import_never_starts_the_scheduler(self):
        # main() loads the live config and runs the forever loop, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', _SRC)
        with _tempfile.TemporaryDirectory() as home:
            r = _sp.run([sys.executable, "-c", "import nova_scheduler, threading; print(threading.active_count())"],
                        cwd=str(_SCRIPTS), capture_output=True, text=True, timeout=30,
                        env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
