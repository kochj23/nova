#!/usr/bin/env python3
"""Tests for nova_analytics_collector.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_analytics_collector.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ac = _load("analytics_collector_under_test", SCRIPT)


class _FakeRedis:
    """In-memory stand-in for redis.asyncio: salt, rate-limit counters and the event stream."""
    def __init__(self, fail_xadd=False):
        self.kv, self.stream, self.fail_xadd = {}, [], fail_xadd

    async def get(self, k): return self.kv.get(k)
    async def set(self, k, v, ex=None): self.kv[k] = v
    async def incr(self, k): self.kv[k] = int(self.kv.get(k, 0)) + 1; return self.kv[k]
    async def expire(self, k, s): pass
    async def xlen(self, k): return len(self.stream)

    async def xadd(self, key, event, maxlen=None, approximate=True):
        if self.fail_xadd:
            raise ConnectionError("redis down")
        self.stream.append(event)


def _client(fail_xadd=False):
    # No `with` block: the lifespan (which would dial the real Redis) never runs; the ASGI transport is in-process.
    ac._redis = _FakeRedis(fail_xadd=fail_xadd)
    return TestClient(ac.app, raise_server_exceptions=False), ac._redis


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_raw_ip_never_reaches_the_stream(self):
        c, r = _client()
        resp = c.post("/collect", json={"site": "digitalnoise.net", "path": "/x"},
                      headers={"cf-connecting-ip": "203.0.113.9", "user-agent": "Mozilla iPhone Safari"})
        self.assertEqual(resp.status_code, 202)
        ev = r.stream[0]
        self.assertNotIn("203.0.113.9", str(ev))
        self.assertEqual(len(ev["visitor_hash"]), 16)
        self.assertEqual(ev["ua_bucket"], "mobile-safari")

    def test_site_allowlist_and_lan_only_internal(self):
        c, r = _client()
        self.assertEqual(c.post("/collect", json={"site": "evil.example", "path": "/"}).status_code, 422)
        # TestClient's peer is "testclient" — not a LAN address — so the internal endpoint must refuse it
        resp = c.post("/collect/internal", json={"site": "digitalnoise.net", "path": "/", "ip": "1.2.3.4"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(r.stream, [])
        self.assertTrue(all(o.startswith("https://") for o in ac.ALLOWED_ORIGINS))


class TestPerformance(unittest.TestCase):
    def test_ua_bucket_and_domain_extract_fast_on_10k(self):
        uas = ["Mozilla/5.0 (iPhone) Safari", "curl/8.0", "Mozilla Chrome Edg", "Firefox tablet"] * 2500
        t0 = time.perf_counter()
        for ua in uas:
            ac.bucket_ua(ua); ac.extract_domain("https://ref.example/path?q=1")
        self.assertLess(time.perf_counter() - t0, 1.5)


class TestRetry(unittest.TestCase):
    def test_push_event_has_no_retry_and_surfaces_a_500(self):
        # RETRY GAP: push_event/_redis.xadd — one attempt; a Redis failure is a 500 to the beacon, not a retry.
        # The beacon is fire-and-forget on the client side, so a dropped event is tolerated by design.
        c, r = _client(fail_xadd=True)
        resp = c.post("/collect", json={"site": "digitalnoise.net", "path": "/"})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(r.stream, [])


class TestUnit(unittest.TestCase):
    def test_bucket_ua_edges(self):
        self.assertEqual(ac.bucket_ua(""), "desktop-other")
        self.assertEqual(ac.bucket_ua("python-requests/2"), "bot")
        self.assertEqual(ac.bucket_ua("Mozilla Android Chrome"), "mobile-chrome")
        self.assertEqual(ac.bucket_ua("Mozilla iPad Safari"), "tablet-safari")
        self.assertEqual(ac.bucket_ua("Mozilla Chrome Edg/1"), "desktop-edge")

    def test_extract_domain_edges(self):
        self.assertEqual(ac.extract_domain(""), "")
        self.assertEqual(ac.extract_domain("https://nova.digitalnoise.net/a/b"), "nova.digitalnoise.net")
        self.assertEqual(ac.extract_domain("bare.example/path"), "bare.example")

    def test_models_validate(self):
        with self.assertRaises(ValueError):
            ac.PageViewEvent(site="digitalnoise.net", path="x" * 501)
        with self.assertRaises(ValueError):
            ac.CustomEvent(site="digitalnoise.net", path="/", event_type="rm -rf")
        self.assertEqual(ac.CustomEvent(site="digitalnoise.net", path="/", event_type="scroll").event_data, {})


class TestIntegration(unittest.TestCase):
    def test_rate_limit_rides_on_the_hashed_visitor(self):
        c, r = _client()
        for _ in range(ac.RATE_LIMIT_MAX):
            self.assertEqual(c.post("/collect", json={"site": "digitalnoise.net", "path": "/"}).status_code, 202)
        self.assertEqual(c.post("/collect", json={"site": "digitalnoise.net", "path": "/"}).status_code, 429)
        self.assertEqual(len(r.stream), ac.RATE_LIMIT_MAX)
        keys = [k for k in r.kv if k.startswith("analytics:ratelimit:")]
        self.assertEqual(len(keys), 1)
        self.assertNotIn("testclient", keys[0])

    def test_stream_key_and_salt_rotation_contract(self):
        c, r = _client()
        c.post("/collect/event", json={"site": "digitalnoise.net", "path": "/", "event_type": "scroll"})
        self.assertEqual(ac.STREAM_KEY, "analytics:events")
        self.assertTrue(any(k.startswith("analytics:salt:") for k in r.kv))
        self.assertEqual(r.stream[0]["type"], "event")
        self.assertEqual(r.stream[0]["event_data"], "{}")


class TestFunctional(unittest.TestCase):
    def test_pixel_golden_path_records_a_pageview_and_returns_a_gif(self):
        c, r = _client()
        resp = c.get("/pixel?s=digitalnoise.net&p=/post", headers={"referer": "https://news.ycombinator.com/x"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/gif")
        self.assertEqual(resp.content, ac.PIXEL_GIF)
        self.assertEqual(r.stream[0]["referrer_domain"], "news.ycombinator.com")
        self.assertEqual(r.stream[0]["path"], "/post")

    def test_pixel_for_unknown_site_still_serves_but_records_nothing(self):
        c, r = _client()
        resp = c.get("/pixel?s=unknown.example")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(r.stream, [])
        self.assertEqual(c.get("/health").json(), {"ok": True})   # non-LAN peer gets no stream length


class TestFrame(unittest.TestCase):
    def test_import_never_binds_a_port(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertEqual(SRC.count("uvicorn.run("), 1)
        self.assertGreater(SRC.index("uvicorn.run("), SRC.index('if __name__ == "__main__":'))
        r = subprocess.run([sys.executable, "-c", "import nova_analytics_collector"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
