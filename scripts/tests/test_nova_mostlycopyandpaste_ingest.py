#!/usr/bin/env python3
"""Tests for nova_mostlycopyandpaste_ingest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_mostlycopyandpaste_ingest.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):
    mc = _load("mcap_t", SCRIPT)
SRC = SCRIPT.read_text()
mc.notify = MagicMock()
mc.log = lambda m: None
URLOPEN = MagicMock(side_effect=RuntimeError("urlopen not mocked in test"))
mc.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=URLOPEN))
_TMP = tempfile.TemporaryDirectory()
mc.STATE_FILE = Path(_TMP.name) / "state" / "mcap.json"

FEED = """<rss><channel>
<item><title>Post One</title><link>https://mostlycopyandpaste.com/posts/one/</link><pubDate>2026-01-05</pubDate></item>
</channel></rss>"""
ARCHIVE = ('<a href="/posts/two/">2</a><a href="/tags/x/">t</a><a href="https://other.example/z">o</a>'
           '<a href="https://mostlycopyandpaste.com/">home</a><a href="/posts/one/">1</a>')
ARTICLE = "<html><title>T2</title><nav>menu</nav><script>x()</script><p>" + ("word " * 400) + "</p></html>"


def _site(pages):
    def fake_fetch(url):
        return pages.get(url)
    return patch.object(mc, "fetch", side_effect=fake_fetch)


def _remembered():
    return [json.loads(c.args[0].data) for c in URLOPEN.call_args_list]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_off_site_and_taxonomy_links_ignored(self):
        with _site({mc.FEED_URL: None, mc.ARCHIVE_URL: ARCHIVE}):
            urls = mc.article_urls()
        self.assertEqual(set(urls), {"https://mostlycopyandpaste.com/posts/two", "https://mostlycopyandpaste.com/posts/one"})

    def test_script_and_nav_stripped(self):
        s = mc.HTMLStripper(); s.feed(ARTICLE)
        self.assertNotIn("x()", s.get_text())
        self.assertNotIn("menu", s.get_text())


class TestPerformance(unittest.TestCase):
    def test_strip_large_page(self):
        page = "<html><p>" + "lorem ipsum " * 10_000 + "</p></html>"
        t0 = time.perf_counter()
        s = mc.HTMLStripper(); s.feed(page)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertGreater(len(s.get_text()), 100_000)


class TestRetry(unittest.TestCase):
    def test_fetch_and_remember_fail_open(self):
        # RETRY GAP: fetch / vector_remember — one attempt each, failures logged, never raised; an unfetched
        # article is NOT marked ingested so the next daily run retries it
        URLOPEN.reset_mock(); URLOPEN.side_effect = OSError("down")
        try:
            self.assertIsNone(mc.fetch("https://x"))
            mc.vector_remember("t")
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        self.assertEqual(URLOPEN.call_count, 2)
        mc.STATE_FILE.unlink(missing_ok=True)
        with _site({mc.FEED_URL: FEED, mc.ARCHIVE_URL: ""}):
            mc.main()
        self.assertEqual(mc.load_state()["ingested_urls"], [])


class TestUnit(unittest.TestCase):
    def test_state_defaults_and_roundtrip(self):
        mc.STATE_FILE.unlink(missing_ok=True)
        self.assertEqual(mc.load_state(), {"ingested_urls": [], "last_check": None})
        mc.save_state({"ingested_urls": ["a"], "last_check": "d"})
        self.assertEqual(mc.load_state()["ingested_urls"], ["a"])
        mc.STATE_FILE.write_text("{corrupt")
        self.assertEqual(mc.load_state()["ingested_urls"], [])

    def test_bad_feed_xml_falls_back_to_archive(self):
        with _site({mc.FEED_URL: "<rss><unclosed>", mc.ARCHIVE_URL: '<a href="/posts/z/">'}):
            self.assertEqual(list(mc.article_urls()), ["https://mostlycopyandpaste.com/posts/z"])


class TestIntegration(unittest.TestCase):
    def test_vector_remember_payload(self):
        URLOPEN.reset_mock(); URLOPEN.side_effect = None
        try:
            mc.vector_remember("hello", metadata={"a": 1})
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        req = URLOPEN.call_args.args[0]
        self.assertEqual(req.full_url, mc.VECTOR_URL + "?async=1")
        self.assertEqual(json.loads(req.data), {"text": "hello", "source": "tech_blog", "metadata": {"a": 1}})


class TestFunctional(unittest.TestCase):
    def setUp(self):
        mc.STATE_FILE.unlink(missing_ok=True)
        mc.notify.reset_mock(); URLOPEN.reset_mock(); URLOPEN.side_effect = None

    def tearDown(self):
        URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")

    def test_first_run_ingests_chunks_then_second_run_is_noop(self):
        pages = {mc.FEED_URL: FEED, mc.ARCHIVE_URL: ARCHIVE,
                 "https://mostlycopyandpaste.com/posts/one": ARTICLE, "https://mostlycopyandpaste.com/posts/two": ARTICLE}
        with _site(pages):
            mc.main()
        mem = _remembered()
        self.assertTrue(mem[0]["text"].startswith('mostlycopyandpaste.com article: "Post One" (2026-01-05):'))
        self.assertIn("blog_post_chunk", {m["source"] for m in mem})
        self.assertEqual(len(mc.load_state()["ingested_urls"]), 2)
        self.assertIn("ingested 2 new", mc.notify.call_args.args[0])
        URLOPEN.reset_mock(); mc.notify.reset_mock()
        with _site(pages):
            mc.main()
        self.assertEqual((URLOPEN.call_count, mc.notify.call_count), (0, 0))

    def test_short_article_skipped_but_tracked(self):
        with _site({mc.FEED_URL: FEED, mc.ARCHIVE_URL: "",
                    "https://mostlycopyandpaste.com/posts/one": "<p>tiny</p>"}):
            mc.main()
        self.assertEqual(URLOPEN.call_count, 0)
        mc.notify.assert_not_called()
        self.assertEqual(mc.load_state()["ingested_urls"], ["https://mostlycopyandpaste.com/posts/one"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys,urllib.request,psycopg2;sys.path.insert(0,'.');"
                "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "urllib.request.urlopen=lambda *a,**k:(_ for _ in ()).throw(SystemExit(9));"
                "import nova_mostlycopyandpaste_ingest;print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
