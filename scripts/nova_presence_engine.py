#!/opt/homebrew/bin/python3
"""
nova_presence_engine.py — Room-level occupancy intelligence for Nova.

Fuses multiple signals into confident room-level presence (noisy-OR over per-signal reliability):
  identity signals (say WHO):  BLE RSSI (ble_rssi), UniFi AP association (wifi_rssi), UniFi client
                               hostname (telemetry.network), HA companion-app zone (gps_tracker)
  room signals (say WHERE):    Aqara FP2 mmWave (mmwave), YOLO camera (camera_vision), Hue motion
  Feeds older than FEED_MAX_AGE_MIN are DEGRADED: excluded from fusion and listed in
  presence_state.detail.degraded_feeds and /health — never silently read as "nobody there".

presence_state (written here, one row per resident) is the SINGLE SOURCE OF TRUTH for occupancy:
nova_embodiment, nova_time_sense and the organ board read it instead of re-deriving "who is home".

Outputs:
  - Writes fused presence to shared_observations (for Nova to act on)
  - Exposes HTTP API on port 37465 for real-time queries
  - Triggers scene engine on transitions (last-person-leaves, first-arrives)

Privacy: all local, never published.

Written by Jordan Koch.
"""

import nova_dsn as _nova_dsn  # noqa: E402
import asyncio
import json
import os
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
DB_DSN = _nova_dsn.pg_url("nova_ops")
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
    # Added 2026-10-08 so presence_state covers every resident and embodiment/time_sense can read it
    # as the single source of truth for "who is home" instead of re-deriving it.
    "amy": {
        "phone_hostname": "Amys-iPhone",
    },
}

# Signal RELIABILITY (0..1): how much one reading, at full confidence, proves the person is where we
# say. Fused by noisy-OR — 1 - prod(1 - rel*conf) — so independent evidence ADDS UP and a sensor that
# doesn't cover the room (or is down) simply contributes nothing. Until 2026-10-08 this was a weighted
# average divided by the sum of ALL weights, so dead feeds (mmwave since the 10-06 reboot) and sensors
# that never cover Jordan's office capped confidence at ~0.2 even with BLE + WiFi + GPS all agreeing.
WEIGHTS = {
    "mmwave": 0.90,
    "camera_vision": 0.85,  # high-confidence room occupancy (YOLOv8), no identity
    "gps_tracker": 0.80,    # HA companion app zone: home/not_home (identity, no room)
    "wifi_rssi": 0.75,      # phone associated to an AP (identity + AP zone)
    "ble_rssi": 0.70,       # phone heard by the office BLE scanner (identity, coarse room)
    "wifi_home": 0.60,      # phone hostname present in UniFi client list (identity, no room)
    "hue_motion": 0.50,
    "vehicle_vision": 0.30,
}

# Feeds and how old their newest row may be before the feed is DEGRADED. A degraded feed is excluded
# from fusion and reported (presence_state.detail.degraded_feeds, /health) instead of silently reading
# as "nobody there". gps_tracker is heartbeated every 15 min by nova_ha_poller.
FEED_MAX_AGE_MIN = {
    "mmwave": 10, "camera_vision": 15, "ble_rssi": 10, "wifi_rssi": 15, "gps_tracker": 45,
}

# BLE "rooms" that are proximity bands, not places: the phone WAS heard (so: home), but where is unknown.
BLE_NON_ROOMS = ("away", "nearby", "unknown")
NON_ROOMS = ("away", "nearby", "unknown", "home", "")
# Minimum rel*conf for a single signal to name a room on its own.
ROOM_EVIDENCE_MIN = 0.25

AWAY_THRESHOLD_MIN = 30
HOME_CONFIDENCE_THRESHOLD = 0.5

_shutdown = False
_pool = None
_start_time = time.time()
_occupancy = {}  # room -> {person, confidence, last_seen, signals}
_home_state = {}  # person -> {"home": bool, "since": ts}
_last_transition = {}

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _stdout_is_logfile():
    """launchd's StandardOutPath IS LOG_FILE, so print() already lands there; appending as well wrote
    every line twice. Only append when stdout is somewhere else (tests, a terminal)."""
    try:
        a, b = os.fstat(sys.stdout.fileno()), os.stat(LOG_FILE)
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except Exception:
        return False


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[presence {ts}] [{level}] {msg}"
    print(line, flush=True)
    if _stdout_is_logfile():
        return
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


async def get_mmwave_presence():
    """Get latest mmWave presence per room (Aqara FP2 sensors)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (room)
                room, confidence, ts, metadata
            FROM telemetry.presence
            WHERE method = 'mmwave' AND ts > now() - interval '2 minutes'
            ORDER BY room, ts DESC
        """)
    return {r["room"]: {"confidence": r["confidence"], "ts": r["ts"], "metadata": r["metadata"]} for r in rows}


async def get_camera_presence():
    """Get latest camera-vision room occupancy (YOLOv8 person detection).
    Room-level only — no person identity (person='camera_detected')."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (room)
                room, confidence, ts
            FROM telemetry.presence
            WHERE method = 'camera_vision' AND ts > now() - interval '2 minutes'
            ORDER BY room, ts DESC
        """)
    return {r["room"]: {"confidence": r["confidence"], "ts": r["ts"]} for r in rows}


async def get_ble_presence():
    """Latest BLE reading per person. method = 'ble_rssi' ONLY — the old `method != 'mmwave'` filter
    let camera/vehicle/wifi/HA rows (person='unknown', 'vehicle', ...) masquerade as BLE (fixed 2026-10-08)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (person)
                person, room, confidence, ts
            FROM telemetry.presence
            WHERE method = 'ble_rssi' AND ts > now() - interval '2 minutes'
            ORDER BY person, ts DESC
        """)
    return {r["person"]: {"room": r["room"], "confidence": r["confidence"], "ts": r["ts"]} for r in rows}


async def get_wifi_rssi_presence():
    """Latest UniFi AP association per person (nova_wifi_presence) — identity + AP zone."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (person)
                person, room, confidence, ts
            FROM telemetry.presence
            WHERE method = 'wifi_rssi' AND ts > now() - interval '5 minutes'
            ORDER BY person, ts DESC
        """)
    return {r["person"]: {"room": r["room"], "confidence": r["confidence"], "ts": r["ts"]} for r in rows}


async def get_gps_presence():
    """Latest HA companion-app zone per person (gps_tracker; confidence 0.99 = home, 0.0 = not_home)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (person)
                person, confidence, ts
            FROM telemetry.presence
            WHERE method = 'gps_tracker' AND ts > now() - interval '45 minutes'
            ORDER BY person, ts DESC
        """)
    return {r["person"]: {"home": (r["confidence"] or 0) >= 0.5, "ts": r["ts"]} for r in rows}


async def get_feed_health():
    """{feed: minutes since newest row} for each FEED_MAX_AGE_MIN feed (None = no row in 2 days)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT method, extract(epoch FROM now() - max(ts)) / 60 AS age_min
            FROM telemetry.presence
            WHERE ts > now() - interval '2 days' AND method = ANY($1::text[])
            GROUP BY method
        """, list(FEED_MAX_AGE_MIN))
    ages = {r["method"]: float(r["age_min"]) for r in rows}
    return {feed: ages.get(feed) for feed in FEED_MAX_AGE_MIN}


def degraded_feeds(health):
    """Feeds whose newest row is older than allowed (or missing) — excluded from fusion."""
    return sorted(f for f, age in health.items() if age is None or age > FEED_MAX_AGE_MIN[f])


async def get_vehicle_presence():
    """Get recent vehicle detections — indicates person is home."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (person)
                person, room, confidence, ts
            FROM telemetry.presence
            WHERE method = 'vehicle_vision' AND ts > now() - interval '30 minutes'
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


RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 1.0


async def _retry(fn, what, *args):
    """Run an async PG step up to RETRY_ATTEMPTS times with exponential backoff (1 s, 2 s). A failed
    attempt is logged (never silent); the last failure is raised so the loop logs and moves on."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await fn(*args)
        except Exception as e:
            if attempt == RETRY_ATTEMPTS:
                raise
            log(f"{what} failed (attempt {attempt}/{RETRY_ATTEMPTS}): {e} — retrying", "WARN")
            await asyncio.sleep(RETRY_BACKOFF_S * 2 ** (attempt - 1))


def _noisy_or(evidence):
    """evidence: iterable of (signal, conf). 1 - prod(1 - rel*conf)."""
    p_absent = 1.0
    for sig, conf in evidence:
        p_absent *= 1.0 - WEIGHTS[sig] * max(0.0, min(1.0, float(conf or 0)))
    return 1.0 - p_absent


async def compute_occupancy():
    """Fuse all signals into a per-person {home, room, confidence} map."""
    health = await get_feed_health()
    degraded = set(degraded_feeds(health))
    mmwave = {} if "mmwave" in degraded else await get_mmwave_presence()
    camera = {} if "camera_vision" in degraded else await get_camera_presence()
    ble = {} if "ble_rssi" in degraded else await get_ble_presence()
    wifi_rssi = {} if "wifi_rssi" in degraded else await get_wifi_rssi_presence()
    gps = {} if "gps_tracker" in degraded else await get_gps_presence()
    motion = await get_hue_motion()
    wifi = await get_wifi_home()
    vehicles = await get_vehicle_presence()

    occupancy = {}
    for person, cfg in PERSON_DEVICES.items():
        signals = {}
        home_ev = []      # evidence the person is home at all
        room_votes = {}   # room -> strength (rel*conf), from identity signals that name a room

        # BLE (office scanner): a real room when strong; 'away'/'nearby' are proximity bands —
        # the phone WAS heard, so it is home evidence, never "away" and never a room.
        if person in ble:
            b = ble[person]
            signals["ble_rssi"] = {"room": b["room"], "confidence": b["confidence"]}
            home_ev.append(("ble_rssi", b["confidence"]))
            if b["room"] not in BLE_NON_ROOMS:
                room_votes[b["room"]] = room_votes.get(b["room"], 0) + WEIGHTS["ble_rssi"] * (b["confidence"] or 0)

        # WiFi AP association — identity + the AP's zone.
        if person in wifi_rssi:
            w = wifi_rssi[person]
            signals["wifi_rssi"] = {"room": w["room"], "confidence": w["confidence"]}
            home_ev.append(("wifi_rssi", w["confidence"]))
            if w["room"] not in NON_ROOMS:
                room_votes[w["room"]] = room_votes.get(w["room"], 0) + WEIGHTS["wifi_rssi"] * (w["confidence"] or 0)

        if wifi.get(person):
            signals["wifi_home"] = True
            home_ev.append(("wifi_home", 1.0))

        # GPS zone is authoritative for AWAY: if the phone says not_home, it is.
        gps_away = False
        if person in gps:
            signals["gps_tracker"] = {"home": gps[person]["home"]}
            if gps[person]["home"]:
                home_ev.append(("gps_tracker", 0.99))
            else:
                gps_away = True

        best = max(room_votes, key=room_votes.get) if room_votes else None
        room = best if best and room_votes[best] >= ROOM_EVIDENCE_MIN else "unknown"

        # mmWave/camera/hue: room-level, NO identity. They corroborate the identity room, or — if the
        # person is otherwise known to be home but unplaced — locate them when exactly one room is lit up.
        room_ev = []
        if room == "unknown" and home_ev and not gps_away:
            # identity-less sensors may PLACE a person already known to be home; they can never make a
            # specific person home on their own (a camera hit in the kitchen could be anyone).
            for feed in (mmwave, camera):
                hits = [r for r, d in feed.items() if d["confidence"] > 0.5]
                if len(hits) == 1:
                    room = hits[0]
                    break
        if room != "unknown":
            for sig, feed in (("mmwave", mmwave), ("camera_vision", camera)):
                if room in feed and feed[room]["confidence"] > 0.5:
                    signals[sig] = {"room": room, "confidence": feed[room]["confidence"]}
                    room_ev.append((sig, feed[room]["confidence"]))
        if room != "unknown" and room in motion:
            signals["hue_motion"] = {"room": room, "triggered": True}
            room_ev.append(("hue_motion", 0.9))

        if person in vehicles:
            signals["vehicle_vision"] = {"vehicle_seen": True, "camera_room": vehicles[person]["room"]}
            home_ev.append(("vehicle_vision", vehicles[person]["confidence"]))

        if gps_away and room == "unknown":
            # phone's zone says not_home and nothing in the house can name a room for this person
            # (a weak BLE band alone doesn't outvote GPS; a live AP association in a room does).
            home, confidence = False, WEIGHTS["gps_tracker"]
        else:
            home = bool(home_ev or room_ev)
            confidence = _noisy_or(home_ev + room_ev) if home else 0.0

        occupancy[person] = {
            "room": room if home else "unknown",
            "confidence": round(confidence, 2),
            "home": home,
            "signals": signals,
            "degraded_feeds": sorted(degraded),
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
            # `since` is when they became HOME, so this is how long they were home. Log a real departure
            # (home >= 1 min); ignore sub-minute flicker. Was inverted until 2026-10-06 (< 1), which logged
            # only flicker and dropped every real departure: 81 departures vs 705 arrivals.
            since = prev.get("since", datetime.now(timezone.utc))
            home_min = (datetime.now(timezone.utc) - since).total_seconds() / 60 if since else 0
            if home_min >= 1:
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


async def persist_presence_state(new_occupancy):
    """Write fused per-person state to the presence_state table — the
    authoritative source the anticipation/automation/autonomy engines read.
    entered_at advances only when the person changes room."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        for person, state in new_occupancy.items():
            if not state["home"]:
                room = "away"
            else:
                room = state["room"] if state["room"] != "unknown" else "home"
            detail = {"home": bool(state["home"]),
                      "signals": sorted(state.get("signals", {})),
                      "degraded_feeds": state.get("degraded_feeds", [])}
            await conn.execute("""
                INSERT INTO presence_state (person, room, confidence, source, activity_state, entered_at,
                                            last_confirmed, detail)
                VALUES ($1, $2, $3, 'fusion', $4, now(), now(), $5::jsonb)
                ON CONFLICT (person) DO UPDATE SET
                    room = EXCLUDED.room,
                    confidence = EXCLUDED.confidence,
                    source = 'fusion',
                    activity_state = EXCLUDED.activity_state,
                    entered_at = CASE WHEN presence_state.room <> EXCLUDED.room
                                      THEN now() ELSE presence_state.entered_at END,
                    last_confirmed = now(),
                    detail = EXCLUDED.detail
            """, person, room, float(state["confidence"]),
                 state.get("signals", {}).get("activity_state", "unknown"), json.dumps(detail))


async def presence_loop():
    """Main loop — compute and publish occupancy every POLL_INTERVAL seconds."""
    await asyncio.sleep(5)
    log(f"Presence engine started (interval={POLL_INTERVAL}s)")

    while not _shutdown:
        try:
            new = await _retry(compute_occupancy, "compute_occupancy")
            _occupancy.update(new)
            await check_transitions(new)
            await _retry(persist_presence_state, "persist_presence_state", new)
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
        "degraded_feeds": next(iter(_occupancy.values()), {}).get("degraded_feeds", []),
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
