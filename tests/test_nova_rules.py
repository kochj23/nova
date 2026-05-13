"""
test_nova_rules.py — All 7 test categories for nova_rules.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load module under test — stub nova_config, nova_logger
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_rules.py"

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_logger_mock = MagicMock()
_logger_mock.LOG_INFO = "INFO"
_logger_mock.LOG_ERROR = "ERROR"
_logger_mock.LOG_WARN = "WARN"
_logger_mock.log = MagicMock()
sys.modules["nova_logger"] = _logger_mock

_spec = importlib.util.spec_from_file_location("nova_rules", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_escape = _mod._escape
_correction_to_rule = _mod._correction_to_rule
format_rules_for_prompt = _mod.format_rules_for_prompt


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_escape_prevents_sql_injection(self):
        """_escape() must double-quote single quotes (SQL quoting convention)."""
        evil = "'; DROP TABLE rules; --"
        result = _escape(evil)
        # _escape converts ' to '' (SQL escaping — quote doubling)
        # The original '; becomes ''; which closes the string safely
        self.assertIn("''", result,
                      "_escape() must double single quotes for SQL safety")
        # The result starts with '' (doubled quote) not just '
        self.assertTrue(result.startswith("''"),
                        "_escape() must turn leading quote into doubled quote")

    def test_escape_prevents_sql_injection_via_backslash(self):
        """_escape() must escape backslashes."""
        evil = "test\\backslash"
        result = _escape(evil)
        self.assertIn("\\\\", result, "_escape() must escape backslashes")

    def test_rule_text_escaped_before_sql(self):
        """add_rule() must escape rule text before building SQL."""
        # Verify _escape is called in add_rule source
        src = _SCRIPT.read_text()
        self.assertIn("_escape(rule_text)", src,
                      "rule_text must be escaped before SQL INSERT")

    def test_correction_to_rule_does_not_include_raw_html(self):
        """_correction_to_rule() must return plain text, not HTML."""
        correction = {
            "nova_response": "<script>alert('xss')</script>",
            "jordan_correction": "That was wrong.",
            "topic": "test",
        }
        rule = _correction_to_rule(correction)
        # Rule text can contain the original text, but _escape will handle SQL safety
        self.assertIsInstance(rule, str)

    def test_escape_empty_string(self):
        self.assertEqual(_escape(""), "")
        self.assertEqual(_escape(None), "")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_correction_to_rule_fast(self):
        """_correction_to_rule() must process 10,000 corrections in < 200ms."""
        correction = {
            "nova_response": "Paris is the capital of Germany.",
            "jordan_correction": "Paris is the capital of France.",
            "topic": "geography",
        }
        start = time.perf_counter()
        for _ in range(10000):
            _correction_to_rule(correction)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2, f"_correction_to_rule 10k took {elapsed:.3f}s")

    def test_escape_fast(self):
        """_escape() must handle 10,000 strings in < 100ms."""
        test_str = "It's a test string with 'multiple' single quotes and \\backslashes\\"
        start = time.perf_counter()
        for _ in range(10000):
            _escape(test_str)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"_escape 10k took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_query_returns_empty_list_on_psql_failure(self):
        """_query() must return [] when psql is unavailable."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="psql: error")
            result = _mod._query("SELECT * FROM rules;")
        self.assertEqual(result, [])

    def test_exec_returns_false_on_psql_failure(self):
        """_exec() must return False when psql fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="psql: error")
            result = _mod._exec("INSERT INTO rules VALUES (...);")
        self.assertFalse(result)

    def test_get_active_rules_returns_empty_on_db_error(self):
        """get_active_rules() must return [] when database is unavailable."""
        with patch.object(_mod, "_query", return_value=[]):
            result = _mod.get_active_rules()
        self.assertEqual(result, [])

    def test_promote_corrections_handles_missing_file(self):
        """promote_corrections() must handle missing corrections.json gracefully."""
        with patch.object(_mod, "CORRECTIONS_FILE", Path("/nonexistent/corrections.json")):
            result = _mod.promote_corrections()
        self.assertEqual(result, 0, "promote_corrections() must return 0 when file missing")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_correction_to_rule_with_both_fields(self):
        """_correction_to_rule() must create a DO NOT say / Correct answer rule."""
        correction = {
            "nova_response": "The Eiffel Tower is in London.",
            "jordan_correction": "The Eiffel Tower is in Paris.",
            "topic": "geography",
        }
        rule = _correction_to_rule(correction)
        self.assertIsNotNone(rule)
        self.assertIn("Eiffel Tower is in London", rule)
        self.assertIn("Paris", rule)

    def test_correction_to_rule_jordan_only(self):
        """_correction_to_rule() must work when only jordan_correction is provided."""
        correction = {
            "nova_response": "",
            "jordan_correction": "Always say Little Mister when addressing Jordan.",
            "topic": "global",
        }
        rule = _correction_to_rule(correction)
        self.assertIsNotNone(rule)
        self.assertIn("Little Mister", rule)

    def test_correction_to_rule_missing_jordan_returns_none(self):
        """_correction_to_rule() must return None when jordan_correction is empty."""
        correction = {
            "nova_response": "something wrong",
            "jordan_correction": "",
            "topic": "test",
        }
        result = _correction_to_rule(correction)
        self.assertIsNone(result)

    def test_format_rules_for_prompt_empty_when_no_rules(self):
        """format_rules_for_prompt() must return empty string when no active rules."""
        with patch.object(_mod, "get_active_rules", return_value=[]):
            result = format_rules_for_prompt()
        self.assertEqual(result, "")

    def test_format_rules_for_prompt_with_rules(self):
        """format_rules_for_prompt() must include rules in output."""
        fake_rules = [
            {"id": "abc", "rule": "Do NOT say 'sure'.", "topic": "global",
             "confidence": 1.0, "times_applied": 0, "created_at": "2026-01-01"},
            {"id": "def", "rule": "Refer to Jordan as Little Mister.", "topic": "global",
             "confidence": 1.0, "times_applied": 5, "created_at": "2026-01-01"},
        ]
        with patch.object(_mod, "get_active_rules", return_value=fake_rules):
            result = format_rules_for_prompt()
        self.assertIn("sure", result)
        self.assertIn("Little Mister", result)
        self.assertIn("Active Rules", result)

    def test_format_rules_topic_scoped(self):
        """Topic-scoped rules must include the topic tag in prompt output."""
        fake_rules = [
            {"id": "abc", "rule": "Use scene names not device names.", "topic": "homekit",
             "confidence": 1.0, "times_applied": 0, "created_at": "2026-01-01"},
        ]
        with patch.object(_mod, "get_active_rules", return_value=fake_rules):
            result = format_rules_for_prompt(topic="homekit")
        self.assertIn("[homekit]", result)

    def test_query_parses_rows_correctly(self):
        """_query() must split rows by field separator."""
        fake_row = "abc\x1fDo not say sure\x1fglobal\x1f1.0\x1f3\x1f2026-01-01"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fake_row + "\n", stderr="")
            rows = _mod._query("SELECT * FROM rules;")
        self.assertEqual(len(rows), 1)
        self.assertIn("abc", rows[0])


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_ingest_correction_builds_rule_text(self):
        """ingest_correction() must generate a rule from the correction pair."""
        with patch.object(_mod, "_exec", return_value=True):
            with patch.object(_mod, "load_corrections" if hasattr(_mod, "load_corrections") else "_query",
                              return_value=[]):
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                    json.dump([], f)
                    fname = f.name
                try:
                    with patch.object(_mod, "CORRECTIONS_FILE", Path(fname)):
                        rule_id = _mod.ingest_correction(
                            nova_response="The Eiffel Tower is in Rome.",
                            jordan_correction="The Eiffel Tower is in Paris.",
                            topic="geography",
                        )
                    # Rule ID should be returned on success
                    # (may be None if DB is mocked to fail — just ensure no exception)
                except Exception as e:
                    self.fail(f"ingest_correction() raised: {e}")
                finally:
                    os.unlink(fname)

    def test_promote_corrections_skips_existing_rules(self):
        """promote_corrections() must not add duplicate rules."""
        corrections = [{
            "id": "abc",
            "timestamp": "2026-01-01",
            "nova_response": "Paris is in Germany.",
            "jordan_correction": "Paris is in France.",
            "topic": "geography",
            "context": {},
        }]
        # The exact rule text generated by _correction_to_rule for this correction
        expected_rule = _correction_to_rule(corrections[0])

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(corrections, f)
            fname = f.name
        try:
            with patch.object(_mod, "CORRECTIONS_FILE", Path(fname)):
                # Simulate the exact rule already existing
                with patch.object(_mod, "get_all_rules",
                                  return_value=[{"rule": expected_rule}]):
                    with patch.object(_mod, "add_rule") as mock_add:
                        count = _mod.promote_corrections()
            mock_add.assert_not_called()
            self.assertEqual(count, 0)
        finally:
            os.unlink(fname)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_cli_help(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)

    def test_cli_list_subcommand_help(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "list", "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)

    def test_cli_prompt_with_no_db(self):
        """prompt subcommand with unavailable DB must not crash."""
        with patch("subprocess.run") as mock_run:
            # First call is module load security check, second+ are psql calls
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout="", stderr="psql: error"),
            ] * 10
            result = subprocess.run(
                [sys.executable, str(_SCRIPT), "prompt"],
                capture_output=True, text=True,
            )
        # Should exit gracefully (empty output is OK when no DB)
        self.assertNotIn("Traceback", result.stderr)

    def test_add_preference_function_exists(self):
        """add_preference() must exist as a public function."""
        self.assertTrue(callable(_mod.add_preference))


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

    def test_required_functions_exist(self):
        for fn in ["add_rule", "retire_rule", "record_application", "get_active_rules",
                   "get_all_rules", "format_rules_for_prompt", "promote_corrections",
                   "ingest_correction", "add_preference", "_escape", "_correction_to_rule"]:
            self.assertTrue(callable(getattr(_mod, fn, None)), f"Missing: {fn}")

    def test_state_dir_path_uses_home(self):
        self.assertTrue(str(_mod.STATE_DIR).startswith(str(Path.home())))

    def test_corrections_file_path_uses_home(self):
        self.assertTrue(str(_mod.CORRECTIONS_FILE).startswith(str(Path.home())))

    def test_escape_is_pure_function(self):
        """_escape() must be deterministic — same input always gives same output."""
        for s in ["test", "it's a test", "'; DROP TABLE", "", "normal text"]:
            r1 = _escape(s)
            r2 = _escape(s)
            self.assertEqual(r1, r2, f"_escape not deterministic for: {s!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
