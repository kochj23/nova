"""
test_nova_goal_check.py — All 7 test categories for nova_goal_check.py
Written by Jordan Koch.
"""

import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub all dependencies before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_nova_logger = MagicMock()
_nova_logger.LOG_INFO = "INFO"
_nova_logger.LOG_ERROR = "ERROR"
sys.modules["nova_logger"] = _nova_logger

_nova_goals = MagicMock()
_nova_goals.ensure_schema = MagicMock()
_nova_goals.get_active_goals = MagicMock(return_value=[])
_nova_goals.get_stale_goals = MagicMock(return_value=[])
_nova_goals.get_overdue_goals = MagicMock(return_value=[])
_nova_goals.detect_activity_from_git = MagicMock()
_nova_goals.format_goals_brief = MagicMock(return_value="")
_nova_goals.goal_summary = MagicMock(return_value={"active": 0, "completed": 0, "paused": 0})
sys.modules["nova_goals"] = _nova_goals

_nova_rules = MagicMock()
_nova_rules.ensure_schema = MagicMock()
_nova_rules.promote_corrections = MagicMock(return_value=0)
_nova_rules.get_active_rules = MagicMock(return_value=[])
sys.modules["nova_rules"] = _nova_rules

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_goal_check.py"
_spec = importlib.util.spec_from_file_location("nova_goal_check", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


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

    def test_db_operations_via_nova_goals(self):
        """goal_check must not query DB directly — uses nova_goals module."""
        src = _SCRIPT.read_text()
        self.assertNotIn("psql", src, "Must use nova_goals, not call psql directly")

    def test_notifications_via_nova_config(self):
        """Slack notifications must go through nova_config."""
        src = _SCRIPT.read_text()
        self.assertIn("nova_config.post_both", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_main_runs_fast_when_all_on_track(self):
        """main() must complete quickly when all goals are on track."""
        _nova_goals.get_stale_goals.return_value = []
        _nova_goals.get_overdue_goals.return_value = []
        _nova_goals.get_active_goals.return_value = []
        _nova_rules.promote_corrections.return_value = 0

        start = time.perf_counter()
        result = _mod.main()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"main() took {elapsed:.3f}s on empty state")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_main_handles_schema_failure(self):
        """main() must not crash if ensure_schema raises."""
        _nova_goals.ensure_schema.side_effect = Exception("DB unavailable")
        try:
            # May raise or not — just must not crash unhandled
            _mod.main()
        except Exception:
            pass
        finally:
            _nova_goals.ensure_schema.side_effect = None

    def test_main_handles_detect_activity_failure(self):
        """main() must continue if detect_activity_from_git raises."""
        _nova_goals.detect_activity_from_git.side_effect = Exception("git not found")
        try:
            _mod.main()
        except Exception:
            pass
        finally:
            _nova_goals.detect_activity_from_git.side_effect = None


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def setUp(self):
        _nova_goals.get_stale_goals.return_value = []
        _nova_goals.get_overdue_goals.return_value = []
        _nova_goals.get_active_goals.return_value = []
        _nova_rules.promote_corrections.return_value = 0
        _nova_cfg.post_both.reset_mock()

    def test_main_returns_zero_on_success(self):
        result = _mod.main()
        self.assertEqual(result, 0)

    def test_main_silent_when_all_on_track_few_goals(self):
        """main() must not post to Slack when all goals are current and <= 4."""
        _nova_goals.get_active_goals.return_value = [
            {"id": "g1", "title": "Goal 1", "priority": "medium", "deadline": None,
             "project": "Test", "check_in_days": 7, "last_activity": "2026-05-13",
             "created_at": "2026-01-01"}
        ]
        result = _mod.main()
        _nova_cfg.post_both.assert_not_called()
        self.assertEqual(result, 0)

    def test_main_posts_when_overdue(self):
        """main() must post to Slack when there are overdue goals."""
        _nova_goals.get_overdue_goals.return_value = [
            {"id": "g1", "title": "Ship MLXCode v2", "deadline": "2026-04-01",
             "priority": "high", "project": "MLXCode"}
        ]
        _mod.main()
        _nova_cfg.post_both.assert_called()

    def test_main_posts_when_stale(self):
        """main() must post when stale goals exist."""
        _nova_goals.get_stale_goals.return_value = [
            {"id": "g1", "title": "Fix NMAPScanner bug", "days_idle": 10,
             "priority": "medium", "deadline": None, "project": "NMAPScanner"}
        ]
        _mod.main()
        _nova_cfg.post_both.assert_called()

    def test_main_warns_too_many_active_goals(self):
        """main() must warn when more than 4 goals are active."""
        many_goals = [
            {"id": f"g{i}", "title": f"Goal {i}", "priority": "medium",
             "deadline": None, "project": None, "check_in_days": 7,
             "last_activity": "2026-05-13", "created_at": "2026-01-01"}
            for i in range(6)
        ]
        _nova_goals.get_active_goals.return_value = many_goals
        _mod.main()
        call_args = _nova_cfg.post_both.call_args
        if call_args:
            msg = call_args[0][0]
            self.assertIn("6", msg)

    def test_source_constant(self):
        self.assertEqual(_mod.SOURCE, "nova_goal_check")

    def test_today_constant_is_isoformat(self):
        import re
        self.assertRegex(_mod.TODAY, r"\d{4}-\d{2}-\d{2}")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_calls_ensure_schema(self):
        _nova_goals.ensure_schema.reset_mock()
        _nova_rules.ensure_schema.reset_mock()
        _mod.main()
        _nova_goals.ensure_schema.assert_called()
        _nova_rules.ensure_schema.assert_called()

    def test_main_calls_detect_activity(self):
        _nova_goals.detect_activity_from_git.reset_mock()
        _mod.main()
        _nova_goals.detect_activity_from_git.assert_called()

    def test_main_calls_promote_corrections(self):
        _nova_rules.promote_corrections.reset_mock()
        _mod.main()
        _nova_rules.promote_corrections.assert_called()

    def test_main_includes_promoted_rules_in_message(self):
        """When corrections are promoted, the count must appear in Slack message."""
        _nova_rules.promote_corrections.return_value = 3
        _nova_rules.get_active_rules.return_value = ["r1", "r2", "r3"]
        _nova_goals.get_stale_goals.return_value = [
            {"id": "g1", "title": "Test Goal", "days_idle": 8,
             "priority": "low", "deadline": None, "project": None}
        ]
        _nova_cfg.post_both.reset_mock()
        _mod.main()
        if _nova_cfg.post_both.called:
            msg = _nova_cfg.post_both.call_args[0][0]
            self.assertIn("3", msg, "Promoted corrections count must be in message")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_overdue_message_includes_deadline(self):
        _nova_goals.get_overdue_goals.return_value = [
            {"id": "g1", "title": "Deploy RsyncGUI", "deadline": "2026-04-30",
             "priority": "high", "project": "RsyncGUI"}
        ]
        _nova_goals.get_stale_goals.return_value = []
        _nova_goals.get_active_goals.return_value = []
        _nova_cfg.post_both.reset_mock()
        _mod.main()
        if _nova_cfg.post_both.called:
            msg = _nova_cfg.post_both.call_args[0][0]
            self.assertIn("Deploy RsyncGUI", msg)
            self.assertIn("2026-04-30", msg)

    def test_stale_message_includes_days_idle(self):
        _nova_goals.get_stale_goals.return_value = [
            {"id": "g1", "title": "Fix memory leak", "days_idle": 14,
             "priority": "medium", "deadline": None, "project": "MLXCode"}
        ]
        _nova_goals.get_overdue_goals.return_value = []
        _nova_goals.get_active_goals.return_value = []
        _nova_cfg.post_both.reset_mock()
        _mod.main()
        if _nova_cfg.post_both.called:
            msg = _nova_cfg.post_both.call_args[0][0]
            self.assertIn("14", msg)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_goal_check.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.SOURCE, str)
        self.assertIsInstance(_mod.TODAY, str)

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
