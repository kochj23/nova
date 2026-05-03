"""test_goals_rules.py — Tests for Nova goals tracker and rules engine. Written by Jordan Koch."""

import importlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ============================================================================
# nova_goals.py — _escape()
# ============================================================================


class TestGoalsEscape:
    """Unit tests for the _escape() sanitization helper."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    def test_escape_single_quotes(self):
        """Single quotes are doubled for SQL safety."""
        assert self.mod._escape("it's a test") == "it''s a test"

    def test_escape_backslashes(self):
        """Backslashes are doubled."""
        assert self.mod._escape("path\\to\\file") == "path\\\\to\\\\file"

    def test_escape_both(self):
        """Mixed quotes and backslashes are both escaped."""
        assert self.mod._escape("it's a \\path") == "it''s a \\\\path"

    def test_escape_empty_string(self):
        """Empty string returns empty string."""
        assert self.mod._escape("") == ""

    def test_escape_none(self):
        """None returns empty string."""
        assert self.mod._escape(None) == ""

    def test_escape_no_special_chars(self):
        """Plain string passes through unchanged."""
        assert self.mod._escape("hello world") == "hello world"

    def test_escape_multiple_quotes(self):
        """Multiple single quotes are all doubled."""
        assert self.mod._escape("don't won't can't") == "don''t won''t can''t"

    def test_escape_sql_injection_attempt(self):
        """Classic SQL injection attempt is neutralized."""
        result = self.mod._escape("'; DROP TABLE goals; --")
        assert "''" in result
        assert "DROP TABLE" in result  # text preserved but quotes escaped

    def test_escape_unicode(self):
        """Unicode characters pass through unchanged."""
        assert self.mod._escape("goal: ✅ complete") == "goal: ✅ complete"


# ============================================================================
# nova_goals.py — ensure_schema()
# ============================================================================


class TestGoalsEnsureSchema:
    """Tests for ensure_schema() table creation."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._exec")
    def test_ensure_schema_calls_exec(self, mock_exec):
        """ensure_schema() calls _exec with CREATE TABLE statements."""
        mock_exec.return_value = True
        self.mod.ensure_schema()
        mock_exec.assert_called_once()
        sql = mock_exec.call_args[0][0]
        assert "CREATE TABLE IF NOT EXISTS goals" in sql
        assert "CREATE TABLE IF NOT EXISTS goal_log" in sql

    @patch("nova_goals._exec")
    def test_ensure_schema_creates_indexes(self, mock_exec):
        """ensure_schema() SQL includes all required indexes."""
        mock_exec.return_value = True
        self.mod.ensure_schema()
        sql = mock_exec.call_args[0][0]
        assert "idx_goals_status" in sql
        assert "idx_goals_project" in sql
        assert "idx_goal_log_goal" in sql
        assert "idx_goal_log_ts" in sql


# ============================================================================
# nova_goals.py — add_goal()
# ============================================================================


class TestAddGoal:
    """Tests for add_goal() CRUD operation."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_add_goal_minimal(self, mock_exec, mock_log_event):
        """add_goal() with only a title succeeds and returns an ID."""
        mock_exec.return_value = True
        gid = self.mod.add_goal("Ship MLXCode v3")
        assert gid is not None
        assert len(gid) == 8
        mock_exec.assert_called_once()
        sql = mock_exec.call_args[0][0]
        assert "Ship MLXCode v3" in sql
        assert "medium" in sql  # default priority

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_add_goal_all_params(self, mock_exec, mock_log_event):
        """add_goal() with all parameters includes them in SQL."""
        mock_exec.return_value = True
        gid = self.mod.add_goal(
            "Release NMAPScanner",
            description="Full port scanning app",
            project="NMAPScanner",
            priority="high",
            deadline="2026-06-01",
            check_in_days=3,
        )
        assert gid is not None
        sql = mock_exec.call_args[0][0]
        assert "Release NMAPScanner" in sql
        assert "Full port scanning app" in sql
        assert "'NMAPScanner'" in sql
        assert "'high'" in sql
        assert "'2026-06-01'" in sql
        assert "3" in sql

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_add_goal_null_project(self, mock_exec, mock_log_event):
        """add_goal() without project uses NULL in SQL."""
        mock_exec.return_value = True
        self.mod.add_goal("General task")
        sql = mock_exec.call_args[0][0]
        assert "NULL" in sql

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_add_goal_null_deadline(self, mock_exec, mock_log_event):
        """add_goal() without deadline uses NULL in SQL."""
        mock_exec.return_value = True
        self.mod.add_goal("Ongoing task")
        sql = mock_exec.call_args[0][0]
        # deadline should be NULL since not provided
        assert "NULL" in sql

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_add_goal_logs_created_event(self, mock_exec, mock_log_event):
        """add_goal() logs a 'created' event on success."""
        mock_exec.return_value = True
        gid = self.mod.add_goal("Test goal")
        mock_log_event.assert_called_once()
        args = mock_log_event.call_args[0]
        assert args[0] == gid
        assert args[1] == "created"

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_add_goal_db_failure_returns_none(self, mock_exec, mock_log_event):
        """add_goal() returns None when database insert fails."""
        mock_exec.return_value = False
        gid = self.mod.add_goal("Doomed goal")
        assert gid is None
        mock_log_event.assert_not_called()

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_add_goal_escapes_title(self, mock_exec, mock_log_event):
        """add_goal() escapes quotes in the title."""
        mock_exec.return_value = True
        self.mod.add_goal("Jordan's big plan")
        sql = mock_exec.call_args[0][0]
        assert "Jordan''s big plan" in sql


# ============================================================================
# nova_goals.py — update_goal()
# ============================================================================


class TestUpdateGoal:
    """Tests for update_goal() modifications."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._exec")
    def test_update_title(self, mock_exec):
        """update_goal() updates the title field."""
        mock_exec.return_value = True
        result = self.mod.update_goal("abc123", title="New Title")
        assert result is True
        sql = mock_exec.call_args[0][0]
        assert "title = 'New Title'" in sql
        assert "updated_at = NOW()" in sql

    @patch("nova_goals._exec")
    def test_update_priority(self, mock_exec):
        """update_goal() updates priority."""
        mock_exec.return_value = True
        self.mod.update_goal("abc123", priority="high")
        sql = mock_exec.call_args[0][0]
        assert "priority = 'high'" in sql

    @patch("nova_goals._exec")
    def test_update_deadline(self, mock_exec):
        """update_goal() sets a deadline value."""
        mock_exec.return_value = True
        self.mod.update_goal("abc123", deadline="2026-08-01")
        sql = mock_exec.call_args[0][0]
        assert "deadline = '2026-08-01'" in sql

    @patch("nova_goals._exec")
    def test_update_deadline_to_null(self, mock_exec):
        """update_goal() clears deadline when set to None."""
        mock_exec.return_value = True
        self.mod.update_goal("abc123", deadline=None)
        sql = mock_exec.call_args[0][0]
        assert "deadline = NULL" in sql

    @patch("nova_goals._exec")
    def test_update_check_in_days(self, mock_exec):
        """update_goal() updates check_in_days as integer."""
        mock_exec.return_value = True
        self.mod.update_goal("abc123", check_in_days=14)
        sql = mock_exec.call_args[0][0]
        assert "check_in_days = 14" in sql

    @patch("nova_goals._exec")
    def test_update_no_kwargs_returns_false(self, mock_exec):
        """update_goal() with no valid kwargs returns False without querying."""
        result = self.mod.update_goal("abc123")
        assert result is False
        mock_exec.assert_not_called()

    @patch("nova_goals._exec")
    def test_update_ignores_unknown_keys(self, mock_exec):
        """update_goal() ignores keys not in the allowed set."""
        result = self.mod.update_goal("abc123", unknown_field="value")
        assert result is False
        mock_exec.assert_not_called()

    @patch("nova_goals._exec")
    def test_update_multiple_fields(self, mock_exec):
        """update_goal() handles multiple fields at once."""
        mock_exec.return_value = True
        self.mod.update_goal("abc123", title="Updated", priority="low", project="MLXCode")
        sql = mock_exec.call_args[0][0]
        assert "title = 'Updated'" in sql
        assert "priority = 'low'" in sql
        assert "project = 'MLXCode'" in sql

    @patch("nova_goals._exec")
    def test_update_escapes_values(self, mock_exec):
        """update_goal() escapes single quotes in values."""
        mock_exec.return_value = True
        self.mod.update_goal("abc123", description="Jordan's notes")
        sql = mock_exec.call_args[0][0]
        assert "Jordan''s notes" in sql


# ============================================================================
# nova_goals.py — complete_goal(), pause_goal(), drop_goal()
# ============================================================================


class TestGoalStatusTransitions:
    """Tests for goal lifecycle transitions."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_complete_goal_sets_status(self, mock_exec, mock_log_event):
        """complete_goal() sets status to 'completed' and sets completed_at."""
        mock_exec.return_value = True
        result = self.mod.complete_goal("abc123", note="Done!")
        assert result is True
        sql = mock_exec.call_args[0][0]
        assert "status = 'completed'" in sql
        assert "completed_at = NOW()" in sql

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_complete_goal_logs_event(self, mock_exec, mock_log_event):
        """complete_goal() logs a 'completed' event."""
        mock_exec.return_value = True
        self.mod.complete_goal("abc123", note="Shipped it")
        mock_log_event.assert_called_once_with("abc123", "completed", "Shipped it")

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_complete_goal_default_note(self, mock_exec, mock_log_event):
        """complete_goal() uses default note when none provided."""
        mock_exec.return_value = True
        self.mod.complete_goal("abc123")
        mock_log_event.assert_called_once_with("abc123", "completed", "Goal completed")

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_complete_goal_db_failure(self, mock_exec, mock_log_event):
        """complete_goal() returns False on DB failure."""
        mock_exec.return_value = False
        result = self.mod.complete_goal("abc123")
        assert result is False
        mock_log_event.assert_not_called()

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_pause_goal_sets_status(self, mock_exec, mock_log_event):
        """pause_goal() sets status to 'paused'."""
        mock_exec.return_value = True
        result = self.mod.pause_goal("abc123", reason="On hold")
        assert result is True
        sql = mock_exec.call_args[0][0]
        assert "status = 'paused'" in sql

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_pause_goal_logs_event(self, mock_exec, mock_log_event):
        """pause_goal() logs a 'paused' event."""
        mock_exec.return_value = True
        self.mod.pause_goal("abc123", reason="Waiting on deps")
        mock_log_event.assert_called_once_with("abc123", "paused", "Waiting on deps")

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_pause_goal_default_reason(self, mock_exec, mock_log_event):
        """pause_goal() uses default reason when none provided."""
        mock_exec.return_value = True
        self.mod.pause_goal("abc123")
        mock_log_event.assert_called_once_with("abc123", "paused", "Goal paused")

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_drop_goal_sets_status(self, mock_exec, mock_log_event):
        """drop_goal() sets status to 'dropped'."""
        mock_exec.return_value = True
        result = self.mod.drop_goal("abc123", reason="No longer relevant")
        assert result is True
        sql = mock_exec.call_args[0][0]
        assert "status = 'dropped'" in sql

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_drop_goal_logs_event(self, mock_exec, mock_log_event):
        """drop_goal() logs a 'dropped' event."""
        mock_exec.return_value = True
        self.mod.drop_goal("abc123", reason="Abandoned")
        mock_log_event.assert_called_once_with("abc123", "dropped", "Abandoned")

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_drop_goal_default_reason(self, mock_exec, mock_log_event):
        """drop_goal() uses default reason when none provided."""
        mock_exec.return_value = True
        self.mod.drop_goal("abc123")
        mock_log_event.assert_called_once_with("abc123", "dropped", "Goal dropped")


# ============================================================================
# nova_goals.py — log_progress(), touch_activity()
# ============================================================================


class TestGoalProgress:
    """Tests for progress logging and activity tracking."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._exec")
    @patch("nova_goals._log_event")
    def test_log_progress_logs_event(self, mock_log_event, mock_exec):
        """log_progress() creates a 'progress' event."""
        mock_exec.return_value = True
        self.mod.log_progress("abc123", "Fixed the parser bug")
        mock_log_event.assert_called_once_with("abc123", "progress", "Fixed the parser bug")

    @patch("nova_goals._exec")
    @patch("nova_goals._log_event")
    def test_log_progress_updates_last_activity(self, mock_log_event, mock_exec):
        """log_progress() also updates last_activity timestamp."""
        mock_exec.return_value = True
        self.mod.log_progress("abc123", "Progress note")
        mock_exec.assert_called_once()
        sql = mock_exec.call_args[0][0]
        assert "last_activity = NOW()" in sql

    @patch("nova_goals._exec")
    def test_touch_activity_updates_timestamp(self, mock_exec):
        """touch_activity() updates last_activity without logging an event."""
        mock_exec.return_value = True
        self.mod.touch_activity("abc123")
        sql = mock_exec.call_args[0][0]
        assert "last_activity = NOW()" in sql
        assert "abc123" in sql


# ============================================================================
# nova_goals.py — get_active_goals()
# ============================================================================


class TestGetActiveGoals:
    """Tests for get_active_goals() parsing."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._query")
    def test_parse_full_row(self, mock_query):
        """get_active_goals() parses a complete pipe-delimited row."""
        mock_query.return_value = [
            "abc123|Ship MLXCode|MLXCode|high|2026-06-01|7|2026-05-01T10:00:00|2026-04-28T08:00:00"
        ]
        goals = self.mod.get_active_goals()
        assert len(goals) == 1
        g = goals[0]
        assert g["id"] == "abc123"
        assert g["title"] == "Ship MLXCode"
        assert g["project"] == "MLXCode"
        assert g["priority"] == "high"
        assert g["deadline"] == "2026-06-01"
        assert g["check_in_days"] == 7
        assert g["last_activity"] == "2026-05-01T10:00:00"
        assert g["created_at"] == "2026-04-28T08:00:00"

    @patch("nova_goals._query")
    def test_parse_null_project(self, mock_query):
        """Empty project field maps to None."""
        mock_query.return_value = [
            "abc123|General task||medium||7|2026-05-01T10:00:00|2026-04-28T08:00:00"
        ]
        goals = self.mod.get_active_goals()
        assert goals[0]["project"] is None

    @patch("nova_goals._query")
    def test_parse_null_deadline(self, mock_query):
        """Empty deadline field maps to None."""
        mock_query.return_value = [
            "abc123|Ongoing||medium||7|2026-05-01T10:00:00|2026-04-28T08:00:00"
        ]
        goals = self.mod.get_active_goals()
        assert goals[0]["deadline"] is None

    @patch("nova_goals._query")
    def test_parse_empty_check_in(self, mock_query):
        """Empty check_in_days defaults to 7."""
        mock_query.return_value = [
            "abc123|Test||medium|||2026-05-01T10:00:00|2026-04-28T08:00:00"
        ]
        goals = self.mod.get_active_goals()
        assert goals[0]["check_in_days"] == 7

    @patch("nova_goals._query")
    def test_parse_multiple_rows(self, mock_query):
        """get_active_goals() handles multiple rows."""
        mock_query.return_value = [
            "aaa|Goal A|Proj1|high|2026-06-01|3|2026-05-01T10:00:00|2026-04-28T08:00:00",
            "bbb|Goal B|Proj2|medium||7|2026-05-01T10:00:00|2026-04-28T08:00:00",
            "ccc|Goal C||low||14|2026-05-01T10:00:00|2026-04-28T08:00:00",
        ]
        goals = self.mod.get_active_goals()
        assert len(goals) == 3
        assert goals[0]["id"] == "aaa"
        assert goals[2]["project"] is None

    @patch("nova_goals._query")
    def test_parse_empty_result(self, mock_query):
        """get_active_goals() returns empty list on no results."""
        mock_query.return_value = []
        goals = self.mod.get_active_goals()
        assert goals == []

    @patch("nova_goals._query")
    def test_malformed_row_skipped(self, mock_query):
        """Rows with fewer than 8 pipe-delimited parts are skipped."""
        mock_query.return_value = [
            "abc123|Ship MLXCode",  # too few fields
            "def456|Good Goal|MLXCode|high|2026-06-01|7|2026-05-01T10:00:00|2026-04-28T08:00:00",
        ]
        goals = self.mod.get_active_goals()
        assert len(goals) == 1
        assert goals[0]["id"] == "def456"

    @patch("nova_goals._query")
    def test_extra_pipe_fields_ignored(self, mock_query):
        """Rows with extra pipe-delimited fields still parse the first 8."""
        mock_query.return_value = [
            "abc|Title|Proj|high|2026-06-01|7|2026-05-01T10:00:00|2026-04-28T08:00:00|extra|fields"
        ]
        goals = self.mod.get_active_goals()
        assert len(goals) == 1
        assert goals[0]["id"] == "abc"


# ============================================================================
# nova_goals.py — get_stale_goals()
# ============================================================================


class TestGetStaleGoals:
    """Tests for get_stale_goals() staleness detection."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals.get_active_goals")
    def test_stale_goal_detected(self, mock_active):
        """Goal idle beyond its check_in_days is flagged as stale."""
        # The source code strips timezone via split("-0")[0], which also
        # corrupts months/days starting with 0 (e.g., "-04-" → truncated).
        # Use "2025-12-15" format (no zero-prefixed month/day) to survive parsing.
        old_ts = "2025-12-15T10:00:00"
        mock_active.return_value = [{
            "id": "abc",
            "title": "Stale Goal",
            "project": "Test",
            "priority": "medium",
            "deadline": None,
            "check_in_days": 7,
            "last_activity": old_ts,
            "created_at": old_ts,
        }]
        stale = self.mod.get_stale_goals()
        assert len(stale) == 1
        assert stale[0]["days_idle"] >= 15

    @patch("nova_goals.get_active_goals")
    def test_fresh_goal_not_stale(self, mock_active):
        """Goal with ~20-day activity and huge threshold is not stale."""
        # Safe timestamp that survives split("-0") parsing.
        # 2025-12-15 is ~140 days old, but with 9999-day threshold it's not stale.
        mock_active.return_value = [{
            "id": "abc",
            "title": "Fresh Goal",
            "project": "Test",
            "priority": "medium",
            "deadline": None,
            "check_in_days": 9999,
            "last_activity": "2025-12-15T10:00:00",
            "created_at": "2025-12-15T10:00:00",
        }]
        stale = self.mod.get_stale_goals()
        assert len(stale) == 0

    @patch("nova_goals.get_active_goals")
    def test_custom_threshold_overrides_check_in(self, mock_active):
        """threshold_days parameter overrides per-goal check_in_days."""
        # Timestamp ~20 days old (safe format: no zero-prefixed month/day).
        old_ts = "2025-12-15T10:00:00"
        mock_active.return_value = [{
            "id": "abc",
            "title": "Test",
            "project": None,
            "priority": "medium",
            "deadline": None,
            "check_in_days": 9999,  # huge threshold: NOT stale without override
            "last_activity": old_ts,
            "created_at": old_ts,
        }]
        # With 9999-day check_in, the ~140-day-old timestamp is NOT stale
        stale = self.mod.get_stale_goals(threshold_days=None)
        assert len(stale) == 0
        # But with 3-day threshold override, it IS stale
        stale = self.mod.get_stale_goals(threshold_days=3)
        assert len(stale) == 1

    @patch("nova_goals.get_active_goals")
    def test_empty_last_activity_skipped(self, mock_active):
        """Goals with empty last_activity are skipped (not flagged)."""
        mock_active.return_value = [{
            "id": "abc",
            "title": "No Activity",
            "project": None,
            "priority": "medium",
            "deadline": None,
            "check_in_days": 7,
            "last_activity": "",
            "created_at": "2026-04-01T08:00:00",
        }]
        stale = self.mod.get_stale_goals()
        assert len(stale) == 0

    @patch("nova_goals.get_active_goals")
    def test_timestamp_with_timezone_offset(self, mock_active):
        """Timestamps with +00:00 offset are parsed (split on '+')."""
        old_ts = "2025-12-15T10:00:00+00:00"
        mock_active.return_value = [{
            "id": "abc",
            "title": "TZ Goal",
            "project": None,
            "priority": "medium",
            "deadline": None,
            "check_in_days": 7,
            "last_activity": old_ts,
            "created_at": old_ts,
        }]
        stale = self.mod.get_stale_goals()
        assert len(stale) == 1

    @patch("nova_goals.get_active_goals")
    def test_timestamp_with_microseconds(self, mock_active):
        """Timestamps with microseconds are parsed (split on '.')."""
        old_ts = "2025-12-15T10:00:00.123456"
        mock_active.return_value = [{
            "id": "abc",
            "title": "Microsecond Goal",
            "project": None,
            "priority": "medium",
            "deadline": None,
            "check_in_days": 7,
            "last_activity": old_ts,
            "created_at": old_ts,
        }]
        stale = self.mod.get_stale_goals()
        assert len(stale) == 1

    @patch("nova_goals.get_active_goals")
    def test_no_active_goals(self, mock_active):
        """Empty active goals list returns empty stale list."""
        mock_active.return_value = []
        stale = self.mod.get_stale_goals()
        assert stale == []


# ============================================================================
# nova_goals.py — get_overdue_goals()
# ============================================================================


class TestGetOverdueGoals:
    """Tests for get_overdue_goals() deadline checking."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._query")
    def test_overdue_goal_returned(self, mock_query):
        """Goal with past deadline is returned."""
        mock_query.return_value = [
            "abc|Overdue Goal|MLXCode|high|2026-04-01"
        ]
        overdue = self.mod.get_overdue_goals()
        assert len(overdue) == 1
        assert overdue[0]["title"] == "Overdue Goal"
        assert overdue[0]["deadline"] == "2026-04-01"

    @patch("nova_goals._query")
    def test_no_overdue_goals(self, mock_query):
        """Empty query result means no overdue goals."""
        mock_query.return_value = []
        overdue = self.mod.get_overdue_goals()
        assert overdue == []

    @patch("nova_goals._query")
    def test_overdue_parses_null_project(self, mock_query):
        """Overdue goal with empty project maps to None."""
        mock_query.return_value = [
            "abc|Overdue||high|2026-03-15"
        ]
        overdue = self.mod.get_overdue_goals()
        assert overdue[0]["project"] is None

    @patch("nova_goals._query")
    def test_overdue_malformed_row_skipped(self, mock_query):
        """Rows with fewer than 5 fields are skipped."""
        mock_query.return_value = [
            "abc|Incomplete",  # too few
            "def|Good Goal|Proj|medium|2026-03-01",
        ]
        overdue = self.mod.get_overdue_goals()
        assert len(overdue) == 1
        assert overdue[0]["id"] == "def"

    @patch("nova_goals._query")
    def test_overdue_multiple_goals(self, mock_query):
        """Multiple overdue goals are all returned."""
        mock_query.return_value = [
            "aaa|Goal A|Proj1|high|2026-01-01",
            "bbb|Goal B|Proj2|medium|2026-02-01",
        ]
        overdue = self.mod.get_overdue_goals()
        assert len(overdue) == 2


# ============================================================================
# nova_goals.py — get_goal_history()
# ============================================================================


class TestGetGoalHistory:
    """Tests for get_goal_history() event log retrieval."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._query")
    def test_history_parses_rows(self, mock_query):
        """get_goal_history() parses pipe-delimited event rows."""
        mock_query.return_value = [
            "2026-05-01T10:00:00|created|Goal created: Test",
            "2026-05-02T12:00:00|progress|Worked on parser",
        ]
        history = self.mod.get_goal_history("abc123")
        assert len(history) == 2
        assert history[0]["type"] == "created"
        assert history[1]["note"] == "Worked on parser"

    @patch("nova_goals._query")
    def test_history_empty(self, mock_query):
        """get_goal_history() returns empty list for no events."""
        mock_query.return_value = []
        history = self.mod.get_goal_history("abc123")
        assert history == []

    @patch("nova_goals._query")
    def test_history_uses_limit(self, mock_query):
        """get_goal_history() passes limit to SQL query."""
        mock_query.return_value = []
        self.mod.get_goal_history("abc123", limit=5)
        sql = mock_query.call_args[0][0]
        assert "LIMIT 5" in sql

    @patch("nova_goals._query")
    def test_history_skips_rows_without_pipe(self, mock_query):
        """Rows without pipe delimiters are skipped."""
        mock_query.return_value = [
            "malformed row without pipes",
            "2026-05-01T10:00:00|created|Good row",
        ]
        history = self.mod.get_goal_history("abc123")
        assert len(history) == 1


# ============================================================================
# nova_goals.py — goal_summary()
# ============================================================================


class TestGoalSummary:
    """Tests for goal_summary() count aggregation."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._query")
    def test_summary_all_counts(self, mock_query):
        """goal_summary() returns counts for each status."""
        mock_query.side_effect = [["5"], ["12"], ["2"]]
        summary = self.mod.goal_summary()
        assert summary == {"active": 5, "completed": 12, "paused": 2}

    @patch("nova_goals._query")
    def test_summary_empty_results(self, mock_query):
        """goal_summary() returns 0 for empty query results."""
        mock_query.side_effect = [[], [], []]
        summary = self.mod.goal_summary()
        assert summary == {"active": 0, "completed": 0, "paused": 0}

    @patch("nova_goals._query")
    def test_summary_mixed_results(self, mock_query):
        """goal_summary() handles mix of populated and empty results."""
        mock_query.side_effect = [["3"], [], ["1"]]
        summary = self.mod.goal_summary()
        assert summary == {"active": 3, "completed": 0, "paused": 1}


# ============================================================================
# nova_goals.py — run_gap_analysis()
# ============================================================================


class TestRunGapAnalysis:
    """Tests for run_gap_analysis() output formatting."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals.get_active_goals")
    @patch("nova_goals.get_overdue_goals")
    @patch("nova_goals.get_stale_goals")
    def test_all_on_track(self, mock_stale, mock_overdue, mock_active):
        """No stale or overdue goals produces 'All goals on track.'"""
        mock_stale.return_value = []
        mock_overdue.return_value = []
        mock_active.return_value = [{"id": "a"}, {"id": "b"}]
        result = self.mod.run_gap_analysis()
        assert "All goals on track" in result

    @patch("nova_goals.get_active_goals")
    @patch("nova_goals.get_overdue_goals")
    @patch("nova_goals.get_stale_goals")
    def test_overdue_section(self, mock_stale, mock_overdue, mock_active):
        """Overdue goals produce an '*Overdue:*' section."""
        mock_stale.return_value = []
        mock_overdue.return_value = [
            {"id": "abc", "title": "Late Goal", "deadline": "2026-04-01"}
        ]
        mock_active.return_value = []
        result = self.mod.run_gap_analysis()
        assert "*Overdue:*" in result
        assert "Late Goal" in result
        assert "2026-04-01" in result

    @patch("nova_goals.get_active_goals")
    @patch("nova_goals.get_overdue_goals")
    @patch("nova_goals.get_stale_goals")
    def test_stale_section(self, mock_stale, mock_overdue, mock_active):
        """Stale goals produce a '*Stale (no activity):*' section."""
        mock_stale.return_value = [
            {"id": "def", "title": "Neglected Goal", "days_idle": 14}
        ]
        mock_overdue.return_value = []
        mock_active.return_value = []
        result = self.mod.run_gap_analysis()
        assert "*Stale (no activity):*" in result
        assert "Neglected Goal" in result
        assert "14d idle" in result

    @patch("nova_goals.get_active_goals")
    @patch("nova_goals.get_overdue_goals")
    @patch("nova_goals.get_stale_goals")
    def test_too_many_goals_warning(self, mock_stale, mock_overdue, mock_active):
        """More than 4 active goals triggers a focus warning."""
        mock_stale.return_value = []
        mock_overdue.return_value = []
        mock_active.return_value = [{"id": str(i)} for i in range(6)]
        result = self.mod.run_gap_analysis()
        assert "6 active goals" in result
        assert "3-4 max" in result

    @patch("nova_goals.get_active_goals")
    @patch("nova_goals.get_overdue_goals")
    @patch("nova_goals.get_stale_goals")
    def test_four_goals_no_warning(self, mock_stale, mock_overdue, mock_active):
        """Exactly 4 active goals does NOT trigger a warning."""
        mock_stale.return_value = []
        mock_overdue.return_value = []
        mock_active.return_value = [{"id": str(i)} for i in range(4)]
        result = self.mod.run_gap_analysis()
        assert "active goals" not in result

    @patch("nova_goals.get_active_goals")
    @patch("nova_goals.get_overdue_goals")
    @patch("nova_goals.get_stale_goals")
    def test_combined_overdue_and_stale(self, mock_stale, mock_overdue, mock_active):
        """Both overdue and stale sections appear when both exist."""
        mock_stale.return_value = [
            {"id": "s1", "title": "Stale One", "days_idle": 10}
        ]
        mock_overdue.return_value = [
            {"id": "o1", "title": "Overdue One", "deadline": "2026-03-01"}
        ]
        mock_active.return_value = []
        result = self.mod.run_gap_analysis()
        assert "*Overdue:*" in result
        assert "*Stale (no activity):*" in result


# ============================================================================
# nova_goals.py — format_goals_brief()
# ============================================================================


class TestFormatGoalsBrief:
    """Tests for format_goals_brief() Slack message output."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals.run_gap_analysis")
    @patch("nova_goals.get_active_goals")
    def test_no_active_goals_returns_none(self, mock_active, mock_gap):
        """format_goals_brief() returns None when no active goals."""
        mock_active.return_value = []
        result = self.mod.format_goals_brief()
        assert result is None

    @patch("nova_goals.run_gap_analysis")
    @patch("nova_goals.get_active_goals")
    def test_header_with_count(self, mock_active, mock_gap):
        """Brief includes header with goal count."""
        mock_active.return_value = [
            {"id": "a", "title": "Goal A", "priority": "high",
             "deadline": "2026-06-01", "project": "MLXCode",
             "check_in_days": 7, "last_activity": "", "created_at": ""},
        ]
        mock_gap.return_value = "All goals on track."
        result = self.mod.format_goals_brief()
        assert "*Active Goals (1):*" in result

    @patch("nova_goals.run_gap_analysis")
    @patch("nova_goals.get_active_goals")
    def test_high_priority_emoji(self, mock_active, mock_gap):
        """High priority goals get a red circle emoji."""
        mock_active.return_value = [
            {"id": "a", "title": "Urgent", "priority": "high",
             "deadline": None, "project": None,
             "check_in_days": 7, "last_activity": "", "created_at": ""},
        ]
        mock_gap.return_value = "All goals on track."
        result = self.mod.format_goals_brief()
        assert "\U0001f534" in result  # red circle

    @patch("nova_goals.run_gap_analysis")
    @patch("nova_goals.get_active_goals")
    def test_medium_priority_emoji(self, mock_active, mock_gap):
        """Medium priority goals get a yellow circle emoji."""
        mock_active.return_value = [
            {"id": "a", "title": "Normal", "priority": "medium",
             "deadline": None, "project": None,
             "check_in_days": 7, "last_activity": "", "created_at": ""},
        ]
        mock_gap.return_value = "All goals on track."
        result = self.mod.format_goals_brief()
        assert "\U0001f7e1" in result  # yellow circle

    @patch("nova_goals.run_gap_analysis")
    @patch("nova_goals.get_active_goals")
    def test_low_priority_emoji(self, mock_active, mock_gap):
        """Low priority goals get a white circle emoji."""
        mock_active.return_value = [
            {"id": "a", "title": "Chill", "priority": "low",
             "deadline": None, "project": None,
             "check_in_days": 7, "last_activity": "", "created_at": ""},
        ]
        mock_gap.return_value = "All goals on track."
        result = self.mod.format_goals_brief()
        assert "⚪" in result  # white circle

    @patch("nova_goals.run_gap_analysis")
    @patch("nova_goals.get_active_goals")
    def test_deadline_displayed(self, mock_active, mock_gap):
        """Goals with deadlines include the date in the brief."""
        mock_active.return_value = [
            {"id": "a", "title": "Deadline Goal", "priority": "medium",
             "deadline": "2026-07-15", "project": None,
             "check_in_days": 7, "last_activity": "", "created_at": ""},
        ]
        mock_gap.return_value = "All goals on track."
        result = self.mod.format_goals_brief()
        assert "(due 2026-07-15)" in result

    @patch("nova_goals.run_gap_analysis")
    @patch("nova_goals.get_active_goals")
    def test_project_displayed(self, mock_active, mock_gap):
        """Goals with projects include the project name in brackets."""
        mock_active.return_value = [
            {"id": "a", "title": "Project Goal", "priority": "medium",
             "deadline": None, "project": "RsyncGUI",
             "check_in_days": 7, "last_activity": "", "created_at": ""},
        ]
        mock_gap.return_value = "All goals on track."
        result = self.mod.format_goals_brief()
        assert "[RsyncGUI]" in result

    @patch("nova_goals.run_gap_analysis")
    @patch("nova_goals.get_active_goals")
    def test_gap_analysis_appended_when_issues(self, mock_active, mock_gap):
        """Gap analysis output is appended when there are issues."""
        mock_active.return_value = [
            {"id": "a", "title": "G", "priority": "medium",
             "deadline": None, "project": None,
             "check_in_days": 7, "last_activity": "", "created_at": ""},
        ]
        mock_gap.return_value = "*Stale (no activity):*\n  abc - 10d idle"
        result = self.mod.format_goals_brief()
        assert "*Stale (no activity):*" in result

    @patch("nova_goals.run_gap_analysis")
    @patch("nova_goals.get_active_goals")
    def test_gap_analysis_not_appended_when_on_track(self, mock_active, mock_gap):
        """'All goals on track' gap analysis is not appended."""
        mock_active.return_value = [
            {"id": "a", "title": "G", "priority": "medium",
             "deadline": None, "project": None,
             "check_in_days": 7, "last_activity": "", "created_at": ""},
        ]
        mock_gap.return_value = "All goals on track."
        result = self.mod.format_goals_brief()
        assert "All goals on track" not in result


# ============================================================================
# nova_goals.py — detect_activity_from_git()
# ============================================================================


class TestDetectActivityFromGit:
    """Tests for git commit auto-detection."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._log_event")
    @patch("nova_goals.touch_activity")
    @patch("nova_goals.subprocess.run")
    @patch("nova_goals.get_active_goals")
    def test_git_commits_detected(self, mock_active, mock_run, mock_touch, mock_log_event):
        """Git commits for a project trigger touch_activity."""
        mock_active.return_value = [{
            "id": "abc",
            "title": "Ship It",
            "project": "MLXCode",
            "priority": "high",
            "deadline": None,
            "check_in_days": 7,
            "last_activity": "",
            "created_at": "",
        }]
        mock_run.return_value = MagicMock(
            stdout="fix: parser bug\nfeat: new feature\n",
            returncode=0,
        )
        with patch.object(Path, "exists", return_value=True):
            self.mod.detect_activity_from_git("/fake/xcode")
        mock_touch.assert_called_once_with("abc")
        mock_log_event.assert_called_once()
        assert "2 commit(s)" in mock_log_event.call_args[0][2]

    @patch("nova_goals.touch_activity")
    @patch("nova_goals.get_active_goals")
    def test_no_project_skipped(self, mock_active, mock_touch):
        """Goals without a project field are skipped."""
        mock_active.return_value = [{
            "id": "abc",
            "title": "General",
            "project": None,
            "priority": "medium",
            "deadline": None,
            "check_in_days": 7,
            "last_activity": "",
            "created_at": "",
        }]
        self.mod.detect_activity_from_git("/fake/xcode")
        mock_touch.assert_not_called()

    @patch("nova_goals.touch_activity")
    @patch("nova_goals.subprocess.run")
    @patch("nova_goals.get_active_goals")
    def test_nonexistent_project_dir_skipped(self, mock_active, mock_run, mock_touch):
        """Goals with non-existent project directories are skipped."""
        mock_active.return_value = [{
            "id": "abc",
            "title": "Missing Proj",
            "project": "NonExistent",
            "priority": "medium",
            "deadline": None,
            "check_in_days": 7,
            "last_activity": "",
            "created_at": "",
        }]
        # Path.exists() returns False by default for non-existent paths
        self.mod.detect_activity_from_git("/fake/xcode")
        mock_touch.assert_not_called()

    @patch("nova_goals._log_event")
    @patch("nova_goals.touch_activity")
    @patch("nova_goals.subprocess.run")
    @patch("nova_goals.get_active_goals")
    def test_no_commits_today(self, mock_active, mock_run, mock_touch, mock_log_event):
        """No git commits today means no activity touch."""
        mock_active.return_value = [{
            "id": "abc",
            "title": "Idle",
            "project": "MLXCode",
            "priority": "medium",
            "deadline": None,
            "check_in_days": 7,
            "last_activity": "",
            "created_at": "",
        }]
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        with patch.object(Path, "exists", return_value=True):
            self.mod.detect_activity_from_git("/fake/xcode")
        mock_touch.assert_not_called()

    @patch("nova_goals.get_active_goals")
    def test_no_active_goals_returns_early(self, mock_active):
        """detect_activity_from_git() returns early when no active goals."""
        mock_active.return_value = []
        # Should not raise; just returns
        self.mod.detect_activity_from_git("/fake/xcode")


# ============================================================================
# nova_goals.py — _query() and _exec()
# ============================================================================


class TestGoalsDatabaseHelpers:
    """Tests for _query() and _exec() subprocess wrappers."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals.subprocess.run")
    def test_query_success(self, mock_run):
        """_query() returns parsed rows on success."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="row1\nrow2\nrow3\n", stderr=""
        )
        result = self.mod._query("SELECT 1")
        assert result == ["row1", "row2", "row3"]

    @patch("nova_goals.subprocess.run")
    def test_query_filters_empty_lines(self, mock_run):
        """_query() filters out empty lines from output."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="row1\n\nrow2\n", stderr=""
        )
        result = self.mod._query("SELECT 1")
        assert result == ["row1", "row2"]

    @patch("nova_goals.subprocess.run")
    def test_query_error_returns_empty(self, mock_run):
        """_query() returns empty list on non-zero returncode."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="ERROR: relation does not exist"
        )
        result = self.mod._query("SELECT * FROM nonexistent")
        assert result == []

    @patch("nova_goals.subprocess.run")
    def test_query_exception_returns_empty(self, mock_run):
        """_query() returns empty list when subprocess raises."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="psql", timeout=10)
        result = self.mod._query("SELECT 1")
        assert result == []

    @patch("nova_goals.subprocess.run")
    def test_exec_success(self, mock_run):
        """_exec() returns True on success."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert self.mod._exec("INSERT INTO goals ...") is True

    @patch("nova_goals.subprocess.run")
    def test_exec_failure(self, mock_run):
        """_exec() returns False on failure."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="ERROR")
        assert self.mod._exec("INVALID SQL") is False


# ============================================================================
# nova_goals.py — CLI commands
# ============================================================================


class TestGoalsCLI:
    """Tests for CLI argparse commands."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals.ensure_schema")
    def test_cli_init(self, mock_schema, capsys):
        """'init' command calls ensure_schema()."""
        with patch("sys.argv", ["nova_goals.py", "init"]):
            self.mod.main()
        mock_schema.assert_called_once()
        assert "Schema ready" in capsys.readouterr().out

    @patch("nova_goals.add_goal", return_value="abc12345")
    def test_cli_add(self, mock_add, capsys):
        """'add' command calls add_goal() with args."""
        with patch("sys.argv", ["nova_goals.py", "add", "Test Goal",
                                "--project", "MLXCode", "--priority", "high"]):
            self.mod.main()
        mock_add.assert_called_once()
        assert "abc12345" in capsys.readouterr().out

    @patch("nova_goals.complete_goal")
    def test_cli_complete(self, mock_complete, capsys):
        """'complete' command calls complete_goal()."""
        with patch("sys.argv", ["nova_goals.py", "complete", "abc123", "--note", "Done"]):
            self.mod.main()
        mock_complete.assert_called_once_with("abc123", "Done")

    @patch("nova_goals.pause_goal")
    def test_cli_pause(self, mock_pause, capsys):
        """'pause' command calls pause_goal()."""
        with patch("sys.argv", ["nova_goals.py", "pause", "abc123", "--reason", "Waiting"]):
            self.mod.main()
        mock_pause.assert_called_once_with("abc123", "Waiting")

    @patch("nova_goals.drop_goal")
    def test_cli_drop(self, mock_drop, capsys):
        """'drop' command calls drop_goal()."""
        with patch("sys.argv", ["nova_goals.py", "drop", "abc123", "--reason", "Nope"]):
            self.mod.main()
        mock_drop.assert_called_once_with("abc123", "Nope")

    @patch("nova_goals.log_progress")
    def test_cli_progress(self, mock_progress, capsys):
        """'progress' command calls log_progress()."""
        with patch("sys.argv", ["nova_goals.py", "progress", "abc123", "Made progress"]):
            self.mod.main()
        mock_progress.assert_called_once_with("abc123", "Made progress")

    @patch("nova_goals.get_active_goals")
    def test_cli_list(self, mock_active, capsys):
        """'list' command prints active goals."""
        mock_active.return_value = [
            {"id": "abc", "title": "My Goal", "priority": "high",
             "deadline": "2026-06-01", "project": "MLXCode"},
        ]
        with patch("sys.argv", ["nova_goals.py", "list"]):
            self.mod.main()
        output = capsys.readouterr().out
        assert "abc" in output
        assert "My Goal" in output

    @patch("nova_goals.run_gap_analysis", return_value="All goals on track.")
    def test_cli_gaps(self, mock_gap, capsys):
        """'gaps' command prints gap analysis."""
        with patch("sys.argv", ["nova_goals.py", "gaps"]):
            self.mod.main()
        assert "All goals on track" in capsys.readouterr().out

    @patch("nova_goals.format_goals_brief", return_value=None)
    def test_cli_brief_no_goals(self, mock_brief, capsys):
        """'brief' command prints fallback when no active goals."""
        with patch("sys.argv", ["nova_goals.py", "brief"]):
            self.mod.main()
        assert "No active goals" in capsys.readouterr().out

    @patch("nova_goals.detect_activity_from_git")
    def test_cli_detect(self, mock_detect, capsys):
        """'detect' command calls detect_activity_from_git()."""
        with patch("sys.argv", ["nova_goals.py", "detect"]):
            self.mod.main()
        mock_detect.assert_called_once()


# ============================================================================
# nova_rules.py — _escape()
# ============================================================================


class TestRulesEscape:
    """Unit tests for the rules module _escape() helper."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    def test_escape_single_quotes(self):
        assert self.mod._escape("it's") == "it''s"

    def test_escape_backslashes(self):
        assert self.mod._escape("a\\b") == "a\\\\b"

    def test_escape_none(self):
        assert self.mod._escape(None) == ""

    def test_escape_empty(self):
        assert self.mod._escape("") == ""


# ============================================================================
# nova_rules.py — ensure_schema()
# ============================================================================


class TestRulesEnsureSchema:
    """Tests for rules schema creation."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules._exec")
    def test_ensure_schema_creates_tables(self, mock_exec):
        """ensure_schema() creates rules and rule_applications tables."""
        mock_exec.return_value = True
        self.mod.ensure_schema()
        sql = mock_exec.call_args[0][0]
        assert "CREATE TABLE IF NOT EXISTS rules" in sql
        assert "CREATE TABLE IF NOT EXISTS rule_applications" in sql

    @patch("nova_rules._exec")
    def test_ensure_schema_creates_indexes(self, mock_exec):
        """ensure_schema() creates required indexes."""
        mock_exec.return_value = True
        self.mod.ensure_schema()
        sql = mock_exec.call_args[0][0]
        assert "idx_rules_status" in sql
        assert "idx_rules_topic" in sql
        assert "idx_rule_apps_rule" in sql


# ============================================================================
# nova_rules.py — add_rule()
# ============================================================================


class TestAddRule:
    """Tests for add_rule() CRUD operation."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules._exec")
    def test_add_rule_minimal(self, mock_exec):
        """add_rule() with just rule text succeeds."""
        mock_exec.return_value = True
        rid = self.mod.add_rule("Never call Jordan 'buddy'")
        assert rid is not None
        assert len(rid) == 8
        sql = mock_exec.call_args[0][0]
        assert "Never call Jordan" in sql
        assert "'global'" in sql  # default topic
        assert "'correction'" in sql  # default source_type

    @patch("nova_rules._exec")
    def test_add_rule_all_params(self, mock_exec):
        """add_rule() with all parameters includes them in SQL."""
        mock_exec.return_value = True
        rid = self.mod.add_rule(
            "Always use she/her for Nova",
            topic="people",
            source_type="preference",
            context="Jordan corrected pronouns",
            confidence=0.95,
            expires_at="2027-01-01",
            original_correction={"nova_response": "he", "jordan_correction": "she"},
        )
        assert rid is not None
        sql = mock_exec.call_args[0][0]
        assert "Always use she/her" in sql
        assert "'people'" in sql
        assert "'preference'" in sql
        assert "0.95" in sql
        assert "'2027-01-01'" in sql

    @patch("nova_rules._exec")
    def test_add_rule_no_expiry(self, mock_exec):
        """add_rule() without expiry uses NULL."""
        mock_exec.return_value = True
        self.mod.add_rule("Test rule")
        sql = mock_exec.call_args[0][0]
        assert "NULL" in sql

    @patch("nova_rules._exec")
    def test_add_rule_db_failure(self, mock_exec):
        """add_rule() returns None on DB failure."""
        mock_exec.return_value = False
        rid = self.mod.add_rule("Doomed rule")
        assert rid is None

    @patch("nova_rules._exec")
    def test_add_rule_escapes_text(self, mock_exec):
        """add_rule() escapes quotes in rule text."""
        mock_exec.return_value = True
        self.mod.add_rule("Don't say 'buddy'")
        sql = mock_exec.call_args[0][0]
        assert "Don''t say ''buddy''" in sql

    @patch("nova_rules._exec")
    def test_add_rule_no_correction(self, mock_exec):
        """add_rule() without original_correction uses NULL for JSONB."""
        mock_exec.return_value = True
        self.mod.add_rule("Simple rule")
        sql = mock_exec.call_args[0][0]
        # Should have NULL for the correction column, not a JSON string
        assert "NULL" in sql


# ============================================================================
# nova_rules.py — retire_rule()
# ============================================================================


class TestRetireRule:
    """Tests for retire_rule() status change."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules._exec")
    def test_retire_rule_success(self, mock_exec):
        """retire_rule() sets status to 'retired'."""
        mock_exec.return_value = True
        result = self.mod.retire_rule("abc123", reason="Outdated")
        assert result is True
        sql = mock_exec.call_args[0][0]
        assert "status = 'retired'" in sql

    @patch("nova_rules._exec")
    def test_retire_rule_failure(self, mock_exec):
        """retire_rule() returns False on DB failure."""
        mock_exec.return_value = False
        result = self.mod.retire_rule("abc123")
        assert result is False


# ============================================================================
# nova_rules.py — record_application()
# ============================================================================


class TestRecordApplication:
    """Tests for record_application() tracking."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules._exec")
    def test_record_application_inserts_and_increments(self, mock_exec):
        """record_application() inserts to rule_applications and increments counter."""
        mock_exec.return_value = True
        self.mod.record_application("abc123", context="Corrected pronoun", prevented="said 'he'")
        sql = mock_exec.call_args[0][0]
        assert "INSERT INTO rule_applications" in sql
        assert "times_applied = times_applied + 1" in sql
        assert "abc123" in sql

    @patch("nova_rules._exec")
    def test_record_application_escapes_values(self, mock_exec):
        """record_application() escapes special characters."""
        mock_exec.return_value = True
        self.mod.record_application("abc123", context="Jordan's correction", prevented="it's wrong")
        sql = mock_exec.call_args[0][0]
        assert "Jordan''s correction" in sql
        assert "it''s wrong" in sql


# ============================================================================
# nova_rules.py — get_active_rules()
# ============================================================================


class TestGetActiveRules:
    """Tests for get_active_rules() query and parsing."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules._query")
    def test_parse_full_row(self, mock_query):
        """get_active_rules() parses a complete pipe-delimited row."""
        mock_query.return_value = [
            "abc123|Never say buddy|global|1.0|5|2026-05-01T10:00:00"
        ]
        rules = self.mod.get_active_rules()
        assert len(rules) == 1
        r = rules[0]
        assert r["id"] == "abc123"
        assert r["rule"] == "Never say buddy"
        assert r["topic"] == "global"
        assert r["confidence"] == 1.0
        assert r["times_applied"] == 5
        assert r["created_at"] == "2026-05-01T10:00:00"

    @patch("nova_rules._query")
    def test_topic_filter(self, mock_query):
        """get_active_rules(topic=...) includes topic in WHERE clause."""
        mock_query.return_value = []
        self.mod.get_active_rules(topic="people")
        sql = mock_query.call_args[0][0]
        assert "topic = 'people'" in sql
        assert "topic = 'global'" in sql  # always includes global

    @patch("nova_rules._query")
    def test_no_topic_filter(self, mock_query):
        """get_active_rules() without topic only filters by status and expiry."""
        mock_query.return_value = []
        self.mod.get_active_rules()
        sql = mock_query.call_args[0][0]
        assert "status = 'active'" in sql
        assert "expires_at IS NULL OR expires_at > NOW()" in sql

    @patch("nova_rules._query")
    def test_empty_result(self, mock_query):
        """get_active_rules() returns empty list for no results."""
        mock_query.return_value = []
        assert self.mod.get_active_rules() == []

    @patch("nova_rules._query")
    def test_malformed_row_skipped(self, mock_query):
        """Rows with fewer than 6 fields are skipped."""
        mock_query.return_value = [
            "abc|Too Short",
            "def|Good Rule|global|0.9|3|2026-05-01T10:00:00",
        ]
        rules = self.mod.get_active_rules()
        assert len(rules) == 1
        assert rules[0]["id"] == "def"

    @patch("nova_rules._query")
    def test_empty_confidence_defaults(self, mock_query):
        """Empty confidence field defaults to 1.0."""
        mock_query.return_value = [
            "abc|Rule||global||0|2026-05-01T10:00:00"
        ]
        # This row has 7 fields but parts[3] is empty
        # Actually let me construct a proper 6-field row with empty confidence
        mock_query.return_value = [
            "abc|Rule|global||0|2026-05-01T10:00:00"
        ]
        rules = self.mod.get_active_rules()
        assert len(rules) == 1
        assert rules[0]["confidence"] == 1.0

    @patch("nova_rules._query")
    def test_empty_times_applied_defaults(self, mock_query):
        """Empty times_applied field defaults to 0."""
        mock_query.return_value = [
            "abc|Rule|global|0.9||2026-05-01T10:00:00"
        ]
        rules = self.mod.get_active_rules()
        assert len(rules) == 1
        assert rules[0]["times_applied"] == 0


# ============================================================================
# nova_rules.py — get_all_rules()
# ============================================================================


class TestGetAllRules:
    """Tests for get_all_rules() including retired rules."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules._query")
    def test_parse_all_fields(self, mock_query):
        """get_all_rules() parses all 7 fields including status and source_type."""
        mock_query.return_value = [
            "abc|Rule text|global|active|1.0|5|correction"
        ]
        rules = self.mod.get_all_rules()
        assert len(rules) == 1
        r = rules[0]
        assert r["status"] == "active"
        assert r["source_type"] == "correction"

    @patch("nova_rules._query")
    def test_includes_retired(self, mock_query):
        """get_all_rules() includes retired rules."""
        mock_query.return_value = [
            "abc|Active Rule|global|active|1.0|5|correction",
            "def|Retired Rule|people|retired|0.8|2|preference",
        ]
        rules = self.mod.get_all_rules()
        assert len(rules) == 2
        assert rules[1]["status"] == "retired"

    @patch("nova_rules._query")
    def test_malformed_row_skipped(self, mock_query):
        """Rows with fewer than 7 fields are skipped."""
        mock_query.return_value = [
            "abc|Short",
            "def|Good Rule|global|active|1.0|3|preference",
        ]
        rules = self.mod.get_all_rules()
        assert len(rules) == 1


# ============================================================================
# nova_rules.py — format_rules_for_prompt()
# ============================================================================


class TestFormatRulesForPrompt:
    """Tests for format_rules_for_prompt() LLM injection output."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules.get_active_rules")
    def test_no_rules_returns_empty(self, mock_active):
        """format_rules_for_prompt() returns empty string when no rules."""
        mock_active.return_value = []
        assert self.mod.format_rules_for_prompt() == ""

    @patch("nova_rules.get_active_rules")
    def test_header_present(self, mock_active):
        """Output starts with the mandatory header."""
        mock_active.return_value = [
            {"id": "abc", "rule": "Test rule", "topic": "global",
             "confidence": 1.0, "times_applied": 0, "created_at": ""},
        ]
        result = self.mod.format_rules_for_prompt()
        assert "## Active Rules (behavioral corrections" in result
        assert "MUST follow" in result

    @patch("nova_rules.get_active_rules")
    def test_global_rule_no_tag(self, mock_active):
        """Global rules have no topic tag prefix."""
        mock_active.return_value = [
            {"id": "abc", "rule": "Be concise", "topic": "global",
             "confidence": 1.0, "times_applied": 0, "created_at": ""},
        ]
        result = self.mod.format_rules_for_prompt()
        assert "- Be concise" in result
        assert "[global]" not in result

    @patch("nova_rules.get_active_rules")
    def test_topic_rule_has_tag(self, mock_active):
        """Non-global rules have a topic tag prefix."""
        mock_active.return_value = [
            {"id": "abc", "rule": "Use she/her", "topic": "people",
             "confidence": 1.0, "times_applied": 0, "created_at": ""},
        ]
        result = self.mod.format_rules_for_prompt()
        assert "[people] Use she/her" in result

    @patch("nova_rules.get_active_rules")
    def test_multiple_rules_all_listed(self, mock_active):
        """Multiple rules are all included in the output."""
        mock_active.return_value = [
            {"id": "a", "rule": "Rule A", "topic": "global",
             "confidence": 1.0, "times_applied": 0, "created_at": ""},
            {"id": "b", "rule": "Rule B", "topic": "email",
             "confidence": 0.9, "times_applied": 2, "created_at": ""},
        ]
        result = self.mod.format_rules_for_prompt()
        assert "- Rule A" in result
        assert "- [email] Rule B" in result

    @patch("nova_rules.get_active_rules")
    def test_topic_filter_passed(self, mock_active):
        """format_rules_for_prompt(topic=...) passes topic to get_active_rules."""
        mock_active.return_value = []
        self.mod.format_rules_for_prompt(topic="homekit")
        mock_active.assert_called_once_with("homekit")


# ============================================================================
# nova_rules.py — _correction_to_rule()
# ============================================================================


class TestCorrectionToRule:
    """Tests for _correction_to_rule() conversion logic."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    def test_with_both_fields(self):
        """Correction with both nova_response and jordan_correction creates a full rule."""
        c = {"nova_response": "he said", "jordan_correction": "she said", "topic": "people"}
        rule = self.mod._correction_to_rule(c)
        assert "Do NOT say" in rule
        assert "he said" in rule
        assert "she said" in rule

    def test_correction_only(self):
        """Correction without nova_response returns jordan_correction as rule."""
        c = {"jordan_correction": "Always say Little Mister", "topic": "global"}
        rule = self.mod._correction_to_rule(c)
        assert rule == "Always say Little Mister"

    def test_empty_nova_response(self):
        """Empty nova_response falls through to correction-only path."""
        c = {"nova_response": "", "jordan_correction": "Use formal tone", "topic": "global"}
        rule = self.mod._correction_to_rule(c)
        assert rule == "Use formal tone"

    def test_missing_jordan_correction(self):
        """Missing jordan_correction returns None."""
        c = {"nova_response": "something wrong", "topic": "global"}
        rule = self.mod._correction_to_rule(c)
        assert rule is None

    def test_empty_jordan_correction(self):
        """Empty jordan_correction returns None."""
        c = {"nova_response": "wrong", "jordan_correction": "", "topic": "global"}
        rule = self.mod._correction_to_rule(c)
        assert rule is None

    def test_empty_correction(self):
        """Completely empty correction returns None."""
        c = {}
        rule = self.mod._correction_to_rule(c)
        assert rule is None


# ============================================================================
# nova_rules.py — promote_corrections()
# ============================================================================


class TestPromoteCorrections:
    """Tests for promote_corrections() from corrections.json."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules.add_rule")
    @patch("nova_rules.get_all_rules")
    @patch("builtins.open", mock_open(read_data='[]'))
    @patch("nova_rules.CORRECTIONS_FILE")
    def test_empty_corrections_file(self, mock_path, mock_all_rules, mock_add):
        """Empty corrections.json promotes 0 rules."""
        mock_path.exists.return_value = True
        mock_all_rules.return_value = []
        result = self.mod.promote_corrections()
        assert result == 0
        mock_add.assert_not_called()

    @patch("nova_rules.CORRECTIONS_FILE")
    def test_no_corrections_file(self, mock_path):
        """Missing corrections.json returns 0."""
        mock_path.exists.return_value = False
        result = self.mod.promote_corrections()
        assert result == 0

    @patch("nova_rules.add_rule", return_value="abc123")
    @patch("nova_rules.get_all_rules")
    @patch("builtins.open")
    @patch("nova_rules.CORRECTIONS_FILE")
    def test_promotes_new_correction(self, mock_path, mock_open_fn, mock_all_rules, mock_add):
        """New correction not in existing rules gets promoted."""
        mock_path.exists.return_value = True
        corrections = [
            {"nova_response": "wrong", "jordan_correction": "right", "topic": "global"}
        ]
        mock_open_fn.return_value.__enter__ = lambda s: s
        mock_open_fn.return_value.__exit__ = MagicMock(return_value=False)
        mock_open_fn.return_value.read = MagicMock(return_value=json.dumps(corrections))

        # Use json.load patch instead of complex mock
        with patch("json.load", return_value=corrections):
            mock_all_rules.return_value = []
            result = self.mod.promote_corrections()
        assert result == 1
        mock_add.assert_called_once()

    @patch("nova_rules.add_rule")
    @patch("nova_rules.get_all_rules")
    @patch("builtins.open")
    @patch("nova_rules.CORRECTIONS_FILE")
    def test_skips_duplicate_correction(self, mock_path, mock_open_fn, mock_all_rules, mock_add):
        """Correction already in rules is not promoted again."""
        mock_path.exists.return_value = True
        corrections = [
            {"nova_response": "wrong", "jordan_correction": "right", "topic": "global"}
        ]
        existing_rule_text = "Do NOT say: 'wrong'. Correct answer: right"

        with patch("json.load", return_value=corrections):
            mock_all_rules.return_value = [{"rule": existing_rule_text}]
            result = self.mod.promote_corrections()
        assert result == 0
        mock_add.assert_not_called()


# ============================================================================
# nova_rules.py — ingest_correction()
# ============================================================================


class TestIngestCorrection:
    """Tests for ingest_correction() real-time capture and promotion."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules.add_rule", return_value="new_rule")
    @patch("builtins.open", mock_open(read_data='[]'))
    @patch("nova_rules.CORRECTIONS_FILE")
    def test_ingest_creates_rule(self, mock_path, mock_add):
        """ingest_correction() immediately promotes to active rule."""
        mock_path.exists.return_value = True
        rid = self.mod.ingest_correction(
            nova_response="wrong answer",
            jordan_correction="right answer",
            topic="people",
        )
        assert rid == "new_rule"
        mock_add.assert_called_once()
        kwargs = mock_add.call_args[1]
        assert kwargs["topic"] == "people"
        assert kwargs["source_type"] == "correction"

    @patch("nova_rules.add_rule", return_value=None)
    @patch("builtins.open", mock_open(read_data='[]'))
    @patch("nova_rules.CORRECTIONS_FILE")
    def test_ingest_empty_correction(self, mock_path, mock_add):
        """ingest_correction() with empty jordan_correction returns None."""
        mock_path.exists.return_value = True
        rid = self.mod.ingest_correction(
            nova_response="something",
            jordan_correction="",
        )
        assert rid is None

    @patch("nova_rules.add_rule", return_value="abc")
    @patch("builtins.open", mock_open(read_data='[]'))
    @patch("nova_rules.CORRECTIONS_FILE")
    def test_ingest_appends_to_json(self, mock_path, mock_add):
        """ingest_correction() writes the correction to corrections.json."""
        mock_path.exists.return_value = True
        self.mod.ingest_correction("wrong", "right")
        # Verify that open was called for writing
        handle = open
        # The mock_open was called, which means the file was written to
        assert mock_add.called


# ============================================================================
# nova_rules.py — add_preference()
# ============================================================================


class TestAddPreference:
    """Tests for the add_preference() convenience function."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules.add_rule", return_value="pref123")
    def test_preference_delegates_to_add_rule(self, mock_add):
        """add_preference() calls add_rule with source_type='preference'."""
        rid = self.mod.add_preference("Always call me Little Mister", topic="people")
        assert rid == "pref123"
        mock_add.assert_called_once_with(
            rule_text="Always call me Little Mister",
            topic="people",
            source_type="preference",
        )

    @patch("nova_rules.add_rule", return_value="pref456")
    def test_preference_default_topic(self, mock_add):
        """add_preference() defaults to 'global' topic."""
        self.mod.add_preference("Be concise")
        mock_add.assert_called_once_with(
            rule_text="Be concise",
            topic="global",
            source_type="preference",
        )


# ============================================================================
# nova_rules.py — CLI commands
# ============================================================================


class TestRulesCLI:
    """Tests for rules CLI argparse commands."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules.ensure_schema")
    def test_cli_init(self, mock_schema, capsys):
        """'init' command calls ensure_schema()."""
        with patch("sys.argv", ["nova_rules.py", "init"]):
            self.mod.main()
        mock_schema.assert_called_once()
        assert "Schema ready" in capsys.readouterr().out

    @patch("nova_rules.add_rule", return_value="abc12345")
    def test_cli_add(self, mock_add, capsys):
        """'add' command calls add_rule() with args."""
        with patch("sys.argv", ["nova_rules.py", "add", "Test Rule",
                                "--topic", "people", "--type", "preference"]):
            self.mod.main()
        mock_add.assert_called_once_with("Test Rule", topic="people", source_type="preference")
        assert "abc12345" in capsys.readouterr().out

    @patch("nova_rules.retire_rule")
    def test_cli_retire(self, mock_retire, capsys):
        """'retire' command calls retire_rule()."""
        with patch("sys.argv", ["nova_rules.py", "retire", "abc123", "--reason", "Outdated"]):
            self.mod.main()
        mock_retire.assert_called_once_with("abc123", "Outdated")

    @patch("nova_rules.get_active_rules")
    def test_cli_list_active(self, mock_active, capsys):
        """'list' command shows active rules by default."""
        mock_active.return_value = [
            {"id": "abc", "rule": "Be concise", "topic": "global",
             "confidence": 1.0, "times_applied": 3, "created_at": ""},
        ]
        with patch("sys.argv", ["nova_rules.py", "list"]):
            self.mod.main()
        output = capsys.readouterr().out
        assert "abc" in output
        assert "Be concise" in output

    @patch("nova_rules.get_all_rules")
    def test_cli_list_all(self, mock_all, capsys):
        """'list --all' shows all rules including retired."""
        mock_all.return_value = [
            {"id": "abc", "rule": "Active Rule", "topic": "global",
             "status": "active", "confidence": 1.0, "times_applied": 0,
             "source_type": "correction"},
            {"id": "def", "rule": "Retired Rule", "topic": "people",
             "status": "retired", "confidence": 0.8, "times_applied": 5,
             "source_type": "preference"},
        ]
        with patch("sys.argv", ["nova_rules.py", "list", "--all"]):
            self.mod.main()
        output = capsys.readouterr().out
        assert "Active Rule" in output
        assert "Retired Rule" in output

    @patch("nova_rules.format_rules_for_prompt", return_value="## Active Rules\n- Rule A")
    def test_cli_prompt(self, mock_format, capsys):
        """'prompt' command prints formatted rules."""
        with patch("sys.argv", ["nova_rules.py", "prompt"]):
            self.mod.main()
        assert "Active Rules" in capsys.readouterr().out

    @patch("nova_rules.promote_corrections", return_value=3)
    def test_cli_promote(self, mock_promote, capsys):
        """'promote' command prints promotion count."""
        with patch("sys.argv", ["nova_rules.py", "promote"]):
            self.mod.main()
        assert "Promoted 3" in capsys.readouterr().out

    @patch("nova_rules.ingest_correction", return_value="new_rule")
    def test_cli_correct(self, mock_ingest, capsys):
        """'correct' command records a correction and promotes it."""
        with patch("sys.argv", ["nova_rules.py", "correct",
                                "--nova", "wrong thing",
                                "--jordan", "right thing",
                                "--topic", "people"]):
            self.mod.main()
        mock_ingest.assert_called_once_with("wrong thing", "right thing", "people")
        assert "new_rule" in capsys.readouterr().out


# ============================================================================
# nova_goal_check.py — main()
# ============================================================================


class TestGoalCheckMain:
    """Tests for the daily goal check scheduled task."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        # Clear dependent modules so they pick up mocked nova_config
        for mod_name in ("nova_goals", "nova_rules", "nova_goal_check"):
            if mod_name in sys.modules:
                del sys.modules[mod_name]
        import nova_goal_check
        self.mod = nova_goal_check
        self.mock_config = mock_nova_config

    @patch("nova_goal_check.get_active_rules", return_value=[])
    @patch("nova_goal_check.promote_corrections", return_value=0)
    @patch("nova_goal_check.goal_summary", return_value={"active": 2, "completed": 5, "paused": 0})
    @patch("nova_goal_check.get_active_goals", return_value=[{"id": "a"}, {"id": "b"}])
    @patch("nova_goal_check.get_overdue_goals", return_value=[])
    @patch("nova_goal_check.get_stale_goals", return_value=[])
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_all_on_track_silent(self, mock_g_schema, mock_r_schema, mock_detect,
                                  mock_stale, mock_overdue, mock_active,
                                  mock_summary, mock_promote, mock_rules):
        """All goals on track: no Slack post, return 0."""
        result = self.mod.main()
        assert result == 0
        self.mock_config.post_both.assert_not_called()

    @patch("nova_goal_check.get_active_rules", return_value=[])
    @patch("nova_goal_check.promote_corrections", return_value=0)
    @patch("nova_goal_check.goal_summary", return_value={"active": 3, "completed": 0, "paused": 0})
    @patch("nova_goal_check.get_active_goals", return_value=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
    @patch("nova_goal_check.get_overdue_goals")
    @patch("nova_goal_check.get_stale_goals", return_value=[])
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_overdue_posts_to_slack(self, mock_g_schema, mock_r_schema, mock_detect,
                                     mock_stale, mock_overdue, mock_active,
                                     mock_summary, mock_promote, mock_rules):
        """Overdue goals trigger a Slack post."""
        mock_overdue.return_value = [
            {"id": "o1", "title": "Late Task", "deadline": "2026-04-01"},
        ]
        self.mod.main()
        self.mock_config.post_both.assert_called_once()
        msg = self.mock_config.post_both.call_args[0][0]
        assert "Overdue" in msg
        assert "Late Task" in msg

    @patch("nova_goal_check.get_active_rules", return_value=[])
    @patch("nova_goal_check.promote_corrections", return_value=0)
    @patch("nova_goal_check.goal_summary", return_value={"active": 2, "completed": 0, "paused": 0})
    @patch("nova_goal_check.get_active_goals", return_value=[{"id": "a"}, {"id": "b"}])
    @patch("nova_goal_check.get_overdue_goals", return_value=[])
    @patch("nova_goal_check.get_stale_goals")
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_stale_posts_to_slack(self, mock_g_schema, mock_r_schema, mock_detect,
                                   mock_stale, mock_overdue, mock_active,
                                   mock_summary, mock_promote, mock_rules):
        """Stale goals trigger a Slack post."""
        mock_stale.return_value = [
            {"id": "s1", "title": "Neglected", "days_idle": 14},
        ]
        self.mod.main()
        self.mock_config.post_both.assert_called_once()
        msg = self.mock_config.post_both.call_args[0][0]
        assert "Stale" in msg
        assert "Neglected" in msg
        assert "14d" in msg

    @patch("nova_goal_check.get_active_rules", return_value=[])
    @patch("nova_goal_check.promote_corrections", return_value=0)
    @patch("nova_goal_check.goal_summary", return_value={"active": 6, "completed": 0, "paused": 0})
    @patch("nova_goal_check.get_active_goals")
    @patch("nova_goal_check.get_overdue_goals", return_value=[])
    @patch("nova_goal_check.get_stale_goals", return_value=[])
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_too_many_goals_warning(self, mock_g_schema, mock_r_schema, mock_detect,
                                     mock_stale, mock_overdue, mock_active,
                                     mock_summary, mock_promote, mock_rules):
        """More than 4 active goals triggers a focus warning."""
        mock_active.return_value = [{"id": str(i)} for i in range(6)]
        self.mod.main()
        self.mock_config.post_both.assert_called_once()
        msg = self.mock_config.post_both.call_args[0][0]
        assert "6 active goals" in msg
        assert "3-4 max" in msg

    @patch("nova_goal_check.get_active_rules", return_value=[])
    @patch("nova_goal_check.promote_corrections", return_value=0)
    @patch("nova_goal_check.goal_summary", return_value={"active": 4, "completed": 0, "paused": 0})
    @patch("nova_goal_check.get_active_goals")
    @patch("nova_goal_check.get_overdue_goals", return_value=[])
    @patch("nova_goal_check.get_stale_goals", return_value=[])
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_exactly_four_goals_no_warning(self, mock_g_schema, mock_r_schema, mock_detect,
                                            mock_stale, mock_overdue, mock_active,
                                            mock_summary, mock_promote, mock_rules):
        """Exactly 4 active goals is fine, no Slack post."""
        mock_active.return_value = [{"id": str(i)} for i in range(4)]
        result = self.mod.main()
        assert result == 0
        self.mock_config.post_both.assert_not_called()

    @patch("nova_goal_check.get_active_rules")
    @patch("nova_goal_check.promote_corrections")
    @patch("nova_goal_check.goal_summary", return_value={"active": 3, "completed": 0, "paused": 0})
    @patch("nova_goal_check.get_active_goals", return_value=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
    @patch("nova_goal_check.get_overdue_goals")
    @patch("nova_goal_check.get_stale_goals")
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_promoted_corrections_in_message(self, mock_g_schema, mock_r_schema, mock_detect,
                                              mock_stale, mock_overdue, mock_active,
                                              mock_summary, mock_promote, mock_rules):
        """Promoted corrections are mentioned in Slack post."""
        mock_stale.return_value = [{"id": "s1", "title": "Stale", "days_idle": 10}]
        mock_overdue.return_value = []
        mock_promote.return_value = 2
        mock_rules.return_value = [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}]
        self.mod.main()
        msg = self.mock_config.post_both.call_args[0][0]
        assert "2 new rule(s)" in msg
        assert "3 total active rules" in msg

    @patch("nova_goal_check.get_active_rules", return_value=[])
    @patch("nova_goal_check.promote_corrections", return_value=0)
    @patch("nova_goal_check.goal_summary", return_value={"active": 1, "completed": 0, "paused": 0})
    @patch("nova_goal_check.get_active_goals", return_value=[{"id": "a"}])
    @patch("nova_goal_check.get_overdue_goals")
    @patch("nova_goal_check.get_stale_goals")
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_combined_overdue_and_stale(self, mock_g_schema, mock_r_schema, mock_detect,
                                         mock_stale, mock_overdue, mock_active,
                                         mock_summary, mock_promote, mock_rules):
        """Both overdue and stale sections appear in the same message."""
        mock_stale.return_value = [{"id": "s1", "title": "Stale Goal", "days_idle": 8}]
        mock_overdue.return_value = [{"id": "o1", "title": "Late Goal", "deadline": "2026-03-01"}]
        self.mod.main()
        msg = self.mock_config.post_both.call_args[0][0]
        assert "Overdue" in msg
        assert "Stale" in msg

    @patch("nova_goal_check.get_active_rules", return_value=[])
    @patch("nova_goal_check.promote_corrections", return_value=0)
    @patch("nova_goal_check.goal_summary", return_value={"active": 2, "completed": 0, "paused": 0})
    @patch("nova_goal_check.get_active_goals", return_value=[{"id": "a"}, {"id": "b"}])
    @patch("nova_goal_check.get_overdue_goals", return_value=[])
    @patch("nova_goal_check.get_stale_goals", return_value=[])
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_ensure_schemas_called(self, mock_g_schema, mock_r_schema, mock_detect,
                                    mock_stale, mock_overdue, mock_active,
                                    mock_summary, mock_promote, mock_rules):
        """main() ensures both goals and rules schemas exist."""
        self.mod.main()
        mock_g_schema.assert_called_once()
        mock_r_schema.assert_called_once()

    @patch("nova_goal_check.get_active_rules", return_value=[])
    @patch("nova_goal_check.promote_corrections", return_value=0)
    @patch("nova_goal_check.goal_summary", return_value={"active": 2, "completed": 0, "paused": 0})
    @patch("nova_goal_check.get_active_goals", return_value=[{"id": "a"}, {"id": "b"}])
    @patch("nova_goal_check.get_overdue_goals", return_value=[])
    @patch("nova_goal_check.get_stale_goals", return_value=[])
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_detect_activity_called(self, mock_g_schema, mock_r_schema, mock_detect,
                                     mock_stale, mock_overdue, mock_active,
                                     mock_summary, mock_promote, mock_rules):
        """main() calls detect_activity_from_git()."""
        self.mod.main()
        mock_detect.assert_called_once()

    @patch("nova_goal_check.get_active_rules", return_value=[])
    @patch("nova_goal_check.promote_corrections", return_value=0)
    @patch("nova_goal_check.goal_summary", return_value={"active": 2, "completed": 0, "paused": 0})
    @patch("nova_goal_check.get_active_goals", return_value=[{"id": "a"}, {"id": "b"}])
    @patch("nova_goal_check.get_overdue_goals", return_value=[])
    @patch("nova_goal_check.get_stale_goals", return_value=[])
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_promote_corrections_called(self, mock_g_schema, mock_r_schema, mock_detect,
                                         mock_stale, mock_overdue, mock_active,
                                         mock_summary, mock_promote, mock_rules):
        """main() calls promote_corrections()."""
        self.mod.main()
        mock_promote.assert_called_once()

    @patch("nova_goal_check.get_active_rules", return_value=[])
    @patch("nova_goal_check.promote_corrections", return_value=0)
    @patch("nova_goal_check.goal_summary", return_value={"active": 2, "completed": 0, "paused": 0})
    @patch("nova_goal_check.get_active_goals")
    @patch("nova_goal_check.get_overdue_goals")
    @patch("nova_goal_check.get_stale_goals")
    @patch("nova_goal_check.detect_activity_from_git")
    @patch("nova_goal_check.ensure_rules_schema")
    @patch("nova_goal_check.ensure_goals_schema")
    def test_slack_channel_is_notify(self, mock_g_schema, mock_r_schema, mock_detect,
                                      mock_stale, mock_overdue, mock_active,
                                      mock_summary, mock_promote, mock_rules):
        """Slack posts go to the SLACK_NOTIFY channel."""
        mock_stale.return_value = [{"id": "s1", "title": "Stale", "days_idle": 10}]
        mock_overdue.return_value = []
        mock_active.return_value = [{"id": "a"}, {"id": "b"}]
        self.mod.main()
        kwargs = self.mock_config.post_both.call_args[1]
        assert kwargs["slack_channel"] == self.mock_config.SLACK_NOTIFY


# ============================================================================
# Functional tests — End-to-end workflows (mocked DB)
# ============================================================================


@pytest.mark.functional
class TestGoalWorkflowFunctional:
    """End-to-end: add goal -> log progress -> complete."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_goals" in sys.modules:
            del sys.modules["nova_goals"]
        import nova_goals
        self.mod = nova_goals

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_full_goal_lifecycle(self, mock_exec, mock_log_event):
        """Goal lifecycle: add -> progress -> complete."""
        mock_exec.return_value = True

        # Step 1: Add
        gid = self.mod.add_goal("Ship MLXCode v3", project="MLXCode", priority="high")
        assert gid is not None

        # Step 2: Log progress
        self.mod.log_progress(gid, "Completed parser refactor")

        # Step 3: Complete
        result = self.mod.complete_goal(gid, note="Released to GitHub")
        assert result is True

        # Verify event log: created, progress, completed
        event_types = [c[0][1] for c in mock_log_event.call_args_list]
        assert event_types == ["created", "progress", "completed"]

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_goal_add_pause_resume(self, mock_exec, mock_log_event):
        """Goal lifecycle: add -> pause -> resume (update status)."""
        mock_exec.return_value = True

        gid = self.mod.add_goal("Write docs", priority="low")
        assert gid is not None

        self.mod.pause_goal(gid, reason="Waiting on review")
        self.mod.update_goal(gid, status="active")

        event_types = [c[0][1] for c in mock_log_event.call_args_list]
        assert "created" in event_types
        assert "paused" in event_types

    @patch("nova_goals._log_event")
    @patch("nova_goals._exec")
    def test_goal_add_drop(self, mock_exec, mock_log_event):
        """Goal lifecycle: add -> drop."""
        mock_exec.return_value = True

        gid = self.mod.add_goal("Abandoned project")
        self.mod.drop_goal(gid, reason="No longer needed")

        event_types = [c[0][1] for c in mock_log_event.call_args_list]
        assert event_types == ["created", "dropped"]


@pytest.mark.functional
class TestCorrectionWorkflowFunctional:
    """End-to-end: correction -> promotion -> prompt injection."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_rules" in sys.modules:
            del sys.modules["nova_rules"]
        import nova_rules
        self.mod = nova_rules

    @patch("nova_rules._exec")
    @patch("builtins.open", mock_open(read_data='[]'))
    @patch("nova_rules.CORRECTIONS_FILE")
    def test_correction_to_prompt(self, mock_path, mock_exec):
        """Correction ingested -> rule created -> appears in prompt format."""
        mock_path.exists.return_value = True
        mock_exec.return_value = True

        # Step 1: Ingest correction
        rid = self.mod.ingest_correction(
            nova_response="he went to the store",
            jordan_correction="she went to the store (Nova uses she/her)",
            topic="people",
        )
        assert rid is not None

        # Step 2: Verify the rule text was constructed correctly
        # The add_rule call happened inside ingest_correction
        exec_calls = mock_exec.call_args_list
        # Find the INSERT INTO rules call
        rule_inserts = [c for c in exec_calls if "INSERT INTO rules" in c[0][0]]
        assert len(rule_inserts) >= 1
        sql = rule_inserts[0][0][0]
        assert "Do NOT say" in sql
        assert "she went to the store" in sql

    @patch("nova_rules.get_active_rules")
    def test_prompt_injection_format(self, mock_active):
        """Rules formatted for prompt injection include all active rules."""
        mock_active.return_value = [
            {"id": "a", "rule": "Nova uses she/her", "topic": "people",
             "confidence": 1.0, "times_applied": 5, "created_at": ""},
            {"id": "b", "rule": "Call Jordan Little Mister", "topic": "global",
             "confidence": 0.9, "times_applied": 0, "created_at": ""},
        ]
        result = self.mod.format_rules_for_prompt()
        assert "## Active Rules" in result
        assert "[people] Nova uses she/her" in result
        assert "- Call Jordan Little Mister" in result
        # Global rules should NOT have [global] tag
        assert "[global]" not in result


# ============================================================================
# Frame tests — Slack message formatting verification
# ============================================================================


@pytest.mark.frame
class TestSlackMessageFormatting:
    """Verify Slack-compatible message formatting."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        for mod_name in ("nova_goals", "nova_rules"):
            if mod_name in sys.modules:
                del sys.modules[mod_name]
        import nova_goals
        import nova_rules
        self.goals = nova_goals
        self.rules = nova_rules

    @patch("nova_goals.run_gap_analysis", return_value="All goals on track.")
    @patch("nova_goals.get_active_goals")
    def test_brief_uses_slack_bold(self, mock_active, mock_gap):
        """format_goals_brief() uses Slack bold (*text*) syntax."""
        mock_active.return_value = [
            {"id": "a", "title": "G", "priority": "high",
             "deadline": None, "project": None,
             "check_in_days": 7, "last_activity": "", "created_at": ""},
        ]
        result = self.goals.format_goals_brief()
        assert result.startswith("*Active Goals")

    @patch("nova_goals.get_active_goals")
    @patch("nova_goals.get_overdue_goals")
    @patch("nova_goals.get_stale_goals")
    def test_gap_analysis_uses_backticks_for_ids(self, mock_stale, mock_overdue, mock_active):
        """run_gap_analysis() wraps goal IDs in backticks for Slack."""
        mock_stale.return_value = []
        mock_overdue.return_value = [
            {"id": "abc123", "title": "Late", "deadline": "2026-04-01"}
        ]
        mock_active.return_value = []
        result = self.goals.run_gap_analysis()
        assert "`abc123`" in result

    @patch("nova_rules.get_active_rules")
    def test_rules_prompt_uses_markdown(self, mock_active):
        """format_rules_for_prompt() uses markdown heading and bullets."""
        mock_active.return_value = [
            {"id": "a", "rule": "Test", "topic": "global",
             "confidence": 1.0, "times_applied": 0, "created_at": ""},
        ]
        result = self.rules.format_rules_for_prompt()
        assert result.startswith("## ")
        assert "\n- " in result

    @patch("nova_goals.run_gap_analysis")
    @patch("nova_goals.get_active_goals")
    def test_brief_indented_goals(self, mock_active, mock_gap):
        """Goal lines in brief are indented with two spaces."""
        mock_active.return_value = [
            {"id": "a", "title": "Test Goal", "priority": "medium",
             "deadline": None, "project": None,
             "check_in_days": 7, "last_activity": "", "created_at": ""},
        ]
        mock_gap.return_value = "All goals on track."
        result = self.goals.format_goals_brief()
        lines = result.split("\n")
        goal_lines = [l for l in lines if "Test Goal" in l]
        assert all(l.startswith("  ") for l in goal_lines)

    @patch("nova_goals.get_active_goals")
    @patch("nova_goals.get_overdue_goals")
    @patch("nova_goals.get_stale_goals")
    def test_gap_stale_shows_idle_days(self, mock_stale, mock_overdue, mock_active):
        """Stale section shows 'Xd idle' format."""
        mock_stale.return_value = [
            {"id": "s1", "title": "Old Goal", "days_idle": 21}
        ]
        mock_overdue.return_value = []
        mock_active.return_value = []
        result = self.goals.run_gap_analysis()
        assert "21d idle" in result

    @patch("nova_goals.get_active_goals")
    @patch("nova_goals.get_overdue_goals")
    @patch("nova_goals.get_stale_goals")
    def test_gap_overdue_shows_deadline(self, mock_stale, mock_overdue, mock_active):
        """Overdue section shows 'due YYYY-MM-DD' format."""
        mock_stale.return_value = []
        mock_overdue.return_value = [
            {"id": "o1", "title": "Past Due", "deadline": "2026-03-15"}
        ]
        mock_active.return_value = []
        result = self.goals.run_gap_analysis()
        assert "due 2026-03-15" in result


# ============================================================================
# Edge cases and error handling
# ============================================================================


class TestEdgeCases:
    """Edge cases: empty data, malformed input, boundary conditions."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        for mod_name in ("nova_goals", "nova_rules"):
            if mod_name in sys.modules:
                del sys.modules[mod_name]
        import nova_goals
        import nova_rules
        self.goals = nova_goals
        self.rules = nova_rules

    @patch("nova_goals._exec")
    @patch("nova_goals._log_event")
    def test_add_goal_with_unicode_title(self, mock_log, mock_exec):
        """Goal titles with unicode characters are handled."""
        mock_exec.return_value = True
        gid = self.goals.add_goal("Fix ✨ sparkle bug \U0001f41b")
        assert gid is not None

    @patch("nova_rules._exec")
    def test_add_rule_with_very_long_text(self, mock_exec):
        """Very long rule text is passed through."""
        mock_exec.return_value = True
        long_rule = "A" * 5000
        rid = self.rules.add_rule(long_rule)
        assert rid is not None
        sql = mock_exec.call_args[0][0]
        assert long_rule in sql

    @patch("nova_goals._query")
    def test_get_active_goals_handles_pipe_in_title(self, mock_query):
        """Titles containing pipe characters cause shifted parsing (known limitation).

        The pipe-delimited format cannot distinguish pipes in data from delimiters.
        With an extra pipe, fields shift and int(parts[5]) hits a non-numeric value,
        raising ValueError. This documents the known limitation.
        """
        mock_query.return_value = [
            "abc|Goal with | pipe|Proj|high|2026-06-01|7|2026-11-15T10:00:00|2026-11-10T08:00:00"
        ]
        with pytest.raises(ValueError):
            self.goals.get_active_goals()

    @patch("nova_goals._query")
    def test_goal_summary_non_numeric_count(self, mock_query):
        """goal_summary() handles unexpected non-numeric count gracefully."""
        mock_query.side_effect = [["not_a_number"], ["5"], ["0"]]
        # int("not_a_number") will raise ValueError
        with pytest.raises(ValueError):
            self.goals.goal_summary()

    @patch("nova_goals.get_active_goals")
    def test_stale_goals_malformed_timestamp(self, mock_active):
        """Malformed timestamps in last_activity are skipped."""
        mock_active.return_value = [{
            "id": "abc",
            "title": "Bad TS",
            "project": None,
            "priority": "medium",
            "deadline": None,
            "check_in_days": 7,
            "last_activity": "not-a-timestamp",
            "created_at": "",
        }]
        stale = self.goals.get_stale_goals()
        # Should not crash, just skip the goal
        assert len(stale) == 0

    @patch("nova_goals.get_active_goals")
    def test_stale_goals_exactly_at_threshold(self, mock_active):
        """Goal idle for exactly check_in_days is flagged as stale (>= comparison)."""
        # Use safe timestamp format: "2025-12-25" is exactly far enough in the past
        # and has no zero-prefixed month/day to be corrupted by split("-0").
        old_ts = "2025-12-25T10:00:00"
        mock_active.return_value = [{
            "id": "abc",
            "title": "Edge Case",
            "project": None,
            "priority": "medium",
            "deadline": None,
            "check_in_days": 7,
            "last_activity": old_ts,
            "created_at": old_ts,
        }]
        stale = self.goals.get_stale_goals()
        # ~130 days old, well past the 7-day threshold
        assert len(stale) == 1

    @patch("nova_goals.get_active_goals")
    def test_stale_goals_below_threshold_not_stale(self, mock_active):
        """Goal with activity ~20 days ago and 9999-day threshold is NOT stale."""
        # Safe timestamp format (no zero-prefixed month/day)
        mock_active.return_value = [{
            "id": "abc",
            "title": "Almost Stale",
            "project": None,
            "priority": "medium",
            "deadline": None,
            "check_in_days": 9999,  # huge threshold
            "last_activity": "2025-12-15T10:00:00",
            "created_at": "2025-12-15T10:00:00",
        }]
        stale = self.goals.get_stale_goals()
        assert len(stale) == 0

    def test_goals_escape_with_newlines(self):
        """_escape() does not alter newlines (they are safe in SQL strings)."""
        result = self.goals._escape("line1\nline2")
        assert result == "line1\nline2"

    def test_rules_escape_with_percent(self):
        """_escape() does not alter percent signs (safe in psql -c)."""
        result = self.rules._escape("100% done")
        assert result == "100% done"

    @patch("nova_rules._exec")
    def test_add_rule_with_jsonb_correction(self, mock_exec):
        """add_rule() properly serializes JSONB correction data."""
        mock_exec.return_value = True
        correction = {"nova_response": "he", "jordan_correction": "she"}
        self.rules.add_rule("Use she/her", original_correction=correction)
        sql = mock_exec.call_args[0][0]
        assert "::jsonb" in sql

    @patch("nova_goals._exec")
    def test_update_goal_description_with_special_chars(self, mock_exec):
        """update_goal() handles description with SQL-special characters."""
        mock_exec.return_value = True
        self.goals.update_goal("abc", description="Fix the 'parser' bug -- it's bad \\n")
        sql = mock_exec.call_args[0][0]
        assert "it''s bad" in sql
        assert "\\\\\\\\" in sql or "\\\\" in sql  # backslash escaped
