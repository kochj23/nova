#!/usr/bin/env python3
"""Tests for nova_dead_letter_replay.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):
    dl = _load("dead_letter_t", SCRIPTS / "nova_dead_letter_replay.py")
SRC = (SCRIPTS / "nova_dead_letter_replay.py").read_text()
dl.notify = MagicMock()
dl.log = MagicMock()


class FakeRedis:
    def __init__(self, dead=(), fail_ping=False):
        self.lists = {dl.REDIS_DEAD_LETTER: list(dead), dl.REDIS_QUEUE: []}; self.fail_ping = fail_ping; self.url = None

    def ping(self):
        if self.fail_ping:
            raise ConnectionError("NOAUTH")
        return True

    def llen(self, k):
        return len(self.lists[k])

    def lpop(self, k):
        return self.lists[k].pop(0) if self.lists[k] else None

    def rpush(self, k, v):
        self.lists[k].append(v)


def _run(fake, env=None):
    def from_url(u):
        fake.url = u
        return fake
    dl.notify.reset_mock()
    mod = types.SimpleNamespace(from_url=from_url)
    with patch.dict(sys.modules, {"redis": mod}), patch.dict(os.environ, env or {}):
        dl.main()
    return fake


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"redis://[^\"'@]*:[^\"'@]+@")   # no inline redis password

    def test_redis_url_overridable_by_env(self):
        f = _run(FakeRedis(), {"REDIS_URL": "redis://test-host:1"})
        self.assertEqual(f.url, "redis://test-host:1")


class TestPerformance(unittest.TestCase):
    def test_replay_10k_items(self):
        items = [json.dumps({"text": f"m{i}", "_retries": 3}) for i in range(10_000)]
        t0 = time.perf_counter()
        f = _run(FakeRedis(items))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(f.lists[dl.REDIS_QUEUE]), 10_000)


class TestRetry(unittest.TestCase):
    def test_redis_down_exits_1_without_retry(self):
        # RETRY GAP: main()/redis ping — one attempt; unreachable redis exits 1 (weekly job retries next run)
        with self.assertRaises(SystemExit) as e:
            _run(FakeRedis(fail_ping=True))
        self.assertEqual(e.exception.code, 1)
        dl.notify.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_retry_and_error_fields_stripped(self):
        f = _run(FakeRedis([json.dumps({"text": "a", "_retries": 3, "_error": "x", "k": 1})]))
        self.assertEqual(json.loads(f.lists[dl.REDIS_QUEUE][0]), {"text": "a", "k": 1})

    def test_empty_dead_letter_is_silent(self):
        _run(FakeRedis())
        dl.notify.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_moves_from_dead_letter_to_ingest_queue(self):
        self.assertEqual(dl.REDIS_QUEUE, "nova:memory:ingest")
        self.assertEqual(dl.REDIS_DEAD_LETTER, "nova:memory:dead-letter")
        f = _run(FakeRedis([json.dumps({"n": 1}), json.dumps({"n": 2})]))
        self.assertEqual(f.lists[dl.REDIS_DEAD_LETTER], [])
        self.assertEqual([json.loads(x)["n"] for x in f.lists[dl.REDIS_QUEUE]], [1, 2])

    def test_uses_shared_notify(self):
        self.assertIn("from nova_notify import notify", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_with_malformed_item(self):
        _run(FakeRedis([json.dumps({"a": 1}), b"{not json", json.dumps({"b": 2})]))
        kw = dl.notify.call_args.kwargs
        self.assertEqual(kw["meta"], {"replayed": 2, "skipped": 1})
        self.assertEqual((kw["level"], kw["dedup_key"]), ("info", "dead-letter-replay"))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys;sys.path.insert(0,'.');import psycopg2;"
                "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "import importlib.util as u;s=u.spec_from_file_location('m','nova_dead_letter_replay.py');"
                "m=u.module_from_spec(s);s.loader.exec_module(m);print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
