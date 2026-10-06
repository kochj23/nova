#!/usr/bin/env python3
"""Tests for nova_gateway/channels/slack.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Slack HTTP, the PG pool, the agent and the WebSocket are all
mocked; no network and no daemon loop (the reconnect loop is driven and stopped by a fake sleep).
Written by Jordan Koch (via Claude)."""
import asyncio
import collections
import importlib
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

sl = importlib.import_module("nova_gateway.channels.slack")
SRC = (SCRIPTS / "nova_gateway" / "channels" / "slack.py").read_text()
JORDAN = "U049EPC2W"
GUEST = "U000GUEST"


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class _Pool:
    def __init__(self, prompt_hit=None):
        self.prompt_hit = prompt_hit; self.executed = []

    async def fetchval(self, sql, *a):
        return self.prompt_hit

    async def execute(self, sql, *a):
        self.executed.append((sql, a))


class _Ctx:
    def __init__(self, http_data=None):
        self.http = types.SimpleNamespace(post=mock.AsyncMock(return_value=_Resp(http_data or {"ok": True})))
        self.channel_locks = collections.defaultdict(asyncio.Lock)
        self.shutdown = types.SimpleNamespace(_stop=False, is_set=lambda: self.shutdown._stop)
        self.tokens = {"slack_bot": "xoxb-test", "slack_app": "xapp-test"}


def _drive(ctx, event, pool, agent_reply="hi there", user_mods=None):
    """Run _slack_handle_event and wait for the background task it spawns."""
    posts = []

    async def _post(c, tok, ch, text, thread_ts=""):
        posts.append((ch, text, thread_ts))

    async def go():
        await sl._slack_handle_event(ctx, event, "UBOT", "xoxb-test")
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

    mods = user_mods or {"nova_spatial_query": types.SimpleNamespace(answer=lambda t: None),
                         "nova_status_query": types.SimpleNamespace(answer=lambda t: None)}
    agent = mock.AsyncMock(return_value=agent_reply)
    with mock.patch.object(sl, "get_pg", new=mock.AsyncMock(return_value=pool)), \
         mock.patch.object(sl, "run_agent", new=agent), \
         mock.patch.object(sl, "slack_post_message", new=_post), \
         mock.patch.dict(sys.modules, mods):
        asyncio.run(go())
    return posts, agent


def _ev(text, user=GUEST, channel=None, **kw):
    return {"type": "message", "text": text, "user": user, "channel": channel or sl.SLACK_CHAT_CHANNEL,
            "ts": "1.1", **kw}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bpa]-\d")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'(execute|fetchval)\(\s*f"')
        self.assertIn("WHERE ts = $1", SRC)

    def test_guest_cannot_trigger_cloud_executor(self):
        pool = _Pool()
        posts, agent = _drive(_Ctx(), _ev("restart the gateway please", user=GUEST), pool)
        self.assertEqual(pool.executed, [])          # no to_claude_code row for a guest
        agent.assert_awaited_once()                  # falls through to plain chat

    def test_bot_and_unlisted_channel_ignored(self):
        for ev in (_ev("hello", user="UBOT"), _ev("hello", bot_id="B1"), _ev("hello", subtype="message_changed"),
                   _ev("hello", channel="C_UNLISTED"), _ev("hello", channel=sl.SLACK_NOTIFY_CHANNEL)):
            posts, agent = _drive(_Ctx(), ev, _Pool())
            self.assertEqual(posts, [])
            agent.assert_not_awaited()


class TestPerformance(unittest.TestCase):
    def test_action_classifier_10k_fast(self):
        msgs = ["nova, please can you restart ollama", "how was your day?", "lol", "check the disks"] * 2500
        t0 = time.perf_counter()
        n = sum(sl._is_action_request(m) for m in msgs)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(n, 5000)


class TestRetry(unittest.TestCase):
    def test_reconnect_uses_exponential_backoff(self):
        ctx = _Ctx()
        sleeps = []

        async def _sleep(s):
            sleeps.append(s)
            if len(sleeps) == 3:
                ctx.shutdown._stop = True

        with mock.patch.object(sl, "_slack_get_bot_user_id", new=mock.AsyncMock(return_value="UBOT")), \
             mock.patch.object(sl, "_slack_get_ws_url", new=mock.AsyncMock(side_effect=RuntimeError("no url"))) as url, \
             mock.patch.object(sl.asyncio, "sleep", new=_sleep):
            asyncio.run(sl.run_slack(ctx))
        self.assertEqual(sleeps, [1, 2, 4])
        self.assertEqual(url.await_count, 3)

    def test_post_failure_fails_open(self):
        ctx = _Ctx()
        ctx.http.post = mock.AsyncMock(side_effect=OSError("down"))
        self.assertIsNone(asyncio.run(sl.slack_post_message(ctx, "t", "C1", "x")))
        self.assertEqual(asyncio.run(sl._slack_get_bot_user_id(ctx, "t")), "")


class TestUnit(unittest.TestCase):
    def test_is_action_request_edges(self):
        self.assertFalse(sl._is_action_request(""))
        self.assertFalse(sl._is_action_request(None))
        self.assertFalse(sl._is_action_request("run"))            # under 8 chars
        self.assertTrue(sl._is_action_request("hey nova, please can you deploy the fix"))
        self.assertFalse(sl._is_action_request("what a lovely afternoon it is"))
        self.assertIn("Claude Code", sl._action_ack("x"))

    def test_ws_url_error_raises(self):
        ctx = _Ctx({"ok": False, "error": "invalid_auth"})
        with self.assertRaisesRegex(RuntimeError, "invalid_auth"):
            asyncio.run(sl._slack_get_ws_url(ctx, "xapp"))

    def test_missing_tokens_disable_channel(self):
        ctx = _Ctx(); ctx.tokens = {}
        self.assertIsNone(asyncio.run(sl.run_slack(ctx)))
        ctx.http.post.assert_not_awaited()


class TestIntegration(unittest.TestCase):
    def test_uses_shared_gateway_helpers(self):
        import nova_gateway.config as cfg
        import nova_gateway.session as ses
        self.assertIs(sl.get_pg, ses.get_pg)
        self.assertEqual(sl._SLACK_LISTEN_CHANNELS,
                         {cfg.SLACK_CHAT_CHANNEL, cfg.SLACK_CLAUDE_CHANNEL, cfg.JORDAN_DM_CHANNEL})

    def test_post_message_shape(self):
        ctx = _Ctx()
        asyncio.run(sl.slack_post_message(ctx, "tok", "C1", "hello", thread_ts="9.9"))
        args, kw = ctx.http.post.call_args
        self.assertEqual(args[0], "https://slack.com/api/chat.postMessage")
        self.assertEqual(kw["json"], {"channel": "C1", "text": "hello", "mrkdwn": True, "thread_ts": "9.9"})
        self.assertEqual(kw["headers"]["Authorization"], "Bearer tok")


class TestFunctional(unittest.TestCase):
    def test_chat_golden_path_posts_agent_reply_in_thread(self):
        posts, agent = _drive(_Ctx(), _ev("how is the weather looking"), _Pool(), agent_reply="Sunny.")
        self.assertEqual(posts, [(sl.SLACK_CHAT_CHANNEL, "Sunny.", "1.1")])
        self.assertEqual(agent.call_args[0][1], "how is the weather looking")

    def test_jordan_action_routes_to_claude_code(self):
        pool = _Pool()
        posts, agent = _drive(_Ctx(), _ev("please restart the memory server", user=JORDAN), pool)
        self.assertIn("to_claude_code", pool.executed[0][0])
        self.assertIn("slack-action-router", pool.executed[0][1][2])
        agent.assert_not_awaited()
        self.assertIn("Claude Code", posts[0][1])

    def test_nova_claude_channel_and_prompt_threads(self):
        pool = _Pool()
        posts, agent = _drive(_Ctx(), _ev("hello claude", channel=sl.SLACK_CLAUDE_CHANNEL), pool)
        self.assertIn("claude_messages", pool.executed[0][0])
        self.assertEqual(posts, [])
        posts, agent = _drive(_Ctx(), _ev("Yes", thread_ts="5.5"), _Pool(prompt_hit=1))
        self.assertEqual(posts, [])
        agent.assert_not_awaited()

    def test_agent_error_posts_apology(self):
        async def boom(*a, **k):
            raise RuntimeError("ollama down")
        posts, _ = _drive(_Ctx(), _ev("tell me a story"), _Pool(), agent_reply=None)
        self.assertIn("rephrase", posts[0][1])               # empty reply fallback
        with mock.patch.object(sl, "run_agent", new=boom):
            ctx = _Ctx(); got = []

            async def _post(c, tok, ch, text, thread_ts=""):
                got.append(text)

            async def go():
                with mock.patch.object(sl, "get_pg", new=mock.AsyncMock(return_value=_Pool())), \
                     mock.patch.object(sl, "slack_post_message", new=_post):
                    await sl._slack_handle_event(ctx, _ev("tell me a story"), "UBOT", "x")
                    await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
            asyncio.run(go())
        self.assertIn("something went wrong", got[0])


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_gateway.channels.slack"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
