#!/usr/bin/env python3
"""Tests for nova_proactive_brief.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_proactive_brief.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):
    pb = _load("proactive_brief_t", SCRIPT)
SRC = SCRIPT.read_text()
REAL_PRIVATE = pb.nova_config.is_private_source
pb.notify = MagicMock()
pb.log = lambda m: None
URLOPEN = MagicMock(side_effect=RuntimeError("urlopen not mocked in test"))
pb.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=URLOPEN),
                                  error=urllib.error)
pb.asyncpg = types.SimpleNamespace(create_pool=AsyncMock(side_effect=RuntimeError("asyncpg not mocked in test")))
_TMP = tempfile.TemporaryDirectory()
pb.STATE_DIR = Path(_TMP.name)
pb.STATE_FILE = pb.STATE_DIR / "proactive_brief_state.json"

LONG = "We should revisit the BGP failover plan for the UniFi gateway before the next maintenance window please"


class _Conn:
    def __init__(self, routes):
        self.routes = routes; self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        for k, v in self.routes.items():
            if k in sql:
                return v
        return []


class _Pool:
    def __init__(self, routes):
        self.conn = _Conn(routes); self.closed = False

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()

    async def close(self):
        self.closed = True


def _hour(h):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz).replace(hour=h)
    return patch.object(pb, "datetime", _DT)


def _run(chat=(), emails=(), matches=(), hour=10, embed=(0.1, 0.2)):
    ops = _Pool({"chatroom_messages": [{"message": m, "created_at": "t"} for m in chat]})
    mem = _Pool({"source = 'email_archive'": [{"text": t, "created_at": "t"} for t in emails],
                 "embedding <=>": list(matches)})
    pb.notify.reset_mock()
    with _hour(hour), patch.object(pb.asyncpg, "create_pool", AsyncMock(side_effect=[ops, mem])) as cp, \
            patch.object(pb, "get_embedding", return_value=list(embed) if embed else None):
        asyncio.run(pb.run())
    return ops, mem, cp


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(pb.OPS_DSN + pb.MEMORIES_DSN, r"kochj:[^@]+@")

    def test_sql_uses_bind_params(self):
        self.assertIsNone(re.search(r'fetch\(\s*f["\']', SRC))
        pb.STATE_FILE.unlink(missing_ok=True)
        _, mem, _ = _run(chat=[LONG], matches=[{"text": "t", "source": "github", "similarity": 0.9}])
        sql, args = [c for c in mem.conn.calls if "embedding <=>" in c[0]][0]
        self.assertIn("$1::vector", sql)
        self.assertEqual(args[1:], ("chatroom", pb.SIMILARITY_THRESHOLD))

    def test_private_sources_never_surface(self):
        pb.STATE_FILE.unlink(missing_ok=True)
        _run(chat=[LONG], matches=[{"text": "bp 150/95", "source": "apple_health", "similarity": 0.95},
                                   {"text": "an email", "source": "email_archive", "similarity": 0.9}])
        pb.notify.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_helpers_on_10k_messages(self):
        msgs = [f"{LONG} #{i}" for i in range(10_000)]
        t0 = time.perf_counter()
        for m in msgs:
            pb.is_substantive(m); pb.fingerprint(m); pb.extract_topic(m)
        pb.format_briefing("t", [{"source": f"s{i % 7}", "text": "x" * 200, "similarity": 0.9} for i in range(10_000)])
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_embedding_failure_fails_open(self):
        # RETRY GAP: get_embedding — one POST to the memory server; failure returns None and the message is skipped
        URLOPEN.reset_mock(); URLOPEN.side_effect = OSError("down")
        try:
            self.assertIsNone(pb.get_embedding("x"))
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        self.assertEqual(URLOPEN.call_count, 1)
        pb.STATE_FILE.unlink(missing_ok=True)
        _run(chat=[LONG], embed=None)
        pb.notify.assert_not_called()

    def test_db_connect_failure_exits_1(self):
        # RETRY GAP: run()/asyncpg.create_pool — one attempt, exit 1 (cron re-runs in 2h)
        with _hour(10), patch.object(pb.asyncpg, "create_pool", AsyncMock(side_effect=OSError("pg down"))):
            with self.assertRaises(SystemExit) as e:
                asyncio.run(pb.run())
        self.assertEqual(e.exception.code, 1)


class TestUnit(unittest.TestCase):
    def test_substantive_topic_fingerprint(self):
        self.assertFalse(pb.is_substantive("thanks"))
        self.assertFalse(pb.is_substantive("ok " + "x" * 80))
        self.assertFalse(pb.is_substantive("short but real"))
        self.assertTrue(pb.is_substantive(LONG))
        self.assertTrue(pb.extract_topic("w " * 60).endswith("..."))
        self.assertEqual(pb.fingerprint(" A "), pb.fingerprint("a"))
        self.assertEqual(len(pb.fingerprint("a")), 16)

    def test_truncate_and_state(self):
        self.assertEqual(pb.truncate_at_boundary("abc", 10), "abc")
        self.assertEqual(pb.truncate_at_boundary("aaaa bbbbbbbbbb", 10), "aaaa bbbbb")
        s = {"briefed": []}
        pb.record_briefed(s, "fp1", "t")
        self.assertTrue(pb.already_briefed(s, "fp1"))
        self.assertFalse(pb.already_briefed({}, "fp1"))

    def test_format_briefing(self):
        out = pb.format_briefing("BGP", [{"source": "github", "text": "a\nb", "similarity": 0.91},
                                         {"source": "github", "text": "c", "similarity": 0.85},
                                         {"source": "docs", "text": "d", "similarity": 0.83}])
        self.assertIn("I have 3 relevant memories across 2 domain(s)", out)
        self.assertIn("*github* (2 matches, 91% relevance): _a b_", out)


class TestIntegration(unittest.TestCase):
    def test_uses_shared_privacy_gate_and_embed_endpoint(self):
        self.assertIn("nova_config.is_private_source(", SRC)
        self.assertTrue(REAL_PRIVATE("apple_health"))
        URLOPEN.reset_mock(); URLOPEN.side_effect = None
        r = MagicMock(); r.read.return_value = b'{"vector": [1, 2]}'
        r.__enter__ = lambda s: s; r.__exit__ = lambda *a: False
        URLOPEN.return_value = r
        try:
            self.assertEqual(pb.get_embedding("hello"), [1, 2])
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        self.assertEqual(URLOPEN.call_args.args[0].full_url, f"{pb.MEMORY_SERVER}/embed")


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_once_and_dedups(self):
        pb.STATE_FILE.unlink(missing_ok=True)
        m = [{"text": "router config notes", "source": "github", "similarity": 0.9}]
        ops, mem, _ = _run(chat=[LONG], matches=m)
        self.assertEqual(pb.notify.call_count, 1)
        self.assertTrue(pb.notify.call_args.args[0].startswith("Proactive Brief — Re: We should revisit"))
        self.assertTrue(ops.closed and mem.closed)
        self.assertEqual(len(json.loads(pb.STATE_FILE.read_text())["briefed"]), 1)
        _run(chat=[LONG], matches=m)
        pb.notify.assert_not_called()

    def test_caps_briefings_and_skips_outside_hours(self):
        pb.STATE_FILE.unlink(missing_ok=True)
        _run(chat=[f"{LONG} {i}" for i in range(6)], matches=[{"text": "x", "source": "github", "similarity": 0.9}])
        self.assertEqual(pb.notify.call_count, pb.MAX_BRIEFINGS_PER_RUN)
        with _hour(22), patch.object(pb.asyncpg, "create_pool", AsyncMock()) as cp:
            asyncio.run(pb.run())
        cp.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys;sys.path.insert(0,'.');import psycopg2;"
                "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "import importlib.util as u;s=u.spec_from_file_location('m','nova_proactive_brief.py');"
                "m=u.module_from_spec(s);s.loader.exec_module(m);print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
