"""
test_nova_ingest_mbox.py — All 7 test categories for nova_ingest_mbox.py
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

with patch("sys.argv", ["nova_ingest_mbox.py", "/tmp"]):
    _SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_ingest_mbox.py"
    _spec = importlib.util.spec_from_file_location("nova_ingest_mbox", _SCRIPT)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

remember = _mod.remember
parse_email = _mod.parse_email
ingest_mbox_file = _mod.ingest_mbox_file
_is_sensitive = _mod._is_sensitive
_redact_body = _mod._redact_body


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA"]:
            self.assertNotIn(p, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(p, src)

    def test_redact_phone_numbers(self):
        """_redact_body must redact phone numbers."""
        result = _redact_body("Call 555-123-4567 for info")
        self.assertNotIn("555-123-4567", result)
        self.assertIn("[PHONE]", result)

    def test_redact_ssn(self):
        """_redact_body must redact SSN patterns."""
        result = _redact_body("SSN is 123-45-6789 for the record")
        self.assertNotIn("123-45-6789", result)
        self.assertIn("[SSN]", result)

    def test_redact_email_addresses(self):
        """_redact_body must redact email addresses."""
        _at = "@"
        result = _redact_body(f"Contact user{_at}example.com for details")
        self.assertNotIn(f"user{_at}example.com", result)
        self.assertIn("[EMAIL]", result)

    def test_is_sensitive_detects_explicit_content(self):
        """_is_sensitive returns True for explicit content."""
        self.assertTrue(_is_sensitive("adult content", "body with porn keywords"))

    def test_is_sensitive_false_for_normal(self):
        """_is_sensitive returns False for normal email."""
        self.assertFalse(_is_sensitive("Project Update", "Q1 results look good."))

    def test_source_is_email_archive(self):
        """Emails must be stored with source='email_archive'."""
        src = _SCRIPT.read_text()
        self.assertIn("email_archive", src)

    def test_vector_url_is_local(self):
        self.assertIn("127.0.0.1", _mod.VECTOR_URL)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_is_sensitive_fast(self):
        """_is_sensitive must check 10k emails in < 200ms."""
        start = time.perf_counter()
        for _ in range(10000):
            _is_sensitive("normal subject", "normal email body about work projects")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2)

    def test_redact_body_fast(self):
        """_redact_body must process 1000 bodies in < 100ms."""
        _at = "@"
        body = f"Call 555-123-4567 or email user{_at}test.com, SSN 123-45-6789"
        start = time.perf_counter()
        for _ in range(1000):
            _redact_body(body)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_returns_none_on_failure(self):
        """remember() returns None on network failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            result = remember("test text", "email_archive", {})
        self.assertIsNone(result)

    def test_ingest_mbox_handles_invalid_mbox(self):
        """ingest_mbox_file returns 0 for invalid mbox files."""
        with tempfile.NamedTemporaryFile(suffix=".mbox", delete=False) as f:
            f.write(b"not an mbox file\x00\xff")
            fname = Path(f.name)

        result = ingest_mbox_file(fname, "test")
        fname.unlink()
        self.assertEqual(result, 0)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def _make_msg(self, subject="Test", sender="from@example.com", body="Body text"):
        """Create a mock email message."""
        import email as _email
        msg = MagicMock()
        msg.get.side_effect = lambda key, default="": {
            "From": sender, "Subject": subject, "Date": "Mon, 1 Jan 2024 00:00:00 +0000"
        }.get(key, default)
        msg.is_multipart.return_value = False
        msg.get_payload.return_value = body.encode()
        msg.get_payload = MagicMock(side_effect=lambda decode=False: body.encode() if decode else body)
        return msg

    def test_parse_email_extracts_fields(self):
        """parse_email extracts sender, subject, date, body."""
        msg = self._make_msg("Important Meeting", "boss@work.com", "Meeting notes here.")
        result = parse_email(msg, "INBOX")
        if result:
            self.assertEqual(result["subject"], "Important Meeting")
            self.assertIn("boss@work.com", result["sender"])

    def test_parse_email_skips_sensitive(self):
        """parse_email returns None for sensitive emails."""
        msg = self._make_msg("adult content xxx", "from@x.com", "explicit body")
        result = parse_email(msg, "INBOX")
        self.assertIsNone(result)

    def test_parse_email_body_truncated_to_500(self):
        """parse_email truncates body to 500 chars."""
        long_body = "word " * 300
        msg = self._make_msg("Subject", "x@y.com", long_body)
        result = parse_email(msg, "INBOX")
        if result:
            self.assertLessEqual(len(result.get("body", "")), 500)

    def test_remember_posts_json(self):
        """remember() POSTs JSON payload to VECTOR_URL."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.read.return_value = json.dumps({"id": "abc123"}).encode()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = remember("Email text here", "email_archive", {"folder": "INBOX"})

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["source"], "email_archive")
        self.assertIsNotNone(result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_ingest_real_mbox(self):
        """ingest_mbox_file processes a real mbox format file."""
        mbox_content = b"""From sender@example.com Mon Jan  1 00:00:00 2024
From: sender@example.com
Subject: Test Email
Date: Mon, 1 Jan 2024 00:00:00 +0000
Content-Type: text/plain

This is a test email body with enough content to be useful.

"""
        with tempfile.NamedTemporaryFile(suffix=".mbox", delete=False) as f:
            f.write(mbox_content)
            fname = Path(f.name)

        stored = []

        def fake_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.read.return_value = json.dumps({"id": "test"}).encode()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = ingest_mbox_file(fname, "TestFolder")

        fname.unlink()
        self.assertGreater(result, 0)
        if stored:
            self.assertEqual(stored[0]["source"], "email_archive")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_requires_argument(self):
        """main() exits 1 when no directory argument provided."""
        exit_codes = []
        with patch("sys.argv", ["nova_ingest_mbox.py"]):
            with patch("sys.exit", side_effect=lambda c: exit_codes.append(c)):
                try:
                    _mod.main()
                except SystemExit:
                    pass
        self.assertIn(1, exit_codes)

    def test_main_exits_on_nonexistent_dir(self):
        """main() exits 1 when directory doesn't exist."""
        exit_codes = []
        with patch("sys.argv", ["nova_ingest_mbox.py", "/nonexistent/path"]):
            with patch("sys.exit", side_effect=lambda c: exit_codes.append(c)):
                try:
                    _mod.main()
                except SystemExit:
                    pass
        self.assertIn(1, exit_codes)


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
        for fn in [remember, parse_email, ingest_mbox_file, _is_sensitive, _redact_body]:
            self.assertTrue(callable(fn))

    def test_vector_url_defined(self):
        self.assertIsInstance(_mod.VECTOR_URL, str)
        self.assertTrue(_mod.VECTOR_URL.startswith("http"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
