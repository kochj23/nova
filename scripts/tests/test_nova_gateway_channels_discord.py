#!/usr/bin/env python3
"""Tests for nova_gateway/channels/discord.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Discord REST (ctx.http), the websocket and the agent are mocked;
the gateway loop runs over a fake socket and stops via ctx.shutdown — no connection is ever opened.
Written by Jordan Koch (via Claude)."""
import asyncio
import collections
import importlib
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

dc = importlib.import_module("nova_gateway.channels.discord")
SRC = (SCRIPTS / "nova_gateway" / "channels" / "discord.py").read_text()
GUILD, CHAN = dc.DISCORD_GUILD_ID, dc.DISCORD_CHAT_CHANNEL
_real_sleep = asyncio.sleep


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status; self._d = data or {}; self.text = json.dumps(self._d)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


class _Ctx:
    def __init__(self, post=None, get=None, token="tkn-test"):
        self.http = types.SimpleNamespace(post=post or mock.AsyncMock(return_value=_Resp()),
                                          get=get or mock.AsyncMock(return_value=_Resp(data={"url": "wss://gw.test"})))
        self.channel_locks = collections.defaultdict(asyncio.Lock)
        self.shutdown = asyncio.Event()
        self.tokens = {"discord": token}


class _WS:
    """Fake gateway socket: yields the scripted frames, records sends, sets shutdown when drained."""
    def __init__(self, ctx, frames):
        self.ctx, self.frames, self.sent, self.closed = ctx, list(frames), [], []

    def __aiter__(self):
        return self

    async def __anext__(self):
        await _real_sleep(0)
        if self.frames:
            f = self.frames.pop(0)
            return f if isinstance(f, str) else json.dumps(f)
        self.ctx.shutdown.set()
        raise StopAsyncIteration

    async def send(self, p): self.sent.append(json.loads(p))
    async def close(self, code=1000, reason=""): self.closed.append(code)
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False


def _msg(text="hi nova", author_id="42", bot=False, guild=GUILD, chan=CHAN):
    return {"author": {"id": author_id, "bot": bot, "username": "jordan"}, "guild_id": str(guild),
            "channel_id": str(chan), "content": text}


async def _drain():
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _gateway(frames_per_conn, get=None, agent_reply="hello back"):
    """Run run_discord over one fake socket per connection; return (ctx, sockets, agent mock, sleeps)."""
    async def go():
        ctx = _Ctx(get=get)
        socks = [_WS(ctx, f) for f in frames_per_conn]
        it = iter(socks)
        ws_mod = types.ModuleType("websockets"); ws_mod.connect = lambda *a, **k: next(it)
        sleeps = []
        async def fast_sleep(s, *a):
            sleeps.append(s); await _real_sleep(0)
        ag = mock.AsyncMock(return_value=agent_reply)
        with mock.patch.dict(sys.modules, {"websockets": ws_mod}), mock.patch.object(dc, "run_agent", ag), \
             mock.patch.object(dc.asyncio, "sleep", fast_sleep):
            await dc.run_discord(ctx)
            await _drain()
        return ctx, socks, ag, sleeps
    return asyncio.run(go())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/._-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('ctx.tokens.get("discord"', SRC)            # token comes from the gateway's secret store

    def test_ignores_bots_self_and_foreign_channels(self):
        async def go(d):
            ctx = _Ctx()
            with mock.patch.object(dc, "run_agent", mock.AsyncMock(return_value="x")) as ag:
                await dc._discord_handle_message(ctx, d, "999", "t")
                await _drain()
            return ag
        for d in (_msg(author_id="999"), _msg(bot=True), _msg(guild=1), _msg(chan=2), _msg(text="   ")):
            asyncio.run(go(d)).assert_not_called()

    def test_missing_token_disables_channel(self):
        async def go():
            ctx = _Ctx(token="")
            await dc.run_discord(ctx)
            return ctx
        ctx = asyncio.run(go())
        ctx.http.get.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_split_large_message(self):
        text = "Sentence number one. " * 10_000
        t0 = time.perf_counter()
        chunks = dc._split_message(text, 1900)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertTrue(all(len(c) <= 1900 for c in chunks))
        self.assertEqual(sum(c.count("Sentence") for c in chunks), 10_000)


class TestRetry(unittest.TestCase):
    def test_gateway_reconnects_with_exponential_backoff(self):
        get = mock.AsyncMock(side_effect=[RuntimeError("dns"), RuntimeError("dns"), _Resp(data={"url": "wss://gw"})])
        ctx, socks, _, sleeps = _gateway([[{"op": 10, "d": {"heartbeat_interval": 10}}]], get=get)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleeps[:2], [1, 2])                       # backoff doubles
        self.assertEqual(socks[0].sent[0]["op"], dc._DISCORD_OP_IDENTIFY)

    def test_rate_limit_retries_once(self):
        post = mock.AsyncMock(side_effect=[_Resp(429, {"retry_after": 0.01}), _Resp(200)])
        asyncio.run(dc.discord_send_message(_Ctx(post=post), "t", CHAN, "hi"))
        self.assertEqual(post.call_count, 2)

    def test_send_exception_is_logged_not_raised(self):
        post = mock.AsyncMock(side_effect=OSError("down"))
        asyncio.run(dc.discord_send_message(_Ctx(post=post), "t", CHAN, "hi"))
        self.assertEqual(post.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_split_edges(self):
        self.assertEqual(dc._split_message(""), [""])
        self.assertEqual(dc._split_message("short"), ["short"])
        self.assertEqual(dc._split_message("x" * 50, 20), ["x" * 20, "x" * 20, "x" * 10])

    def test_intents_and_opcodes(self):
        self.assertEqual(dc._DISCORD_INTENTS, 33281)
        self.assertEqual((dc._DISCORD_OP_HELLO, dc._DISCORD_OP_IDENTIFY, dc._DISCORD_OP_RESUME), (10, 2, 6))


class TestIntegration(unittest.TestCase):
    def test_agent_gets_stable_session_and_chat_agent(self):
        async def go():
            ctx = _Ctx()
            with mock.patch.object(dc, "run_agent", mock.AsyncMock(return_value="ok")) as ag:
                await dc._discord_handle_message(ctx, _msg("status?"), "999", "t")
                await _drain()
            return ctx, ag
        ctx, ag = asyncio.run(go())
        args = ag.call_args[0]
        self.assertEqual(args[1:4], ("status?", f"gw2:discord:{CHAN}", "chat"))
        urls = [c.args[0] for c in ctx.http.post.call_args_list]
        self.assertTrue(urls[0].endswith("/typing") and urls[1].endswith("/messages"))

    def test_resume_after_ready(self):
        ready = {"op": 0, "t": "READY", "s": 5, "d": {"session_id": "S1", "resume_gateway_url": "wss://r",
                                                       "user": {"id": "999"}}}
        frames1 = [{"op": 10, "d": {"heartbeat_interval": 10}}, ready, {"op": 7}]
        frames2 = [{"op": 10, "d": {"heartbeat_interval": 10}}]
        ctx, socks, _, _ = _gateway([frames1, frames2])
        self.assertIn(4000, socks[0].closed)
        resume = [s for s in socks[1].sent if s["op"] == dc._DISCORD_OP_RESUME]
        self.assertEqual(resume[0]["d"]["session_id"], "S1")
        self.assertEqual(ctx.http.get.call_count, 1)               # resume URL reused, no second lookup


class TestFunctional(unittest.TestCase):
    def test_golden_path_message_reply(self):
        frames = [{"op": 10, "d": {"heartbeat_interval": 10}},
                  {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "S", "user": {"id": "999"}}},
                  "not json", {"op": 0, "t": "MESSAGE_CREATE", "s": 2, "d": _msg("hello")}]
        ctx, _, ag, _ = _gateway([frames])
        ag.assert_called_once()
        sent = [c.kwargs["json"]["content"] for c in ctx.http.post.call_args_list if "json" in c.kwargs]
        self.assertEqual(sent, ["hello back"])

    def test_agent_error_sends_apology(self):
        async def go():
            ctx = _Ctx()
            with mock.patch.object(dc, "run_agent", mock.AsyncMock(side_effect=RuntimeError("llm down"))):
                await dc._discord_handle_message(ctx, _msg(), "999", "t")
                await _drain()
            return ctx
        ctx = asyncio.run(go())
        self.assertIn("Something went wrong", ctx.http.post.call_args.kwargs["json"]["content"])


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_gateway.channels.discord as d; print(callable(d.run_discord))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
