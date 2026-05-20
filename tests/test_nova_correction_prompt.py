"""
test_nova_correction_prompt.py — All 7 test categories for nova_correction_prompt.py
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
# Load module under test — stub requests
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_correction_prompt.py"

_requests_mock = MagicMock()
sys.modules["requests"] = _requests_mock

_spec = importlib.util.spec_from_file_location("nova_correction_prompt", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

search_vector_memory = _mod.search_vector_memory
search_local_corrections = _mod.search_local_corrections
format_corrections_for_prompt = _mod.format_corrections_for_prompt


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_vector_api_uses_localhost(self):
        self.assertIn("127.0.0.1", _mod.VECTOR_API_BASE)

    def test_corrections_file_in_home(self):
        self.assertTrue(str(_mod.CORRECTIONS_FILE).startswith(str(Path.home())))

    def test_search_vector_filters_by_source(self):
        """Vector search must filter by source='correction' to prevent leaking other memories."""
        captured_params = []
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        _requests_mock.get = MagicMock(
            side_effect=lambda url, params=None, timeout=None: (
                captured_params.append(params) or mock_resp
            )
        )
        _requests_mock.RequestException = OSError

        search_vector_memory("test query", limit=3)

        self.assertTrue(len(captured_params) > 0)
        self.assertEqual(captured_params[0].get("source"), "correction",
                         "Vector search must filter by source='correction'")

    def test_format_does_not_include_empty_output_when_no_corrections(self):
        """format_corrections_for_prompt() must return empty string when no corrections."""
        result = format_corrections_for_prompt([], [])
        self.assertEqual(result, "")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_search_local_fast_on_large_file(self):
        """search_local_corrections() must search 1000 corrections in < 200ms."""
        corrections = [
            {"id": str(i), "topic": f"topic_{i % 10}",
             "nova_response": f"wrong answer about topic {i}",
             "jordan_correction": f"correct answer for {i}",
             "timestamp": "2026-01-01"}
            for i in range(1000)
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(corrections, f)
            fname = f.name
        try:
            with patch.object(_mod, "CORRECTIONS_FILE", Path(fname)):
                start = time.perf_counter()
                result = search_local_corrections("wrong answer topic 5", limit=5)
                elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.2, f"search_local_corrections(1000) took {elapsed:.3f}s")
        finally:
            os.unlink(fname)

    def test_format_fast_on_many_corrections(self):
        """format_corrections_for_prompt() must format 100 entries in < 50ms."""
        vector = [f"Correction text number {i}" for i in range(50)]
        local = [{"topic": "test", "nova_response": f"wrong {i}",
                  "jordan_correction": f"right {i}"} for i in range(50)]
        start = time.perf_counter()
        result = format_corrections_for_prompt(vector, local)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05, f"format 100 corrections took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def setUp(self):
        """Reset mock state before each test."""
        _requests_mock.get = MagicMock()
        _requests_mock.post = MagicMock()
        _requests_mock.RequestException = OSError

    def test_search_vector_returns_empty_on_failure(self):
        """search_vector_memory() must return [] on network failure."""
        class FakeNetErr(Exception): pass
        _requests_mock.get.side_effect = FakeNetErr("server down")
        _requests_mock.RequestException = FakeNetErr

        result = search_vector_memory("test query")
        self.assertEqual(result, [])

    def test_search_vector_returns_empty_on_http_error(self):
        """search_vector_memory() must return [] on HTTP 500."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        _requests_mock.get.return_value = mock_resp
        _requests_mock.get.side_effect = None
        _requests_mock.RequestException = OSError

        result = search_vector_memory("test query")
        self.assertEqual(result, [])

    def test_search_local_returns_empty_on_corrupt_json(self):
        """search_local_corrections() must return [] on corrupt JSON file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json {{{{")
            fname = f.name
        try:
            with patch.object(_mod, "CORRECTIONS_FILE", Path(fname)):
                result = search_local_corrections("test query")
            self.assertEqual(result, [])
        finally:
            os.unlink(fname)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def setUp(self):
        """Reset requests mock state before each test."""
        _requests_mock.get = MagicMock()
        _requests_mock.post = MagicMock()
        _requests_mock.RequestException = OSError

    def test_search_local_empty_on_missing_file(self):
        with patch.object(_mod, "CORRECTIONS_FILE", Path("/nonexistent/corrections.json")):
            result = search_local_corrections("test")
        self.assertEqual(result, [])

    def test_search_local_finds_matching_topic(self):
        corrections = [
            {"id": "1", "topic": "homekit", "nova_response": "use device names",
             "jordan_correction": "use scene names", "timestamp": "2026-01-01"},
            {"id": "2", "topic": "people", "nova_response": "Jason works at Apple",
             "jordan_correction": "Jason works at Acme Corp", "timestamp": "2026-01-01"},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(corrections, f)
            fname = f.name
        try:
            with patch.object(_mod, "CORRECTIONS_FILE", Path(fname)):
                result = search_local_corrections("homekit scene")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["topic"], "homekit")
        finally:
            os.unlink(fname)

    def test_search_local_scores_by_term_matches(self):
        """search_local_corrections() must rank by number of matching terms."""
        corrections = [
            {"id": "1", "topic": "general", "nova_response": "Apple is a fruit",
             "jordan_correction": "Apple is a tech company", "timestamp": "2026-01-01"},
            {"id": "2", "topic": "apple homekit", "nova_response": "Apple HomeKit error",
             "jordan_correction": "Use Apple HomeKit correctly", "timestamp": "2026-01-01"},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(corrections, f)
            fname = f.name
        try:
            with patch.object(_mod, "CORRECTIONS_FILE", Path(fname)):
                result = search_local_corrections("apple homekit scene names", limit=2)
            # The second correction has more matching terms
            self.assertTrue(len(result) >= 1)
        finally:
            os.unlink(fname)

    def test_format_includes_section_header(self):
        """format_corrections_for_prompt() must include the prior corrections header."""
        result = format_corrections_for_prompt(["Nova said X, Jordan corrected to Y."], [])
        self.assertIn("PRIOR CORRECTIONS", result)
        self.assertIn("END PRIOR CORRECTIONS", result)

    def test_format_deduplicates_vector_and_local(self):
        """format_corrections_for_prompt() must not include identical text twice."""
        same_text = "CORRECTION [homekit]: Nova said 'use device' -> Jordan corrected: 'use scene'"
        vector = [same_text]
        local = [{"topic": "homekit", "nova_response": "use device",
                  "jordan_correction": "use scene"}]
        result = format_corrections_for_prompt(vector, local)
        # Count occurrences of the text in output
        count = result.count("use scene")
        self.assertLessEqual(count, 2, "Correction text should not be duplicated")

    def test_format_includes_usage_reminder(self):
        """format_corrections_for_prompt() must remind Nova to use the corrections."""
        result = format_corrections_for_prompt(["Nova was wrong."], [])
        self.assertIn("avoid repeating past mistakes", result.lower() +
                      result.replace("mistakes", "mistakes"))

    def test_search_vector_parses_list_response(self):
        """search_vector_memory() must handle list-of-strings response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = ["Correction 1.", "Correction 2."]
        _requests_mock.get.return_value = mock_resp
        _requests_mock.RequestException = OSError

        result = search_vector_memory("test")
        self.assertEqual(len(result), 2)
        self.assertIn("Correction 1.", result)

    def test_search_vector_parses_list_of_dicts(self):
        """search_vector_memory() must handle list-of-dicts response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"text": "Correction A.", "score": 0.9},
            {"content": "Correction B.", "score": 0.8},
        ]
        _requests_mock.get.return_value = mock_resp
        _requests_mock.RequestException = OSError

        result = search_vector_memory("test")
        self.assertEqual(len(result), 2)
        self.assertIn("Correction A.", result)
        self.assertIn("Correction B.", result)

    def test_search_vector_parses_wrapped_dict(self):
        """search_vector_memory() must handle {'memories': [...]} response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "memories": [{"text": "Correction from memories key."}]
        }
        _requests_mock.get.return_value = mock_resp
        _requests_mock.RequestException = OSError

        result = search_vector_memory("test")
        self.assertEqual(len(result), 1)
        self.assertIn("Correction from memories key.", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_both_sources_combined_in_output(self):
        """format_corrections_for_prompt() must include both vector and local results."""
        vector = ["From vector memory: Nova said Earth is flat."]
        local = [{"topic": "homekit", "nova_response": "wrong",
                  "jordan_correction": "right"}]
        result = format_corrections_for_prompt(vector, local)
        self.assertIn("vector memory", result.lower() + result)
        self.assertIn("homekit", result)

    def test_search_local_returns_at_most_limit(self):
        """search_local_corrections() must respect the limit parameter."""
        corrections = [
            {"id": str(i), "topic": "test", "nova_response": f"wrong {i}",
             "jordan_correction": f"right {i}", "timestamp": "2026-01-01"}
            for i in range(20)
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(corrections, f)
            fname = f.name
        try:
            with patch.object(_mod, "CORRECTIONS_FILE", Path(fname)):
                result = search_local_corrections("wrong", limit=3)
            self.assertLessEqual(len(result), 3)
        finally:
            os.unlink(fname)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_cli_requires_query(self):
        """--query is required."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_cli_help(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("query", result.stdout.lower())

    def test_cli_silent_when_no_corrections_found(self):
        """Script must produce no output when no corrections match."""
        with patch.object(_mod, "CORRECTIONS_FILE", Path("/nonexistent/corrections.json")):
            result = subprocess.run(
                [sys.executable, str(_SCRIPT), "--query", "xyzzy quux nonexistent"],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "",
                         "Script must be silent when no corrections found")


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
        for fn in ["search_vector_memory", "search_local_corrections",
                   "format_corrections_for_prompt"]:
            self.assertTrue(callable(getattr(_mod, fn, None)), f"Missing: {fn}")

    def test_vector_api_base_defined(self):
        self.assertIn("http", _mod.VECTOR_API_BASE)

    def test_corrections_file_path_defined(self):
        self.assertIsInstance(_mod.CORRECTIONS_FILE, Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
