"""
test_nova_journal_stats_poller.py — All 7 test categories for nova_journal_stats_poller.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_journal_stats_poller.py"
_spec = importlib.util.spec_from_file_location("nova_journal_stats_poller", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

fetch_traffic = _mod.fetch_traffic
fetch_top_paths = _mod.fetch_top_paths
fetch_referrers = _mod.fetch_referrers
scan_content = _mod.scan_content
get_scheduler_state = _mod.get_scheduler_state
next_run_time = _mod.next_run_time
update_history = _mod.update_history
build_schedule_panel = _mod.build_schedule_panel
SECTIONS = _mod.SECTIONS
SECTION_SCHEDULES = _mod.SECTION_SCHEDULES


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(p, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_stats_file_uses_home_path(self):
        self.assertTrue(str(_mod.STATS_FILE).startswith(str(Path.home())))

    def test_uses_gh_cli_not_hardcoded_token(self):
        """Must use 'gh' CLI for GitHub API (uses its stored token, not hardcoded)."""
        src = _SCRIPT.read_text()
        self.assertIn('"gh"', src)
        # No Bearer token pattern
        self.assertNotIn("Bearer ghp_", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_next_run_time_fast(self):
        """next_run_time must complete in < 1ms."""
        start = time.perf_counter()
        for _ in range(10000):
            next_run_time(9, 0)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_build_schedule_panel_fast(self):
        """build_schedule_panel must complete in < 50ms."""
        start = time.perf_counter()
        result = build_schedule_panel()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05)

    def test_scan_content_fast_when_dir_missing(self):
        """scan_content must return quickly when CONTENT_DIR is missing."""
        with patch.object(_mod, "CONTENT_DIR", Path("/nonexistent")):
            start = time.perf_counter()
            result = scan_content()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_fetch_traffic_returns_defaults_on_gh_failure(self):
        """fetch_traffic returns empty defaults when gh CLI fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = fetch_traffic()
        self.assertIn("total_count", result)
        self.assertEqual(result["total_count"], 0)

    def test_fetch_top_paths_returns_empty_on_failure(self):
        """fetch_top_paths returns [] on gh CLI failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = fetch_top_paths()
        self.assertIsInstance(result, list)

    def test_get_scheduler_state_handles_missing_file(self):
        """get_scheduler_state returns empty dict when state file missing."""
        with patch.object(_mod, "STATS_FILE", Path("/nonexistent")):
            result = get_scheduler_state()
        # Should be a dict (possibly empty due to missing file)
        self.assertIsInstance(result, dict)

    def test_fetch_recent_deploys_returns_empty_on_failure(self):
        """fetch_recent_deploys returns [] when gh run list fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = _mod.fetch_recent_deploys()
        self.assertIsInstance(result, list)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_next_run_time_future(self):
        """next_run_time must always return a future timestamp."""
        result = next_run_time(9, 0)
        now = datetime.now()
        result_dt = datetime.fromisoformat(result)
        self.assertGreater(result_dt, now)

    def test_next_run_time_tomorrow_if_past(self):
        """next_run_time returns tomorrow if the time has already passed today."""
        now = datetime.now()
        # Use a time that's definitely in the past today
        past_hour = (now - timedelta(hours=2)).hour
        past_minute = (now - timedelta(hours=2)).minute
        result = next_run_time(past_hour, past_minute)
        result_dt = datetime.fromisoformat(result)
        self.assertGreater(result_dt, now)

    def test_sections_defined(self):
        """SECTIONS must contain all expected content types."""
        expected = {"dreams", "essays", "opinions", "after-dark", "tech-today"}
        self.assertTrue(expected.issubset(set(SECTIONS)))

    def test_section_schedules_defined(self):
        """SECTION_SCHEDULES must define schedule for all sections."""
        for section in SECTIONS:
            self.assertIn(section, SECTION_SCHEDULES,
                          f"Section {section!r} missing from SECTION_SCHEDULES")

    def test_section_schedule_has_valid_time(self):
        """Section schedules must have valid hour/minute values."""
        for section, (task_id, hour, minute) in SECTION_SCHEDULES.items():
            self.assertGreaterEqual(hour, 0)
            self.assertLessEqual(hour, 23)
            self.assertGreaterEqual(minute, 0)
            self.assertLessEqual(minute, 59)

    def test_build_schedule_panel_returns_list(self):
        """build_schedule_panel returns a sorted list of schedule items."""
        result = build_schedule_panel()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), len(SECTION_SCHEDULES))

    def test_build_schedule_panel_sorted_by_time(self):
        """build_schedule_panel is sorted by fires_at."""
        result = build_schedule_panel()
        times = [item["fires_at"] for item in result]
        self.assertEqual(times, sorted(times))

    def test_fetch_traffic_parses_views_data(self):
        """fetch_traffic correctly parses GitHub traffic API response."""
        gh_response = json.dumps({
            "count": 1500,
            "uniques": 300,
            "views": [
                {"timestamp": "2026-05-01T00:00:00Z", "count": 100, "uniques": 20},
                {"timestamp": "2026-05-02T00:00:00Z", "count": 200, "uniques": 30},
            ]
        })

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=gh_response)
            result = fetch_traffic()

        self.assertEqual(result["total_count"], 1500)
        self.assertEqual(result["total_uniques"], 300)
        self.assertEqual(len(result["days"]), 2)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_update_history_persists_traffic_days(self):
        """update_history appends traffic days to history file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            history_file = Path(tmpdir) / "history.json"
            with patch.object(_mod, "HISTORY_FILE", history_file):
                traffic = {
                    "days": [
                        {"date": "2026-05-01", "count": 100, "uniques": 20},
                        {"date": "2026-05-02", "count": 150, "uniques": 25},
                    ]
                }
                history = update_history(traffic)

        self.assertIn("2026-05-01", history)
        self.assertIn("2026-05-02", history)
        self.assertEqual(history["2026-05-01"]["count"], 100)

    def test_update_history_merges_with_existing(self):
        """update_history merges new data with existing history."""
        with tempfile.TemporaryDirectory() as tmpdir:
            history_file = Path(tmpdir) / "history.json"
            existing = {"2026-04-30": {"count": 50, "uniques": 10}}
            history_file.write_text(json.dumps(existing))

            with patch.object(_mod, "HISTORY_FILE", history_file):
                traffic = {
                    "days": [{"date": "2026-05-01", "count": 100, "uniques": 20}]
                }
                history = update_history(traffic)

        self.assertIn("2026-04-30", history)
        self.assertIn("2026-05-01", history)

    def test_scan_content_reads_posts(self):
        """scan_content correctly reads posts from content directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            content_dir = Path(tmpdir)
            dreams_dir = content_dir / "dreams"
            dreams_dir.mkdir()

            post = """---
title: "Test Dream"
date: 2026-05-01T09:00:00-07:00
---
Dream content here.
"""
            (dreams_dir / "2026-05-01-dream.md").write_text(post)
            (dreams_dir / "_index.md").write_text("")  # Should be skipped

            with patch.object(_mod, "CONTENT_DIR", content_dir):
                result = scan_content()

        self.assertIn("dreams", result)
        self.assertGreater(result["dreams"]["post_count"], 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_fetch_top_paths_infers_section(self):
        """fetch_top_paths correctly infers section from URL path."""
        gh_response = json.dumps([
            {"path": "/dreams/2026-05-01-my-dream/", "title": "Dream Post",
             "count": 50, "uniques": 10},
            {"path": "/essays/my-essay/", "title": "Essay Post",
             "count": 30, "uniques": 8},
            {"path": "/other/page/", "title": "Other",
             "count": 5, "uniques": 2},
        ])

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=gh_response)
            result = fetch_top_paths()

        sections = {r["section"] for r in result}
        self.assertIn("dreams", sections)
        self.assertIn("essays", sections)
        self.assertIn("other", sections)

    def test_main_writes_stats_file(self):
        """main() writes stats JSON file to STATS_FILE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stats_file = Path(tmpdir) / "journal_stats.json"

            with patch.object(_mod, "STATS_FILE", stats_file):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1, stdout="")
                    with patch.object(_mod, "CONTENT_DIR", Path("/nonexistent")):
                        with patch.object(_mod, "HISTORY_FILE", Path(tmpdir) / "history.json"):
                            _mod.main()

        self.assertTrue(stats_file.exists())
        data = json.loads(stats_file.read_text())
        self.assertIn("polled_at", data)
        self.assertIn("totals", data)


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

    def test_key_functions_callable(self):
        for fn in [fetch_traffic, fetch_top_paths, fetch_referrers, scan_content,
                   get_scheduler_state, next_run_time, update_history, build_schedule_panel]:
            self.assertTrue(callable(fn))

    def test_sections_nonempty(self):
        self.assertGreater(len(SECTIONS), 0)

    def test_stats_and_history_files_defined(self):
        self.assertIsInstance(_mod.STATS_FILE, Path)
        self.assertIsInstance(_mod.HISTORY_FILE, Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
