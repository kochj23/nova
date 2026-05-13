"""
test_ingest_demonology.py — All 7 test categories for ingest_demonology.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = (Path(__file__).parent.parent / "scripts" / "ingest_demonology.py"
           if (Path(__file__).parent.parent / "scripts" / "ingest_demonology.py").exists()
           else Path(__file__).parent.parent / "scripts" / "_archive" / "ingest_demonology.py")
_spec = importlib.util.spec_from_file_location("ingest_demonology", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_vector_url_is_localhost(self):
        self.assertIn("127.0.0.1", _mod.VECTOR_URL)

    def test_payload_json_encoded(self):
        src = _SCRIPT.read_text()
        self.assertIn("json.dumps", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_batch_delay_defined(self):
        self.assertIsNotNone(_mod.BATCH_DELAY)
        self.assertLessEqual(_mod.BATCH_DELAY, 1.0,
                             "BATCH_DELAY should be ≤ 1s to not be too slow")

    def test_ingest_fast_in_dry_run(self):
        """Dry-run mode must complete quickly (no network)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                         delete=False) as f:
            for i in range(10):
                f.write(json.dumps({"text": f"Fact {i}", "source": "demonology"}) + "\n")
            path = Path(f.name)

        try:
            start = time.perf_counter()
            with patch.object(_mod, "DATA_FILE", path):
                _mod.ingest(dry_run=True)
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 2.0,
                            "Dry-run ingest of 10 facts must complete in < 2s")
        finally:
            path.unlink(missing_ok=True)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_ingest_continues_on_http_failure(self):
        """ingest() must continue to next fact on HTTP failure."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                         delete=False) as f:
            for i in range(3):
                f.write(json.dumps({"text": f"Fact {i}", "source": "demonology"}) + "\n")
            path = Path(f.name)

        call_count = [0]

        def sometimes_fail(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("connection refused")
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.status = 200
            return r

        try:
            with patch.object(_mod, "DATA_FILE", path):
                with patch("urllib.request.urlopen", side_effect=sometimes_fail):
                    with patch("time.sleep"):
                        _mod.ingest(dry_run=False)
            # Stats endpoint is also called, so count may be > 3
            self.assertGreaterEqual(call_count[0], 3,
                                    "Must attempt all 3 facts despite failure")
        finally:
            path.unlink(missing_ok=True)

    def test_failed_count_tracked(self):
        """ingest() must track failed requests."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                         delete=False) as f:
            f.write(json.dumps({"text": "Fact 1", "source": "demonology"}) + "\n")
            path = Path(f.name)

        def always_fail(req, timeout=None):
            raise OSError("server down")

        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        try:
            with patch.object(_mod, "DATA_FILE", path):
                with patch("urllib.request.urlopen", side_effect=always_fail):
                    with patch("time.sleep"):
                        with redirect_stdout(output):
                            _mod.ingest(dry_run=False)
            # Check output mentions "failed"
            self.assertIn("1 failed", output.getvalue())
        finally:
            path.unlink(missing_ok=True)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.VECTOR_URL)
        self.assertIsNotNone(_mod.DATA_FILE)
        self.assertIsNotNone(_mod.BATCH_DELAY)

    def test_vector_url_has_remember_endpoint(self):
        self.assertIn("/remember", _mod.VECTOR_URL)

    def test_data_file_path_structure(self):
        """DATA_FILE must point to the data directory."""
        self.assertIn("data", str(_mod.DATA_FILE))
        self.assertIn("demonology_facts", str(_mod.DATA_FILE))

    def test_dry_run_skips_network(self):
        """dry_run=True must never call urlopen."""
        calls = []

        def should_not_call(*args, **kwargs):
            calls.append(args)
            raise AssertionError("urlopen called during dry run!")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                         delete=False) as f:
            f.write(json.dumps({"text": "Test demonology fact", "source": "demo"}) + "\n")
            path = Path(f.name)

        try:
            with patch.object(_mod, "DATA_FILE", path):
                with patch("urllib.request.urlopen", side_effect=should_not_call):
                    _mod.ingest(dry_run=True)
            self.assertEqual(calls, [],
                             "dry_run=True must not call urlopen")
        finally:
            path.unlink(missing_ok=True)

    def test_invalid_json_line_skipped(self):
        """ingest() must skip lines with invalid JSON."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                         delete=False) as f:
            f.write("not valid json\n")
            f.write(json.dumps({"text": "Valid fact", "source": "demo"}) + "\n")
            path = Path(f.name)

        fact_calls = []
        def mock_urlopen(req, timeout=None):
            # Only count calls to /remember (not /stats)
            if "/remember" in getattr(req, "full_url", ""):
                fact_calls.append(req)
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.status = 200
            r.read.return_value = json.dumps({"count": 1}).encode()
            return r

        try:
            with patch.object(_mod, "DATA_FILE", path):
                with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                    with patch("time.sleep"):
                        _mod.ingest(dry_run=False)
            # Only 1 valid fact should have been posted to /remember
            self.assertEqual(len(fact_calls), 1,
                             "Invalid JSON line must be skipped")
        finally:
            path.unlink(missing_ok=True)

    def test_empty_text_line_skipped(self):
        """ingest() must skip facts with empty text."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                         delete=False) as f:
            f.write(json.dumps({"text": "", "source": "demo"}) + "\n")
            path = Path(f.name)

        remember_calls = []

        def mock_urlopen(req, timeout=None):
            if "/remember" in getattr(req, "full_url", ""):
                remember_calls.append(req)
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.status = 200
            r.read.return_value = json.dumps({"count": 0}).encode()
            return r

        try:
            with patch.object(_mod, "DATA_FILE", path):
                with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                    _mod.ingest(dry_run=False)
            self.assertEqual(remember_calls, [],
                             "Empty text fact must be skipped (no /remember calls)")
        finally:
            path.unlink(missing_ok=True)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_ingest_posts_correct_payload(self):
        """ingest() must post fact text, source, and metadata."""
        posted = []

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                         delete=False) as f:
            f.write(json.dumps({
                "text": "Beelzebub is a demon associated with gluttony.",
                "source": "demonology",
                "metadata": {"type": "demon_fact"}
            }) + "\n")
            path = Path(f.name)

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.status = 200
            return r

        try:
            with patch.object(_mod, "DATA_FILE", path):
                with patch("urllib.request.urlopen", side_effect=capture):
                    with patch("time.sleep"):
                        _mod.ingest(dry_run=False)
            self.assertEqual(len(posted), 1)
            self.assertEqual(posted[0]["source"], "demonology")
            self.assertIn("Beelzebub", posted[0]["text"])
        finally:
            path.unlink(missing_ok=True)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_ingest_exits_when_file_missing(self):
        """ingest() must exit when DATA_FILE doesn't exist."""
        missing = Path("/tmp/nonexistent_demonology_file_xyz.jsonl")
        with patch.object(_mod, "DATA_FILE", missing):
            with self.assertRaises(SystemExit) as cm:
                _mod.ingest(dry_run=False)
            self.assertEqual(cm.exception.code, 1)

    def test_stats_checked_after_ingest(self):
        """After real ingest, stats endpoint must be queried."""
        src = _SCRIPT.read_text()
        self.assertIn("/stats", src,
                      "Stats endpoint must be checked after ingest")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_demonology.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_ingest_function_exists(self):
        self.assertTrue(callable(_mod.ingest))

    def test_main_guard_present(self):
        src = _SCRIPT.read_text()
        self.assertIn('if __name__ == "__main__"', src)

    def test_dry_run_flag_supported(self):
        src = _SCRIPT.read_text()
        self.assertIn("--dry-run", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
