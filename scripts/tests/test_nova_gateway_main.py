#!/usr/bin/env python3
"""Tests for nova_gateway/main.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

main() is driven with every collaborator mocked (tokens, router, httpx, PG schema, health server, all
four channels, Slack). No port is bound, no channel loop starts, signal handlers go to a fake loop."""
import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gateway" / "main.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="gw-main-test-"))
(TMP / ".openclaw" / "logs").mkdir(parents=True)

# main.py opens ~/.openclaw/logs/nova_gateway_v2.log at import: point HOME at a tempdir for the load
with patch.dict(os.environ, {"HOME": str(TMP), "NOVA_TEST_QUIET": "1"}):
    import nova_gateway.main  # noqa: F401  (the package re-exports main(), shadowing the submodule name)
    from nova_gateway.context import GatewayContext
gm = sys.modules["nova_gateway.main"]

CHANNELS = ("run_slack", "run_discord", "run_signal", "run_claude_channel")


class _Harness:
    """Patch every collaborator of main(); `stop_from` names the mock that sets ctx.shutdown."""
    def __init__(self, tc, standby=False, tokens=None, schema_exc=None):
        self.tc, self.ctx_seen, self.loop = tc, [], MagicMock()
        tokens = tokens if tokens is not None else {"slack_bot": "x", "discord": "y", "openrouter": ""}

        async def health(ctx):
            self.ctx_seen.append(ctx)
            if standby:
                ctx.shutdown.set()

        async def channel(ctx):
            ctx.shutdown.set()
            await asyncio.sleep(3600)        # cancelled by main() on shutdown

        self.http = MagicMock(aclose=AsyncMock())
        self.patches = [
            patch.object(gm, "GW_STANDBY", standby),
            patch.object(gm, "load_tokens", return_value=tokens),
            patch.object(gm, "ModelRouter", return_value=MagicMock()),
            patch.object(gm.httpx, "AsyncClient", return_value=self.http),
            patch.object(gm, "ensure_pg_schema", AsyncMock(side_effect=schema_exc)),
            patch.object(gm, "health_server", health),
            patch.object(gm, "_post_startup_slack", AsyncMock()),
            patch.object(gm.asyncio, "get_event_loop", return_value=self.loop),
        ] + [patch.object(gm, name, MagicMock(side_effect=channel)) for name in CHANNELS]

    def __enter__(self):
        self.mocks = [p.start() for p in self.patches]
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()

    def m(self, name):
        return getattr(gm, name)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bpa]-")

    def test_secrets_come_from_load_tokens(self):
        self.assertIn("tokens = load_tokens()", SRC)
        self.assertIn('ctx.tokens.get("slack_bot", "")', SRC)


class TestPerformance(unittest.TestCase):
    def test_startup_and_clean_shutdown_fast(self):
        t0 = time.perf_counter()
        with _Harness(self):
            asyncio.run(gm.main())
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_pg_schema_failure_fails_open(self):
        # RETRY GAP: main()/ensure_pg_schema — one attempt; failure is logged and the gateway keeps running
        with _Harness(self, schema_exc=RuntimeError("pg down")) as h, self.assertLogs("nova_gateway_v2", "WARNING") as cm:
            asyncio.run(gm.main())
        self.assertTrue(any("PG schema setup failed" in m for m in cm.output))
        self.assertEqual(len(h.ctx_seen), 1)       # health server still started

    def test_startup_slack_swallows_errors(self):
        ctx = GatewayContext(tokens={"slack_bot": "x"}, router=MagicMock(status=AsyncMock(side_effect=OSError("x"))))
        with patch.object(gm, "slack_post_message", AsyncMock()) as post:
            asyncio.run(gm._post_startup_slack(ctx))
        post.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_startup_slack_skips_without_token(self):
        ctx = GatewayContext(tokens={}, router=MagicMock())
        with patch.object(gm, "slack_post_message", AsyncMock()) as post:
            asyncio.run(gm._post_startup_slack(ctx))
        post.assert_not_called()

    def test_startup_slack_lists_healthy_backends(self):
        router = MagicMock(status=AsyncMock(return_value={"ollama": {"healthy": True}, "mlx": {"healthy": False},
                                                          "junk": "x"}))
        ctx = GatewayContext(tokens={"slack_bot": "tok"}, router=router)
        with patch.object(gm, "slack_post_message", AsyncMock()) as post:
            asyncio.run(gm._post_startup_slack(ctx))
        _, token, channel, text = post.call_args.args
        self.assertEqual((token, channel), ("tok", gm.SLACK_NOTIFY_CHANNEL))
        self.assertIn("Backends UP: ollama\n", text)

    def test_missing_tokens_warned_except_openrouter(self):
        with _Harness(self, tokens={"slack_bot": "", "openrouter": ""}), \
                self.assertLogs("nova_gateway_v2", "WARNING") as cm:
            asyncio.run(gm.main())
        warn = [m for m in cm.output if "Missing tokens" in m][0]
        self.assertIn("slack_bot", warn)
        self.assertNotIn("openrouter", warn)


class TestIntegration(unittest.TestCase):
    def test_signal_handlers_registered(self):
        import signal
        with _Harness(self) as h:
            asyncio.run(gm.main())
        sigs = [c.args[0] for c in h.loop.add_signal_handler.call_args_list]
        self.assertEqual(sigs, [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])

    def test_uses_shared_context_and_package_modules(self):
        with _Harness(self) as h:
            asyncio.run(gm.main())
        ctx = h.ctx_seen[0]
        self.assertIsInstance(ctx, GatewayContext)
        self.assertIs(ctx.http, h.http)
        self.assertIn("from nova_gateway.channels.slack import run_slack", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_launches_all_channels_and_shuts_down_clean(self):
        with _Harness(self) as h:
            calls = {}
            asyncio.run(gm.main())
            for name in CHANNELS:
                calls[name] = getattr(gm, name).call_count
            posted = gm._post_startup_slack.await_count
        self.assertEqual(set(calls.values()), {1})
        self.assertEqual(posted, 1)
        h.http.aclose.assert_awaited_once()

    def test_standby_runs_no_channels_and_no_slack(self):
        with _Harness(self, standby=True):
            asyncio.run(gm.main())
            counts = [getattr(gm, n).call_count for n in CHANNELS]
            posted = gm._post_startup_slack.await_count
        self.assertEqual(counts, [0, 0, 0, 0])
        self.assertEqual(posted, 0)


class TestFrame(unittest.TestCase):
    def test_import_never_starts_gateway(self):
        # main.py has no __main__ entry; importing it (HOME in a tempdir) must not run main()
        self.assertNotIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import sys, asyncio, nova_gateway.main; m = sys.modules['nova_gateway.main'];"
                            "print(asyncio.iscoroutinefunction(m.main))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": str(TMP), "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "True")


if __name__ == "__main__":
    unittest.main()
