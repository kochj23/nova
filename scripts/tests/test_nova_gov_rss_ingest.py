#!/usr/bin/env python3
"""Tests for nova_gov_rss_ingest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
Feeds, the memory server and nova_notify are mocked at module load; seen-state lives in a tempdir."""
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
SCRIPT = SCRIPTS / "nova_gov_rss_ingest.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="govrss-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("gov_rss_ingest_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gr = _load()
# module-level stubs: no feeds fetched, no memory writes, no notifications, state in a tempdir
gr.STATE_FILE = TMP / "seen.json"
gr.notify = MagicMock()
gr.log = MagicMock()
gr.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
    Request=gr.urllib.request.Request, urlopen=MagicMock(side_effect=OSError("offline"))))

RSS = """<rss><channel>
<item><title>Big &amp; breach</title><link>https://a.test/1</link><description>&lt;p&gt;Details of the breach at a vendor with many words here.&lt;/p&gt;</description><pubDate>Mon</pubDate></item>
<item><title>Second</title><guid>https://a.test/2</guid></item>
</channel></rss>"""
ATOM = """<feed><entry><title type="html">Atom post</title><link rel="alternate" href="https://b.test/x"/>
<summary>Summary text</summary><published>2026-01-01</published></entry></feed>"""


def _cm(body):
    r = MagicMock(); r.__enter__.return_value.read.return_value = body.encode(); return r


def _reset():
    gr.notify.reset_mock()
    if gr.STATE_FILE.exists():
        gr.STATE_FILE.unlink()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_feed_markup_is_stripped_before_memory(self):
        out = gr.clean_html("<script>alert(1)</script><b>bold</b> &amp; text")
        self.assertNotIn("<", out)
        self.assertEqual(out, "alert(1) bold & text")

    def test_feeds_are_well_formed(self):
        for url, vector, label in gr.FEEDS:
            self.assertRegex(url, r"^https?://", label)
            self.assertTrue(vector and label)


class TestPerformance(unittest.TestCase):
    def test_chunk_and_clean_large_text(self):
        text = "word " * 100_000
        t0 = time.perf_counter()
        chunks = gr.chunk_text(gr.clean_html(text), "[feed] title")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(all(len(c) <= gr.CHUNK_SIZE for c in chunks))

    def test_seen_state_bounded(self):
        gr.save_seen(dict.fromkeys(str(i) for i in range(30_000)))
        kept = json.loads(gr.STATE_FILE.read_text())
        self.assertEqual(len(kept), 25_000)
        self.assertEqual(kept[-1], "29999")                     # newest retained


class TestRetry(unittest.TestCase):
    def test_fetch_failure_returns_empty(self):
        # RETRY GAP: fetch_feed — one urlopen attempt per run (the 6-hourly schedule is the retry)
        gr.urllib.request.urlopen.reset_mock()
        self.assertEqual(gr.fetch_feed("https://down.test/rss"), [])
        self.assertEqual(gr.urllib.request.urlopen.call_count, 1)

    def test_ingest_failure_returns_false(self):
        # RETRY GAP: ingest_chunk — one POST; False on failure (item is still marked seen)
        self.assertFalse(gr.ingest_chunk("t", "law", {}))


class TestUnit(unittest.TestCase):
    def test_parse_rss_and_guid_fallback(self):
        with patch.object(gr.urllib.request, "urlopen", return_value=_cm(RSS)):
            items = gr.fetch_feed("https://a.test/rss")
        self.assertEqual([i["link"] for i in items], ["https://a.test/1", "https://a.test/2"])
        self.assertEqual(items[0]["pubDate"], "Mon")

    def test_parse_atom(self):
        with patch.object(gr.urllib.request, "urlopen", return_value=_cm(ATOM)):
            items = gr.fetch_feed("https://b.test/atom")
        self.assertEqual(items, [{"title": "Atom post", "link": "https://b.test/x",
                                  "description": "Summary text", "pubDate": "2026-01-01"}])

    def test_truncate_and_chunk_edges(self):
        self.assertEqual(gr.truncate_at_boundary("short"), "short")
        self.assertEqual(len(gr.truncate_at_boundary("a" * 5000, 100)), 100)
        self.assertEqual(gr.chunk_text("tiny", "p"), [])           # below 50 chars -> dropped
        _reset()
        self.assertEqual(gr.load_seen(), {})
        gr.STATE_FILE.write_text("not json")
        self.assertEqual(gr.load_seen(), {})


class TestIntegration(unittest.TestCase):
    def test_uses_shared_notify_and_memory_endpoint(self):
        self.assertIn("from nova_notify import notify", SRC)
        self.assertTrue(gr.MEMORY_URL.endswith("/remember?async=1"))
        with patch.object(gr.urllib.request, "urlopen") as m:
            self.assertTrue(gr.ingest_chunk("text", "law", {"feed": "f"}))
        body = json.loads(m.call_args[0][0].data)
        self.assertEqual((body["source"], body["metadata"]["feed"]), ("law", "f"))


class TestFunctional(unittest.TestCase):
    def _run(self, items):
        feeds = [("https://a.test/rss", "law", "FBI News"), ("https://b.test/rss", "intelligence", "Talos")]
        ingested = []
        with patch.object(gr, "FEEDS", feeds), \
             patch.object(gr, "fetch_feed", side_effect=lambda u: items if "a.test" in u else []), \
             patch.object(gr, "ingest_chunk", side_effect=lambda t, v, m: ingested.append((v, m["feed"])) or True):
            gr.run()
        return ingested

    def test_new_items_ingested_and_summarized_then_deduped(self):
        _reset()
        items = [{"title": "Agency announces a major new enforcement action", "link": "https://a.test/1",
                  "description": "Long description text", "pubDate": "Mon"}]
        ingested = self._run(items)
        self.assertEqual(ingested, [("law", "FBI News")])
        title = gr.notify.call_args[0][0]
        self.assertIn("1 new items across 1 feeds", title)
        self.assertIn("FBI News", gr.notify.call_args[1]["body"])
        gr.notify.reset_mock()
        self.assertEqual(self._run(items), [])                     # seen state dedups the second run
        gr.notify.assert_not_called()

    def test_nothing_new_posts_nothing(self):
        _reset()
        self.assertEqual(self._run([]), [])
        gr.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: running the script fetches ~700 live feeds, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_gov_rss_ingest as g; print(len(g.FEEDS) > 100)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
