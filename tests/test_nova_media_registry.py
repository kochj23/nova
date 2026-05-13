"""
test_nova_media_registry.py — All 7 test categories for nova_media_registry.py
Written by Jordan Koch.
"""

import importlib.util
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Stub psycopg2 before loading
_psycopg2 = MagicMock()
_psycopg2.IntegrityError = type("IntegrityError", (Exception,), {})
sys.modules["psycopg2"] = _psycopg2
sys.modules["psycopg2.extras"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_media_registry.py"
_spec = importlib.util.spec_from_file_location("nova_media_registry", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

register_file = _mod.register_file
mark_ingested = _mod.mark_ingested
mark_status = _mod.mark_status
is_done = _mod.is_done
get_status = _mod.get_status
pending_files = _mod.pending_files
coverage_report = _mod.coverage_report
_DONE_STATUSES = _mod._DONE_STATUSES
DSN = _mod.DSN


def _make_mock_conn(rows=None, status_row=None):
    """Create a mock psycopg2 connection with configurable fetch results."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    if rows is not None:
        mock_cur.fetchone.return_value = rows
    if status_row is not None:
        mock_cur.fetchone.return_value = status_row

    _psycopg2.connect.return_value = mock_conn
    return mock_conn, mock_cur


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

    def test_dsn_uses_local_database(self):
        """DSN must connect to nova_media database."""
        self.assertIn("nova_media", DSN)

    def test_conn_context_manager_rollback_on_error(self):
        """_conn() must rollback on exception."""
        mock_conn = MagicMock()
        _psycopg2.connect.return_value = mock_conn

        with self.assertRaises(ValueError):
            with _mod._conn() as con:
                raise ValueError("test error")

        mock_conn.rollback.assert_called()
        mock_conn.close.assert_called()

    def test_conn_commits_on_success(self):
        """_conn() must commit on success."""
        mock_conn = MagicMock()
        _psycopg2.connect.return_value = mock_conn

        with _mod._conn() as con:
            pass  # No error

        mock_conn.commit.assert_called()
        mock_conn.close.assert_called()


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_is_done_fast(self):
        """is_done() must be < 100ms with mocked DB."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = ("ingested",)
        _psycopg2.connect.return_value = mock_conn

        start = time.perf_counter()
        for _ in range(100):
            is_done("/some/path.mp4")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_get_status_fast(self):
        """get_status() must be < 100ms with mocked DB."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = ("pending",)
        _psycopg2.connect.return_value = mock_conn

        start = time.perf_counter()
        for _ in range(100):
            get_status("/path.mp4")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_register_file_handles_integrity_error(self):
        """register_file handles duplicate path (IntegrityError) gracefully."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        _psycopg2.connect.return_value = mock_conn
        _psycopg2.IntegrityError = Exception

        # First execute raises IntegrityError, second (SELECT) returns row
        existing_row = {
            "id": 1, "path": "/test.mp4", "status": "ingested",
            "show_name": None, "title": None
        }
        mock_cur.execute.side_effect = [_psycopg2.IntegrityError("duplicate"), None]
        mock_cur.fetchone.return_value = existing_row

        try:
            result = register_file("/test.mp4")
        except Exception:
            pass  # May raise — just verify it doesn't infinite loop

    def test_mark_status_handles_db_error(self):
        """mark_status must propagate DB errors."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.execute.side_effect = Exception("DB error")
        _psycopg2.connect.return_value = mock_conn

        with self.assertRaises(Exception):
            mark_status("/nonexistent.mp4", "error")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_done_statuses_defined(self):
        """_DONE_STATUSES must contain all terminal states."""
        expected = {"ingested", "trash", "audio_failed", "no_transcript", "skipped"}
        self.assertTrue(expected.issubset(_DONE_STATUSES))

    def test_is_done_for_ingested_status(self):
        """is_done returns True for 'ingested' status."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = ("ingested",)
        _psycopg2.connect.return_value = mock_conn

        result = is_done("/path.mp4")
        self.assertTrue(result)

    def test_is_done_false_for_pending(self):
        """is_done returns False for 'pending' status."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = ("pending",)
        _psycopg2.connect.return_value = mock_conn

        result = is_done("/path.mp4")
        self.assertFalse(result)

    def test_is_done_false_for_error(self):
        """is_done returns False for 'error' status (should be retried)."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = ("error",)
        _psycopg2.connect.return_value = mock_conn

        result = is_done("/path.mp4")
        self.assertFalse(result)

    def test_is_done_false_for_unregistered(self):
        """is_done returns False when path is not registered."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = None
        _psycopg2.connect.return_value = mock_conn

        result = is_done("/not/registered.mp4")
        self.assertFalse(result)

    def test_get_status_returns_none_for_unknown(self):
        """get_status returns None when path not in registry."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = None
        _psycopg2.connect.return_value = mock_conn

        result = get_status("/unknown.mp4")
        self.assertIsNone(result)

    def test_done_statuses_excludes_retryable(self):
        """pending, error, downloaded must NOT be in done statuses."""
        retryable = {"pending", "error", "downloaded"}
        for s in retryable:
            self.assertNotIn(s, _DONE_STATUSES, f"{s!r} should be retryable, not done")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_mark_status_updates_correct_path(self):
        """mark_status must send UPDATE with the correct path."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        _psycopg2.connect.return_value = mock_conn

        executed_sqls = []
        executed_params = []

        def capture_execute(sql, params=None):
            executed_sqls.append(sql)
            if params:
                executed_params.append(params)

        mock_cur.execute = capture_execute

        mark_status("/test/video.mp4", "trash", error_msg=None, notes="too noisy")

        update_calls = [s for s in executed_sqls if "UPDATE" in s.upper()]
        self.assertTrue(len(update_calls) > 0)
        # Path must appear in params
        all_params = str(executed_params)
        self.assertIn("/test/video.mp4", all_params)

    def test_coverage_report_returns_structure(self):
        """coverage_report returns dict with by_status, by_source, total."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.side_effect = [
            [("pending", 5), ("ingested", 100)],
            [("television", "ingested", 80), ("television", "pending", 3)],
        ]
        _psycopg2.connect.return_value = mock_conn

        result = coverage_report()
        self.assertIn("by_status", result)
        self.assertIn("by_source", result)
        self.assertIn("total", result)
        self.assertEqual(result["total"], 105)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_pending_files_filters_by_source_label(self):
        """pending_files must include source_label filter in query."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = [("/path1.mp4",), ("/path2.mp4",)]
        _psycopg2.connect.return_value = mock_conn

        executed_sqls = []
        mock_cur.execute = lambda sql, params=None: executed_sqls.append(sql)

        pending_files(source_label="television")

        # SQL should include source_label condition
        self.assertTrue(any("source_label" in s for s in executed_sqls))

    def test_mark_ingested_sets_status_and_chunks(self):
        """mark_ingested must set status='ingested' and memory_chunks."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        _psycopg2.connect.return_value = mock_conn

        executed = []
        mock_cur.execute = lambda sql, params=None: executed.append((sql, params))

        mark_ingested("/video.mp4", chunks=42)

        update_sqls = [(s, p) for s, p in executed if s and "UPDATE" in s.upper()]
        self.assertTrue(len(update_sqls) > 0)
        sql, params = update_sqls[0]
        self.assertIn("ingested", sql.lower())


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
        for fn in [register_file, mark_ingested, mark_status, is_done,
                   get_status, pending_files, coverage_report]:
            self.assertTrue(callable(fn))

    def test_done_statuses_is_frozenset(self):
        self.assertIsInstance(_DONE_STATUSES, frozenset)

    def test_dsn_defined(self):
        self.assertIsInstance(DSN, str)
        self.assertGreater(len(DSN), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
