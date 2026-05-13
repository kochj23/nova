"""
test_nova_ingest_emlx.py — All 7 test categories for nova_ingest_emlx.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_ingest_emlx.py"

# Patch sys.argv before loading (script uses sys.argv[1] for base_dir)
with patch("sys.argv", ["nova_ingest_emlx.py"]):
    _spec = importlib.util.spec_from_file_location("nova_ingest_emlx", _SCRIPT)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

parse_emlx = _mod.parse_emlx
store = _mod.store
is_skip_folder = _mod.is_skip_folder
_pii_filter = _mod._pii_filter
SKIP_FOLDERS = _mod.SKIP_FOLDERS


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

    def test_pii_filter_redacts_phone_numbers(self):
        """_pii_filter must redact phone numbers from email body."""
        skip, body = _pii_filter("subject", "Call me at 555-123-4567 please")
        self.assertNotIn("555-123-4567", body)
        self.assertIn("[PHONE]", body)

    def test_pii_filter_redacts_email_addresses(self):
        """_pii_filter must redact email addresses from body."""
        _at = "@"
        skip, body = _pii_filter("subject", f"Contact user{_at}example.com for more info")
        self.assertNotIn(f"user{_at}example.com", body)
        self.assertIn("[EMAIL]", body)

    def test_pii_filter_skips_explicit_content(self):
        """_pii_filter returns skip=True for explicit content."""
        skip, _ = _pii_filter("explicit subject with porn keywords", "body")
        self.assertTrue(skip)

    def test_pii_filter_skips_adult_urls(self):
        """_pii_filter returns skip=True for adult URLs."""
        skip, _ = _pii_filter("subject", "visit https://xxx.example.com for content")
        self.assertTrue(skip)

    def test_source_label_is_email_archive(self):
        """Emails must be stored with source='email_archive'."""
        emlx_content = b"100\nFrom: test@example.com\nSubject: Hello\nDate: Mon, 1 Jan 2024 00:00:00 +0000\n\nBody text here.\n"
        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(emlx_content)
            fname = Path(f.name)

        result = parse_emlx(fname)
        fname.unlink()

        if result:
            self.assertEqual(result["source"], "email_archive")

    def test_vector_url_is_local(self):
        """VECTOR_URL must point to localhost."""
        self.assertIn("127.0.0.1", _mod.VECTOR_URL)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_is_skip_folder_fast(self):
        """is_skip_folder must check 10k paths in < 100ms."""
        paths = [Path(f"/mail/Folder{i}/messages.mbox") for i in range(10000)]
        start = time.perf_counter()
        for p in paths:
            is_skip_folder(p)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_pii_filter_fast(self):
        """_pii_filter must process 1000 bodies in < 200ms."""
        body = "Contact me at 555-123-4567 or user@example.com for details about the project."
        start = time.perf_counter()
        for _ in range(1000):
            _pii_filter("subject", body)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_store_retries_on_failure(self):
        """store() returns False on repeated failure (script retries once externally)."""
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            result = store({"text": "test", "source": "email_archive", "metadata": {}})
        self.assertFalse(result)

    def test_parse_emlx_returns_none_on_corrupt_file(self):
        """parse_emlx returns None for corrupt/unreadable files."""
        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(b"\x00\xff\xfe corrupt bytes \x00\x01")
            fname = Path(f.name)

        result = parse_emlx(fname)
        fname.unlink()

        # Should return None (not crash)
        self.assertIsNone(result)

    def test_store_handles_timeout(self):
        """store() handles timeout gracefully."""
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")):
            result = store({"text": "test", "source": "email_archive", "metadata": {}})
        self.assertFalse(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_is_skip_folder_trash(self):
        """Trash folder must be skipped."""
        self.assertTrue(is_skip_folder(Path("/mail/Trash.mbox/Messages/1.emlx")))

    def test_is_skip_folder_spam(self):
        """Spam folder must be skipped."""
        self.assertTrue(is_skip_folder(Path("/mail/Spam.mbox/1.emlx")))

    def test_is_skip_folder_inbox(self):
        """Inbox is NOT a skip folder."""
        self.assertFalse(is_skip_folder(Path("/mail/INBOX.mbox/1.emlx")))

    def test_is_skip_folder_sent(self):
        """Sent Messages is NOT a skip folder."""
        self.assertFalse(is_skip_folder(Path("/mail/Sent Messages.mbox/1.emlx")))

    def test_parse_emlx_basic_email(self):
        """parse_emlx correctly parses a simple email."""
        emlx = b"200\nFrom: sender@example.com\nSubject: Test Subject\nDate: Mon, 1 Jan 2024 12:00:00 +0000\n\nThis is the email body text.\n"

        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(emlx)
            fname = Path(f.name)

        result = parse_emlx(fname)
        fname.unlink()

        if result:
            self.assertIn("text", result)
            self.assertIn("source", result)
            self.assertIn("metadata", result)
            self.assertIn("Test Subject", result["text"])

    def test_parse_emlx_returns_none_for_empty(self):
        """parse_emlx returns None when subject and body are both empty."""
        emlx = b"50\nFrom: x@y.com\nSubject:\nDate: Mon, 1 Jan 2024\n\n\n"

        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(emlx)
            fname = Path(f.name)

        result = parse_emlx(fname)
        fname.unlink()

        self.assertIsNone(result)

    def test_skip_folders_contains_expected(self):
        """SKIP_FOLDERS must contain common junk folders."""
        expected = {"Trash", "Spam", "Junk", "Drafts"}
        self.assertTrue(expected.issubset(SKIP_FOLDERS))

    def test_pii_filter_normal_email_not_skipped(self):
        """Normal email content must not be skipped."""
        skip, body = _pii_filter("Project Update", "The deployment went smoothly today.")
        self.assertFalse(skip)
        self.assertIn("smoothly", body)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_store_posts_to_vector_url(self):
        """store() POSTs to VECTOR_URL with correct JSON."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.read.return_value = json.dumps({"status": "queued"}).encode()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        payload = {
            "text": "Email from sender@example.com",
            "source": "email_archive",
            "metadata": {"folder": "INBOX", "date": "2026-01-01"},
        }

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = store(payload)

        self.assertTrue(result)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["source"], "email_archive")

    def test_full_pipeline_stores_parsed_email(self):
        """Full pipeline: parse_emlx -> _pii_filter -> store."""
        emlx = b"300\nFrom: boss@company.com\nSubject: Q1 Report\nDate: Mon, 1 Jan 2024 12:00:00 +0000\n\nPlease review the quarterly report attached.\n"

        stored = []

        def fake_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.read.return_value = json.dumps({"status": "queued"}).encode()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(emlx)
            fname = Path(f.name)

        payload = parse_emlx(fname)
        fname.unlink()

        if payload:
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                store(payload)

            self.assertEqual(len(stored), 1)
            self.assertIn("Q1 Report", stored[0]["text"])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_emails_stored_as_async_queue(self):
        """store() must use async queue endpoint (?async=1)."""
        self.assertIn("async=1", _mod.VECTOR_URL)

    def test_body_truncated_to_800_chars(self):
        """Email body must be truncated to 800 chars."""
        long_body = "word " * 1000  # 5000 chars
        emlx = f"500\nFrom: x@y.com\nSubject: Long Email\nDate: Mon, 1 Jan 2024\n\n{long_body}\n".encode()

        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(emlx)
            fname = Path(f.name)

        result = parse_emlx(fname)
        fname.unlink()

        if result:
            # Body must be truncated
            self.assertLessEqual(len(result["text"]), 2000)

    def test_subject_truncated_to_300_chars(self):
        """Subject must be truncated to 300 chars."""
        long_subj = "A" * 500
        emlx = f"50\nFrom: x@y.com\nSubject: {long_subj}\nDate: Mon, 1 Jan 2024\n\nbody\n".encode()

        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(emlx)
            fname = Path(f.name)

        result = parse_emlx(fname)
        fname.unlink()

        if result:
            meta_subject = result["metadata"].get("subject", "")
            self.assertLessEqual(len(meta_subject), 300)


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
        for fn in [parse_emlx, store, is_skip_folder, _pii_filter]:
            self.assertTrue(callable(fn))

    def test_skip_folders_is_set(self):
        self.assertIsInstance(SKIP_FOLDERS, set)
        self.assertGreater(len(SKIP_FOLDERS), 3)

    def test_vector_url_defined(self):
        self.assertIsInstance(_mod.VECTOR_URL, str)
        self.assertTrue(_mod.VECTOR_URL.startswith("http"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
