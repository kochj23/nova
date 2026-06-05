#!/usr/bin/env python3
"""
nova_inference_queue.py — Priority-based shared inference queue (Frigate pattern).

Single Redis-backed queue serving all LLM inference consumers with priority
scheduling. No source can starve others. Load shedding under pressure.

Priority levels:
  P1 = interactive (chat, Slack replies) — never shed
  P2 = proactive (briefs, alerts) — shed at depth 100
  P3 = creative (journal, dream, essays) — shed at depth 50
  P4 = background (ingest tagging, memory consolidation) — shed at depth 30

Architecture:
  - Redis sorted set for queue (score = priority * 10000 + timestamp_fraction)
  - Worker loop pops highest-priority item, routes to Ollama/MLX
  - Concurrency limiter: max 2 concurrent inference calls
  - HTTP API on port 37470: /health, /queue/stats, /queue/submit

Usage:
  # As daemon:
  python3 nova_inference_queue.py

  # As client library:
  from nova_inference_client import queue_and_wait
  result = queue_and_wait("What is 2+2?", intent="quick", priority=1)

Written by Jordan Koch.
"""

import asyncio
import json
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import redis
    from aiohttp import web
except ImportError as e:
    print(f"FATAL: missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
HTTP_PORT = 37470
BIND_ADDR = "0.0.0.0"
REDIS_URL = "redis://192.168.1.6:6379"
LOG_FILE = Path.home() / ".openclaw/logs/nova_inference_queue.log"

MAX_CONCURRENT = 2
QUEUE_KEY = "nova:inference:queue"
ACTIVE_KEY = "nova:inference:active"
RESULTS_PREFIX = "nova:inference:result:"
STATS_KEY = "nova:inference:stats"

# Load shedding thresholds (reject items at or above these depths)
SHED_THRESHOLDS = {
    4: 30,   # P4 shed when queue >= 30
    3: 50,   # P3 shed when queue >= 50
    2: 100,  # P2 shed when queue >= 100
    1: 9999, # P1 never shed
}

RESULT_TTL = 300  # results expire after 5 min

# ── State ─────────────────────────────────────────────────────────────────────

_shutdown = False
_start_time = time.time()
_stats = {
    "submitted": 0,
    "completed": 0,
    "shed": 0,
    "errors": 0,
    "active": 0,
}
_semaphore = None


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[inference-queue {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Redis ─────────────────────────────────────────────────────────────────────

_rc = None


def get_redis():
    global _rc
    if _rc is None:
        _rc = redis.from_url(REDIS_URL, decode_responses=True)
    return _rc


# ── Queue Operations ──────────────────────────────────────────────────────────

def submit_request(prompt: str, intent: str, priority: int,
                   system: str = "", model: str = "", options: dict = None,
                   callback_channel: str = "") -> dict:
    """Submit an inference request to the queue.

    Returns {"request_id": str, "queued": bool} or {"error": str, "shed": True}
    """
    rc = get_redis()
    depth = rc.zcard(QUEUE_KEY)

    # Load shedding check
    threshold = SHED_THRESHOLDS.get(priority, 9999)
    if depth >= threshold:
        _stats["shed"] += 1
        return {"error": f"Queue depth {depth} exceeds threshold {threshold} for P{priority}", "shed": True}

    request_id = str(uuid.uuid4())[:12]
    # Score: lower priority number = higher priority. Within same priority, FIFO by time.
    score = priority * 10000 + (time.time() % 10000)

    payload = json.dumps({
        "id": request_id,
        "prompt": prompt,
        "intent": intent,
        "priority": priority,
        "system": system,
        "model": model,
        "options": options or {},
        "callback_channel": callback_channel,
        "submitted_at": time.time(),
    })

    rc.zadd(QUEUE_KEY, {payload: score})
    _stats["submitted"] += 1

    return {"request_id": request_id, "queued": True, "depth": depth + 1}


def get_result(request_id: str, timeout: float = 60) -> dict:
    """Block until result is available or timeout."""
    rc = get_redis()
    key = f"{RESULTS_PREFIX}{request_id}"
    deadline = time.time() + timeout

    while time.time() < deadline:
        result = rc.get(key)
        if result:
            return json.loads(result)
        time.sleep(0.1)

    return {"error": "timeout", "request_id": request_id}


# ── Worker ────────────────────────────────────────────────────────────────────

async def process_request(payload: dict):
    """Process a single inference request via intent_router."""
    request_id = payload["id"]
    rc = get_redis()

    try:
        rc.sadd(ACTIVE_KEY, request_id)
        _stats["active"] += 1

        # Import here to avoid circular imports at module level
        from nova_intent_router import route

        start = time.time()
        result = route(
            intent=payload["intent"],
            prompt=payload["prompt"],
            system=payload.get("system") or None,
            model=payload.get("model") or None,
            options=payload.get("options") or None,
        )
        elapsed = time.time() - start

        result["request_id"] = request_id
        result["queue_wait_s"] = start - payload["submitted_at"]
        result["inference_s"] = elapsed

        # Store result for polling clients
        rc.setex(f"{RESULTS_PREFIX}{request_id}", RESULT_TTL, json.dumps(result, default=str))

        # Publish to callback channel if specified
        if payload.get("callback_channel"):
            rc.publish(payload["callback_channel"], json.dumps(result, default=str))

        _stats["completed"] += 1
        rc.hincrby(STATS_KEY, "completed", 1)
        rc.hincrby(STATS_KEY, f"p{payload['priority']}_completed", 1)

    except Exception as e:
        _stats["errors"] += 1
        error_result = {"error": str(e), "request_id": request_id}
        rc.setex(f"{RESULTS_PREFIX}{request_id}", RESULT_TTL, json.dumps(error_result))
        log(f"Request {request_id} failed: {e}", "ERROR")

    finally:
        rc.srem(ACTIVE_KEY, request_id)
        _stats["active"] -= 1


async def worker_loop():
    """Main worker: pop from queue and process with concurrency limit."""
    global _semaphore
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    rc = get_redis()

    log(f"Worker started (max_concurrent={MAX_CONCURRENT})")

    while not _shutdown:
        try:
            # Pop highest-priority item (lowest score)
            items = rc.zpopmin(QUEUE_KEY, count=1)
            if not items:
                await asyncio.sleep(0.1)
                continue

            payload_str, score = items[0]
            payload = json.loads(payload_str)

            async with _semaphore:
                # Run in executor to not block event loop (route() is synchronous)
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: asyncio.run(process_request(payload)))

        except Exception as e:
            log(f"Worker error: {e}", "ERROR")
            await asyncio.sleep(1)


# ── HTTP API ──────────────────────────────────────────────────────────────────

async def handle_health(request):
    rc = get_redis()
    depth = rc.zcard(QUEUE_KEY) if rc else 0
    active = rc.scard(ACTIVE_KEY) if rc else 0

    return web.json_response({
        "ok": True,
        "service": "nova_inference_queue",
        "version": VERSION,
        "port": HTTP_PORT,
        "uptime_s": int(time.time() - _start_time),
        "queue_depth": depth,
        "active_requests": active,
        "max_concurrent": MAX_CONCURRENT,
        "stats": _stats,
    })


async def handle_submit(request):
    """POST /queue/submit — submit an inference request."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    prompt = data.get("prompt", "").strip()
    if not prompt:
        return web.json_response({"error": "Empty prompt"}, status=400)

    result = submit_request(
        prompt=prompt,
        intent=data.get("intent", "conversation"),
        priority=data.get("priority", 2),
        system=data.get("system", ""),
        model=data.get("model", ""),
        options=data.get("options", {}),
        callback_channel=data.get("callback_channel", ""),
    )

    status = 200 if result.get("queued") else 429
    return web.json_response(result, status=status)


async def handle_result(request):
    """GET /queue/result/{request_id} — poll for result."""
    request_id = request.match_info["request_id"]
    rc = get_redis()
    key = f"{RESULTS_PREFIX}{request_id}"
    result = rc.get(key)
    if result:
        return web.json_response(json.loads(result))
    return web.json_response({"status": "pending", "request_id": request_id}, status=202)


async def handle_stats(request):
    """GET /queue/stats — queue statistics."""
    rc = get_redis()
    depth = rc.zcard(QUEUE_KEY)
    active = rc.scard(ACTIVE_KEY)
    redis_stats = rc.hgetall(STATS_KEY)

    return web.json_response({
        "depth": depth,
        "active": active,
        "by_priority": {
            f"p{p}": int(redis_stats.get(f"p{p}_completed", 0))
            for p in range(1, 5)
        },
        "total_completed": int(redis_stats.get("completed", 0)),
        "session_stats": _stats,
    })


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


async def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova Inference Queue v{VERSION} starting...")

    # HTTP API
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/queue/submit", handle_submit)
    app.router.add_get("/queue/result/{request_id}", handle_result)
    app.router.add_get("/queue/stats", handle_stats)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND_ADDR, HTTP_PORT)
    await site.start()
    log(f"HTTP API on {BIND_ADDR}:{HTTP_PORT}")

    # Worker
    worker_task = asyncio.create_task(worker_loop())

    while not _shutdown:
        await asyncio.sleep(1)

    worker_task.cancel()
    await runner.cleanup()
    log("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
