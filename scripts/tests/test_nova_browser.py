#!/usr/bin/env python3
"""Tests for nova_browser.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). nova_config (Keychain-backed) is stubbed at load; all HTTP, the
Slack upload curl and Playwright are mocked. No browser is launched, no network, no Keychain.
Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

# Stub nova_config BEFORE load so the module-level slack_bot_token()/Keychain call never runs.
_FAKE_CFG = types.SimpleNamespace(
    slack_bot_token=lambda: "xoxb-test", SLACK_PHOTOS="C_PHOTOS",
    VECTOR_URL="http://memory.test/remember")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"nova_config": _FAKE_CFG}):
        spec.loader.exec_module(mod)
    return mod


br = _load("nova_browser_t", SCRIPTS / "nova_browser.py")
SRC = (SCRIPTS / "nova_browser.py").read_text()


def _resp(html):
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = html.encode()
    return mock.Mock(return_value=cm)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_slack_token_from_config_not_source(self):
        self.assertIn("SLACK_TOKEN = nova_config.slack_bot_token()", SRC)
        self.assertNotRegex(SRC, r"xoxb-[A-Za-z0-9-]{10,}")

    def test_html_to_text_strips_script_and_style(self):
        html = "<html><script>steal()</script><style>x{}</style><p>safe text</p></html>"
        out = br._html_to_text(html)
        self.assertIn("safe text", out)
        self.assertNotIn("steal()", out)
        self.assertNotIn("x{}", out)


class TestPerformance(unittest.TestCase):
    def test_html_to_text_large_doc(self):
        html = "<p>" + ("word " * 50_000) + "</p>"
        t0 = time.perf_counter()
        br._html_to_text(html)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_fetch_simple_http_error(self):
        # RETRY GAP: _fetch_simple_sync()/urlopen — single attempt; errors become an {"error":...} dict
        import urllib.error
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("u", 404, "Not Found", {}, None)) as u:
            r = br._fetch_simple_sync("http://x")
        self.assertIn("HTTP 404", r["error"])
        self.assertEqual(u.call_count, 1)

    def test_fetch_rendered_falls_back_to_http_without_playwright(self):
        with mock.patch.object(br, "_playwright_available", return_value=False), \
             mock.patch("urllib.request.urlopen", _resp("<title>Fallback</title><p>hi</p>")), \
             mock.patch.object(br, "log"):
            r = asyncio.run(br.fetch_rendered("http://x"))
        self.assertEqual(r["method"], "simple_http")
        self.assertEqual(r["title"], "Fallback")

    def test_vector_remember_swallows_errors(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("mem down")):
            br.vector_remember("text")  # must not raise


class TestUnit(unittest.TestCase):
    def test_extract_title(self):
        self.assertEqual(br._extract_title("<TITLE> Hello </TITLE>"), "Hello")
        self.assertEqual(br._extract_title("<p>no title</p>"), "")

    def test_html_entities_decoded(self):
        self.assertIn("A & B", br._html_to_text("<p>A &amp; B</p>"))
        self.assertIn("<tag>", br._html_to_text("<p>&lt;tag&gt;</p>"))

    def test_playwright_available_false_when_cache_missing(self):
        with mock.patch.object(br.Path, "home", return_value=Path("/nonexistent-home-xyz")):
            self.assertFalse(br._playwright_available())


class TestIntegration(unittest.TestCase):
    def test_fetch_simple_sync_shape(self):
        with mock.patch("urllib.request.urlopen", _resp("<title>T</title><body><p>body text</p></body>")):
            r = br._fetch_simple_sync("http://x")
        self.assertEqual(r["method"], "simple_http")
        self.assertEqual(r["title"], "T")
        self.assertIn("body text", r["text"])
        self.assertEqual(r["url"], "http://x")

    def test_slack_upload_uses_token_and_channel(self):
        with mock.patch.object(br.__dict__.get("subprocess", subprocess) if False else subprocess, "run"):
            pass
        with mock.patch("subprocess.run") as run:
            br.slack_upload("/tmp/x.png", "a shot")
        cmd = run.call_args.args[0]
        self.assertIn("https://slack.com/api/files.upload", cmd)
        self.assertTrue(any("Bearer xoxb-test" in str(c) for c in cmd))
        self.assertTrue(any("C_PHOTOS" in str(c) for c in cmd))


class TestFunctional(unittest.TestCase):
    def test_vector_remember_payload(self):
        cap = {}
        def urlopen(req, timeout=None):
            cap["url"] = req.full_url; cap["body"] = json.loads(req.data)
            return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            br.vector_remember("page text", metadata={"url": "http://x"})
        self.assertEqual(cap["body"]["source"], "browser")
        self.assertEqual(cap["body"]["metadata"]["url"], "http://x")

    def test_fetch_rendered_uses_playwright_when_available(self):
        async def _run():
            page = mock.AsyncMock()
            page.content.return_value = "<html>" + "x" * 100 + "</html>"
            page.inner_text.return_value = "rendered body"
            page.title.return_value = "SPA Title"
            with mock.patch.object(br, "_playwright_available", return_value=True), \
                 mock.patch.object(br, "create_browser", new=mock.AsyncMock(return_value=("pw", "ctx", page))), \
                 mock.patch.object(br, "close_browser", new=mock.AsyncMock()), \
                 mock.patch.object(br, "log"):
                return await br.fetch_rendered("http://spa")
        r = asyncio.run(_run())
        self.assertEqual(r["method"], "playwright")
        self.assertEqual(r["title"], "SPA Title")
        self.assertEqual(r["text"], "rendered body")

    def test_fetch_rendered_playwright_crash_falls_back(self):
        async def _run():
            with mock.patch.object(br, "_playwright_available", return_value=True), \
                 mock.patch.object(br, "create_browser", new=mock.AsyncMock(side_effect=RuntimeError("no chrome"))), \
                 mock.patch("urllib.request.urlopen", _resp("<title>HTTP</title>")), \
                 mock.patch.object(br, "log"):
                return await br.fetch_rendered("http://x")
        r = asyncio.run(_run())
        self.assertEqual(r["method"], "simple_http")


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_browser.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--fetch", r.stdout)

    def test_import_never_launches_browser(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        # all CLI dispatch lives under the main guard, not at module scope
        before_guard = SRC.split('if __name__')[0]
        self.assertNotIn("argparse.ArgumentParser(", before_guard)


if __name__ == "__main__":
    unittest.main()
