#!/usr/bin/env python3
"""
nova_attention_zones.py — Zone-based context with inertia (Frigate zone pattern).

Logical attention zones gate notification delivery and adjust sensitivity.
Each zone has inertia (must sustain activity before activating) and loitering
detection (items stuck too long trigger nudges).

Zones:
  - work: Slack work channels, PRs, deployments — high sensitivity
  - home: smart home, security, cameras — moderate sensitivity
  - focus: DND active, deep work — only critical breaks through
  - rest: overnight, low activity — queue everything non-critical

HTTP API on port 37471: /zones/status, /zones/activate, /zones/signal

Written by Jordan Koch.
"""

import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import redis
    from aiohttp import web
except ImportError as e:
    print(f"FATAL: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))

# ── Config ────────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
HTTP_PORT = 37471
BIND_ADDR = "0.0.0.0"
REDIS_URL = "redis://192.168.1.6:6379"
LOG_FILE = Path.home() / ".openclaw/logs/nova_attention_zones.log"

# Zone definitions
ZONES = {
    "work": {
        "description": "Active work — Slack, PRs, deployments",
        "channels": ["slack_work", "github", "jira", "deploy"],
        "sensitivity": 0.8,
        "inertia_s": 60,       # 60s sustained activity to activate
        "active_hours": (8, 18),  # 8am-6pm
        "notification_gate": "all",  # allow all notifications
    },
    "home": {
        "description": "Home automation, security, cameras",
        "channels": ["homekit", "camera", "security", "protect"],
        "sensitivity": 0.6,
        "inertia_s": 30,
        "active_hours": (0, 24),  # always eligible
        "notification_gate": "all",
    },
    "focus": {
        "description": "Deep work — only critical interruptions",
        "channels": [],  # activated manually or by DND detection
        "sensitivity": 0.2,
        "inertia_s": 0,  # instant activation
        "active_hours": (0, 24),
        "notification_gate": "critical_only",
    },
    "rest": {
        "description": "Overnight — queue non-critical for morning",
        "channels": [],
        "sensitivity": 0.3,
        "inertia_s": 0,
        "active_hours": (22, 8),  # 10pm-8am (wraps midnight)
        "notification_gate": "critical_only",
    },
}

# Loitering thresholds (seconds)
LOITERING_THRESHOLDS = {
    "pr_review": 4 * 3600,      # PR unreviewed for 4h
    "incident": 30 * 60,        # Incident unresolved for 30min
    "message": 2 * 3600,        # Message unanswered for 2h
    "deployment": 1 * 3600,     # Deploy stuck for 1h
}

# ── State ─────────────────────────────────────────────────────────────────────

_shutdown = False
_start_time = time.time()
_rc = None


def get_redis():
    global _rc
    if _rc is None:
        _rc = redis.from_url(REDIS_URL, decode_responses=True)
    return _rc


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[zones {ts}] [{level}] {msg}", flush=True)


# ── Zone Logic ────────────────────────────────────────────────────────────────

def get_active_zone() -> str:
    """Determine the currently active zone based on time, signals, and inertia."""
    rc = get_redis()
    now = time.time()
    hour = datetime.now().hour

    # Check for manual override (focus mode)
    manual = rc.get("nova:zone:manual_override")
    if manual:
        return manual

    # Check DND / focus signals
    dnd = rc.get("nova:zone:dnd_active")
    if dnd == "true":
        return "focus"

    # Time-based default
    if _in_hours(hour, ZONES["rest"]["active_hours"]):
        return "rest"

    # Activity-based: check which zone has sustained signals
    best_zone = "work" if _in_hours(hour, ZONES["work"]["active_hours"]) else "home"
    for zone_name, zone_def in ZONES.items():
        if zone_name in ("focus", "rest"):
            continue
        activity_key = f"nova:zone:{zone_name}:last_signal"
        last_signal = rc.get(activity_key)
        if last_signal:
            elapsed = now - float(last_signal)
            if elapsed < zone_def["inertia_s"] * 3:
                # Recent activity in this zone
                inertia_key = f"nova:zone:{zone_name}:inertia_start"
                inertia_start = rc.get(inertia_key)
                if inertia_start and (now - float(inertia_start)) >= zone_def["inertia_s"]:
                    best_zone = zone_name

    rc.set("nova:zone:active", best_zone)
    return best_zone


def signal_zone(zone_name: str, source: str = ""):
    """Signal activity in a zone (builds inertia)."""
    rc = get_redis()
    now = time.time()

    activity_key = f"nova:zone:{zone_name}:last_signal"
    inertia_key = f"nova:zone:{zone_name}:inertia_start"

    # Check if this is continuation of existing inertia buildup
    last = rc.get(activity_key)
    if not last or (now - float(last)) > 300:
        # Gap > 5 min — reset inertia
        rc.set(inertia_key, str(now))

    rc.set(activity_key, str(now))
    rc.expire(activity_key, 600)
    rc.expire(inertia_key, 600)


def should_notify(severity: str) -> bool:
    """Should a notification be delivered given the current zone?"""
    zone = get_active_zone()
    gate = ZONES[zone]["notification_gate"]

    if gate == "all":
        return True
    elif gate == "critical_only":
        return severity in ("critical", "emergency")
    return False


def get_zone_sensitivity() -> float:
    """Get current zone's sensitivity level (0.0-1.0)."""
    zone = get_active_zone()
    return ZONES[zone]["sensitivity"]


# ── Loitering Detection ───────────────────────────────────────────────────────

def register_item(item_id: str, item_type: str, zone: str):
    """Register an item entering a zone (for loitering detection)."""
    rc = get_redis()
    key = f"nova:zone:loitering:{zone}:{item_id}"
    rc.set(key, json.dumps({"type": item_type, "entered_at": time.time()}))
    rc.expire(key, 86400)


def resolve_item(item_id: str, zone: str):
    """Remove item from loitering tracking."""
    rc = get_redis()
    rc.delete(f"nova:zone:loitering:{zone}:{item_id}")


def check_loitering() -> list:
    """Check for items that have been in a zone too long."""
    rc = get_redis()
    now = time.time()
    alerts = []

    for key in rc.scan_iter("nova:zone:loitering:*"):
        data = rc.get(key)
        if not data:
            continue
        item = json.loads(data)
        item_type = item.get("type", "unknown")
        entered = item.get("entered_at", now)
        threshold = LOITERING_THRESHOLDS.get(item_type, 8 * 3600)

        if (now - entered) > threshold:
            zone = key.split(":")[3]
            item_id = key.split(":")[-1]
            alerts.append({
                "item_id": item_id,
                "type": item_type,
                "zone": zone,
                "stuck_for_s": int(now - entered),
                "threshold_s": threshold,
            })
    return alerts


# ── Helpers ───────────────────────────────────────────────────────────────────

def _in_hours(hour: int, hours_range: tuple) -> bool:
    """Check if hour falls within range (handles midnight wrap)."""
    start, end = hours_range
    if start < end:
        return start <= hour < end
    else:
        return hour >= start or hour < end


# ── HTTP API ──────────────────────────────────────────────────────────────────

async def handle_status(request):
    zone = get_active_zone()
    rc = get_redis()
    loitering = check_loitering()

    zone_activity = {}
    for name in ZONES:
        last = rc.get(f"nova:zone:{name}:last_signal")
        zone_activity[name] = {
            "last_signal_ago_s": int(time.time() - float(last)) if last else None,
            "definition": ZONES[name],
        }

    return web.json_response({
        "ok": True,
        "active_zone": zone,
        "sensitivity": ZONES[zone]["sensitivity"],
        "notification_gate": ZONES[zone]["notification_gate"],
        "zones": zone_activity,
        "loitering_items": loitering,
        "uptime_s": int(time.time() - _start_time),
    })


async def handle_signal(request):
    """POST /zones/signal — signal activity in a zone."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    zone = data.get("zone", "")
    source = data.get("source", "")
    if zone not in ZONES:
        return web.json_response({"error": f"Unknown zone: {zone}"}, status=400)

    signal_zone(zone, source)
    return web.json_response({"ok": True, "zone": zone, "active": get_active_zone()})


async def handle_activate(request):
    """POST /zones/activate — manually set active zone (override)."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    zone = data.get("zone", "")
    duration_s = data.get("duration_s", 3600)

    if zone not in ZONES and zone != "auto":
        return web.json_response({"error": f"Unknown zone: {zone}"}, status=400)

    rc = get_redis()
    if zone == "auto":
        rc.delete("nova:zone:manual_override")
    else:
        rc.setex("nova:zone:manual_override", int(duration_s), zone)

    return web.json_response({"ok": True, "zone": zone, "duration_s": duration_s})


async def handle_should_notify(request):
    """GET /zones/should_notify?severity=warning"""
    severity = request.query.get("severity", "info")
    allowed = should_notify(severity)
    return web.json_response({
        "allowed": allowed,
        "active_zone": get_active_zone(),
        "severity": severity,
    })


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True


async def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova Attention Zones v{VERSION} starting...")

    app = web.Application()
    app.router.add_get("/zones/status", handle_status)
    app.router.add_post("/zones/signal", handle_signal)
    app.router.add_post("/zones/activate", handle_activate)
    app.router.add_get("/zones/should_notify", handle_should_notify)
    app.router.add_get("/health", handle_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND_ADDR, HTTP_PORT)
    await site.start()
    log(f"HTTP API on {BIND_ADDR}:{HTTP_PORT}")

    while not _shutdown:
        await asyncio.sleep(1)

    await runner.cleanup()
    log("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
