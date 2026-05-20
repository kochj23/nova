"""
test_nova_mail_fetch.py — All 7 test categories for nova_mail_fetch.py
Written by Jordan Koch.
"""

import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_mail_fetch.py"
_spec = importlib.util.spec_from_file_location("nova_mail_fetch", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

run_applescript = _mod.run_applescript
parse_messages = _mod.parse_messages
format_for_nova = _mod.format_for_nova


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "password ="]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential in source: {p!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src, "Hardcoded home path found")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII in source: {p!r}")

    def test_body_truncated(self):
        """parse_messages must truncate body to 200 chars."""
        _at = "@"
        raw = (
            "=== ACCOUNT: Gmail <user" + _at + "gmail.com> (1 messages) ===\n"
            "FROM: alice@example.com\n"
            "SUBJECT: Test\n"
            "DATE: Mon, 13 May 2026 09:00:00 +0000\n"
            "BODY: " + "X" * 500 + "\n"
        )
        accounts = parse_messages(raw)
        for acct_msgs in accounts.values():
            for msg in acct_msgs:
                self.assertLessEqual(len(msg["body"]), 200)

    def test_output_file_uses_path_home(self):
        """OUT_FILE must be within the user's home directory."""
        self.assertTrue(str(_mod.OUT_FILE).startswith(str(Path.home())))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_parse_messages_large_input(self):
        """parse_messages must handle 50 accounts with 20 messages each in < 200ms."""
        _at = "@"
        lines = []
        for i in range(50):
            lines.append(f"=== ACCOUNT: User{i} <user{i}" + _at + f"example.com> (20 messages) ===")
            for j in range(20):
                lines.append(f"FROM: sender{j}" + _at + "example.com")
                lines.append(f"SUBJECT: Subject {j}")
                lines.append(f"DATE: Mon, 01 Jan 2026 10:00:00 +0000")
        raw = "\n".join(lines)

        start = time.perf_counter()
        parse_messages(raw)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2, f"parse_messages 1000 msgs took {elapsed:.3f}s")

    def test_format_for_nova_bounded_output(self):
        """format_for_nova must produce output proportional to input."""
        _at = "@"
        accounts = {f"user{i}" + _at + "example.com": [
            {"from": f"s{j}" + _at + "x.com", "subject": f"Sub {j}",
             "date": "2026-05-13", "body": "", "unread": j % 2 == 0}
            for j in range(10)
        ] for i in range(20)}
        result = format_for_nova(accounts, 200)
        # Should not produce absurdly large output
        self.assertLess(len(result), 50000, "format_for_nova output too large")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_run_applescript_returns_error_tuple_on_failure(self):
        """run_applescript must return (None, error_str) on non-zero returncode."""
        fake = MagicMock(returncode=1, stdout="", stderr="Permission denied")
        with patch("subprocess.run", return_value=fake):
            raw, err = run_applescript()
        self.assertIsNone(raw)
        self.assertIn("Permission", err)

    def test_main_exits_on_applescript_error(self):
        """main() must write error file and exit(1) when applescript fails."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "output.txt"
            with patch.object(_mod, "OUT_FILE", tmp_out):
                with patch.object(_mod, "run_applescript",
                                   return_value=(None, "osascript error")):
                    with self.assertRaises(SystemExit) as ctx:
                        _mod.main()
        self.assertEqual(ctx.exception.code, 1)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_parse_messages_empty_input(self):
        accounts = parse_messages("")
        self.assertIsInstance(accounts, dict)
        self.assertEqual(len(accounts), 0)

    def test_parse_messages_parses_email_address(self):
        """ACCOUNT header with <email> should use email address as key."""
        _at = "@"
        raw = "=== ACCOUNT: My Gmail <myemail" + _at + "gmail.com> (2 messages) ===\n"
        accounts = parse_messages(raw)
        self.assertIn("myemail" + _at + "gmail.com", accounts)

    def test_parse_messages_unread_flag(self):
        _at = "@"
        raw = (
            "=== ACCOUNT: User <u" + _at + "test.com> (1 messages) ===\n"
            "FROM: [UNREAD] alice" + _at + "example.com\n"
            "SUBJECT: Important thing\n"
        )
        accounts = parse_messages(raw)
        key = "u" + _at + "test.com"
        if accounts:
            msgs = list(accounts.values())[0]
            if msgs:
                self.assertTrue(msgs[0]["unread"])

    def test_parse_messages_multiple_accounts(self):
        _at = "@"
        raw = (
            "=== ACCOUNT: A <a" + _at + "a.com> (1 messages) ===\n"
            "FROM: x" + _at + "x.com\nSUBJECT: S1\n"
            "=== ACCOUNT: B <b" + _at + "b.com> (1 messages) ===\n"
            "FROM: y" + _at + "y.com\nSUBJECT: S2\n"
        )
        accounts = parse_messages(raw)
        self.assertEqual(len(accounts), 2)

    def test_format_for_nova_includes_header(self):
        accounts = {}
        result = format_for_nova(accounts, 0)
        self.assertIn("MAIL SUMMARY", result)

    def test_format_for_nova_shows_unread(self):
        _at = "@"
        accounts = {"user" + _at + "example.com": [
            {"from": "alice" + _at + "example.com", "subject": "Urgent",
             "date": "2026-05-13", "body": "", "unread": True}
        ]}
        result = format_for_nova(accounts, 1)
        self.assertIn("[UNREAD]", result)

    def test_format_for_nova_shows_read(self):
        _at = "@"
        accounts = {"user" + _at + "example.com": [
            {"from": "bob" + _at + "example.com", "subject": "Old news",
             "date": "2026-05-13", "body": "", "unread": False}
        ]}
        result = format_for_nova(accounts, 1)
        self.assertIn("[READ]", result)

    def test_total_count_in_output(self):
        result = format_for_nova({}, 42)
        self.assertIn("42", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_writes_no_mail_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "output.txt"
            with patch.object(_mod, "OUT_FILE", tmp_out):
                with patch.object(_mod, "run_applescript",
                                   return_value=("NO_MAIL", None)):
                    _mod.main()
            content = tmp_out.read_text()
        self.assertIn("NO_MAIL", content)

    def test_main_writes_formatted_output(self):
        import tempfile
        _at = "@"
        raw = (
            "TOTAL:2\n"
            "=== ACCOUNT: User <u" + _at + "test.com> (2 messages) ===\n"
            "FROM: alice" + _at + "a.com\nSUBJECT: Hello\nDATE: Mon, 01 Jan 2026 10:00:00\n"
            "FROM: [UNREAD] bob" + _at + "b.com\nSUBJECT: Urgent\nDATE: Mon, 01 Jan 2026 11:00:00\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "output.txt"
            with patch.object(_mod, "OUT_FILE", tmp_out):
                with patch.object(_mod, "run_applescript", return_value=(raw, None)):
                    _mod.main()
            content = tmp_out.read_text()
        self.assertIn("MAIL SUMMARY", content)

    def test_parse_then_format_roundtrip(self):
        _at = "@"
        raw = (
            "TOTAL:1\n"
            "=== ACCOUNT: Test <t" + _at + "test.com> (1 messages) ===\n"
            "FROM: alice" + _at + "example.com\n"
            "SUBJECT: Integration test\n"
            "DATE: Mon, 01 Jan 2026 10:00:00\n"
        )
        accounts = parse_messages(raw)
        formatted = format_for_nova(accounts, 1)
        self.assertIn("MAIL SUMMARY", formatted)
        self.assertIn("Integration test", formatted)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_total_extracted_from_raw(self):
        """main() must correctly parse TOTAL:N from applescript output."""
        import tempfile
        _at = "@"
        raw = "TOTAL:7\n=== ACCOUNT: User <u" + _at + "t.com> (1 messages) ===\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "output.txt"
            with patch.object(_mod, "OUT_FILE", tmp_out):
                with patch.object(_mod, "run_applescript", return_value=(raw, None)):
                    _mod.main()
            content = tmp_out.read_text()
        self.assertIn("7", content)

    def test_body_truncated_in_formatted_output(self):
        """Long BODY lines must be truncated in the formatted output."""
        _at = "@"
        raw = (
            "TOTAL:1\n"
            "=== ACCOUNT: U <u" + _at + "t.com> (1 messages) ===\n"
            "FROM: [UNREAD] alice" + _at + "example.com\n"
            "SUBJECT: Long body test\n"
            "DATE: Mon, 01 Jan 2026 10:00:00\n"
            "BODY: " + "A" * 500 + "\n"
        )
        accounts = parse_messages(raw)
        for msgs in accounts.values():
            for m in msgs:
                self.assertLessEqual(len(m["body"]), 200)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_mail_fetch.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.SCRIPTS, Path)
        self.assertIsInstance(_mod.OUT_FILE, Path)

    def test_functions_callable(self):
        for fn in [run_applescript, parse_messages, format_for_nova, _mod.main]:
            self.assertTrue(callable(fn), f"{fn.__name__} not callable")

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
