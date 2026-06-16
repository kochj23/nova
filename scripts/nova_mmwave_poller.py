#!/opt/homebrew/bin/python3
"""
nova_mmwave_poller.py — Aqara FP2 mmWave presence sensor integration for Nova.

Polls Home Assistant REST API for 4x Aqara FP2 sensors placed in:
  - Master Bedroom
  - Office
  - Living Room
  - Patio

FP2 connects via WiFi → Aqara Home → HomeKit → Home Assistant integration.
HA exposes each sensor as binary_sensor (occupancy) + per-zone entities.

Feeds zone-level presence data into telemetry.presence with method='mmwave'.
The presence_engine fuses this with BLE, Hue motion, and WiFi signals.

Written by Jordan Koch.
"""

import asyncio
import json
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import asyncpg
except ImportError as e:
    print(f"FATAL: missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))

VERSION = "2.0.0"
DB_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
HA_URL = "http://127.0.0.1:8123"
POLL_INTERVAL = 10
LOG_FILE = Path.home() / ".openclaw/logs/nova_mmwave.log"

# Entity ID patterns for Aqara FP2 sensors in HA
# HA creates entities like: binary_sensor.aqara_fp2_bedroom_occupancy
# These will be auto-discovered, but we map rooms for the ones we know
ROOM_ENTITY_MAP = {
    "bedroom": ["presence_sensor_fp2_bedroom", "aqara_fp2_bedroom"],
    "office": ["presence_sensor_fp2_office", "aqara_fp2_office"],
    "living_room": ["presence_sensor_fp2_living_room", "aqara_fp2_living_room", "aqara_fp2_living"],
    "patio": ["presence_sensor_fp2_patio", "aqara_fp2_patio"],
}

_shutdown = False
_pool = None
_start_time = time.time()
_last_state = {}  # room -> {presence, ts}
_access_token = None
_token_expires = 0

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[mmwave {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _keychain(service):
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", service, "-w"],
        capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def get_ha_token():
    """Get HA access token using refresh token from Keychain."""
    global _access_token, _token_expires

    if _access_token and time.time() < _token_expires:
        return _access_token

    refresh_token = _keychain("nova-hass-refresh-token")
    if not refresh_token:
        log("No HA refresh token in Keychain (nova-hass-refresh-token)", "ERROR")
        return None

    form_data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()

    try:
        req = urllib.request.Request(f"{HA_URL}/auth/token", data=form_data)
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        _access_token = result["access_token"]
        _token_expires = time.time() + 1800  # refresh every 30min
        return _access_token
    except Exception as e:
        log(f"Failed to get HA token: {e}", "ERROR")
        return None


def ha_api(endpoint):
    """Call HA REST API and return JSON."""
    token = get_ha_token()
    if not token:
        return None

    try:
        req = urllib.request.Request(
            f"{HA_URL}/api/{endpoint}",
            headers={"Authorization": f"Bearer {token}"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        log(f"HA API error ({endpoint}): {e}", "ERROR")
        return None


def discover_fp2_entities():
    """Auto-discover Aqara FP2 entities from HA state."""
    states = ha_api("states")
    if not states:
        return {}

    discovered = {}  # room -> [entity_ids]

    for state in states:
        eid = state["entity_id"]
        attrs = state.get("attributes", {})

        # Match by entity_id pattern or device class
        is_fp2 = False
        matched_room = None

        for room, patterns in ROOM_ENTITY_MAP.items():
            for pattern in patterns:
                if pattern in eid.lower():
                    is_fp2 = True
                    matched_room = room
                    break
            if is_fp2:
                break

        # Also match by manufacturer attribute
        if not is_fp2 and "aqara" in attrs.get("manufacturer", "").lower():
            if "fp2" in attrs.get("model", "").lower() or "presence" in eid:
                is_fp2 = True

        if is_fp2 and ("binary_sensor" in eid or "sensor" in eid):
            if matched_room:
                discovered.setdefault(matched_room, []).append(eid)
            else:
                discovered.setdefault("_unmatched", []).append(eid)

    return discovered


def poll_presence():
    """Poll HA for current FP2 sensor states."""
    states = ha_api("states")
    if not states:
        return {}

    state_map = {s["entity_id"]: s for s in states}
    results = {}  # room -> {presence, zones, confidence, metadata}

    for room, patterns in ROOM_ENTITY_MAP.items():
        presence = False
        zones = {}
        metadata = {}

        for pattern in patterns:
            # Check occupancy binary sensor
            for eid, state in state_map.items():
                if pattern not in eid.lower():
                    continue

                if "binary_sensor" in eid and "occupancy" in eid:
                    presence = state["state"] == "on"
                    metadata["occupancy_entity"] = eid
                    metadata["last_changed"] = state.get("last_changed")

                elif "binary_sensor" in eid and "zone" in eid:
                    zone_name = eid.split("_zone_")[-1] if "_zone_" in eid else eid
                    zones[zone_name] = {
                        "occupied": state["state"] == "on",
                        "last_changed": state.get("last_changed"),
                    }

                elif "sensor" in eid and "illuminance" in eid:
                    try:
                        metadata["illuminance_lux"] = float(state["state"])
                    except (ValueError, TypeError):
                        pass

        # If any zone is occupied, room is occupied
        if zones and any(z["occupied"] for z in zones.values()):
            presence = True

        if presence or zones or metadata.get("occupancy_entity"):
            results[room] = {
                "presence": presence,
                "confidence": 0.95 if presence else 0.0,
                "zones": zones,
                "metadata": metadata,
            }

    return results


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    return _pool


async def write_presence(room, confidence, metadata):
    """Write mmWave presence reading to telemetry.presence."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)
            VALUES (now(), 'mmwave_zone', $1, $2, 'mmwave', $3)
        """, room, confidence, json.dumps(metadata))


async def write_observation(room, observation):
    """Write presence transition to shared_observations."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity)
            VALUES ('mmwave_poller', 'presence', $1, $2, 'info')
        """, f"room_{room}", observation)


async def presence_loop():
    """Main loop — poll HA and write presence data."""
    await asyncio.sleep(5)
    log(f"Presence polling started (interval={POLL_INTERVAL}s)")

    # Initial discovery
    discovered = discover_fp2_entities()
    if discovered:
        log(f"Discovered FP2 entities: {json.dumps(discovered, indent=2)}")
    else:
        log("No FP2 entities found yet — sensors may not be paired")

    while not _shutdown:
        try:
            results = poll_presence()

            for room, data in results.items():
                now = time.time()
                prev = _last_state.get(room, {})
                prev_presence = prev.get("presence", None)
                prev_ts = prev.get("ts", 0)

                state_changed = (data["presence"] != prev_presence)
                heartbeat_due = (now - prev_ts) > 60

                if state_changed or heartbeat_due:
                    _last_state[room] = {
                        "presence": data["presence"],
                        "ts": now,
                    }

                    # Write to DB
                    metadata = {
                        "zones": data["zones"],
                        **data.get("metadata", {}),
                    }
                    await write_presence(room, data["confidence"], metadata)

                    if state_changed:
                        event = "enter" if data["presence"] else "leave"
                        log(f"{room}: {'PRESENT' if data['presence'] else 'EMPTY'}")
                        await write_observation(room, f"mmWave: {event} detected in {room}")

        except Exception as e:
            log(f"Poll error: {e}", "ERROR")

        await asyncio.sleep(POLL_INTERVAL)


async def health_reporter():
    """Periodically log health stats."""
    while not _shutdown:
        await asyncio.sleep(300)
        rooms_present = [r for r, s in _last_state.items() if s.get("presence")]
        log(f"Health: up {int(time.time() - _start_time)}s, "
            f"rooms tracked: {len(_last_state)}, "
            f"currently occupied: {rooms_present or 'none'}")


def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


async def main():
    global _shutdown

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova mmWave Poller v{VERSION} starting...")
    log(f"Polling Home Assistant at {HA_URL}")
    log(f"Rooms configured: {list(ROOM_ENTITY_MAP.keys())}")

    # Verify HA connectivity
    token = get_ha_token()
    if token:
        log("HA authentication successful")
    else:
        log("HA authentication failed — will retry", "WARN")

    tasks = [
        asyncio.create_task(presence_loop()),
        asyncio.create_task(health_reporter()),
    ]

    while not _shutdown:
        await asyncio.sleep(1)

    log("Shutting down...")
    for task in tasks:
        task.cancel()
    if _pool:
        await _pool.close()
    log("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
