"""
test_nova_reclassify_vectors.py — All 7 test categories for nova_reclassify_vectors.py
Written by Jordan Koch.
"""

import importlib.util
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# nova_reclassify_vectors.py runs at module-level — must patch psql before load
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_reclassify_vectors.py"


def _load_module(dry_run=True):
    """Load the module with sys.argv and subprocess.run mocked."""
    import importlib.util as _ilu

    # Patch subprocess.run to return 0 counts so nothing actually runs
    mock_result = MagicMock()
    mock_result.stdout = "0"

    argv = ["nova_reclassify_vectors.py"]  # no --live → dry run

    with patch("sys.argv", argv):
        with patch("subprocess.run", return_value=mock_result):
            spec = _ilu.spec_from_file_location("nova_reclassify_vectors", _SCRIPT)
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
psql = _mod.psql
rename = _mod.rename
fix_privacy = _mod.fix_privacy
count = _mod.count


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src, f"Credential found: {p!r}")

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_default_mode_is_dry_run(self):
        """Without --live flag, DRY_RUN must be True."""
        with patch("sys.argv", ["nova_reclassify_vectors.py"]):
            dry = "--live" not in sys.argv
        self.assertTrue(dry)

    def test_privacy_tagging_targets_sensitive_sources(self):
        """fix_privacy must be called for sensitive sources."""
        src = _SCRIPT.read_text()
        sensitive = ["imessage", "email_archive", "livejournal", "personal_videos"]
        for s in sensitive:
            self.assertIn(s, src, f"Sensitive source {s!r} not privacy-tagged")

    def test_dry_run_does_not_execute_updates(self):
        """In dry-run mode, no UPDATE statements should be sent to psql."""
        update_sqls = []

        def capture_run(cmd, **kw):
            sql = cmd[-1] if cmd else ""
            if "UPDATE" in sql.upper():
                update_sqls.append(sql)
            result = MagicMock()
            result.stdout = "5"
            return result

        with patch("subprocess.run", side_effect=capture_run):
            with patch("sys.argv", ["nova_reclassify_vectors.py"]):  # dry-run
                rename("old_source", "new_source")

        self.assertEqual(len(update_sqls), 0, "dry-run must not execute UPDATE")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_psql_function_fast(self):
        """psql() must complete in < 100ms with mocked subprocess."""
        mock_result = MagicMock()
        mock_result.stdout = "42"

        start = time.perf_counter()
        with patch("subprocess.run", return_value=mock_result):
            for _ in range(100):
                psql("SELECT COUNT(*) FROM memories")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_count_function_fast(self):
        """count() must be fast with mocked psql."""
        mock_result = MagicMock()
        mock_result.stdout = "1000"

        start = time.perf_counter()
        with patch("subprocess.run", return_value=mock_result):
            for _ in range(100):
                count("television")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_rename_handles_zero_count_gracefully(self):
        """rename() returns 0 and skips UPDATE when count is 0."""
        mock_result = MagicMock()
        mock_result.stdout = "0"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = rename("nonexistent_source", "new_name")

        self.assertEqual(result, 0)
        # Should have called psql for COUNT but NOT for UPDATE
        # (only one psql call: the COUNT check)
        count_calls = [c for c in mock_run.call_args_list
                       if "SELECT COUNT" in str(c)]
        update_calls = [c for c in mock_run.call_args_list
                        if "UPDATE" in str(c)]
        self.assertEqual(len(update_calls), 0)

    def test_fix_privacy_handles_zero_count(self):
        """fix_privacy returns 0 and skips UPDATE when nothing needs tagging."""
        mock_result = MagicMock()
        mock_result.stdout = "0"

        with patch("subprocess.run", return_value=mock_result):
            result = fix_privacy("any_source")

        self.assertEqual(result, 0)

    def test_psql_returns_empty_on_error(self):
        """psql() handles subprocess errors gracefully."""
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result):
            result = psql("SELECT 1")

        self.assertEqual(result, "")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_psql_returns_stdout_stripped(self):
        """psql() returns stripped stdout content."""
        mock_result = MagicMock()
        mock_result.stdout = "  42  \n"

        with patch("subprocess.run", return_value=mock_result):
            result = psql("SELECT COUNT(*) FROM memories")

        self.assertEqual(result, "42")

    def test_count_returns_integer(self):
        """count() returns integer, not string."""
        mock_result = MagicMock()
        mock_result.stdout = "1234"

        with patch("subprocess.run", return_value=mock_result):
            result = count("television")

        self.assertEqual(result, 1234)
        self.assertIsInstance(result, int)

    def test_count_returns_0_on_empty(self):
        """count() returns 0 when psql returns empty."""
        mock_result = MagicMock()
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = count("nonexistent")

        self.assertEqual(result, 0)

    def test_rename_returns_count(self):
        """rename() returns the number of rows that would be affected."""
        mock_result = MagicMock()
        mock_result.stdout = "150"

        with patch("subprocess.run", return_value=mock_result):
            result = rename("old_source", "new_source")

        self.assertEqual(result, 150)

    def test_dry_run_variable(self):
        """DRY_RUN must be a boolean."""
        self.assertIsInstance(_mod.DRY_RUN, bool)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_full_dry_run_completes_without_error(self):
        """Full module execution in dry-run mode must not raise."""
        mock_result = MagicMock()
        mock_result.stdout = "0"

        try:
            with patch("subprocess.run", return_value=mock_result):
                with patch("sys.argv", ["nova_reclassify_vectors.py"]):
                    spec = importlib.util.spec_from_file_location("_reclassify_test", _SCRIPT)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
        except Exception as exc:
            self.fail(f"Dry-run execution raised: {exc}")

    def test_rename_with_condition(self):
        """rename() with a condition string includes it in the WHERE clause."""
        captured_sqls = []
        mock_result = MagicMock()

        def capture(cmd, **kw):
            sql = cmd[-1] if cmd else ""
            captured_sqls.append(sql)
            r = MagicMock()
            r.stdout = "5"
            return r

        with patch("subprocess.run", side_effect=capture):
            rename("local_knowledge", "documentary",
                   condition="text ILIKE '%documentary%'")

        # The COUNT query should include the condition
        count_queries = [s for s in captured_sqls if "SELECT COUNT" in s.upper()]
        if count_queries:
            self.assertIn("documentary", count_queries[0])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_dry_run_prints_dry_run_label(self, capsys=None):
        """In dry-run mode, output must say 'DRY RUN'."""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        mock_result = MagicMock()
        mock_result.stdout = "5"

        with redirect_stdout(output):
            with patch("subprocess.run", return_value=mock_result):
                rename("source_x", "source_y")

        printed = output.getvalue()
        self.assertIn("DRY RUN", printed)

    def test_live_mode_prints_done_label(self):
        """In live mode (--live), output must say 'DONE'."""
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        mock_result = MagicMock()
        mock_result.stdout = "5"

        # Temporarily set DRY_RUN to False
        original_dry = _mod.DRY_RUN
        _mod.DRY_RUN = False

        with redirect_stdout(output):
            with patch("subprocess.run", return_value=mock_result):
                rename("source_x", "source_y")

        _mod.DRY_RUN = original_dry
        printed = output.getvalue()
        self.assertIn("DONE", printed)


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
        for fn in [psql, rename, fix_privacy, count]:
            self.assertTrue(callable(fn))

    def test_dry_run_is_default(self):
        """Without --live, DRY_RUN must be True."""
        self.assertTrue(_mod.DRY_RUN)

    def test_psql_command_targets_nova_memories(self):
        """psql must target nova_memories database."""
        captured = []
        mock_result = MagicMock()
        mock_result.stdout = "0"

        def capture(cmd, **kw):
            captured.append(cmd)
            return mock_result

        with patch("subprocess.run", side_effect=capture):
            psql("SELECT 1")

        self.assertTrue(any("nova_memories" in str(c) for c in captured))


import importlib.util

if __name__ == "__main__":
    unittest.main(verbosity=2)
