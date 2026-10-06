#!/usr/bin/env python3
"""Tests for nova_chatroom.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Loaded with Path.home() pointed at a tempdir and logging.basicConfig stubbed, so the import never
touches the real log file; the Keychain lookup at import is stubbed. asyncpg pools, aiohttp sessions,
broadcasts and subprocess execution are all mocked."""
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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_chatroom.py"
SRC = SCRIPT.read_text()
_TD = tempfile.TemporaryDirectory(prefix="chatroom_test_")


def _load():
    spec = importlib.util.spec_from_file_location("chatroom_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", return_value=Path(_TD.name)), patch.object(logging, "basicConfig"), \
         patch.dict(os.environ, {"NOVA_JORDAN_EMAILS": "jordan@example.test"}), \
         patch("subprocess.run", side_effect=RuntimeError("no keychain in tests")):
        spec.loader.exec_module(mod)
    return mod


cr = _load()
cr._session_secret = "test-secret-not-real"
cr.broadcast = AsyncMock(name="broadcast")            # never reaches a websocket
cr._pool = MagicMock(name="pool")                     # get_pool() never creates an asyncpg pool


def _req(remote="203.0.113.9", headers=None, cookies=None, json_body=None):
    r = SimpleNamespace(remote=remote, headers=headers or {}, cookies=cookies or {})
    r.json = AsyncMock(return_value=json_body or {})
    return r


class _Conn:
    def __init__(self, fetchrow=None, fetchval=None):
        self.fetchrow = AsyncMock(return_value=fetchrow)
        self.fetchval = AsyncMock(return_value=fetchval)
        self.execute = AsyncMock()


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


def _pool(conn):
    p = MagicMock(); p.acquire.return_value = _Acquire(conn)
    return p


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_personal_emails(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("kochj23" + "@" + "gmail.com", SRC)
        self.assertIn("os.environ.get(\"NOVA_JORDAN_EMAILS\"", SRC)

    def test_session_cookie_forgery_and_expiry_rejected(self):
        good = cr._make_session_cookie("guest@example.test")
        self.assertEqual(cr._validate_session_cookie(good), "guest@example.test")
        email, ts, sig = good.split("|")
        self.assertIsNone(cr._validate_session_cookie(f"jordan@example.test|{ts}|{sig}"))     # swapped identity
        old = int(time.time()) - cr.SESSION_MAX_AGE - 10
        self.assertIsNone(cr._validate_session_cookie(f"{email}|{old}|{cr._sign_session(email, old)}"))
        for junk in ("", "a|b", "a|notint|c"):
            self.assertIsNone(cr._validate_session_cookie(junk))

    def test_input_and_output_sanitizers(self):
        self.assertEqual(cr._sanitize_input("hi\x00\x07 there\n\n\n\n\n\n\nx"), "hi there\n\n\n\nx")
        self.assertEqual(len(cr._sanitize_input("a" * 9000)), cr.MAX_MESSAGE_LENGTH)
        self.assertEqual(cr._html_escape("<img src=x onerror='a'>"), "&lt;img src=x onerror=&#x27;a&#x27;&gt;")
        leak = "my salary is $200k per year and the api_key: abc123"
        self.assertNotIn("salary", cr._sanitize_response_for_external(leak, "Guest"))
        self.assertEqual(cr._sanitize_response_for_external(leak, "Jordan"), leak)

    def test_execute_refused_for_non_allowlisted_sender(self):
        ws = MagicMock(); ws.send_str = AsyncMock()
        cr._pool = _pool(_Conn(fetchrow={"sender": "Guest"}))
        with patch.object(cr.asyncio, "create_subprocess_exec") as spawn:
            asyncio.run(cr.handle_execute({"message_id": 7, "code": "import os", "language": "python"}, ws))
        spawn.assert_not_called()
        self.assertIn("Execution only allowed", ws.send_str.call_args[0][0])


class TestPerformance(unittest.TestCase):
    def test_truncate_and_rate_limit_10k_bounded(self):
        text = "\n".join(f"line {i} is unique content here" for i in range(10_000))
        t0 = time.perf_counter()
        self.assertEqual(cr._truncate_degenerate(text).count("\n"), 9_999)
        cr._api_rate_limits.clear()
        allowed = sum(cr._check_rate_limit(_req(remote=f"10.9.{i % 250}.{i % 7}")) for i in range(10_000))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertLessEqual(allowed, 10_000)


class TestRetry(unittest.TestCase):
    def test_gateway_failure_falls_back_once_to_ollama(self):
        # RETRY GAP: _get_nova_via_gateway() — one POST, no retry; any error returns None and
        # get_nova_response() falls back exactly once to direct Ollama instead of raising.
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session); session.__aexit__ = AsyncMock(return_value=False)
        session.post = MagicMock(side_effect=OSError("gateway down"))
        with patch.object(cr.aiohttp, "ClientSession", return_value=session), \
             patch.object(cr, "_get_nova_via_ollama", AsyncMock(return_value="fallback answer")) as oll:
            self.assertEqual(asyncio.run(cr.get_nova_response("hi", sender="Guest")), "fallback answer")
        self.assertEqual(session.post.call_count, 1)
        oll.assert_awaited_once_with("hi", "Guest")

    def test_queue_for_claude_failure_is_swallowed(self):
        cr._pool = MagicMock(); cr._pool.acquire.side_effect = RuntimeError("pg down")
        asyncio.run(cr._queue_for_claude("@claude fix it", "Jordan", "general"))   # logs, never raises


class TestUnit(unittest.TestCase):
    def test_should_nova_respond_and_claude_mention(self):
        self.assertTrue(cr._should_nova_respond("hey nova, what's up"))
        self.assertFalse(cr._should_nova_respond("claude can you fix this"))
        self.assertTrue(cr._should_nova_respond("good morning everyone"))
        self.assertFalse(cr._should_nova_respond("ok"))
        self.assertTrue(cr._is_claude_mention("@Claude please look"))
        self.assertFalse(cr._is_claude_mention("closed the ticket"))

    def test_truncate_degenerate(self):
        self.assertEqual(cr._truncate_degenerate("Real answer.\nI'm just a chat assistant.\nmore"), "Real answer.")
        looped = "A thing\nsame line\nsame line\nsame line\nafter"
        self.assertEqual(cr._truncate_degenerate(looped), "A thing\nsame line")

    def test_identity_and_rate_limit(self):
        self.assertEqual(cr._resolve_identity(_req(headers={"Cf-Access-Authenticated-User-Email": "Jordan@Example.test"})), "Jordan")
        self.assertEqual(cr._resolve_identity(_req(headers={"Cf-Access-Authenticated-User-Email": "jane.doe@x.test"})), "Jane Doe")
        self.assertEqual(cr._resolve_identity(_req(remote="192.168.1.50")), "Jordan")
        self.assertEqual(cr._resolve_identity(_req()), "Guest")
        cr._api_rate_limits.clear()
        r = _req(remote="198.51.100.1")
        self.assertEqual([cr._check_rate_limit(r) for _ in range(cr.API_RATE_LIMIT + 1)][-2:], [True, False])

    def test_herd_direct_mention_is_deterministic(self):
        name = next(iter(cr.HERD_MEMBERS))
        self.assertEqual(cr._pick_herd_responder(f"hey @{name.lower()} what do you think"), name)


class TestIntegration(unittest.TestCase):
    def test_queue_for_claude_uses_claude_queue_with_bound_params(self):
        conn = _Conn(fetchval=None); cr._pool = _pool(conn)
        asyncio.run(cr._queue_for_claude("@claude it's broken'); --", "Jordan", "general"))
        sql, *params = conn.execute.call_args[0]
        self.assertIn("INSERT INTO claude_queue", sql)
        self.assertNotIn("broken", sql)
        self.assertEqual(params[1], "@claude it's broken'); --")

    def test_store_message_writes_chatroom_messages(self):
        conn = _Conn(fetchrow={"id": 11, "created_at": "t"}); cr._pool = _pool(conn)
        self.assertEqual(asyncio.run(cr.store_message("Nova", "agent", "hi", channel="general")), (11, "t"))
        self.assertIn("INSERT INTO chatroom_messages", conn.fetchrow.call_args[0][0])


class TestFunctional(unittest.TestCase):
    def test_api_message_golden_path(self):
        from datetime import datetime
        cr._api_rate_limits.clear(); cr.broadcast.reset_mock()
        store = AsyncMock(return_value=(42, datetime(2026, 1, 1, 12, 0)))
        req = _req(remote="192.168.1.9", json_body={"message": "deploy done", "sender": "Claude Code", "channel": "nope"})
        with patch.object(cr, "store_message", store):
            resp = asyncio.run(cr.handle_api_message(req))
        self.assertEqual(resp.status, 200)
        self.assertEqual(store.call_args[0][:3], ("Claude Code", "agent", "deploy done"))
        self.assertEqual(store.call_args.kwargs["channel"], cr.DEFAULT_CHANNEL)        # unknown channel coerced
        self.assertEqual(cr.broadcast.call_args[0][0]["id"], 42)

    def test_api_message_unauthorized_and_empty(self):
        with patch.object(cr, "store_message", AsyncMock()) as store:
            self.assertEqual(asyncio.run(cr.handle_api_message(_req())).status, 401)
            cr._api_rate_limits.clear()
            self.assertEqual(asyncio.run(cr.handle_api_message(_req(remote="192.168.1.9", json_body={"message": "\x00"}))).status, 400)
        store.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # a bare run binds :37480 forever, so the smoke is an import in a throwaway process with HOME redirected
        home = tempfile.mkdtemp(prefix="chatroom_home_")
        r = subprocess.run([sys.executable, "-c", "import nova_chatroom as m; assert callable(m.create_app)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=60,
                           env={**os.environ, "HOME": home, "NOVA_JORDAN_EMAILS": "j@example.test", "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Starting Nova Chatroom", r.stdout + r.stderr)

    def test_create_app_registers_routes_without_binding(self):
        with patch.object(cr, "_load_or_create_session_secret", return_value="s"), \
             patch.object(cr, "FILE_STORAGE_DIR", Path(_TD.name) / "files"):
            app = cr.create_app()
        paths = {r.resource.canonical for r in app.router.routes()}
        self.assertTrue({"/api/message", "/health", "/ws", "/login"} <= paths)


if __name__ == "__main__":
    unittest.main()
