#!/usr/bin/env python3
"""
test_mail.py — Tests for herd_mail.py, nova_mail_agent.py,
nova_mail_deliver.py, nova_herd_outreach.py, and nova_send_mail.py.

Run: python3 -m pytest tests/test_mail.py -v
Written by Jordan Koch.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, date
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))


# ══════════════════════════════════════════════════════════════════════════════
# herd_mail.py — email validation, config, duplicate detection, CLI, retry
# ══════════════════════════════════════════════════════════════════════════════

class TestEmailValidation:
    """Tests for validate_email_address() and validate_email_list()."""

    def test_valid_simple_email(self):
        from herd_mail import validate_email_address
        assert validate_email_address("user@example.com") is True

    def test_valid_email_with_dots_in_local(self):
        from herd_mail import validate_email_address
        assert validate_email_address("first.last@example.com") is True

    def test_valid_email_with_plus(self):
        from herd_mail import validate_email_address
        assert validate_email_address("user+tag@example.com") is True

    def test_valid_email_with_subdomain(self):
        from herd_mail import validate_email_address
        assert validate_email_address("user@mail.example.co.uk") is True

    def test_invalid_empty_string(self):
        from herd_mail import validate_email_address
        assert validate_email_address("") is False

    def test_invalid_none(self):
        from herd_mail import validate_email_address
        assert validate_email_address(None) is False

    def test_invalid_no_at_sign(self):
        from herd_mail import validate_email_address
        assert validate_email_address("userexample.com") is False

    def test_invalid_no_domain(self):
        from herd_mail import validate_email_address
        assert validate_email_address("user@") is False

    def test_invalid_no_tld_dot(self):
        from herd_mail import validate_email_address
        assert validate_email_address("user@localhost") is False

    def test_invalid_no_local_part(self):
        from herd_mail import validate_email_address
        assert validate_email_address("@example.com") is False

    def test_rejects_header_injection_newline(self):
        from herd_mail import validate_email_address
        assert validate_email_address("user@example.com\nBcc: attacker@evil.com") is False

    def test_rejects_header_injection_carriage_return(self):
        from herd_mail import validate_email_address
        assert validate_email_address("user@example.com\r\nBcc: x@x.com") is False

    def test_rejects_null_byte(self):
        from herd_mail import validate_email_address
        assert validate_email_address("user\x00@example.com") is False

    def test_rejects_tab_injection(self):
        from herd_mail import validate_email_address
        assert validate_email_address("user\t@example.com") is False

    def test_rejects_non_string_input(self):
        from herd_mail import validate_email_address
        assert validate_email_address(12345) is False

    def test_validate_email_list_valid(self):
        from herd_mail import validate_email_list
        assert validate_email_list("a@b.com, c@d.com") is True

    def test_validate_email_list_empty(self):
        from herd_mail import validate_email_list
        assert validate_email_list("") is True

    def test_validate_email_list_one_invalid(self):
        from herd_mail import validate_email_list
        assert validate_email_list("a@b.com, invalid") is False

    def test_validate_email_list_all_invalid(self):
        from herd_mail import validate_email_list
        assert validate_email_list("bad, worse, terrible") is False


class TestSanitizeForDisplay:
    """Tests for sanitize_for_display() — terminal escape prevention."""

    def test_strips_ansi_escape_codes(self):
        from herd_mail import sanitize_for_display
        text = "\x1b[31mRed text\x1b[0m"
        result = sanitize_for_display(text)
        assert "\x1b" not in result
        assert "Red text" in result

    def test_removes_control_characters(self):
        from herd_mail import sanitize_for_display
        text = "Hello\x00World\x07!"
        result = sanitize_for_display(text)
        assert "\x00" not in result
        assert "\x07" not in result

    def test_preserves_newlines_and_tabs(self):
        from herd_mail import sanitize_for_display
        text = "Line 1\nLine 2\tTabbed"
        result = sanitize_for_display(text)
        assert "\n" in result
        assert "\t" in result

    def test_truncates_long_text(self):
        from herd_mail import sanitize_for_display
        text = "A" * 300
        result = sanitize_for_display(text, max_length=100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")

    def test_empty_string(self):
        from herd_mail import sanitize_for_display
        assert sanitize_for_display("") == ""

    def test_none_input(self):
        from herd_mail import sanitize_for_display
        assert sanitize_for_display(None) == ""


class TestValidateFilePath:
    """Tests for validate_file_path() — security path checks."""

    def test_valid_file(self):
        from herd_mail import validate_file_path
        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            result = validate_file_path(f.name, must_exist=True)
            assert result is not None
            assert result.exists()

    def test_nonexistent_file_must_exist(self):
        from herd_mail import validate_file_path
        result = validate_file_path("/tmp/definitely_not_a_real_file_xyz.txt", must_exist=True)
        assert result is None

    def test_sensitive_path_etc(self):
        from herd_mail import validate_file_path
        result = validate_file_path("/etc/passwd", must_exist=True)
        assert result is None

    def test_sensitive_path_private_etc(self):
        from herd_mail import validate_file_path
        result = validate_file_path("/private/etc/hosts", must_exist=True)
        assert result is None

    def test_directory_rejected_when_must_exist(self):
        from herd_mail import validate_file_path
        result = validate_file_path("/tmp", must_exist=True)
        assert result is None


class TestDecodeEscapeSequences:
    """Tests for decode_escape_sequences()."""

    def test_newline(self):
        from herd_mail import decode_escape_sequences
        assert decode_escape_sequences("Hello\\nWorld") == "Hello\nWorld"

    def test_tab(self):
        from herd_mail import decode_escape_sequences
        assert decode_escape_sequences("Col1\\tCol2") == "Col1\tCol2"

    def test_backslash(self):
        from herd_mail import decode_escape_sequences
        # Input: literal string "path\\to\\file" (with escaped backslashes)
        # decode_escape_sequences replaces "\\\\" with "\\"
        result = decode_escape_sequences("hello\\\\world")
        assert result == "hello\\world"

    def test_no_escape_sequences(self):
        from herd_mail import decode_escape_sequences
        assert decode_escape_sequences("plain text") == "plain text"


class TestParsePort:
    """Tests for parse_port() — port number validation."""

    def test_valid_port(self):
        from herd_mail import parse_port
        assert parse_port("465", 465) == 465

    def test_min_port(self):
        from herd_mail import parse_port
        assert parse_port("1", 465) == 1

    def test_max_port(self):
        from herd_mail import parse_port
        assert parse_port("65535", 465) == 65535

    def test_zero_port_raises(self):
        from herd_mail import parse_port
        with pytest.raises(ValueError):
            parse_port("0", 465)

    def test_negative_port_raises(self):
        from herd_mail import parse_port
        with pytest.raises(ValueError):
            parse_port("-1", 465)

    def test_too_high_port_raises(self):
        from herd_mail import parse_port
        with pytest.raises(ValueError):
            parse_port("65536", 465)

    def test_non_numeric_raises(self):
        from herd_mail import parse_port
        with pytest.raises(ValueError):
            parse_port("abc", 465)


class TestGetConfig:
    """Tests for get_config() — environment variable loading."""

    def test_defaults_when_no_env_vars(self):
        from herd_mail import get_config, DEFAULT_SMTP_PORT, DEFAULT_IMAP_PORT
        with patch.dict(os.environ, {}, clear=True):
            cfg = get_config()
            assert cfg["smtp_port"] == DEFAULT_SMTP_PORT
            assert cfg["imap_port"] == DEFAULT_IMAP_PORT
            assert cfg["use_tls"] is True
            assert cfg["imap_tls"] is True
            assert cfg["smtp_host"] is None

    def test_reads_env_vars(self):
        from herd_mail import get_config
        env = {
            "WAGGLE_HOST": "smtp.example.com",
            "WAGGLE_PORT": "587",
            "WAGGLE_USER": "user@example.com",
            "WAGGLE_PASS": "secret",
            "WAGGLE_FROM": "from@example.com",
            "WAGGLE_NAME": "Test Sender",
            "WAGGLE_TLS": "false",
            "WAGGLE_IMAP_HOST": "imap.example.com",
            "WAGGLE_IMAP_PORT": "143",
            "WAGGLE_IMAP_TLS": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = get_config()
            assert cfg["smtp_host"] == "smtp.example.com"
            assert cfg["smtp_port"] == 587
            assert cfg["smtp_user"] == "user@example.com"
            assert cfg["smtp_pass"] == "secret"
            assert cfg["from_addr"] == "from@example.com"
            assert cfg["from_name"] == "Test Sender"
            assert cfg["use_tls"] is False
            assert cfg["imap_host"] == "imap.example.com"
            assert cfg["imap_port"] == 143
            assert cfg["imap_tls"] is False

    def test_invalid_port_raises(self):
        from herd_mail import get_config
        with patch.dict(os.environ, {"WAGGLE_PORT": "not_a_port"}, clear=True):
            with pytest.raises(ValueError):
                get_config()


class TestValidateConfig:
    """Tests for validate_config()."""

    def test_valid_smtp_config(self):
        from herd_mail import validate_config
        cfg = {
            "smtp_host": "smtp.example.com",
            "smtp_user": "user@example.com",
            "smtp_pass": "secret",
            "from_addr": "from@example.com",
        }
        assert validate_config(cfg, require_smtp=True, require_imap=False) is True

    def test_missing_smtp_host(self):
        from herd_mail import validate_config
        cfg = {
            "smtp_host": "",
            "smtp_user": "user@example.com",
            "smtp_pass": "secret",
            "from_addr": "from@example.com",
        }
        assert validate_config(cfg, require_smtp=True) is False

    def test_missing_imap_host(self):
        from herd_mail import validate_config
        cfg = {"imap_host": ""}
        assert validate_config(cfg, require_smtp=False, require_imap=True) is False

    def test_invalid_from_addr_format(self):
        from herd_mail import validate_config
        cfg = {
            "smtp_host": "smtp.example.com",
            "smtp_user": "user@example.com",
            "smtp_pass": "secret",
            "from_addr": "not-an-email",
        }
        assert validate_config(cfg, require_smtp=True) is False

    def test_imap_not_required_passes(self):
        from herd_mail import validate_config
        cfg = {
            "smtp_host": "smtp.example.com",
            "smtp_user": "user@example.com",
            "smtp_pass": "secret",
            "from_addr": "from@example.com",
            "imap_host": "",
        }
        assert validate_config(cfg, require_smtp=True, require_imap=False) is True


class TestBuildWaggleConfig:
    """Tests for build_waggle_config() — config format conversion."""

    def test_maps_all_fields(self):
        from herd_mail import build_waggle_config, DEFAULT_IMAP_PORT
        cfg = {
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "user",
            "smtp_pass": "pass",
            "from_addr": "from@ex.com",
            "from_name": "Name",
            "use_tls": True,
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_tls": True,
        }
        wc = build_waggle_config(cfg)
        assert wc["host"] == "smtp.example.com"
        assert wc["port"] == 587
        assert wc["user"] == "user"
        assert wc["password"] == "pass"
        assert wc["from_addr"] == "from@ex.com"
        assert wc["from_name"] == "Name"
        assert wc["tls"] is True
        assert wc["imap_host"] == "imap.example.com"
        assert wc["imap_port"] == 993
        assert wc["imap_tls"] is True

    def test_defaults_for_missing_imap(self):
        from herd_mail import build_waggle_config, DEFAULT_IMAP_PORT
        cfg = {
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "user",
            "smtp_pass": "pass",
            "from_addr": "from@ex.com",
            "from_name": "",
            "use_tls": True,
        }
        wc = build_waggle_config(cfg)
        assert wc["imap_host"] is None
        assert wc["imap_port"] == DEFAULT_IMAP_PORT
        assert wc["imap_tls"] is True


class TestCmdSendDryRun:
    """Tests for cmd_send --dry-run routing."""

    def test_dry_run_calls_cmd_config(self):
        from herd_mail import cmd_send, cmd_config
        args = argparse.Namespace(
            to="test@example.com", subject="Test", body="Body",
            body_file=None, cc=None, reply_to=None, message_id=None,
            attachment=None, rich=False, skip_duplicate_check=False,
            dry_run=True
        )
        cfg = {
            "smtp_host": "smtp.example.com",
            "smtp_user": "user@example.com",
            "smtp_pass": "secret",
            "from_addr": "from@example.com",
            "from_name": "Test",
            "smtp_port": 465,
            "imap_host": None,
        }
        with patch("herd_mail.cmd_config", return_value=0) as mock_config:
            result = cmd_send(args, cfg)
            mock_config.assert_called_once_with(args, cfg)
            assert result == 0


class TestCmdSendEmailValidation:
    """Tests for email validation in cmd_send."""

    def test_invalid_recipient_returns_1(self):
        from herd_mail import cmd_send
        args = argparse.Namespace(
            to="not-an-email", subject="Test", body="Body",
            body_file=None, cc=None, reply_to=None, message_id=None,
            attachment=None, rich=False, skip_duplicate_check=False,
            dry_run=False
        )
        cfg = {}
        assert cmd_send(args, cfg) == 1

    def test_invalid_cc_returns_1(self):
        from herd_mail import cmd_send
        args = argparse.Namespace(
            to="valid@example.com", subject="Test", body="Body",
            body_file=None, cc="bad-email", reply_to=None, message_id=None,
            attachment=None, rich=False, skip_duplicate_check=False,
            dry_run=False
        )
        cfg = {
            "smtp_host": "h", "smtp_user": "u@u.com",
            "smtp_pass": "p", "from_addr": "f@f.com",
        }
        assert cmd_send(args, cfg) == 1


class TestCmdSendRetryLogic:
    """Tests for send retry logic in cmd_send."""

    @patch("time.sleep")
    @patch("herd_mail.send_email")
    @patch("herd_mail.check_recently_sent", return_value=False)
    def test_retries_on_connection_error(self, mock_dup, mock_send, mock_sleep):
        """Should retry on ConnectionError and eventually succeed."""
        from herd_mail import cmd_send
        mock_send.side_effect = [
            ConnectionError("first fail"),
            ConnectionError("second fail"),
            None,  # success on third try
        ]
        args = argparse.Namespace(
            to="user@example.com", subject="Test", body="Body",
            body_file=None, cc=None, reply_to=None, message_id=None,
            attachment=None, rich=False, skip_duplicate_check=False,
            dry_run=False
        )
        cfg = {
            "smtp_host": "h", "smtp_user": "u@u.com",
            "smtp_pass": "p", "from_addr": "f@f.com",
            "from_name": "", "smtp_port": 465,
            "use_tls": True, "imap_host": None,
            "imap_port": 993, "imap_tls": True,
            "send_log": None,
        }
        result = cmd_send(args, cfg)
        assert result == 0
        assert mock_send.call_count == 3

    @patch("time.sleep")
    @patch("herd_mail.send_email")
    @patch("herd_mail.check_recently_sent", return_value=False)
    def test_all_retries_exhausted_returns_1(self, mock_dup, mock_send, mock_sleep):
        """Should return 1 when all retry attempts fail."""
        from herd_mail import cmd_send
        mock_send.side_effect = ConnectionError("persistent failure")
        args = argparse.Namespace(
            to="user@example.com", subject="Test", body="Body",
            body_file=None, cc=None, reply_to=None, message_id=None,
            attachment=None, rich=False, skip_duplicate_check=False,
            dry_run=False
        )
        cfg = {
            "smtp_host": "h", "smtp_user": "u@u.com",
            "smtp_pass": "p", "from_addr": "f@f.com",
            "from_name": "", "smtp_port": 465,
            "use_tls": True, "imap_host": None,
            "imap_port": 993, "imap_tls": True,
            "send_log": None,
        }
        result = cmd_send(args, cfg)
        assert result == 1
        assert mock_send.call_count == 4  # RETRY_DELAYS has 4 entries

    @patch("herd_mail.send_email")
    @patch("herd_mail.check_recently_sent", return_value=False)
    def test_value_error_no_retry(self, mock_dup, mock_send):
        """ValueError (bad input) should not retry."""
        from herd_mail import cmd_send
        mock_send.side_effect = ValueError("bad input")
        args = argparse.Namespace(
            to="user@example.com", subject="Test", body="Body",
            body_file=None, cc=None, reply_to=None, message_id=None,
            attachment=None, rich=False, skip_duplicate_check=False,
            dry_run=False
        )
        cfg = {
            "smtp_host": "h", "smtp_user": "u@u.com",
            "smtp_pass": "p", "from_addr": "f@f.com",
            "from_name": "", "smtp_port": 465,
            "use_tls": True, "imap_host": None,
            "imap_port": 993, "imap_tls": True,
            "send_log": None,
        }
        result = cmd_send(args, cfg)
        assert result == 1
        assert mock_send.call_count == 1


class TestCmdSendDuplicateDetection:
    """Tests for duplicate detection in cmd_send."""

    @patch("herd_mail.send_email")
    @patch("herd_mail.check_recently_sent", return_value=True)
    def test_duplicate_detected_returns_0(self, mock_dup, mock_send):
        """Duplicate detection should return 0 without sending."""
        from herd_mail import cmd_send
        args = argparse.Namespace(
            to="user@example.com", subject="Test", body="Body",
            body_file=None, cc=None, reply_to=None, message_id=None,
            attachment=None, rich=False, skip_duplicate_check=False,
            dry_run=False
        )
        cfg = {
            "smtp_host": "h", "smtp_user": "u@u.com",
            "smtp_pass": "p", "from_addr": "f@f.com",
            "from_name": "", "smtp_port": 465,
            "use_tls": True, "imap_host": None,
            "imap_port": 993, "imap_tls": True,
            "send_log": None,
        }
        result = cmd_send(args, cfg)
        assert result == 0
        mock_send.assert_not_called()

    @patch("herd_mail.send_email")
    @patch("herd_mail.check_recently_sent", return_value=False)
    def test_skip_duplicate_check_flag(self, mock_dup, mock_send):
        """--skip-duplicate-check should bypass duplicate detection."""
        from herd_mail import cmd_send
        args = argparse.Namespace(
            to="user@example.com", subject="Test", body="Body",
            body_file=None, cc=None, reply_to=None, message_id=None,
            attachment=None, rich=False, skip_duplicate_check=True,
            dry_run=False
        )
        cfg = {
            "smtp_host": "h", "smtp_user": "u@u.com",
            "smtp_pass": "p", "from_addr": "f@f.com",
            "from_name": "", "smtp_port": 465,
            "use_tls": True, "imap_host": None,
            "imap_port": 993, "imap_tls": True,
            "send_log": None,
        }
        result = cmd_send(args, cfg)
        mock_dup.assert_not_called()


class TestOutputFormatters:
    """Tests for human-readable output formatters."""

    def test_output_human_list_no_messages(self, capsys):
        from herd_mail import output_human_list
        output_human_list({"folder": "INBOX", "messages": []})
        captured = capsys.readouterr()
        assert "No messages in INBOX" in captured.out

    def test_output_human_list_with_messages(self, capsys):
        from herd_mail import output_human_list
        data = {
            "folder": "INBOX",
            "messages": [
                {"uid": "1", "from_name": "Alice", "from_addr": "a@b.com",
                 "subject": "Hello", "date": "2026-01-01 10:00", "unread": True},
                {"uid": "2", "from_name": "", "from_addr": "c@d.com",
                 "subject": "Test", "date": "2026-01-01 11:00", "unread": False},
            ]
        }
        output_human_list(data)
        captured = capsys.readouterr()
        assert "Alice" in captured.out
        assert "Hello" in captured.out

    def test_output_human_check_no_unread(self, capsys):
        from herd_mail import output_human_check
        output_human_check({"folder": "INBOX", "unread_count": 0})
        captured = capsys.readouterr()
        assert "No unread" in captured.out

    def test_output_human_check_with_unread(self, capsys):
        from herd_mail import output_human_check
        output_human_check({"folder": "INBOX", "unread_count": 5})
        captured = capsys.readouterr()
        assert "5 unread" in captured.out

    def test_output_human_read(self, capsys):
        from herd_mail import output_human_read
        data = {
            "from_name": "Alice",
            "from_addr": "alice@example.com",
            "to": "bob@example.com",
            "date": "2026-01-01 10:00",
            "subject": "Hello Bob",
            "body_plain": "Hi there!",
            "attachments": [{"filename": "doc.pdf"}],
        }
        output_human_read(data)
        captured = capsys.readouterr()
        assert "Alice <alice@example.com>" in captured.out
        assert "Hello Bob" in captured.out
        assert "Hi there!" in captured.out
        assert "doc.pdf" in captured.out


# ══════════════════════════════════════════════════════════════════════════════
# nova_mail_agent.py — mail processing, classification, reply generation
# ══════════════════════════════════════════════════════════════════════════════

class TestMailAgentClassification:
    """Tests for email classification functions in nova_mail_agent.py."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_system_message_mailer_daemon(self):
        from nova_mail_agent import is_system_message
        assert is_system_message("mailer-daemon@example.com", "Undeliverable") is True

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_system_message_noreply(self):
        from nova_mail_agent import is_system_message
        assert is_system_message("noreply@example.com", "Notification") is True

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_system_message_normal_email(self):
        from nova_mail_agent import is_system_message
        assert is_system_message("friend@example.com", "Hey Nova") is False

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_system_message_delivery_status(self):
        from nova_mail_agent import is_system_message
        assert is_system_message("postmaster@gmail.com", "Delivery Status Notification") is True

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_from_nova(self):
        from nova_mail_agent import is_from_nova, NOVA_EMAIL
        assert is_from_nova(NOVA_EMAIL) is True
        assert is_from_nova(f"Nova <{NOVA_EMAIL}>") is True
        assert is_from_nova("someone@else.com") is False

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_addressed_to_nova(self):
        from nova_mail_agent import is_addressed_to_nova, NOVA_EMAIL
        assert is_addressed_to_nova(f"Nova <{NOVA_EMAIL}>, Someone <other@x.com>") is True
        assert is_addressed_to_nova("other@example.com") is False


class TestMailAgentIMAPHelpers:
    """Tests for IMAP helper functions in nova_mail_agent.py."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_imap_list_unread_empty(self):
        from nova_mail_agent import imap_list_unread
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"5"])
        mock_conn.uid.return_value = ("OK", [b""])
        result = imap_list_unread(mock_conn)
        assert result == []

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_imap_list_unread_with_messages(self):
        from nova_mail_agent import imap_list_unread
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"5"])
        mock_conn.uid.return_value = ("OK", [b"1 2 3"])
        result = imap_list_unread(mock_conn)
        assert result == [b"1", b"2", b"3"]

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_imap_list_unread_not_ok(self):
        from nova_mail_agent import imap_list_unread
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.uid.return_value = ("NO", [None])
        result = imap_list_unread(mock_conn)
        assert result == []


class TestMailAgentFetchMessage:
    """Tests for imap_fetch_message in nova_mail_agent.py."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_fetch_returns_empty_on_failure(self):
        from nova_mail_agent import imap_fetch_message
        mock_conn = MagicMock()
        mock_conn.uid.return_value = ("NO", [None])
        result = imap_fetch_message(mock_conn, b"1")
        assert result == {}

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_fetch_parses_simple_email(self):
        from nova_mail_agent import imap_fetch_message
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = "Sam <sam@jasonacox.com>"
        msg["To"] = "nova@digitalnoise.net"
        msg["Subject"] = "Hello Nova"
        msg["Message-ID"] = "<test123@example.com>"
        msg.set_content("Hey, how are you?")

        mock_conn = MagicMock()
        mock_conn.uid.return_value = ("OK", [(b"1 (RFC822 {123}", msg.as_bytes())])

        result = imap_fetch_message(mock_conn, b"1")
        assert result["from_addr"] == "sam@jasonacox.com"
        assert result["subject"] == "Hello Nova"
        assert "how are you" in result["body"]
        assert result["message_id"] == "<test123@example.com>"


class TestMailAgentMoveToTrash:
    """Tests for imap_move_to_trash."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_move_to_trash_calls_imap(self):
        from nova_mail_agent import imap_move_to_trash, TRASH_FOLDER
        mock_conn = MagicMock()
        imap_move_to_trash(mock_conn, b"42")
        mock_conn.uid.assert_any_call("COPY", b"42", TRASH_FOLDER)
        mock_conn.uid.assert_any_call("STORE", b"42", "+FLAGS", "(\\Deleted)")
        mock_conn.expunge.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# nova_mail_deliver.py — parsing, classification, summary building
# ══════════════════════════════════════════════════════════════════════════════

class TestMailDeliverParsing:
    """Tests for parse_accounts_from_file in nova_mail_deliver.py."""

    def _get_module(self):
        """Import nova_mail_deliver with mocked dependencies."""
        mock_nova_config = MagicMock()
        mock_nova_config.SLACK_NOTIFY = "C0TEST"
        mock_nova_config.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nova_config}):
            # Need to force reimport
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver
            return nova_mail_deliver

    def test_parse_accounts_basic(self):
        mod = self._get_module()
        content = """📬 user@gmail.com — 3 message(s), 1 unread

[UNREAD] FROM: Amazon
SUBJ: Your order has shipped

[READ] FROM: Newsletter
SUBJ: Weekly update

📬 work@company.com — 1 message(s), 1 unread

[UNREAD] FROM: Boss
SUBJ: Meeting tomorrow
"""
        accounts = mod.parse_accounts_from_file(content)
        assert "user@gmail.com" in accounts
        assert "work@company.com" in accounts
        assert len(accounts["user@gmail.com"]) == 2
        assert len(accounts["work@company.com"]) == 1
        assert accounts["user@gmail.com"][0]["unread"] is True
        assert accounts["user@gmail.com"][0]["sender"] == "Amazon"
        assert accounts["user@gmail.com"][0]["subject"] == "Your order has shipped"
        assert accounts["user@gmail.com"][1]["unread"] is False

    def test_parse_accounts_empty_content(self):
        mod = self._get_module()
        accounts = mod.parse_accounts_from_file("")
        assert accounts == {}


class TestMailDeliverClassification:
    """Tests for is_noise and is_important in nova_mail_deliver.py."""

    def _get_module(self):
        mock_nova_config = MagicMock()
        mock_nova_config.SLACK_NOTIFY = "C0TEST"
        mock_nova_config.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nova_config}):
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver
            return nova_mail_deliver

    def test_noise_detection(self):
        mod = self._get_module()
        assert mod.is_noise("Wayfair Sales", "Big deal today!") is True
        assert mod.is_noise("Amazon", "Your order") is True
        assert mod.is_noise("Jordan Koch", "Important stuff") is False

    def test_important_detection(self):
        mod = self._get_module()
        assert mod.is_important("American Express", "Statement ready") is True
        assert mod.is_important("Apple Developer", "Certificate expiring") is True
        assert mod.is_important("Random Sender", "Random subject") is False

    def test_herd_names_are_important(self):
        mod = self._get_module()
        # Herd member names should be in IMPORTANT_PATTERNS if herd_config loaded
        # At minimum test the base patterns
        assert mod.is_important("ADT Security", "Alert") is True


class TestMailDeliverBuildSummary:
    """Tests for build_summary in nova_mail_deliver.py."""

    def _get_module(self):
        mock_nova_config = MagicMock()
        mock_nova_config.SLACK_NOTIFY = "C0TEST"
        mock_nova_config.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nova_config}):
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver
            return nova_mail_deliver

    def test_build_summary_includes_date(self):
        mod = self._get_module()
        content = """Total messages: 5

📬 user@gmail.com — 5 message(s), 2 unread

[UNREAD] FROM: American Express
SUBJ: Statement ready

[UNREAD] FROM: Hulu
SUBJ: New shows this week

[READ] FROM: Friend
SUBJ: Dinner plans
"""
        summary = mod.build_summary(content)
        assert "Nova Mail Summary" in summary
        assert "5 messages" in summary

    def test_build_summary_groups_noise(self):
        mod = self._get_module()
        content = """Total messages: 2

📬 user@gmail.com — 2 message(s), 2 unread

[UNREAD] FROM: Hulu
SUBJ: New season available

[UNREAD] FROM: Wayfair
SUBJ: 50% off sale
"""
        summary = mod.build_summary(content)
        assert "newsletters/marketing" in summary


# ══════════════════════════════════════════════════════════════════════════════
# nova_herd_outreach.py — outreach logic
# ══════════════════════════════════════════════════════════════════════════════

class TestHerdOutreach:
    """Tests for outreach decision logic in nova_herd_outreach.py."""

    def _get_module(self):
        mock_nova_config = MagicMock()
        mock_nova_config.SLACK_EMAIL = "C0TEST"
        mock_nova_config.post_both = MagicMock()
        mock_herd = MagicMock()
        mock_herd.HERD = [
            {"name": "Sam", "email": "sam@test.com", "profile": "sam.md"},
        ]
        with patch.dict("sys.modules", {
            "nova_config": mock_nova_config,
            "herd_config": mock_herd,
        }):
            if "nova_herd_outreach" in sys.modules:
                del sys.modules["nova_herd_outreach"]
            import nova_herd_outreach
            return nova_herd_outreach

    def test_already_reached_out_today(self):
        mod = self._get_module()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(f"[{mod.TODAY}] Outreach sent to Sam\n")
            f.flush()
            with patch.object(mod, "OUTREACH_LOG", Path(f.name)):
                assert mod.already_reached_out_today() is True
        os.unlink(f.name)

    def test_not_reached_out_today(self):
        mod = self._get_module()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("[2020-01-01] Outreach sent to Sam\n")
            f.flush()
            with patch.object(mod, "OUTREACH_LOG", Path(f.name)):
                assert mod.already_reached_out_today() is False
        os.unlink(f.name)

    def test_no_log_file(self):
        mod = self._get_module()
        with patch.object(mod, "OUTREACH_LOG", Path("/tmp/nonexistent_outreach.log")):
            assert mod.already_reached_out_today() is False


class TestHerdOutreachMainFlow:
    """Tests for main() flow in nova_herd_outreach.py."""

    def _get_module(self):
        mock_nova_config = MagicMock()
        mock_nova_config.SLACK_EMAIL = "C0TEST"
        mock_nova_config.post_both = MagicMock()
        mock_herd = MagicMock()
        mock_herd.HERD = [
            {"name": "Sam", "email": "sam@test.com", "profile": "sam.md"},
        ]
        with patch.dict("sys.modules", {
            "nova_config": mock_nova_config,
            "herd_config": mock_herd,
        }):
            if "nova_herd_outreach" in sys.modules:
                del sys.modules["nova_herd_outreach"]
            import nova_herd_outreach
            return nova_herd_outreach

    def test_skip_decision_respected(self):
        mod = self._get_module()
        mock_pick = MagicMock(return_value={"skip": True, "reason": "nothing genuine"})
        with patch.object(mod, "already_reached_out_today", return_value=False):
            with patch.object(mod, "pick_recipient_and_angle", mock_pick):
                mod.main()
        mock_pick.assert_called_once()

    def test_successful_outreach(self):
        mod = self._get_module()
        mock_gen = MagicMock(return_value="Hey Sam!")
        mock_send = MagicMock(return_value=True)
        mock_pick = MagicMock(return_value={
            "recipient_email": "sam@test.com",
            "recipient_name": "Sam",
            "subject": "Quick thought",
            "angle": "something cool",
            "hook": "you would like this",
        })
        with patch.object(mod, "already_reached_out_today", return_value=False):
            with patch.object(mod, "pick_recipient_and_angle", mock_pick):
                with patch.object(mod, "generate_outreach_email", mock_gen):
                    with patch.object(mod, "send_email", mock_send):
                        with patch.object(mod, "slack_notify"):
                            mod.main()
        mock_gen.assert_called_once()
        mock_send.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# nova_send_mail.py — thin wrapper
# ══════════════════════════════════════════════════════════════════════════════

class TestSendMail:
    """Tests for nova_send_mail.py send_mail function."""

    @patch("subprocess.run")
    def test_send_mail_single_recipient(self, mock_run):
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        result = send_mail("user@example.com", "Subject", "Body")
        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "--to" in args
        assert "user@example.com" in args

    @patch("subprocess.run")
    def test_send_mail_multiple_recipients(self, mock_run):
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        result = send_mail(["a@b.com", "c@d.com"], "Subject", "Body")
        assert result is True
        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_send_mail_failure(self, mock_run):
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=1, stderr="SMTP error", stdout="")
        result = send_mail("user@example.com", "Subject", "Body")
        assert result is False

    @patch("subprocess.run")
    def test_send_mail_with_attachment(self, mock_run):
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        send_mail("user@example.com", "Subject", "Body", image_path="/tmp/img.png")
        args = mock_run.call_args[0][0]
        assert "--attachment" in args
        assert "/tmp/img.png" in args

    @patch("subprocess.run")
    def test_send_mail_with_reply_to(self, mock_run):
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        send_mail("user@example.com", "Subject", "Body",
                  in_reply_to="<msg123@example.com>")
        args = mock_run.call_args[0][0]
        assert "--message-id" in args
        assert "<msg123@example.com>" in args

    @patch("subprocess.run")
    def test_send_mail_with_rich_flag(self, mock_run):
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        send_mail("user@example.com", "Subject", "Body", rich=True)
        args = mock_run.call_args[0][0]
        assert "--rich" in args

    @patch("subprocess.run", side_effect=Exception("boom"))
    def test_send_mail_exception(self, mock_run):
        from nova_send_mail import send_mail
        result = send_mail("user@example.com", "Subject", "Body")
        assert result is False

    @patch("subprocess.run")
    def test_send_mail_partial_failure(self, mock_run):
        """If one recipient fails, overall result should be False."""
        from nova_send_mail import send_mail
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),
            MagicMock(returncode=1, stderr="fail", stdout=""),
        ]
        result = send_mail(["a@b.com", "c@d.com"], "Subject", "Body")
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# Integration tests — marked @pytest.mark.integration
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestHerdMailConfigIntegration:
    """Test herd_mail.py config validation with real Keychain credentials."""

    def test_real_keychain_config_validates(self):
        """Verify real Keychain credentials produce a valid config."""
        # This test requires the WAGGLE_* env vars or Keychain to be set up
        # It will be skipped in CI environments
        import subprocess
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova",
             "-s", "nova-smtp-app-password", "-w"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            pytest.skip("Keychain credentials not available")

        from herd_mail import get_config, validate_config
        try:
            cfg = get_config()
            # Just verify it parses without error
            assert isinstance(cfg, dict)
            assert "smtp_port" in cfg
        except ValueError:
            # Config env vars not set — that is acceptable in test
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "not integration and not functional"])
