#!/opt/homebrew/bin/python3
"""
nova_presence_engine.py — Room-level occupancy intelligence for Nova.

Fuses multiple signals into confident room-level presence:
  - BLE RSSI (telemetry.presence via nova_ble_monitor — every 30-40s)
  - Hue motion sensors (telemetry.climate with motion=true)
  - UniFi network clients (telemetry.network — device on WiFi = person home)
  - Power draw patterns (telemetry.energy — desk lamp on = room occupied)

Outputs:
  - Writes fused presence to shared_observations (for Nova to act on)
  - Exposes HTTP API on port 37465 for real-time queries
  - Triggers scene engine on transitions (last-person-leaves, first-arrives)

Privacy: all local, never published.

Written by Jordan Koch.
"""

import asyncio
import json
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import asyncpg
    from aiohttp import web
except ImportError as e:
    print(f"FATAL: missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VERSION = "1.0.0"
HTTP_PORT = 37465
DB_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
LOG_FILE = Path.home() / ".openclaw/logs/nova_presence.log"
POLL_INTERVAL = 30

ROOMS = [
    "office", "living_room", "bedroom", "kitchen", "garage",
    "guest_bedroom", "dylans_room", "patio", "dining",
]

PERSON_DEVICES = {
    "jordan": {
        "ble_name": "Jordan",
        "wifi_macs": [],  # populated from UniFi
        "phone_hostname": "Jordans-iPhone",
    },
}

# Confidence weights for each signal source
WEIGHTS = {
    "ble_rssi": 0.4,
    "hue_motion": 0.3,
    "power_draw": 0.2,
    "wifi_home": 0.1,
}

AWAY_THRESHOLD_MIN = 30
HOME_CONFIDENCE_THRESHOLD = 0.5

_shutdown = False
_pool = None
_start_time = time.time()
_occupancy = {}  # room -> {person, confidence, last_seen, signals}
_home_state = {}  # person -> {"home": bool, "since": ts}
_last_transition = {}

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[presence {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    return _pool


async def get_ble_presence():
    """Get latest BLE presence per person."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (person)
                person, room, confidence, ts
            FROM telemetry.presence
            WHERE ts > now() - interval '2 minutes'
            ORDER BY person, ts DESC
        """)
    return {r["person"]: {"room": r["room"], "confidence": r["confidence"], "ts": r["ts"]} for r in rows}


async def get_hue_motion():
    """Get recent Hue motion events."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT room, ts FROM telemetry.climate
            WHERE motion = true AND ts > now() - interval '5 minutes'
            ORDER BY ts DESC
        """)
    motion_rooms = {}
    for r in rows:
        room = r["room"].replace("hue_", "").replace("_motion_sensor_1", "")
        if room not in motion_rooms:
            motion_rooms[room] = r["ts"]
    return motion_rooms


async def get_wifi_home():
    """Check if known person devices are on WiFi (person is home)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT client_name FROM telemetry.network
            WHERE ts > now() - interval '10 minutes'
        """)
    client_names = {r["client_name"].lower() for r in rows if r["client_name"]}
    results = {}
    for person, cfg in PERSON_DEVICES.items():
        phone = cfg.get("phone_hostname", "").lower()
        results[person] = phone in client_names if phone else False
    return results


async def compute_occupancy():
    """Fuse all signals into room-level occupancy map."""
    ble = await get_ble_presence()
    motion = await get_hue_motion()
    wifi = await get_wifi_home()

    occupancy = {}
    for person, cfg in PERSON_DEVICES.items():
        signals = {}
        room = "unknown"
        total_confidence = 0.0

        # BLE signal
        if person in ble:
            ble_data = ble[person]
            room = ble_data["room"]
            signals["ble_rssi"] = {"room": room, "confidence": ble_data["confidence"]}
            total_confidence += ble_data["confidence"] * WEIGHTS["ble_rssi"]

        # Hue motion corroboration
        if room in motion:
            signals["hue_motion"] = {"room": room, "triggered": True}
            total_confidence += 0.9 * WEIGHTS["hue_motion"]

        # WiFi home check
        if wifi.get(person):
            signals["wifi_home"] = True
            total_confidence += 1.0 * WEIGHTS["wifi_home"]

        final_confidence = min(total_confidence / sum(WEIGHTS.values()), 1.0) if total_confidence > 0 else 0

        occupancy[person] = {
            "room": room if final_confidence > 0.3 else "unknown",
            "confidence": round(final_confidence, 2),
            "home": wifi.get(person, False) or (person in ble),
            "signals": signals,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    return occupancy


async def check_transitions(new_occupancy):
    """Detect home/away transitions and trigger scenes."""
    for person, state in new_occupancy.items():
        prev = _home_state.get(person, {})
        was_home = prev.get("home", None)
        is_home = state["home"]

        if was_home is True and not is_home:
            since = prev.get("since", datetime.now(timezone.utc))
            away_min = (datetime.now(timezone.utc) - since).total_seconds() / 60 if since else 0
            if away_min < 1:
                key = f"{person}:left"
                if key not in _last_transition or time.time() - _last_transition[key] > 1800:
                    _last_transition[key] = time.time()
                    log(f"TRANSITION: {person} left home")
                    pool = await get_pool()
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            INSERT INTO shared_observations (observer, category, subject, observation, severity)
                            VALUES ('presence_engine', 'presence', 'person_left', $1, 'info')
                        """, f"{person} left home — last seen in {prev.get('room', '?')}")

        elif was_home is False and is_home:
            key = f"{person}:arrived"
            if key not in _last_transition or time.time() - _last_transition[key] > 1800:
                _last_transition[key] = time.time()
                log(f"TRANSITION: {person} arrived home (room: {state['room']})")
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO shared_observations (observer, category, subject, observation, severity)
                        VALUES ('presence_engine', 'presence', 'person_arrived', $1, 'info')
                    """, f"{person} arrived home — detected in {state['room']}")

        _home_state[person] = {
            "home": is_home,
            "room": state["room"],
            "since": datetime.now(timezone.utc) if is_home != was_home else prev.get("since", datetime.now(timezone.utc)),
        }


async def presence_loop():
    """Main loop — compute and publish occupancy every POLL_INTERVAL seconds."""
    await asyncio.sleep(5)
    log(f"Presence engine started (interval={POLL_INTERVAL}s)")

    while not _shutdown:
        try:
            new = await compute_occupancy()
            _occupancy.update(new)
            await check_transitions(new)
        except Exception as e:
            log(f"Presence loop error: {e}", "ERROR")
        await asyncio.sleep(POLL_INTERVAL)


# ── HTTP API ─────────────────────────────────────────────────────────────────

async def handle_health(request):
    return web.json_response({
        "ok": True,
        "service": "nova_presence_engine",
        "version": VERSION,
        "uptime_s": int(time.time() - _start_time),
    })


async def handle_occupancy(request):
    """GET /occupancy — current room-level occupancy for all persons."""
    return web.json_response({
        "ok": True,
        "occupancy": _occupancy,
        "home_state": {p: {"home": s["home"], "room": s["room"]} for p, s in _home_state.items()},
        "ts": datetime.now(timezone.utc).isoformat(),
    })


async def handle_room(request):
    """GET /room/<name> — who's in a specific room."""
    room = request.match_info.get("room", "")
    people_in_room = [
        {"person": p, "confidence": s["confidence"]}
        for p, s in _occupancy.items()
        if s.get("room") == room
    ]
    return web.json_response({
        "ok": True,
        "room": room,
        "occupied": len(people_in_room) > 0,
        "people": people_in_room,
    })


# ── Lifecycle ────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


async def main():
    global _shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova Presence Engine v{VERSION} starting...")

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/occupancy", handle_occupancy)
    app.router.add_get("/room/{room}", handle_room)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    log(f"HTTP API listening on 0.0.0.0:{HTTP_PORT}")

    tasks = [asyncio.create_task(presence_loop())]

    while not _shutdown:
        await asyncio.sleep(1)

    log("Shutting down...")
    for task in tasks:
        task.cancel()
    await runner.cleanup()
    if _pool:
        await _pool.close()
    log("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
