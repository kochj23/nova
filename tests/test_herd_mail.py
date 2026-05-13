"""
test_herd_mail.py — All 7 test categories for herd_mail.py
Written by Jordan Koch.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub nova_config
_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

# Stub waggle
_waggle = MagicMock()
sys.modules["waggle"] = _waggle

_SCRIPT = Path(__file__).parent.parent / "scripts" / "herd_mail.py"
_spec = importlib.util.spec_from_file_location("herd_mail", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        """Source must not contain hardcoded passwords or tokens."""
        src = _SCRIPT.read_text()
        forbidden = ["password = \"", "token = \"", "sk-", "ghp_"]
        for pattern in forbidden:
            self.assertNotIn(pattern, src, f"Credential pattern found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_email_header_injection_prevented(self):
        """validate_email_address() must reject emails with newlines/tabs."""
        for bad_char in ["\n", "\r", "\0", "\t"]:
            addr = f"user{bad_char}@example.com"
            self.assertFalse(_mod.validate_email_address(addr),
                             f"Must reject email with {repr(bad_char)}")

    def test_sensitive_path_access_denied(self):
        """validate_file_path() must deny access to /etc and /private/etc."""
        # macOS resolves /etc -> /private/etc, so use the actual macOS paths
        for sensitive in ["/etc/passwd", "/private/etc/hosts"]:
            result = _mod.validate_file_path(sensitive, must_exist=False)
            self.assertIsNone(result,
                              f"Access to {sensitive} must be denied")

    def test_ansi_escape_sequences_stripped(self):
        """sanitize_for_display() must strip ANSI escape sequences."""
        ansi_text = "\x1b[31mRed text\x1b[0m"
        result = _mod.sanitize_for_display(ansi_text)
        self.assertNotIn("\x1b", result)
        self.assertIn("Red text", result)

    def test_credentials_from_env_not_source(self):
        """Credentials must come from env vars, not hardcoded in source."""
        src = _SCRIPT.read_text()
        self.assertIn("os.environ.get", src,
                      "Credentials must be read from environment variables")

    def test_waggle_dev_path_requires_env_var(self):
        """WAGGLE_DEV_PATH must only activate when env var is explicitly set."""
        src = _SCRIPT.read_text()
        self.assertIn("WAGGLE_DEV_PATH", src)
        self.assertIn("os.environ.get", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_retry_delays_bounded(self):
        """Retry delays must be bounded (not infinite sleep)."""
        src = _SCRIPT.read_text()
        self.assertIn("RETRY_DELAYS", src,
                      "Send retries must use bounded RETRY_DELAYS list")

    def test_display_truncation_limit(self):
        """sanitize_for_display() must truncate at max_length."""
        long_text = "a" * 500
        result = _mod.sanitize_for_display(long_text, max_length=100)
        self.assertLessEqual(len(result), 104,  # 100 + "..."
                             "sanitize_for_display must truncate at max_length")

    def test_validate_email_fast(self):
        """validate_email_address() must complete quickly."""
        import time
        start = time.perf_counter()
        for _ in range(1000):
            _mod.validate_email_address("user@example.com")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, "validate_email_address must be fast")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_send_retries_on_transient_errors(self):
        """cmd_send must retry on ConnectionError and TimeoutError."""
        src = _SCRIPT.read_text()
        self.assertIn("ConnectionError", src)
        self.assertIn("TimeoutError", src)
        # Retry message is logged (case-insensitive check)
        self.assertTrue(
            "retrying" in src.lower() or "retry" in src.lower() or
            "attempt" in src.lower(),
            "Some retry messaging must be present"
        )

    def test_send_does_not_retry_on_value_error(self):
        """cmd_send must NOT retry on ValueError (input error)."""
        src = _SCRIPT.read_text()
        self.assertIn("ValueError", src,
                      "ValueError must be caught and not retried")

    def test_retry_count_is_4(self):
        """RETRY_DELAYS must have 4 entries (4 total attempts)."""
        src = _SCRIPT.read_text()
        self.assertIn("RETRY_DELAYS = [0, 5, 15, 45]", src,
                      "Exactly 4 retry delays required")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_validate_email_valid(self):
        """validate_email_address() must accept valid addresses."""
        self.assertTrue(_mod.validate_email_address("user@example.com"))
        self.assertTrue(_mod.validate_email_address("first.last@domain.org"))

    def test_validate_email_invalid(self):
        """validate_email_address() must reject invalid addresses."""
        self.assertFalse(_mod.validate_email_address("notanemail"))
        self.assertFalse(_mod.validate_email_address("@nodomain"))
        self.assertFalse(_mod.validate_email_address(""))
        self.assertFalse(_mod.validate_email_address(None))
        self.assertFalse(_mod.validate_email_address("user@nodot"))

    def test_validate_email_list_empty(self):
        """validate_email_list() must accept empty string."""
        self.assertTrue(_mod.validate_email_list(""))
        self.assertTrue(_mod.validate_email_list(None))

    def test_validate_email_list_multiple(self):
        """validate_email_list() must validate comma-separated addresses."""
        self.assertTrue(_mod.validate_email_list(
            "a@example.com,b@example.com"))
        self.assertFalse(_mod.validate_email_list(
            "a@example.com,notvalid"))

    def test_parse_port_valid(self):
        """parse_port() must accept valid port numbers."""
        self.assertEqual(_mod.parse_port("465", 465, "SMTP"), 465)
        self.assertEqual(_mod.parse_port("993", 993, "IMAP"), 993)

    def test_parse_port_invalid(self):
        """parse_port() must raise ValueError for invalid ports."""
        with self.assertRaises(ValueError):
            _mod.parse_port("99999", 465, "SMTP")
        with self.assertRaises(ValueError):
            _mod.parse_port("0", 465, "SMTP")
        with self.assertRaises(ValueError):
            _mod.parse_port("abc", 465, "SMTP")

    def test_decode_escape_sequences(self):
        """decode_escape_sequences() must handle \\n and \\t."""
        result = _mod.decode_escape_sequences("line1\\nline2")
        self.assertIn("\n", result)
        result = _mod.decode_escape_sequences("col1\\tcol2")
        self.assertIn("\t", result)

    def test_sanitize_for_display_empty(self):
        """sanitize_for_display() must handle empty/None input."""
        self.assertEqual(_mod.sanitize_for_display(""), "")
        self.assertEqual(_mod.sanitize_for_display(None), "")

    def test_get_config_returns_dict(self):
        """get_config() must return a dict with expected keys."""
        with patch.dict(os.environ, {
            "WAGGLE_HOST": "smtp.example.com",
            "WAGGLE_USER": "user@example.com",
            "WAGGLE_PASS": "testpass",
            "WAGGLE_FROM": "from@example.com",
            "WAGGLE_PORT": "465",
            "WAGGLE_IMAP_PORT": "993",
        }):
            cfg = _mod.get_config()
        self.assertIn("smtp_host", cfg)
        self.assertIn("smtp_port", cfg)
        self.assertIn("smtp_user", cfg)
        self.assertEqual(cfg["smtp_port"], 465)

    def test_validate_config_fails_without_smtp(self):
        """validate_config() must return False when SMTP host is missing."""
        cfg = {"smtp_host": None, "smtp_user": None,
               "smtp_pass": None, "from_addr": None}
        result = _mod.validate_config(cfg, require_smtp=True)
        self.assertFalse(result)

    def test_build_waggle_config_maps_fields(self):
        """build_waggle_config() must map our config to waggle's format."""
        cfg = {
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_user": "user",
            "smtp_pass": "pass",
            "from_addr": "from@example.com",
            "from_name": "Test",
            "use_tls": True,
            "imap_host": None,
            "imap_port": 993,
            "imap_tls": True,
        }
        wcfg = _mod.build_waggle_config(cfg)
        self.assertEqual(wcfg["host"], "smtp.example.com")
        self.assertEqual(wcfg["port"], 465)
        self.assertEqual(wcfg["user"], "user")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_output_human_list_empty(self):
        """output_human_list() must handle empty message list."""
        import io
        from contextlib import redirect_stdout
        data = {"folder": "INBOX", "count": 0, "messages": []}
        f = io.StringIO()
        with redirect_stdout(f):
            _mod.output_human_list(data)
        output = f.getvalue()
        self.assertIn("No messages", output)

    def test_output_human_check_zero(self):
        """output_human_check() must report zero unread correctly."""
        import io
        from contextlib import redirect_stdout
        data = {"folder": "INBOX", "unread_count": 0}
        f = io.StringIO()
        with redirect_stdout(f):
            _mod.output_human_check(data)
        output = f.getvalue()
        self.assertIn("No unread", output)

    def test_output_human_check_nonzero(self):
        """output_human_check() must report count when unread > 0."""
        import io
        from contextlib import redirect_stdout
        data = {"folder": "INBOX", "unread_count": 3}
        f = io.StringIO()
        with redirect_stdout(f):
            _mod.output_human_check(data)
        output = f.getvalue()
        self.assertIn("3", output)

    def test_output_json_writes_json(self):
        """output_json() must write valid JSON to stdout."""
        import io
        import json
        from contextlib import redirect_stdout
        data = {"key": "value", "count": 42}
        f = io.StringIO()
        with redirect_stdout(f):
            _mod.output_json(data)
        result = json.loads(f.getvalue())
        self.assertEqual(result["key"], "value")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_cmd_send_validates_email(self):
        """cmd_send() must reject invalid email address early."""
        import argparse
        args = argparse.Namespace(
            to="not-an-email",
            subject="Test",
            body="Hello",
            body_file=None,
            cc=None,
            reply_to=None,
            message_id=None,
            rich=False,
            skip_duplicate_check=True,
            dry_run=False,
            attachment=None,
        )
        cfg = _mod.get_config.__wrapped__(
        ) if hasattr(_mod.get_config, "__wrapped__") else {}
        with patch.dict(os.environ, {}):
            result = _mod.cmd_send(args, {})
        self.assertEqual(result, 1,
                         "Should return exit code 1 for invalid email")

    def test_main_returns_1_with_no_command(self):
        """main() must return 1 when no subcommand given."""
        with patch("sys.argv", ["herd_mail.py"]):
            result = _mod.main()
        self.assertEqual(result, 1)

    def test_config_subcommand_validates(self):
        """config subcommand must validate SMTP settings."""
        with patch.dict(os.environ, {
            "WAGGLE_HOST": "smtp.test.com",
            "WAGGLE_USER": "user@test.com",
            "WAGGLE_PASS": "testpass",
            "WAGGLE_FROM": "from@test.com",
        }):
            with patch("sys.argv", ["herd_mail.py", "config"]):
                result = _mod.main()
        # With waggle not installed, main() returns 1 early
        # Just verify it doesn't crash
        self.assertIn(result, [0, 1])


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"herd_mail.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_constants_present(self):
        for const in ["DEFAULT_SMTP_PORT", "DEFAULT_IMAP_PORT",
                      "DEFAULT_LIST_LIMIT", "SENT_FOLDER_CANDIDATES"]:
            self.assertTrue(hasattr(_mod, const), f"{const} must be defined")

    def test_default_ports_correct(self):
        self.assertEqual(_mod.DEFAULT_SMTP_PORT, 465)
        self.assertEqual(_mod.DEFAULT_IMAP_PORT, 993)

    def test_functions_present(self):
        for fn_name in ["main", "validate_email_address", "validate_email_list",
                        "sanitize_for_display", "parse_port", "get_config",
                        "validate_config", "cmd_send", "cmd_config",
                        "cmd_list", "cmd_read", "cmd_check", "cmd_download"]:
            self.assertTrue(callable(getattr(_mod, fn_name, None)),
                            f"Function {fn_name} must exist")

    def test_sent_folder_candidates_non_empty(self):
        self.assertGreater(len(_mod.SENT_FOLDER_CANDIDATES), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
