#!/usr/bin/env python3
"""Tests for nova_attention_zones.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_attention_zones.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("naz_under_test", SCRIPTS / "nova_attention_zones.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


az = _load()


class FakeRedis:
    """In-memory stand-in for the redis client (get/set/setex/expire/delete/scan_iter)."""
    def __init__(self):
        self.d = {}; self.calls = 0

    def get(self, k):
        self.calls += 1
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v

    def setex(self, k, ttl, v):
        self.d[k] = v

    def expire(self, k, ttl):
        pass

    def delete(self, k):
        self.d.pop(k, None)

    def scan_iter(self, pat):
        prefix = pat.rstrip("*")
        return [k for k in list(self.d) if k.startswith(prefix)]


class _Req:
    def __init__(self, body=None, query=None, bad=False):
        self._body, self.query, self._bad = body, query or {}, bad

    async def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._body


class _Base(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()
        p = patch.object(az, "get_redis", return_value=self.r)
        p.start(); self.addCleanup(p.stop)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_unknown_zone_rejected_by_api(self):
        r = FakeRedis()
        with patch.object(az, "get_redis", return_value=r):
            resp = asyncio.run(az.handle_signal(_Req({"zone": "../etc"})))
            self.assertEqual(resp.status, 400)
            resp = asyncio.run(az.handle_activate(_Req({"zone": "root"})))
            self.assertEqual(resp.status, 400)
        self.assertNotIn("nova:zone:manual_override", r.d)

    def test_invalid_json_is_400(self):
        self.assertEqual(asyncio.run(az.handle_signal(_Req(bad=True))).status, 400)
        self.assertEqual(asyncio.run(az.handle_activate(_Req(bad=True))).status, 400)


class TestPerformance(_Base):
    def test_loitering_scan_10k_items_fast(self):
        old = time.time() - 10 * 3600
        for i in range(10_000):
            self.r.d[f"nova:zone:loitering:work:item{i}"] = json.dumps({"type": "pr_review", "entered_at": old})
        t0 = time.perf_counter()
        alerts = az.check_loitering()
        self.assertEqual(len(alerts), 10_000)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_in_hours_10k_calls(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            az._in_hours(i % 24, (22, 8))
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def test_redis_client_is_lazy_singleton(self):
        # RETRY GAP: get_redis/redis.from_url — no retry; a single client is built lazily and reused
        with patch.object(az, "_rc", None), patch.object(az.redis, "from_url", return_value=FakeRedis()) as fu:
            a = az.get_redis(); b = az.get_redis()
        self.assertIs(a, b)
        self.assertEqual(fu.call_count, 1)

    def test_redis_failure_propagates_from_should_notify(self):
        # RETRY GAP: should_notify — a redis outage is not swallowed (no retry, no fail-open default)
        class Down(FakeRedis):
            def get(self, k):
                raise ConnectionError("redis down")
        with patch.object(az, "get_redis", return_value=Down()):
            with self.assertRaises(ConnectionError):
                az.should_notify("info")


class TestUnit(_Base):
    def test_in_hours_wrap_and_plain(self):
        self.assertTrue(az._in_hours(23, (22, 8)))
        self.assertTrue(az._in_hours(3, (22, 8)))
        self.assertFalse(az._in_hours(12, (22, 8)))
        self.assertTrue(az._in_hours(8, (8, 18)))
        self.assertFalse(az._in_hours(18, (8, 18)))

    def test_manual_override_and_dnd(self):
        self.r.d["nova:zone:manual_override"] = "home"
        self.assertEqual(az.get_active_zone(), "home")
        del self.r.d["nova:zone:manual_override"]
        self.r.d["nova:zone:dnd_active"] = "true"
        self.assertEqual(az.get_active_zone(), "focus")
        self.assertFalse(az.should_notify("warning"))
        self.assertTrue(az.should_notify("critical"))
        self.assertEqual(az.get_zone_sensitivity(), az.ZONES["focus"]["sensitivity"])

    def test_signal_zone_resets_inertia_after_gap(self):
        self.r.d["nova:zone:work:last_signal"] = str(time.time() - 1000)
        az.signal_zone("work")
        self.assertAlmostEqual(float(self.r.d["nova:zone:work:inertia_start"]), time.time(), delta=5)

    def test_loitering_threshold_and_resolve(self):
        az.register_item("pr1", "pr_review", "work")
        self.assertEqual(az.check_loitering(), [])        # fresh
        self.r.d["nova:zone:loitering:work:pr1"] = json.dumps({"type": "pr_review", "entered_at": time.time() - 5 * 3600})
        a = az.check_loitering()
        self.assertEqual((a[0]["item_id"], a[0]["zone"]), ("pr1", "work"))
        az.resolve_item("pr1", "work")
        self.assertEqual(az.check_loitering(), [])


class TestIntegration(_Base):
    def test_signal_then_zone_uses_inertia(self):
        # sustained signal in "home" past its inertia makes it active outside rest hours
        now = time.time()
        self.r.d["nova:zone:home:last_signal"] = str(now)
        self.r.d["nova:zone:home:inertia_start"] = str(now - 40)
        with patch.object(az, "_in_hours", side_effect=lambda h, rng: rng == (0, 24)):
            self.assertEqual(az.get_active_zone(), "home")
        self.assertEqual(self.r.d["nova:zone:active"], "home")

    def test_keys_are_namespaced(self):
        az.signal_zone("work")
        self.assertTrue(all(k.startswith("nova:zone:") for k in self.r.d))


class TestFunctional(_Base):
    def test_activate_signal_status_golden_path(self):
        resp = asyncio.run(az.handle_activate(_Req({"zone": "focus", "duration_s": 60})))
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.r.d["nova:zone:manual_override"], "focus")
        resp = asyncio.run(az.handle_signal(_Req({"zone": "work", "source": "github"})))
        self.assertEqual(json.loads(resp.body)["active"], "focus")
        st = json.loads(asyncio.run(az.handle_status(_Req())).body)
        self.assertEqual(st["active_zone"], "focus")
        self.assertIn("work", st["zones"])
        sn = json.loads(asyncio.run(az.handle_should_notify(_Req(query={"severity": "info"}))).body)
        self.assertFalse(sn["allowed"])

    def test_activate_auto_clears_override(self):
        self.r.d["nova:zone:manual_override"] = "focus"
        asyncio.run(az.handle_activate(_Req({"zone": "auto"})))
        self.assertNotIn("nova:zone:manual_override", self.r.d)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_attention_zones"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
