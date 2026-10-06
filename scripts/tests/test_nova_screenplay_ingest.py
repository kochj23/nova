#!/usr/bin/env python3
"""Tests for nova_screenplay_ingest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

fetch()/urlopen, the nova_ingest.py subprocess, pdftotext and the Scribd browser are mocked; STAGING is a
tempdir."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_screenplay_ingest.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


si = _load("nova_screenplay_ingest_t", SCRIPT)
DIALOG = "\n\n".join(f"ANNIE\n\nYou are my number one fan and this is line {i} of the scene."
                     for i in range(150))
PAGE = (f'<html><head><title>Misery Script at IMSDb.</title><script>var x=1;</script></head>'
        f'<body><pre>{DIALOG}</pre></body></html>')


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        for p in (patch.object(si, "STAGING", Path(self.td.name) / "staging"),
                  patch.object(si.urllib.request, "urlopen", boom), patch("subprocess.run", boom)):
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, argv, page=PAGE, ingest=None):
        ingest = ingest or MagicMock(return_value=_cp(0, "stored 40 chunks\n"))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["x", *argv]), patch.object(si, "fetch", return_value=page.encode()), \
                patch.object(si.subprocess, "run", ingest), redirect_stdout(out), redirect_stderr(err):
            rc = si.main()
        return rc, out.getvalue(), err.getvalue(), ingest


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_scripts_and_markup_stripped(self):
        title, body = si.extract('<script>alert("x")</script><pre><b>JACK</b>\n\nHeeere</pre>')
        self.assertNotIn("alert", body)
        self.assertNotIn("<b>", body)

    def test_staging_filename_is_sanitized(self):
        rc, out, _, _ = self.run_main(["https://imsdb.com/x.html", "--source", "s", "--title", "../../etc/passwd",
                                       "--dry-run"])
        files = list((Path(self.td.name) / "staging").iterdir())
        self.assertEqual([f.name for f in files], ["etc_passwd.txt"])


class TestPerformance(_Base):
    def test_reflow_large_script_fast(self):
        raw = "\n\n".join(f"JACK\n\n  All work and no play {i}\n  makes Jack a dull boy." for i in range(10_000))
        t0 = time.perf_counter()
        text = si.reflow(raw)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(text.count("JACK: All work"), 10_000)


class TestRetry(_Base):
    def test_fetch_is_one_shot_and_raises(self):
        # RETRY GAP: fetch/urlopen — one attempt, no retry; the error propagates to the operator (hand-run CLI)
        with patch.object(si.urllib.request, "urlopen", side_effect=OSError("465")) as uo:
            with self.assertRaises(OSError):
                si.fetch("https://imsdb.com/x.html")
        self.assertEqual(uo.call_count, 1)
        self.assertIn("User-agent", uo.call_args.args[0].headers)

    def test_ingest_failure_returns_nonzero(self):
        rc, *_ = self.run_main(["https://imsdb.com/x.html", "--source", "s"],
                               ingest=MagicMock(return_value=_cp(1, "", "memory server down\n")))
        self.assertEqual(rc, 1)


class TestUnit(_Base):
    def test_reflow_joins_cues(self):
        self.assertEqual(si.reflow("ANNIE\n\nHello there.\n\n\n\nINT. HOUSE - NIGHT\n\nRain falls."),
                         "ANNIE: Hello there.\n\nINT. HOUSE - NIGHT: Rain falls.")
        self.assertEqual(si.reflow(""), "")

    def test_extract_title_skips_site_names(self):
        title, body = si.extract(PAGE)
        self.assertEqual(title, "Misery")
        t2, _ = si.extract('<meta property="og:title" content="Internet Movie Script Database"><h1>Jaws</h1>')
        self.assertEqual(t2, "Jaws")

    def test_pdf_link_and_alpha(self):
        self.assertEqual(si.pdf_link("https://x/s.pdf?dl=1", ""), "https://x/s.pdf?dl=1")
        self.assertEqual(si.pdf_link("https://x/p", 'href="https://c.example/a/b.pdf"'), "https://c.example/a/b.pdf")
        self.assertIsNone(si.pdf_link("https://x/p", "<html/>"))
        self.assertEqual(si.alpha_ratio(""), 0)
        self.assertEqual(si.alpha_ratio("ab12"), 0.5)


class TestIntegration(_Base):
    def test_hands_off_to_nova_ingest_file_mode(self):
        rc, out, _, ingest = self.run_main(["https://imsdb.com/Misery.html", "--source", "horror"])
        argv = ingest.call_args.args[0]
        self.assertTrue(argv[1].endswith("nova_ingest.py"))
        self.assertEqual(argv[2:3] + argv[4:], ["file", "--source", "horror", "--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("stored 40 chunks", out)


class TestFunctional(_Base):
    def test_dry_run_writes_staging_file_only(self):
        rc, out, _, ingest = self.run_main(["https://imsdb.com/Misery.html", "--source", "horror", "--dry-run"])
        self.assertEqual(rc, 0)
        ingest.assert_not_called()
        text = (Path(self.td.name) / "staging" / "misery.txt").read_text()
        self.assertTrue(text.startswith("Misery (imsdb.com/Misery.html)"))
        self.assertIn("ANNIE: You are my number one fan", text)

    def test_wrapper_page_and_thin_text_skip(self):
        rc, _, err, ingest = self.run_main(["https://x.example/p", "--source", "s"], page="<html>reader</html>")
        self.assertEqual(rc, 2)
        self.assertIn("no <pre> script block", err)
        rc, _, err, _ = self.run_main(["https://x.example/p", "--source", "s"], page="<pre>JACK\n\nhi</pre>")
        self.assertEqual(rc, 2)
        self.assertIn("only", err)
        ingest.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--source", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
