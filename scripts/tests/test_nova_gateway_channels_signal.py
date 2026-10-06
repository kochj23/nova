#!/usr/bin/env python3
"""Tests for nova_gateway/channels/signal.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). signal-cli HTTP + TCP and the agent are mocked; the listener
loop is driven by a fake stream and stopped via ctx.shutdown — no socket is ever opened.
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

sg = importlib.import_module("nova_gateway.channels.signal")
SRC = (SCRIPTS / "nova_gateway" / "channels" / "signal.py").read_text()
JORDAN = sg.JORDAN_SIGNAL
STRANGER = "+15550000000"


class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class _Ctx:
    def __init__(self, data=None):
        self.http = types.SimpleNamespace(post=mock.AsyncMock(return_value=_Resp(data or {"result": {}})))
        self.channel_locks = collections.defaultdict(asyncio.Lock)
        self.shutdown = asyncio.Event()


class _Reader:
    def __init__(self, ctx, lines):
        self.ctx, self.lines = ctx, list(lines)

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        self.ctx.shutdown.set()
        return b""


class _Writer:
    def __init__(self):
        self.sent = []

    def write(self, b):
        self.sent.append(json.loads(b))

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


def _env(sender, text, ts):
    return (json.dumps({"params": {"envelope": {"sourceNumber": sender, "timestamp": ts,
                                                 "dataMessage": {"message": text}}}}) + "\n").encode()


def _listen(lines, agent_reply="hello back", open_side=None, agent=None):
    """Run run_signal over a fake stream; return (ctx, writer, agent mock, send mock)."""
    async def go():
        ctx = _Ctx()
        w = _Writer()
        sub = (json.dumps({"result": 7}) + "\n").encode()
        conn = mock.AsyncMock(side_effect=open_side(ctx, w) if open_side else None,
                              return_value=(_Reader(ctx, [sub] + lines), w))
        ag = agent or mock.AsyncMock(return_value=agent_reply)
        send = mock.AsyncMock()
        with mock.patch.object(sg.asyncio, "open_connection", conn), mock.patch.object(sg, "run_agent", ag), \
             mock.patch.object(sg, "send_signal", send), mock.patch.object(sg.asyncio, "sleep", mock.AsyncMock()):
            await sg.run_signal(ctx)
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending)
        return ctx, w, ag, send, conn
    return asyncio.run(go())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_only_allowlisted_sender_reaches_agent(self):
        _, _, agent, send, _ = _listen([_env(STRANGER, "run rm on everything", 5), _env(JORDAN, "hi", 6)])
        self.assertEqual(agent.await_count, 1)
        self.assertEqual(agent.await_args[0][1], "hi")
        self.assertEqual(send.await_args[0][1], JORDAN)

    def test_replayed_timestamp_ignored(self):
        _, _, agent, _, _ = _listen([_env(JORDAN, "first", 10), _env(JORDAN, "replay", 10), _env(JORDAN, "old", 9)])
        self.assertEqual(agent.await_count, 1)


class TestPerformance(unittest.TestCase):
    def test_split_large_message(self):
        text = "This is a sentence. " * 5000
        t0 = time.perf_counter()
        chunks = sg._split_message(text, 1000)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("".join(chunks).count("sentence"), 5000)


class TestRetry(unittest.TestCase):
    def test_reconnects_after_refusal(self):
        attempts = []

        def open_side(ctx, w):
            def f(*a, **k):
                attempts.append(1)
                if len(attempts) < 3:
                    raise ConnectionRefusedError()
                return (_Reader(ctx, [(json.dumps({"result": 1}) + "\n").encode()]), w)
            return f
        _, w, _, _, _ = _listen([], open_side=open_side)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(w.sent[0]["method"], "subscribeReceive")

    def test_send_failure_is_logged_not_raised(self):
        # RETRY GAP: send_signal — each chunk sent once; errors are logged and swallowed
        ctx = _Ctx()
        ctx.http.post = mock.AsyncMock(side_effect=OSError("down"))
        with self.assertLogs("nova_gateway_v2", level="ERROR") as cm:
            asyncio.run(sg.send_signal(ctx, JORDAN, "x"))
        self.assertIn("Signal send failed", cm.output[0])


class TestUnit(unittest.TestCase):
    def test_split_edges(self):
        self.assertEqual(sg._split_message("short"), ["short"])
        self.assertEqual(sg._split_message("a" * 2500, 1000), ["a" * 1000, "a" * 1000, "a" * 500])
        self.assertEqual(sg._split_message("One two. Three four.", 12), ["One two.", "Three four."])

    def test_signal_rpc_payload(self):
        ctx = _Ctx({"result": "ok"})
        out = asyncio.run(sg.signal_rpc(ctx, "send", {"recipient": "r"}))
        self.assertEqual(out, {"result": "ok"})
        url = ctx.http.post.await_args[0][0]
        self.assertTrue(url.endswith("/api/v1/rpc"))
        self.assertEqual(ctx.http.post.await_args.kwargs["json"]["params"], {"recipient": "r"})


class TestIntegration(unittest.TestCase):
    def test_send_chunks_through_rpc(self):
        ctx = _Ctx({"result": {}})
        asyncio.run(sg.send_signal(ctx, JORDAN, "Sentence here. " * 150))
        self.assertGreater(ctx.http.post.await_count, 1)
        for c in ctx.http.post.await_args_list:
            self.assertEqual(c.kwargs["json"]["method"], "send")
            self.assertEqual(c.kwargs["json"]["params"]["recipient"], JORDAN)

    def test_agent_session_and_trace(self):
        _, _, agent, _, _ = _listen([_env(JORDAN, "status?", 3)])
        args = agent.await_args
        self.assertEqual(args[0][2], sg.session_id("signal", JORDAN))
        self.assertEqual(args[0][3], "chat")
        self.assertTrue(args.kwargs["trace_id"])


class TestFunctional(unittest.TestCase):
    def test_golden_path_reply(self):
        _, _, _, send, _ = _listen([_env(JORDAN, "how are you", 1)], agent_reply="fine, thanks")
        send.assert_awaited_once()
        self.assertEqual(send.await_args[0][1:], (JORDAN, "fine, thanks"))

    def test_agent_error_sends_apology_and_skips_bad_json(self):
        failing = mock.AsyncMock(side_effect=RuntimeError("llm down"))
        with self.assertLogs("nova_gateway_v2", level="ERROR"):
            _, _, _, send, _ = _listen([b"not json\n", _env(JORDAN, "boom", 1)], agent=failing)
        failing.assert_awaited_once()
        self.assertEqual(send.await_args[0][1:], (JORDAN, "Something went wrong on my end."))


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_gateway.channels.signal as s; print(callable(s.run_signal))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
