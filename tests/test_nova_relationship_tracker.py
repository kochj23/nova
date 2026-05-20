"""
test_nova_relationship_tracker.py — All 7 test categories for nova_relationship_tracker.py
Written by Jordan Koch.
"""

from __future__ import annotations
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.slack_bot_token.return_value = "xoxb-fake"
_nova_cfg.SLACK_EMAIL = "C0ATAF7NZG9"
sys.modules["nova_config"] = _nova_cfg
sys.modules["herd_config"] = MagicMock(HERD=[
    {"name": "Sam", "email": "sam@example.com"},
    {"name": "Gaston", "email": "gaston@example.com"},
])

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_relationship_tracker.py"

# Python 3.9 compatibility: rewrite X | Y return type annotations
def _load_compat(script_path, module_name):
    src = script_path.read_text()
    if sys.version_info < (3, 10):
        src = re.sub(r'\)\s*->\s*(\w+)\s*\|\s*(\w+)\s*:', r') -> "\1 | \2":', src)
    mod = types.ModuleType(module_name)
    mod.__file__ = str(script_path)
    exec(compile(src, str(script_path), "exec"), mod.__dict__)
    return mod

_mod = _load_compat(_SCRIPT, "nova_relationship_tracker")

days_since = _mod.days_since
extract_latest_date_from_notes = _mod.extract_latest_date_from_notes
format_slack_message = _mod.format_slack_message


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential: {p!r}")

    def test_slack_token_from_nova_config(self):
        src = _SCRIPT.read_text()
        self.assertIn("slack_bot_token", src)

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII: {p!r}")

    def test_slack_message_does_not_leak_tokens(self):
        """format_slack_message must not include raw auth tokens."""
        result = format_slack_message([], [], [], [])
        self.assertNotIn("xoxb", result)

    def test_oneonone_api_local(self):
        """OneOnOne API must be local (127.0.0.1)."""
        self.assertIn("127.0.0.1", _mod.ONEONONE_URL)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_days_since_fast(self):
        now = datetime.now(timezone.utc)
        iso = now.isoformat()
        start = time.perf_counter()
        for _ in range(5000):
            days_since(iso)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2, f"days_since 5000x: {elapsed:.3f}s")

    def test_extract_date_fast_on_large_notes(self):
        notes = ("Lots of text without dates. " * 100 +
                 "4/07/26: Meeting went well. " * 10)
        start = time.perf_counter()
        for _ in range(200):
            extract_latest_date_from_notes(notes)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_check_oneonone_contacts_returns_empty_on_api_failure(self):
        """check_oneonone_contacts must return ([], []) when API unavailable."""
        with patch.object(_mod, "get", return_value=None):
            overdue, ok = _mod.check_oneonone_contacts()
        self.assertEqual(overdue, [])
        self.assertEqual(ok, [])

    def test_check_herd_contacts_with_failed_memory_search(self):
        """check_herd_contacts must handle memory search failures."""
        with patch.object(_mod, "last_email_contact", return_value=(None, "error")):
            overdue, ok = _mod.check_herd_contacts()
        self.assertIsInstance(overdue, list)
        self.assertIsInstance(ok, list)

    def test_post_slack_raises_on_failure(self):
        """post_slack raises when Slack API fails."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with self.assertRaises(Exception):
                _mod.post_slack("test message")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_days_since_recent(self):
        now = datetime.now(timezone.utc)
        two_days_ago = (now - timedelta(days=2)).isoformat()
        result = days_since(two_days_ago)
        self.assertAlmostEqual(result, 2, delta=1)

    def test_days_since_none_on_empty(self):
        result = days_since("")
        self.assertIsNone(result)

    def test_days_since_none_on_invalid(self):
        result = days_since("not a date")
        self.assertIsNone(result)

    def test_extract_date_finds_m_d_yy(self):
        notes = "4/07/26: Great meeting today"
        result = extract_latest_date_from_notes(notes)
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 4)
        self.assertEqual(result.day, 7)

    def test_extract_date_finds_mm_dd_yy(self):
        notes = "03/28/26: Review completed"
        result = extract_latest_date_from_notes(notes)
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)

    def test_extract_date_returns_most_recent(self):
        notes = "3/01/26: First meeting\n4/15/26: Second meeting\n2/01/26: Old meeting"
        result = extract_latest_date_from_notes(notes)
        self.assertIsNotNone(result)
        self.assertEqual(result.month, 4)
        self.assertEqual(result.day, 15)

    def test_extract_date_returns_none_on_empty(self):
        result = extract_latest_date_from_notes("")
        self.assertIsNone(result)

    def test_extract_date_rejects_future_dates(self):
        notes = "12/31/99: Far future meeting"
        result = extract_latest_date_from_notes(notes)
        # 99 → 2099 is in the future, should be rejected
        self.assertIsNone(result)

    def test_format_slack_all_current(self):
        """format_slack_message must say 'All relationships current' when nothing overdue."""
        result = format_slack_message([], [], [], [])
        self.assertIn("All relationships", result)

    def test_format_slack_shows_overdue_1on1(self):
        overdue = [{"name": "Alice", "days_ago": 30, "threshold": 14,
                     "title": "Manager", "dept": "Engineering",
                     "frequency": "Bi-weekly", "status": "overdue",
                     "last_date": "2026-04-13", "source": "notes"}]
        result = format_slack_message(overdue, [], [], [])
        self.assertIn("Alice", result)
        self.assertIn("30d ago", result)

    def test_format_slack_shows_herd_overdue(self):
        herd_overdue = [{"name": "Sam", "email": "sam@example.com",
                          "days_ago": 20, "snippet": "test snippet", "status": "overdue"}]
        result = format_slack_message([], [], herd_overdue, [])
        self.assertIn("Sam", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_check_herd_all_overdue_when_no_email_history(self):
        """When no email history, all herd members should be overdue."""
        with patch.object(_mod, "last_email_contact", return_value=(None, "no history")):
            overdue, ok = _mod.check_herd_contacts()
        self.assertGreater(len(overdue), 0, "All members should be overdue when no email history")
        self.assertEqual(len(ok), 0)

    def test_check_herd_all_ok_when_recent_contact(self):
        """When all members contacted recently, none should be overdue."""
        with patch.object(_mod, "last_email_contact", return_value=(5, "recent email")):
            overdue, ok = _mod.check_herd_contacts()
        self.assertEqual(len(overdue), 0, "No one should be overdue when recently contacted")
        self.assertGreater(len(ok), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_report_mode_does_not_post_to_slack(self):
        """--report mode must print without posting to Slack."""
        post_calls = []
        with patch("sys.argv", ["nova_relationship_tracker.py", "--report"]):
            with patch.object(_mod, "check_oneonone_contacts",
                               return_value=([], [])):
                with patch.object(_mod, "check_herd_contacts",
                                   return_value=([], [])):
                    with patch.object(_mod, "post_slack",
                                       side_effect=lambda t: post_calls.append(t)):
                        _mod.main()
        self.assertEqual(len(post_calls), 0, "--report must not post to Slack")

    def test_main_quiet_mode_skips_when_nothing_overdue(self):
        """--quiet mode must skip post when nothing is overdue."""
        post_calls = []
        with patch("sys.argv", ["nova_relationship_tracker.py", "--quiet"]):
            with patch.object(_mod, "check_oneonone_contacts",
                               return_value=([], [])):
                with patch.object(_mod, "check_herd_contacts",
                                   return_value=([], [])):
                    with patch.object(_mod, "post_slack",
                                       side_effect=lambda t: post_calls.append(t)):
                        _mod.main()
        self.assertEqual(len(post_calls), 0, "--quiet must not post when all current")

    def test_cadence_thresholds_present(self):
        self.assertIn("Weekly", _mod.CADENCE_THRESHOLDS)
        self.assertIn("Monthly", _mod.CADENCE_THRESHOLDS)
        self.assertIn("Bi-weekly", _mod.CADENCE_THRESHOLDS)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_relationship_tracker.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.ONEONONE_URL, str)
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.CADENCE_THRESHOLDS, dict)
        self.assertIsInstance(_mod.HERD_THRESHOLD, timedelta)

    def test_all_functions_callable(self):
        for fn in [days_since, extract_latest_date_from_notes, format_slack_message,
                    _mod.check_oneonone_contacts, _mod.check_herd_contacts,
                    _mod.post_slack, _mod.main]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
