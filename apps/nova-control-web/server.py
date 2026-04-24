import asyncio
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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

SCHEDULER_BASE = "http://127.0.0.1:37460"
GATEWAY_HEALTH = "http://127.0.0.1:18789/health"
OLLAMA_PS = "http://127.0.0.1:11434/api/ps"
REDIS_URL = "redis://127.0.0.1:6379"
TASK_DB = Path.home() / ".openclaw" / "tasks" / "runs.sqlite"
FLOW_DB = Path.home() / ".openclaw" / "flows" / "registry.sqlite"
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
