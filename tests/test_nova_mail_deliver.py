"""
test_nova_mail_deliver.py — All 7 test categories for nova_mail_deliver.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub dependencies before loading module
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg
sys.modules["herd_config"] = MagicMock(HERD=[{"name": "Sam"}, {"name": "Gaston"}])

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_mail_deliver.py"
_spec = importlib.util.spec_from_file_location("nova_mail_deliver", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

is_noise = _mod.is_noise
is_important = _mod.is_important
parse_accounts_from_file = _mod.parse_accounts_from_file
build_summary = _mod.build_summary
vector_remember = _mod.vector_remember
send_email = _mod.send_email


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "xoxb-"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Potential credential: {p!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src, "Hardcoded home path found")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
            "kochj" + _at + "digitalnoise.net",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII in source: {p!r}")

    def test_send_email_is_disabled(self):
        """send_email must be a no-op (disabled to prevent duplicate scanning)."""
        called_smtp = []
        with patch("smtplib.SMTP", side_effect=lambda *a: called_smtp.append(1)):
            send_email("Test Subject", "Test body")
        self.assertEqual(len(called_smtp), 0, "SMTP should not be called (function disabled)")

    def test_vector_remember_swallows_errors(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            try:
                vector_remember("test text")
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_is_noise_fast(self):
        start = time.perf_counter()
        for _ in range(2000):
            is_noise("amazon.com", "Your order shipped")
            is_noise("alice@example.com", "Meeting tomorrow")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"is_noise 4000x took {elapsed:.3f}s")

    def test_parse_accounts_large_file(self):
        # Simulate 100 accounts with 10 messages each
        lines = []
        for i in range(100):
            _at = "@"
            lines.append(f"📬 user{i}" + _at + f"example.com — 10 message(s), 3 unread")
            for j in range(10):
                lines.append(f"[UNREAD] FROM: sender{j}" + _at + "example.com")
                lines.append(f"SUBJ: Subject {j}")
        content = "\n".join(lines)
        start = time.perf_counter()
        result = parse_accounts_from_file(content)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"parse_accounts 1000 messages took {elapsed:.3f}s")

    def test_build_summary_no_unbounded_output(self):
        """build_summary must produce a bounded output even with many messages."""
        _at = "@"
        lines = [f"📬 user" + _at + f"example.com — 200 message(s), 50 unread"]
        for i in range(50):
            lines.append(f"[UNREAD] FROM: sender{i}" + _at + "example.com")
            lines.append(f"SUBJ: Topic {i}")
        content = "\n".join(lines)
        result = build_summary(content)
        # Each account caps unread display at 6+more
        self.assertLess(len(result), 5000, "build_summary output too large")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_vector_remember_silent_on_all_failures(self):
        """vector_remember must silently fail, never raising."""
        for exc in [OSError("net"), TimeoutError("timeout"), Exception("server error")]:
            with patch("urllib.request.urlopen", side_effect=exc):
                try:
                    vector_remember("some text")
                except Exception as e:
                    self.fail(f"vector_remember raised {e!r} on {exc!r}")

    def test_main_handles_fetch_subprocess_failure(self):
        """main() must exit(1) when nova_mail_fetch.py fails."""
        import subprocess
        fake_result = MagicMock(returncode=1, stderr="script failed")
        with patch("subprocess.run", return_value=fake_result):
            with self.assertRaises(SystemExit) as ctx:
                _mod.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_main_handles_missing_summary_file(self):
        """main() must exit(1) if summary file doesn't exist after fetch."""
        good_result = MagicMock(returncode=0, stderr="")
        with patch("subprocess.run", return_value=good_result):
            with patch.object(_mod, "SUMMARY_FILE", Path("/nonexistent/path/nova_mail_fetch.txt")):
                with self.assertRaises(SystemExit) as ctx:
                    _mod.main()
        self.assertEqual(ctx.exception.code, 1)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_is_noise_amazon(self):
        self.assertTrue(is_noise("Amazon", "Your Amazon order"))

    def test_is_noise_wayfair(self):
        self.assertTrue(is_noise("Wayfair", "Deal of the day"))

    def test_is_noise_false_for_bank(self):
        self.assertFalse(is_noise("Chase Bank", "Account statement"))

    def test_is_important_amex(self):
        self.assertTrue(is_important("American Express", "Statement ready"))

    def test_is_important_apple_developer(self):
        self.assertTrue(is_important("Apple Developer", "App review update"))

    def test_is_important_false_for_spam(self):
        self.assertFalse(is_important("random_company@spam.com", "Win a prize"))

    def test_parse_accounts_single_account(self):
        _at = "@"
        content = (
            "📬 user" + _at + "example.com — 2 message(s), 1 unread\n"
            "[UNREAD] FROM: alice" + _at + "example.com\n"
            "SUBJ: Hello there\n"
            "[READ] FROM: bob" + _at + "example.com\n"
            "SUBJ: Old message\n"
        )
        accounts = parse_accounts_from_file(content)
        key = "user" + _at + "example.com"
        self.assertIn(key, accounts)
        self.assertEqual(len(accounts[key]), 2)

    def test_parse_accounts_unread_flag(self):
        _at = "@"
        content = (
            "📬 user" + _at + "example.com — 1 message(s), 1 unread\n"
            "[UNREAD] FROM: alice" + _at + "example.com\n"
            "SUBJ: Urgent\n"
        )
        accounts = parse_accounts_from_file(content)
        key = "user" + _at + "example.com"
        msgs = accounts[key]
        self.assertTrue(msgs[0]["unread"])

    def test_parse_accounts_empty_content(self):
        accounts = parse_accounts_from_file("")
        self.assertIsInstance(accounts, dict)
        self.assertEqual(len(accounts), 0)

    def test_build_summary_no_mail(self):
        """build_summary must handle content with no accounts gracefully."""
        result = build_summary("Total messages: 0\n")
        self.assertIn("Nova Mail Summary", result)

    def test_build_summary_shows_important(self):
        _at = "@"
        content = (
            "Total messages: 1\n"
            "📬 user" + _at + "example.com — 1 message(s), 1 unread\n"
            "[UNREAD] FROM: American Express\n"
            "SUBJ: Statement ready\n"
        )
        result = build_summary(content)
        self.assertIn("Important", result)

    def test_slack_post_chunks_long_text(self):
        """slack_post must chunk messages over 3000 chars."""
        post_calls = []
        _nova_cfg.post_both.side_effect = lambda t, **kw: post_calls.append(len(t))
        _mod.slack_post("X" * 7000)
        self.assertGreater(len(post_calls), 1, "Should have chunked the long message")
        for c in post_calls:
            self.assertLessEqual(c, 3000)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_posts_no_mail_message(self):
        """main() must post a 'no mail' Slack message when fetch returns NO_MAIL."""
        import tempfile, subprocess as _sp
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("NO_MAIL: no messages")
            tmp = Path(f.name)

        post_calls = []
        _nova_cfg.post_both.side_effect = lambda t, **kw: post_calls.append(t)

        good_result = MagicMock(returncode=0, stderr="")
        with patch("subprocess.run", return_value=good_result):
            with patch.object(_mod, "SUMMARY_FILE", tmp):
                _mod.main()

        self.assertTrue(any("No new mail" in c for c in post_calls),
                        "Should post 'No new mail' message")
        tmp.unlink(missing_ok=True)

    def test_main_stores_important_in_memory(self):
        """main() must call vector_remember for important messages."""
        import tempfile
        _at = "@"
        content = (
            "Total messages: 1\n"
            "📬 user" + _at + "example.com — 1 message(s), 1 unread\n"
            "[UNREAD] FROM: American Express\n"
            "SUBJ: Statement ready\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            tmp = Path(f.name)

        remembered = []
        good_result = MagicMock(returncode=0, stderr="")
        with patch("subprocess.run", return_value=good_result):
            with patch.object(_mod, "SUMMARY_FILE", tmp):
                with patch.object(_mod, "vector_remember",
                                  side_effect=lambda t, m=None: remembered.append(t)):
                    _mod.main()

        self.assertGreater(len(remembered), 0, "Should have stored important emails in memory")
        tmp.unlink(missing_ok=True)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_build_summary_contains_date(self):
        from datetime import datetime
        result = build_summary("Total messages: 0\n")
        today_year = str(datetime.now().year)
        self.assertIn(today_year, result)

    def test_build_summary_noise_label(self):
        _at = "@"
        content = (
            "Total messages: 3\n"
            "📬 user" + _at + "example.com — 3 message(s), 3 unread\n"
            "[UNREAD] FROM: Amazon\n"
            "SUBJ: Deal of the day\n"
            "[UNREAD] FROM: Wayfair\n"
            "SUBJ: Sale event\n"
            "[UNREAD] FROM: Hulu\n"
            "SUBJ: Watch now\n"
        )
        result = build_summary(content)
        # All 3 are noise unread — should show newsletter label
        self.assertIn("newsletter", result.lower())

    def test_important_patterns_include_herd(self):
        """IMPORTANT_PATTERNS must include herd member names from config stub."""
        for name in ["sam", "gaston"]:
            self.assertTrue(
                any(name in p.lower() for p in _mod.IMPORTANT_PATTERNS),
                f"Herd member '{name}' not in IMPORTANT_PATTERNS"
            )


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_mail_deliver.py has syntax errors: {e}")

    def test_module_constants_present(self):
        self.assertIsInstance(_mod.VECTOR_MEM_URL, str)
        self.assertIsInstance(_mod.NOISE_PATTERNS, list)
        self.assertIsInstance(_mod.IMPORTANT_PATTERNS, list)
        self.assertGreater(len(_mod.NOISE_PATTERNS), 0)
        self.assertGreater(len(_mod.IMPORTANT_PATTERNS), 0)

    def test_all_public_functions_callable(self):
        for fn_name in ["is_noise", "is_important", "parse_accounts_from_file",
                         "build_summary", "send_email", "slack_post",
                         "vector_remember", "main"]:
            fn = getattr(_mod, fn_name, None)
            self.assertIsNotNone(fn, f"Missing function: {fn_name}")
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
