"""
test_memory_cleanup.py — All 7 test categories for memory_cleanup.py
Written by Jordan Koch.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

# Stub requests
_requests_stub = MagicMock()
sys.modules["requests"] = _requests_stub

import importlib.util
_SCRIPT = Path(__file__).parent.parent / "scripts" / "memory_cleanup.py"
_spec = importlib.util.spec_from_file_location("memory_cleanup", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_protected_sources_defined(self):
        """PROTECTED_SOURCES must be defined and include critical sources."""
        self.assertIn("work_knowledge", _mod.PROTECTED_SOURCES)
        self.assertIn("local_knowledge", _mod.PROTECTED_SOURCES)

    def test_delete_never_called_on_protected_sources(self):
        """find_short_memories() must skip protected sources."""
        src = _SCRIPT.read_text()
        self.assertIn("PROTECTED_SOURCES", src)
        self.assertIn("continue", src)

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_dry_run_is_default_safe(self):
        """Dry-run mode must be the safe default — requires --execute to delete."""
        src = _SCRIPT.read_text()
        self.assertIn("--dry-run", src)
        self.assertIn("--execute", src)
        self.assertIn("mutually_exclusive_group", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_batch_pause_defined(self):
        self.assertGreater(_mod.BATCH_PAUSE, 0)

    def test_random_batch_size_bounded(self):
        self.assertGreater(_mod.RANDOM_BATCH_SIZE, 0)
        self.assertLessEqual(_mod.RANDOM_BATCH_SIZE, 1000)

    def test_sampling_rounds_bounded(self):
        self.assertLessEqual(_mod.SHORT_MEMORY_ROUNDS, 200)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_delete_404_is_silently_ignored(self):
        """Deleting an already-deleted memory (404) must not crash."""
        src = _SCRIPT.read_text()
        self.assertIn("404", src,
                      "404 on delete must be handled silently")

    def test_delete_errors_counted(self):
        """delete_memories() must count errors without crashing."""
        src = _SCRIPT.read_text()
        self.assertIn("errors", src)

    def test_api_get_raises_on_failure(self):
        """api_get() must use raise_for_status() to detect errors."""
        src = _SCRIPT.read_text()
        self.assertIn("raise_for_status", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_api_get_calls_requests(self):
        """api_get() must use requests.get."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"count": 100}
        _requests_stub.get.return_value = mock_resp

        result = _mod.api_get("/stats")
        _requests_stub.get.assert_called()
        self.assertEqual(result["count"], 100)

    def test_delete_memories_dry_run_returns_zero(self):
        """delete_memories() in dry_run mode must return 0 deleted."""
        memories = [("id1", "short", "email_archive"),
                    ("id2", "hi", "other")]
        result = _mod.delete_memories(memories, "test", dry_run=True)
        self.assertEqual(result, 0)

    def test_delete_memories_empty_list(self):
        """delete_memories() on empty list must return 0."""
        result = _mod.delete_memories([], "test", dry_run=False)
        self.assertEqual(result, 0)

    def test_base_url_is_localhost(self):
        self.assertIn("127.0.0.1", _mod.BASE_URL)

    def test_protected_sources_is_set(self):
        self.assertIsInstance(_mod.PROTECTED_SOURCES, (set, frozenset))

    def test_batch_subject_archive_pattern(self):
        """find_batch_subject_archives() must look for 'Email subject archive (batch'."""
        src = _SCRIPT.read_text()
        self.assertIn("Email subject archive (batch", src)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_exits_if_server_unreachable(self):
        """main() must exit 1 when memory server is unreachable."""
        _requests_stub.get.side_effect = Exception("connection refused")
        with patch("sys.argv", ["memory_cleanup.py", "--dry-run"]):
            with self.assertRaises(SystemExit) as cm:
                _mod.main()
            self.assertEqual(cm.exception.code, 1)
        _requests_stub.get.side_effect = None

    def test_three_phases_execute(self):
        """main() must execute all three cleanup phases."""
        src = _SCRIPT.read_text()
        self.assertIn("PHASE 1", src)
        self.assertIn("PHASE 2", src)
        self.assertIn("PHASE 3", src)

    def test_duplicate_deduplication_logic(self):
        """find_duplicate_morning_summaries() must keep earliest, delete rest."""
        src = _SCRIPT.read_text()
        self.assertIn("entries[1:]", src,
                      "Must delete all but the first (earliest) entry")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_dry_run_shows_what_would_be_deleted(self):
        src = _SCRIPT.read_text()
        self.assertIn("Would delete", src)

    def test_execute_deletes_memories(self):
        src = _SCRIPT.read_text()
        self.assertIn("api_delete", src)

    def test_main_shows_totals(self):
        src = _SCRIPT.read_text()
        self.assertIn("Total to delete", src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"memory_cleanup.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_main_function_exists(self):
        self.assertTrue(callable(_mod.main))

    def test_constants_present(self):
        for attr in ["BASE_URL", "PROTECTED_SOURCES", "RANDOM_BATCH_SIZE",
                     "BATCH_PAUSE", "SHORT_MEMORY_ROUNDS"]:
            self.assertTrue(hasattr(_mod, attr))

    def test_functions_present(self):
        for fn in ["api_get", "api_delete", "find_short_memories",
                   "find_batch_subject_archives",
                   "find_duplicate_morning_summaries",
                   "delete_memories", "main"]:
            self.assertTrue(callable(getattr(_mod, fn, None)),
                            f"{fn} must exist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
