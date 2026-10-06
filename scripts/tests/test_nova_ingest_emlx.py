#!/usr/bin/env python3
"""Tests for nova_ingest_emlx.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Never touches a real mailbox: BASE_DIR is pointed at a tempdir of synthetic .emlx files."""
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
    with patch.object(sys, "argv", ["nova_ingest_emlx.py"]):      # BASE_DIR reads argv[1] at import
        spec.loader.exec_module(mod)
    return mod


em = _load("nova_ingest_emlx_t", SCRIPTS / "nova_ingest_emlx.py")
_TMP = Path(tempfile.mkdtemp())
em.BASE_DIR = _TMP / "V10"
SRC = (SCRIPTS / "nova_ingest_emlx.py").read_text()
SENDER = "alice" + "@" + "example.com"


def _emlx(path, subject="Hello", body="Lunch on Friday?", sender=SENDER):
    raw = (f"From: {sender}\r\nSubject: {subject}\r\nDate: Mon, 01 Jun 2026 10:00:00 -0700\r\n"
           f"Content-Type: text/plain\r\n\r\n{body}\r\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(str(len(raw)).encode() + b"\n" + raw + b"<?xml version='1.0'?><plist/>")
    return path


class _Resp:
    def __init__(self, obj):
        self.obj = obj

    def read(self):
        return json.dumps(self.obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_work_and_trash_folders_never_ingested(self):
        for p in ("Work.mbox/x.emlx", "Work - 2022.mbox/a/b.emlx", "Acct/Trash.mbox/x.emlx", "Junk.mbox/1.emlx"):
            self.assertTrue(em.is_skip_folder(Path(p)), p)
        self.assertFalse(em.is_skip_folder(Path("Acct/INBOX.mbox/Messages/1.emlx")))
        self.assertFalse(em.is_skip_folder(Path("Workshop.mbox/1.emlx")))

    def test_pii_redacted_and_explicit_skipped(self):
        skip, body = em._pii_filter("hi", f"call 818-555-1234, ssn 123-45-6789, mail {SENDER}")
        self.assertFalse(skip)
        self.assertEqual(body, "call [PHONE], ssn [SSN], mail [EMAIL]")
        self.assertTrue(em._pii_filter("xxx deals", "body")[0])


class TestPerformance(unittest.TestCase):
    def test_pii_filter_10k_bodies(self):
        body = "Meeting notes about the quarterly roadmap and staffing. " * 10
        t0 = time.perf_counter()
        for _ in range(10_000):
            em._pii_filter("Subject", body)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(em.BASE_DIR, ignore_errors=True)
        _emlx(em.BASE_DIR / "A/INBOX.mbox/Messages/1.emlx")

    def test_store_retried_once_after_failure(self):
        st = MagicMock(side_effect=[False, True])
        with patch.object(em, "store", st), patch.object(em.time, "sleep") as sl, redirect_stdout(io.StringIO()) as out:
            em.main()
        self.assertEqual(st.call_count, 2)
        sl.assert_called_once_with(0.5)
        self.assertIn("Queued for ingest: 1", out.getvalue())

    def test_two_failures_count_as_error(self):
        with patch.object(em, "store", MagicMock(return_value=False)), patch.object(em.time, "sleep"), \
                redirect_stdout(io.StringIO()) as out:
            em.main()
        self.assertIn("Errors:            1", out.getvalue())

    def test_store_network_error_fails_open(self):
        with patch.object(em.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertFalse(em.store({"text": "t"}))


class TestUnit(unittest.TestCase):
    def test_parse_emlx_fields(self):
        p = _emlx(_TMP / "u/Personal.mbox/Messages/9.emlx", body=f"ring 818.555.1234 or {SENDER}")
        d = em.parse_emlx(p)
        self.assertEqual(d["metadata"]["folder"], "Personal")
        self.assertEqual(d["metadata"]["subject"], "Hello")
        self.assertTrue(d["metadata"]["date"].startswith("2026-06-01T10:00:00"))
        self.assertIn("[PHONE]", d["text"])
        self.assertNotIn("818.555.1234", d["text"])

    def test_parse_emlx_bad_inputs(self):
        (_TMP / "u").mkdir(exist_ok=True)
        one = _TMP / "u/one.emlx"; one.write_bytes(b"onlyoneline")
        self.assertIsNone(em.parse_emlx(one))
        self.assertIsNone(em.parse_emlx(_TMP / "u/missing.emlx"))
        p = _emlx(_TMP / "u/x.mbox/2.emlx", subject="porn offer", body="x")
        self.assertIsNone(em.parse_emlx(p))


class TestIntegration(unittest.TestCase):
    def test_store_posts_to_async_remember(self):
        seen = {}

        def uo(req, timeout=None):
            seen["url"] = req.full_url; seen["body"] = json.loads(req.data)
            return _Resp({"status": "queued"})
        payload = em.parse_emlx(_emlx(_TMP / "i/INBOX.mbox/3.emlx"))
        with patch.object(em.urllib.request, "urlopen", side_effect=uo):
            self.assertTrue(em.store(payload))
        self.assertTrue(seen["url"].endswith("/remember?async=1"))
        self.assertEqual(seen["body"]["source"], "email_archive")
        with patch.object(em.urllib.request, "urlopen", return_value=_Resp({"status": "error"})):
            self.assertFalse(em.store(payload))


class TestFunctional(unittest.TestCase):
    def test_main_scans_skips_work_and_queues_rest(self):
        import shutil
        shutil.rmtree(em.BASE_DIR, ignore_errors=True)
        _emlx(em.BASE_DIR / "A/INBOX.mbox/Messages/1.emlx")
        _emlx(em.BASE_DIR / "A/INBOX.mbox/Messages/2.emlx", subject="", body="")
        _emlx(em.BASE_DIR / "A/Work.mbox/Messages/3.emlx")
        _emlx(em.BASE_DIR / "A/Trash.mbox/Messages/4.emlx")
        sent = []
        with patch.object(em, "store", side_effect=lambda p: (sent.append(p), True)[1]), \
                redirect_stdout(io.StringIO()) as out:
            em.main()
        self.assertIn("Found 2 emlx files", out.getvalue())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["metadata"]["folder"], "INBOX")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ingest_emlx"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
