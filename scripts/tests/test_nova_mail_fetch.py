#!/usr/bin/env python3
"""Tests for nova_mail_fetch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

No mailbox is ever touched: the osascript call is mocked and every input is a fixture string."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_mail_fetch.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="mail-fetch-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("mail_fetch_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(subprocess, "run", side_effect=AssertionError("osascript at import")):
        spec.loader.exec_module(mod)
    mod.OUT_FILE = TMP / "nova_mail_fetch.txt"          # never write the real workspace state file
    return mod


mf = _load()

FIXTURE = """TOTAL:3
=== ACCOUNT: Digitalnoise Gmail <nova@digitalnoise.net> (2 messages) ===
FROM: Sam <sam@example.org> [UNREAD]
SUBJECT: Re: the herd thread
DATE: Mon Oct 5 09:00
BODY: Here is a long body that should be truncated to two hundred characters """ + "z" * 300 + """
FROM: Postmaster <postmaster@example.org>
SUBJECT: bounce
DATE: Mon Oct 5 08:00
=== ACCOUNT: Legacy Box (1 messages) ===
FROM: Dylan <dylan@example.org> [UNREAD]
SUBJECT: hi
DATE: Sun
BODY: short
"""


def _run(stdout="", rc=0, stderr=""):
    run = MagicMock(return_value=types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr))
    mf.OUT_FILE.unlink(missing_ok=True)
    with patch.object(subprocess, "run", run), redirect_stdout(io.StringIO()) as out:
        try:
            mf.main(); code = 0
        except SystemExit as e:
            code = e.code
    return code, run, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("imaplib", SRC)                      # reads only via the Apple Mail applescript

    def test_mail_is_read_only_through_the_fixed_applescript_argv(self):
        code, run, _ = _run("NO_MAIL")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["osascript", str(mf.SCRIPTS / "nova_mail_summary.applescript")])
        self.assertEqual(run.call_args[1]["timeout"], 120)
        self.assertNotIn("Work", (SCRIPTS / "nova_mail_summary.applescript").read_text().split("\n")[0])

    def test_bodies_are_truncated_in_the_parsed_record(self):
        accounts = mf.parse_messages(FIXTURE)
        self.assertEqual(len(accounts["nova@digitalnoise.net"][0]["body"]), 200)


class TestPerformance(unittest.TestCase):
    def test_parse_10k_messages_fast(self):
        raw = "TOTAL:10000\n=== ACCOUNT: Big <big@example.org> (10000 messages) ===\n" + "".join(
            f"FROM: p{i} <p{i}@example.org> [UNREAD]\nSUBJECT: s{i}\nDATE: d\nBODY: b{i}\n" for i in range(10_000))
        t0 = time.perf_counter()
        acc = mf.parse_messages(raw)
        txt = mf.format_for_nova(acc, 10_000)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(acc["big@example.org"]), 10_000)
        self.assertIn("10000 message(s), 10000 unread", txt)


class TestRetry(unittest.TestCase):
    def test_applescript_failure_is_one_shot_and_recorded(self):
        # RETRY GAP: run_applescript — a single osascript run; rc!=0 writes an ERROR file and exits 1
        code, run, out = _run(rc=1, stderr="Mail got an error: timed out")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(code, 1)
        self.assertEqual(mf.OUT_FILE.read_text(), "ERROR running mail summary: Mail got an error: timed out")
        self.assertIn("ERROR", out)


class TestUnit(unittest.TestCase):
    def test_parse_new_and_legacy_account_headers(self):
        acc = mf.parse_messages(FIXTURE)
        self.assertEqual(list(acc), ["nova@digitalnoise.net", "Legacy Box"])
        self.assertEqual(acc["nova@digitalnoise.net"][0]["from"], "Sam <sam@example.org> [UNREAD]")
        self.assertTrue(acc["nova@digitalnoise.net"][0]["unread"])
        self.assertFalse(acc["nova@digitalnoise.net"][1]["unread"])
        self.assertEqual(acc["nova@digitalnoise.net"][1]["body"], "")
        self.assertEqual(acc["Legacy Box"][0]["subject"], "hi")

    def test_parse_ignores_fields_before_any_account_and_merges_dupes(self):
        self.assertEqual(mf.parse_messages("FROM: x\nSUBJECT: y\n"), {})
        self.assertEqual(mf.parse_messages(""), {})
        raw = ("=== ACCOUNT: A <a@x.org> (1 messages) ===\nFROM: p\n"
               "=== ACCOUNT: A dup <A@X.ORG> (1 messages) ===\nFROM: q\n")
        self.assertEqual([m["from"] for m in mf.parse_messages(raw)["a@x.org"]], ["p", "q"])

    def test_format_skips_empty_accounts_and_orders_unread_first(self):
        acc = {"empty@x": [], "a@x": [{"from": "r", "subject": "s1", "date": "d", "body": "", "unread": False},
                                     {"from": "u", "subject": "s2", "date": "d", "body": "hello", "unread": True}]}
        txt = mf.format_for_nova(acc, 2)
        self.assertNotIn("empty@x", txt)
        self.assertLess(txt.index("[UNREAD] FROM: u"), txt.index("[READ]   FROM: r"))
        self.assertIn("BODY: hello...", txt)
        self.assertTrue(txt.startswith("MAIL SUMMARY — ")); self.assertTrue(txt.endswith("END OF MAIL SUMMARY"))


class TestIntegration(unittest.TestCase):
    def test_main_chains_parse_and_format_into_the_summary_file(self):
        code, run, out = _run(FIXTURE)
        text = mf.OUT_FILE.read_text(encoding="utf-8")
        self.assertIn("Total messages (last 24 hours): 3", text)
        self.assertIn("nova@digitalnoise.net — 2 message(s), 1 unread", text)
        self.assertIn("Legacy Box — 1 message(s), 1 unread", text)
        self.assertIn(f"SUMMARY_FILE: {mf.OUT_FILE}", out)

    def test_run_applescript_contract(self):
        with patch.object(subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=" ok \n", stderr="")):
            self.assertEqual(mf.run_applescript(), ("ok", None))
        with patch.object(subprocess, "run", return_value=types.SimpleNamespace(returncode=2, stdout="", stderr=" boom ")):
            self.assertEqual(mf.run_applescript(), (None, "boom"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_formatted_summary(self):
        code, run, out = _run(FIXTURE)
        self.assertEqual(code, 0)
        self.assertIn("[nova_mail_fetch] Done. 3 messages written to", out)
        self.assertIn("[UNREAD] FROM: Sam <sam@example.org> [UNREAD]", mf.OUT_FILE.read_text())

    def test_no_mail_path(self):
        code, run, out = _run("NO_MAIL")
        self.assertEqual(code, 0)
        self.assertEqual(mf.OUT_FILE.read_text(), "NO_MAIL: No messages in the last 24 hours across all accounts.")
        self.assertIn("No mail found", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        boot = ("import sys, unittest.mock as um, subprocess, runpy; "
                "subprocess.run = um.MagicMock(side_effect=AssertionError('osascript at import')); "
                "runpy.run_path(sys.argv[1], run_name='imported'); print('IMPORT_OK')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK")


if __name__ == "__main__":
    unittest.main()
