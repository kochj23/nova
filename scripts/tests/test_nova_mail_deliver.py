#!/usr/bin/env python3
"""Tests for nova_mail_deliver.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
No real mailbox is read: the nova_mail_fetch.py subprocess is mocked and the summary file is a
synthetic one in a tempdir. nova_notify and the memory server are mocked at module load."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_mail_deliver.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="maildeliver-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("mail_deliver_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


md = _load()
# module-level stubs: no fetch subprocess, no notification bus, no memory server, no SMTP
md.SUMMARY_FILE = TMP / "nova_mail_fetch.txt"
md.nova_notify = MagicMock()
md.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: fetch stubbed")))
md.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
    Request=md.urllib.request.Request, urlopen=MagicMock(side_effect=OSError("offline"))))
md.log = MagicMock()

SAMPLE = """Total messages: 5
📬 alpha@example.test — 4 message(s), 3 unread
[UNREAD] FROM: American Express <x@amex.test>
SUBJ: Your statement is ready
[UNREAD] FROM: Wayfair <deals@wayfair.test>
SUBJ: 70% off sofas
[UNREAD] FROM: Pal <pal@example.test>
SUBJ: Lunch Friday?
[READ] FROM: Someone <s@example.test>
SUBJ: old thread
📬 beta@example.test — 1 message(s), 0 unread
[READ] FROM: Bot <b@example.test>
SUBJ:
"""


def _fetch_ok():
    return MagicMock(return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_digest_goes_to_personal_not_work_address(self):
        self.assertNotIn("JORDAN_WORK_EMAIL)", SRC)
        sm = types.ModuleType("nova_send_mail"); sm.send_mail = MagicMock()
        cfg = types.SimpleNamespace(JORDAN_EMAIL="personal@example.test")
        with patch.dict(sys.modules, {"nova_send_mail": sm}), patch.object(md, "nova_config", cfg):
            md.send_email("s", "b")
        self.assertEqual(sm.send_mail.call_args[0][0], "personal@example.test")

    def test_fetch_is_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_parse_and_summarize_10k_messages(self):
        block = "".join(f"[UNREAD] FROM: Person {i} <p{i}@x.test>\nSUBJ: subject {i}\n" for i in range(10_000))
        content = "Total messages: 10000\n📬 big@example.test — 10000 message(s)\n" + block
        t0 = time.perf_counter()
        acc = md.parse_accounts_from_file(content)
        s = md.build_summary(content)
        self.assertLess(time.perf_counter() - t0, 10.0)
        self.assertEqual(len(acc["big@example.test"]), 10_000)
        self.assertIn("+9994 more", s)                         # unread list capped at 6


class TestRetry(unittest.TestCase):
    def test_vector_remember_fails_silently(self):
        # RETRY GAP: vector_remember — one POST, failure swallowed (summary already posted)
        md.urllib.request.urlopen.reset_mock()
        self.assertIsNone(md.vector_remember("t", {"a": 1}))
        self.assertEqual(md.urllib.request.urlopen.call_count, 1)

    def test_fetch_failure_exits_without_posting(self):
        # RETRY GAP: main()/nova_mail_fetch.py — one attempt; non-zero exit -> sys.exit(1), nothing posted
        md.nova_notify.reset_mock()
        bad = MagicMock(return_value=types.SimpleNamespace(returncode=2, stdout="", stderr="imap down"))
        with patch.object(md.subprocess, "run", bad), self.assertRaises(SystemExit):
            md.main()
        md.nova_notify.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_noise_and_important(self):
        self.assertTrue(md.is_noise("Wayfair <x>", "sale"))
        self.assertFalse(md.is_noise("Pal", "Lunch"))
        self.assertTrue(md.is_important("AMEX", ""))
        self.assertFalse(md.is_important("", ""))

    def test_parse_accounts(self):
        acc = md.parse_accounts_from_file(SAMPLE)
        self.assertEqual(sorted(acc), ["alpha@example.test", "beta@example.test"])
        self.assertEqual(acc["alpha@example.test"][0],
                         {"sender": "American Express <x@amex.test>", "subject": "Your statement is ready", "unread": True})
        self.assertFalse(acc["beta@example.test"][0]["unread"])
        self.assertEqual(md.parse_accounts_from_file(""), {})

    def test_build_summary_sections(self):
        s = md.build_summary(SAMPLE)
        self.assertIn("📬 5 messages · 3 unread across 2 addresses", s)
        self.assertIn("Your statement is ready", s)
        self.assertIn("Lunch Friday?", s)
        self.assertIn("1 newsletters/marketing", s)
        self.assertNotIn("70% off sofas", s)


class TestIntegration(unittest.TestCase):
    def test_slack_post_goes_through_notify_bus_deduped(self):
        md.nova_notify.reset_mock()
        md.slack_post("*Title here*\nline 1\nline 2")
        args, kw = md.nova_notify.call_args
        self.assertEqual(args[0], "Title here")
        self.assertEqual(kw["body"], "line 1\nline 2")
        self.assertEqual((kw["category"], kw["dedup_key"]), ("email", "mail-summary-digest"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_and_remembers_only_relevant(self):
        md.SUMMARY_FILE.write_text(SAMPLE, encoding="utf-8")
        md.nova_notify.reset_mock()
        with patch.object(md.subprocess, "run", _fetch_ok()), patch.object(md, "vector_remember") as vr:
            md.main()
        self.assertTrue(md.nova_notify.call_args[0][0].startswith("Nova Mail Summary"))
        stored = {(c.args[0].split(": ", 1)[1], c.args[1]["priority"]) for c in vr.call_args_list}
        self.assertEqual(stored, {("Your statement is ready", "high"), ("Lunch Friday?", "normal")})

    def test_no_mail_posts_empty_summary(self):
        md.SUMMARY_FILE.write_text("NO_MAIL", encoding="utf-8")
        md.nova_notify.reset_mock()
        with patch.object(md.subprocess, "run", _fetch_ok()), patch.object(md, "vector_remember") as vr:
            md.main()
        self.assertIn("No new mail", md.nova_notify.call_args[1]["body"])
        vr.assert_not_called()

    def test_missing_summary_file_exits(self):
        if md.SUMMARY_FILE.exists():
            md.SUMMARY_FILE.unlink()
        with patch.object(md.subprocess, "run", _fetch_ok()), self.assertRaises(SystemExit):
            md.main()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: running the script fetches the real mailboxes, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_mail_deliver as m; print(callable(m.main))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
