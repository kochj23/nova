"""
nova_gateway.main — Entry point. Creates context, launches all tasks, handles shutdown.

Written by Jordan Koch.
"""

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

# Ensure the scripts directory is on sys.path so nova_config is importable
_scripts_dir = str(Path(__file__).parent.parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import httpx

from nova_gateway.config import (
    VERSION, SLACK_NOTIFY_CHANNEL, load_tokens,
)
from nova_gateway.context import GatewayContext
from nova_gateway.router import ModelRouter
from nova_gateway.session import ensure_pg_schema
from nova_gateway.health import health_server, reload_config
from nova_gateway.channels.slack import run_slack, slack_post_message
from nova_gateway.channels.discord import run_discord
from nova_gateway.channels.signal import run_signal
from nova_gateway.channels.claude import run_claude_channel

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path.home() / ".openclaw/logs/nova_gateway_v2.log"),
    ],
)
log = logging.getLogger("nova_gateway_v2")


async def _post_startup_slack(ctx: GatewayContext):
    """Post startup notification to #nova-notifications via REST API."""
    bot_token = ctx.tokens.get("slack_bot", "")
    if not bot_token:
        return
    try:
        router_status = await ctx.router.status(ctx=ctx)
        healthy_backends = [n for n, s in router_status.items()
                           if isinstance(s, dict) and s.get("healthy")]
        await slack_post_message(
            ctx,
            bot_token,
            SLACK_NOTIFY_CHANNEL,
            (
                f":rocket: *Nova Gateway v{VERSION} started*\n"
                f"  Channels: Slack (Socket Mode) + Discord (Gateway WS) + Signal + Claude Code\n"
                f"  Routing: Ollama -> MLX -> llama.cpp -> OpenRouter (auto-failover)\n"
                f"  Backends UP: {', '.join(healthy_backends) or 'checking...'}\n"
                f"  Memory: 1.48M vectors . PG bootstrap\n"
                f"  No Node.js dependency — pure Python"
            ),
        )
    except Exception:
        pass


async def main():
    log.info(f"Nova Gateway v{VERSION} starting...")

    # Load secrets from Keychain
    tokens = load_tokens()
    missing = [k for k, v in tokens.items() if not v and k != "openrouter"]
    if missing:
        log.warning(f"Missing tokens: {missing} — those channels will be disabled")

    # Create shared context
    ctx = GatewayContext(
        tokens=tokens,
        router=ModelRouter(),
        start_time=time.time(),
        startup_time=time.time(),
    )

    # Init HTTP client
    ctx.http = httpx.AsyncClient(timeout=60.0, follow_redirects=True)

    # Init PG and ensure schema
    try:
        await ensure_pg_schema(ctx)
    except Exception as e:
        log.warning(f"PG schema setup failed: {e} — continuing without PG logging")

    # Signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: ctx.shutdown.set())
    loop.add_signal_handler(
        signal.SIGHUP,
        lambda: asyncio.ensure_future(reload_config(ctx))
    )

    # Start health API
    await health_server(ctx)

    # Start all channels concurrently
    tasks = [
        asyncio.create_task(run_slack(ctx),          name="slack"),
        asyncio.create_task(run_discord(ctx),        name="discord"),
        asyncio.create_task(run_signal(ctx),         name="signal"),
        asyncio.create_task(run_claude_channel(ctx), name="claude-code"),
    ]

    log.info("All channel tasks launched — gateway running (incl. Claude Code bridge)")

    # Post startup notification to Slack
    await _post_startup_slack(ctx)

    # Wait for shutdown
    await ctx.shutdown.wait()
    log.info("Shutdown signal received — stopping channels")

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if ctx.pg_pool:
        await ctx.pg_pool.close()
    if ctx.http:
        await ctx.http.aclose()

    log.info("Nova Gateway v2 stopped cleanly")
