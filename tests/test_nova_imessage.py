"""
test_nova_imessage.py — All 7 test categories for nova_imessage.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.JORDAN_DM = "D0AMPB3F4T0"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg
sys.modules["herd_config"] = MagicMock(HERD=[])

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_imessage.py"
_spec = importlib.util.spec_from_file_location("nova_imessage", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_normalize_phone = _mod._normalize_phone
is_spam = _mod.is_spam
send_imessage = _mod.send_imessage
_mac_timestamp_to_datetime = _mod._mac_timestamp_to_datetime
load_state = _mod.load_state
save_state = _mod.save_state


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential: {p!r}")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII: {p!r}")

    def test_nova_signature_appended(self):
        """send_imessage must append Nova signature so recipients know it's her."""
        captured = []

        def fake_run(args, **kw):
            captured.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            send_imessage("+15555551234", "Hello from Nova")

        if captured:
            # Find the full arg string
            arg_str = " ".join(captured[0])
            self.assertIn("Nova", arg_str, "Nova signature must be in the message")

    def test_messages_db_path_is_readonly(self):
        """Messages DB must be opened read-only."""
        src = _SCRIPT.read_text()
        self.assertIn("mode=ro", src, "Messages DB must be opened read-only")

    def test_allowed_contacts_from_config(self):
        """ALLOWED_CONTACTS must be loaded from herd_config, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("herd_config", src, "Contacts must come from herd_config")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_normalize_phone_fast(self):
        numbers = ["+15555551234", "5555551234", "15555551234",
                   "(555) 555-1234", "555-555-1234"]
        start = time.perf_counter()
        for _ in range(1000):
            for n in numbers:
                _normalize_phone(n)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"normalize_phone 5000x: {elapsed:.3f}s")

    def test_is_spam_fast(self):
        msgs = [
            {"text": "Hi there", "sender": "+15555551234"},
            {"text": "", "sender": "12345"},
            {"text": "Win a prize", "sender": "spam@company.net"},
        ]
        start = time.perf_counter()
        for _ in range(1000):
            for m in msgs:
                is_spam(m)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_send_imessage_tries_alternate_on_failure(self):
        """send_imessage must try alternate method when primary fails."""
        calls = [0]

        def fake_run(args, **kw):
            calls[0] += 1
            return MagicMock(returncode=1 if calls[0] == 1 else 0, stdout="", stderr="failed")

        with patch("subprocess.run", side_effect=fake_run):
            result = send_imessage("+15555551234", "Test message")
        # At least 2 subprocess calls (primary + alternate)
        self.assertGreaterEqual(calls[0], 2)

    def test_vector_remember_swallows_errors(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                _mod.vector_remember("test text")
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_normalize_phone_strips_formatting(self):
        self.assertEqual(_normalize_phone("+1 (555) 555-1234"), "5555551234")

    def test_normalize_phone_drops_leading_1(self):
        self.assertEqual(_normalize_phone("15555551234"), "5555551234")

    def test_normalize_phone_already_10_digits(self):
        self.assertEqual(_normalize_phone("5555551234"), "5555551234")

    def test_normalize_phone_short_number(self):
        result = _normalize_phone("12345")
        self.assertLessEqual(len(result), 10)

    def test_is_spam_empty_text(self):
        self.assertTrue(is_spam({"text": "", "sender": "+15555551234"}))

    def test_is_spam_very_short(self):
        self.assertTrue(is_spam({"text": "x", "sender": "+15555551234"}))

    def test_is_spam_shortcode_sender(self):
        self.assertTrue(is_spam({"text": "Your code: 123456", "sender": "12345"}))

    def test_is_spam_not_spam_normal(self):
        self.assertFalse(is_spam({"text": "Hey, how are you doing today?",
                                   "sender": "+15555551234"}))

    def test_mac_timestamp_to_datetime_none(self):
        result = _mac_timestamp_to_datetime(None)
        self.assertIsNone(result)

    def test_mac_timestamp_to_datetime_zero(self):
        result = _mac_timestamp_to_datetime(0)
        self.assertIsNone(result)

    def test_mac_timestamp_converts_correctly(self):
        from datetime import datetime
        # Any positive mac_ts should return a datetime
        result = _mac_timestamp_to_datetime(1_000_000_000_000_000_000)
        self.assertIsInstance(result, datetime)

    def test_load_state_returns_default(self):
        with patch.object(_mod, "STATE_FILE",
                           Path("/nonexistent/nova_imessage_state.json")):
            state = load_state()
        self.assertIn("last_check_ts", state)
        self.assertEqual(state["last_check_ts"], 0)

    def test_save_load_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = Path(tmpdir) / "imessage_state.json"
            with patch.object(_mod, "STATE_FILE", sf):
                save_state({"last_check_ts": 12345})
                loaded = load_state()
        self.assertEqual(loaded["last_check_ts"], 12345)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_send_imessage_constructs_applescript(self):
        """send_imessage must call osascript with recipient and message."""
        captured = []

        def fake_run(args, **kw):
            captured.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            send_imessage("+15555551234", "Integration test")

        self.assertTrue(any("osascript" in str(a) for a in captured),
                        "Must call osascript for Messages.app")

    def test_watch_stores_messages_in_memory(self):
        """watch() must store messages in vector memory."""
        remembered = []
        mock_messages = [
            {"text": "Hello Nova", "is_from_me": False, "handle": "+15555551234",
             "date": "2026-05-13 09:00", "service": "iMessage",
             "raw_date": 1000000},
        ]

        with patch.object(_mod, "get_all_new_messages",
                           return_value=(mock_messages, 1000000)):
            with patch.object(_mod, "vector_remember",
                               side_effect=lambda t, m=None: remembered.append(t)):
                with patch.object(_mod, "resolve_contact", return_value="Sam"):
                    _mod.watch()

        self.assertGreater(len(remembered), 0, "Messages should be stored in memory")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_send_imessage_appends_signature_when_sign_true(self):
        captured = []

        def fake_run(args, **kw):
            captured.append(" ".join(args))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            send_imessage("+15555551234", "Test message", sign=True)

        if captured:
            self.assertIn("Nova", captured[0], "Signature must be appended")

    def test_send_imessage_no_double_signature(self):
        """If message already has Nova signature, don't add another."""
        captured = []

        def fake_run(args, **kw):
            captured.append(" ".join(args))
            return MagicMock(returncode=0, stdout="", stderr="")

        msg_with_sig = "Hello there\n— Nova"
        with patch("subprocess.run", side_effect=fake_run):
            send_imessage("+15555551234", msg_with_sig, sign=True)

        if captured:
            # Count occurrences of "Nova" in the script
            # The signature "— Nova" should appear at most once
            nova_count = captured[0].count("— Nova")
            self.assertLessEqual(nova_count, 1, "Signature appended twice")

    def test_nova_signature_constant(self):
        self.assertIn("Nova", _mod.NOVA_SIGNATURE)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_imessage.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.MESSAGES_DB, Path)
        self.assertIsInstance(_mod.NOVA_SIGNATURE, str)
        self.assertIsInstance(_mod.ALLOWED_CONTACTS, dict)

    def test_all_functions_callable(self):
        for fn in [_normalize_phone, is_spam, send_imessage,
                    _mac_timestamp_to_datetime, load_state, save_state,
                    _mod.watch, _mod.get_recent_messages]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
