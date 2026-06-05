#!/usr/bin/env python3
"""
nova_semantic_triggers.py — Semantic similarity triggers (Frigate pattern).

Fires actions when new memory content matches defined reference events above
a similarity threshold. Like watchers, but for semantic similarity.

Architecture:
  - Subscribes to Redis pub/sub channel nova:memory:new
  - On each new memory: compute cosine similarity against all active triggers
  - If similarity > threshold AND cooldown expired: fire action
  - Actions: slack_notify, queue_for_claude, run_script, save_to_memory

Management API on port 37472: /triggers/list, /triggers/create, /triggers/test

Written by Jordan Koch.
"""

import asyncio
import json
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import redis
    import psycopg2
    from aiohttp import web
except ImportError as e:
    print(f"FATAL: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
HTTP_PORT = 37472
BIND_ADDR = "0.0.0.0"
REDIS_URL = "redis://192.168.1.6:6379"
MEMORY_URL = "http://192.168.1.6:18790"
OPS_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
LOG_FILE = Path.home() / ".openclaw/logs/nova_semantic_triggers.log"
NEW_MEMORY_CHANNEL = "nova:memory:new"

_shutdown = False
_start_time = time.time()
_stats = {"evaluated": 0, "fired": 0, "suppressed_cooldown": 0}


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[triggers {ts}] [{level}] {msg}", flush=True)


# ── Database ──────────────────────────────────────────────────────────────────

def _db_query(sql, params=None):
    try:
        conn = psycopg2.connect(OPS_DSN)
        cur = conn.cursor()
        cur.execute(sql, params or ())
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            rows = []
        conn.commit()
        conn.close()
        return rows
    except Exception as e:
        log(f"DB error: {e}", "ERROR")
        return []


def _db_exec(sql, params=None):
    try:
        conn = psycopg2.connect(OPS_DSN)
        cur = conn.cursor()
        cur.execute(sql, params or ())
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"DB exec error: {e}", "ERROR")


# ── Embedding ─────────────────────────────────────────────────────────────────

def get_embedding(text: str) -> list:
    """Get embedding vector from memory server."""
    try:
        payload = json.dumps({"text": text[:1000]}).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/embed", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            return data.get("embedding", [])
    except Exception:
        return []


def cosine_similarity(a: list, b: list) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── Trigger Evaluation ────────────────────────────────────────────────────────

def load_active_triggers() -> list:
    """Load all enabled triggers from database."""
    return _db_query("SELECT * FROM semantic_triggers WHERE enabled = true")


def evaluate_triggers(text: str, embedding: list = None) -> list:
    """Evaluate text against all active triggers. Returns list of fired triggers."""
    if not embedding:
        embedding = get_embedding(text)
    if not embedding:
        return []

    triggers = load_active_triggers()
    fired = []
    now = time.time()

    for trigger in triggers:
        _stats["evaluated"] += 1

        # Get reference embedding (compute on-the-fly for now)
        ref_embedding = get_embedding(trigger["reference_text"])
        if not ref_embedding:
            continue

        sim = cosine_similarity(embedding, ref_embedding)

        if sim >= trigger["threshold"]:
            # Check cooldown
            last_fired = trigger.get("last_fired_at")
            if last_fired:
                elapsed = now - last_fired.timestamp()
                if elapsed < trigger["cooldown_s"]:
                    _stats["suppressed_cooldown"] += 1
                    continue

            # Fire!
            fired.append({
                "trigger_id": trigger["id"],
                "name": trigger["name"],
                "similarity": sim,
                "action_type": trigger["action_type"],
                "action_config": trigger["action_config"],
            })

            # Update fire state
            _db_exec(
                "UPDATE semantic_triggers SET last_fired_at = now(), fire_count = fire_count + 1 WHERE id = %s",
                (trigger["id"],))
            _stats["fired"] += 1

    return fired


def execute_action(trigger_result: dict, source_text: str):
    """Execute the action associated with a fired trigger."""
    action = trigger_result["action_type"]
    config = trigger_result["action_config"] if isinstance(trigger_result["action_config"], dict) else {}
    name = trigger_result["name"]
    sim = trigger_result["similarity"]

    log(f"Firing trigger '{name}' (sim={sim:.3f}, action={action})")

    if action == "slack_notify":
        channel = config.get("channel", nova_config.SLACK_NOTIFY)
        msg = (
            f":dart: *Semantic Trigger Fired*: {name}\n"
            f"  Similarity: {sim:.2f}\n"
            f"  Content: _{source_text[:150]}..._"
        )
        nova_config.post_both(msg, slack_channel=channel)

    elif action == "queue_for_claude":
        priority = config.get("priority", 3)
        _db_exec(
            "INSERT INTO claude_queue (session_id, description, status, priority) "
            "VALUES ('semantic_trigger', %s, 'queued', %s)",
            (f"Trigger '{name}' fired (sim={sim:.2f}): {source_text[:200]}", priority))

    elif action == "run_script":
        script = config.get("script", "")
        if script and Path(script).exists():
            try:
                subprocess.Popen(["/opt/homebrew/bin/python3", script],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"Script exec failed: {e}", "ERROR")

    elif action == "save_to_memory":
        source = config.get("source", "semantic_trigger_match")
        payload = json.dumps({
            "text": f"Semantic trigger '{name}' matched (sim={sim:.2f}): {source_text[:500]}",
            "source": source,
            "metadata": {"trigger": name, "similarity": sim},
        }).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/remember?async=1", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass


# ── Redis Subscriber ──────────────────────────────────────────────────────────

def subscriber_loop():
    """Subscribe to new memory events and evaluate triggers."""
    rc = redis.from_url(REDIS_URL, decode_responses=True)
    pubsub = rc.pubsub()
    pubsub.subscribe(NEW_MEMORY_CHANNEL)

    log(f"Subscribed to {NEW_MEMORY_CHANNEL}")

    for message in pubsub.listen():
        if _shutdown:
            break
        if message["type"] != "message":
            continue

        try:
            data = json.loads(message["data"])
            text = data.get("text", "")
            embedding = data.get("embedding", [])

            if not text:
                continue

            fired = evaluate_triggers(text, embedding)
            for trigger_result in fired:
                execute_action(trigger_result, text)

        except Exception as e:
            log(f"Subscriber error: {e}", "ERROR")


# ── HTTP API ──────────────────────────────────────────────────────────────────

async def handle_list(request):
    triggers = _db_query("SELECT * FROM semantic_triggers ORDER BY created_at DESC")
    return web.json_response({"ok": True, "triggers": triggers}, default=str)


async def handle_create(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    name = data.get("name", "").strip()
    ref_text = data.get("reference_text", "").strip()
    action_type = data.get("action_type", "slack_notify")
    threshold = data.get("threshold", 0.85)
    cooldown = data.get("cooldown_s", 3600)
    action_config = data.get("action_config", {})

    if not name or not ref_text:
        return web.json_response({"error": "name and reference_text required"}, status=400)

    _db_exec(
        "INSERT INTO semantic_triggers (name, reference_text, threshold, action_type, action_config, cooldown_s) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (name, ref_text, threshold, action_type, json.dumps(action_config), cooldown))

    log(f"Created trigger: {name}")
    return web.json_response({"ok": True, "name": name})


async def handle_test(request):
    """POST /triggers/test — test text against all triggers without firing."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    text = data.get("text", "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)

    embedding = get_embedding(text)
    triggers = load_active_triggers()
    results = []

    for trigger in triggers:
        ref_emb = get_embedding(trigger["reference_text"])
        if ref_emb:
            sim = cosine_similarity(embedding, ref_emb)
            results.append({
                "name": trigger["name"],
                "similarity": round(sim, 4),
                "would_fire": sim >= trigger["threshold"],
                "threshold": trigger["threshold"],
            })

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return web.json_response({"ok": True, "results": results})


async def handle_health(request):
    return web.json_response({
        "ok": True,
        "service": "nova_semantic_triggers",
        "version": VERSION,
        "uptime_s": int(time.time() - _start_time),
        "stats": _stats,
    })


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True


async def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova Semantic Triggers v{VERSION} starting...")

    # HTTP API
    app = web.Application()
    app.router.add_get("/triggers/list", handle_list)
    app.router.add_post("/triggers/create", handle_create)
    app.router.add_post("/triggers/test", handle_test)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND_ADDR, HTTP_PORT)
    await site.start()
    log(f"HTTP API on {BIND_ADDR}:{HTTP_PORT}")

    # Start subscriber in thread (blocking Redis pubsub)
    import threading
    sub_thread = threading.Thread(target=subscriber_loop, daemon=True)
    sub_thread.start()

    while not _shutdown:
        await asyncio.sleep(1)

    await runner.cleanup()
    log("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
