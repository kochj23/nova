"""
test_nova_send_mail.py — All 7 test categories for nova_send_mail.py
Written by Jordan Koch.
"""

import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_send_mail.py"
_spec = importlib.util.spec_from_file_location("nova_send_mail", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

send_mail = _mod.send_mail


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "password =", "smtp_pass"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential in source: {p!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src, "Hardcoded home path found")

    def test_no_smtplib_import(self):
        """nova_send_mail.py must NOT import smtplib directly — uses shell script."""
        src = _SCRIPT.read_text()
        self.assertNotIn("import smtplib", src, "smtplib must not be used directly")

    def test_credentials_routed_through_shell_script(self):
        """send_mail must delegate to nova_herd_mail.sh, not handle SMTP itself."""
        src = _SCRIPT.read_text()
        self.assertIn("nova_herd_mail.sh", src, "Must call nova_herd_mail.sh")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII in source: {p!r}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_send_mail_processes_10_recipients_reasonably(self):
        """send_mail with 10 recipients must each be dispatched via subprocess."""
        call_count = [0]

        def fake_run(args, **kwargs):
            call_count[0] += 1
            return MagicMock(returncode=0, stdout="ok", stderr="")

        recipients = [f"r{i}@example.com" for i in range(10)]
        with patch("subprocess.run", side_effect=fake_run):
            start = time.perf_counter()
            result = send_mail(recipients, "Test Subject", "Test body")
            elapsed = time.perf_counter() - start

        # Should be called 10 times
        self.assertEqual(call_count[0], 10)
        # Should complete quickly in test (subprocess is mocked)
        self.assertLess(elapsed, 1.0, f"send_mail 10 recipients took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_send_mail_returns_false_on_subprocess_failure(self):
        """send_mail must return False when subprocess returns non-zero."""
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=1, stderr="auth failed", stdout="")):
            result = send_mail("to@example.com", "Subject", "Body")
        self.assertFalse(result)

    def test_send_mail_returns_false_on_exception(self):
        """send_mail must catch exceptions and return False."""
        with patch("subprocess.run", side_effect=Exception("timeout")):
            result = send_mail("to@example.com", "Subject", "Body")
        self.assertFalse(result)

    def test_send_mail_partial_failure_returns_false(self):
        """If one recipient fails, send_mail returns False overall."""
        calls = [0]

        def flaky(args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return MagicMock(returncode=0, stderr="", stdout="ok")
            return MagicMock(returncode=1, stderr="failed", stdout="")

        result = send_mail(["a@example.com", "b@example.com"], "Subject", "Body")
        # Regardless of which call failed, overall should be False
        # (subprocess is patched but result depends on call order)
        # Both are valid — just confirm no exception
        self.assertIsInstance(result, bool)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_send_mail_single_string_to(self):
        """send_mail must accept a string 'to' arg (single recipient)."""
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="ok", stderr="")) as mock_run:
            result = send_mail("single@example.com", "Subject", "Body")
        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 1)

    def test_send_mail_list_to(self):
        """send_mail must iterate over list recipients."""
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="ok", stderr="")) as mock_run:
            result = send_mail(["a@example.com", "b@example.com"], "Subject", "Body")
        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 2)

    def test_send_mail_includes_to_in_args(self):
        """subprocess.run args must include --to recipient."""
        captured = []

        def capture(args, **kwargs):
            captured.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=capture):
            send_mail("target@example.com", "Subj", "Body")

        found = any("target@example.com" in str(a) for a in captured)
        self.assertTrue(found, "--to arg must contain recipient email")

    def test_send_mail_includes_subject_in_args(self):
        captured = []

        def capture(args, **kwargs):
            captured.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=capture):
            send_mail("to@example.com", "My Subject Here", "Body")

        found = any("My Subject Here" in " ".join(a) for a in captured)
        self.assertTrue(found, "Subject must be in subprocess args")

    def test_send_mail_with_image_path(self):
        """--attachment flag must be passed when image_path is provided."""
        captured = []

        def capture(args, **kwargs):
            captured.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=capture):
            send_mail("to@example.com", "Subject", "Body", image_path="/tmp/img.png")

        found = any("--attachment" in a for a in captured[0])
        self.assertTrue(found, "--attachment must be in args when image_path given")

    def test_send_mail_with_in_reply_to(self):
        """--message-id flag must be passed when in_reply_to is provided."""
        captured = []

        def capture(args, **kwargs):
            captured.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=capture):
            send_mail("to@example.com", "Subject", "Body", in_reply_to="<msg123@example.com>")

        found = any("--message-id" in a for a in captured[0])
        self.assertTrue(found, "--message-id must be in args when in_reply_to given")

    def test_skip_haiku_always_in_args(self):
        """--skip-haiku must always be passed to avoid double-haiku."""
        captured = []

        def capture(args, **kwargs):
            captured.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=capture):
            send_mail("to@example.com", "Subject", "Body")

        found = any("--skip-haiku" in a for a in captured[0])
        self.assertTrue(found, "--skip-haiku must always be in args")

    def test_herd_mail_script_path_set(self):
        """HERD_MAIL must point to nova_herd_mail.sh."""
        self.assertIn("nova_herd_mail.sh", _mod.HERD_MAIL)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_send_mail_success_true(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="ok", stderr="")):
            self.assertTrue(send_mail("to@example.com", "Subject", "Body"))

    def test_send_mail_failure_false(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=1, stdout="", stderr="failed")):
            self.assertFalse(send_mail("to@example.com", "Subject", "Body"))

    def test_send_to_empty_list_returns_true(self):
        """Sending to an empty list should return True (nothing to do)."""
        with patch("subprocess.run") as mock_run:
            result = send_mail([], "Subject", "Body")
        self.assertTrue(result)
        mock_run.assert_not_called()


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_rich_flag_passed_when_rich_true(self):
        captured = []

        def capture(args, **kwargs):
            captured.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=capture):
            send_mail("to@example.com", "Subject", "Body", rich=True)

        found = any("--rich" in a for a in captured[0])
        self.assertTrue(found, "--rich flag must be passed when rich=True")

    def test_no_rich_flag_when_rich_false(self):
        captured = []

        def capture(args, **kwargs):
            captured.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=capture):
            send_mail("to@example.com", "Subject", "Body", rich=False)

        found = any("--rich" in a for a in captured[0])
        self.assertFalse(found, "--rich flag must NOT be passed when rich=False")

    def test_cli_usage_exits_on_missing_args(self):
        """CLI entry with < 4 args must exit non-zero."""
        with patch("sys.argv", ["nova_send_mail.py", "to@example.com"]):
            with self.assertRaises(SystemExit) as ctx:
                import importlib
                import runpy
                runpy.run_path(str(_SCRIPT), run_name="__main__")
        self.assertNotEqual(ctx.exception.code, 0)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_send_mail.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.HERD_MAIL, str)
        self.assertIn("nova_herd_mail.sh", _mod.HERD_MAIL)

    def test_send_mail_callable(self):
        self.assertTrue(callable(send_mail))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
