import asyncio
import json as _json
import os
import re
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import aiosqlite
import psutil
import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

SCHEDULER_BASE = "http://127.0.0.1:37460"
GATEWAY_HEALTH = "http://127.0.0.1:18789/health"
OLLAMA_PS = "http://127.0.0.1:11434/api/ps"
REDIS_URL = "redis://127.0.0.1:6379"
TASK_DB = Path.home() / ".openclaw" / "tasks" / "runs.sqlite"
FLOW_DB = Path.home() / ".openclaw" / "flows" / "registry.sqlite"
SESSIONS_JSON = Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"
GATEWAY_QUERY_DB = Path.home() / ".nova_gateway" / "context.db"
PG_DB = "nova_memories"

AGENTS = ["analyst", "sentinel", "coder", "lookout", "librarian"]

SERVICE_PORTS = {
    "ollama": {"port": 11434, "url": "http://127.0.0.1:11434"},
    "tinychat": {"port": 8000, "url": "http://127.0.0.1:8000"},
    "mlx_chat": {"port": 5000, "url": "http://127.0.0.1:5000"},
    "openwebui": {"port": 3000, "url": "http://127.0.0.1:3000"},
    "swarmui": {"port": 7801, "url": "http://127.0.0.1:7801"},
    "comfyui": {"port": 8188, "url": "http://127.0.0.1:8188"},
    "memory_server": {"port": 18790, "url": "http://127.0.0.1:18790"},
}

POLL_INTERVAL = 2.5
LATENCY_HISTORY_SIZE = 120  # ~5 min at 2.5s intervals
TASK_THROUGHPUT_HOURS = 24

current_state: dict = {}
connected_clients: set[WebSocket] = set()
latency_history: dict[str, deque] = {}
task_throughput_cache: list = []
task_throughput_ts: float = 0

# Traffic flow tracking
GATEWAY_LOG = Path.home() / ".openclaw" / "logs" / "gateway.log"
CHANNEL_PATTERN = re.compile(r"\[(slack|discord|signal|ws)\]")
_log_offset: int = 0
_prev_scheduler_runs: int = -1
_prev_ingest_depth: int = -1
_prev_task_total: int = -1


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_session = aiohttp.ClientSession()
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=False)
    poll_task = asyncio.create_task(poll_loop())
    yield
    poll_task.cancel()
    await app.state.http_session.close()
    await app.state.redis.close()


app = FastAPI(title="Nova Control", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


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
        elif service.startswith("agent-"):
            agent_name = service.replace("agent-", "")
            return JSONResponse(await _detail_agent(agent_name))
        elif service in ("slack", "discord", "signal", "imessage", "email"):
            return JSONResponse(await _detail_channel(service))
        elif service == "openrouter":
            return JSONResponse(await _detail_openrouter())
        elif service in ("tinychat", "mlx_chat", "openwebui", "comfyui", "swarmui"):
            return JSONResponse(await _detail_service(service))
        elif service == "memory_server":
            return JSONResponse(await _detail_memory_server())
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
    async with session.get("http://127.0.0.1:11434/api/tags", timeout=aiohttp.ClientTimeout(total=3)) as resp:
        tags = await resp.json()
    async with session.get("http://127.0.0.1:11434/api/ps", timeout=aiohttp.ClientTimeout(total=3)) as resp:
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
    async with aiosqlite.connect(f"file:{TASK_DB}?mode=ro", uri=True) as db:
        cursor = await db.execute(
            "SELECT agent_id, status, COUNT(*) FROM task_runs GROUP BY agent_id, status ORDER BY COUNT(*) DESC")
        by_agent = {}
        for agent, status, count in await cursor.fetchall():
            a = agent or "(scheduler)"
            if a not in by_agent:
                by_agent[a] = {}
            by_agent[a][status] = count

        cursor = await db.execute(
            """SELECT label, status,
                      CASE WHEN ended_at > 0 AND started_at > 0 THEN ended_at - started_at ELSE NULL END as duration,
                      created_at
               FROM task_runs ORDER BY created_at DESC LIMIT 25""")
        recent = []
        for label, status, dur, created in await cursor.fetchall():
            recent.append({"label": label or "?", "status": status, "duration_s": round(dur, 1) if dur else None, "created_at": created})

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

    async with aiosqlite.connect(f"file:{TASK_DB}?mode=ro", uri=True) as db:
        cursor = await db.execute(
            """SELECT label, status,
                      CASE WHEN ended_at > 0 AND started_at > 0 THEN ended_at - started_at ELSE NULL END,
                      created_at
               FROM task_runs WHERE agent_id = ? ORDER BY created_at DESC LIMIT 15""", (name,))
        recent = []
        for label, st, dur, created in await cursor.fetchall():
            recent.append({"label": label or "?", "status": st, "duration_s": round(dur, 1) if dur else None, "created_at": created})

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
        "ollama": {"model": "deepseek-r1:8b", "role": "Reasoning, analysis, generalist default"},
        "tinychat": {"model": "deepseek-r1:8b", "role": "Lightweight chat, quick responses, email"},
        "mlx_chat": {"model": "Qwen2.5-7B-Instruct-4bit", "role": "Fast general inference (Apple ANE)"},
        "openwebui": {"model": "qwen3-vl:4b", "role": "Vision, UI, multimodal, RAG"},
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
        async with session.get("http://127.0.0.1:18790/health", timeout=aiohttp.ClientTimeout(total=3)) as resp:
            health = await resp.json()
    except Exception:
        pass
    try:
        async with session.get("http://127.0.0.1:18790/stats", timeout=aiohttp.ClientTimeout(total=3)) as resp:
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


async def collect_gateway(session: aiohttp.ClientSession) -> dict:
    try:
        async with session.get(GATEWAY_HEALTH, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            data = await resp.json()
        ws_reachable = True
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 18789), timeout=1.0
            )
            writer.close()
            await writer.wait_closed()
        except Exception:
            ws_reachable = False

        return {
            "status": "ok",
            "ok": data.get("ok", False),
            "gateway_status": data.get("status", "unknown"),
            "ws_reachable": ws_reachable,
        }
    except Exception as e:
        return {"status": "error", "ok": False, "gateway_status": "down", "ws_reachable": False, "error": str(e)}


async def collect_task_history() -> dict:
    try:
        async with aiosqlite.connect(f"file:{TASK_DB}?mode=ro", uri=True) as db:
            cursor = await db.execute("SELECT status, COUNT(*) FROM task_runs GROUP BY status")
            rows = await cursor.fetchall()
            all_time = {}
            for status, count in rows:
                all_time[status] = count

            day_ago = time.time() - 86400
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM task_runs WHERE created_at > ? GROUP BY status",
                (day_ago,)
            )
            rows = await cursor.fetchall()
            last_24h = {}
            for status, count in rows:
                last_24h[status] = count

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
        start = time.monotonic()
        try:
            async with session.get(info["url"], timeout=aiohttp.ClientTimeout(total=1.5)) as resp:
                latency = round((time.monotonic() - start) * 1000)
                results[name] = {"status": "up", "port": info["port"], "http_code": resp.status, "latency_ms": latency}
        except Exception:
            try:
                t0 = time.monotonic()
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", info["port"]), timeout=1.0
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
            if part.mountpoint in ("/", "/System/Volumes/Data", "/Volumes/Data", "/Volumes/MoreData"):
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

        return {
            "status": "ok",
            "db_size_gb": round(db_size_bytes / (1024**3), 2),
            "total_rows": total_rows,
            "tables": tables,
            "index_count": index_count,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "db_size_gb": 0, "total_rows": 0, "tables": [], "index_count": 0}


async def collect_flow_runs() -> dict:
    try:
        async with aiosqlite.connect(f"file:{FLOW_DB}?mode=ro", uri=True) as db:
            cursor = await db.execute("SELECT status, COUNT(*) FROM flow_runs GROUP BY status")
            rows = await cursor.fetchall()
            flows = {}
            for status, count in rows:
                flows[status] = count
        return {"status": "ok", "flows": flows}
    except Exception as e:
        return {"status": "error", "error": str(e), "flows": {}}


async def collect_task_throughput() -> list:
    global task_throughput_cache, task_throughput_ts
    now = time.time()
    if now - task_throughput_ts < 60:
        return task_throughput_cache
    try:
        cutoff = now - (TASK_THROUGHPUT_HOURS * 3600)
        async with aiosqlite.connect(f"file:{TASK_DB}?mode=ro", uri=True) as db:
            cursor = await db.execute(
                """SELECT CAST((created_at - ?) / 3600 AS INTEGER) as hour_bucket,
                          status, COUNT(*)
                   FROM task_runs
                   WHERE created_at > ?
                   GROUP BY hour_bucket, status
                   ORDER BY hour_bucket""",
                (cutoff, cutoff)
            )
            rows = await cursor.fetchall()
        buckets = {}
        for hour_bucket, status, count in rows:
            h = int(hour_bucket)
            if h not in buckets:
                buckets[h] = {"hour": h, "succeeded": 0, "failed": 0, "timed_out": 0, "lost": 0}
            if status in buckets[h]:
                buckets[h][status] = count
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
        if not GATEWAY_QUERY_DB.exists() or GATEWAY_QUERY_DB.stat().st_size == 0:
            return {"status": "empty", "backends": {}, "total_queries": 0}

        async with aiosqlite.connect(f"file:{GATEWAY_QUERY_DB}?mode=ro", uri=True) as db:
            cursor = await db.execute(
                """SELECT backend_used, model_used, COUNT(*) as cnt,
                          AVG(latency_ms) as avg_lat,
                          SUM(prompt_length) as total_prompt,
                          SUM(response_length) as total_response,
                          SUM(fallback_used) as fallbacks
                   FROM query_log GROUP BY backend_used, model_used"""
            )
            rows = await cursor.fetchall()
            backends = {}
            total_queries = 0
            for backend, model, cnt, avg_lat, total_prompt, total_resp, fallbacks in rows:
                total_queries += cnt
                if backend not in backends:
                    backends[backend] = {"models": {}, "total_queries": 0, "total_prompt_chars": 0, "total_response_chars": 0}
                backends[backend]["total_queries"] += cnt
                backends[backend]["total_prompt_chars"] += total_prompt or 0
                backends[backend]["total_response_chars"] += total_resp or 0
                backends[backend]["models"][model] = {
                    "queries": cnt,
                    "avg_latency_ms": round(avg_lat or 0),
                    "prompt_chars": total_prompt or 0,
                    "response_chars": total_resp or 0,
                    "fallbacks": fallbacks or 0,
                }

            cursor2 = await db.execute("SELECT COUNT(*) FROM query_log")
            total = (await cursor2.fetchone())[0]

        return {"status": "ok", "backends": backends, "total_queries": total}
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


# --- Poll Loop ---

async def poll_loop():
    global current_state
    session = app.state.http_session
    redis_client = app.state.redis

    psutil.cpu_percent(interval=None)
    await asyncio.sleep(0.1)

    while True:
        start = time.monotonic()
        results = await asyncio.gather(
            collect_scheduler(session),
            collect_agents(redis_client),
            collect_gateway(session),
            collect_task_history(),
            collect_redis_info(redis_client),
            collect_services(session),
            collect_system_resources(),
            collect_ollama_models(session),
            collect_postgresql(),
            collect_flow_runs(),
            collect_task_throughput(),
            collect_model_usage(),
            collect_gateway_query_log(),
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
            "model_usage": safe(11),
            "gateway_queries": safe(12),
            "traffic_flow": traffic,
            "poll_duration_ms": round((time.monotonic() - start) * 1000),
        }
        current_state = state

        dead = set()
        for ws in list(connected_clients):
            try:
                await ws.send_json(state)
            except Exception:
                dead.add(ws)
        connected_clients -= dead

        elapsed = time.monotonic() - start
        await asyncio.sleep(max(0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=37450)
