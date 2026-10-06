#!/usr/bin/env python3
"""Tests for herd_mail.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hm = _load("herd_mail_t", SCRIPTS / "herd_mail.py")
SRC = (SCRIPTS / "herd_mail.py").read_text()
# Stub every outbound waggle call at load: nothing in this file may reach SMTP/IMAP.
for _fn in ("send_email", "check_recently_sent", "read_message", "list_inbox", "download_attachments", "move_message"):
    setattr(hm, _fn, MagicMock(side_effect=RuntimeError(f"{_fn} not mocked in test")))
hm.imaplib = MagicMock(IMAP4=MagicMock(error=Exception))
hm.logger.disabled = True

CFG = {"smtp_host": "smtp.example.com", "smtp_port": 465, "smtp_user": "bot", "smtp_pass": "pw",
       "from_addr": "bot@example.com", "from_name": "Bot", "use_tls": True, "imap_host": None,
       "imap_port": 993, "imap_tls": True, "send_log": None}


def _send_args(**kw):
    d = dict(dry_run=False, to="peer@example.com", cc=None, reply_to=None, body="hi\\nthere", body_file=None,
             subject="Hello", skip_duplicate_check=True, message_id=None, attachment=None, rich=False)
    d.update(kw)
    return argparse.Namespace(**d)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('os.environ.get("WAGGLE_PASS")', SRC)

    def test_header_injection_rejected(self):
        for bad in ("a@b.com\r\nBcc: x@y.com", "a@b.com\n", "a\0@b.com", "", None, "nodomain@", "x@localhost"):
            self.assertFalse(hm.validate_email_address(bad), repr(bad))
        self.assertFalse(hm.validate_email_list("ok@a.com, bad\n@b.com"))

    def test_forbidden_content_hard_block(self):
        hm.send_email.reset_mock()
        rc = hm.cmd_send(_send_args(body="your Plex viewing summary"), dict(CFG))
        self.assertEqual(rc, 2)
        hm.send_email.assert_not_called()
        self.assertEqual(hm._forbidden_email_content("ok", "nice weather"), "")

    def test_sensitive_paths_and_ansi_stripped(self):
        self.assertIsNone(hm.validate_file_path("/etc/passwd"))
        self.assertEqual(hm.sanitize_for_display("\x1b[31mred\x1b[0m\x07"), "red")


class TestPerformance(unittest.TestCase):
    def test_validation_and_sanitize_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            hm.validate_email_address(f"user{i}@example.com")
            hm.sanitize_for_display("\x1b[1mx\x1b[0m" * 5)
            hm._forbidden_email_content("subj", "body text " * 10)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_send_retries_transient_then_succeeds(self):
        send = MagicMock(side_effect=[ConnectionError("x"), TimeoutError("y"), None])
        with patch.object(hm, "send_email", send), patch("time.sleep") as sl:
            self.assertEqual(hm.cmd_send(_send_args(), dict(CFG)), 0)
        self.assertEqual(send.call_count, 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [5, 15])

    def test_send_gives_up_after_four_and_value_error_not_retried(self):
        send = MagicMock(side_effect=OSError("down"))
        with patch.object(hm, "send_email", send), patch("time.sleep"):
            self.assertEqual(hm.cmd_send(_send_args(), dict(CFG)), 1)
        self.assertEqual(send.call_count, 4)
        send = MagicMock(side_effect=ValueError("bad"))
        with patch.object(hm, "send_email", send), patch("time.sleep"):
            self.assertEqual(hm.cmd_send(_send_args(), dict(CFG)), 1)
        self.assertEqual(send.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_decode_escape_sequences(self):
        self.assertEqual(hm.decode_escape_sequences("a\\nb\\tc"), "a\nb\tc")
        self.assertEqual(hm.decode_escape_sequences(""), "")

    def test_parse_port_edges(self):
        self.assertEqual(hm.parse_port("587", 465), 587)
        for bad in ("0", "70000", "abc", None):
            with self.assertRaises(ValueError):
                hm.parse_port(bad, 465)

    def test_validate_config_and_sanitize_truncation(self):
        self.assertTrue(hm.validate_config(dict(CFG)))
        self.assertFalse(hm.validate_config({**CFG, "smtp_pass": None}))
        self.assertFalse(hm.validate_config(dict(CFG), require_smtp=False, require_imap=True))
        self.assertEqual(hm.sanitize_for_display("x" * 300, 10), "x" * 10 + "...")
        self.assertEqual(hm.sanitize_for_display(""), "")


class TestIntegration(unittest.TestCase):
    def test_get_config_feeds_build_waggle_config(self):
        env = {"WAGGLE_HOST": "h", "WAGGLE_USER": "u", "WAGGLE_PASS": "p", "WAGGLE_FROM": "f@x.com",
               "WAGGLE_PORT": "587", "WAGGLE_IMAP_HOST": "imap.x", "WAGGLE_TLS": "false"}
        with patch.dict(os.environ, env):
            w = hm.build_waggle_config(hm.get_config())
        self.assertEqual((w["host"], w["port"], w["password"], w["tls"], w["imap_host"]),
                         ("h", 587, "p", False, "imap.x"))

    def test_save_to_sent_appends_to_found_folder(self):
        conn = MagicMock()
        conn.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Sent) "/" "Sent"'])
        conn.append.return_value = ("OK", None)
        with patch.object(hm.imaplib, "IMAP4_SSL", return_value=conn):
            self.assertTrue(hm.save_to_sent({**CFG, "imap_host": "imap.x"}, "a@b.com", "s", "b"))
        self.assertEqual(conn.append.call_args.args[0], '"Sent"')
        conn.logout.assert_called_once()


class TestFunctional(unittest.TestCase):
    def test_send_golden_path_passes_decoded_body(self):
        send = MagicMock(return_value=None)
        with patch.object(hm, "send_email", send):
            self.assertEqual(hm.cmd_send(_send_args(), dict(CFG)), 0)
        kw = send.call_args.kwargs
        self.assertEqual((kw["to"], kw["subject"], kw["body_md"]), ("peer@example.com", "Hello", "hi\nthere"))
        self.assertEqual(kw["config"]["from_addr"], "bot@example.com")

    def test_duplicate_suppresses_send(self):
        send = MagicMock()
        with patch.object(hm, "send_email", send), patch.object(hm, "check_recently_sent", return_value=True):
            self.assertEqual(hm.cmd_send(_send_args(skip_duplicate_check=False), dict(CFG)), 0)
        send.assert_not_called()

    def test_check_exit_codes_and_json(self):
        a = argparse.Namespace(folder="INBOX", human=False)
        cfg = {**CFG, "imap_host": "imap.x"}
        buf = io.StringIO()
        with patch.object(hm, "list_inbox", return_value=[{"uid": 1, "unread": True}, {"uid": 2}]), redirect_stdout(buf):
            self.assertEqual(hm.cmd_check(a, cfg), 0)
        self.assertEqual(json.loads(buf.getvalue())["unread_count"], 1)
        with patch.object(hm, "list_inbox", side_effect=OSError("imap down")):
            self.assertEqual(hm.cmd_check(a, cfg), 2)


class TestFrame(unittest.TestCase):
    def test_help_exits_cleanly(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "herd_mail.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0 if hm.WAGGLE_AVAILABLE else 1, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import herd_mail"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
