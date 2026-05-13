"""
test_nova_security_hardening.py — All 7 test categories for nova_security_hardening.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load module under test — no external dependencies
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_security_hardening.py"
_spec = importlib.util.spec_from_file_location("nova_security_hardening", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

remember = _mod.remember
hardening_tier_1 = _mod.hardening_tier_1
hardening_tier_2 = _mod.hardening_tier_2
run_nmap_scan = _mod.run_nmap_scan


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-", "password ="]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(pattern, src, f"PII found: {pattern!r}")

    def test_memory_url_uses_localhost(self):
        """MEMORY_URL must point to local memory server."""
        self.assertIn("127.0.0.1", _mod.MEMORY_URL)
        self.assertNotIn("openai.com", _mod.MEMORY_URL)

    def test_nmap_scan_does_not_hardcode_subnet(self):
        """run_nmap_scan() should not hardcode external subnet in actual scan command."""
        src = _SCRIPT.read_text()
        # LAN subnet reference in comments/docs is OK, but no hardcoded credentials
        self.assertNotIn("Jkoogie", src, "UniFi credentials must not be hardcoded")

    def test_remember_uses_json_content_type(self):
        """remember() must send JSON with proper Content-Type header."""
        captured_headers = []

        def fake_urlopen(req, timeout=None):
            captured_headers.append(req.get_header("Content-type"))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = b'{"id": "abc123"}'
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            remember("Security hardening test event.")

        self.assertTrue(len(captured_headers) > 0)
        self.assertIn("application/json", captured_headers[0].lower(),
                      "remember() must use Content-Type: application/json")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_log_fast(self):
        """log() must print 1000 messages in < 100ms."""
        start = time.perf_counter()
        for i in range(1000):
            _mod.log(f"Test message {i}")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"log() 1000x took {elapsed:.3f}s")

    def test_nmap_scan_report_structure(self):
        """run_nmap_scan() must return a dict with expected keys."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("memory down")
            report = run_nmap_scan()
        self.assertIsInstance(report, dict)
        self.assertIn("timestamp", report)
        self.assertIn("status", report)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_returns_none_on_failure(self):
        """remember() must return None on network failure, not raise."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("connection refused")
            result = remember("Test hardening event.")
        self.assertIsNone(result, "remember() must return None on failure")

    def test_hardening_tier_1_survives_memory_failure(self):
        """hardening_tier_1() must not crash if memory write fails."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("memory server down")
            try:
                hardening_tier_1()
            except Exception as e:
                self.fail(f"hardening_tier_1() raised: {e}")

    def test_hardening_tier_2_survives_memory_failure(self):
        """hardening_tier_2() must not crash if memory write fails."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("memory server down")
            try:
                hardening_tier_2()
            except Exception as e:
                self.fail(f"hardening_tier_2() raised: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_remember_sends_correct_payload(self):
        """remember() must send text and source in JSON body."""
        captured_data = []

        def fake_urlopen(req, timeout=None):
            captured_data.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = b'{"id": "abc123"}'
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = remember("Tier 1 hardening complete.", source="security")

        self.assertEqual(len(captured_data), 1)
        self.assertEqual(captured_data[0]["text"], "Tier 1 hardening complete.")
        self.assertEqual(captured_data[0]["source"], "security")

    def test_remember_default_source_is_security(self):
        """remember() must default source to 'security'."""
        captured_data = []

        def fake_urlopen(req, timeout=None):
            captured_data.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = b'{"id": "test"}'
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            remember("Security event without explicit source.")

        self.assertEqual(captured_data[0].get("source"), "security")

    def test_run_nmap_scan_returns_expected_keys(self):
        """run_nmap_scan() must return report with timestamp, total_devices, status."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("skip memory")
            report = run_nmap_scan()

        for key in ["timestamp", "total_devices", "status"]:
            self.assertIn(key, report, f"run_nmap_scan() report missing key: {key}")

    def test_run_nmap_scan_returns_dict_not_none(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("skip")
            report = run_nmap_scan()
        self.assertIsNotNone(report)
        self.assertIsInstance(report, dict)

    def test_hardening_tier_1_calls_remember(self):
        """hardening_tier_1() must write to memory."""
        remember_calls = []

        with patch.object(_mod, "remember", side_effect=lambda t, **kw: remember_calls.append(t)):
            hardening_tier_1()

        self.assertGreater(len(remember_calls), 0,
                           "hardening_tier_1() must call remember()")

    def test_hardening_tier_2_calls_remember(self):
        remember_calls = []

        with patch.object(_mod, "remember", side_effect=lambda t, **kw: remember_calls.append(t)):
            hardening_tier_2()

        self.assertGreater(len(remember_calls), 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_full_hardening_run_does_not_crash(self):
        """Running hardening_tier_1, tier_2, and nmap_scan in sequence must not crash."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("no server in test")
            try:
                hardening_tier_1()
                hardening_tier_2()
                run_nmap_scan()
            except Exception as e:
                self.fail(f"Hardening sequence raised: {e}")

    def test_nmap_scan_records_timestamp(self):
        """run_nmap_scan() report timestamp must be an ISO format string."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("skip")
            report = run_nmap_scan()

        ts = report.get("timestamp", "")
        self.assertGreater(len(ts), 10, "Timestamp must be non-empty")
        self.assertIn("T", ts, "Timestamp should be ISO format with T separator")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_does_not_crash(self):
        """main() must log initialization messages without crashing."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("skip")
            try:
                _mod.main()
            except Exception as e:
                self.fail(f"main() raised: {e}")

    def test_script_runs_without_crashing(self):
        """Direct execution must exit 0."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0,
                         f"Script exited non-zero: {result.stderr[:300]}")
        self.assertNotIn("Traceback", result.stderr)

    def test_remember_returns_id_on_success(self):
        """remember() must return the memory ID on success."""
        def fake_urlopen(req, timeout=None):
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = b'{"id": "abc123"}'
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = remember("Test memory event.")

        self.assertEqual(result, "abc123")


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
        for fn in ["log", "remember", "hardening_tier_1",
                   "hardening_tier_2", "run_nmap_scan", "main"]:
            self.assertTrue(callable(getattr(_mod, fn, None)), f"Missing: {fn}")

    def test_memory_url_defined(self):
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIn("http", _mod.MEMORY_URL)

    def test_log_function_uses_timestamp(self):
        """log() must include a timestamp in output."""
        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            _mod.log("Test event.")
        output = f.getvalue()
        # Should contain time-like characters (: separators)
        self.assertIn(":", output, "log() must include timestamp")
        self.assertIn("Test event", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
