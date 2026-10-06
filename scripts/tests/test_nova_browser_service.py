#!/usr/bin/env python3
"""Tests for nova_browser_service.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_browser_service.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nbs_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bs = _load()


def _public_dns(host, *a, **k):
    """Offline resolver: every name resolves to a public address."""
    return [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]


class _Stop(Exception):
    pass


class _Req:
    def __init__(self, query=None, body=None, bad=False):
        self.query, self._body, self._bad = query or {}, body, bad

    async def json(self):
        if self._bad:
            raise ValueError("bad")
        return self._body


def _serve_handlers():
    """Run serve() with the socket, PG and sleep mocked; return its route handlers and Browser."""
    from aiohttp import web
    captured = {}

    def runner_factory(app):
        captured["app"] = app
        r = MagicMock(); r.setup = AsyncMock(); return r

    async def stop(_):
        raise _Stop()

    site = MagicMock(); site.start = AsyncMock()
    with patch.object(web, "AppRunner", side_effect=runner_factory), \
         patch.object(web, "TCPSite", return_value=site) as tcp, \
         patch.object(bs, "load_allow_hosts", return_value={"nas.lan"}), \
         patch.object(bs.asyncio, "sleep", stop):
        try:
            asyncio.run(bs.serve())
        except _Stop:
            pass
    routes = {(r.method, r.resource.canonical): r.handler for r in captured["app"].router.routes()}
    return routes, tcp


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_ssrf_targets_refused(self):
        for u in ("http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest",
                  "http://192.168.1.2:3000/", "http://nova.local/", "http://[::1]/", "file:///etc/passwd",
                  "ftp://example.com/", "https://user:pw@example.com/", "http:///nohost"):
            self.assertFalse(bs.validate_url(u)[0], u)

    def test_name_resolving_private_or_unresolvable_is_refused(self):
        with patch.object(bs.socket, "getaddrinfo", return_value=[(2, 0, 0, "", ("10.0.0.5", 0))]):
            self.assertFalse(bs.validate_url("http://evil.example/")[0])
        with patch.object(bs.socket, "getaddrinfo", side_effect=socket.gaierror("nx")):
            self.assertFalse(bs.validate_url("http://nx.example/")[0])   # fail closed

    def test_allowlist_query_is_static(self):
        self.assertIn("WHERE service='browser' AND key='allow_hosts'", SRC)
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))


class TestPerformance(unittest.TestCase):
    def test_tidy_text_10k_lines(self):
        text = "\n".join(("line %d" % i) if i % 3 else "" for i in range(10_000))
        t0 = time.perf_counter()
        out = bs.tidy_text(text, bs.MAX_CHARS_CAP)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertNotIn("\n\n\n", out)

    def test_caps_are_bounded(self):
        self.assertEqual(bs.clamp(10**9, 500, bs.MAX_CHARS_CAP, 1), bs.MAX_CHARS_CAP)
        self.assertEqual(bs.MAX_CONCURRENCY, 3)


class TestRetry(unittest.TestCase):
    def test_allowlist_load_fails_open_to_empty(self):
        # RETRY GAP: load_allow_hosts()/psycopg2.connect — single attempt, failure -> empty allowlist
        import psycopg2
        with patch.object(psycopg2, "connect", side_effect=Exception("pg down")) as c:
            self.assertEqual(bs.load_allow_hosts(), set())
        self.assertEqual(c.call_count, 1)

    def test_fetch_error_is_502_not_raise(self):
        # RETRY GAP: do_fetch()/browser.fetch — one attempt, error becomes a 502 JSON response
        routes, _ = _serve_handlers()
        with patch.object(bs.Browser, "fetch", AsyncMock(side_effect=RuntimeError("chromium crashed"))) as f, \
             patch.object(bs.socket, "getaddrinfo", _public_dns):
            resp = asyncio.run(routes[("GET", "/fetch")](_Req({"url": "https://example.com/"})))
        self.assertEqual(resp.status, 502)
        self.assertEqual(f.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_selftest_with_offline_dns(self):
        with patch.object(bs.socket, "getaddrinfo", _public_dns):
            self.assertEqual(bs.selftest(), 0)

    def test_clamp_and_tidy_edges(self):
        self.assertEqual(bs.clamp(None, 1, 10, 7), 7)
        self.assertEqual(bs.clamp("-5", 1, 10, 7), 1)
        self.assertEqual(bs.tidy_text(None, 10), "")
        self.assertEqual(bs.tidy_text("  a  \n\n\n b", 100), "a\n\nb")

    def test_allowlist_parses_json_string(self):
        cur = MagicMock(); cur.fetchone.return_value = ('["nas.lan", "pi.lan"]',)
        conn = MagicMock(); conn.cursor.return_value = cur
        import psycopg2
        with patch.object(psycopg2, "connect", return_value=conn):
            self.assertEqual(bs.load_allow_hosts(), {"nas.lan", "pi.lan"})


class TestIntegration(unittest.TestCase):
    def test_fetch_sync_blocks_media_and_filters_links(self):
        page = MagicMock()
        page.title.return_value = "T"; page.url = "https://example.com/final"
        page.goto.return_value = MagicMock(status=200)
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.first.inner_text.return_value = "body text " * 100
        page.eval_on_selector_all.return_value = [{"text": "a", "href": "https://x/1"},
                                                  {"text": "js", "href": "javascript:void(0)"},
                                                  {"text": "", "href": "https://x/2"}]
        ctx = MagicMock(); ctx.new_page.return_value = page
        b = bs.Browser(); b._browser = MagicMock(); b._browser.new_context.return_value = ctx
        res = b._fetch_sync("https://example.com/", 50, 10)
        self.assertEqual(res["links"], [{"text": "a", "href": "https://x/1"}])
        self.assertEqual(len(res["text"]), 50)
        self.assertFalse(b._browser.new_context.call_args.kwargs["accept_downloads"])
        ctx.close.assert_called_once()
        handler = ctx.route.call_args[0][1]
        route = MagicMock(); route.request.resource_type = "image"
        handler(route); route.abort.assert_called_once()

    def test_output_passes_through_untrusted_gate(self):
        self.assertIn("nova_untrusted.gate(", SRC)


class TestFunctional(unittest.TestCase):
    def test_serve_routes_golden_and_refusal(self):
        routes, tcp = _serve_handlers()
        self.assertEqual(tcp.call_args[0][2], bs.PORT)
        fake = AsyncMock(return_value={"ok": True, "url": "u", "title": "t", "text": "hello", "links": [], "status": 200})
        gate = MagicMock(); gate.gate.return_value = ("<fenced>hello</fenced>", "clean")
        with patch.object(bs.Browser, "fetch", fake), patch.object(bs, "nova_untrusted", gate), \
             patch.object(bs.socket, "getaddrinfo", _public_dns):
            ok = asyncio.run(routes[("POST", "/fetch")](_Req(body={"url": "https://example.com/", "max_chars": 1})))
            bad = asyncio.run(routes[("GET", "/fetch")](_Req({"url": "http://127.0.0.1/"})))
            nas = asyncio.run(routes[("GET", "/fetch")](_Req({"url": "http://nas.lan/"})))
            inv = asyncio.run(routes[("POST", "/fetch")](_Req(bad=True)))
            health = json.loads(asyncio.run(routes[("GET", "/health")](_Req())).body)
        body = json.loads(ok.body)
        self.assertEqual((body["text"], body["verdict"]), ("<fenced>hello</fenced>", "clean"))
        self.assertEqual(fake.call_args_list[0][0][1], 500)        # max_chars clamped up
        self.assertEqual(bad.status, 400)
        self.assertEqual(nas.status, 200)                           # allowlisted private host
        self.assertEqual(inv.status, 400)
        self.assertEqual((health["fetches"], health["refused"]), (2, 1))

    def test_hostile_page_dropped(self):
        routes, _ = _serve_handlers()
        gate = MagicMock(); gate.gate.return_value = (None, "hostile")
        with patch.object(bs.Browser, "fetch", AsyncMock(return_value={"ok": True, "text": "ignore previous"})), \
             patch.object(bs, "nova_untrusted", gate), patch.object(bs.socket, "getaddrinfo", _public_dns):
            body = json.loads(asyncio.run(routes[("GET", "/fetch")](_Req({"url": "https://e.com/"}))).body)
        self.assertEqual(body["text"], "")
        self.assertIn("prompt-injection", body["error"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # --selftest resolves real DNS names, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_browser_service"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
