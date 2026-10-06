#!/usr/bin/env python3
"""Tests for nova_chatroom_sessions.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The module creates a log FileHandler at import, so it is loaded with Path.home -> tempdir and
logging.basicConfig stubbed; asyncpg.connect and aiohttp.ClientSession are mocked throughout."""
import asyncio
import importlib.util
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_chatroom_sessions.py"
SRC = PATH.read_text()
_TD = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("chatroom_sessions_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", return_value=Path(_TD.name)), patch.object(logging, "basicConfig"):
        spec.loader.exec_module(mod)
    return mod


cs = _load()
_PATCHES = []


def setUpModule():
    cs.log.disabled = True
    for p in (patch.object(cs.asyncpg, "connect", AsyncMock(side_effect=AssertionError("unmocked PG"))),
              patch.object(cs.aiohttp, "ClientSession", side_effect=AssertionError("unmocked HTTP")),
              patch.object(cs.nova_config, "post_both")):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    cs.log.disabled = False
    while _PATCHES:
        _PATCHES.pop().stop()


T0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def _msgs(n, start=1):
    return [{"id": start + i, "sender": "jordan" if i % 2 else "nova", "message": f"m{i}", "created_at": T0}
            for i in range(n)]


def _conn(count=25, last_id=0, msgs=None, rows=None):
    c = MagicMock()
    c.execute = AsyncMock(); c.close = AsyncMock()
    c.fetchval = AsyncMock(side_effect=[last_id, count])
    c.fetch = AsyncMock(return_value=msgs if msgs is not None else (rows or []))
    return c


class _Resp:
    def __init__(self, status=200, data=None):
        self.status, self.data = status, data or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.data

    async def text(self):
        return "err"


class _Session:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.posts = resp, exc, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if self.exc:
            raise self.exc
        return self.resp


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_queries_use_positional_params(self):
        evil = "general'; SELECT pg_sleep(9);--"
        conn = _conn(count=0)
        asyncio.run(cs.get_new_message_count(conn, evil))
        sql, arg = conn.fetchval.call_args_list[0].args
        self.assertNotIn(evil, sql)
        self.assertIn("$1", sql)
        self.assertEqual(arg, evil)
        self.assertIsNone(re.search(r'(execute|fetch\w*)\(\s*f["\']', SRC))


class TestPerformance(unittest.TestCase):
    def test_parse_10k_responses_fast(self):
        txt = "<think>x</think>SUMMARY: talked about backups.\nTOPICS: pg, replicas, coffee"
        t0 = time.perf_counter()
        for _ in range(10_000):
            cs.parse_summary_response(txt)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_ollama_error_fails_open(self):
        # RETRY GAP: summarize_with_ollama() — one POST; any error returns None and the next message retries
        s = _Session(exc=OSError("ollama down"))
        with patch.object(cs.aiohttp, "ClientSession", return_value=s):
            self.assertIsNone(asyncio.run(cs.summarize_with_ollama(_msgs(2))))
        self.assertEqual(len(s.posts), 1)

    def test_non_200_returns_none(self):
        with patch.object(cs.aiohttp, "ClientSession", return_value=_Session(_Resp(500))):
            self.assertIsNone(asyncio.run(cs.summarize_with_ollama(_msgs(1))))

    def test_db_error_inside_summarize_returns_false_and_closes(self):
        conn = _conn(); conn.fetchval = AsyncMock(side_effect=RuntimeError("pg gone"))
        with patch.object(cs.asyncpg, "connect", AsyncMock(return_value=conn)):
            self.assertFalse(asyncio.run(cs.maybe_summarize()))
        conn.close.assert_awaited_once()


class TestUnit(unittest.TestCase):
    def test_parse_structured(self):
        s, t = cs.parse_summary_response("SUMMARY: We fixed PG.\nTOPICS: pg, - replicas, •coffee, a, b, c")
        self.assertEqual(s, "We fixed PG.")
        self.assertEqual(len(t), 5)
        self.assertEqual(t[0], "pg")

    def test_parse_unstructured_and_empty(self):
        s, t = cs.parse_summary_response("Line one\nline two\nline three")
        self.assertEqual((s, t), ("Line one", ["line two", "line three"]))
        self.assertEqual(cs.parse_summary_response(""), ("", []))
        self.assertEqual(len(cs.parse_summary_response("x" * 5000)[0]), 1000)

    def test_strip_think(self):
        self.assertEqual(cs.strip_think_tags("<think>a\nb</think> ok"), "ok")


class TestIntegration(unittest.TestCase):
    def test_ollama_payload_shape(self):
        s = _Session(_Resp(200, {"message": {"content": "SUMMARY: x"}}))
        with patch.object(cs.aiohttp, "ClientSession", return_value=s):
            self.assertEqual(asyncio.run(cs.summarize_with_ollama(_msgs(2))), "SUMMARY: x")
        url, payload = s.posts[0]
        self.assertEqual((url, payload["model"], payload["stream"]), (cs.OLLAMA_URL, cs.OLLAMA_MODEL, False))
        self.assertIn("[10:00] nova: m0", payload["messages"][1]["content"])

    def test_session_context_chronological(self):
        rows = [{"summary": "newer", "key_topics": ["b"], "message_count": 20, "ended_at": T0},
                {"summary": "older", "key_topics": None, "message_count": 21, "ended_at": None}]
        conn = _conn(rows=rows)
        with patch.object(cs.asyncpg, "connect", AsyncMock(return_value=conn)):
            out = asyncio.run(cs.get_session_context())
        lines = out.splitlines()
        self.assertEqual(lines[0], "Recent conversation context:")
        self.assertIn("[unknown] (21 msgs) older", lines[1])
        self.assertTrue(lines[2].endswith("newer Topics: b"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_stores_session(self):
        conn = _conn(count=25, last_id=10, msgs=_msgs(25, start=11))
        with patch.object(cs.asyncpg, "connect", AsyncMock(return_value=conn)), \
                patch.object(cs, "summarize_with_ollama", AsyncMock(return_value="SUMMARY: chat.\nTOPICS: a, b")):
            self.assertTrue(asyncio.run(cs.maybe_summarize()))
        sql, *args = conn.execute.call_args.args
        self.assertIn("INSERT INTO chatroom_sessions", sql)
        self.assertEqual(args[3:], ["chat.", ["a", "b"], 25, 11, 35])
        self.assertEqual(conn.fetch.call_args.args[1:], (10, 100))
        conn.close.assert_awaited_once()

    def test_below_threshold_skips_llm(self):
        conn = _conn(count=3)
        with patch.object(cs.asyncpg, "connect", AsyncMock(return_value=conn)), \
                patch.object(cs, "summarize_with_ollama", AsyncMock()) as llm:
            self.assertFalse(asyncio.run(cs.maybe_summarize()))
        llm.assert_not_awaited()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_chatroom_sessions"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "HOME": home, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)
        self.assertNotIn("Checking for unsummarized", r.stderr)


if __name__ == "__main__":
    unittest.main()
