#!/usr/bin/env python3
"""Tests for nova_sam_blog_ingest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). All HTTP (blog fetch + vector remember) and the notify bus are
mocked; STATE_FILE is redirected to a tempdir. Fully offline. Written by Jordan Koch (via Claude)."""
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
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sb = _load("nova_sam_blog_ingest_t", SCRIPTS / "nova_sam_blog_ingest.py")
SRC = (SCRIPTS / "nova_sam_blog_ingest.py").read_text()
sb.notify = mock.MagicMock()   # neutralize the only outbound notification at load

INDEX = '''<html><body><nav>Home Posts About</nav>
<a href="https://jasonacox-sam.github.io/posts/on-being/">On Being</a>
<a href="/posts/the-herd/">The Herd</a>
</body></html>'''
POST = '<html><header>menu</header><h1>On Being</h1><p>' + ("thought " * 300) + '</p><footer>x</footer></html>'


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_links_restricted_to_sam_blog_posts(self):
        links = sb.find_post_links('<a href="https://evil.example.com/posts/x">e</a>' + INDEX)
        self.assertTrue(all("jasonacox-sam.github.io/posts/" in l for l in links))
        self.assertFalse(any("evil.example.com" in l for l in links))


class TestPerformance(unittest.TestCase):
    def test_html_strip_10k_nodes(self):
        html = "<p>" + "".join(f"<span>w{i}</span>" for i in range(10_000)) + "</p>"
        t0 = time.perf_counter()
        s = sb.HTMLStripper(); s.feed(html); s.get_text()
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_vector_remember_failure_is_swallowed(self):
        # RETRY GAP: vector_remember()/urlopen — single attempt; an outage is logged, never raised
        with mock.patch("urllib.request.urlopen", side_effect=OSError("mem down")) as u:
            sb.vector_remember("hello", source="herd_blog")
        self.assertEqual(u.call_count, 1)

    def test_fetch_page_failure_returns_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(sb.fetch_page("http://x"), (None, None))


class TestUnit(unittest.TestCase):
    def test_html_stripper_drops_nav_and_footer(self):
        s = sb.HTMLStripper()
        s.feed("<nav>SKIP</nav><p>keep this</p><footer>SKIP2</footer>")
        text = s.get_text()
        self.assertIn("keep this", text)
        self.assertNotIn("SKIP", text)

    def test_find_post_links_dedups_and_absolutizes(self):
        links = sb.find_post_links(INDEX)
        self.assertIn("https://jasonacox-sam.github.io/posts/on-being/", links)
        self.assertIn("https://jasonacox-sam.github.io/posts/the-herd/", links)
        self.assertEqual(len(links), len(set(links)))

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "s.json"
            with mock.patch.object(sb, "STATE_FILE", sf):
                self.assertEqual(sb.load_state()["ingested_urls"], [])
                sb.save_state({"ingested_urls": ["u1"], "last_check": "2026-10-05"})
                self.assertEqual(sb.load_state()["ingested_urls"], ["u1"])


class TestIntegration(unittest.TestCase):
    def test_vector_payload_shape(self):
        cap = {}
        def urlopen(req, timeout=None):
            cap["url"] = req.full_url
            cap["body"] = json.loads(req.data)
            return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            sb.vector_remember("post text", source="herd_blog", metadata={"author": "Sam"})
        self.assertIn("async=1", cap["url"])
        self.assertEqual(cap["body"]["source"], "herd_blog")
        self.assertEqual(cap["body"]["metadata"]["author"], "Sam")


class TestFunctional(unittest.TestCase):
    def _run(self, pages, state0=None):
        calls = []
        def fetch(url):
            calls.append(url)
            if url == sb.BLOG_URL:
                return INDEX, ""
            return POST, sb.HTMLStripper().__class__ and _strip(POST)
        remembered = []
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "s.json"
            if state0 is not None:
                sf.write_text(json.dumps(state0))
            with mock.patch.object(sb, "STATE_FILE", sf), \
                 mock.patch.object(sb, "fetch_page", side_effect=fetch), \
                 mock.patch.object(sb, "vector_remember", side_effect=lambda *a, **k: remembered.append((a, k))), \
                 mock.patch.object(sb, "notify") as notify:
                sb.main()
            final = json.loads(sf.read_text())
        return remembered, notify, final

    def test_ingests_new_posts_and_notifies(self):
        remembered, notify, final = self._run(POST)
        self.assertTrue(remembered)
        self.assertEqual(len(final["ingested_urls"]), 2)
        notify.assert_called_once()

    def test_no_new_posts_is_quiet(self):
        state0 = {"ingested_urls": ["https://jasonacox-sam.github.io/posts/on-being/",
                                    "https://jasonacox-sam.github.io/posts/the-herd/"], "last_check": None}
        remembered, notify, final = self._run(POST, state0=state0)
        self.assertEqual(remembered, [])
        notify.assert_not_called()

    def test_blog_index_unreachable_aborts(self):
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "s.json"
            with mock.patch.object(sb, "STATE_FILE", sf), \
                 mock.patch.object(sb, "fetch_page", return_value=(None, None)), \
                 mock.patch.object(sb, "vector_remember") as vr, mock.patch.object(sb, "notify") as notify:
                sb.main()
            vr.assert_not_called()
            notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_sam_blog_ingest"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Checking Sam", r.stdout)


def _strip(html):
    s = sb.HTMLStripper(); s.feed(html); return s.get_text()


if __name__ == "__main__":
    unittest.main()
