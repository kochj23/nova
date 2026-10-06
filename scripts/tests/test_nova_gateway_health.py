#!/usr/bin/env python3
"""Tests for nova_gateway/health.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The PG pool, router and agent are mocked; the aiohttp site is
never started (AppRunner/TCPSite are stubbed) and the route handlers are called directly.
Written by Jordan Koch (via Claude)."""
import asyncio
import importlib
import json
import os
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

hl = importlib.import_module("nova_gateway.health")
cfg = importlib.import_module("nova_gateway.config")
SRC = (SCRIPTS / "nova_gateway" / "health.py").read_text()
_KEYS = ("OLLAMA_URL", "MLX_URL", "LLAMACPP_URL", "OPENROUTER", "RESPONSE_RESERVE", "COMPACTION_THRESHOLD",
         "SIGNAL_URL", "SIGNAL_TCP_HOST", "SIGNAL_TCP_PORT", "STARTUP_GRACE")


class _Pool:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc, self.sql = rows or [], exc, []

    def acquire(self):
        pool = self

        class _A:
            async def __aenter__(self):
                if pool.exc:
                    raise pool.exc
                return types.SimpleNamespace(fetch=pool.fetch)

            async def __aexit__(self, *a):
                return False
        return _A()

    async def fetch(self, sql, *a):
        self.sql.append(sql)
        return self.rows


def _ctx(pool=None):
    router = types.SimpleNamespace(BACKENDS=[], _health_cache={"x": 1}, HEALTH_TTL=30.0,
                                   status=mock.AsyncMock(return_value={"ollama": "up"}))
    return types.SimpleNamespace(pg_pool=pool, router=router, last_reload=0.0, sessions={"a": [], "b": []},
                                 start_time=time.time() - 50, claude_active_task=None, claude_editing_files=[],
                                 agent_disabled_until={"x": time.time() + 100, "old": time.time() - 5})


class _CfgGuard(unittest.TestCase):
    """reload_config mutates nova_gateway.config globals — snapshot and restore them."""
    def setUp(self):
        self._saved = {k: getattr(cfg, k) for k in _KEYS}
        self._limits = dict(cfg.CONTEXT_LIMITS)
        self._routing = dict(cfg.CHANNEL_AGENT)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(cfg, k, v)
        cfg.CONTEXT_LIMITS.clear(); cfg.CONTEXT_LIMITS.update(self._limits)
        cfg.CHANNEL_AGENT.clear(); cfg.CHANNEL_AGENT.update(self._routing)


def _app(ctx):
    """Build the management app without binding: capture it from AppRunner."""
    from aiohttp import web
    captured = {}

    def runner(app):
        captured["app"] = app
        return types.SimpleNamespace(setup=mock.AsyncMock())
    site = mock.MagicMock()
    site.return_value.start = mock.AsyncMock()
    with mock.patch.object(web, "AppRunner", side_effect=runner), mock.patch.object(web, "TCPSite", site):
        asyncio.run(hl.health_server(ctx))
    routes = {(r.method, r.resource.canonical): r.handler for r in captured["app"].router.routes()}
    return routes, site


class _Req:
    def __init__(self, data=None, bad=False):
        self.data, self.bad = data, bad

    async def json(self):
        if self.bad:
            raise ValueError("bad json")
        return self.data


def _call(handler, req=None):
    resp = asyncio.run(handler(req))
    return resp.status, json.loads(resp.body)


class TestSecurity(_CfgGuard):
    def test_no_hardcoded_credentials(self):
        import re
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_reload_sql_scoped_to_gateway_service(self):
        pool = _Pool([])
        asyncio.run(hl.reload_config(_ctx(pool)))
        self.assertEqual(pool.sql, ["SELECT key, value FROM service_config WHERE service = 'gateway'"])

    def test_chat_rejects_bad_and_empty_input(self):
        routes, _ = _app(_ctx())
        h = routes[("POST", "/api/chat")]
        self.assertEqual(_call(h, _Req(bad=True))[0], 400)
        self.assertEqual(_call(h, _Req({"message": "   "}))[0], 400)
        self.assertEqual(_call(routes[("POST", "/autonomy/resolve")], _Req({"pending_id": " "}))[0], 400)


class TestPerformance(_CfgGuard):
    def test_reload_large_config_fast(self):
        rows = [{"key": f"k{i}", "value": json.dumps({"v": i})} for i in range(10_000)]
        t0 = time.perf_counter()
        out = asyncio.run(hl.reload_config(_ctx(_Pool(rows))))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(out, {"ok": True, "changes": []})


class TestRetry(_CfgGuard):
    def test_pg_failure_fails_open(self):
        # RETRY GAP: reload_config — one pool.acquire attempt; error returned, config untouched
        before = cfg.OLLAMA_URL
        with self.assertLogs("nova_gateway_v2", level="ERROR"):
            out = asyncio.run(hl.reload_config(_ctx(_Pool(exc=OSError("pg down")))))
        self.assertEqual(out, {"ok": False, "error": "pg down"})
        self.assertEqual(cfg.OLLAMA_URL, before)

    def test_no_pool(self):
        self.assertEqual(asyncio.run(hl.reload_config(_ctx(None)))["error"], "PG pool not initialized")


class TestUnit(_CfgGuard):
    def test_backend_change_rebuilds_router(self):
        ctx = _ctx(_Pool([{"key": "backends", "value": {"ollama_url": "http://o:1", "health_ttl": 5}}]))
        out = asyncio.run(hl.reload_config(ctx))
        self.assertEqual(cfg.OLLAMA_URL, "http://o:1")
        self.assertEqual(ctx.router.BACKENDS[0], ("ollama", "http://o:1", "/api/tags", True))
        self.assertEqual(ctx.router._health_cache, {})
        self.assertEqual(ctx.router.HEALTH_TTL, 5.0)
        self.assertEqual(len(out["changes"]), 2)

    def test_context_signal_startup_sections(self):
        rows = [{"key": "context_limits", "value": json.dumps({"response_reserve": 1234, "zz-model": 99})},
                {"key": "signal", "value": {"url": "http://s:9", "tcp_port": 1}},
                {"key": "startup", "value": {"grace_period": 77}}]
        out = asyncio.run(hl.reload_config(_ctx(_Pool(rows))))
        self.assertEqual((cfg.RESPONSE_RESERVE, cfg.SIGNAL_URL, cfg.SIGNAL_TCP_PORT, cfg.STARTUP_GRACE),
                         (1234, "http://s:9", 1, 77))
        self.assertEqual(cfg.CONTEXT_LIMITS["zz-model"], 99)
        self.assertTrue(out["ok"])


class TestIntegration(_CfgGuard):
    def test_reload_endpoint_uses_reload_config(self):
        ctx = _ctx(_Pool([]))
        routes, _ = _app(ctx)
        status, body = _call(routes[("POST", "/reload")])
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertGreater(ctx.last_reload, 0)
        ctx.pg_pool = None
        self.assertEqual(_call(routes[("POST", "/reload")])[0], 500)

    def test_site_binds_management_port_only_via_tcpsite(self):
        _, site = _app(_ctx())
        self.assertEqual(site.call_args[0][1:], ("0.0.0.0", 18792))


class TestFunctional(_CfgGuard):
    def test_health_payload(self):
        ctx = _ctx()
        routes, _ = _app(ctx)
        with mock.patch.object(hl, "_is_degraded", mock.AsyncMock(return_value=False)):
            status, body = _call(routes[("GET", "/health")])
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], cfg.VERSION)
        self.assertEqual(body["sessions"], 2)
        self.assertGreaterEqual(body["uptime_s"], 50)
        self.assertEqual(list(body["circuit_breakers"]), ["x"])

    def test_chat_golden_and_error(self):
        routes, _ = _app(_ctx())
        h = routes[("POST", "/api/chat")]
        with mock.patch.object(hl, "run_agent", mock.AsyncMock(return_value="pong")) as ra:
            self.assertEqual(_call(h, _Req({"message": "ping"})), (200, {"ok": True, "response": "pong"}))
        self.assertEqual(ra.await_args[0][2:], ("chatroom:general", "chat"))
        with mock.patch.object(hl, "run_agent", mock.AsyncMock(side_effect=RuntimeError("boom"))), \
             self.assertLogs("nova_gateway_v2", level="ERROR"):
            self.assertEqual(_call(h, _Req({"message": "ping"}))[0], 500)


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_gateway.health as h; print(callable(h.health_server))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
