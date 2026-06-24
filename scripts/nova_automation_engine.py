#!/opt/homebrew/bin/python3
"""
nova_automation_engine.py — Rule-based home automation for Nova.

Consumes telemetry signals (presence, climate, energy, network) and triggers
actions (lights, scenes, notifications) based on configurable rules.

Covers:
  - BLE presence → automation (queue #159): room transitions trigger scenes
  - Climate intelligence (queue #141): per-room temp alerts + automation
  - Energy management (queue #140): anomaly detection on power draw
  - Predictive automation (queue #142): time-pattern learning
  - Guest mode (queue #143): unknown device detection + privacy mode

Port: 37468 (HTTP API for status + manual triggers)
Written by Jordan Koch.
"""

import asyncio
import json
import signal
import sys
import time
import subprocess
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import asyncpg
    from aiohttp import web
except ImportError as e:
    print(f"FATAL: missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))

VERSION = "1.0.0"
HTTP_PORT = 37468
DB_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
PRESENCE_URL = "http://127.0.0.1:37465/occupancy"
HUE_URL = "http://127.0.0.1:37476"
LOG_FILE = Path.home() / ".openclaw/logs/nova_automation.log"

EVAL_INTERVAL = 15
PATTERN_LEARN_INTERVAL = 3600
GUEST_CHECK_INTERVAL = 300

_shutdown = False
_pool = None
_start_time = time.time()
_rule_history = deque(maxlen=200)
_patterns = {}
_guest_state = {"active": False, "unknown_devices": [], "since": None}
_last_actions = {}

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[automation {ts}] [{level}] {msg}"
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


# ── Actions ─────────────────────────────────────────────────────────────────

async def hue_set_light(light_id: int, on: bool, brightness: int = None):
    """Set a Hue light state."""
    import urllib.request
    body = {"on": on}
    if brightness is not None and on:
        body["bri"] = max(1, min(254, brightness))
    data = json.dumps(body).encode()
    try:
        req = urllib.request.Request(
            f"{HUE_URL}/light/{light_id}/state",
            data=data, method="PUT",
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        log(f"Hue action failed (light {light_id}): {e}", "WARN")
        return False


async def run_scene(scene_name: str):
    """Trigger a predefined scene via nova_home_control."""
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/python3", str(Path(__file__).parent / "nova_home_control.py"), "scene", scene_name],
            capture_output=True, text=True, timeout=15
        )
        log(f"Scene '{scene_name}' triggered (rc={result.returncode})")
        return result.returncode == 0
    except Exception as e:
        log(f"Scene '{scene_name}' failed: {e}", "ERROR")
        return False


async def notify_slack(msg: str):
    """Send notification via Nova's Slack channel."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity)
            VALUES ('automation_engine', 'automation', 'trigger', $1, 'info')
        """, msg)


def action_cooldown(action_key: str, cooldown_s: int = 600) -> bool:
    """Returns True if action is on cooldown (should NOT fire)."""
    last = _last_actions.get(action_key, 0)
    if time.time() - last < cooldown_s:
        return True
    _last_actions[action_key] = time.time()
    return False


# ── Rule: BLE Presence → Automation (#159) ───────────────────────────────────

ROOM_LIGHTS = {
    "office": [45],
    "living_room": [43, 44, 36],
    "kitchen": [35, 38, 40],
    "bedroom": [32, 41],
    "dining": [37],
}

ROOM_BRIGHTNESS = {
    "office": 200,
    "living_room": 180,
    "kitchen": 220,
    "bedroom": 120,
    "dining": 150,
}


DARK_LUX_THRESHOLD = 40   # outdoor lux below this = dark enough to auto-light (#680)
OUTDOOR_LUX_ENTITY = "sensor.hue_outdoor_motion_sensor_1_illuminance"


async def is_dark():
    """True when it's genuinely dark OUTSIDE — real illuminance, not the clock (#680).
    Reads the Hue outdoor lux sensor; falls back to a conservative clock window only
    if there's no fresh reading (sensor offline)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            lux = await conn.fetchval(
                "SELECT state_numeric FROM telemetry.ha_sensors "
                "WHERE entity_id = $1 AND ts > now() - interval '30 minutes' "
                "ORDER BY ts DESC LIMIT 1", OUTDOOR_LUX_ENTITY)
        if lux is not None:
            return lux < DARK_LUX_THRESHOLD
    except Exception:
        pass
    h = datetime.now().hour          # fallback: sensor missing/stale
    return h >= 17 or h < 7


async def rule_presence_lights():
    """Turn on lights when Jordan enters a room, off when leaving."""
    import urllib.request
    try:
        resp = urllib.request.urlopen(PRESENCE_URL, timeout=5)
        data = json.loads(resp.read())
    except Exception:
        return

    if not data.get("ok"):
        return

    occupancy = data.get("occupancy", {})
    jordan = occupancy.get("jordan", {})
    room = jordan.get("room", "unknown")
    confidence = jordan.get("confidence", 0)
    is_home = jordan.get("home", False)

    if not is_home:
        return

    now = datetime.now()

    # Lux-gate: only auto-light when it's genuinely dark outside (real outdoor
    # illuminance, not the clock) — handles dark stormy afternoons + bright winter
    # evenings instead of a fixed 17:00 cutoff. #680.
    if not await is_dark():
        return

    if confidence < 0.5 or room == "unknown":
        return

    action_key = f"lights:{room}:on"
    if action_cooldown(action_key, cooldown_s=300):
        return

    lights = ROOM_LIGHTS.get(room, [])
    brightness = ROOM_BRIGHTNESS.get(room, 180)

    for light_id in lights:
        await hue_set_light(light_id, on=True, brightness=brightness)

    if lights:
        _rule_history.append({
            "ts": now.isoformat(),
            "rule": "presence_lights",
            "room": room,
            "action": f"lights_on ({len(lights)} lights, bri={brightness})",
        })
        log(f"RULE presence_lights: {room} → lights on (confidence={confidence})")


# ── Rule: Presence-driven device power — patio + office (Jordan 2026-06-22) ───
# When presence is detected in a zone, power its devices ON immediately. When
# presence has been gone for OFF_DELAY (15 min), power them OFF. Everything is
# driven through the NovaHomeKit power endpoint, which matches by accessory OR
# outlet/service name — so patio strip-outlets and office bulbs use one path.
NOVAHOMEKIT_POWER = "http://127.0.0.1:37433/api/accessories/power"
PRESENCE_DEVICE_ZONES = {
    # Both zones gate on the Zigbee mmWave sensors (metadata source 'fp300'); the HomeKit
    # FP2s false-positive "occupied" when the room is empty, so they can't be trusted for OFF.
    "patio":  {"devices": ["Bug Zapper", "TV, Stereo, Apple TV"], "source": "fp300"},
    "office": {"devices": ["Office Corner", "Office - Lamp", "Office accent", "Office Torcherie", "Server closet"], "source": "fp300"},
}
PRESENCE_OFF_DELAY_S = 15 * 60   # power OFF this long after the LAST positive presence reading
PRESENCE_FRESH_S = 5 * 60        # a positive reading within this window counts as "present now" (drives ON)
_zone_on = {}                    # zone -> bool: are its devices currently commanded ON


def _hk_power(name: str, on: bool) -> bool:
    import urllib.request, urllib.parse
    url = f"{NOVAHOMEKIT_POWER}?name={urllib.parse.quote(name)}&on={'true' if on else 'false'}"
    try:
        urllib.request.urlopen(url, timeout=6).read()
        return True
    except Exception as e:
        log(f"hk_power '{name}'={on} failed: {e}", "WARN")
        return False


async def rule_presence_devices():
    """Patio/office: devices ON the moment presence is seen, OFF after 15 min vacant."""
    pool = await get_pool()
    now = time.time()
    for zone, cfg in PRESENCE_DEVICE_ZONES.items():
        devices, src = cfg["devices"], cfg["source"]
        async with pool.acquire() as conn:
            # epoch of the most recent POSITIVE presence reading — i.e. when the zone last
            # actually saw someone. This is robust to the sensor never sending a "vacant" event.
            row = await conn.fetchrow(
                "SELECT extract(epoch from max(ts)) AS ep FROM telemetry.presence "
                "WHERE room = $1 AND ($2::text IS NULL OR metadata->>'source' = $2) "
                "AND confidence > 0", zone, src)
        last_seen = float(row["ep"]) if row and row["ep"] is not None else 0.0
        age = (now - last_seen) if last_seen else 1e9   # seconds since the zone last saw anyone

        if age <= PRESENCE_FRESH_S:                      # a recent positive reading -> occupied
            if not _zone_on.get(zone):                   # arrival edge -> power ON
                for d in devices:
                    await asyncio.to_thread(_hk_power, d, True)
                _zone_on[zone] = True
                log(f"RULE presence_devices: {zone} occupied → ON ({', '.join(devices)})")
                _rule_history.append({"ts": datetime.now().isoformat(),
                    "rule": "presence_devices_on", "room": zone, "action": f"on: {', '.join(devices)}"})
        elif _zone_on.get(zone) and age >= PRESENCE_OFF_DELAY_S:   # no presence for OFF_DELAY -> power OFF
            for d in devices:
                await asyncio.to_thread(_hk_power, d, False)
            _zone_on[zone] = False
            log(f"RULE presence_devices: {zone} no presence {int(age/60)}m → OFF ({', '.join(devices)})")
            _rule_history.append({"ts": datetime.now().isoformat(),
                "rule": "presence_devices_off", "room": zone, "action": f"off: {', '.join(devices)}"})


# ── Rule: Climate Intelligence (#141) ────────────────────────────────────────

CLIMATE_THRESHOLDS = {
    "outdoor": {"high_f": 105, "low_f": 32},
    "indoor": {"high_f": 82, "low_f": 62},
}


async def rule_climate_alerts():
    """Alert on extreme temperatures from weather station + Hue sensors."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        weather = await conn.fetchrow("""
            SELECT temp_f, humidity, feels_like_f
            FROM telemetry.weather
            WHERE ts > now() - interval '5 minutes'
            ORDER BY ts DESC LIMIT 1
        """)
        indoor = await conn.fetch("""
            SELECT DISTINCT ON (room) room, temp_f, humidity
            FROM telemetry.climate
            WHERE ts > now() - interval '10 minutes' AND temp_f IS NOT NULL
            ORDER BY room, ts DESC
        """)

    if weather and weather["temp_f"]:
        temp = weather["temp_f"]
        if temp >= CLIMATE_THRESHOLDS["outdoor"]["high_f"]:
            if not action_cooldown("climate:outdoor:high", 3600):
                await notify_slack(
                    f"🌡️ Extreme heat alert: {temp}°F outside "
                    f"(feels like {weather['feels_like_f'] or temp}°F). "
                    f"Humidity: {weather['humidity']}%"
                )
                _rule_history.append({
                    "ts": datetime.now().isoformat(),
                    "rule": "climate_outdoor_high",
                    "action": f"alert: {temp}°F",
                })

    for row in indoor:
        room = row["room"]
        if "outdoor" in room or "outside" in room:
            continue
        temp = row["temp_f"]
        if temp and temp >= CLIMATE_THRESHOLDS["indoor"]["high_f"]:
            key = f"climate:indoor:{room}:high"
            if not action_cooldown(key, 3600):
                log(f"RULE climate: {room} too hot ({temp}°F)")
                _rule_history.append({
                    "ts": datetime.now().isoformat(),
                    "rule": "climate_indoor_high",
                    "room": room,
                    "action": f"alert: {temp}°F",
                })


# ── Rule: Energy Anomaly Detection (#140) ────────────────────────────────────

async def rule_energy_anomalies():
    """Detect power anomalies from Eve strips (when data available)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT reltuples::bigint FROM pg_class WHERE relname = 'energy'"
        )
    if not count or count < 10:
        return

    async with pool.acquire() as conn:
        anomalies = await conn.fetch("""
            SELECT device_name, watts, ts
            FROM telemetry.energy
            WHERE ts > now() - interval '5 minutes'
              AND watts > 500
            ORDER BY watts DESC LIMIT 5
        """)

    for a in anomalies:
        key = f"energy:high:{a['device_name']}"
        if not action_cooldown(key, 3600):
            log(f"RULE energy: {a['device_name']} drawing {a['watts']}W")
            _rule_history.append({
                "ts": datetime.now().isoformat(),
                "rule": "energy_anomaly",
                "device": a["device_name"],
                "action": f"alert: {a['watts']}W",
            })


# ── Rule: Guest Mode (#143) ──────────────────────────────────────────────────

KNOWN_DEVICE_PREFIXES = [
    "Jordan", "Tricia", "Dylan", "Mac", "iPhone", "iPad", "Apple",
    "HomePod", "Nest", "interior", "exterior", "outside", "Eve",
    "Koogeek", "Onkyo", "Nintendo", "UNAS", "Outpod", "esp32",
    "Google", "Roku", "Body", "Ring", "Lutron",
]


async def rule_guest_detection():
    """Detect unknown WiFi devices that might indicate guests."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        recent_clients = await conn.fetch("""
            SELECT DISTINCT client_name, client_mac, ip
            FROM telemetry.network
            WHERE ts > now() - interval '30 minutes'
              AND client_name IS NOT NULL
              AND client_name != ''
        """)
        known_macs = await conn.fetch(
            "SELECT client_mac FROM telemetry.known_devices"
        )

    known_set = {r["client_mac"] for r in known_macs}
    unknown = []

    for client in recent_clients:
        if client["client_mac"] in known_set:
            continue
        name = client["client_name"] or "unknown"
        if any(name.lower().startswith(p.lower()) for p in KNOWN_DEVICE_PREFIXES):
            continue
        if name == "unknown" or name.startswith("\\x"):
            continue
        unknown.append({
            "name": name,
            "mac": client["client_mac"],
            "ip": client["ip"],
        })

    prev_count = len(_guest_state["unknown_devices"])
    _guest_state["unknown_devices"] = unknown

    if unknown and not _guest_state["active"]:
        _guest_state["active"] = True
        _guest_state["since"] = datetime.now(timezone.utc).isoformat()
        if not action_cooldown("guest:detected", 3600):
            names = ", ".join(d["name"] for d in unknown[:5])
            log(f"RULE guest_mode: {len(unknown)} unknown devices detected: {names}")
            _rule_history.append({
                "ts": datetime.now().isoformat(),
                "rule": "guest_detected",
                "action": f"{len(unknown)} unknown: {names}",
            })
    elif not unknown and _guest_state["active"]:
        _guest_state["active"] = False
        _guest_state["since"] = None
        log("RULE guest_mode: all unknown devices gone — guest mode off")


# ── Rule: Predictive Automation (#142) ───────────────────────────────────────

async def rule_learn_patterns():
    """Learn time-of-day patterns from presence + light state history."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                EXTRACT(DOW FROM ts) as dow,
                EXTRACT(HOUR FROM ts) as hour,
                room,
                COUNT(*) as occurrences
            FROM telemetry.presence
            WHERE ts > now() - interval '14 days'
              AND confidence > 0.5
            GROUP BY dow, hour, room
            HAVING COUNT(*) > 3
            ORDER BY occurrences DESC
            LIMIT 50
        """)

    patterns = defaultdict(list)
    for r in rows:
        dow = int(r["dow"])
        hour = int(r["hour"])
        patterns[f"{dow}:{hour}"].append({
            "room": r["room"],
            "occurrences": r["occurrences"],
        })

    _patterns.update(patterns)
    if patterns:
        log(f"PATTERN learner: {len(patterns)} time-room patterns found")


async def rule_predictive_scenes():
    """Suggest/trigger scenes based on learned patterns."""
    now = datetime.now()
    dow = now.weekday()
    # Python weekday: Mon=0..Sun=6; PG DOW: Sun=0..Sat=6
    pg_dow = (dow + 1) % 7
    hour = now.hour
    key = f"{pg_dow}:{hour}"

    if key not in _patterns:
        return

    top = _patterns[key]
    if not top:
        return

    expected_room = top[0]["room"]

    # Only trigger predictive if confidence is high (>10 occurrences in 14 days)
    if top[0]["occurrences"] < 10:
        return

    # Example: if Jordan is typically in bedroom at 10pm, suggest goodnight
    if expected_room == "bedroom" and 22 <= hour <= 23:
        if not action_cooldown("predictive:goodnight", 86400):
            log(f"PREDICTIVE: Jordan typically in bedroom at {hour}:00 — suggesting goodnight scene")
            _rule_history.append({
                "ts": now.isoformat(),
                "rule": "predictive_goodnight",
                "action": "suggest goodnight scene (pattern-based)",
            })


# ── Main Evaluation Loop ─────────────────────────────────────────────────────

async def eval_loop():
    """Run all rules every EVAL_INTERVAL seconds."""
    await asyncio.sleep(10)
    log(f"Automation engine started (interval={EVAL_INTERVAL}s)")

    while not _shutdown:
        try:
            await rule_presence_lights()
            await rule_presence_devices()
            await rule_climate_alerts()
            await rule_energy_anomalies()
            await rule_predictive_scenes()
        except Exception as e:
            log(f"Eval loop error: {e}", "ERROR")
        await asyncio.sleep(EVAL_INTERVAL)


async def guest_loop():
    """Check for guest devices periodically."""
    await asyncio.sleep(30)
    while not _shutdown:
        try:
            await rule_guest_detection()
        except Exception as e:
            log(f"Guest detection error: {e}", "ERROR")
        await asyncio.sleep(GUEST_CHECK_INTERVAL)


async def pattern_loop():
    """Learn patterns periodically."""
    await asyncio.sleep(60)
    while not _shutdown:
        try:
            await rule_learn_patterns()
        except Exception as e:
            log(f"Pattern learning error: {e}", "ERROR")
        await asyncio.sleep(PATTERN_LEARN_INTERVAL)


# ── HTTP API ─────────────────────────────────────────────────────────────────

async def handle_health(request):
    return web.json_response({
        "ok": True,
        "service": "nova_automation_engine",
        "version": VERSION,
        "uptime_s": int(time.time() - _start_time),
        "rules_fired": len(_rule_history),
    })


async def handle_status(request):
    return web.json_response({
        "ok": True,
        "version": VERSION,
        "uptime_s": int(time.time() - _start_time),
        "recent_rules": list(_rule_history)[-20:],
        "patterns_learned": len(_patterns),
        "guest_mode": _guest_state,
        "cooldowns_active": {k: int(time.time() - v) for k, v in _last_actions.items()
                            if time.time() - v < 3600},
    })


async def handle_trigger(request):
    """POST /trigger — manually trigger a scene."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    scene = body.get("scene")
    if not scene:
        return web.json_response({"error": "missing 'scene' field"}, status=400)

    ok = await run_scene(scene)
    return web.json_response({"ok": ok, "scene": scene})


async def handle_rules(request):
    """GET /rules — list all rules and their last fire time."""
    rules = {
        "presence_lights": "Turn on room lights when Jordan enters (after dark)",
        "climate_alerts": "Alert on extreme indoor/outdoor temps",
        "energy_anomalies": "Detect high power draw from Eve strips",
        "guest_detection": "Spot unknown WiFi devices (possible guests)",
        "predictive_scenes": "Suggest scenes based on 14-day time-room patterns",
    }
    return web.json_response({"rules": rules, "history_count": len(_rule_history)})


# ── Lifecycle ────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


async def main():
    global _shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova Automation Engine v{VERSION} starting...")

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/rules", handle_rules)
    app.router.add_post("/trigger", handle_trigger)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    log(f"HTTP API listening on 0.0.0.0:{HTTP_PORT}")

    tasks = [
        asyncio.create_task(eval_loop()),
        asyncio.create_task(guest_loop()),
        asyncio.create_task(pattern_loop()),
    ]

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
