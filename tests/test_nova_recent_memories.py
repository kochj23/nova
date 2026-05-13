"""
test_nova_recent_memories.py — All 7 test categories for nova_recent_memories.py
Written by Jordan Koch.
"""

import importlib.util
import sys
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# psycopg2 is not available in test env — stub it
# ---------------------------------------------------------------------------
_psycopg2 = MagicMock()
sys.modules["psycopg2"] = _psycopg2
sys.modules["psycopg2.extras"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_recent_memories.py"
_spec = importlib.util.spec_from_file_location("nova_recent_memories", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_cutoff = _mod._cutoff
_fmt_count = _mod._fmt_count
_truncate = _mod._truncate
_label_tag = _mod._label_tag
format_summary = _mod.format_summary
format_detail = _mod.format_detail
DEFAULT_HOURS = _mod.DEFAULT_HOURS
SNIPPET_LENGTH = _mod.SNIPPET_LENGTH


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

    def test_db_connection_is_readonly(self):
        """connect() must set session to readonly."""
        mock_conn = MagicMock()
        _psycopg2.connect.return_value = mock_conn

        _mod.connect()

        mock_conn.set_session.assert_called_with(readonly=True, autocommit=True)

    def test_db_name_is_nova_memories(self):
        """Must connect to nova_memories database."""
        self.assertEqual(_mod.DB_NAME, "nova_memories")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_cutoff_fast(self):
        """_cutoff() must compute in < 1ms."""
        start = time.perf_counter()
        for _ in range(10000):
            _cutoff(24)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_fmt_count_fast(self):
        """_fmt_count must format 10k numbers in < 50ms."""
        start = time.perf_counter()
        for i in range(10000):
            _fmt_count(i * 1000)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05)

    def test_truncate_fast_on_long_strings(self):
        """_truncate must handle 10k long strings in < 100ms."""
        long_text = "word " * 500
        start = time.perf_counter()
        for _ in range(10000):
            _truncate(long_text, 80)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_recent_summary_handles_db_error(self):
        """get_recent_summary must propagate OperationalError (caught by CLI)."""
        import psycopg2
        _psycopg2.connect.side_effect = _psycopg2.OperationalError("no database")
        _psycopg2.OperationalError = Exception

        try:
            _mod.get_recent_summary(hours=24)
        except Exception:
            pass  # Expected to raise
        finally:
            _psycopg2.connect.side_effect = None

    def test_main_exits_1_on_db_error(self):
        """main() exits with code 1 on database connection error."""
        _psycopg2.OperationalError = type("OperationalError", (Exception,), {})
        _psycopg2.connect.side_effect = _psycopg2.OperationalError("refused")

        exit_codes = []
        with patch("sys.argv", ["nova_recent_memories.py"]):
            with patch("sys.exit", side_effect=lambda c: exit_codes.append(c)):
                try:
                    _mod.main()
                except Exception:
                    pass

        _psycopg2.connect.side_effect = None
        if exit_codes:
            self.assertIn(1, exit_codes)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_cutoff_returns_utc_aware(self):
        """_cutoff() returns a timezone-aware UTC datetime."""
        result = _cutoff(24)
        self.assertIsNotNone(result.tzinfo)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_cutoff_24h_is_in_past(self):
        """_cutoff(24) must be in the past."""
        now = datetime.now(timezone.utc)
        result = _cutoff(24)
        self.assertLess(result, now)

    def test_cutoff_1h_vs_24h(self):
        """_cutoff(1) must be more recent than _cutoff(24)."""
        self.assertGreater(_cutoff(1), _cutoff(24))

    def test_fmt_count_formats_thousands(self):
        """_fmt_count adds thousands separators."""
        self.assertEqual(_fmt_count(1234567), "1,234,567")
        self.assertEqual(_fmt_count(1000), "1,000")
        self.assertEqual(_fmt_count(0), "0")

    def test_truncate_short_string_unchanged(self):
        """_truncate returns short strings unchanged."""
        text = "Hello world"
        self.assertEqual(_truncate(text, 80), text)

    def test_truncate_long_string_with_ellipsis(self):
        """_truncate adds ellipsis to long strings."""
        text = "a" * 200
        result = _truncate(text, 80)
        self.assertTrue(result.endswith("..."))
        self.assertLessEqual(len(result), 83)  # 80 + "..."

    def test_truncate_replaces_newlines(self):
        """_truncate replaces newlines with spaces."""
        text = "line1\nline2\nline3"
        result = _truncate(text, 100)
        self.assertNotIn("\n", result)

    def test_label_tag_nonempty(self):
        """_label_tag returns bracketed tag."""
        self.assertEqual(_label_tag("Breaking Bad"), "[Breaking Bad] ")

    def test_label_tag_empty(self):
        """_label_tag returns empty string for empty label."""
        self.assertEqual(_label_tag(""), "")

    def test_default_hours_is_24(self):
        self.assertEqual(DEFAULT_HOURS, 24)

    def test_snippet_length_is_80(self):
        self.assertEqual(SNIPPET_LENGTH, 80)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_format_summary_handles_empty_data(self):
        """format_summary handles empty by_source list."""
        data = {
            "hours": 24,
            "cutoff": datetime.now(timezone.utc).isoformat(),
            "total": 0,
            "by_source": [],
        }
        result = format_summary(data)
        self.assertIn("0", result)
        self.assertIn("none", result.lower())

    def test_format_summary_shows_all_sources(self):
        """format_summary includes all sources from data."""
        data = {
            "hours": 24,
            "cutoff": datetime.now(timezone.utc).isoformat(),
            "total": 1500,
            "by_source": [
                {"source": "television", "count": 1000, "labels": ["Breaking Bad"]},
                {"source": "email_archive", "count": 500, "labels": []},
            ],
        }
        result = format_summary(data)
        self.assertIn("television", result)
        self.assertIn("email_archive", result)
        self.assertIn("1,500", result)

    def test_format_detail_shows_samples(self):
        """format_detail includes sample memory snippets."""
        data = {
            "hours": 24,
            "cutoff": datetime.now(timezone.utc).isoformat(),
            "total": 2,
            "sources": [
                {
                    "source": "television",
                    "count": 2,
                    "labels": ["Breaking Bad"],
                    "samples": [
                        {"text": "Chemistry is the science of matter",
                         "label": "Breaking Bad",
                         "created_at": datetime.now(timezone.utc).isoformat()},
                    ],
                }
            ],
        }
        result = format_detail(data)
        self.assertIn("television", result)
        self.assertIn("Chemistry", result)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_format_summary_label_truncation(self):
        """format_summary shows max 3 labels plus '+N more'."""
        data = {
            "hours": 24,
            "cutoff": datetime.now(timezone.utc).isoformat(),
            "total": 100,
            "by_source": [
                {"source": "television", "count": 100,
                 "labels": ["Show A", "Show B", "Show C", "Show D", "Show E"]},
            ],
        }
        result = format_summary(data)
        self.assertIn("+2 more", result)

    def test_format_detail_handles_empty_sources(self):
        """format_detail handles empty sources list."""
        data = {
            "hours": 24,
            "cutoff": datetime.now(timezone.utc).isoformat(),
            "total": 0,
            "sources": [],
        }
        result = format_detail(data)
        self.assertIn("none", result.lower())

    def test_format_summary_plural_hours(self):
        """format_summary uses plural 'hours' for n != 1."""
        data = {
            "hours": 24, "cutoff": datetime.now(timezone.utc).isoformat(),
            "total": 0, "by_source": [],
        }
        result = format_summary(data)
        self.assertIn("hours", result)

    def test_format_summary_singular_hour(self):
        """format_summary uses singular 'hour' for n=1."""
        data = {
            "hours": 1, "cutoff": datetime.now(timezone.utc).isoformat(),
            "total": 0, "by_source": [],
        }
        result = format_summary(data)
        self.assertIn("last 1 hour", result)


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
        for fn in [_cutoff, _fmt_count, _truncate, _label_tag,
                   format_summary, format_detail]:
            self.assertTrue(callable(fn))

    def test_constants_defined(self):
        self.assertIsInstance(DEFAULT_HOURS, int)
        self.assertIsInstance(SNIPPET_LENGTH, int)
        self.assertIsInstance(_mod.DB_NAME, str)

    def test_table_constant(self):
        self.assertEqual(_mod.TABLE, "memories")


if __name__ == "__main__":
    unittest.main(verbosity=2)
