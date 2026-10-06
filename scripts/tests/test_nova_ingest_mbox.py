#!/usr/bin/env python3
"""Tests for nova_ingest_mbox.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Every mailbox in this file is built from fixture bytes inside a tempdir. No real mbox, no Apple Mail
"Work" mailbox, nothing under ~/Library/Mail is ever opened."""
import importlib.util
import io
import json
import mailbox
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ingest_mbox.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="ingest-mbox-test-"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


im = _load("ingest_mbox_under_test", SCRIPT)
# Module-level stub: the memory server is never reachable from this file.
im.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=im.urllib.request.Request,
                                                                urlopen=MagicMock(side_effect=OSError("offline: urlopen stubbed"))),
                                  error=im.urllib.error)


def _msg(subject="Hello", body="plain body", sender="alice@example.com", date="Mon, 05 Oct 2026 10:00:00 +0000", html=None):
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"], m["To"] = sender, subject, date, "bob@example.com"
    m.set_content(body)
    if html is not None:
        m.add_alternative(html, subtype="html")
    return m


def _mbox(dirname, msgs, folder="INBOX"):
    """Create <TMP>/<dirname>/<folder>.mbox/mbox from fixture messages; returns the mbox file path."""
    d = TMP / dirname / f"{folder}.mbox"; d.mkdir(parents=True, exist_ok=True)
    path = d / "mbox"
    box = mailbox.mbox(str(path))
    for m in msgs:
        box.add(m)
    box.flush(); box.close()
    return path


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _mem(ids=("m1",)):
    seq = list(ids)
    return MagicMock(side_effect=lambda req, timeout=None: _Resp({"id": seq.pop(0) if len(seq) > 1 else seq[0]}))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_no_real_mailbox_paths_in_source_or_tests(self):
        for banned in ("Library/Mail", "/Work", "osascript", "Mail.app"):
            self.assertNotIn(banned, SRC)
        self.assertTrue(str(TMP).startswith(tempfile.gettempdir()))

    def test_pii_is_redacted_before_storage(self):
        body = "call 818-555-1234 or 818.555.9876, ssn 123-45-6789, mail me at carol@example.org, feeling horny"
        out = im._redact_body(body)
        self.assertEqual(out, "call [PHONE] or [PHONE], ssn [SSN], mail me at [EMAIL], feeling [REDACTED]")
        parsed = im.parse_email(_msg(body=body))
        self.assertNotIn("818-555", parsed["body"]); self.assertNotIn("carol@", parsed["body"])

    def test_explicit_content_is_skipped_entirely(self):
        self.assertTrue(im._is_sensitive("Check this adult site", ""))
        self.assertTrue(im._is_sensitive("", "see https://example.com/xxx-vid"))
        self.assertFalse(im._is_sensitive("Invoice", "please pay"))
        self.assertIsNone(im.parse_email(_msg(subject="porn")))
        with patch.object(im.urllib.request, "urlopen", _mem()) as uo, redirect_stdout(io.StringIO()):
            n = im.ingest_mbox_file(_mbox("sec", [_msg(subject="porn"), _msg(subject="ok")]), "f")
        self.assertEqual(n, 1)
        self.assertEqual(uo.call_count, 1)                          # the sensitive one never reached the wire


class TestPerformance(unittest.TestCase):
    def test_redaction_fast_on_10k_bodies(self):
        bodies = [f"Hi {i}, call 818-555-{i % 10000:04d} or write to person{i}@example.com — lustful nonsense" for i in range(10_000)]
        t0 = time.perf_counter()
        for b in bodies:
            im._is_sensitive("subj", b); im._redact_body(b)
        self.assertLess(time.perf_counter() - t0, 4.0)

    def test_ingest_scales_linearly_with_one_post_per_mail(self):
        path = _mbox("perf", [_msg(subject=f"s{i}") for i in range(300)])
        with patch.object(im.urllib.request, "urlopen", _mem()) as uo, redirect_stdout(io.StringIO()):
            t0 = time.perf_counter()
            n = im.ingest_mbox_file(path, "f")
            self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual((n, uo.call_count), (300, 300))


class TestRetry(unittest.TestCase):
    def test_remember_fails_open_to_none(self):
        # RETRY GAP: remember — one POST to the memory server; any failure logs and returns None
        uo = MagicMock(side_effect=OSError("memory down"))
        with patch.object(im.urllib.request, "urlopen", uo), redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(im.remember("t"))
        self.assertEqual(uo.call_count, 1)
        self.assertIn("Error storing memory: memory down", out.getvalue())

    def test_store_failures_do_not_abort_the_mailbox(self):
        # RETRY GAP: ingest_mbox_file — a failed store just isn't counted; the loop continues to the next mail
        path = _mbox("retry", [_msg(subject="a"), _msg(subject="b"), _msg(subject="c")])
        uo = MagicMock(side_effect=[OSError("x"), _Resp({"id": "m2"}), OSError("y")])
        with patch.object(im.urllib.request, "urlopen", uo), redirect_stdout(io.StringIO()):
            self.assertEqual(im.ingest_mbox_file(path, "f"), 1)
        self.assertEqual(uo.call_count, 3)

    def test_unreadable_mbox_fails_open_to_zero(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(im.ingest_mbox_file(TMP / "does-not-exist" / "mbox", "f"), 0)
        self.assertIn("Error processing", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_parse_email_plain_multipart_and_truncation(self):
        p = im.parse_email(_msg(subject="S", body="x" * 900, html="<b>hi</b>"), "folder")
        self.assertEqual((p["sender"], p["subject"], p["folder"]), ("alice@example.com", "S", "folder"))
        self.assertEqual(p["date"], "2026-10-05T10:00:00+00:00")
        self.assertEqual(len(p["body"]), 500)
        p = im.parse_email(_msg(body="plain only"))
        self.assertEqual(p["body"].strip(), "plain only")

    def test_parse_email_bad_date_and_missing_headers(self):
        p = im.parse_email(_msg(date="not a date"))
        self.assertEqual(p["date"], "not a date")
        m = EmailMessage(); m.set_content("body")
        p = im.parse_email(m)
        self.assertEqual((p["sender"], p["subject"], p["date"], p["folder"]), ("unknown", "(no subject)", "unknown", "unknown"))

    def test_remember_posts_expected_payload(self):
        uo = _mem(("abc",))
        with patch.object(im.urllib.request, "urlopen", uo):
            self.assertEqual(im.remember("text", source="email_archive", metadata={"k": "v"}), "abc")
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, im.VECTOR_URL)
        self.assertEqual(json.loads(req.data), {"text": "text", "source": "email_archive", "metadata": {"k": "v"}})
        with patch.object(im.urllib.request, "urlopen", MagicMock(return_value=_Resp({}))):
            self.assertEqual(im.remember("t"), "unknown")

    def test_log_prefixes_timestamp(self):
        with redirect_stdout(io.StringIO()) as out:
            im.log("hi")
        self.assertRegex(out.getvalue(), r"^\[\d{4}-\d\d-\d\dT[\d:.]+\] hi\n$")


class TestIntegration(unittest.TestCase):
    def test_ingest_composes_text_and_metadata_from_parse_email(self):
        path = _mbox("integ", [_msg(subject="Lunch?", body="noon at 818-555-0000")], folder="Personal")
        with patch.object(im.urllib.request, "urlopen", _mem(("id1",))) as uo, redirect_stdout(io.StringIO()):
            self.assertEqual(im.ingest_mbox_file(path), 1)
        payload = json.loads(uo.call_args[0][0].data)
        self.assertTrue(payload["text"].startswith("Email from alice@example.com (2026-10-05T10:00:00+00:00): Lunch?\n\nnoon at [PHONE]"))
        self.assertEqual(payload["source"], "email_archive")
        self.assertEqual(payload["metadata"], {"sender": "alice@example.com", "subject": "Lunch?", "date": "2026-10-05T10:00:00+00:00",
                                               "folder": "Personal.mbox", "mbox_file": "mbox"})   # default folder = parent dir name

    def test_folder_name_from_main_is_the_account_dir(self):
        _mbox("acct", [_msg(subject="x")], folder="Sent")
        with patch.object(im.urllib.request, "urlopen", _mem()) as uo, patch.object(sys, "argv", ["x", str(TMP / "acct")]), redirect_stdout(io.StringIO()):
            self.assertEqual(im.main(), 0)
        self.assertEqual(json.loads(uo.call_args[0][0].data)["metadata"]["folder"], "acct")


class TestFunctional(unittest.TestCase):
    def test_golden_path_ingests_every_mbox_under_the_directory(self):
        root = TMP / "golden"
        _mbox("golden", [_msg(subject="a"), _msg(subject="b")], folder="INBOX")
        _mbox("golden/sub", [_msg(subject="c")], folder="Archive")
        with patch.object(im.urllib.request, "urlopen", _mem()) as uo, patch.object(sys, "argv", ["nova_ingest_mbox.py", str(root)]), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(im.main(), 0)
        self.assertEqual(uo.call_count, 3)
        self.assertIn("Found 2 mbox file(s)", out.getvalue())
        self.assertIn("COMPLETE: 3 emails ingested into vector memory", out.getvalue())
        self.assertTrue(all(str(root) not in json.loads(c[0][0].data)["text"] for c in uo.call_args_list))

    def test_error_paths_exit_1_before_any_network(self):
        uo = MagicMock()
        with patch.object(im.urllib.request, "urlopen", uo), redirect_stdout(io.StringIO()):
            for argv in (["x"], ["x", str(TMP / "nope")], ["x", str(TMP)]):          # no arg / missing dir / no mbox at top level
                with patch.object(sys, "argv", argv):
                    with self.assertRaises(SystemExit) as cm:
                        im.main()
                    self.assertEqual(cm.exception.code, 1)
        uo.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_usage_exit_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage: python3 nova_ingest_mbox.py", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ingest_mbox as m; print('IMPORT-OK', m.VECTOR_URL.endswith('/remember'))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT-OK True")


if __name__ == "__main__":
    unittest.main()
