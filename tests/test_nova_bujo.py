"""
test_nova_bujo.py — All 7 test categories for nova_bujo.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_bujo.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"

sys.modules["nova_config"] = _nova_cfg

_mod = load_script_compat(_SCRIPT, "nova_bujo")

load_json = _mod.load_json
save_json = _mod.save_json
short_id = _mod.short_id
get_day = _mod.get_day
find_task = _mod.find_task
is_stale = _mod.is_stale
is_stuck = _mod.is_stuck
get_stale_tasks = _mod.get_stale_tasks
get_stuck_tasks = _mod.get_stuck_tasks
fmt_task = _mod.fmt_task
remember = _mod.remember


def _make_task(title="Test Task", priority="medium", status="open", days_old=0, migrations=0):
    created = (datetime.now() - timedelta(days=days_old)).isoformat()
    return {
        "id": short_id(),
        "title": title,
        "priority": priority,
        "tags": [],
        "status": status,
        "created_at": created,
        "completed_at": None,
        "migration_history": [{"from": "x", "to": "y"}] * migrations,
    }


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-", "password ="]:
            self.assertNotIn(pat, src, f"Credential: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)

    def test_vector_remember_marks_privacy_local_only(self):
        """remember() must mark data as local-only privacy."""
        payloads = []
        def capture(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))
        with patch("urllib.request.urlopen", side_effect=capture):
            remember("test task entry", ["tag1"])
        if payloads:
            meta = payloads[0].get("metadata", {})
            self.assertEqual(meta.get("privacy"), "local-only")

    def test_task_id_uses_uuid_not_sequential(self):
        """Task IDs must use UUID hex, producing unique non-predictable values."""
        ids = [short_id() for _ in range(100)]
        # All IDs should be unique (UUID-based, not sequential)
        self.assertEqual(len(set(ids)), 100)
        # All should be 8 hex chars
        import re
        for id_ in ids:
            self.assertRegex(id_, r'^[0-9a-f]{8}$', f"ID not hex: {id_!r}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_load_json_fast_on_large_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            data = {str(i): {"tasks": [{"id": str(j)} for j in range(10)]} for i in range(500)}
            json.dump(data, f)
            tmp = Path(f.name)
        try:
            start = time.perf_counter()
            loaded = load_json(tmp)
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.5)
            self.assertEqual(len(loaded), 500)
        finally:
            tmp.unlink(missing_ok=True)

    def test_get_stale_tasks_fast_on_many_tasks(self):
        data = {}
        for i in range(100):
            d = (datetime.now() - timedelta(days=i % 20)).date().isoformat()
            data[d] = {"tasks": [_make_task(days_old=i % 20) for _ in range(10)]}
        start = time.perf_counter()
        get_stale_tasks(data)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_short_id_generation_fast(self):
        start = time.perf_counter()
        for _ in range(10000):
            short_id()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            try:
                remember("test", [])
            except Exception as e:
                self.fail(f"remember raised: {e}")

    def test_git_commit_handles_failure_gracefully(self):
        """git_commit catches subprocess.CalledProcessError and logs it."""
        import subprocess
        with patch("subprocess.run",
                   side_effect=subprocess.CalledProcessError(1, "git")):
            try:
                _mod.git_commit("test commit")
            except Exception as e:
                self.fail(f"git_commit raised: {e}")

    def test_load_json_returns_empty_on_corrupt(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("CORRUPT{{{{")
            tmp = Path(f.name)
        try:
            result = load_json(tmp)
            self.assertEqual(result, {})
        finally:
            tmp.unlink(missing_ok=True)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_json_missing_file_returns_empty(self):
        result = load_json(Path("/nonexistent/path.json"))
        self.assertEqual(result, {})

    def test_save_load_json_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            data = {"key": "value", "num": 42}
            save_json(tmp, data)
            loaded = load_json(tmp)
            self.assertEqual(loaded["key"], "value")
            self.assertEqual(loaded["num"], 42)
        finally:
            tmp.unlink(missing_ok=True)

    def test_short_id_is_8_chars(self):
        for _ in range(100):
            self.assertEqual(len(short_id()), 8)

    def test_get_day_initializes_keys(self):
        data = {}
        day = get_day(data, "2026-01-01")
        self.assertIn("tasks", day)
        self.assertIn("events", day)
        self.assertIn("notes", day)

    def test_get_day_reuses_existing(self):
        data = {"2026-01-01": {"tasks": [{"id": "abc"}], "events": [], "notes": []}}
        day = get_day(data, "2026-01-01")
        self.assertEqual(len(day["tasks"]), 1)

    def test_find_task_by_id(self):
        task = _make_task("Find Me Task")
        data = {date.today().isoformat(): {"tasks": [task], "events": [], "notes": []}}
        day_key, idx, found = find_task(data, task["id"][:8])
        self.assertIsNotNone(found)
        self.assertEqual(found["title"], "Find Me Task")

    def test_find_task_returns_none_on_missing(self):
        data = {}
        day_key, idx, found = find_task(data, "notexist")
        self.assertIsNone(found)

    def test_is_stale_true_when_old(self):
        task = _make_task(days_old=10)
        self.assertTrue(is_stale(task))

    def test_is_stale_false_when_recent(self):
        task = _make_task(days_old=1)
        self.assertFalse(is_stale(task))

    def test_is_stale_false_when_completed(self):
        task = _make_task(days_old=30, status="completed")
        self.assertFalse(is_stale(task))

    def test_is_stuck_true_on_many_migrations(self):
        task = _make_task(migrations=3)
        self.assertTrue(is_stuck(task))

    def test_is_stuck_false_on_few_migrations(self):
        task = _make_task(migrations=1)
        self.assertFalse(is_stuck(task))

    def test_fmt_task_includes_id_and_title(self):
        task = _make_task("My Important Task")
        output = fmt_task(task)
        self.assertIn(task["id"][:8], output)
        self.assertIn("My Important Task", output)

    def test_fmt_task_shows_stale_flag(self):
        task = _make_task(days_old=10)
        output = fmt_task(task)
        self.assertIn("STALE", output)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_add_task_then_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily.json"
            daily.write_text("{}\n")
            with patch.object(_mod, "DAILY_FILE", daily):
                with patch.object(_mod, "TODAY", date.today().isoformat()):
                    with patch.object(_mod, "git_commit"):
                        with patch("urllib.request.urlopen",
                                   return_value=MagicMock(__enter__=lambda s: s,
                                                          __exit__=MagicMock(return_value=False))):
                            # Add task
                            args = MagicMock()
                            args.type = "task"
                            args.text = "Integration test task"
                            args.priority = "high"
                            args.tag = ["test"]
                            args.date = None
                            _mod.cmd_add(args)

                            data = load_json(daily)
                            today = date.today().isoformat()
                            self.assertEqual(len(data[today]["tasks"]), 1)
                            task_id = data[today]["tasks"][0]["id"]

                            # Complete it
                            c_args = MagicMock()
                            c_args.task_id = task_id[:8]
                            _mod.cmd_complete(c_args)

                            data2 = load_json(daily)
                            self.assertEqual(data2[today]["tasks"][0]["status"], "completed")

    def test_add_event_stores_in_daily(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily.json"
            daily.write_text("{}\n")
            with patch.object(_mod, "DAILY_FILE", daily):
                with patch.object(_mod, "TODAY", date.today().isoformat()):
                    with patch.object(_mod, "git_commit"):
                        with patch("urllib.request.urlopen",
                                   return_value=MagicMock(__enter__=lambda s: s,
                                                          __exit__=MagicMock(return_value=False))):
                            args = MagicMock()
                            args.type = "event"
                            args.text = "Team meeting"
                            args.priority = None
                            args.tag = []
                            args.date = None
                            _mod.cmd_add(args)

                            data = load_json(daily)
                            today = date.today().isoformat()
                            self.assertEqual(len(data[today]["events"]), 1)

    def test_migrate_records_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily.json"
            today = date.today().isoformat()
            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            task = _make_task("Migrate me task")
            daily.write_text(json.dumps({today: {"tasks": [task], "events": [], "notes": []}}))

            with patch.object(_mod, "DAILY_FILE", daily):
                with patch.object(_mod, "TODAY", today):
                    with patch.object(_mod, "git_commit"):
                        args = MagicMock()
                        args.task_id = task["id"][:8]
                        args.to = tomorrow
                        _mod.cmd_migrate(args)

                        data = load_json(daily)
                        old_task = data[today]["tasks"][0]
                        self.assertEqual(old_task["status"], "migrated")
                        new_tasks = data[tomorrow]["tasks"]
                        self.assertEqual(len(new_tasks), 1)
                        self.assertEqual(new_tasks[0]["status"], "open")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_cmd_digest_quiet_no_slack(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily.json"
            monthly = Path(tmp) / "monthly.json"
            daily.write_text("{}\n")
            monthly.write_text("{}\n")
            with patch.object(_mod, "DAILY_FILE", daily):
                with patch.object(_mod, "MONTHLY_FILE", monthly):
                    with patch.object(_mod, "TODAY", date.today().isoformat()):
                        posts = []
                        _nova_cfg.post_both.side_effect = lambda m, **kw: posts.append(m)
                        args = MagicMock()
                        args.quiet = True
                        _mod.cmd_digest(args)
                        self.assertEqual(len(posts), 0)
                        _nova_cfg.post_both.side_effect = None

    def test_stale_tasks_appear_in_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily.json"
            monthly = Path(tmp) / "monthly.json"
            stale_task = _make_task("Stale old task", days_old=10)
            today = date.today().isoformat()
            daily.write_text(json.dumps({today: {"tasks": [stale_task], "events": [], "notes": []}}))
            monthly.write_text("{}\n")
            with patch.object(_mod, "DAILY_FILE", daily):
                with patch.object(_mod, "MONTHLY_FILE", monthly):
                    with patch.object(_mod, "TODAY", today):
                        args = MagicMock()
                        args.quiet = True
                        result = _mod.cmd_digest(args)
            self.assertIn("Stale", result)

    def test_completion_rate_in_weekly_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily.json"
            monthly = Path(tmp) / "monthly.json"
            today = date.today().isoformat()
            tasks = [_make_task("Done task", status="completed"), _make_task("Open task")]
            tasks[0]["completed_at"] = datetime.now().isoformat()
            daily.write_text(json.dumps({today: {"tasks": tasks, "events": [], "notes": []}}))
            monthly.write_text("{}\n")
            with patch.object(_mod, "DAILY_FILE", daily):
                with patch.object(_mod, "MONTHLY_FILE", monthly):
                    with patch.object(_mod, "TODAY", today):
                        with patch("urllib.request.urlopen",
                                   return_value=MagicMock(__enter__=lambda s: s,
                                                          __exit__=MagicMock(return_value=False))):
                            args = MagicMock()
                            args.quiet = True
                            result = _mod.cmd_weekly(args)
            self.assertIn("50%", result)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_key_functions_exist(self):
        for fn in ["main", "cmd_add", "cmd_complete", "cmd_cancel", "cmd_migrate",
                   "cmd_list", "cmd_stale", "cmd_month", "cmd_future",
                   "cmd_collection", "cmd_digest", "cmd_weekly", "build_parser"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_parser_builds_without_error(self):
        parser = _mod.build_parser()
        self.assertIsNotNone(parser)

    def test_constants_defined(self):
        self.assertGreater(_mod.STALE_DAYS, 0)
        self.assertGreater(_mod.STUCK_MIGRATES, 0)

    def test_priority_icons_cover_all_levels(self):
        for lvl in ["high", "medium", "low"]:
            self.assertIn(lvl, _mod.PRIORITY_ICON)

    def test_status_icons_cover_all_statuses(self):
        for st in ["open", "completed", "cancelled", "migrated"]:
            self.assertIn(st, _mod.STATUS_ICON)


if __name__ == "__main__":
    unittest.main(verbosity=2)
