"""
nova_gateway.health — HTTP management API on port 18792 with /health, /reload, /api/chat.

Written by Jordan Koch.
"""

import json
import logging
import time

from nova_gateway.config import VERSION, STARTUP_GRACE
from nova_gateway.context import GatewayContext
from nova_gateway.agent import run_agent, _is_degraded

log = logging.getLogger("nova_gateway_v2")


async def reload_config(ctx: GatewayContext) -> dict:
    """Reload config from nova_ops.service_config. Updates globals + router backends."""
    from nova_gateway import config

    changes = []
    try:
        pool = ctx.pg_pool
        if pool is None:
            return {"ok": False, "error": "PG pool not initialized"}

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM service_config WHERE service = 'gateway'"
            )

        cfg = {}
        for row in rows:
            v = row["value"]
            cfg[row["key"]] = json.loads(v) if isinstance(v, str) else v

        if "backends" in cfg:
            b = cfg["backends"]
            new_ollama = b.get("ollama_url", config.OLLAMA_URL)
            new_mlx = b.get("mlx_url", config.MLX_URL)
            new_llamacpp = b.get("llamacpp_url", config.LLAMACPP_URL)
            new_openrouter = b.get("openrouter_url", config.OPENROUTER)

            if new_ollama != config.OLLAMA_URL:
                changes.append(f"ollama_url: {config.OLLAMA_URL} -> {new_ollama}")
                config.OLLAMA_URL = new_ollama
            if new_mlx != config.MLX_URL:
                changes.append(f"mlx_url: {config.MLX_URL} -> {new_mlx}")
                config.MLX_URL = new_mlx
            if new_llamacpp != config.LLAMACPP_URL:
                changes.append(f"llamacpp_url: {config.LLAMACPP_URL} -> {new_llamacpp}")
                config.LLAMACPP_URL = new_llamacpp
            if new_openrouter != config.OPENROUTER:
                changes.append(f"openrouter_url: {config.OPENROUTER} -> {new_openrouter}")
                config.OPENROUTER = new_openrouter

            ctx.router.BACKENDS = [
                ("ollama",     config.OLLAMA_URL,   "/api/tags",   True),
                ("mlx",        config.MLX_URL,      "/v1/models",  True),
                ("llamacpp",   config.LLAMACPP_URL, "/v1/models",  True),
                ("openrouter", config.OPENROUTER,   "/models",     False),
            ]
            ctx.router._health_cache.clear()

            new_ttl = b.get("health_ttl")
            if new_ttl and new_ttl != ctx.router.HEALTH_TTL:
                changes.append(f"health_ttl: {ctx.router.HEALTH_TTL} -> {new_ttl}")
                ctx.router.HEALTH_TTL = float(new_ttl)

        if "context_limits" in cfg:
            c = cfg["context_limits"]
            new_limits = {
                k: v for k, v in c.items()
                if k not in ("response_reserve", "compaction_threshold")
            }
            if new_limits != config.CONTEXT_LIMITS:
                changes.append(f"context_limits updated")
                config.CONTEXT_LIMITS.update(new_limits)
            if "response_reserve" in c and c["response_reserve"] != config.RESPONSE_RESERVE:
                changes.append(f"response_reserve: {config.RESPONSE_RESERVE} -> {c['response_reserve']}")
                config.RESPONSE_RESERVE = c["response_reserve"]
            if "compaction_threshold" in c and c["compaction_threshold"] != config.COMPACTION_THRESHOLD:
                changes.append(f"compaction_threshold: {config.COMPACTION_THRESHOLD} -> {c['compaction_threshold']}")
                config.COMPACTION_THRESHOLD = c["compaction_threshold"]

        if "channel_routing" in cfg:
            new_routing = cfg["channel_routing"]
            if new_routing != config.CHANNEL_AGENT:
                changes.append(f"channel_routing updated")
                config.CHANNEL_AGENT.update(new_routing)

        if "signal" in cfg:
            s = cfg["signal"]
            if s.get("url") and s["url"] != config.SIGNAL_URL:
                changes.append(f"signal_url: {config.SIGNAL_URL} -> {s['url']}")
                config.SIGNAL_URL = s["url"]
            if s.get("tcp_host") and s["tcp_host"] != config.SIGNAL_TCP_HOST:
                config.SIGNAL_TCP_HOST = s["tcp_host"]
            if s.get("tcp_port") and s["tcp_port"] != config.SIGNAL_TCP_PORT:
                config.SIGNAL_TCP_PORT = s["tcp_port"]

        if "startup" in cfg:
            gp = cfg["startup"].get("grace_period")
            if gp and gp != config.STARTUP_GRACE:
                changes.append(f"startup_grace: {config.STARTUP_GRACE} -> {gp}")
                config.STARTUP_GRACE = gp

        ctx.last_reload = time.time()
        if changes:
            log.info(f"Config reloaded: {', '.join(changes)}")
        else:
            log.info("Config reloaded (no changes)")

        return {"ok": True, "changes": changes}

    except Exception as e:
        log.error(f"Config reload failed: {e}")
        return {"ok": False, "error": str(e)}


async def health_server(ctx: GatewayContext):
    """HTTP management API on port 18792. Provides health checks and hot-reload."""
    from aiohttp import web

    async def health(_):
        router_status = await ctx.router.status(ctx=ctx)
        degraded = await _is_degraded(ctx)
        return web.json_response({
            "ok": True, "version": VERSION,
            "degraded": degraded,
            "sessions": len(ctx.sessions),
            "uptime_s": int(time.time() - ctx.start_time),
            "last_reload": ctx.last_reload,
            "backends": router_status,
            "claude_active_task": ctx.claude_active_task,
            "claude_editing": ctx.claude_editing_files,
            "circuit_breakers": {
                agent: {"disabled_until": ts, "remaining_s": int(ts - time.time())}
                for agent, ts in ctx.agent_disabled_until.items()
                if time.time() < ts
            },
        })

    async def reload(_):
        result = await reload_config(ctx)
        status = 200 if result.get("ok") else 500
        return web.json_response(result, status=status)

    async def chat_api(request):
        """POST /api/chat — Run a message through the full agent pipeline.

        Used by nova_chatroom.py to give chatroom Nova the same capabilities
        as Slack/Discord Nova (tools, web browsing, memory, function calling).

        Body: {"message": "...", "session_id": "chatroom:general", "agent_id": "chat"}
        Returns: {"ok": true, "response": "..."}
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        message = data.get("message", "").strip()
        if not message:
            return web.json_response({"ok": False, "error": "Empty message"}, status=400)

        session_id = data.get("session_id", "chatroom:general")
        agent_id = data.get("agent_id", "chat")

        try:
            response = await run_agent(ctx, message, session_id, agent_id)
            return web.json_response({"ok": True, "response": response})
        except Exception as e:
            log.error(f"Chat API error: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/reload", reload)
    app.router.add_post("/api/chat", chat_api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 18792)
    await site.start()
    log.info("Management API on 0.0.0.0:18792 (/health, POST /reload, POST /api/chat)")
