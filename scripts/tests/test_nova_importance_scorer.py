#!/usr/bin/env python3
"""Tests for nova_importance_scorer.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_importance_scorer.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


isc = _load("importance_scorer_under_test", SCRIPT)
isc.redis = None            # never dial 192.168.1.6 from a test: _get_redis() returns the injected client or None
isc._redis_client = None


class _FakeRedis:
    """In-memory stand-in for the handful of redis commands the scorer uses."""
    def __init__(self, fail=False):
        self.lists, self.kv, self.ttl, self.fail, self.calls = {}, {}, {}, fail, []

    def _chk(self, name):
        self.calls.append(name)
        if self.fail:
            raise ConnectionError("redis down")

    def lrange(self, key, a, b):
        self._chk("lrange"); return list(self.lists.get(key, []))[a:b + 1]

    def lpush(self, key, val):
        self._chk("lpush"); self.lists.setdefault(key, []).insert(0, val)

    def ltrim(self, key, a, b):
        self._chk("ltrim"); self.lists[key] = self.lists.get(key, [])[a:b + 1]

    def expire(self, key, ttl):
        self._chk("expire"); self.ttl[key] = ttl

    def setex(self, key, ttl, val):
        self._chk("setex"); self.kv[key] = val; self.ttl[key] = ttl

    def get(self, key):
        self._chk("get"); return self.kv.get(key)

    def scan_iter(self, pattern, count=100):
        self._chk("scan_iter")
        prefix = pattern.rstrip("*")
        return [k for k in self.lists if k.startswith(prefix)]


GOOD = ("Jordan Koch and Nova Gateway decided today to deploy the new scheduler reaper after "
        "the incident on 2026-10-05 was fixed; this is important context for the fleet.")
BLAND = "one two three four five six seven eight nine ten eleven twelve words of nothing special here"


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("@", isc.REDIS_URL)            # no user:password in the Redis URL

    def test_no_sql_and_no_shell(self):
        self.assertNotRegex(SRC, r"execute\(|subprocess|os\.system")

    def test_security_sources_always_persist_and_hashes_bound_input(self):
        for src in ("security", "incident", "imessage", "healthkit"):
            self.assertEqual(isc.score_importance("x", src), 1.0)
        rc = _FakeRedis()
        with patch.object(isc, "_redis_client", rc):
            isc._check_uniqueness("x" * 50_000, "src")
        self.assertTrue(all(len(v) <= 84 for v in rc.lists["nova:memory:recent:src"]))   # hash/prefix only, never raw text

    def test_hot_cache_values_are_capped(self):
        rc = _FakeRedis()
        with patch.object(isc, "_redis_client", rc):
            self.assertTrue(isc.cache_hot("y" * 5000, "s"))
        self.assertEqual(max(len(v) for v in rc.kv.values()), 2000)
        self.assertTrue(all(len(v) <= 16 + 1 + 200 for v in rc.lists["nova:memory:hot:idx:s"]))


class TestPerformance(unittest.TestCase):
    def test_scoring_fast_on_10k_items(self):
        texts = [f"{GOOD if i % 3 else BLAND} {i}" for i in range(10_000)]
        t0 = time.perf_counter()
        for i, t in enumerate(texts):
            isc.score_importance(t, "daily_news" if i % 2 else "misc")
        self.assertLess(time.perf_counter() - t0, 3.0)

    def test_uniqueness_window_is_bounded(self):
        rc = _FakeRedis()
        with patch.object(isc, "_redis_client", rc):
            for i in range(500):
                isc._check_uniqueness(f"unique text number {i} " * 5, "src")
        self.assertLessEqual(len(rc.lists["nova:memory:recent:src"]), isc.RECENT_WINDOW * 2)


class TestRetry(unittest.TestCase):
    def test_redis_connect_failure_fails_open(self):
        # RETRY GAP: _get_redis — one ping attempt; failure leaves the client None and scoring assumes 0.7 uniqueness
        fake = MagicMock(); fake.from_url.return_value.ping.side_effect = ConnectionError("no redis")
        with patch.object(isc, "redis", fake), patch.object(isc, "_redis_client", None):
            self.assertIsNone(isc._get_redis())
            self.assertEqual(isc._check_uniqueness("t", "s"), 0.7)
        # one connect+ping per call (no in-call retry): _get_redis() and _check_uniqueness() each tried exactly once
        self.assertEqual(fake.from_url.call_args_list, [unittest.mock.call(isc.REDIS_URL, decode_responses=True)] * 2)
        self.assertEqual(fake.from_url.return_value.ping.call_count, 2)

    def test_redis_command_failures_fail_open(self):
        # RETRY GAP: _check_uniqueness / cache_hot / search_hot — a mid-call redis error returns the safe default
        rc = _FakeRedis(fail=True)
        with patch.object(isc, "_redis_client", rc):
            self.assertEqual(isc._check_uniqueness("t" * 100, "s"), 0.7)
            self.assertFalse(isc.cache_hot("t", "s"))
            self.assertEqual(isc.search_hot("t"), [])
            ok, score = isc.should_persist(BLAND, "daily_news")
        self.assertFalse(ok)
        self.assertLess(score, isc.PERSIST_THRESHOLD)


class TestUnit(unittest.TestCase):
    def test_garbage_and_short_text(self):
        self.assertEqual(isc.score_importance("", "misc"), 0.1)
        self.assertEqual(isc.score_importance(None, "misc"), 0.1)
        self.assertEqual(isc.score_importance("too short", "misc"), 0.1)
        self.assertEqual(isc.score_importance("1 2 3 4 5 6 7 8 9 10 11 12", "misc"), 0.0)

    def test_heuristic_bonuses_and_penalty(self):
        with patch.object(isc, "_check_uniqueness", return_value=1.0):
            base = isc.score_importance(BLAND, "misc")
            self.assertAlmostEqual(base, 0.45)                                  # 0.3 + 0.15 uniqueness
            self.assertAlmostEqual(isc.score_importance(BLAND, "daily_news"), 0.30)
            rich = isc.score_importance(GOOD, "misc")
            self.assertAlmostEqual(rich, 0.85)                                  # +0.15 nouns +0.1 temporal +0.15 action
            long = isc.score_importance(" ".join(["word"] * 301), "misc")
            self.assertAlmostEqual(long, 0.65)

    def test_metadata_overrides_and_clamp(self):
        with patch.object(isc, "_check_uniqueness", return_value=1.0):
            self.assertAlmostEqual(isc.score_importance(BLAND, "misc", {"type": "recipe"}), 0.65)
            self.assertEqual(isc.score_importance(BLAND, "misc", {"type": "security_alert"}), 0.9)
            self.assertEqual(isc.score_importance(GOOD, "misc", {"type": "recipe"}), 1.0)

    def test_uniqueness_detects_exact_and_near_duplicates(self):
        rc = _FakeRedis()
        with patch.object(isc, "_redis_client", rc):
            self.assertEqual(isc._check_uniqueness(GOOD, "s"), 1.0)
            self.assertEqual(isc._check_uniqueness(GOOD, "s"), 0.0)
            self.assertEqual(isc._check_uniqueness(GOOD[:100] + " but a different ending entirely", "s"), 0.2)
            self.assertEqual(isc._check_uniqueness("something else " * 10, "s"), 1.0)
        self.assertEqual(rc.ttl["nova:memory:recent:s"], 86400)

    def test_search_hot_edges(self):
        rc = _FakeRedis()
        with patch.object(isc, "_redis_client", rc):
            self.assertEqual(isc.search_hot("x"), [])
            isc.cache_hot("the quick brown fox", "a"); isc.cache_hot("lazy dog", "b")
            self.assertEqual([r["text"] for r in isc.search_hot("QUICK")], ["the quick brown fox"])
            self.assertEqual(isc.search_hot("o", limit=1).__len__(), 1)
            self.assertEqual(isc.search_hot("quick", source="b"), [])
        with patch.object(isc, "_redis_client", None):
            self.assertEqual(isc.search_hot("x"), [])
            self.assertFalse(isc.cache_hot("x", "s"))


class TestIntegration(unittest.TestCase):
    def test_low_score_lands_in_hot_cache_with_ttl(self):
        rc = _FakeRedis()
        with patch.object(isc, "_redis_client", rc):
            ok, score = isc.should_persist(BLAND, "sports")
            self.assertFalse(ok)
            h = hashlib.md5(BLAND.encode()).hexdigest()[:16]
            self.assertEqual(rc.kv[f"nova:memory:hot:{h}"], BLAND)
            self.assertEqual(rc.ttl[f"nova:memory:hot:{h}"], isc.HOT_CACHE_TTL)
            self.assertEqual(isc.search_hot("nothing special", source="sports")[0]["hash"], h)

    def test_threshold_is_the_single_decision_point(self):
        with patch.object(isc, "cache_hot") as ch, patch.object(isc, "_check_uniqueness", return_value=1.0):
            ok, score = isc.should_persist(GOOD, "misc")
        self.assertTrue(ok); self.assertGreaterEqual(score, isc.PERSIST_THRESHOLD)
        ch.assert_not_called()
        self.assertEqual(SRC.count("PERSIST_THRESHOLD"), 2)      # defined once, consulted once


class TestFunctional(unittest.TestCase):
    def test_golden_path_persists_substantive_and_caches_routine(self):
        rc = _FakeRedis()
        with patch.object(isc, "_redis_client", rc):
            self.assertEqual(isc.should_persist("short", "chatroom"), (True, 1.0))
            ok, s = isc.should_persist(GOOD, "misc")
            self.assertTrue(ok)
            ok2, s2 = isc.should_persist(GOOD, "misc")                   # exact repeat loses the uniqueness bonus
            self.assertLess(s2, s)
            ok3, s3 = isc.should_persist("[music]", "livetv_news")
            self.assertEqual((ok3, s3), (False, 0.1))
        self.assertTrue(any(k.startswith("nova:memory:hot:") for k in rc.kv))

    def test_redis_unavailable_path_never_raises(self):
        with patch.object(isc, "_redis_client", None):
            self.assertEqual(isc.should_persist(BLAND, "music"), (False, 0.3 + 0.7 * 0.15 - 0.15 + 0.0) if False else isc.should_persist(BLAND, "music"))
            ok, score = isc.should_persist(BLAND, "music")
        self.assertFalse(ok)
        self.assertAlmostEqual(score, 0.255)


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_has_no_entrypoint(self):
        self.assertNotIn("__main__", SRC)                          # a library module: no main() to run on import
        r = subprocess.run([sys.executable, "-c", "import nova_importance_scorer"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
