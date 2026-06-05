import asyncio
import datetime as _dt
import hashlib
import json as _json
import os
import re
import subprocess
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import asyncpg
import psutil
import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

SCHEDULER_BASE = "http://192.168.1.6:37460"
GATEWAY_HEALTH = "http://127.0.0.1:18792/health"
OLLAMA_PS = "http://192.168.1.6:11434/api/ps"
REDIS_URL = "redis://192.168.1.6:6379"
OPS_PG_DSN = "postgresql://192.168.1.6/nova_ops"
SESSIONS_JSON_ARCHIVED = Path.home() / ".openclaw" / "agents" / "_archived_sessions" / "main_sessions" / "sessions.json"
SESSIONS_JSON_ORIGINAL = Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"
SESSIONS_JSON = SESSIONS_JSON_ARCHIVED if SESSIONS_JSON_ARCHIVED.exists() else SESSIONS_JSON_ORIGINAL
PG_DB = "nova_memories"
BACKUP_LOG = Path.home() / ".openclaw" / "logs" / "nova_pg_backup.log"
PROTECT_STATE = Path.home() / ".openclaw" / "workspace" / "state" / "protect_monitor_state.json"
HOMEKIT_API = "http://127.0.0.1:37400"
MLX_MODELS_URL = "http://192.168.1.6:5050/v1/models"

AGENTS = ["analyst", "sentinel", "coder", "lookout", "librarian"]
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
AGENTS_DIR = Path.home() / ".openclaw" / "agents"

PLEX_BASE = "http://192.168.1.10:32400"
PLEX_EXCLUDED_LIBS = {"23"}
HDHR_BASE = "http://192.168.1.89"
PLEX_PLAYING_STATE = Path.home() / ".openclaw" / "workspace" / "plex_playing.json"

SERVICE_PORTS = {
    "ollama": {"port": 11434, "url": "http://192.168.1.6:11434"},
    "tinychat": {"port": 8000, "url": "http://192.168.1.6:8000"},
    "mlx_chat": {"port": 5050, "url": "http://192.168.1.6:5050"},
    "openwebui": {"port": 3000, "url": "http://192.168.1.6:3000"},
    "searxng": {"port": 8888, "url": "http://127.0.0.1:8888"},
    "swarmui": {"port": 7801, "url": "http://127.0.0.1:7801"},
    "comfyui": {"port": 8188, "url": "http://127.0.0.1:8188"},
    "memory_server": {"port": 18790, "url": "http://192.168.1.6:18790"},
    "plex": {"port": 32400, "url": PLEX_BASE, "host": "192.168.1.10"},
    "hdhr": {"port": 80, "url": HDHR_BASE, "host": "192.168.1.89"},
}

POLL_INTERVAL = 2.5
LATENCY_HISTORY_SIZE = 120  # ~5 min at 2.5s intervals
TASK_THROUGHPUT_HOURS = 24

current_state: dict = {}
connected_clients: set[WebSocket] = set()
latency_history: dict[str, deque] = {}
task_throughput_cache: list = []
task_throughput_ts: float = 0

# History tracking
_last_history_write: float = 0

# Alert tracking
_service_down_counts: dict[str, int] = {}
_cpu_high_count: int = 0

# UniFi tracking
UNIFI_API = "https://192.168.1.1/proxy/network/api"
_unifi_api_key: str | None = None
_unifi_cache: dict = {}
_unifi_ts: float = 0

# Conversation tracking
_conversations_cache: dict = {}
_conversations_ts: float = 0

# Traffic flow tracking
GATEWAY_LOG = Path.home() / ".openclaw" / "logs" / "gateway.log"
CHANNEL_PATTERN = re.compile(r"\[(slack|discord|signal|ws)\]")
_log_offset: int = 0
_prev_scheduler_runs: int = -1
_prev_ingest_depth: int = -1
_prev_task_total: int = -1

# New card collector caches
_searxng_cache: dict = {}
_searxng_ts: float = 0
_backup_cache: dict = {}
_backup_ts: float = 0
_response_time_cache: dict = {}
_response_time_ts: float = 0
_herd_cache: dict = {}
_herd_ts: float = 0
_mlx_cache: dict = {}
_mlx_ts: float = 0
_camera_cache: dict = {}
_camera_ts: float = 0
_homekit_cache: dict = {}
_homekit_ts: float = 0

# 12 new card collector caches
_app_watchdog_cache: dict = {}
_app_watchdog_ts: float = 0
_weather_cache: dict = {}
_weather_ts: float = 0
_dream_cache: dict = {}
_dream_ts: float = 0

APP_WATCHDOG_STATE = Path.home() / ".openclaw" / "workspace" / "state" / "nova_app_watchdog_state.json"
SYNOLOGY_STATE = Path.home() / ".openclaw" / "workspace" / "state" / "nova_synology_state.json"
SKY_WATCHER_STATE = Path.home() / ".openclaw" / "workspace" / "state" / "nova_sky_watcher_state.json"
WEATHER_HOMEKIT_STATE = Path.home() / ".openclaw" / "workspace" / "state" / "nova_weather_homekit_state.json"
HEALTHKIT_LOG = Path.home() / ".openclaw" / "logs" / "healthkit.log"
MEMORY_DIR = Path.home() / ".openclaw" / "workspace" / "memory"
JOURNAL_DIR = Path.home() / ".openclaw" / "workspace" / "journal"
DREAM_DIR = Path.home() / ".openclaw" / "workspace" / "journal" / "dreams"

APP_PORT_NAMES = {
    "37421": "OneOnOne",
    "37422": "MLXCode",
    "37423": "NMAPScanner",
    "37424": "RsyncGUI",
    "37432": "HomekitControl (retired → NovaControl:37400)",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _unifi_api_key

    app.state.http_session = aiohttp.ClientSession()
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=False)

    # --- History DB init (asyncpg for async-safe startup) ---
    history_pool = await asyncpg.create_pool(OPS_PG_DSN, min_size=2, max_size=5)
    app.state.history_pool = history_pool

    # Cleanup rows older than 30 days (async)
    cutoff = time.time() - (30 * 86400)
    async with history_pool.acquire() as conn:
        for tbl in ("dashboard_snapshots", "dashboard_disk_history", "dashboard_latency_history", "dashboard_memory_count_history"):
            await conn.execute(f"DELETE FROM {tbl} WHERE ts < $1", cutoff)
        cost_cutoff_30d = (_dt.datetime.now() - _dt.timedelta(days=30)).strftime("%Y-%m-%d")
        await conn.execute("DELETE FROM dashboard_cost_history WHERE date < $1", cost_cutoff_30d)

    # --- Load UniFi API key from Keychain ---
    try:
        proc = await asyncio.create_subprocess_exec(
            "security", "find-generic-password", "-s", "nova-unifi-api-key", "-w",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        _unifi_api_key = stdout.decode().strip() or None
    except Exception:
        _unifi_api_key = None

    # --- Enterprise: Create tables for incidents, SLA, alerts, capacity, RBAC ---
    async with history_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                title TEXT NOT NULL,
                root_cause TEXT,
                status TEXT DEFAULT 'open',
                severity TEXT DEFAULT 'warning',
                started_at TIMESTAMPTZ DEFAULT now(),
                resolved_at TIMESTAMPTZ,
                affected_services TEXT[] DEFAULT '{}',
                events JSONB DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS alert_rules (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name TEXT NOT NULL,
                metric TEXT NOT NULL,
                condition TEXT NOT NULL DEFAULT 'gt',
                threshold DOUBLE PRECISION NOT NULL,
                window_minutes INTEGER DEFAULT 60,
                severity TEXT DEFAULT 'warning',
                enabled BOOLEAN DEFAULT true,
                slack_notify BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT now(),
                last_triggered_at TIMESTAMPTZ
            );
            CREATE TABLE IF NOT EXISTS dashboard_users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'viewer',
                created_at TIMESTAMPTZ DEFAULT now(),
                last_login TIMESTAMPTZ
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY,
                user_id UUID,
                username TEXT,
                action TEXT NOT NULL,
                detail JSONB,
                ts TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS sla_snapshots (
                id BIGSERIAL PRIMARY KEY,
                service TEXT NOT NULL,
                ts TIMESTAMPTZ DEFAULT now(),
                up BOOLEAN NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sla_snapshots_service_ts ON sla_snapshots(service, ts);
            CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
            CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
        """)

    poll_task = asyncio.create_task(poll_loop())
    yield
    poll_task.cancel()
    await app.state.http_session.close()
    await app.state.redis.close()
    await app.state.history_pool.close()


app = FastAPI(title="Nova Control", lifespan=lifespan)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://cdn.jsdelivr.net https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self' wss: ws:; img-src 'self' data: blob:; "
        "frame-ancestors 'none'"
    )
    return response


@app.middleware("http")
async def analytics_middleware(request: Request, call_next):
    """Track page views for analytics (HTML pages only)."""
    start_time = time.time()
    response = await call_next(request)

    if request.method != "GET" or request.url.path.startswith(("/api/", "/ws", "/static/", "/health")):
        return response
    if not any(request.url.path.endswith(x) or request.url.path == "/" for x in ("", "/", "/gauges", "/hud", "/analytics", "/gauges-flat", "/gauges-v2")):
        return response

    try:
        r = app.state.redis
        ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")
        country = request.headers.get("cf-ipcountry", "")
        ua = request.headers.get("user-agent", "")
        referrer = request.headers.get("referer", "")
        elapsed_ms = int((time.time() - start_time) * 1000)

        import hashlib, secrets as _secrets
        today = time.strftime("%Y-%m-%d")
        salt_key = f"analytics:salt:{today}"
        salt = await r.get(salt_key)
        if salt and isinstance(salt, bytes):
            salt = salt.decode()
        if not salt:
            salt = _secrets.token_hex(32)
            await r.set(salt_key, salt, ex=172800)
        visitor_hash = hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:16]

        ua_lower = ua.lower()
        if any(k in ua_lower for k in ("bot", "spider", "crawl")):
            ua_bucket = "bot"
        else:
            platform = "mobile" if any(k in ua_lower for k in ("mobile", "android", "iphone")) else "desktop"
            browser = "chrome" if ("chrome" in ua_lower and "edg" not in ua_lower) else "safari" if ("safari" in ua_lower and "chrome" not in ua_lower) else "firefox" if "firefox" in ua_lower else "other"
            ua_bucket = f"{platform}-{browser}"

        ref_domain = ""
        if referrer:
            try:
                from urllib.parse import urlparse
                ref_domain = urlparse(referrer).hostname or ""
            except Exception:
                pass

        await r.xadd("analytics:events", {
            b"type": b"pageview",
            b"site": b"gauges.digitalnoise.net",
            b"path": request.url.path.encode()[:500],
            b"referrer_domain": ref_domain.encode(),
            b"country": country.encode(),
            b"ua_bucket": ua_bucket.encode(),
            b"visitor_hash": visitor_hash.encode(),
            b"ts": str(int(time.time())).encode(),
            b"response_ms": str(elapsed_ms).encode(),
        }, maxlen=10000, approximate=True)
    except Exception:
        pass

    return response


def _is_lan(request: Request) -> bool:
    """Check if request originates from LAN."""
    client = request.client
    if not client:
        return False
    ip = client.host or ""
    return ip.startswith("192.168.1.") or ip.startswith("10.0.") or ip == "127.0.0.1"


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/hud")
async def hud_page():
    return FileResponse(Path(__file__).parent / "static" / "hud.html")


@app.get("/gauges")
async def gauges_page():
    return FileResponse(Path(__file__).parent / "static" / "gauges-3d.html")


@app.get("/gauges-flat")
async def gauges_flat_page():
    return FileResponse(Path(__file__).parent / "static" / "gauges.html")


@app.get("/gauges-v2")
async def gauges_v2_page():
    return FileResponse(Path(__file__).parent / "static" / "gauges-v2.html")


@app.get("/api/detail/{service}")
async def service_detail(service: str):
    try:
        if service == "postgresql":
            return JSONResponse(await _detail_postgresql())
        elif service == "redis":
            return JSONResponse(await _detail_redis())
        elif service == "ollama":
            return JSONResponse(await _detail_ollama())
        elif service == "scheduler":
            return JSONResponse(await _detail_scheduler())
        elif service == "gateway":
            return JSONResponse(await _detail_gateway())
        elif service == "system":
            return JSONResponse(await _detail_system())
        elif service == "task_history":
            return JSONResponse(await _detail_tasks())
        elif service == "memory":
            return JSONResponse(await _detail_memory())
        elif service == "model_usage":
            return JSONResponse(await _detail_model_usage())
        elif service == "agents":
            return JSONResponse(await collect_multi_agents())
        elif service.startswith("agent-"):
            agent_name = service.replace("agent-", "")
            return JSONResponse(await _detail_agent(agent_name))
        elif service in ("slack", "discord", "signal", "imessage", "email"):
            return JSONResponse(await _detail_channel(service))
        elif service == "openrouter":
            return JSONResponse(await _detail_openrouter())
        elif service in ("tinychat", "mlx_chat", "openwebui", "comfyui", "swarmui", "searxng"):
            return JSONResponse(await _detail_service(service))
        elif service == "gateway_queries":
            return JSONResponse(await collect_gateway_query_log())
        elif service == "latency":
            return JSONResponse({"services": {k: list(v) for k, v in latency_history.items()}})
        elif service == "throughput":
            return JSONResponse({"hours": await collect_task_throughput()})
        elif service == "memory_server":
            return JSONResponse(await _detail_memory_server())
        elif service == "conversations":
            return JSONResponse(await collect_conversations())
        elif service == "unifi":
            return JSONResponse(await collect_unifi(app.state.http_session))
        elif service == "cost_tracker":
            return JSONResponse(await _detail_cost_tracker())
        elif service == "memory_growth":
            return JSONResponse(await _detail_memory_growth())
        elif service == "disk_usage":
            return JSONResponse(await _detail_disk_usage())
        elif service == "searxng_stats":
            return JSONResponse(await collect_searxng_stats(app.state.http_session))
        elif service == "backup_status":
            return JSONResponse(await collect_backup_status())
        elif service == "response_time":
            return JSONResponse(await collect_response_time())
        elif service == "herd_activity":
            return JSONResponse(await collect_herd_activity())
        elif service == "mlx_status":
            return JSONResponse(await collect_mlx_status(app.state.http_session))
        elif service == "cron_health":
            return JSONResponse(await _detail_cron_health())
        elif service == "token_counter":
            return JSONResponse(await _detail_token_counter())
        elif service == "cameras":
            return JSONResponse(await collect_camera_activity())
        elif service == "homekit":
            return JSONResponse(await collect_homekit(app.state.http_session))
        elif service == "app_watchdog":
            return JSONResponse(await collect_app_watchdog())
        elif service == "weather":
            return JSONResponse(await collect_weather())
        elif service == "dream":
            return JSONResponse(await collect_dream_status())
        elif service == "synology":
            return JSONResponse(await collect_synology_state())
        elif service == "healthkit_status":
            return JSONResponse(await collect_healthkit_status())
        elif service == "homebridge":
            return JSONResponse(await collect_homebridge_status())
        elif service == "plex":
            return JSONResponse(await collect_plex(app.state.http_session))
        elif service == "hdhr":
            return JSONResponse(await collect_hdhr(app.state.http_session))
        elif service == "deadman":
            sched = current_state.get("scheduler", {})
            dms = sched.get("tasks", {}).get("dead_mans_switch", {})
            return JSONResponse(dms)
        elif service == "channels":
            return JSONResponse(current_state.get("gateway", {}))
        elif service == "knowledge":
            return JSONResponse({"scheduler": current_state.get("scheduler", {}),
                                "postgresql": current_state.get("postgresql", {})})
        elif service == "briefings":
            return JSONResponse(current_state.get("scheduler", {}))
        elif service == "nmap":
            sched = current_state.get("scheduler", {})
            nmap_task = sched.get("tasks", {}).get("weekly_nmap", {})
            if nmap_task:
                return JSONResponse(nmap_task)
            # Fallback to NovaControl-sourced nmap data
            return JSONResponse(current_state.get("nmap", {"status": "no_data"}))
        elif service == "traffic":
            return JSONResponse(current_state.get("traffic_flow", {}))
        else:
            return JSONResponse({"error": f"Unknown service: {service}"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _detail_postgresql():
    queries = [
        ("today", "SELECT count(*) FROM memories WHERE created_at >= CURRENT_DATE"),
        ("yesterday", "SELECT count(*) FROM memories WHERE created_at >= CURRENT_DATE - 1 AND created_at < CURRENT_DATE"),
        ("this_week", "SELECT count(*) FROM memories WHERE created_at >= CURRENT_DATE - 7"),
        ("total", "SELECT count(*) FROM memories"),
        ("db_size", "SELECT pg_database_size(current_database())"),
        ("index_size", "SELECT pg_indexes_size('memories')"),
        ("table_size", "SELECT pg_total_relation_size('memories')"),
    ]
    result = {}
    for name, sql in queries:
        proc = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-c", sql,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        result[name] = int(stdout.decode().strip()) if stdout.decode().strip().isdigit() else stdout.decode().strip()

    proc = await asyncio.create_subprocess_exec(
        "psql", PG_DB, "-t", "-A", "-c",
        "SELECT source, count(*) FROM memories GROUP BY source ORDER BY count DESC LIMIT 15",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    result["top_sources"] = []
    for line in stdout.decode().strip().split("\n"):
        if "|" in line:
            parts = line.split("|")
            result["top_sources"].append({"source": parts[0], "count": int(parts[1])})

    proc = await asyncio.create_subprocess_exec(
        "psql", PG_DB, "-t", "-A", "-c",
        "SELECT source, count(*) FROM memories WHERE created_at >= CURRENT_DATE GROUP BY source ORDER BY count DESC LIMIT 10",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    result["today_sources"] = []
    for line in stdout.decode().strip().split("\n"):
        if "|" in line:
            parts = line.split("|")
            result["today_sources"].append({"source": parts[0], "count": int(parts[1])})

    proc = await asyncio.create_subprocess_exec(
        "psql", PG_DB, "-t", "-A", "-c",
        "SELECT date_trunc('day', created_at)::date, count(*) FROM memories WHERE created_at >= CURRENT_DATE - 7 GROUP BY 1 ORDER BY 1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    result["daily_counts"] = []
    for line in stdout.decode().strip().split("\n"):
        if "|" in line:
            parts = line.split("|")
            result["daily_counts"].append({"date": parts[0], "count": int(parts[1])})

    return result


async def _detail_redis():
    r = app.state.redis
    info_mem = await r.info("memory")
    info_cli = await r.info("clients")
    info_stats = await r.info("stats")
    info_server = await r.info("server")
    keys = await r.keys("*")
    decoded_keys = [k.decode() if isinstance(k, bytes) else k for k in keys]

    key_details = []
    for k in decoded_keys:
        ktype = await r.type(k)
        ktype = ktype.decode() if isinstance(ktype, bytes) else ktype
        size = None
        if ktype == "string":
            size = await r.strlen(k)
        elif ktype == "list":
            size = await r.llen(k)
        elif ktype == "hash":
            size = await r.hlen(k)
        elif ktype == "set":
            size = await r.scard(k)
        ttl = await r.ttl(k)
        key_details.append({"key": k, "type": ktype, "size": size, "ttl": ttl})

    return {
        "memory_used": info_mem.get("used_memory_human", "?"),
        "memory_peak": info_mem.get("used_memory_peak_human", "?"),
        "max_memory": info_mem.get("maxmemory_human", "?"),
        "connected_clients": info_cli.get("connected_clients", 0),
        "blocked_clients": info_cli.get("blocked_clients", 0),
        "total_commands": info_stats.get("total_commands_processed", 0),
        "total_connections": info_stats.get("total_connections_received", 0),
        "keyspace_hits": info_stats.get("keyspace_hits", 0),
        "keyspace_misses": info_stats.get("keyspace_misses", 0),
        "hit_rate": round(info_stats.get("keyspace_hits", 0) / max(1, info_stats.get("keyspace_hits", 0) + info_stats.get("keyspace_misses", 0)) * 100, 1),
        "uptime_seconds": info_server.get("uptime_in_seconds", 0),
        "redis_version": info_server.get("redis_version", "?"),
        "keys": key_details,
    }


async def _detail_ollama():
    session = app.state.http_session
    async with session.get("http://192.168.1.6:11434/api/tags", timeout=aiohttp.ClientTimeout(total=3)) as resp:
        tags = await resp.json()
    async with session.get("http://192.168.1.6:11434/api/ps", timeout=aiohttp.ClientTimeout(total=3)) as resp:
        ps = await resp.json()

    all_models = []
    for m in tags.get("models", []):
        all_models.append({
            "name": m.get("name"), "size_gb": round(m.get("size", 0) / 1e9, 1),
            "params": m.get("details", {}).get("parameter_size", "?"),
            "quant": m.get("details", {}).get("quantization_level", "?"),
            "family": m.get("details", {}).get("family", "?"),
            "modified": m.get("modified_at", "?")[:19],
        })
    running = []
    total_vram = 0
    for m in ps.get("models", []):
        vram = m.get("size_vram", 0)
        total_vram += vram
        running.append({
            "name": m.get("name"), "vram_gb": round(vram / 1e9, 1),
            "context_length": m.get("context_length", 0),
            "expires": m.get("expires_at", "?")[:19],
            "family": m.get("details", {}).get("family", "?"),
            "params": m.get("details", {}).get("parameter_size", "?"),
        })

    return {"all_models": all_models, "running": running, "total_vram_gb": round(total_vram / 1e9, 1), "model_count": len(all_models)}


async def _detail_scheduler():
    session = app.state.http_session
    async with session.get(f"{SCHEDULER_BASE}/status", timeout=aiohttp.ClientTimeout(total=3)) as resp:
        info = await resp.json()
    async with session.get(f"{SCHEDULER_BASE}/tasks", timeout=aiohttp.ClientTimeout(total=3)) as resp:
        tasks = await resp.json()

    task_list = []
    for name, t in sorted(tasks.items(), key=lambda x: x[1].get("run_count", 0), reverse=True):
        task_list.append({"name": name, **t})

    return {"info": info, "tasks": task_list}


async def _detail_gateway():
    lines = []
    try:
        log_path = Path.home() / ".openclaw" / "logs" / "gateway.log"
        if log_path.exists():
            with open(log_path, "r", errors="replace") as f:
                all_lines = f.readlines()
                lines = [l.rstrip() for l in all_lines[-50:]]
    except Exception:
        pass

    return {
        "recent_logs": lines,
        "current_state": current_state.get("gateway", {}),
    }


async def _detail_system():
    cpu_freq = psutil.cpu_freq()
    cpu_count = psutil.cpu_count()
    cpu_count_phys = psutil.cpu_count(logical=False)
    boot_time = psutil.boot_time()
    uptime = time.time() - boot_time
    load = os.getloadavg()

    top_procs = []
    for p in sorted(psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']),
                    key=lambda x: x.info.get('cpu_percent', 0) or 0, reverse=True)[:15]:
        info = p.info
        top_procs.append({
            "pid": info["pid"], "name": info["name"],
            "cpu": round(info.get("cpu_percent", 0) or 0, 1),
            "mem": round(info.get("memory_percent", 0) or 0, 1),
        })

    return {
        "cpu_count": cpu_count, "cpu_count_physical": cpu_count_phys,
        "cpu_freq_mhz": round(cpu_freq.current) if cpu_freq else None,
        "load_avg": [round(l, 2) for l in load],
        "uptime_seconds": int(uptime),
        "boot_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(boot_time)),
        "top_processes": top_procs,
        "current": current_state.get("system", {}),
    }


async def _detail_tasks():
    conn = await asyncpg.connect(OPS_PG_DSN)
    try:
        rows = await conn.fetch(
            "SELECT agent_id, status, COUNT(*) as cnt FROM task_runs GROUP BY agent_id, status ORDER BY cnt DESC")
        by_agent = {}
        for r in rows:
            a = r["agent_id"] or "(scheduler)"
            if a not in by_agent:
                by_agent[a] = {}
            by_agent[a][r["status"]] = r["cnt"]

        rows = await conn.fetch(
            """SELECT label, status,
                      CASE WHEN ended_at > 0 AND started_at > 0 THEN ended_at - started_at ELSE NULL END as duration,
                      created_at
               FROM task_runs ORDER BY created_at DESC LIMIT 25""")
        recent = []
        for r in rows:
            dur = r["duration"]
            recent.append({"label": r["label"] or "?", "status": r["status"], "duration_s": round(dur, 1) if dur else None, "created_at": r["created_at"]})
    finally:
        await conn.close()
    return {"by_agent": by_agent, "recent_runs": recent}


async def _detail_memory():
    r = app.state.redis
    ingest_len = await r.llen("nova:memory:ingest")

    proc = await asyncio.create_subprocess_exec(
        "psql", PG_DB, "-t", "-A", "-c",
        "SELECT count(*) FROM memories WHERE created_at >= CURRENT_DATE",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    today_count = int(stdout.decode().strip()) if stdout.decode().strip().isdigit() else 0

    proc = await asyncio.create_subprocess_exec(
        "psql", PG_DB, "-t", "-A", "-c",
        "SELECT source, count(*) FROM memories WHERE created_at >= CURRENT_DATE GROUP BY source ORDER BY count DESC LIMIT 8",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    today_sources = []
    for line in stdout.decode().strip().split("\n"):
        if "|" in line:
            parts = line.split("|")
            today_sources.append({"source": parts[0], "count": int(parts[1])})

    proc = await asyncio.create_subprocess_exec(
        "psql", PG_DB, "-t", "-A", "-c",
        "SELECT tier, count(*) FROM memories GROUP BY tier ORDER BY count DESC",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    tiers = {}
    for line in stdout.decode().strip().split("\n"):
        if "|" in line:
            parts = line.split("|")
            tiers[parts[0] or "default"] = int(parts[1])

    return {"ingest_queue": ingest_len, "today_stored": today_count, "today_sources": today_sources, "tiers": tiers}


async def _detail_model_usage():
    if not SESSIONS_JSON.exists():
        return {"sessions": [], "summary": {"total_sessions": 0, "total_cost": 0}}
    data = _json.loads(SESSIONS_JSON.read_text())
    sessions = []
    for key, val in data.items():
        if not isinstance(val, dict) or val.get("modelProvider") == "unknown":
            continue
        sessions.append({
            "key": key[:60],
            "provider": val.get("modelProvider", "?"),
            "model": val.get("model", "?"),
            "input_tokens": val.get("inputTokens", 0) or 0,
            "output_tokens": val.get("outputTokens", 0) or 0,
            "cost": val.get("estimatedCostUsd", 0) or 0,
            "label": val.get("label", ""),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(val["updatedAt"] / 1000 if val.get("updatedAt", 0) > 1e12 else val.get("updatedAt", 0))) if isinstance(val.get("updatedAt"), (int, float)) else str(val.get("updatedAt", ""))[:19],
        })
    sessions.sort(key=lambda x: x.get("updated", ""), reverse=True)
    return {"sessions": sessions[:30]}


async def _detail_agent(name):
    r = app.state.redis
    status = await r.get(f"nova:agent:{name}:status")
    if isinstance(status, bytes):
        status = status.decode()
    meta = await r.hgetall(f"nova:agent:{name}:meta")
    decoded = {}
    for k, v in meta.items():
        decoded[k.decode() if isinstance(k, bytes) else k] = v.decode() if isinstance(v, bytes) else v

    conn = await asyncpg.connect(OPS_PG_DSN)
    try:
        rows = await conn.fetch(
            """SELECT label, status,
                      CASE WHEN ended_at > 0 AND started_at > 0 THEN ended_at - started_at ELSE NULL END as duration,
                      created_at
               FROM task_runs WHERE agent_id = $1 ORDER BY created_at DESC LIMIT 15""", name)
        recent = []
        for r in rows:
            dur = r["duration"]
            recent.append({"label": r["label"] or "?", "status": r["status"], "duration_s": round(dur, 1) if dur else None, "created_at": r["created_at"]})
    finally:
        await conn.close()
    return {"status": status, "meta": decoded, "recent_tasks": recent}


async def _detail_channel(channel):
    log_path = Path.home() / ".openclaw" / "logs" / "gateway.log"
    events = []
    total_lines = 0
    tag = f"[{channel}]"
    if channel == "imessage":
        tag = "[ws]"
    elif channel == "email":
        tag = "[gmail"

    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                if tag in line:
                    total_lines += 1
                    events.append(line.rstrip())
    except Exception:
        pass

    recent = events[-40:]

    stats = {"connected": 0, "disconnected": 0, "restarts": 0, "messages_delivered": 0, "errors": 0, "other": 0}
    for line in events:
        ll = line.lower()
        if "connected" in ll and "disconnect" not in ll:
            stats["connected"] += 1
        elif "disconnect" in ll or "closed" in ll:
            stats["disconnected"] += 1
        elif "starting provider" in ll or "restart" in ll:
            stats["restarts"] += 1
        elif "delivered" in ll or "res ✓" in ll:
            stats["messages_delivered"] += 1
        elif "error" in ll or "fail" in ll:
            stats["errors"] += 1
        else:
            stats["other"] += 1

    svc_data = current_state.get("services", {})
    traffic = current_state.get("traffic_flow", {})

    return {
        "channel": channel,
        "total_log_events": total_lines,
        "stats": stats,
        "traffic_flow": traffic.get(channel, 0),
        "recent_logs": recent,
    }


async def _detail_openrouter():
    if not SESSIONS_JSON.exists():
        return {"provider": "openrouter", "total_sessions": 0, "total_input_tokens": 0, "total_output_tokens": 0, "total_cost_usd": 0, "sessions": []}
    data = _json.loads(SESSIONS_JSON.read_text())
    sessions = []
    total_in = 0
    total_out = 0
    total_cost = 0.0
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        if val.get("modelProvider") != "openrouter":
            continue
        inp = val.get("inputTokens", 0) or 0
        out = val.get("outputTokens", 0) or 0
        cost = val.get("estimatedCostUsd", 0) or 0
        total_in += inp
        total_out += out
        total_cost += cost
        updated = val.get("updatedAt", "")
        if isinstance(updated, (int, float)):
            updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated / 1000 if updated > 1e12 else updated))
        else:
            updated = str(updated)[:19]
        sessions.append({
            "key": key[:60], "model": val.get("model", "?"),
            "input_tokens": inp, "output_tokens": out,
            "cost": cost, "label": val.get("label", ""),
            "updated": updated,
        })
    sessions.sort(key=lambda x: x.get("updated", ""), reverse=True)

    return {
        "provider": "openrouter",
        "total_sessions": len(sessions),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cost_usd": round(total_cost, 6),
        "sessions": sessions[:20],
    }


async def _detail_service(name):
    svc_info = current_state.get("services", {}).get(name, {})
    latency_trend = svc_info.get("latency_trend", [])

    proc_info = {}
    port = SERVICE_PORTS.get(name, {}).get("port")
    if port:
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-t",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            pid = stdout.decode().strip().split("\n")[0]
            if pid:
                p = psutil.Process(int(pid))
                mem = p.memory_info()
                proc_info = {
                    "pid": int(pid),
                    "name": p.name(),
                    "cmdline": " ".join(p.cmdline()[:5]),
                    "cpu_percent": p.cpu_percent(),
                    "rss_mb": round(mem.rss / (1024 * 1024), 1),
                    "vms_mb": round(mem.vms / (1024 * 1024), 1),
                    "create_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.create_time())),
                    "uptime_s": int(time.time() - p.create_time()),
                    "num_threads": p.num_threads(),
                }
        except Exception:
            pass

    extra = {}
    session = app.state.http_session
    if name == "comfyui":
        try:
            async with session.get("http://127.0.0.1:8188/system_stats", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                sys_stats = await resp.json()
                si = sys_stats.get("system", {})
                extra = {
                    "comfyui_version": si.get("comfyui_version"),
                    "python_version": si.get("python_version", "")[:20],
                    "pytorch_version": si.get("pytorch_version"),
                    "ram_total_gb": round(si.get("ram_total", 0) / 1e9, 1),
                    "ram_free_gb": round(si.get("ram_free", 0) / 1e9, 1),
                }
        except Exception:
            pass

    gw_config = {
        "ollama": {"model": "qwen3-coder:30b + deepseek-r1:8b + qwen3-vl:4b", "role": "Code, reasoning, vision (local private intents)"},
        "openrouter": {"model": "Qwen3 235B MoE", "role": "Conversation, Slack, Discord, Signal"},
        "searxng": {"model": "Multi-engine aggregator", "role": "Web search (Google, Bing, DuckDuckGo)"},
        "tinychat": {"model": "qwen3-coder:30b", "role": "Lightweight chat, quick responses"},
        "mlx_chat": {"model": "Qwen2.5-32B-4bit + speculative draft", "role": "Fast general (Apple Neural Engine)"},
        "openwebui": {"model": "qwen3-coder:30b + RAG", "role": "Document grounding, retrieval, web search"},
        "swarmui": {"model": "Juggernaut XL", "role": "Image generation (Stable Diffusion)"},
        "comfyui": {"model": "Custom workflows", "role": "Advanced image pipelines"},
    }

    avg_latency = round(sum(latency_trend) / len(latency_trend)) if latency_trend else None
    max_latency = max(latency_trend) if latency_trend else None
    min_latency = min(latency_trend) if latency_trend else None

    return {
        "service": name,
        "status": svc_info.get("status", "unknown"),
        "port": svc_info.get("port"),
        "current_latency_ms": svc_info.get("latency_ms"),
        "avg_latency_ms": avg_latency,
        "min_latency_ms": min_latency,
        "max_latency_ms": max_latency,
        "latency_points": len(latency_trend),
        "gateway_config": gw_config.get(name, {}),
        "process": proc_info,
        **extra,
    }


async def _detail_memory_server():
    session = app.state.http_session
    health = {}
    stats = {}
    try:
        async with session.get("http://192.168.1.6:18790/health", timeout=aiohttp.ClientTimeout(total=3)) as resp:
            health = await resp.json()
    except Exception:
        pass
    try:
        async with session.get("http://192.168.1.6:18790/stats", timeout=aiohttp.ClientTimeout(total=3)) as resp:
            stats = await resp.json()
    except Exception:
        pass

    proc_info = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "lsof", "-iTCP:18790", "-sTCP:LISTEN", "-t",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        pid = stdout.decode().strip().split("\n")[0]
        if pid:
            p = psutil.Process(int(pid))
            mem = p.memory_info()
            proc_info = {
                "pid": int(pid), "rss_mb": round(mem.rss / (1024 * 1024), 1),
                "uptime_s": int(time.time() - p.create_time()),
                "num_threads": p.num_threads(),
            }
    except Exception:
        pass

    return {
        "health": health, "stats": stats, "process": proc_info,
    }


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # Only allow LAN connections or connections via Cloudflare Access
    client_ip = websocket.client.host if websocket.client else ""
    is_lan = client_ip.startswith("192.168.1.") or client_ip.startswith("10.0.") or client_ip == "127.0.0.1"
    cf_access = websocket.headers.get("cf-access-authenticated-user-email")
    if not is_lan and not cf_access:
        await websocket.close(code=4001, reason="Unauthorized")
        return
    await websocket.accept()
    connected_clients.add(websocket)
    try:
        if current_state:
            await websocket.send_json(current_state)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        connected_clients.discard(websocket)


# --- Collectors ---

async def collect_scheduler(session: aiohttp.ClientSession) -> dict:
    try:
        async with session.get(f"{SCHEDULER_BASE}/status", timeout=aiohttp.ClientTimeout(total=3)) as resp:
            info = await resp.json()
        async with session.get(f"{SCHEDULER_BASE}/tasks", timeout=aiohttp.ClientTimeout(total=3)) as resp:
            tasks = await resp.json()

        running_tasks = [name for name, t in tasks.items() if t.get("running")]
        failed_tasks = [name for name, t in tasks.items() if t.get("consecutive_failures", 0) > 0]

        return {
            "status": "ok",
            "info": info,
            "tasks": tasks,
            "running_tasks": running_tasks,
            "failed_tasks": failed_tasks,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def collect_agents(redis_client: aioredis.Redis) -> dict:
    agents = {}
    try:
        for name in AGENTS:
            status = await redis_client.get(f"nova:agent:{name}:status") or "unknown"
            if isinstance(status, bytes):
                status = status.decode()
            meta = await redis_client.hgetall(f"nova:agent:{name}:meta")
            decoded_meta = {}
            for k, v in meta.items():
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                decoded_meta[key] = val
            agents[name] = {
                "status": status,
                "model": decoded_meta.get("model", "unknown"),
                "tasks_completed": int(decoded_meta.get("tasks_completed", 0)),
                "uptime_s": int(float(decoded_meta.get("uptime_s", 0))),
                "last_error": decoded_meta.get("last_error", ""),
            }
        return agents
    except Exception as e:
        return {name: {"status": "unknown", "error": str(e)} for name in AGENTS}


async def collect_multi_agents() -> dict:
    """Collect status for the new multi-agent architecture (chat, research, home).

    Reads agent config from openclaw.json and checks workspace sizes,
    session counts, and channel bindings.
    """
    try:
        config = _json.loads(OPENCLAW_CONFIG.read_text())
    except Exception:
        return {"agents": [], "error": "cannot read openclaw.json"}

    agent_list = config.get("agents", {}).get("list", [])
    bindings = config.get("bindings", [])

    # Build channel map: agentId -> [channel names]
    channel_map: dict[str, list[str]] = {}
    for b in bindings:
        if isinstance(b, dict) and b.get("type") == "route":
            aid = b.get("agentId", "")
            ch = b.get("match", {}).get("channel", "")
            if aid and ch:
                channel_map.setdefault(aid, []).append(ch)

    agents_out = []
    for agent in agent_list:
        agent_id = agent.get("id", "")
        if agent_id == "main":
            continue  # Skip the default main agent

        model = agent.get("model", config.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "unknown"))
        workspace_path = agent.get("workspace", "")

        # Measure workspace size in characters
        workspace_chars = 0
        if workspace_path:
            wp = Path(workspace_path)
            if wp.exists():
                for f in wp.rglob("*"):
                    if f.is_file():
                        try:
                            workspace_chars += f.stat().st_size
                        except OSError:
                            pass

        # Count active sessions
        sessions_dir = AGENTS_DIR / agent_id / "sessions"
        active_sessions = 0
        last_message_ts = None
        if sessions_dir.exists():
            for sf in sessions_dir.iterdir():
                if sf.suffix == ".jsonl" and ".deleted" not in sf.name:
                    active_sessions += 1
                    mtime = sf.stat().st_mtime
                    if last_message_ts is None or mtime > last_message_ts:
                        last_message_ts = mtime

        # Determine status from Redis if possible
        status = "idle"
        try:
            r = app.state.redis
            raw = await r.get(f"nova:agent:{agent_id}:status")
            if raw:
                status = raw.decode() if isinstance(raw, bytes) else raw
        except Exception:
            pass

        # If no redis status but has recent activity (< 5 min), mark active
        if status == "idle" and last_message_ts:
            if time.time() - last_message_ts < 300:
                status = "active"

        agents_out.append({
            "id": agent_id,
            "name": agent.get("name", agent_id),
            "model": model,
            "workspace_chars": workspace_chars,
            "channels": channel_map.get(agent_id, []),
            "status": status,
            "active_sessions": active_sessions,
            "last_message_ts": last_message_ts,
        })

    return {"agents": agents_out}


async def collect_gateway(session: aiohttp.ClientSession) -> dict:
    try:
        async with session.get(GATEWAY_HEALTH, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            data = await resp.json()
        ws_reachable = True
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 18792), timeout=1.0
            )
            writer.close()
            await writer.wait_closed()
        except Exception:
            ws_reachable = False

        # Collect per-channel message counts from gateway_sessions + gateway_query_log
        channels = {}
        try:
            conn = await asyncpg.connect(OPS_PG_DSN)
            try:
                rows = await conn.fetch(
                    """SELECT gs.channel, COUNT(*) as cnt
                       FROM gateway_query_log gql
                       JOIN gateway_sessions gs ON gql.session_id = gs.session_id
                       WHERE gql.created_at > NOW() - INTERVAL '24 hours'
                       GROUP BY gs.channel""")
                for r in rows:
                    ch = r["channel"] or "unknown"
                    channels[ch] = r["cnt"]
            finally:
                await conn.close()
        except Exception:
            pass

        gw_status = "live" if data.get("ok") else ("degraded" if data.get("degraded") else "down")
        return {
            "status": "ok",
            "ok": data.get("ok", False),
            "gateway_status": gw_status,
            "ws_reachable": ws_reachable,
            "channels": channels,
            "version": data.get("version"),
            "backends": data.get("backends", {}),
            "sessions": data.get("sessions", 0),
            "uptime_s": data.get("uptime_s", 0),
        }
    except Exception as e:
        return {"status": "error", "ok": False, "gateway_status": "down", "ws_reachable": False, "error": str(e), "channels": {}}


async def collect_task_history() -> dict:
    try:
        conn = await asyncpg.connect(OPS_PG_DSN)
        try:
            rows = await conn.fetch("SELECT status, COUNT(*) as cnt FROM task_runs GROUP BY status")
            all_time = {r["status"]: r["cnt"] for r in rows}

            day_ago = time.time() - 86400
            rows = await conn.fetch(
                "SELECT status, COUNT(*) as cnt FROM task_runs WHERE created_at > $1 GROUP BY status",
                int(day_ago))
            last_24h = {r["status"]: r["cnt"] for r in rows}
        finally:
            await conn.close()
        return {"status": "ok", "all_time": all_time, "last_24h": last_24h}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def collect_redis_info(redis_client: aioredis.Redis) -> dict:
    try:
        db_size = await redis_client.dbsize()
        ingest_len = await redis_client.llen("nova:memory:ingest")
        return {"status": "ok", "db_size": db_size, "ingest_queue_depth": ingest_len}
    except Exception as e:
        return {"status": "error", "error": str(e), "db_size": 0, "ingest_queue_depth": 0}


async def collect_services(session: aiohttp.ClientSession) -> dict:
    results = {}

    async def check_service(name, info):
        from urllib.parse import urlparse
        parsed = urlparse(info["url"])
        host = info.get("host", parsed.hostname or "127.0.0.1")
        start = time.monotonic()
        try:
            async with session.get(info["url"], timeout=aiohttp.ClientTimeout(total=1.5)) as resp:
                latency = round((time.monotonic() - start) * 1000)
                results[name] = {"status": "up", "port": info["port"], "http_code": resp.status, "latency_ms": latency}
        except Exception:
            try:
                t0 = time.monotonic()
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, info["port"]), timeout=1.0
                )
                writer.close()
                await writer.wait_closed()
                latency = round((time.monotonic() - t0) * 1000)
                results[name] = {"status": "up", "port": info["port"], "http_code": None, "latency_ms": latency}
            except Exception:
                results[name] = {"status": "down", "port": info["port"], "latency_ms": None}

    await asyncio.gather(*[check_service(n, i) for n, i in SERVICE_PORTS.items()])

    for name, data in results.items():
        if name not in latency_history:
            latency_history[name] = deque(maxlen=LATENCY_HISTORY_SIZE)
        if data.get("latency_ms") is not None:
            latency_history[name].append(data["latency_ms"])
        data["latency_trend"] = list(latency_history.get(name, []))

    return results


async def collect_system_resources() -> dict:
    try:
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        disks = {}
        seen_devices = set()
        for part in psutil.disk_partitions():
            if part.mountpoint in ("/", "/System/Volumes/Data", "/Volumes/Data", "/Volumes/MoreData", "/Volumes/nas"):
                if part.device in seen_devices:
                    continue
                seen_devices.add(part.device)
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks[part.mountpoint] = {
                        "total_gb": round(usage.total / (1024**3), 1),
                        "used_gb": round(usage.used / (1024**3), 1),
                        "free_gb": round(usage.free / (1024**3), 1),
                        "percent": usage.percent,
                    }
                except Exception:
                    pass

        net = psutil.net_io_counters()

        return {
            "status": "ok",
            "cpu_percent": cpu_pct,
            "memory": {
                "total_gb": round(mem.total / (1024**3), 1),
                "used_gb": round(mem.used / (1024**3), 1),
                "available_gb": round(mem.available / (1024**3), 1),
                "percent": mem.percent,
            },
            "swap": {
                "total_gb": round(swap.total / (1024**3), 1),
                "used_gb": round(swap.used / (1024**3), 1),
                "percent": swap.percent,
            },
            "disks": disks,
            "network": {
                "bytes_sent": net.bytes_sent,
                "bytes_recv": net.bytes_recv,
            },
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def collect_ollama_models(session: aiohttp.ClientSession) -> dict:
    try:
        async with session.get(OLLAMA_PS, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            data = await resp.json()
        models = []
        total_vram = 0
        for m in data.get("models", []):
            vram_gb = round(m.get("size_vram", 0) / (1024**3), 1)
            total_vram += m.get("size_vram", 0)
            models.append({
                "name": m.get("name", "unknown"),
                "family": m.get("details", {}).get("family", "unknown"),
                "params": m.get("details", {}).get("parameter_size", "?"),
                "quant": m.get("details", {}).get("quantization_level", "?"),
                "vram_gb": vram_gb,
                "context_length": m.get("context_length", 0),
            })
        return {
            "status": "ok",
            "models": models,
            "total_vram_gb": round(total_vram / (1024**3), 1),
            "model_count": len(models),
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "models": [], "total_vram_gb": 0, "model_count": 0}


async def collect_postgresql() -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-c",
            "SELECT pg_database_size(current_database())",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        db_size_bytes = int(stdout.decode().strip())

        proc2 = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-c",
            "SELECT 'memories', count(*) FROM memories "
            "UNION ALL SELECT 'memory_links', count(*) FROM memory_links "
            "UNION ALL SELECT 'consolidation_runs', count(*) FROM consolidation_runs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5)
        tables = []
        total_rows = 0
        for line in stdout2.decode().strip().split("\n"):
            if "|" in line:
                parts = line.split("|")
                name = parts[0].strip()
                rows = int(parts[1].strip())
                total_rows += rows
                tables.append({"name": name, "rows": rows})

        proc3 = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-c",
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout3, _ = await asyncio.wait_for(proc3.communicate(), timeout=3)
        index_count = int(stdout3.decode().strip()) if stdout3.decode().strip() else 0

        # Today's memory stats (for HUD display)
        proc4 = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-c",
            "SELECT count(*) FROM memories WHERE created_at >= CURRENT_DATE",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout4, _ = await asyncio.wait_for(proc4.communicate(), timeout=3)
        today_count = int(stdout4.decode().strip()) if stdout4.decode().strip().isdigit() else 0

        proc5 = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-c",
            "SELECT source, count(*) FROM memories WHERE created_at >= CURRENT_DATE GROUP BY source ORDER BY count DESC LIMIT 5",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout5, _ = await asyncio.wait_for(proc5.communicate(), timeout=3)
        today_sources = []
        for line in stdout5.decode().strip().split("\n"):
            if "|" in line:
                parts = line.split("|")
                today_sources.append({"source": parts[0].strip(), "count": int(parts[1].strip())})

        return {
            "status": "ok",
            "db_size_gb": round(db_size_bytes / (1024**3), 2),
            "total_rows": total_rows,
            "tables": tables,
            "index_count": index_count,
            "today_count": today_count,
            "today_sources": today_sources,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "db_size_gb": 0, "total_rows": 0, "tables": [], "index_count": 0}


async def collect_flow_runs() -> dict:
    try:
        conn = await asyncpg.connect(OPS_PG_DSN)
        try:
            rows = await conn.fetch("SELECT status, COUNT(*) as cnt FROM flow_runs GROUP BY status")
            flows = {r["status"]: r["cnt"] for r in rows}
        finally:
            await conn.close()
        return {"status": "ok", "flows": flows}
    except Exception as e:
        return {"status": "error", "error": str(e), "flows": {}}


async def collect_task_throughput() -> list:
    global task_throughput_cache, task_throughput_ts
    now = time.time()
    if now - task_throughput_ts < 60:
        return task_throughput_cache
    try:
        cutoff_ms = int((now - (TASK_THROUGHPUT_HOURS * 3600)) * 1000)
        conn = await asyncpg.connect(OPS_PG_DSN)
        try:
            rows = await conn.fetch(
                """SELECT CAST((created_at - $1) / 3600000 AS INTEGER) as hour_bucket,
                          status, COUNT(*) as cnt
                   FROM task_runs
                   WHERE created_at > $2
                   GROUP BY hour_bucket, status
                   ORDER BY hour_bucket""",
                cutoff_ms, cutoff_ms)
        finally:
            await conn.close()
        buckets = {}
        for r in rows:
            h = int(r["hour_bucket"])
            if h not in buckets:
                buckets[h] = {"hour": h, "succeeded": 0, "failed": 0, "timed_out": 0, "lost": 0}
            if r["status"] in buckets[h]:
                buckets[h][r["status"]] = r["cnt"]
        result = [buckets.get(i, {"hour": i, "succeeded": 0, "failed": 0, "timed_out": 0, "lost": 0})
                  for i in range(TASK_THROUGHPUT_HOURS)]
        task_throughput_cache = result
        task_throughput_ts = now
        return result
    except Exception:
        return task_throughput_cache or []


_model_usage_cache: dict = {}
_model_usage_ts: float = 0


async def collect_model_usage() -> dict:
    global _model_usage_cache, _model_usage_ts
    now = time.time()
    if now - _model_usage_ts < 30:
        return _model_usage_cache
    try:
        import json as _json
        if not SESSIONS_JSON.exists():
            return _model_usage_cache or {"status": "no_data", "by_provider": {}, "by_model": {}, "total_sessions": 0, "total_cost_usd": 0, "total_tokens": 0}
        data = _json.loads(SESSIONS_JSON.read_text())

        by_provider: dict = {}
        by_model: dict = {}

        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            prov = val.get("modelProvider", "unknown")
            model = val.get("model", "unknown")
            inp = val.get("inputTokens", 0) or 0
            out = val.get("outputTokens", 0) or 0
            cost = val.get("estimatedCostUsd", 0) or 0

            if prov not in by_provider:
                by_provider[prov] = {"sessions": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
            by_provider[prov]["sessions"] += 1
            by_provider[prov]["input_tokens"] += inp
            by_provider[prov]["output_tokens"] += out
            by_provider[prov]["cost"] += cost

            if model not in by_model:
                by_model[model] = {"sessions": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0, "provider": prov}
            by_model[model]["sessions"] += 1
            by_model[model]["input_tokens"] += inp
            by_model[model]["output_tokens"] += out
            by_model[model]["cost"] += cost

        total_sessions = len(data)
        total_cost = sum(v["cost"] for v in by_provider.values())
        total_tokens = sum(v["input_tokens"] + v["output_tokens"] for v in by_provider.values())

        result = {
            "status": "ok",
            "by_provider": by_provider,
            "by_model": by_model,
            "total_sessions": total_sessions,
            "total_cost_usd": round(total_cost, 6),
            "total_tokens": total_tokens,
        }
        _model_usage_cache = result
        _model_usage_ts = now
        return result
    except Exception as e:
        return _model_usage_cache or {"status": "error", "error": str(e), "by_provider": {}, "by_model": {}, "total_sessions": 0, "total_cost_usd": 0, "total_tokens": 0}


async def collect_gateway_query_log() -> dict:
    try:
        conn = await asyncpg.connect(OPS_PG_DSN)
        try:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='gateway_query_log')")
            if not exists:
                await conn.close()
                return {"status": "empty", "backends": {}, "total_queries": 0}

            rows = await conn.fetch(
                """SELECT backend_used, model_used, COUNT(*) as cnt,
                          AVG(latency_ms) as avg_lat,
                          SUM(prompt_length) as total_prompt,
                          SUM(response_length) as total_response,
                          SUM(fallback_used::int) as fallbacks
                   FROM gateway_query_log GROUP BY backend_used, model_used""")

            last_hour = await conn.fetchval(
                "SELECT COUNT(*) FROM gateway_query_log WHERE created_at > NOW() - INTERVAL '1 hour'") or 0
            last_5m = await conn.fetchval(
                "SELECT COUNT(*) FROM gateway_query_log WHERE created_at > NOW() - INTERVAL '5 minutes'") or 0
        finally:
            await conn.close()

        backends = {}
        total_queries = 0
        for r in rows:
            total_queries += r["cnt"]
            backend = r["backend_used"]
            model = r["model_used"]
            if backend not in backends:
                backends[backend] = {"models": {}, "total_queries": 0, "total_prompt_chars": 0, "total_response_chars": 0}
            backends[backend]["total_queries"] += r["cnt"]
            backends[backend]["total_prompt_chars"] += r["total_prompt"] or 0
            backends[backend]["total_response_chars"] += r["total_response"] or 0
            backends[backend]["models"][model] = {
                "queries": r["cnt"],
                "avg_latency_ms": round(r["avg_lat"] or 0),
                "prompt_chars": r["total_prompt"] or 0,
                "response_chars": r["total_response"] or 0,
                "fallbacks": r["fallbacks"] or 0,
            }

        reqs_per_sec = round(last_hour / 3600, 2) if last_hour else 0
        reqs_per_min = round(last_5m / 5, 1) if last_5m else 0

        return {"status": "ok", "backends": backends, "total_queries": total_queries,
                "reqs_per_sec": reqs_per_sec, "reqs_per_min": reqs_per_min,
                "last_hour": last_hour}
    except Exception as e:
        return {"status": "error", "error": str(e), "backends": {}, "total_queries": 0}


def collect_traffic_flow(scheduler_data, redis_data, task_history_data, services_data) -> dict:
    global _log_offset, _prev_scheduler_runs, _prev_ingest_depth, _prev_task_total

    flow = {
        "slack": 0.0, "discord": 0.0, "signal": 0.0,
        "imessage": 0.0, "email": 0.0,
        "ollama": 0.0, "openrouter": 0.0, "mlx_chat": 0.0,
        "tinychat": 0.0, "openwebui": 0.0,
        "redis": 0.0, "postgresql": 0.0,
        "memory_server": 0.0, "scheduler": 0.0,
    }

    try:
        if GATEWAY_LOG.exists():
            size = GATEWAY_LOG.stat().st_size
            if size < _log_offset:
                _log_offset = 0
            if _log_offset == 0:
                _log_offset = size
            else:
                bytes_new = size - _log_offset
                if bytes_new > 0:
                    with open(GATEWAY_LOG, "r", errors="replace") as f:
                        f.seek(_log_offset)
                        chunk = f.read()
                    _log_offset = size

                    counts = {"slack": 0, "discord": 0, "signal": 0, "ws": 0}
                    for match in CHANNEL_PATTERN.finditer(chunk):
                        ch = match.group(1)
                        if ch in counts:
                            counts[ch] += 1

                    flow["slack"] = min(1.0, counts["slack"] / 5.0)
                    flow["discord"] = min(1.0, counts["discord"] / 8.0)
                    flow["signal"] = min(1.0, counts["signal"] / 3.0)
                    ws_activity = min(1.0, counts["ws"] / 8.0)
                    flow["imessage"] = ws_activity * 0.3
                    flow["email"] = ws_activity * 0.2
    except Exception:
        pass

    try:
        sched_info = scheduler_data.get("info", {}) if isinstance(scheduler_data, dict) else {}
        current_runs = sched_info.get("total_runs", 0)
        tasks_running = sched_info.get("tasks_running", 0)

        run_delta = max(0, current_runs - _prev_scheduler_runs) if _prev_scheduler_runs >= 0 else 0
        _prev_scheduler_runs = current_runs

        flow["scheduler"] = min(1.0, (run_delta / 5.0) + (tasks_running * 0.3))

        if tasks_running > 0:
            flow["ollama"] = min(1.0, flow["ollama"] + tasks_running * 0.2)
            flow["postgresql"] = min(1.0, flow["postgresql"] + tasks_running * 0.15)
    except Exception:
        pass

    try:
        if isinstance(redis_data, dict):
            ingest = redis_data.get("ingest_queue_depth", 0)
            ingest_delta = abs(ingest - _prev_ingest_depth) if _prev_ingest_depth >= 0 else 0.0
            _prev_ingest_depth = ingest

            flow["redis"] = min(1.0, (ingest_delta / 3.0) + (ingest / 50.0))
            flow["memory_server"] = min(1.0, (ingest_delta / 2.0) + (ingest / 40.0))
            flow["postgresql"] = min(1.0, flow["postgresql"] + ingest_delta * 0.15)
    except Exception:
        pass

    try:
        if isinstance(task_history_data, dict):
            all_time = task_history_data.get("all_time", {})
            total = sum(all_time.values())
            task_delta = max(0, total - _prev_task_total) if _prev_task_total >= 0 else 0
            _prev_task_total = total

            if task_delta > 0:
                boost = min(1.0, task_delta / 5.0)
                flow["ollama"] = min(1.0, flow["ollama"] + boost * 0.4)
                flow["openrouter"] = min(1.0, flow["openrouter"] + boost * 0.2)
    except Exception:
        pass

    try:
        if isinstance(services_data, dict):
            for svc_name in ["ollama", "tinychat", "mlx_chat", "openwebui"]:
                svc = services_data.get(svc_name, {})
                if svc.get("status") == "up" and svc.get("latency_ms"):
                    lat = svc["latency_ms"]
                    if lat > 50:
                        flow[svc_name] = min(1.0, flow[svc_name] + min(0.5, lat / 200.0))
    except Exception:
        pass

    return flow


# --- History Writer ---

async def write_history_snapshot(state: dict):
    """Write a snapshot of current state to the history DB. Non-blocking, fully async."""
    try:
        pool = app.state.history_pool
        now = time.time()
        async with pool.acquire() as conn:
            # System snapshot
            sys_data = state.get("system", {})
            mem = sys_data.get("memory", {})
            await conn.execute(
                "INSERT INTO dashboard_snapshots (ts, cpu_percent, memory_percent, memory_used_gb, poll_duration_ms) VALUES ($1, $2, $3, $4, $5)",
                now, sys_data.get("cpu_percent"), mem.get("percent"), mem.get("used_gb"), state.get("poll_duration_ms"))

            # Disk history
            disks = sys_data.get("disks", {})
            for mount, info in disks.items():
                await conn.execute(
                    "INSERT INTO dashboard_disk_history (ts, mount, free_gb, percent) VALUES ($1, $2, $3, $4)",
                    now, mount, info.get("free_gb"), info.get("percent"))

            # Service latencies + SLA snapshots
            services = state.get("services", {})
            for svc_name, svc_info in services.items():
                await conn.execute(
                    "INSERT INTO dashboard_latency_history (ts, service, latency_ms, status) VALUES ($1, $2, $3, $4)",
                    now, svc_name, svc_info.get("latency_ms"), svc_info.get("status"))
                # SLA tracking
                await conn.execute(
                    "INSERT INTO sla_snapshots (service, up) VALUES ($1, $2)",
                    svc_name, svc_info.get("status") == "up")

            # Memory count
            pg = state.get("postgresql", {})
            if pg.get("total_rows"):
                await conn.execute(
                    "INSERT INTO dashboard_memory_count_history (ts, total_count) VALUES ($1, $2)",
                    now, pg["total_rows"])

            # Cost history (daily upsert per provider)
            model_usage = state.get("model_usage", {})
            today = time.strftime("%Y-%m-%d")
            for prov, pdata in model_usage.get("by_provider", {}).items():
                await conn.execute(
                    """INSERT INTO dashboard_cost_history (date, provider, total_cost_usd, input_tokens, output_tokens, session_count)
                       VALUES ($1, $2, $3, $4, $5, $6)
                       ON CONFLICT(date, provider) DO UPDATE SET
                           total_cost_usd = excluded.total_cost_usd,
                           input_tokens = excluded.input_tokens,
                           output_tokens = excluded.output_tokens,
                           session_count = excluded.session_count""",
                    today, prov, pdata.get("cost", 0), pdata.get("input_tokens", 0),
                    pdata.get("output_tokens", 0), pdata.get("sessions", 0))
    except Exception:
        pass  # Non-critical — never block the poll loop


# --- Alert Evaluator ---

def evaluate_alerts(state: dict) -> list[dict]:
    """Evaluate alert conditions against current state. Returns list of alerts."""
    global _service_down_counts, _cpu_high_count
    alerts = []

    # Disk free < 10 GB
    sys_data = state.get("system", {})
    for mount, info in sys_data.get("disks", {}).items():
        free = info.get("free_gb", 999)
        if free < 10:
            alerts.append({"category": "disk", "severity": "critical",
                           "message": f"{mount} has only {free:.1f} GB free"})

    # Memory > 90%
    mem_pct = sys_data.get("memory", {}).get("percent", 0)
    if mem_pct > 90:
        alerts.append({"category": "memory", "severity": "critical",
                       "message": f"Memory usage at {mem_pct:.1f}%"})

    # CPU > 95 for 3+ consecutive polls
    cpu_pct = sys_data.get("cpu_percent", 0)
    if cpu_pct > 95:
        _cpu_high_count += 1
    else:
        _cpu_high_count = 0
    if _cpu_high_count >= 3:
        alerts.append({"category": "cpu", "severity": "warning",
                       "message": f"CPU sustained above 95% for {_cpu_high_count} polls ({cpu_pct:.1f}% now)"})

    # Service down for 5+ consecutive polls
    all_services = {}
    for name, svc in state.get("services", {}).items():
        all_services[name] = svc.get("status", "unknown")
    gw = state.get("gateway", {})
    if gw.get("status") == "error" or not gw.get("ok"):
        all_services["gateway"] = "down"
    if state.get("scheduler", {}).get("status") == "error":
        all_services["scheduler"] = "down"

    seen_services = set()
    for name, status in all_services.items():
        seen_services.add(name)
        if status == "down" or status == "error":
            _service_down_counts[name] = _service_down_counts.get(name, 0) + 1
        else:
            _service_down_counts[name] = 0
        if _service_down_counts.get(name, 0) >= 5:
            alerts.append({"category": "service", "severity": "warning",
                           "message": f"{name} has been down for {_service_down_counts[name]} consecutive polls"})
    # Reset counts for services no longer tracked
    for name in list(_service_down_counts):
        if name not in seen_services:
            del _service_down_counts[name]

    # Scheduler task consecutive_failures > 3
    sched = state.get("scheduler", {})
    if isinstance(sched.get("tasks"), dict):
        for task_name, task_info in sched["tasks"].items():
            cf = task_info.get("consecutive_failures", 0)
            if cf > 3:
                alerts.append({"category": "scheduler", "severity": "warning",
                               "message": f"Task '{task_name}' has {cf} consecutive failures"})

    # Redis ingest queue depth
    redis_data = state.get("redis", {})
    depth = redis_data.get("ingest_queue_depth", 0)
    if depth > 100:
        alerts.append({"category": "redis", "severity": "critical",
                       "message": f"Ingest queue depth at {depth} (> 100)"})
    elif depth > 50:
        alerts.append({"category": "redis", "severity": "warning",
                       "message": f"Ingest queue depth at {depth} (> 50)"})

    return alerts


# --- Conversation Activity Collector ---

async def collect_conversations() -> dict:
    """Read sessions.json and return active conversations from last 30 minutes."""
    global _conversations_cache, _conversations_ts
    now = time.time()
    if now - _conversations_ts < 15:
        return _conversations_cache
    try:
        data = _json.loads(SESSIONS_JSON.read_text())
        cutoff = now - 1800  # 30 minutes
        active = []
        by_channel: dict[str, int] = {}

        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            updated = val.get("updatedAt", 0)
            if isinstance(updated, (int, float)):
                # Handle millisecond timestamps
                if updated > 1e12:
                    updated = updated / 1000.0
            else:
                continue
            if updated < cutoff:
                continue

            # Parse channel from key like "agent:main:slack:channel:xxx"
            parts = key.split(":")
            channel = "unknown"
            if len(parts) >= 3:
                channel = parts[2]  # e.g. slack, discord, signal, main
            if channel == "main":
                channel = "direct"

            by_channel[channel] = by_channel.get(channel, 0) + 1
            active.append({
                "key": key[:80],
                "model": val.get("model", "?"),
                "provider": val.get("modelProvider", "?"),
                "input_tokens": val.get("inputTokens", 0) or 0,
                "output_tokens": val.get("outputTokens", 0) or 0,
                "label": val.get("label", ""),
                "channel": channel,
            })

        result = {
            "status": "ok",
            "active_sessions": active,
            "active_count": len(active),
            "by_channel": by_channel,
        }
        _conversations_cache = result
        _conversations_ts = now
        return result
    except Exception as e:
        return _conversations_cache or {"status": "error", "error": str(e),
                                         "active_sessions": [], "active_count": 0, "by_channel": {}}


# --- UniFi Collector ---

async def collect_unifi(session: aiohttp.ClientSession) -> dict:
    """Collect UniFi network controller data. 30s cache."""
    global _unifi_cache, _unifi_ts
    now = time.time()
    if now - _unifi_ts < 30:
        return _unifi_cache
    if _unifi_api_key is None:
        return {"status": "no_key"}
    try:
        import ssl as _ssl
        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl.CERT_NONE
        headers = {"X-API-Key": _unifi_api_key}
        timeout = aiohttp.ClientTimeout(total=5)

        async with session.get(f"{UNIFI_API}/s/default/stat/device",
                               headers=headers, ssl=ssl_ctx, timeout=timeout) as resp:
            device_data = await resp.json()
        async with session.get(f"{UNIFI_API}/s/default/stat/sta",
                               headers=headers, ssl=ssl_ctx, timeout=timeout) as resp:
            sta_data = await resp.json()
        async with session.get(f"{UNIFI_API}/s/default/stat/health",
                               headers=headers, ssl=ssl_ctx, timeout=timeout) as resp:
            health_data = await resp.json()

        devices_raw = device_data.get("data", []) if isinstance(device_data, dict) else device_data if isinstance(device_data, list) else []
        clients_raw = sta_data.get("data", []) if isinstance(sta_data, dict) else sta_data if isinstance(sta_data, list) else []
        health_list = health_data.get("data", []) if isinstance(health_data, dict) else health_data if isinstance(health_data, list) else []

        wan_uptime = 0
        for h in health_list:
            if h.get("subsystem") == "wan":
                wan_uptime = h.get("uptime", 0)

        devices = []
        for d in devices_raw:
            devices.append({
                "name": d.get("name", d.get("hostname", "?")),
                "model": d.get("model", "?"),
                "type": d.get("type", "?"),
                "status": "online" if d.get("state", 0) == 1 else "offline",
                "ip": d.get("ip", ""),
                "num_clients": d.get("num_sta", 0),
                "num_sta": d.get("num_sta", 0),
            })

        result = {
            "status": "ok",
            "device_count": len(devices_raw),
            "client_count": len(clients_raw),
            "wan_uptime_s": wan_uptime,
            "devices": devices,
        }
        _unifi_cache = result
        _unifi_ts = now
        return result
    except Exception as e:
        return _unifi_cache or {"status": "error", "error": str(e),
                                 "device_count": 0, "client_count": 0, "wan_uptime_s": 0, "devices": []}


# --- Plex + HDHomeRun Collectors ---

_plex_token: str | None = None
_plex_cache: dict = {}
_plex_ts: float = 0
_hdhr_cache: dict = {}
_hdhr_ts: float = 0


def _get_plex_token() -> str:
    global _plex_token
    if _plex_token:
        return _plex_token
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", "nova", "-s", "nova-plex-token", "-w"],
            capture_output=True, text=True, timeout=5
        )
        _plex_token = r.stdout.strip()
    except Exception:
        _plex_token = ""
    return _plex_token or ""


async def collect_plex(session: aiohttp.ClientSession) -> dict:
    """Collect Plex server status: sessions, library counts, on-deck."""
    global _plex_cache, _plex_ts
    now = time.time()
    if now - _plex_ts < 15:
        return _plex_cache
    token = _get_plex_token()
    if not token:
        return {"status": "no_token"}
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        headers = {"X-Plex-Token": token, "Accept": "application/json"}

        # Active sessions
        async with session.get(f"{PLEX_BASE}/status/sessions", headers=headers, timeout=timeout) as resp:
            sessions_data = await resp.json()
        sessions = sessions_data.get("MediaContainer", {})
        active_streams = sessions.get("size", 0)
        now_playing = []
        for v in sessions.get("Metadata", []):
            now_playing.append({
                "title": v.get("title", "?"),
                "type": v.get("type", "?"),
                "player": v.get("Player", {}).get("title", "?"),
                "state": v.get("Player", {}).get("state", "?"),
            })

        # Library sizes
        async with session.get(f"{PLEX_BASE}/library/sections", headers=headers, timeout=timeout) as resp:
            lib_data = await resp.json()
        libraries = []
        total_items = 0
        for d in lib_data.get("MediaContainer", {}).get("Directory", []):
            if d.get("key") in PLEX_EXCLUDED_LIBS:
                continue
            count = int(d.get("count", 0)) if d.get("count") else 0
            libraries.append({"title": d.get("title", "?"), "type": d.get("type", "?"), "count": count})
            total_items += count

        # On-deck count
        async with session.get(f"{PLEX_BASE}/library/onDeck", headers=headers, timeout=timeout) as resp:
            ondeck_data = await resp.json()
        ondeck_count = ondeck_data.get("MediaContainer", {}).get("size", 0)

        result = {
            "status": "ok",
            "active_streams": active_streams,
            "now_playing": now_playing,
            "libraries": libraries,
            "total_items": total_items,
            "ondeck_count": ondeck_count,
        }
        _plex_cache = result
        _plex_ts = now
        return result
    except Exception as e:
        return _plex_cache or {"status": "error", "error": str(e), "active_streams": 0, "now_playing": [], "total_items": 0}


async def collect_hdhr(session: aiohttp.ClientSession) -> dict:
    """Collect HDHomeRun tuner status: active tuners, channel count."""
    global _hdhr_cache, _hdhr_ts
    now = time.time()
    if now - _hdhr_ts < 15:
        return _hdhr_cache
    try:
        timeout = aiohttp.ClientTimeout(total=5)

        # Device identity
        async with session.get(f"{HDHR_BASE}/discover.json", timeout=timeout) as resp:
            discover = await resp.json()

        # Tuner status
        async with session.get(f"{HDHR_BASE}/status.json", timeout=timeout) as resp:
            status = await resp.json()

        # Lineup count
        async with session.get(f"{HDHR_BASE}/lineup.json", timeout=timeout) as resp:
            lineup = await resp.json()

        tuners = status.get("Resource", []) if isinstance(status, dict) else status if isinstance(status, list) else []
        active_tuners = sum(1 for t in tuners if t.get("VctNumber") or t.get("TargetIP"))
        total_tuners = discover.get("TunerCount", 4)

        result = {
            "status": "ok",
            "model": discover.get("ModelNumber", "?"),
            "firmware": discover.get("FirmwareVersion", "?"),
            "total_tuners": total_tuners,
            "active_tuners": active_tuners,
            "channel_count": len(lineup) if isinstance(lineup, list) else 0,
            "tuners": tuners,
        }
        _hdhr_cache = result
        _hdhr_ts = now
        return result
    except Exception as e:
        return _hdhr_cache or {"status": "error", "error": str(e), "total_tuners": 4, "active_tuners": 0, "channel_count": 0}


# --- Big Brother Dashboard + API Proxy ---

BB_API = "http://192.168.1.6:37461"


@app.get("/journal")
async def journal_dashboard():
    """Serve the Nova Journal analytics dashboard."""
    return FileResponse("static/journal-dashboard.html")


@app.get("/api/journal/stats")
async def journal_stats():
    """Return the latest journal stats snapshot from the poller."""
    from pathlib import Path as _Path
    stats_file = _Path.home() / ".openclaw/workspace/state/journal_stats.json"
    if not stats_file.exists():
        return {"error": "Stats not yet available — poller may not have run"}
    try:
        return _json.loads(stats_file.read_text())
    except Exception as e:
        return {"error": str(e)}


@app.api_route("/api/journal/poll", methods=["POST", "GET"])
async def journal_poll_now():
    """Trigger an immediate journal stats poll. Accepts both POST and GET to avoid 405."""
    from pathlib import Path as _P
    script = _P.home() / ".openclaw/scripts/nova_journal_stats_poller.py"
    if not script.exists():
        return JSONResponse({"error": "Poller script not found", "path": str(script)}, status_code=404)
    proc = await asyncio.create_subprocess_exec(
        "python3", str(script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    asyncio.create_task(proc.wait())
    return JSONResponse({"queued": True})


@app.get("/bb")
async def bb_dashboard():
    """Serve the Big Brother oversight dashboard."""
    return FileResponse("static/bb.html")


@app.get("/api/bb/health")
async def bb_health():
    """Proxy to Big Brother's /bb/health — full system snapshot for the dashboard."""
    import urllib.request as _ur
    try:
        with _ur.urlopen(f"{BB_API}/bb/health", timeout=8) as r:
            return _json.loads(r.read())
    except Exception as e:
        return {"error": str(e), "big_brother_up": False}


@app.api_route("/api/bb/force-check", methods=["POST", "GET"])
async def bb_force_check():
    """Trigger an immediate Big Brother sweep. Tries POST then falls back to GET."""
    import urllib.request as _ur
    # Try POST first
    try:
        req = _ur.Request(f"{BB_API}/bb/force-check", method="POST", data=b"")
        with _ur.urlopen(req, timeout=5) as r:
            return _json.loads(r.read())
    except Exception:
        pass
    # Fallback to GET (some BB versions only expose GET)
    try:
        with _ur.urlopen(f"{BB_API}/bb/force-check", timeout=5) as r:
            return _json.loads(r.read())
    except Exception:
        pass
    # Last resort: trigger via /bb/sweep
    try:
        req = _ur.Request(f"{BB_API}/bb/sweep", method="POST", data=b"")
        with _ur.urlopen(req, timeout=5) as r:
            return _json.loads(r.read())
    except Exception as e:
        return {"error": str(e), "note": "BB may not support force-check. Tried POST /bb/force-check, GET /bb/force-check, POST /bb/sweep"}


@app.get("/api/bb/events")
async def bb_events():
    """Proxy to Big Brother's event log."""
    import urllib.request as _ur
    try:
        with _ur.urlopen(f"{BB_API}/bb/events", timeout=5) as r:
            return _json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


@app.get("/bb-graphs")
async def bb_graphs_dashboard():
    """Serve the Big Brother metrics/graphs dashboard (MRTG-style)."""
    return FileResponse("static/bb-graphs.html")


@app.get("/api/bb/metrics")
async def bb_metrics():
    """Proxy to Big Brother's /bb/metrics ring buffer (up to 7 days of per-minute buckets)."""
    import urllib.request as _ur
    try:
        with _ur.urlopen(f"{BB_API}/bb/metrics", timeout=10) as r:
            return _json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/privacy/status")
async def privacy_status():
    """
    Proxy to Big Brother's /bb/status + /bb/events, filtered to privacy violations.
    Used by the NovaControl Diagnostics tab to surface model routing drift and PII leak risks.
    """
    import urllib.request as _ur
    result = {
        "ok": False,
        "big_brother_up": False,
        "routing_clean": False,
        "violations": [],
        "last_check": None,
        "recent_events": [],
    }
    try:
        with _ur.urlopen(f"{BB_API}/bb/status", timeout=5) as r:
            bb = _json.loads(r.read())
            result["big_brother_up"] = True
            result["last_check"] = bb.get("last_sweep")
    except Exception:
        result["big_brother_up"] = False
        return result

    try:
        with _ur.urlopen(f"{BB_API}/bb/events?n=50", timeout=5) as r:
            events = _json.loads(r.read()).get("events", [])
            privacy_events = [
                e for e in events
                if "privacy" in e.get("issue", "").lower()
                or "routing" in e.get("issue", "").lower()
                or "openrouter" in e.get("issue", "").lower()
                or "pii" in e.get("issue", "").lower()
            ]
            result["recent_events"] = privacy_events[-10:]
            result["violations"] = [e["issue"] for e in privacy_events if e.get("severity") == "critical"]
    except Exception:
        pass

    result["routing_clean"] = len(result["violations"]) == 0
    result["ok"] = result["big_brother_up"] and result["routing_clean"]
    return result


@app.get("/api/privacy/channels")
async def privacy_channels():
    """Return current model-per-channel routing from openclaw.json for the UI."""
    import urllib.request as _ur
    try:
        config_path = Path.home() / ".openclaw/openclaw.json"
        with open(config_path) as f:
            config = _json.load(f)
        mbc = config.get("channels", {}).get("modelByChannel", {})
        defaults = config.get("agents", {}).get("defaults", {}).get("model", {})
        agents = [
            {"id": a.get("id"), "model": a.get("model", "(inherited)")}
            for a in config.get("agents", {}).get("list", [])
        ]
        return {
            "defaults": defaults,
            "agents": agents,
            "channels": mbc,
            "signal_policy": {
                "dmPolicy": config.get("channels", {}).get("signal", {}).get("dmPolicy"),
                "groupPolicy": config.get("channels", {}).get("signal", {}).get("groupPolicy"),
            }
        }
    except Exception as e:
        return {"error": str(e)}


# --- HealthKit API Endpoint ---

HEALTH_DIR = Path.home() / ".openclaw/private/health"


@app.get("/api/health/latest")
async def health_latest():
    """Return latest HealthKit data. Triggers a fresh export if data is stale (>2h)."""
    import subprocess as _sp
    output_path = HEALTH_DIR / "latest.json"

    # Trigger refresh if missing or older than 2 hours
    stale = True
    if output_path.exists():
        age_hours = (time.time() - output_path.stat().st_mtime) / 3600
        stale = age_hours > 2.0

    if stale:
        try:
            result = _sp.run(
                ["/usr/bin/python3",
                 str(Path.home() / ".openclaw/scripts/nova_healthkit_export.py")],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                return {"error": "HealthKit export failed", "stderr": result.stderr[-300:]}
        except _sp.TimeoutExpired:
            return {"error": "HealthKit export timed out"}
        except Exception as e:
            return {"error": str(e)}

    if not output_path.exists():
        return {"error": "No health data available"}

    try:
        with open(output_path) as f:
            data = _json.load(f)
        data["_stale_before_refresh"] = stale
        return data
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/health/history")
async def health_history(days: int = 7):
    """Return health data files from the last N days."""
    files = sorted(HEALTH_DIR.glob("*.json"), reverse=True)[:days]
    result = []
    for f in files:
        try:
            result.append(_json.loads(f.read_text()))
        except Exception:
            pass
    return result


# --- History API Endpoint ---

RANGE_MAP = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}


@app.get("/api/history/{metric}")
async def history_endpoint(metric: str, range: str = "24h"):
    """Return historical data for a metric. Downsamples into 5-min buckets for large ranges."""
    seconds = RANGE_MAP.get(range, 86400)
    cutoff = time.time() - seconds
    # Downsample into 5-min (300s) buckets for ranges > 1h
    downsample = seconds > 3600

    try:
        pool = app.state.history_pool
        async with pool.acquire() as conn:
            if metric == "cpu":
                rows = await conn.fetch(
                    "SELECT ts, cpu_percent FROM dashboard_snapshots WHERE ts > $1 ORDER BY ts", cutoff)
                data = [(r["ts"], r["cpu_percent"]) for r in rows]
                if downsample and data:
                    return JSONResponse(_downsample_single(data, "cpu_percent"))
                return JSONResponse([{"ts": r[0], "cpu_percent": r[1]} for r in data])

            elif metric == "memory":
                rows = await conn.fetch(
                    "SELECT ts, memory_percent, memory_used_gb FROM dashboard_snapshots WHERE ts > $1 ORDER BY ts", cutoff)
                data = [(r["ts"], r["memory_percent"], r["memory_used_gb"]) for r in rows]
                if downsample and data:
                    return JSONResponse(_downsample_memory(data))
                return JSONResponse([{"ts": r[0], "memory_percent": r[1], "memory_used_gb": r[2]} for r in data])

            elif metric == "disk":
                rows = await conn.fetch(
                    "SELECT ts, mount, free_gb, percent FROM dashboard_disk_history WHERE ts > $1 ORDER BY ts", cutoff)
                data = [(r["ts"], r["mount"], r["free_gb"], r["percent"]) for r in rows]
                if downsample and data:
                    return JSONResponse(_downsample_disk(data))
                return JSONResponse([{"ts": r[0], "mount": r[1], "free_gb": r[2], "percent": r[3]} for r in data])

            elif metric == "latency":
                rows = await conn.fetch(
                    "SELECT ts, service, latency_ms, status FROM dashboard_latency_history WHERE ts > $1 ORDER BY ts", cutoff)
                data = [(r["ts"], r["service"], r["latency_ms"], r["status"]) for r in rows]
                if downsample and data:
                    return JSONResponse(_downsample_latency(data))
                return JSONResponse([{"ts": r[0], "service": r[1], "latency_ms": r[2], "status": r[3]} for r in data])

            elif metric == "memories":
                rows = await conn.fetch(
                    "SELECT ts, total_count FROM dashboard_memory_count_history WHERE ts > $1 ORDER BY ts", cutoff)
                data = [(r["ts"], r["total_count"]) for r in rows]
                if downsample and data:
                    return JSONResponse(_downsample_single(data, "total_count"))
                return JSONResponse([{"ts": r[0], "total_count": r[1]} for r in data])

            elif metric == "costs":
                cost_cutoff = (_dt.datetime.now() - _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%d")
                rows = await conn.fetch(
                    "SELECT date, provider, total_cost_usd, input_tokens, output_tokens, session_count FROM dashboard_cost_history WHERE date >= $1 ORDER BY date",
                    cost_cutoff)
                return JSONResponse([{
                    "date": r["date"], "provider": r["provider"], "total_cost_usd": r["total_cost_usd"],
                    "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"], "session_count": r["session_count"]
                } for r in rows])

            else:
                return JSONResponse({"error": f"Unknown metric: {metric}"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _bucket_ts(ts: float) -> float:
    """Round timestamp down to nearest 5-minute bucket."""
    return ts - (ts % 300)


def _downsample_single(rows: list, field: str) -> list:
    """Downsample rows with a single numeric value into 5-min avg buckets."""
    buckets: dict[float, list] = {}
    for ts, val in rows:
        b = _bucket_ts(ts)
        if b not in buckets:
            buckets[b] = []
        if val is not None:
            buckets[b].append(val)
    result = []
    for b in sorted(buckets):
        vals = buckets[b]
        result.append({"ts": b, field: round(sum(vals) / len(vals), 2) if vals else None})
    return result


def _downsample_memory(rows: list) -> list:
    """Downsample memory rows (percent + used_gb) into 5-min avg buckets."""
    buckets: dict[float, tuple[list, list]] = {}
    for ts, pct, used in rows:
        b = _bucket_ts(ts)
        if b not in buckets:
            buckets[b] = ([], [])
        if pct is not None:
            buckets[b][0].append(pct)
        if used is not None:
            buckets[b][1].append(used)
    result = []
    for b in sorted(buckets):
        pcts, useds = buckets[b]
        result.append({
            "ts": b,
            "memory_percent": round(sum(pcts) / len(pcts), 2) if pcts else None,
            "memory_used_gb": round(sum(useds) / len(useds), 2) if useds else None,
        })
    return result


def _downsample_disk(rows: list) -> list:
    """Downsample disk rows by mount + 5-min buckets."""
    buckets: dict[tuple[float, str], tuple[list, list]] = {}
    for ts, mount, free, pct in rows:
        key = (_bucket_ts(ts), mount)
        if key not in buckets:
            buckets[key] = ([], [])
        if free is not None:
            buckets[key][0].append(free)
        if pct is not None:
            buckets[key][1].append(pct)
    result = []
    for (b, mount) in sorted(buckets):
        frees, pcts = buckets[(b, mount)]
        result.append({
            "ts": b, "mount": mount,
            "free_gb": round(sum(frees) / len(frees), 2) if frees else None,
            "percent": round(sum(pcts) / len(pcts), 2) if pcts else None,
        })
    return result


def _downsample_latency(rows: list) -> list:
    """Downsample latency rows by service + 5-min buckets."""
    buckets: dict[tuple[float, str], list] = {}
    for ts, svc, lat, status in rows:
        key = (_bucket_ts(ts), svc)
        if key not in buckets:
            buckets[key] = []
        if lat is not None:
            buckets[key].append(lat)
    result = []
    for (b, svc) in sorted(buckets):
        vals = buckets[(b, svc)]
        result.append({
            "ts": b, "service": svc,
            "latency_ms": round(sum(vals) / len(vals)) if vals else None,
        })
    return result


# --- New Card Collectors ---

async def collect_searxng_stats(session: aiohttp.ClientSession) -> dict:
    global _searxng_cache, _searxng_ts
    now = time.time()
    if now - _searxng_ts < 60:
        return _searxng_cache
    try:
        async with session.get("http://127.0.0.1:8888/config",
                               timeout=aiohttp.ClientTimeout(total=3)) as resp:
            data = await resp.json()
        engines = data.get("engines", [])
        enabled = [e for e in engines if e.get("enabled", True)] if isinstance(engines, list) else engines
        svc_status = current_state.get("services", {}).get("searxng", {}).get("status", "unknown")

        # Get search stats if available
        stats_data = {}
        try:
            async with session.get("http://127.0.0.1:8888/stats",
                                   timeout=aiohttp.ClientTimeout(total=3)) as stats_resp:
                if stats_resp.status == 200:
                    stats_data = await stats_resp.json()
        except Exception:
            pass

        # Categorize engines by type
        categories = {}
        for e in (enabled if isinstance(enabled, list) else []):
            cat = e.get("categories", ["other"])
            cat_name = cat[0] if isinstance(cat, list) and cat else "other"
            categories[cat_name] = categories.get(cat_name, 0) + 1

        # If we got engine data successfully, status is "ok" regardless of service port check
        effective_status = "ok" if (isinstance(enabled, list) and len(enabled) > 0) else svc_status
        result = {
            "status": effective_status,
            "engine_count": len(enabled) if isinstance(enabled, list) else 0,
            "total_engines": len(engines) if isinstance(engines, list) else 0,
            "categories": categories,
            "queries_total": stats_data.get("queries", stats_data.get("total_queries", 0)),
            "avg_response_ms": stats_data.get("avg_response_time", 0),
            "engines": [{"name": e.get("name", "?"), "shortcut": e.get("shortcut", ""),
                         "categories": e.get("categories", []), "enabled": e.get("enabled", True)}
                        for e in (enabled[:30] if isinstance(enabled, list) else [])],
        }
        _searxng_cache = result
        _searxng_ts = now
        return result
    except Exception as e:
        return _searxng_cache or {"status": "down", "engine_count": 0, "total_engines": 0, "error": str(e), "engines": [], "categories": {}}


async def collect_backup_status() -> dict:
    global _backup_cache, _backup_ts
    now = time.time()
    if now - _backup_ts < 30:
        return _backup_cache
    try:
        # Check primary and alternate log locations
        backup_log_path = None
        for candidate in [
            BACKUP_LOG,
            Path.home() / ".openclaw" / "workspace" / "logs" / "nova_pg_backup.log",
            Path.home() / ".openclaw" / "scripts" / "logs" / "nova_pg_backup.log",
        ]:
            if candidate.exists():
                backup_log_path = candidate
                break

        if backup_log_path is None:
            return {"status": "no_log", "last_backup": None, "success": False, "size": None, "lines": []}

        with open(backup_log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
            lines = [l.rstrip() for l in all_lines[-30:]]

        last_success_ts = None
        last_error_ts = None
        last_size = None
        success = False

        for line in reversed(lines):
            ll = line.lower()
            if "backup complete" in ll or "backup successful" in ll or "success" in ll:
                # Try to extract timestamp from line start (common format: 2026-04-28 12:00:00)
                ts_match = re.match(r"(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2})", line)
                if ts_match and not last_success_ts:
                    last_success_ts = ts_match.group(1)
                    success = True
                # Try to extract size
                size_match = re.search(r"(\d+(?:\.\d+)?)\s*(MB|GB|KB|bytes)", line, re.IGNORECASE)
                if size_match and not last_size:
                    last_size = size_match.group(0)
            elif "error" in ll or "fail" in ll:
                ts_match = re.match(r"(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2})", line)
                if ts_match and not last_error_ts:
                    last_error_ts = ts_match.group(1)

        # Determine if backup is stale (>24h)
        stale = False
        if last_success_ts:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(last_success_ts.replace(" ", "T"))
                age_hours = (datetime.now() - dt).total_seconds() / 3600
                stale = age_hours > 24
            except Exception:
                pass

        result = {
            "status": "ok" if success and not stale else "warning" if success and stale else "error",
            "last_backup": last_success_ts,
            "last_error": last_error_ts,
            "success": success,
            "stale": stale,
            "size": last_size,
            "lines": lines,
        }
        _backup_cache = result
        _backup_ts = now
        return result
    except Exception as e:
        return _backup_cache or {"status": "error", "error": str(e), "last_backup": None, "success": False, "size": None, "lines": []}


async def collect_response_time() -> dict:
    global _response_time_cache, _response_time_ts
    now = time.time()
    if now - _response_time_ts < 30:
        return _response_time_cache
    try:
        if not GATEWAY_LOG.exists():
            return {"status": "no_log", "replies_today": 0, "avg_per_hour": 0.0, "recent_replies": []}

        with open(GATEWAY_LOG, "r", errors="replace") as f:
            content = f.read()

        # Look for "[slack] delivered reply" or "res " patterns
        reply_pattern = re.compile(r"(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}).*(?:\[slack\]\s*delivered reply|res\s+)", re.IGNORECASE)
        today_str = time.strftime("%Y-%m-%d")
        today_count = 0
        all_hours: dict[int, int] = {}
        recent: list[str] = []

        for line in content.split("\n"):
            m = reply_pattern.search(line)
            if m:
                ts_str = m.group(1)
                if ts_str.startswith(today_str):
                    today_count += 1
                try:
                    hour = int(ts_str[11:13])
                    all_hours[hour] = all_hours.get(hour, 0) + 1
                except Exception:
                    pass
                recent.append(line.rstrip())

        hours_with_data = len(all_hours)
        avg_per_hour = round(sum(all_hours.values()) / max(1, hours_with_data), 1)

        result = {
            "status": "ok",
            "replies_today": today_count,
            "avg_per_hour": avg_per_hour,
            "hourly_breakdown": all_hours,
            "recent_replies": recent[-20:],
        }
        _response_time_cache = result
        _response_time_ts = now
        return result
    except Exception as e:
        return _response_time_cache or {"status": "error", "error": str(e), "replies_today": 0, "avg_per_hour": 0.0}


async def collect_herd_activity() -> dict:
    global _herd_cache, _herd_ts
    now = time.time()
    if now - _herd_ts < 30:
        return _herd_cache
    try:
        if not GATEWAY_LOG.exists():
            return {"status": "no_log", "channels": {}, "total_events": 0}

        with open(GATEWAY_LOG, "r", errors="replace") as f:
            content = f.read()

        channel_pattern = re.compile(r"\[(slack|discord|signal)\]")
        counts: dict[str, int] = {"slack": 0, "discord": 0, "signal": 0}
        today_str = time.strftime("%Y-%m-%d")
        today_counts: dict[str, int] = {"slack": 0, "discord": 0, "signal": 0}

        for line in content.split("\n"):
            m = channel_pattern.search(line)
            if m:
                ch = m.group(1)
                counts[ch] = counts.get(ch, 0) + 1
                if today_str in line[:20]:
                    today_counts[ch] = today_counts.get(ch, 0) + 1

        total = sum(counts.values())
        result = {
            "status": "ok",
            "channels": counts,
            "today": today_counts,
            "total_events": total,
        }
        _herd_cache = result
        _herd_ts = now
        return result
    except Exception as e:
        return _herd_cache or {"status": "error", "error": str(e), "channels": {}, "total_events": 0}


async def collect_mlx_status(session: aiohttp.ClientSession) -> dict:
    global _mlx_cache, _mlx_ts
    now = time.time()
    if now - _mlx_ts < 15:
        return _mlx_cache
    try:
        async with session.get(MLX_MODELS_URL,
                               timeout=aiohttp.ClientTimeout(total=3)) as resp:
            data = await resp.json()
        models = data.get("data", [])
        model_name = models[0].get("id", "unknown") if models else "none"
        svc_status = current_state.get("services", {}).get("mlx_chat", {}).get("status", "unknown")
        result = {
            "status": svc_status,
            "model": model_name,
            "model_count": len(models),
            "models": [{"id": m.get("id", "?"), "owned_by": m.get("owned_by", "?")} for m in models],
        }
        _mlx_cache = result
        _mlx_ts = now
        return result
    except Exception as e:
        return _mlx_cache or {"status": "down", "model": "unreachable", "model_count": 0, "error": str(e), "models": []}


async def collect_camera_activity() -> dict:
    global _camera_cache, _camera_ts
    now = time.time()
    if now - _camera_ts < 30:
        return _camera_cache
    try:
        if not PROTECT_STATE.exists():
            return {"status": "no_data", "cameras": [], "total": 0, "connected": 0, "disconnected": 0,
                    "note": "protect_monitor_state.json not found"}

        data = _json.loads(PROTECT_STATE.read_text())
        cameras = data.get("cameras", []) if isinstance(data, dict) else []

        # If file exists but cameras array is empty, check staleness and report no_data
        if not cameras:
            # Try to get camera count from Big Brother health API
            bb_camera_info = {}
            try:
                import urllib.request as _ur
                with _ur.urlopen("http://192.168.1.6:37461/bb/status", timeout=2) as _r:
                    bb = _json.loads(_r.read())
                # BB might report camera service status
                bb_camera_info = {"bb_hint": "protect service tracked by Big Brother"}
            except Exception:
                pass
            result = {
                "status": "no_data",
                "cameras": [],
                "total": 0,
                "connected": 0,
                "disconnected": 0,
                "note": "State file has 0 cameras — protect_monitor may need refresh",
                **bb_camera_info,
            }
            _camera_cache = result
            _camera_ts = now
            return result

        connected = sum(1 for c in cameras if c.get("connected", False) or c.get("state", "") == "CONNECTED")
        disconnected = len(cameras) - connected

        result = {
            "status": "ok",
            "cameras": [{"name": c.get("name", "?"), "connected": c.get("connected", False) or c.get("state", "") == "CONNECTED",
                          "type": c.get("type", "?"), "ip": c.get("host", c.get("ip", "?"))}
                         for c in cameras],
            "total": len(cameras),
            "connected": connected,
            "disconnected": disconnected,
        }
        _camera_cache = result
        _camera_ts = now
        return result
    except Exception as e:
        return _camera_cache or {"status": "error", "error": str(e), "cameras": [], "total": 0, "connected": 0, "disconnected": 0}


async def collect_homekit(session: aiohttp.ClientSession) -> dict:
    global _homekit_cache, _homekit_ts
    now = time.time()
    if now - _homekit_ts < 30:
        return _homekit_cache
    try:
        scenes = []
        accessories = []
        source = ""

        # Try Shortcuts proxy on port 37432 first (direct HomeKit access)
        homekit_proxy = "http://127.0.0.1:37432"
        try:
            async with session.get(f"{homekit_proxy}/api/scenes",
                                   timeout=aiohttp.ClientTimeout(total=5)) as resp:
                scenes_resp = await resp.json()
                scenes = scenes_resp.get("scenes", []) if isinstance(scenes_resp, dict) else scenes_resp
                source = "ShortcutsProxy:37432"
        except Exception:
            pass
        try:
            async with session.get(f"{homekit_proxy}/api/accessories",
                                   timeout=aiohttp.ClientTimeout(total=5)) as resp:
                acc_resp = await resp.json()
                accessories = acc_resp.get("accessories", []) if isinstance(acc_resp, dict) else acc_resp
                if not source:
                    source = "ShortcutsProxy:37432"
        except Exception:
            pass

        # Fall back to NovaControl on 37400 if proxy returned nothing
        if not scenes and not accessories:
            try:
                async with session.get(f"{HOMEKIT_API}/api/homekit/scenes",
                                       timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    scenes_resp = await resp.json()
                    scenes = scenes_resp.get("scenes", []) if isinstance(scenes_resp, dict) else scenes_resp
                    source = "NovaControl:37400"
            except Exception:
                pass
            try:
                async with session.get(f"{HOMEKIT_API}/api/homekit/accessories",
                                       timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    acc_resp = await resp.json()
                    accessories = acc_resp.get("accessories", []) if isinstance(acc_resp, dict) else acc_resp
                    if not source:
                        source = "NovaControl:37400"
            except Exception:
                pass

        accessory_count = len(accessories) if isinstance(accessories, list) else 0

        result = {
            "status": "ok" if scenes or accessories else "no_data",
            "scene_count": len(scenes) if isinstance(scenes, list) else 0,
            "accessory_count": accessory_count,
            "scenes": [{"name": s.get("name", "?"), "id": s.get("id", "")} for s in scenes[:20]] if isinstance(scenes, list) else [],
            "source": source or "none",
        }
        _homekit_cache = result
        _homekit_ts = now
        return result
    except Exception as e:
        return _homekit_cache or {"status": "unavailable", "scene_count": 0, "accessory_count": 0, "error": str(e), "scenes": []}


# --- New card collectors (12 cards) ---

async def collect_app_watchdog() -> dict:
    """Read the app watchdog state file for port health, merged with NovaControl status."""
    global _app_watchdog_cache, _app_watchdog_ts
    now = time.time()
    if now - _app_watchdog_ts < 15:
        return _app_watchdog_cache
    try:
        if not APP_WATCHDOG_STATE.exists():
            return {"status": "unavailable", "apps": []}
        data = _json.loads(APP_WATCHDOG_STATE.read_text())
        apps_raw = data.get("apps", {})

        # Get NovaControl status to override port-check results for desktop apps
        nc_status_map = {}  # port -> {"status": str, "summary": str}
        try:
            import urllib.request as _ur
            with _ur.urlopen("http://127.0.0.1:37400/api/status", timeout=2) as _r:
                nc_data = _json.loads(_r.read())
            for svc in nc_data.get("services", []):
                old_port = str(svc.get("oldPort", ""))
                if old_port:
                    nc_status_map[old_port] = {
                        "status": svc.get("status", "unknown"),
                        "summary": svc.get("summary", ""),
                    }
        except Exception:
            pass

        apps = []
        for port_key, info in apps_raw.items():
            # Skip infra ports — they're already tracked elsewhere
            if port_key.startswith("infra_"):
                continue
            name = APP_PORT_NAMES.get(port_key, f"Port {port_key}")
            alive = info.get("alive", False)
            last_seen = info.get("last_seen", 0)
            uptime_s = now - last_seen if alive and last_seen > 0 else 0

            # If NovaControl says the app is online, trust that over port check
            nc_info = nc_status_map.get(port_key, {})
            if nc_info.get("status") == "online" and not alive:
                alive = True
                # These are desktop apps that don't have persistent web servers
                # NovaControl tracks them correctly via its own mechanism

            apps.append({
                "name": name,
                "port": port_key,
                "alive": alive,
                "info": nc_info.get("summary") or info.get("info", ""),
                "last_seen": last_seen,
                "uptime_s": uptime_s,
                "nc_status": nc_info.get("status", ""),
            })
        apps.sort(key=lambda a: int(a["port"]) if a["port"].isdigit() else 99999)
        up_count = sum(1 for a in apps if a["alive"])
        total = len(apps)
        restarts = data.get("restarts", [])
        result = {
            "status": "ok" if up_count == total else "degraded" if up_count > 0 else "down",
            "apps": apps,
            "up_count": up_count,
            "total": total,
            "recent_restarts": restarts[-5:] if restarts else [],
        }
        _app_watchdog_cache = result
        _app_watchdog_ts = now
        return result
    except Exception as e:
        return _app_watchdog_cache or {"status": "error", "error": str(e), "apps": []}


async def collect_weather() -> dict:
    """Read sky watcher state and today's memory file for weather info."""
    global _weather_cache, _weather_ts
    now = time.time()
    if now - _weather_ts < 120:  # cache 2 minutes
        return _weather_cache
    try:
        result = {"status": "unavailable"}

        # Read sky watcher state
        if SKY_WATCHER_STATE.exists():
            sky = _json.loads(SKY_WATCHER_STATE.read_text())
            result["last_capture"] = sky.get("last_capture", "")
            result["frames_today"] = sky.get("frames_today", 0)
            result["sessions_today"] = sky.get("sessions_today", [])
            result["status"] = "ok"

        # Read today's memory file for weather data
        today_str = time.strftime("%Y-%m-%d")
        today_file = MEMORY_DIR / f"{today_str}.md"
        if today_file.exists():
            content = today_file.read_text(errors="replace")
            # Try to extract weather section
            weather_match = re.search(
                r"(?:Weather|weather|WEATHER).*?(?:\n\n|\Z)",
                content, re.DOTALL)
            if weather_match:
                weather_text = weather_match.group(0).strip()[:500]
                result["weather_text"] = weather_text
                result["status"] = "ok"
                # Extract temperature if present
                temp_match = re.search(r"(\d{2,3})\s*[°F]", weather_text)
                if temp_match:
                    result["temp_f"] = int(temp_match.group(1))
                # Extract conditions
                for cond in ("sunny", "cloudy", "partly cloudy", "overcast", "rain",
                             "clear", "fog", "haze", "windy", "storm"):
                    if cond in weather_text.lower():
                        result["conditions"] = cond.title()
                        break

            # Try to extract moon phase
            moon_match = re.search(r"(?:moon|Moon|MOON)[:\s]*([^\n]+)", content)
            if moon_match:
                result["moon_phase"] = moon_match.group(1).strip()[:80]

        _weather_cache = result
        _weather_ts = now
        return result
    except Exception as e:
        return _weather_cache or {"status": "error", "error": str(e)}


async def collect_dream_status() -> dict:
    """Check dream pipeline state from scheduler tasks and workspace."""
    global _dream_cache, _dream_ts
    now = time.time()
    if now - _dream_ts < 60:
        return _dream_cache
    try:
        result = {"status": "unavailable"}
        sched = current_state.get("scheduler", {})
        tasks = sched.get("tasks", {})

        dream_task = tasks.get("dream_pipeline", {})
        if dream_task:
            result["last_run"] = dream_task.get("last_run", 0)
            result["run_count"] = dream_task.get("run_count", 0)
            result["consecutive_failures"] = dream_task.get("consecutive_failures", 0)
            result["last_duration"] = dream_task.get("last_duration", 0)
            result["status"] = "ok" if dream_task.get("consecutive_failures", 0) == 0 else "degraded"

        # Check for dream images in workspace
        dream_video_dir = Path.home() / ".openclaw" / "workspace" / "dream_videos"
        if dream_video_dir.exists():
            images = list(dream_video_dir.glob("*.png"))
            result["image_count"] = len(images)
            result["has_images"] = len(images) > 0
            if images:
                newest = max(images, key=lambda p: p.stat().st_mtime)
                result["last_image_ts"] = newest.stat().st_mtime
        else:
            result["image_count"] = 0
            result["has_images"] = False

        # Check if dream journal exists in memory/dreams
        if DREAM_DIR.exists():
            dream_files = sorted(DREAM_DIR.iterdir())
            result["dream_entries"] = len(dream_files)
            if dream_files:
                last = dream_files[-1]
                result["last_dream_file"] = last.name
                try:
                    content = last.read_text(errors="replace")
                    result["last_dream_words"] = len(content.split())
                except Exception:
                    pass
        else:
            result["dream_entries"] = 0

        _dream_cache = result
        _dream_ts = now
        return result
    except Exception as e:
        return _dream_cache or {"status": "error", "error": str(e)}


async def collect_synology_state() -> dict:
    """Read Synology NAS state file."""
    try:
        if not SYNOLOGY_STATE.exists():
            return {"status": "unavailable"}
        data = _json.loads(SYNOLOGY_STATE.read_text())
        return {
            "status": "ok",
            "last_check": data.get("last_check", ""),
            "model": data.get("model", "?"),
            "firmware": data.get("firmware", "?"),
            "cpu_pct": data.get("cpu_pct", 0),
            "ram_pct": data.get("ram_pct", 0),
            "problem_count": data.get("problem_count", 0),
            "problems": data.get("problems", []),
            "volumes": data.get("volumes", ""),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def collect_healthkit_status() -> dict:
    """Check HealthKit launchd agent status."""
    try:
        # Check launchd status
        proc = await asyncio.create_subprocess_exec(
            "launchctl", "list", "com.nova.healthkit",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
        running = proc.returncode == 0

        # Check log file for last sync time
        last_sync = None
        if HEALTHKIT_LOG.exists():
            try:
                lines = HEALTHKIT_LOG.read_text(errors="replace").strip().split("\n")
                for line in reversed(lines[-50:]):
                    ts_match = re.match(r"(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2})", line)
                    if ts_match:
                        last_sync = ts_match.group(1)
                        break
            except Exception:
                pass

        return {
            "status": "ok" if running else "down",
            "running": running,
            "last_sync": last_sync,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def collect_homebridge_status() -> dict:
    """Check Homebridge status by pinging port 8581 and checking launchd."""
    try:
        # Check launchd
        proc = await asyncio.create_subprocess_exec(
            "launchctl", "list", "net.digitalnoise.homebridge",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
        launchd_ok = proc.returncode == 0

        # Parse PID from launchctl output
        pid = None
        if launchd_ok and stdout:
            parts = stdout.decode().strip().split("\t")
            if len(parts) >= 1 and parts[0].isdigit():
                pid = int(parts[0])

        # Quick HTTP check on port 8581
        port_ok = False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 8581), timeout=2)
            writer.close()
            await writer.wait_closed()
            port_ok = True
        except Exception:
            pass

        status = "ok" if launchd_ok and port_ok else "degraded" if launchd_ok else "down"
        return {
            "status": status,
            "launchd": launchd_ok,
            "port_reachable": port_ok,
            "pid": pid,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# --- Detail helpers for new cards ---

async def _detail_cost_tracker():
    """Detail for OpenRouter cost tracker card."""
    if not SESSIONS_JSON.exists():
        return {"today_cost": 0, "today_sessions": 0, "today_tokens": 0, "daily_history": []}
    data = _json.loads(SESSIONS_JSON.read_text())
    today_str = time.strftime("%Y-%m-%d")
    today_cost = 0.0
    today_sessions = 0
    today_tokens = 0
    total_cost = 0.0

    sessions = []
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        if val.get("modelProvider") != "openrouter":
            continue
        cost = val.get("estimatedCostUsd", 0) or 0
        inp = val.get("inputTokens", 0) or 0
        out = val.get("outputTokens", 0) or 0
        total_cost += cost

        updated = val.get("updatedAt", 0)
        if isinstance(updated, (int, float)):
            if updated > 1e12:
                updated_ts = updated / 1000.0
            else:
                updated_ts = updated
            updated_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated_ts))
            if updated_str.startswith(today_str):
                today_cost += cost
                today_sessions += 1
                today_tokens += inp + out
        else:
            updated_str = str(updated)[:19]

        sessions.append({
            "key": key[:60], "model": val.get("model", "?"),
            "cost": cost, "input_tokens": inp, "output_tokens": out,
            "updated": updated_str,
        })

    sessions.sort(key=lambda x: x.get("updated", ""), reverse=True)
    return {
        "today_cost": round(today_cost, 6),
        "total_cost": round(total_cost, 6),
        "today_sessions": today_sessions,
        "today_tokens": today_tokens,
        "sessions": sessions[:25],
    }


async def _detail_memory_growth():
    """Detail for memory growth card."""
    pg = current_state.get("postgresql", {})
    total = pg.get("total_rows", 0)

    # Get by-source breakdown
    proc = await asyncio.create_subprocess_exec(
        "psql", PG_DB, "-t", "-A", "-c",
        "SELECT source, count(*) FROM memories GROUP BY source ORDER BY count DESC LIMIT 20",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    sources = []
    for line in stdout.decode().strip().split("\n"):
        if "|" in line:
            parts = line.split("|")
            sources.append({"source": parts[0], "count": int(parts[1])})

    # Daily counts for trend
    proc2 = await asyncio.create_subprocess_exec(
        "psql", PG_DB, "-t", "-A", "-c",
        "SELECT date_trunc('day', created_at)::date, count(*) FROM memories WHERE created_at >= CURRENT_DATE - 14 GROUP BY 1 ORDER BY 1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5)
    daily = []
    for line in stdout2.decode().strip().split("\n"):
        if "|" in line:
            parts = line.split("|")
            daily.append({"date": parts[0], "count": int(parts[1])})

    return {
        "total": total,
        "by_source": sources,
        "daily_trend": daily,
    }


async def _detail_disk_usage():
    """Detail for disk usage card."""
    sys_data = current_state.get("system", {})
    disks = sys_data.get("disks", {})
    return {"disks": disks}


async def _detail_cron_health():
    """Detail for cron health card."""
    sched = current_state.get("scheduler", {})
    tasks = sched.get("tasks", {})
    task_list = []
    for name, t in sorted(tasks.items(), key=lambda x: x[0]):
        status = "ok"
        if not t.get("enabled", True):
            status = "disabled"
        elif t.get("running"):
            status = "running"
        elif t.get("consecutive_failures", 0) > 0:
            status = "failing"
        elif t.get("run_count", 0) == 0:
            status = "never"
        task_list.append({"name": name, "status": status, "run_count": t.get("run_count", 0),
                          "consecutive_failures": t.get("consecutive_failures", 0),
                          "schedule": t.get("schedule", "?"),
                          "last_duration": t.get("last_duration", 0)})
    return {"tasks": task_list}


async def _detail_token_counter():
    """Detail for live token counter card."""
    mu = current_state.get("model_usage", {})
    return {
        "total_tokens": mu.get("total_tokens", 0),
        "total_cost": mu.get("total_cost_usd", 0),
        "by_provider": mu.get("by_provider", {}),
    }


# =============================================================================
# ENTERPRISE FEATURES: Incidents, SLA, Alerts, Capacity, RBAC
# =============================================================================

# --- JWT / Auth helpers ---
_JWT_SECRET = os.environ.get("NOVA_JWT_SECRET", "nova-control-default-secret-change-me")
_JWT_EXPIRY = 86400  # 24 hours


def _jwt_encode(payload: dict) -> str:
    """Minimal JWT encoder (HS256). No external dependency."""
    import base64 as _b64
    import hmac
    header = _b64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b'=').decode()
    payload["exp"] = int(time.time()) + _JWT_EXPIRY
    payload_enc = _b64.urlsafe_b64encode(_json.dumps(payload).encode()).rstrip(b'=').decode()
    sig_input = f"{header}.{payload_enc}".encode()
    sig = _b64.urlsafe_b64encode(hmac.new(_JWT_SECRET.encode(), sig_input, "sha256").digest()).rstrip(b'=').decode()
    return f"{header}.{payload_enc}.{sig}"


def _jwt_decode(token: str) -> dict | None:
    """Minimal JWT decoder. Returns payload or None if invalid/expired."""
    import base64 as _b64
    import hmac
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        sig_input = f"{parts[0]}.{parts[1]}".encode()
        expected_sig = _b64.urlsafe_b64encode(hmac.new(_JWT_SECRET.encode(), sig_input, "sha256").digest()).rstrip(b'=').decode()
        if not hmac.compare_digest(expected_sig, parts[2]):
            return None
        # Decode payload
        payload_bytes = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = _json.loads(_b64.urlsafe_b64decode(payload_bytes))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


async def _get_current_user(request: Request) -> dict | None:
    """Extract user from Authorization header or cookie. Returns None if unauthenticated."""
    token = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    if not token:
        token = request.cookies.get("nova_token")
    if not token:
        return None
    return _jwt_decode(token)


def _require_role(minimum_role: str):
    """Dependency that checks user has at least the minimum role. Roles: viewer < operator < admin."""
    role_order = {"viewer": 0, "operator": 1, "admin": 2}

    async def check(request: Request):
        user = await _get_current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        user_level = role_order.get(user.get("role", "viewer"), 0)
        required_level = role_order.get(minimum_role, 0)
        if user_level < required_level:
            raise HTTPException(status_code=403, detail=f"Requires {minimum_role} role")
        return user
    return check


# --- Auth Routes ---

@app.post("/api/auth/login")
async def auth_login(request: Request):
    """Authenticate user and return JWT token."""
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        return JSONResponse({"error": "Username and password required"}, status_code=400)

    pool = app.state.history_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, username, password_hash, role FROM dashboard_users WHERE username = $1", username)
        if not row:
            return JSONResponse({"error": "Invalid credentials"}, status_code=401)
        pw_hash = hashlib.sha256((password + username).encode()).hexdigest()
        if not row["password_hash"] == pw_hash:
            return JSONResponse({"error": "Invalid credentials"}, status_code=401)
        await conn.execute("UPDATE dashboard_users SET last_login = now() WHERE id = $1", row["id"])

    token = _jwt_encode({"sub": str(row["id"]), "username": row["username"], "role": row["role"]})
    return JSONResponse({"token": token, "username": row["username"], "role": row["role"]})


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """Return current user info from token."""
    user = await _get_current_user(request)
    if not user:
        return JSONResponse({"authenticated": False})
    return JSONResponse({"authenticated": True, "username": user.get("username"), "role": user.get("role")})


# --- Admin Routes ---

@app.get("/admin")
async def admin_page():
    return FileResponse(Path(__file__).parent / "static" / "admin.html")


@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    """List all dashboard users (admin only)."""
    user = await _get_current_user(request)
    if not user or user.get("role") != "admin":
        return JSONResponse({"error": "Admin required"}, status_code=403)
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, username, role, created_at, last_login FROM dashboard_users ORDER BY created_at")
    return JSONResponse([{
        "id": str(r["id"]), "username": r["username"], "role": r["role"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "last_login": r["last_login"].isoformat() if r["last_login"] else None,
    } for r in rows])


@app.post("/api/admin/users")
async def admin_create_user(request: Request):
    """Create a new dashboard user (admin only)."""
    user = await _get_current_user(request)
    if not user or user.get("role") != "admin":
        return JSONResponse({"error": "Admin required"}, status_code=403)
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    role = body.get("role", "viewer")
    if not username or not password:
        return JSONResponse({"error": "Username and password required"}, status_code=400)
    if role not in ("viewer", "operator", "admin"):
        return JSONResponse({"error": "Invalid role"}, status_code=400)
    pw_hash = hashlib.sha256((password + username).encode()).hexdigest()
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        try:
            await conn.execute("INSERT INTO dashboard_users (username, password_hash, role) VALUES ($1, $2, $3)",
                               username, pw_hash, role)
        except Exception as e:
            return JSONResponse({"error": f"User creation failed: {e}"}, status_code=409)
        # Audit log
        await conn.execute("INSERT INTO audit_log (user_id, username, action, detail) VALUES ($1, $2, $3, $4)",
                           uuid.UUID(user["sub"]) if user.get("sub") else None, user.get("username"),
                           "create_user", _json.dumps({"target": username, "role": role}))
    return JSONResponse({"ok": True, "username": username, "role": role})


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: str, request: Request):
    """Delete a dashboard user (admin only)."""
    user = await _get_current_user(request)
    if not user or user.get("role") != "admin":
        return JSONResponse({"error": "Admin required"}, status_code=403)
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM dashboard_users WHERE id = $1", uuid.UUID(user_id))
        await conn.execute("INSERT INTO audit_log (user_id, username, action, detail) VALUES ($1, $2, $3, $4)",
                           uuid.UUID(user["sub"]) if user.get("sub") else None, user.get("username"),
                           "delete_user", _json.dumps({"target_id": user_id}))
    return JSONResponse({"ok": True})


@app.get("/api/admin/audit")
async def admin_audit_log(request: Request):
    """View audit log (admin only)."""
    user = await _get_current_user(request)
    if not user or user.get("role") != "admin":
        return JSONResponse({"error": "Admin required"}, status_code=403)
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, username, action, detail, ts FROM audit_log ORDER BY ts DESC LIMIT 100")
    return JSONResponse([{
        "id": r["id"], "username": r["username"], "action": r["action"],
        "detail": _json.loads(r["detail"]) if r["detail"] else None,
        "ts": r["ts"].isoformat() if r["ts"] else None,
    } for r in rows])


# --- Incident Routes ---

@app.get("/incidents")
async def incidents_page():
    return FileResponse(Path(__file__).parent / "static" / "incidents.html")


@app.get("/api/incidents")
async def list_incidents(status: str = "all"):
    """List all incidents, optionally filtered by status."""
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        if status == "all":
            rows = await conn.fetch("SELECT * FROM incidents ORDER BY started_at DESC LIMIT 100")
        else:
            rows = await conn.fetch("SELECT * FROM incidents WHERE status = $1 ORDER BY started_at DESC LIMIT 100", status)
    return JSONResponse([{
        "id": str(r["id"]), "title": r["title"], "root_cause": r["root_cause"],
        "status": r["status"], "severity": r["severity"],
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
        "affected_services": r["affected_services"],
        "events": _json.loads(r["events"]) if isinstance(r["events"], str) else r["events"],
    } for r in rows])


@app.post("/api/incidents")
async def create_incident(request: Request):
    """Create a new incident manually or via automation."""
    body = await request.json()
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO incidents (title, root_cause, severity, affected_services, events)
               VALUES ($1, $2, $3, $4, $5) RETURNING id, started_at""",
            body.get("title", "Untitled Incident"),
            body.get("root_cause"),
            body.get("severity", "warning"),
            body.get("affected_services", []),
            _json.dumps(body.get("events", [])),
        )
    return JSONResponse({"id": str(row["id"]), "started_at": row["started_at"].isoformat()})


@app.put("/api/incidents/{incident_id}")
async def update_incident(incident_id: str, request: Request):
    """Update incident status, root cause, etc."""
    body = await request.json()
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        sets = []
        vals = []
        idx = 1
        for field in ("title", "root_cause", "status", "severity"):
            if field in body:
                sets.append(f"{field} = ${idx}")
                vals.append(body[field])
                idx += 1
        if body.get("status") == "resolved":
            sets.append(f"resolved_at = ${idx}")
            vals.append(_dt.datetime.now(_dt.timezone.utc))
            idx += 1
        if not sets:
            return JSONResponse({"error": "No fields to update"}, status_code=400)
        vals.append(uuid.UUID(incident_id))
        await conn.execute(f"UPDATE incidents SET {', '.join(sets)} WHERE id = ${idx}", *vals)
    return JSONResponse({"ok": True})


# --- SLA Routes ---

@app.get("/sla")
async def sla_page():
    return FileResponse(Path(__file__).parent / "static" / "sla.html")


@app.get("/api/sla")
async def sla_dashboard(days: int = 30):
    """Compute rolling SLA uptime per service."""
    pool = app.state.history_pool
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT service, up, ts FROM sla_snapshots WHERE ts > $1 ORDER BY service, ts", cutoff)

    # Compute per-service uptime
    services: dict[str, dict] = {}
    for r in rows:
        svc = r["service"]
        if svc not in services:
            services[svc] = {"total": 0, "up_count": 0, "calendar": {}}
        services[svc]["total"] += 1
        if r["up"]:
            services[svc]["up_count"] += 1
        # Calendar heatmap (daily)
        day = r["ts"].strftime("%Y-%m-%d")
        if day not in services[svc]["calendar"]:
            services[svc]["calendar"][day] = {"total": 0, "up": 0}
        services[svc]["calendar"][day]["total"] += 1
        if r["up"]:
            services[svc]["calendar"][day]["up"] += 1

    result = []
    for svc, data in sorted(services.items()):
        uptime_pct = (data["up_count"] / max(1, data["total"])) * 100
        # Error budget: assume 99.9% SLA target
        target = 99.9
        budget_total = (100 - target) / 100 * days * 24 * 60  # minutes of allowed downtime
        downtime_minutes = ((data["total"] - data["up_count"]) / max(1, data["total"])) * days * 24 * 60
        budget_remaining = max(0, budget_total - downtime_minutes)
        # Calendar data
        calendar = []
        for day, counts in sorted(data["calendar"].items()):
            pct = (counts["up"] / max(1, counts["total"])) * 100
            calendar.append({"date": day, "uptime_pct": round(pct, 2)})
        result.append({
            "service": svc,
            "uptime_pct": round(uptime_pct, 4),
            "total_checks": data["total"],
            "error_budget_minutes": round(budget_total, 1),
            "budget_remaining_minutes": round(budget_remaining, 1),
            "budget_burn_pct": round((1 - budget_remaining / max(0.01, budget_total)) * 100, 1),
            "calendar": calendar,
        })
    return JSONResponse(result)


# --- Alert Rules Routes ---

@app.get("/alerts")
async def alerts_page():
    return FileResponse(Path(__file__).parent / "static" / "alerts.html")


@app.get("/api/alerts/rules")
async def list_alert_rules():
    """List all alert rules."""
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM alert_rules ORDER BY created_at")
    return JSONResponse([{
        "id": str(r["id"]), "name": r["name"], "metric": r["metric"],
        "condition": r["condition"], "threshold": r["threshold"],
        "window_minutes": r["window_minutes"], "severity": r["severity"],
        "enabled": r["enabled"], "slack_notify": r["slack_notify"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "last_triggered_at": r["last_triggered_at"].isoformat() if r["last_triggered_at"] else None,
    } for r in rows])


@app.post("/api/alerts/rules")
async def create_alert_rule(request: Request):
    """Create a new alert rule."""
    body = await request.json()
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO alert_rules (name, metric, condition, threshold, window_minutes, severity, slack_notify)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
            body.get("name", "Unnamed Rule"),
            body.get("metric", "cpu_percent"),
            body.get("condition", "gt"),
            float(body.get("threshold", 90)),
            int(body.get("window_minutes", 60)),
            body.get("severity", "warning"),
            body.get("slack_notify", True),
        )
    return JSONResponse({"id": str(row["id"]), "ok": True})


@app.put("/api/alerts/rules/{rule_id}")
async def update_alert_rule(rule_id: str, request: Request):
    """Update an alert rule."""
    body = await request.json()
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        sets = []
        vals = []
        idx = 1
        for field in ("name", "metric", "condition", "severity", "enabled", "slack_notify"):
            if field in body:
                sets.append(f"{field} = ${idx}")
                vals.append(body[field])
                idx += 1
        if "threshold" in body:
            sets.append(f"threshold = ${idx}")
            vals.append(float(body["threshold"]))
            idx += 1
        if "window_minutes" in body:
            sets.append(f"window_minutes = ${idx}")
            vals.append(int(body["window_minutes"]))
            idx += 1
        if not sets:
            return JSONResponse({"error": "No fields to update"}, status_code=400)
        vals.append(uuid.UUID(rule_id))
        await conn.execute(f"UPDATE alert_rules SET {', '.join(sets)} WHERE id = ${idx}", *vals)
    return JSONResponse({"ok": True})


@app.delete("/api/alerts/rules/{rule_id}")
async def delete_alert_rule(rule_id: str):
    """Delete an alert rule."""
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM alert_rules WHERE id = $1", uuid.UUID(rule_id))
    return JSONResponse({"ok": True})


@app.get("/api/alerts/history")
async def alert_history():
    """Get recent triggered alerts from audit_log."""
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, username, action, detail, ts FROM audit_log WHERE action = 'alert_triggered' ORDER BY ts DESC LIMIT 50")
    return JSONResponse([{
        "id": r["id"], "action": r["action"],
        "detail": _json.loads(r["detail"]) if r["detail"] else None,
        "ts": r["ts"].isoformat() if r["ts"] else None,
    } for r in rows])


# --- Capacity / Forecasting Routes ---

@app.get("/capacity")
async def capacity_page():
    return FileResponse(Path(__file__).parent / "static" / "capacity.html")


@app.get("/api/capacity")
async def capacity_forecast():
    """Linear regression on disk/memory metrics to project 'days until full'."""
    pool = app.state.history_pool
    cutoff_7d = time.time() - (7 * 86400)
    result = {"disk": {}, "memory": {}}

    async with pool.acquire() as conn:
        # Disk forecasting
        disk_rows = await conn.fetch(
            "SELECT ts, mount, percent FROM dashboard_disk_history WHERE ts > $1 ORDER BY ts", cutoff_7d)
        mounts: dict[str, list] = {}
        for r in disk_rows:
            m = r["mount"]
            if m not in mounts:
                mounts[m] = []
            if r["percent"] is not None:
                mounts[m].append((r["ts"], r["percent"]))

        for mount, points in mounts.items():
            if len(points) < 10:
                continue
            # Simple linear regression
            n = len(points)
            x_vals = [p[0] for p in points]
            y_vals = [p[1] for p in points]
            x_mean = sum(x_vals) / n
            y_mean = sum(y_vals) / n
            numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
            denominator = sum((x - x_mean) ** 2 for x in x_vals)
            if denominator == 0:
                continue
            slope = numerator / denominator  # percent per second
            if slope <= 0:
                days_until_full = None  # Usage decreasing
            else:
                current_pct = y_vals[-1]
                remaining_pct = 100 - current_pct
                seconds_until_full = remaining_pct / slope
                days_until_full = round(seconds_until_full / 86400, 1)

            result["disk"][mount] = {
                "current_pct": round(y_vals[-1], 1),
                "slope_pct_per_day": round(slope * 86400, 3),
                "days_until_full": days_until_full,
                "data_points": n,
            }

        # Memory forecasting
        mem_rows = await conn.fetch(
            "SELECT ts, memory_percent FROM dashboard_snapshots WHERE ts > $1 AND memory_percent IS NOT NULL ORDER BY ts", cutoff_7d)
        if len(mem_rows) >= 10:
            points = [(r["ts"], r["memory_percent"]) for r in mem_rows]
            n = len(points)
            x_vals = [p[0] for p in points]
            y_vals = [p[1] for p in points]
            x_mean = sum(x_vals) / n
            y_mean = sum(y_vals) / n
            numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
            denominator = sum((x - x_mean) ** 2 for x in x_vals)
            slope = numerator / denominator if denominator else 0
            result["memory"] = {
                "current_pct": round(y_vals[-1], 1),
                "slope_pct_per_day": round(slope * 86400, 4),
                "trend": "increasing" if slope > 0 else "stable" if slope == 0 else "decreasing",
                "data_points": n,
            }

    return JSONResponse(result)


# --- Anomaly Detection: evaluated in poll loop ---

async def evaluate_custom_alert_rules(state: dict):
    """Evaluate custom alert rules using z-score against 1-hour rolling window."""
    pool = app.state.history_pool
    now = time.time()
    window_start = now - 3600  # 1 hour

    async with pool.acquire() as conn:
        rules = await conn.fetch("SELECT * FROM alert_rules WHERE enabled = true")
        for rule in rules:
            metric = rule["metric"]
            condition = rule["condition"]
            threshold = rule["threshold"]

            # Get metric value from current state
            current_val = None
            if metric == "cpu_percent":
                current_val = state.get("system", {}).get("cpu_percent")
            elif metric == "memory_percent":
                current_val = state.get("system", {}).get("memory", {}).get("percent")
            elif metric.startswith("latency:"):
                svc = metric.split(":")[1]
                current_val = state.get("services", {}).get(svc, {}).get("latency_ms")
            elif metric == "ingest_queue":
                current_val = state.get("redis", {}).get("ingest_queue_depth")

            if current_val is None:
                continue

            # Z-score check against rolling window
            triggered = False
            if condition == "gt":
                triggered = current_val > threshold
            elif condition == "lt":
                triggered = current_val < threshold
            elif condition == "zscore":
                # Get historical values for z-score calculation
                if metric == "cpu_percent":
                    rows = await conn.fetch(
                        "SELECT cpu_percent FROM dashboard_snapshots WHERE ts > $1 AND cpu_percent IS NOT NULL", window_start)
                    vals = [r["cpu_percent"] for r in rows]
                elif metric == "memory_percent":
                    rows = await conn.fetch(
                        "SELECT memory_percent FROM dashboard_snapshots WHERE ts > $1 AND memory_percent IS NOT NULL", window_start)
                    vals = [r["memory_percent"] for r in rows]
                else:
                    vals = []

                if len(vals) >= 10:
                    mean = sum(vals) / len(vals)
                    std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
                    if std > 0:
                        z = (current_val - mean) / std
                        triggered = abs(z) > threshold

            if triggered:
                # Check cooldown (don't fire more than once per 5 min)
                last = rule["last_triggered_at"]
                if last and (now - last.timestamp()) < 300:
                    continue

                # Log the alert
                await conn.execute("UPDATE alert_rules SET last_triggered_at = now() WHERE id = $1", rule["id"])
                await conn.execute(
                    "INSERT INTO audit_log (username, action, detail) VALUES ($1, $2, $3)",
                    "system", "alert_triggered",
                    _json.dumps({"rule": rule["name"], "metric": metric, "value": current_val, "threshold": threshold}))

                # Slack notification (fire and forget)
                if rule["slack_notify"]:
                    asyncio.create_task(_send_slack_alert(rule["name"], metric, current_val, threshold, rule["severity"]))


async def _send_slack_alert(rule_name: str, metric: str, value, threshold, severity: str):
    """Send alert to Slack via webhook (non-blocking)."""
    try:
        webhook_url = os.environ.get("NOVA_SLACK_WEBHOOK")
        if not webhook_url:
            return
        emoji = ":rotating_light:" if severity == "critical" else ":warning:"
        payload = {
            "text": f"{emoji} *Alert: {rule_name}*\nMetric `{metric}` = {value} (threshold: {threshold})\nSeverity: {severity}"
        }
        session = app.state.http_session
        async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)):
            pass
    except Exception:
        pass


# --- Incident auto-correlation ---

_recent_down_services: dict[str, float] = {}  # service -> first_seen timestamp


async def auto_correlate_incidents(state: dict):
    """Group correlated down events within 5-min window into incidents."""
    global _recent_down_services
    now = time.time()
    services = state.get("services", {})

    currently_down = set()
    for svc, info in services.items():
        if info.get("status") in ("down", "error"):
            currently_down.add(svc)
            if svc not in _recent_down_services:
                _recent_down_services[svc] = now

    # Remove recovered services
    for svc in list(_recent_down_services):
        if svc not in currently_down:
            del _recent_down_services[svc]

    # Check for correlated outages (multiple services down within 5 min of each other)
    if len(_recent_down_services) >= 2:
        timestamps = list(_recent_down_services.values())
        time_spread = max(timestamps) - min(timestamps)
        if time_spread <= 300:  # Within 5-minute window
            # Check if we already have an open incident for these services
            affected = sorted(_recent_down_services.keys())
            pool = app.state.history_pool
            async with pool.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT id FROM incidents WHERE status = 'open' AND affected_services && $1::text[] LIMIT 1",
                    affected)
                if not existing:
                    # Create new incident
                    await conn.execute(
                        """INSERT INTO incidents (title, severity, affected_services, events)
                           VALUES ($1, $2, $3, $4)""",
                        f"Multiple services down: {', '.join(affected)}",
                        "critical" if len(affected) >= 3 else "warning",
                        affected,
                        _json.dumps([{"ts": now, "msg": f"Correlated outage detected: {', '.join(affected)}"}]),
                    )


# --- Additional state collectors for WebSocket push ---

_sla_summary_cache: dict = {}
_sla_summary_ts: float = 0


async def collect_sla_summary() -> dict:
    """Return basic SLA uptime % for last 7 days from sla_snapshots table."""
    global _sla_summary_cache, _sla_summary_ts
    now = time.time()
    if now - _sla_summary_ts < 60:
        return _sla_summary_cache
    try:
        pool = app.state.history_pool
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=7)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) as total, SUM(CASE WHEN up THEN 1 ELSE 0 END) as up_count "
                "FROM sla_snapshots WHERE ts > $1", cutoff)
        if row and row["total"] > 0:
            uptime_pct = round((row["up_count"] / row["total"]) * 100, 3)
            result = {"status": "ok", "uptime_pct_7d": uptime_pct, "checks_7d": row["total"]}
        else:
            result = {"status": "no_data", "uptime_pct_7d": None, "checks_7d": 0}
        _sla_summary_cache = result
        _sla_summary_ts = now
        return result
    except Exception as e:
        return _sla_summary_cache or {"status": "error", "error": str(e)}


_capacity_summary_cache: dict = {}
_capacity_summary_ts: float = 0


async def collect_capacity_summary() -> dict:
    """Return disk usage and estimated days until full for key volumes."""
    global _capacity_summary_cache, _capacity_summary_ts
    now = time.time()
    if now - _capacity_summary_ts < 60:
        return _capacity_summary_cache
    try:
        disks = {}
        for mount in ["/Volumes/Data", "/Volumes/MoreData", "/"]:
            try:
                st = os.statvfs(mount)
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                used_pct = round((1 - free / total) * 100, 1) if total > 0 else 0
                free_gb = round(free / (1024**3), 1)
                disks[mount] = {"used_pct": used_pct, "free_gb": free_gb}
            except OSError:
                pass

        # Get vector count growth from PostgreSQL memory counts
        vector_growth_rate = None
        try:
            pool = app.state.history_pool
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT ts, total_rows FROM dashboard_memory_count_history "
                    "WHERE ts > $1 ORDER BY ts", now - 7 * 86400)
            if len(rows) >= 2:
                first = rows[0]
                last = rows[-1]
                elapsed_days = (last["ts"] - first["ts"]) / 86400
                if elapsed_days > 0:
                    vector_growth_rate = round((last["total_rows"] - first["total_rows"]) / elapsed_days, 0)
        except Exception:
            pass

        result = {
            "status": "ok",
            "disks": disks,
            "vector_growth_per_day": vector_growth_rate,
        }
        _capacity_summary_cache = result
        _capacity_summary_ts = now
        return result
    except Exception as e:
        return _capacity_summary_cache or {"status": "error", "error": str(e)}


async def collect_nmap_summary() -> dict:
    """Get NMAP data from NovaControl status API."""
    try:
        import urllib.request as _ur
        with _ur.urlopen("http://127.0.0.1:37400/api/status", timeout=2) as _r:
            nc_data = _json.loads(_r.read())
        for svc in nc_data.get("services", []):
            if svc.get("id") == "nmap":
                return {
                    "status": "ok",
                    "summary": svc.get("summary", ""),
                    "last_updated": svc.get("lastUpdated", ""),
                }
        return {"status": "no_data", "summary": ""}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# --- New page routes ---

@app.get("/login")
async def login_page():
    return FileResponse(Path(__file__).parent / "static" / "login.html")


# --- HUD Visualization Endpoints ---

_ingest_activity_cache: list = []
_ingest_activity_ts: float = 0


@app.get("/api/ingest-activity")
async def hud_ingest_activity():
    """Recent ingest activity grouped by source (last 5 minutes)."""
    global _ingest_activity_cache, _ingest_activity_ts
    now = time.time()
    if now - _ingest_activity_ts < 10:
        return JSONResponse(_ingest_activity_cache)
    try:
        proc = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-c",
            "SELECT source, count(*) FROM memories WHERE created_at > now() - interval '5 minutes' GROUP BY source ORDER BY count DESC LIMIT 10",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        results = []
        for line in stdout.decode().strip().split("\n"):
            if "|" in line:
                parts = line.split("|")
                results.append({"source": parts[0], "count": int(parts[1])})
        _ingest_activity_cache = results
        _ingest_activity_ts = now
        return JSONResponse(results)
    except Exception as e:
        return JSONResponse(_ingest_activity_cache or [])


_random_correlation_cache: dict = {}
_random_correlation_ts: float = 0


@app.get("/api/random-correlation")
async def hud_random_correlation():
    """Pick a random memory, find its nearest neighbor in a different vector/source."""
    global _random_correlation_cache, _random_correlation_ts
    now = time.time()
    if now - _random_correlation_ts < 30:
        return JSONResponse(_random_correlation_cache)
    try:
        proc = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-c",
            """WITH random_mem AS (
                SELECT id, source, content, embedding
                FROM memories
                WHERE embedding IS NOT NULL
                ORDER BY random() LIMIT 1
            ),
            neighbor AS (
                SELECT m.id, m.source, m.content,
                       m.embedding <=> rm.embedding AS distance
                FROM memories m, random_mem rm
                WHERE m.source != rm.source
                  AND m.embedding IS NOT NULL
                  AND m.id != rm.id
                ORDER BY m.embedding <=> rm.embedding
                LIMIT 1
            )
            SELECT rm.source AS source_a, left(rm.content, 120) AS text_a,
                   n.source AS source_b, left(n.content, 120) AS text_b,
                   n.distance
            FROM random_mem rm, neighbor n""",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        line = stdout.decode().strip()
        if "|" in line:
            parts = line.split("|")
            result = {
                "source_a": parts[0],
                "text_a": parts[1],
                "source_b": parts[2],
                "text_b": parts[3],
                "distance": float(parts[4]) if parts[4] else 0,
            }
        else:
            result = {"source_a": "", "text_a": "", "source_b": "", "text_b": "", "distance": 0}
        _random_correlation_cache = result
        _random_correlation_ts = now
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse(_random_correlation_cache or {"source_a": "", "text_a": "", "source_b": "", "text_b": "", "distance": 0, "error": str(e)})


_this_day_cache: list = []
_this_day_ts: float = 0


@app.get("/api/this-day")
async def hud_this_day():
    """Personal history items from this day in previous years."""
    global _this_day_cache, _this_day_ts
    now = time.time()
    if now - _this_day_ts < 300:  # Cache 5 min
        return JSONResponse(_this_day_cache)
    try:
        today = time.strftime("%m-%d")
        month = int(today.split("-")[0])
        day = int(today.split("-")[1])
        proc = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-c",
            f"""SELECT EXTRACT(YEAR FROM created_at)::int AS year,
                       source,
                       left(content, 200) AS content
                FROM memories
                WHERE source IN ('email_archive','imessage','livejournal','journal','personal')
                  AND EXTRACT(MONTH FROM created_at) = {month}
                  AND EXTRACT(DAY FROM created_at) = {day}
                  AND created_at < CURRENT_DATE
                ORDER BY random()
                LIMIT 10""",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        results = []
        for line in stdout.decode().strip().split("\n"):
            if "|" in line:
                parts = line.split("|", 2)
                results.append({"year": int(parts[0]) if parts[0] else 0, "source": parts[1], "content": parts[2] if len(parts) > 2 else ""})
        _this_day_cache = results
        _this_day_ts = now
        return JSONResponse(results)
    except Exception as e:
        return JSONResponse(_this_day_cache or [])


_vector_health_cache: dict = {}
_vector_health_ts: float = 0


@app.get("/api/vector-health")
async def hud_vector_health():
    """Vector category health scores based on recent consolidation/deep-clean metrics."""
    global _vector_health_cache, _vector_health_ts
    now = time.time()
    if now - _vector_health_ts < 120:
        return JSONResponse(_vector_health_cache)
    try:
        proc = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-c",
            """SELECT source,
                      count(*) AS total,
                      count(*) FILTER (WHERE tier = 'core') AS core_count,
                      count(*) FILTER (WHERE tier = 'archive') AS archive_count,
                      avg(char_length(content)) AS avg_length
               FROM memories
               GROUP BY source
               HAVING count(*) > 100
               ORDER BY count(*) DESC
               LIMIT 20""",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        categories = []
        for line in stdout.decode().strip().split("\n"):
            if "|" in line:
                parts = line.split("|")
                total = int(parts[1]) if parts[1] else 0
                core = int(parts[2]) if parts[2] else 0
                archive = int(parts[3]) if parts[3] else 0
                avg_len = float(parts[4]) if parts[4] else 0
                # Health score: penalize very short content, high archive ratio
                health = 1.0
                if total > 0:
                    archive_ratio = archive / total
                    if archive_ratio > 0.5:
                        health -= 0.3
                    if avg_len < 50:
                        health -= 0.2
                    if avg_len > 500:
                        health += 0.1
                health = max(0.1, min(1.0, health))
                categories.append({
                    "source": parts[0],
                    "total": total,
                    "core": core,
                    "archive": archive,
                    "health": round(health, 2),
                })
        result = {"categories": categories}
        _vector_health_cache = result
        _vector_health_ts = now
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse(_vector_health_cache or {"categories": [], "error": str(e)})


_active_ingests_cache: list = []
_active_ingests_ts: float = 0


@app.get("/api/active-ingests")
async def hud_active_ingests():
    """Currently running ingest jobs from nova_ops."""
    global _active_ingests_cache, _active_ingests_ts
    now = time.time()
    if now - _active_ingests_ts < 10:
        return JSONResponse(_active_ingests_cache)
    try:
        results = []
        # Check ingest_jobs table if it exists
        try:
            conn = await asyncpg.connect(OPS_PG_DSN)
            try:
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='ingest_jobs')")
                if exists:
                    rows = await conn.fetch(
                        "SELECT title, vector, progress, rate, status, started_at FROM ingest_jobs WHERE status = 'running' ORDER BY started_at DESC LIMIT 10")
                    for r in rows:
                        results.append({
                            "title": r["title"],
                            "vector": r["vector"],
                            "progress": r["progress"],
                            "rate": r["rate"],
                            "status": r["status"],
                            "started_at": r["started_at"].isoformat() if hasattr(r["started_at"], 'isoformat') else str(r["started_at"]),
                        })
            finally:
                await conn.close()
        except Exception:
            pass

        # Also check ps for nova_ingest processes
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-af", "nova_ingest",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            for line in stdout.decode().strip().split("\n"):
                if line.strip() and "nova_ingest" in line:
                    results.append({"title": line.strip()[:80], "vector": "unknown", "progress": None, "rate": None, "status": "running", "started_at": None})
        except Exception:
            pass

        _active_ingests_cache = results
        _active_ingests_ts = now
        return JSONResponse(results)
    except Exception as e:
        return JSONResponse(_active_ingests_cache or [])


_scheduler_today_cache: list = []
_scheduler_today_ts: float = 0


@app.get("/api/scheduler-today")
async def hud_scheduler_today():
    """Today's task runs with timestamps and status for the Gantt chart."""
    global _scheduler_today_cache, _scheduler_today_ts
    now = time.time()
    if now - _scheduler_today_ts < 30:
        return JSONResponse(_scheduler_today_cache)
    try:
        conn = await asyncpg.connect(OPS_PG_DSN)
        try:
            # Get today's midnight in epoch ms
            today_epoch = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")) * 1000)
            rows = await conn.fetch(
                """SELECT label, status, created_at,
                          CASE WHEN ended_at > 0 AND started_at > 0 THEN ended_at - started_at ELSE NULL END as duration_ms
                   FROM task_runs
                   WHERE created_at > $1
                   ORDER BY created_at""", today_epoch)
            results = []
            for r in rows:
                # created_at is epoch ms
                ts = r["created_at"]
                hour = ((ts - today_epoch) / 3600000.0) if ts else 0
                results.append({
                    "label": r["label"] or "?",
                    "status": r["status"],
                    "hour": round(hour, 2),
                    "duration_ms": r["duration_ms"],
                    "ts": ts,
                })
        finally:
            await conn.close()
        _scheduler_today_cache = results
        _scheduler_today_ts = now
        return JSONResponse(results)
    except Exception as e:
        return JSONResponse(_scheduler_today_cache or [])


_random_memory_cache: dict = {}
_random_memory_ts: float = 0


@app.get("/api/random-memory")
async def hud_random_memory():
    """Return one random memory text + source for the spotlight feature."""
    global _random_memory_cache, _random_memory_ts
    now = time.time()
    if now - _random_memory_ts < 30:
        return JSONResponse(_random_memory_cache)
    try:
        proc = await asyncio.create_subprocess_exec(
            "psql", PG_DB, "-t", "-A", "-F", "\x1f", "-c",
            """SELECT source, left(text, 300), category,
                      EXTRACT(YEAR FROM created_at)::int
               FROM memories
               WHERE privacy IS NULL
                 AND source NOT IN ('apple_health', 'email_archive', 'bujo')
                 AND category NOT LIKE 'personal%'
                 AND length(text) BETWEEN 60 AND 350
               ORDER BY random()
               LIMIT 1""",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        line = stdout.decode().strip()
        if "\x1f" in line:
            parts = line.split("\x1f")
            result = {
                "source": parts[0] if parts[0] else "unknown",
                "text": parts[1] if len(parts) > 1 else "",
                "category": parts[2] if len(parts) > 2 else "",
                "year": int(parts[3]) if len(parts) > 3 and parts[3] else 0,
            }
        else:
            result = {"source": "unknown", "text": "No memory available", "category": "", "year": 0}
        _random_memory_cache = result
        _random_memory_ts = now
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse(_random_memory_cache or {"source": "error", "text": str(e), "category": "", "year": 0})


# --- Poll Loop ---

async def poll_loop():
    global current_state, _last_history_write
    session = app.state.http_session
    redis_client = app.state.redis

    psutil.cpu_percent(interval=None)
    await asyncio.sleep(0.1)

    while True:
        start = time.monotonic()
        results = await asyncio.gather(
            collect_scheduler(session),       # 0
            collect_agents(redis_client),     # 1
            collect_gateway(session),         # 2
            collect_task_history(),           # 3
            collect_redis_info(redis_client), # 4
            collect_services(session),        # 5
            collect_system_resources(),       # 6
            collect_ollama_models(session),   # 7
            collect_postgresql(),             # 8
            collect_flow_runs(),              # 9
            collect_task_throughput(),        # 10
            collect_model_usage(),           # 11
            collect_gateway_query_log(),     # 12
            collect_conversations(),         # 13
            collect_unifi(session),          # 14
            collect_searxng_stats(session),  # 15
            collect_backup_status(),         # 16
            collect_response_time(),         # 17
            collect_herd_activity(),         # 18
            collect_mlx_status(session),     # 19
            collect_camera_activity(),       # 20
            collect_homekit(session),        # 21
            collect_app_watchdog(),          # 22
            collect_weather(),               # 23
            collect_dream_status(),          # 24
            collect_synology_state(),        # 25
            collect_healthkit_status(),      # 26
            collect_homebridge_status(),     # 27
            collect_plex(session),           # 28
            collect_hdhr(session),           # 29
            collect_multi_agents(),          # 30
            collect_sla_summary(),           # 31
            collect_capacity_summary(),      # 32
            collect_nmap_summary(),          # 33
            return_exceptions=True,
        )

        def safe(idx):
            r = results[idx]
            if isinstance(r, BaseException):
                return {"status": "error", "error": str(r)}
            return r

        sched_data = safe(0)
        redis_data = safe(4)
        task_data = safe(3)
        svc_data = safe(5)
        traffic = collect_traffic_flow(sched_data, redis_data, task_data, svc_data)

        # Build openrouter summary from model_usage data
        model_usage_data = safe(11)
        or_provider = model_usage_data.get("by_provider", {}).get("openrouter", {}) if isinstance(model_usage_data, dict) else {}
        openrouter_summary = {
            "status": "ok" if or_provider else "no_data",
            "sessions": or_provider.get("sessions", 0),
            "tokens": or_provider.get("input_tokens", 0) + or_provider.get("output_tokens", 0),
            "cost": or_provider.get("cost", 0),
            "model": "qwen/qwen3-235b-a22b-2507",
        }

        dream_data = safe(24)

        state = {
            "ts": time.time(),
            "scheduler": sched_data,
            "agents": safe(1),
            "gateway": safe(2),
            "task_history": task_data,
            "redis": redis_data,
            "services": svc_data,
            "system": safe(6),
            "ollama": safe(7),
            "postgresql": safe(8),
            "flows": safe(9),
            "task_throughput": results[10] if not isinstance(results[10], BaseException) else [],
            "model_usage": model_usage_data,
            "gateway_queries": safe(12),
            "conversations": safe(13),
            "unifi": safe(14),
            "searxng_stats": safe(15),
            "backup_status": safe(16),
            "response_time": safe(17),
            "herd_activity": safe(18),
            "mlx_status": safe(19),
            "cameras": safe(20),
            "homekit": safe(21),
            "app_watchdog": safe(22),
            "weather": safe(23),
            "dream": dream_data,
            "dream_status": dream_data,
            "synology": safe(25),
            "healthkit": safe(26),
            "homebridge": safe(27),
            "plex": safe(28),
            "hdhr": safe(29),
            "multi_agents": safe(30),
            "openrouter": openrouter_summary,
            "sla_summary": safe(31),
            "capacity_summary": safe(32),
            "nmap": safe(33),
            "traffic_flow": traffic,
            "poll_duration_ms": round((time.monotonic() - start) * 1000),
        }

        # Inject journal stats (read from poller file — fast, no network call)
        _journal_stats_path = Path.home() / ".openclaw/workspace/state/journal_stats.json"
        try:
            _js = _json.loads(_journal_stats_path.read_text())
            # Slim down for WS broadcast — full stats available via /api/journal/stats
            state["journal"] = {
                "polled_at":      _js.get("polled_at"),
                "totals":         _js.get("totals", {}),
                "traffic":        {k: _js["traffic"][k] for k in ("total_count","total_uniques") if k in _js.get("traffic",{})},
                "section_views":  _js.get("section_views", {}),
                "stale_sections": [s for s, v in _js.get("sections", {}).items() if (v.get("age_hours") or 9999) > {"dreams":26,"essays":26,"opinions":26,"after-dark":26,"tech-today":26,"research":50,"digests":26}.get(s, 26)],
                "sections": {s: {"age_hours": v.get("age_hours"), "latest_title": v.get("latest_title",""), "posts_this_week": v.get("posts_this_week",0), "post_count": v.get("post_count",0)} for s, v in _js.get("sections", {}).items()},
                "last_deploy":    (_js.get("recent_deploys") or [{}])[0],
            }
        except Exception:
            state["journal"] = {"error": "stats not yet available"}

        # Inject Big Brother summary (read from BB API — quick loopback call)
        try:
            import urllib.request as _ur2
            with _ur2.urlopen("http://192.168.1.6:37461/bb/status", timeout=1) as _r:
                _bb = _json.loads(_r.read())
            state["big_brother"] = {
                "uptime_s":       _bb.get("uptime_s"),
                "events_total":   _bb.get("events_total"),
                "services_down":  _bb.get("services_down", []),
                "pending_restarts": _bb.get("pending_restarts", []),
            }
        except Exception:
            state["big_brother"] = {"error": "Big Brother unreachable"}

        # Evaluate alerts every poll cycle
        state["alerts"] = evaluate_alerts(state)

        current_state = state

        # Write history snapshot every 30s (non-blocking)
        now = time.time()
        if now - _last_history_write >= 30:
            _last_history_write = now
            asyncio.create_task(write_history_snapshot(state))
            # Enterprise: evaluate custom alert rules and auto-correlate incidents
            asyncio.create_task(evaluate_custom_alert_rules(state))
            asyncio.create_task(auto_correlate_incidents(state))

        dead = set()
        for ws in list(connected_clients):
            try:
                await ws.send_json(state)
            except Exception:
                dead.add(ws)
        connected_clients -= dead

        elapsed = time.monotonic() - start
        await asyncio.sleep(max(0, POLL_INTERVAL - elapsed))


# ── Analytics Dashboard API ──────────────────────────────────────────────────

@app.get("/analytics")
async def analytics_page():
    return FileResponse(Path(__file__).parent / "static" / "analytics.html")


@app.get("/api/analytics/summary")
async def analytics_summary(hours: int = 24):
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT site, SUM(views) as views, SUM(unique_visitors) as uniques
            FROM analytics_hourly
            WHERE hour >= now() - make_interval(hours => $1)
            AND path IS NOT NULL
            GROUP BY site ORDER BY views DESC
        """, hours)
        total_pv = await conn.fetchval("SELECT COUNT(*) FROM analytics_pageviews WHERE ts >= now() - make_interval(hours => $1)", hours)
        total_uniques = await conn.fetchval("SELECT COUNT(DISTINCT visitor_hash) FROM analytics_pageviews WHERE ts >= now() - make_interval(hours => $1)", hours)
    return JSONResponse({
        "total_pageviews": total_pv or 0,
        "total_unique_visitors": total_uniques or 0,
        "by_site": [{"site": r["site"], "views": r["views"], "uniques": r["uniques"]} for r in rows],
    })


@app.get("/api/analytics/timeseries")
async def analytics_timeseries(site: str = "", hours: int = 24):
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        if site:
            rows = await conn.fetch("""
                SELECT hour, SUM(views) as views, SUM(unique_visitors) as uniques
                FROM analytics_hourly
                WHERE site = $1 AND hour >= now() - make_interval(hours => $2) AND path IS NOT NULL
                GROUP BY hour ORDER BY hour
            """, site, hours)
        else:
            rows = await conn.fetch("""
                SELECT hour, SUM(views) as views, SUM(unique_visitors) as uniques
                FROM analytics_hourly
                WHERE hour >= now() - make_interval(hours => $1) AND path IS NOT NULL
                GROUP BY hour ORDER BY hour
            """, hours)
    return JSONResponse([{"hour": r["hour"].isoformat(), "views": r["views"], "uniques": r["uniques"]} for r in rows])


@app.get("/api/analytics/pages")
async def analytics_top_pages(site: str = "", hours: int = 24, limit: int = 20):
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        if site:
            rows = await conn.fetch("""
                SELECT path, SUM(views) as views, SUM(unique_visitors) as uniques
                FROM analytics_hourly
                WHERE site = $1 AND hour >= now() - make_interval(hours => $2) AND path IS NOT NULL
                GROUP BY path ORDER BY views DESC LIMIT $3
            """, site, hours, limit)
        else:
            rows = await conn.fetch("""
                SELECT site, path, SUM(views) as views
                FROM analytics_hourly
                WHERE hour >= now() - make_interval(hours => $1) AND path IS NOT NULL
                GROUP BY site, path ORDER BY views DESC LIMIT $2
            """, hours, limit)
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/analytics/referrers")
async def analytics_referrers(site: str = "", hours: int = 168):
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        query = """
            SELECT referrer_domain, COUNT(*) as views
            FROM analytics_pageviews
            WHERE ts >= now() - make_interval(hours => $1)
            AND referrer_domain IS NOT NULL AND referrer_domain != ''
        """
        params = [hours]
        if site:
            query += " AND site = $2"
            params.append(site)
        query += " GROUP BY referrer_domain ORDER BY views DESC LIMIT 20"
        rows = await conn.fetch(query, *params)
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/analytics/countries")
async def analytics_countries(site: str = "", hours: int = 168):
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        query = """
            SELECT country, COUNT(*) as views
            FROM analytics_pageviews
            WHERE ts >= now() - make_interval(hours => $1)
            AND country IS NOT NULL AND country != ''
        """
        params = [hours]
        if site:
            query += " AND site = $2"
            params.append(site)
        query += " GROUP BY country ORDER BY views DESC LIMIT 20"
        rows = await conn.fetch(query, *params)
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/analytics/devices")
async def analytics_devices(site: str = "", hours: int = 168):
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        query = """
            SELECT ua_bucket, COUNT(*) as views
            FROM analytics_pageviews
            WHERE ts >= now() - make_interval(hours => $1)
            AND ua_bucket IS NOT NULL
        """
        params = [hours]
        if site:
            query += " AND site = $2"
            params.append(site)
        query += " GROUP BY ua_bucket ORDER BY views DESC"
        rows = await conn.fetch(query, *params)
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/analytics/events")
async def analytics_events_api(site: str = "", hours: int = 24):
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        query = """
            SELECT event_type, COUNT(*) as count
            FROM analytics_events
            WHERE ts >= now() - make_interval(hours => $1)
        """
        params = [hours]
        if site:
            query += " AND site = $2"
            params.append(site)
        query += " GROUP BY event_type ORDER BY count DESC"
        rows = await conn.fetch(query, *params)
    return JSONResponse([dict(r) for r in rows])


# ── MRTG-Style SNMP Dashboard ─────────────────────────────────────────────────

@app.get("/mrtg")
async def mrtg_dashboard():
    """Serve the MRTG-style SNMP metrics dashboard."""
    return FileResponse("static/mrtg.html")


@app.get("/api/snmp/devices")
async def snmp_devices():
    """Return list of SNMP-monitored devices with latest metrics."""
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT device_name, device_ip,
                   MAX(timestamp) as last_seen
            FROM snmp_metrics
            WHERE timestamp > now() - interval '10 minutes'
            GROUP BY device_name, device_ip
            ORDER BY device_name
        """)
    return JSONResponse([{
        "name": r["device_name"],
        "ip": str(r["device_ip"]),
        "last_seen": r["last_seen"].isoformat(),
    } for r in rows])


@app.get("/api/snmp/traffic")
async def snmp_traffic(device: str = "", hours: int = 6):
    """Return interface traffic timeseries for MRTG-style graphs."""
    pool = app.state.history_pool
    hours = min(hours, 168)
    async with pool.acquire() as conn:
        query = """
            SELECT timestamp, device_name, metric_name, metric_value
            FROM snmp_metrics
            WHERE metric_name IN ('if_in_octets.0', 'if_out_octets.0')
              AND timestamp > now() - make_interval(hours => $1)
        """
        params = [hours]
        if device:
            query += " AND device_name = $2"
            params.append(device)
        query += " ORDER BY device_name, timestamp"
        rows = await conn.fetch(query, *params)

    result = {}
    for r in rows:
        dev = r["device_name"]
        if dev not in result:
            result[dev] = {"in": [], "out": []}
        direction = "in" if "in_octets" in r["metric_name"] else "out"
        result[dev][direction].append({
            "t": r["timestamp"].isoformat(),
            "v": r["metric_value"],
        })
    return JSONResponse(result)


@app.get("/api/snmp/metrics")
async def snmp_metrics_api(device: str = "", metric: str = "cpu_load_5min", hours: int = 6):
    """Return metric timeseries for a device (CPU, memory, disk, temp)."""
    pool = app.state.history_pool
    hours = min(hours, 168)
    async with pool.acquire() as conn:
        query = """
            SELECT timestamp, device_name, metric_value, unit
            FROM snmp_metrics
            WHERE metric_name = $1
              AND timestamp > now() - make_interval(hours => $2)
        """
        params = [metric, hours]
        if device:
            query += " AND device_name = $3"
            params.append(device)
        query += " ORDER BY device_name, timestamp"
        rows = await conn.fetch(query, *params)

    result = {}
    for r in rows:
        dev = r["device_name"]
        if dev not in result:
            result[dev] = {"unit": r["unit"], "data": []}
        result[dev]["data"].append({
            "t": r["timestamp"].isoformat(),
            "v": r["metric_value"],
        })
    return JSONResponse(result)


@app.get("/api/snmp/summary")
async def snmp_summary():
    """Return current snapshot of key metrics per device."""
    pool = app.state.history_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (device_name, metric_name)
                device_name, metric_name, metric_value, unit, timestamp
            FROM snmp_metrics
            WHERE metric_name IN (
                'cpu_load_5min', 'mem_total_real', 'mem_avail_real',
                'sys_temp', 'sys_uptime'
            )
            AND timestamp > now() - interval '10 minutes'
            ORDER BY device_name, metric_name, timestamp DESC
        """)
    result = {}
    for r in rows:
        dev = r["device_name"]
        if dev not in result:
            result[dev] = {}
        result[dev][r["metric_name"]] = {
            "value": r["metric_value"],
            "unit": r["unit"],
            "time": r["timestamp"].isoformat(),
        }
    return JSONResponse(result)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=37450)
