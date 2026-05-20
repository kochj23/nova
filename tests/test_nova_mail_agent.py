"""
test_nova_mail_agent.py — All 7 test categories for nova_mail_agent.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub heavy dependencies before loading module
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.SLACK_EMAIL = "#nova-email"
sys.modules["nova_config"] = _nova_cfg
sys.modules["herd_config"] = MagicMock(HERD=[], HERD_EMAILS=set())
sys.modules["known_senders"] = MagicMock(
    KNOWN_SENDERS=set(), JORDAN_EMAILS=set(), JORDAN_CC_ADDR=""
)

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_mail_agent.py"
_spec = importlib.util.spec_from_file_location("nova_mail_agent", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

is_system_message = _mod.is_system_message
is_from_nova = _mod.is_from_nova
is_from_jordan = _mod.is_from_jordan
is_from_herd = _mod.is_from_herd
is_addressed_to_nova = _mod.is_addressed_to_nova
is_known_sender = _mod.is_known_sender
imap_fetch_message = _mod.imap_fetch_message
generate_haiku = _mod.generate_haiku
vector_remember = _mod.vector_remember
_read_file = _mod._read_file
_load_sender_profile = _mod._load_sender_profile


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        """Source must not contain API keys or plaintext passwords."""
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]
        for pattern in forbidden:
            self.assertNotIn(pattern, src, f"Potential credential in source: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode a literal home path."""
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src, "Hardcoded home path found — use Path.home()")

    def test_no_pii_email_literals(self):
        """Source must not contain personal email addresses as literals."""
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII in source: {p!r}")

    def test_password_loaded_from_keychain(self):
        """_get_app_password must call macOS security CLI (Keychain)."""
        src = _SCRIPT.read_text()
        self.assertIn("security", src, "Should use macOS security CLI for Keychain")
        self.assertIn("find-generic-password", src)

    def test_body_truncated_before_llm(self):
        """imap_fetch_message must truncate body to 3000 chars."""
        conn = MagicMock()
        long_body = "A" * 5000
        raw_email = (
            b"From: test@example.com\r\n"
            b"Subject: Test\r\n"
            b"Content-Type: text/plain\r\n\r\n"
        ) + long_body.encode()
        conn.uid.return_value = ("OK", [(b"1 (RFC822 {100})", raw_email)])
        result = imap_fetch_message(conn, b"1")
        self.assertLessEqual(len(result.get("body", "")), 3000,
                             "Body should be truncated to 3000 chars")

    def test_nova_email_is_constant_not_pii(self):
        """NOVA_EMAIL constant must be nova@digitalnoise.net (not Jordan's personal)."""
        self.assertEqual(_mod.NOVA_EMAIL, "nova@digitalnoise.net")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_is_system_message_fast(self):
        """is_system_message must complete in < 5ms."""
        start = time.perf_counter()
        for _ in range(500):
            is_system_message("mailer-daemon@example.com", "Mail Delivery Notification")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"is_system_message 500x took {elapsed:.3f}s")

    def test_classification_funcs_fast(self):
        """Classification functions must all be < 10ms for 1000 calls."""
        addr = "test@example.com"
        start = time.perf_counter()
        for _ in range(1000):
            is_from_nova(addr)
            is_from_jordan(addr)
            is_known_sender(addr)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2, f"Classification 3000x took {elapsed:.3f}s")

    def test_vector_remember_no_blocking_on_timeout(self):
        """vector_remember must not block the main thread on network failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            start = time.perf_counter()
            vector_remember("test text")
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, "vector_remember blocked too long on failure")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_haiku_returns_fallback_on_error(self):
        """generate_haiku must return fallback string when Ollama is unavailable."""
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = generate_haiku("test topic")
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0, "Haiku fallback should be non-empty")

    def test_smtp_send_returns_false_on_error(self):
        """smtp_send must return (False, msg_bytes) on SMTP failure."""
        import smtplib
        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.side_effect = smtplib.SMTPException("error")
            ok, msg_bytes = _mod.smtp_send(
                "pw", ["to@example.com"], [], "Subject", "Body"
            )
        self.assertFalse(ok)
        self.assertIsInstance(msg_bytes, bytes)

    def test_vector_remember_swallows_exception(self):
        """vector_remember must not raise even when server is down."""
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            try:
                vector_remember("test data")
            except Exception as e:
                self.fail(f"vector_remember raised unexpectedly: {e}")

    def test_get_app_password_returns_empty_on_failure(self):
        """_get_app_password must return '' when Keychain lookup fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = _mod._get_app_password()
        self.assertEqual(result, "")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_is_system_message_mailer_daemon(self):
        self.assertTrue(is_system_message("mailer-daemon@example.com", "subject"))

    def test_is_system_message_noreply(self):
        self.assertTrue(is_system_message("noreply@service.com", "newsletter"))

    def test_is_system_message_false_for_human(self):
        self.assertFalse(is_system_message("alice@example.com", "Hello there"))

    def test_is_from_nova_true(self):
        self.assertTrue(is_from_nova("nova@digitalnoise.net"))

    def test_is_from_nova_false(self):
        self.assertFalse(is_from_nova("alice@example.com"))

    def test_is_from_nova_case_insensitive(self):
        self.assertTrue(is_from_nova("Nova@digitalnoise.net"))

    def test_is_addressed_to_nova_true(self):
        self.assertTrue(is_addressed_to_nova("nova@digitalnoise.net, other@example.com"))

    def test_is_addressed_to_nova_false(self):
        self.assertFalse(is_addressed_to_nova("other@example.com"))

    def test_is_from_jordan_uses_jordan_emails_set(self):
        """is_from_jordan must check against JORDAN_EMAILS set."""
        # With empty JORDAN_EMAILS (our stub), any addr should return False
        self.assertFalse(is_from_jordan("anyone@example.com"))

    def test_read_file_returns_empty_on_missing(self):
        """_read_file must return '' when file doesn't exist."""
        result = _read_file(Path("/nonexistent/path/file.md"), 500)
        self.assertEqual(result, "")

    def test_read_file_truncates(self):
        """_read_file must honour max_chars limit."""
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("x" * 1000)
            fname = f.name
        try:
            result = _read_file(Path(fname), 100)
            self.assertLessEqual(len(result), 100)
        finally:
            os.unlink(fname)

    def test_imap_move_to_trash_catches_exception(self):
        """imap_move_to_trash must not raise when IMAP call fails."""
        conn = MagicMock()
        conn.uid.side_effect = Exception("connection dropped")
        try:
            _mod.imap_move_to_trash(conn, b"99")
        except Exception as e:
            self.fail(f"imap_move_to_trash raised: {e}")

    def test_imap_save_to_sent_catches_exception(self):
        conn = MagicMock()
        conn.append.side_effect = Exception("server error")
        try:
            _mod.imap_save_to_sent(conn, b"raw bytes")
        except Exception as e:
            self.fail(f"imap_save_to_sent raised: {e}")

    def test_system_sender_patterns_populated(self):
        self.assertGreater(len(_mod.SYSTEM_SENDER_PATTERNS), 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_exits_early_no_password(self):
        """main() must return immediately when no app password is found."""
        called = []
        with patch.object(_mod, "_get_app_password", return_value=""):
            with patch.object(_mod, "imap_connect", side_effect=lambda p: called.append(p)):
                _mod.main()
        self.assertEqual(len(called), 0, "imap_connect should not be called without password")

    def test_main_exits_on_imap_connect_failure(self):
        """main() must log and return when IMAP connect fails."""
        with patch.object(_mod, "_get_app_password", return_value="testpw"):
            with patch.object(_mod, "imap_connect", side_effect=Exception("refused")):
                try:
                    _mod.main()
                except Exception as e:
                    self.fail(f"main() raised unexpectedly: {e}")

    def test_main_no_unread_returns_cleanly(self):
        """main() must return cleanly when there are no unread messages."""
        conn = MagicMock()
        conn.uid.return_value = ("OK", [b""])
        with patch.object(_mod, "_get_app_password", return_value="pw"):
            with patch.object(_mod, "imap_connect", return_value=conn):
                with patch.object(_mod, "imap_list_unread", return_value=[]):
                    _mod.main()
        conn.logout.assert_called()

    def test_system_message_gets_trashed_not_replied(self):
        """System messages must be trashed without generating a reply."""
        conn = MagicMock()
        reply_calls = []

        msg_data = {
            "uid": b"1",
            "from_addr": "mailer-daemon@example.com",
            "from_raw": "Mail Delivery <mailer-daemon@example.com>",
            "from_name": "Mail Delivery",
            "to_raw": "nova@digitalnoise.net",
            "cc_raw": "",
            "subject": "Undeliverable: test",
            "body": "delivery failed",
            "message_id": "<id>",
            "references": "",
            "in_reply_to": "",
        }

        with patch.object(_mod, "_get_app_password", return_value="pw"):
            with patch.object(_mod, "imap_connect", return_value=conn):
                with patch.object(_mod, "imap_list_unread", return_value=[b"1"]):
                    with patch.object(_mod, "imap_fetch_message", return_value=msg_data):
                        with patch.object(_mod, "smtp_send",
                                          side_effect=lambda *a, **kw: reply_calls.append(1)):
                            with patch.object(_mod, "imap_move_to_trash"):
                                _mod.main()

        self.assertEqual(len(reply_calls), 0, "smtp_send should not be called for system messages")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_generate_reply_returns_string(self):
        """generate_reply must return a string (may be empty on LLM failure)."""
        with patch("urllib.request.urlopen", side_effect=OSError("no server")):
            result = _mod.generate_reply("Alice", "Hello", "How are you?", "alice@example.com")
        self.assertIsInstance(result, str)

    def test_generate_haiku_fallback_is_three_lines(self):
        """Haiku fallback must be 3-line format."""
        with patch("urllib.request.urlopen", side_effect=OSError("no server")):
            result = generate_haiku()
        lines = [l for l in result.strip().split("\n") if l.strip()]
        self.assertEqual(len(lines), 3, f"Fallback haiku should have 3 lines, got: {result!r}")

    def test_generate_haiku_strips_think_tags(self):
        """generate_haiku must strip </think> reasoning artifacts."""
        fake_response = json.dumps({
            "response": "<think>reasoning here</think>\nLeaves fall softly down\nWind carries them far away\nSilence fills the space"
        }).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_response

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = generate_haiku("autumn")

        self.assertNotIn("</think>", result)
        self.assertNotIn("<think>", result)

    def test_smtp_send_constructs_message_correctly(self):
        """smtp_send must put From/To/Cc headers in the message."""
        import smtplib
        captured = []

        class FakeSMTP:
            def __init__(self, host, port): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def ehlo(self): pass
            def starttls(self): pass
            def login(self, u, p): pass
            def sendmail(self, frm, to, data):
                captured.append(data)

        with patch("smtplib.SMTP", FakeSMTP):
            ok, msg_bytes = _mod.smtp_send(
                "testpw",
                to_addrs=["to@example.com"],
                cc_addrs=["cc@example.com"],
                subject="Test Subject",
                body="Test body text here."
            )

        self.assertTrue(ok)
        self.assertIn(b"nova@digitalnoise.net", msg_bytes)
        self.assertIn(b"Test Subject", msg_bytes)

    def test_imap_list_unread_returns_empty_on_bad_status(self):
        conn = MagicMock()
        conn.select.return_value = ("OK", [])
        conn.uid.return_value = ("NO", [None])
        result = _mod.imap_list_unread(conn)
        self.assertEqual(result, [])


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_mail_agent.py has syntax errors: {e}")

    def test_module_constants_present(self):
        self.assertEqual(_mod.IMAP_HOST, "imap.gmail.com")
        self.assertEqual(_mod.IMAP_PORT, 993)
        self.assertEqual(_mod.SMTP_HOST, "smtp.gmail.com")
        self.assertEqual(_mod.SMTP_PORT, 587)
        self.assertIsInstance(_mod.NOVA_EMAIL, str)
        self.assertIn("@", _mod.NOVA_EMAIL)

    def test_system_sender_patterns_list(self):
        self.assertIsInstance(_mod.SYSTEM_SENDER_PATTERNS, list)
        self.assertIn("noreply", _mod.SYSTEM_SENDER_PATTERNS)

    def test_all_functions_callable(self):
        funcs = [
            _mod._get_app_password, _mod.imap_connect, _mod.imap_list_unread,
            _mod.imap_fetch_message, _mod.imap_move_to_trash, _mod.imap_save_to_sent,
            _mod.smtp_send, _mod.generate_haiku, _mod.generate_reply, _mod.vector_remember,
            _mod.is_system_message, _mod.is_from_nova, _mod.is_from_jordan,
            _mod.is_from_herd, _mod.is_known_sender, _mod.main,
        ]
        for fn in funcs:
            self.assertTrue(callable(fn), f"{fn.__name__} is not callable")

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK),
                        f"{_SCRIPT} is not executable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
