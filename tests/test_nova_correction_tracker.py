"""
test_nova_correction_tracker.py — All 7 test categories for nova_correction_tracker.py
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
# Load module under test — stubs requests if needed
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_correction_tracker.py"

# Stub requests (optional dep)
_requests_mock = MagicMock()
sys.modules["requests"] = _requests_mock

_spec = importlib.util.spec_from_file_location("nova_correction_tracker", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_corrections = _mod.load_corrections
save_corrections = _mod.save_corrections
store_to_vector_memory = _mod.store_to_vector_memory
log_correction = _mod.log_correction
list_corrections = _mod.list_corrections
show_stats = _mod.show_stats
export_corrections = _mod.export_corrections


def _with_temp_corrections_file(fn):
    """Context helper — run fn with a temp directory for corrections."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.object(_mod, "CORRECTIONS_DIR", Path(tmpdir)):
            with patch.object(_mod, "CORRECTIONS_FILE", Path(tmpdir) / "corrections.json"):
                fn(Path(tmpdir))


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_corrections_stored_locally_when_vector_fails(self):
        """Corrections must be saved locally even when vector memory is unreachable."""

        class FakeRequestException(Exception):
            pass

        def run_fn(tmpdir):
            # Use a real exception subclass so except clause works
            _requests_mock.post.side_effect = FakeRequestException("server down")
            _requests_mock.RequestException = FakeRequestException

            correction, vector_ok = log_correction(
                nova_response="Paris is the capital of Germany.",
                jordan_correction="Paris is the capital of France.",
                topic="geography",
            )
            self.assertIn("id", correction)
            # Verify file was written
            corrections_file = Path(tmpdir) / "corrections.json"
            self.assertTrue(corrections_file.exists(), "Corrections file must be created")
            data = json.loads(corrections_file.read_text())
            self.assertEqual(len(data), 1)
            self.assertFalse(vector_ok, "vector_ok must be False when server is down")

        _with_temp_corrections_file(run_fn)

    def test_correction_records_include_timestamp(self):
        """Every correction must have a timestamp for audit trail."""
        def run_fn(tmpdir):
            correction, _ = log_correction("wrong", "right", "test")
            self.assertIn("timestamp", correction)
            self.assertGreater(len(correction["timestamp"]), 10)

        _with_temp_corrections_file(run_fn)

    def test_correction_includes_uuid(self):
        """Every correction must have a unique UUID."""
        def run_fn(tmpdir):
            c1, _ = log_correction("wrong1", "right1")
            c2, _ = log_correction("wrong2", "right2")
            self.assertNotEqual(c1["id"], c2["id"])

        _with_temp_corrections_file(run_fn)

    def test_vector_memory_marked_local_only(self):
        """Vector memory payload must mark correction as local-only privacy."""
        captured_payloads = []

        def fake_post(url, json=None, timeout=None):
            captured_payloads.append(json)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            return mock_resp

        _requests_mock.post = fake_post
        _requests_mock.RequestException = OSError

        def run_fn(tmpdir):
            correction = {
                "id": "test-id",
                "timestamp": "2026-01-01T00:00:00",
                "nova_response": "wrong",
                "jordan_correction": "right",
                "topic": "test",
                "context": {},
            }
            store_to_vector_memory(correction)

        _with_temp_corrections_file(run_fn)

        if captured_payloads:
            metadata = captured_payloads[0].get("metadata", {})
            self.assertIn("privacy", metadata)
            self.assertEqual(metadata["privacy"], "local-only")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_load_corrections_fast(self):
        """load_corrections() must load 1000 corrections in < 200ms."""
        corrections = [
            {
                "id": f"id-{i}",
                "timestamp": "2026-01-01T00:00:00",
                "nova_response": f"wrong answer {i}",
                "jordan_correction": f"right answer {i}",
                "topic": "test",
                "context": {},
            }
            for i in range(1000)
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(corrections, f)
            fname = f.name

        try:
            with patch.object(_mod, "CORRECTIONS_FILE", Path(fname)):
                start = time.perf_counter()
                result = load_corrections()
                elapsed = time.perf_counter() - start

            self.assertEqual(len(result), 1000)
            self.assertLess(elapsed, 0.2, f"load_corrections(1000) took {elapsed:.3f}s")
        finally:
            os.unlink(fname)

    def test_show_stats_fast_on_large_dataset(self):
        """show_stats() must handle 500 corrections without slowdown."""
        corrections = [
            {"id": str(i), "timestamp": "2026-01-01T00:00:00",
             "nova_response": "x", "jordan_correction": "y",
             "topic": f"topic_{i % 10}", "context": {}}
            for i in range(500)
        ]
        with patch.object(_mod, "load_corrections", return_value=corrections):
            import io
            from contextlib import redirect_stdout
            f = io.StringIO()
            start = time.perf_counter()
            with redirect_stdout(f):
                show_stats()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"show_stats(500) took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def setUp(self):
        """Reset requests mock state before each test."""
        _requests_mock.get = MagicMock()
        _requests_mock.post = MagicMock()
        _requests_mock.RequestException = OSError

    def test_store_to_vector_handles_request_exception(self):
        """store_to_vector_memory() must return False on network failure, not raise."""
        class FakeReqException(Exception):
            pass

        _requests_mock.post = MagicMock(side_effect=FakeReqException("connection refused"))
        _requests_mock.RequestException = FakeReqException

        correction = {
            "id": "test-id",
            "timestamp": "2026-01-01T00:00:00",
            "nova_response": "wrong",
            "jordan_correction": "right",
            "topic": "test",
            "context": {},
        }
        result = store_to_vector_memory(correction)
        self.assertFalse(result, "store_to_vector_memory() must return False on failure")

    def test_store_to_vector_handles_http_error(self):
        """store_to_vector_memory() must return False on HTTP 500."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        _requests_mock.post = MagicMock(return_value=mock_resp)
        _requests_mock.RequestException = OSError

        correction = {
            "id": "test-id",
            "timestamp": "2026-01-01T00:00:00",
            "nova_response": "wrong",
            "jordan_correction": "right",
            "topic": "test",
            "context": {},
        }
        result = store_to_vector_memory(correction)
        self.assertFalse(result, "store_to_vector_memory() must return False on HTTP error")

    def test_log_correction_always_saves_locally(self):
        """log_correction() must always persist locally regardless of vector memory result."""
        class FakeReqException(Exception):
            pass

        _requests_mock.post.side_effect = FakeReqException("network error")
        _requests_mock.RequestException = FakeReqException

        def run_fn(tmpdir):
            corrections_file = Path(tmpdir) / "corrections.json"
            log_correction("wrong answer here", "right answer here", "test-topic")
            self.assertTrue(corrections_file.exists())
            data = json.loads(corrections_file.read_text())
            self.assertEqual(len(data), 1)

        _with_temp_corrections_file(run_fn)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def setUp(self):
        """Reset requests mock state before each test."""
        _requests_mock.get = MagicMock()
        _requests_mock.post = MagicMock()
        _requests_mock.RequestException = OSError

    def test_load_corrections_returns_empty_when_file_missing(self):
        with patch.object(_mod, "CORRECTIONS_FILE", Path("/nonexistent/path/corrections.json")):
            result = load_corrections()
        self.assertEqual(result, [])

    def test_load_corrections_returns_empty_on_corrupt_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            fname = f.name
        try:
            with patch.object(_mod, "CORRECTIONS_FILE", Path(fname)):
                result = load_corrections()
            self.assertEqual(result, [])
        finally:
            os.unlink(fname)

    def test_save_and_load_roundtrip(self):
        def run_fn(tmpdir):
            corrections = [
                {"id": "abc", "timestamp": "2026-01-01", "nova_response": "wrong",
                 "jordan_correction": "right", "topic": "test", "context": {}}
            ]
            save_corrections(corrections)
            loaded = load_corrections()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["id"], "abc")

        _with_temp_corrections_file(run_fn)

    def test_log_correction_default_topic(self):
        """log_correction() without topic must default to 'general'."""
        def run_fn(tmpdir):
            correction, _ = log_correction("Nova was wrong", "Jordan's correction")
            self.assertEqual(correction["topic"], "general")

        _requests_mock.post.side_effect = OSError("skip")
        _requests_mock.RequestException = OSError
        _with_temp_corrections_file(run_fn)

    def test_log_correction_custom_topic(self):
        def run_fn(tmpdir):
            correction, _ = log_correction("wrong", "right", topic="homekit")
            self.assertEqual(correction["topic"], "homekit")

        _requests_mock.post.side_effect = OSError("skip")
        _requests_mock.RequestException = OSError
        _with_temp_corrections_file(run_fn)

    def test_list_corrections_empty_message(self):
        """list_corrections() with no data must print a message, not crash."""
        import io
        from contextlib import redirect_stdout
        with patch.object(_mod, "load_corrections", return_value=[]):
            f = io.StringIO()
            with redirect_stdout(f):
                list_corrections()
        self.assertIn("No corrections", f.getvalue())

    def test_show_stats_empty_message(self):
        import io
        from contextlib import redirect_stdout
        with patch.object(_mod, "load_corrections", return_value=[]):
            f = io.StringIO()
            with redirect_stdout(f):
                show_stats()
        self.assertIn("No corrections", f.getvalue())

    def test_export_corrections_outputs_json(self):
        import io
        from contextlib import redirect_stdout
        corrections = [{"id": "abc", "topic": "test"}]
        with patch.object(_mod, "load_corrections", return_value=corrections):
            f = io.StringIO()
            with redirect_stdout(f):
                export_corrections()
        data = json.loads(f.getvalue())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "abc")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def setUp(self):
        """Reset requests mock state before each test."""
        _requests_mock.get = MagicMock()
        _requests_mock.post = MagicMock()
        _requests_mock.RequestException = OSError

    def test_multiple_corrections_accumulate(self):
        """Multiple log_correction() calls must accumulate in the same file."""
        _requests_mock.post.side_effect = OSError("skip")
        _requests_mock.RequestException = OSError

        def run_fn(tmpdir):
            for i in range(3):
                log_correction(f"wrong {i}", f"right {i}", f"topic-{i}")
            loaded = load_corrections()
            self.assertEqual(len(loaded), 3)

        _with_temp_corrections_file(run_fn)

    def test_show_stats_counts_by_topic(self):
        """show_stats() must count corrections by topic."""
        import io
        from contextlib import redirect_stdout
        corrections = [
            {"id": "1", "topic": "homekit", "timestamp": "2026-01-01",
             "nova_response": "x", "jordan_correction": "y", "context": {}},
            {"id": "2", "topic": "homekit", "timestamp": "2026-01-01",
             "nova_response": "x", "jordan_correction": "y", "context": {}},
            {"id": "3", "topic": "people", "timestamp": "2026-01-01",
             "nova_response": "x", "jordan_correction": "y", "context": {}},
        ]
        with patch.object(_mod, "load_corrections", return_value=corrections):
            f = io.StringIO()
            with redirect_stdout(f):
                show_stats()
        output = f.getvalue()
        self.assertIn("homekit", output)
        self.assertIn("people", output)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_help_exits_cleanly(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("correction", result.stdout.lower())

    def test_missing_log_or_correction_exits_1(self):
        """--log without --correction must exit with error."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--log", "Nova was wrong"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("required", result.stderr.lower() + result.stdout.lower())

    def test_no_args_prints_help(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)


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
        for fn in ["load_corrections", "save_corrections", "store_to_vector_memory",
                   "log_correction", "list_corrections", "show_stats", "export_corrections"]:
            self.assertTrue(callable(getattr(_mod, fn, None)), f"Missing: {fn}")

    def test_corrections_file_path_uses_home(self):
        """CORRECTIONS_FILE must be under the user's home directory."""
        self.assertTrue(str(_mod.CORRECTIONS_FILE).startswith(str(Path.home())),
                        "CORRECTIONS_FILE must be in home directory")

    def test_vector_api_base_uses_localhost(self):
        self.assertIn("127.0.0.1", _mod.VECTOR_API_BASE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
