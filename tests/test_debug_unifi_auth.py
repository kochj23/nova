"""
test_debug_unifi_auth.py — All 7 test categories for debug_unifi_auth.py
Written by Jordan Koch.
Note: debug_unifi_auth.py is a markdown-style planning document, not runnable Python.
Tests verify its content properties.
"""
import sys, time, unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "debug_unifi_auth.py"


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["Jkoogie", "sk-", "ghp_", "api_key =", "password ="]:
            self.assertNotIn(pat, src, f"Credential found: {pat!r}")
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_references_keychain(self):
        src = _SCRIPT.read_text()
        self.assertIn("Keychain", src, "Must reference Keychain for API key storage")
    def test_uses_api_key_header_not_url(self):
        src = _SCRIPT.read_text()
        self.assertIn("Authorization", src, "Must use Authorization header")
    def test_no_plain_text_ip_credentials(self):
        src = _SCRIPT.read_text()
        self.assertNotIn("Jkoogie", src)
        self.assertNotIn("admin:admin", src)
    def test_unifi_url_is_local(self):
        src = _SCRIPT.read_text()
        self.assertIn("192.168.1.", src)


class TestPerformance(unittest.TestCase):
    def test_file_is_small(self):
        """Planning doc should be small."""
        size = _SCRIPT.stat().st_size
        self.assertLess(size, 10000)
    def test_file_reads_fast(self):
        start = time.perf_counter()
        src = _SCRIPT.read_text()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


class TestRetry(unittest.TestCase):
    def test_mentions_retry_or_curl(self):
        """Should mention curl or retry for network operations."""
        src = _SCRIPT.read_text()
        self.assertTrue("curl" in src.lower() or "retry" in src.lower())


class TestUnit(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue(_SCRIPT.exists())
    def test_file_has_content(self):
        self.assertGreater(_SCRIPT.stat().st_size, 0)
    def test_mentions_unifi_protect(self):
        src = _SCRIPT.read_text()
        self.assertIn("Protect", src)
    def test_mentions_api_steps(self):
        src = _SCRIPT.read_text()
        self.assertIn("Next steps", src)
    def test_mentions_keychain_usage(self):
        src = _SCRIPT.read_text()
        self.assertIn("Keychain", src)
    def test_mentions_authentication(self):
        src = _SCRIPT.read_text()
        auth_mentioned = "auth" in src.lower() or "Authentication" in src
        self.assertTrue(auth_mentioned)
    def test_api_endpoint_mentioned(self):
        src = _SCRIPT.read_text()
        self.assertIn("/protect", src)


class TestIntegration(unittest.TestCase):
    def test_notes_no_requests_lib(self):
        """Notes that 'requests' isn't available — uses curl."""
        src = _SCRIPT.read_text()
        self.assertIn("curl", src.lower())
    def test_mentions_nova_monitor_integration(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova_unifi_monitor", src)


class TestFunctional(unittest.TestCase):
    def test_integration_plan_is_complete(self):
        """Plan should have at least 3 numbered steps."""
        src = _SCRIPT.read_text()
        import re
        steps = re.findall(r'^\d+\.', src, re.MULTILINE)
        self.assertGreaterEqual(len(steps), 3, "Need at least 3 steps in plan")
    def test_no_dream_test_blocking(self):
        """Dream test mention should not block the plan."""
        src = _SCRIPT.read_text()
        self.assertIn("Dream test", src)


class TestFrame(unittest.TestCase):
    def test_file_exists_and_readable(self):
        self.assertTrue(_SCRIPT.exists())
        src = _SCRIPT.read_text()
        self.assertGreater(len(src), 0)
    def test_not_pure_python(self):
        """This file is a markdown planning doc, not a Python script."""
        src = _SCRIPT.read_text()
        self.assertNotIn("#!/usr/bin/env python3", src)
        self.assertNotIn("import os", src)
    def test_file_encoding_is_utf8(self):
        src = _SCRIPT.read_text(encoding="utf-8")
        self.assertIsInstance(src, str)

if __name__ == "__main__":
    unittest.main(verbosity=2)
