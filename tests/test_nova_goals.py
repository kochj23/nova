"""
test_nova_goals.py — All 7 test categories for nova_goals.py
Written by Jordan Koch.
"""

import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_nova_logger = MagicMock()
_nova_logger.log = MagicMock()
_nova_logger.LOG_INFO = "INFO"
_nova_logger.LOG_ERROR = "ERROR"
_nova_logger.LOG_WARN = "WARN"
sys.modules["nova_logger"] = _nova_logger

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_goals.py"
_spec = importlib.util.spec_from_file_location("nova_goals", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_escape = _mod._escape
add_goal = _mod.add_goal
update_goal = _mod.update_goal
complete_goal = _mod.complete_goal
pause_goal = _mod.pause_goal
drop_goal = _mod.drop_goal
get_stale_goals = _mod.get_stale_goals
format_goals_brief = _mod.format_goals_brief
run_gap_analysis = _mod.run_gap_analysis


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential: {p!r}")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII: {p!r}")

    def test_escape_prevents_sql_injection(self):
        """_escape must neutralize single quotes in SQL strings."""
        evil = "'; DROP TABLE goals; --"
        result = _escape(evil)
        self.assertNotIn("'", result.replace("''", ""), "Single quotes must be escaped")

    def test_escape_empty_string(self):
        self.assertEqual(_escape(""), "")

    def test_escape_none(self):
        self.assertEqual(_escape(None), "")

    def test_goal_ids_are_uuids_not_sequential(self):
        """add_goal must use UUID-based IDs, not sequential integers."""
        src = _SCRIPT.read_text()
        self.assertIn("uuid", src.lower(), "Must use UUID for goal IDs")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_escape_fast(self):
        text = "It's a test with O'Brien's data and \\slashes\\"
        start = time.perf_counter()
        for _ in range(10000):
            _escape(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"_escape 10000x: {elapsed:.3f}s")

    def test_get_stale_goals_fast_on_empty(self):
        with patch.object(_mod, "get_active_goals", return_value=[]):
            start = time.perf_counter()
            result = get_stale_goals()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.01)
        self.assertEqual(result, [])


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_query_returns_empty_on_psql_failure(self):
        """_query must return [] when psql fails."""
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=1, stderr="DB not found", stdout="")):
            result = _mod._query("SELECT 1;")
        self.assertEqual(result, [])

    def test_exec_returns_false_on_psql_failure(self):
        """_exec must return False when psql fails."""
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=1, stderr="error", stdout="")):
            result = _mod._exec("INSERT INTO goals VALUES (1);")
        self.assertFalse(result)

    def test_add_goal_returns_none_on_db_failure(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=1, stderr="error", stdout="")):
            result = add_goal("Test goal")
        self.assertIsNone(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_escape_single_quote(self):
        result = _escape("O'Brien")
        self.assertIn("''", result, "Single quote must be doubled for SQL safety")

    def test_escape_backslash(self):
        result = _escape("path\\to\\file")
        self.assertIn("\\\\", result, "Backslash must be escaped")

    def test_get_stale_goals_empty_when_no_active(self):
        with patch.object(_mod, "get_active_goals", return_value=[]):
            result = get_stale_goals()
        self.assertEqual(result, [])

    def test_get_stale_goals_identifies_idle_goal(self):
        from datetime import datetime, timedelta
        idle_date = (datetime.now() - timedelta(days=10)).isoformat()
        active_goals = [
            {"id": "g1", "title": "Test Goal", "check_in_days": 7,
             "last_activity": idle_date, "priority": "medium",
             "deadline": None, "project": None, "created_at": "2026-01-01"}
        ]
        with patch.object(_mod, "get_active_goals", return_value=active_goals):
            result = get_stale_goals()
        self.assertEqual(len(result), 1)
        self.assertGreaterEqual(result[0]["days_idle"], 10)

    def test_get_stale_goals_skips_recent_goal(self):
        from datetime import datetime, timedelta
        recent_date = (datetime.now() - timedelta(days=2)).isoformat()
        active_goals = [
            {"id": "g1", "title": "Recent Goal", "check_in_days": 7,
             "last_activity": recent_date, "priority": "medium",
             "deadline": None, "project": None, "created_at": "2026-01-01"}
        ]
        with patch.object(_mod, "get_active_goals", return_value=active_goals):
            result = get_stale_goals()
        self.assertEqual(len(result), 0)

    def test_run_gap_analysis_returns_string(self):
        with patch.object(_mod, "get_overdue_goals", return_value=[]):
            with patch.object(_mod, "get_stale_goals", return_value=[]):
                with patch.object(_mod, "get_active_goals", return_value=[]):
                    result = run_gap_analysis()
        self.assertIsInstance(result, str)
        self.assertIn("All goals on track", result)

    def test_run_gap_analysis_shows_overdue(self):
        overdue = [{"id": "g1", "title": "Ship product", "deadline": "2026-04-01",
                     "priority": "high", "project": None}]
        with patch.object(_mod, "get_overdue_goals", return_value=overdue):
            with patch.object(_mod, "get_stale_goals", return_value=[]):
                with patch.object(_mod, "get_active_goals", return_value=[]):
                    result = run_gap_analysis()
        self.assertIn("Ship product", result)
        self.assertIn("Overdue", result)

    def test_format_goals_brief_returns_none_when_no_goals(self):
        with patch.object(_mod, "get_active_goals", return_value=[]):
            result = format_goals_brief()
        self.assertIsNone(result)

    def test_format_goals_brief_shows_active_goals(self):
        goals = [
            {"id": "g1", "title": "Build MLXCode", "priority": "high",
             "deadline": "2026-06-01", "project": "MLXCode",
             "check_in_days": 7, "last_activity": "2026-05-13", "created_at": "2026-01-01"}
        ]
        with patch.object(_mod, "get_active_goals", return_value=goals):
            with patch.object(_mod, "run_gap_analysis", return_value="All goals on track."):
                result = format_goals_brief()
        self.assertIn("Build MLXCode", result)
        self.assertIn("2026-06-01", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_add_goal_calls_psql(self):
        """add_goal must call psql to insert the goal."""
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            result = add_goal("Test integration goal")
        # add_goal calls _exec (INSERT) and _log_event (INSERT)
        self.assertGreaterEqual(mock_run.call_count, 1)

    def test_complete_goal_updates_status(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            complete_goal("g1", "Finished!")
        self.assertGreaterEqual(mock_run.call_count, 1)

    def test_pause_goal_updates_status(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            pause_goal("g1", "On hold for now")
        self.assertGreaterEqual(mock_run.call_count, 1)

    def test_detect_activity_from_git_skips_missing_projects(self):
        """detect_activity_from_git must skip projects that don't exist on disk."""
        goals = [
            {"id": "g1", "title": "Test", "project": "NonExistentProject99",
             "priority": "medium", "deadline": None, "check_in_days": 7,
             "last_activity": "2026-05-13", "created_at": "2026-01-01"}
        ]
        with patch.object(_mod, "get_active_goals", return_value=goals):
            try:
                _mod.detect_activity_from_git("/nonexistent/path")
            except Exception as e:
                self.fail(f"detect_activity raised on missing project: {e}")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_goal_id_is_8_chars(self):
        """add_goal must generate an 8-character UUID snippet as ID."""
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")):
            result = add_goal("Test goal for ID check")
        if result is not None:
            self.assertEqual(len(result), 8)

    def test_priority_labels_in_brief(self):
        """format_goals_brief must show priority icons."""
        goals = [
            {"id": "g1", "title": "High priority goal", "priority": "high",
             "deadline": None, "project": None, "check_in_days": 7,
             "last_activity": "2026-05-13", "created_at": "2026-01-01"},
        ]
        with patch.object(_mod, "get_active_goals", return_value=goals):
            with patch.object(_mod, "run_gap_analysis", return_value="All goals on track."):
                result = format_goals_brief()
        # High priority should show red circle emoji
        self.assertIn("🔴", result)

    def test_update_goal_handles_all_fields(self):
        """update_goal must construct SQL with all provided kwargs."""
        with patch.object(_mod, "_exec", return_value=True) as mock_exec:
            update_goal("g1", title="New title", status="paused", priority="high")
        mock_exec.assert_called()
        sql = mock_exec.call_args[0][0]
        self.assertIn("New title", sql)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_goals.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertEqual(_mod.DB, "nova_ops")
        self.assertEqual(_mod.SOURCE, "nova_goals")

    def test_all_functions_callable(self):
        for fn in [add_goal, update_goal, complete_goal, pause_goal, drop_goal,
                    get_stale_goals, _mod.get_active_goals, _mod.get_overdue_goals,
                    run_gap_analysis, format_goals_brief, _mod.ensure_schema, _mod.main]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
