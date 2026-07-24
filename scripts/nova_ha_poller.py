#!/opt/homebrew/bin/python3
"""
nova_ha_poller.py — Poll Home Assistant for all sensor data and feed Nova.

Reads from HA REST API every 30s and writes to:
  - telemetry.climate (temperature, illuminance, humidity)
  - telemetry.presence (light-inferred occupancy, motion, media activity)
  - shared_observations (state transitions)

Covers: Hue lights (43), media players (20), motion sensors, Lutron switches.

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

VERSION = "1.0.0"
DB_DSN = "postgresql://kochj@pg-primary.digitalnoise.net:5432/nova_ops"
HA_URL = "http://127.0.0.1:8123"
POLL_INTERVAL = 30
LOG_FILE = Path.home() / ".openclaw/logs/nova_ha_poller.log"

# Room inference from light entity names
LIGHT_ROOMS = {
    "office": ["office", "office_accent", "office_corner", "office_lamp"],
    "kitchen": ["kitchen", "kitchen_buddha", "kitchen_coffee", "kitchen_fridge",
                "kitchen_knives", "kitchen_lightstrip", "kitchen_main_lights",
                "kitchen_toaster", "kitchen_tv"],
    "living_room": ["living_room", "living_room_cat", "living_room_chairs",
                    "living_room_couch", "living_room_lamp", "living_room_main_lights_1",
                    "living_room_tv", "lr_tv_2"],
    "bedroom": ["master_lamp", "master_tv", "bedroom_lamp"],
    "dining": ["dining_room_lamp", "dining_room_light"],
    "patio": ["outdoor_patio_2", "outside", "patio_lamp", "patio_light_2"],
    "garage": ["garage_light_2", "garage_light_3", "carport_light"],
    "hall": ["hall", "bar"],
    "server_closet": ["server_closet"],
}

# Media player -> room mapping
MEDIA_ROOMS = {
    "officepod": "office",
    "office": "office",
    "office_hub": "office",
    "kitchen": "kitchen",
    "living_room_speaker": "living_room",
    "living_room_speaker_2": "living_room",
    "bedroom": "bedroom",
    "master_bedroom_hub": "bedroom",
    "master_bedroom_2": "bedroom",
    "master_bedroom_4": "living_room",
    "mbath": "bedroom",
    "dylans_room": "dylans_room",
    "guest_bedroom": "guest_bedroom",
    "guest_bedroom_hub": "guest_bedroom",
    "gbath": "guest_bedroom",
    "garage_display": "garage",
    "garpod": "garage",
    "outpod": "patio",
    "backdoorpod": "patio",
    "patio_display": "patio",
    "onkyo_tx_nr5100_f56435": "living_room",
}

_shutdown = False
_pool = None
_start_time = time.time()
_access_token = None
_token_expires = 0
_prev_light_state = {}  # room -> bool (any light on)
_prev_motion_state = {}
_prev_media_state = {}  # room -> active/idle

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[ha_poller {ts}] [{level}] {msg}"
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
    global _access_token, _token_expires
    if _access_token and time.time() < _token_expires:
        return _access_token

    refresh_token = _keychain("nova-hass-refresh-token")
    if not refresh_token:
        log("No HA refresh token in Keychain", "ERROR")
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
        _token_expires = time.time() + 1800
        return _access_token
    except Exception as e:
        log(f"Failed to get HA token: {e}", "ERROR")
        return None


def ha_get_states():
    token = get_ha_token()
    if not token:
        return None
    try:
        req = urllib.request.Request(f"{HA_URL}/api/states",
            headers={"Authorization": f"Bearer {token}"})
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except Exception as e:
        log(f"HA API error: {e}", "ERROR")
        return None


def analyze_lights(states):
    """Determine which rooms have lights on -> occupancy signal."""
    room_lit = {}
    for s in states:
        eid = s["entity_id"]
        if not eid.startswith("light."):
            continue
        name = eid.replace("light.", "")
        state = s["state"]
        brightness = s.get("attributes", {}).get("brightness")

        for room, light_names in LIGHT_ROOMS.items():
            if name in light_names:
                if state == "on" and brightness and brightness > 10:
                    room_lit[room] = True
                elif room not in room_lit:
                    room_lit[room] = False
                break

    return room_lit


def analyze_media(states):
    """Determine which rooms have active media -> presence signal."""
    room_active = {}
    for s in states:
        eid = s["entity_id"]
        if not eid.startswith("media_player."):
            continue
        name = eid.replace("media_player.", "")
        state = s["state"]
        attrs = s.get("attributes", {})

        room = MEDIA_ROOMS.get(name)
        if not room:
            continue

        is_active = state in ("playing", "paused", "standby") and state != "off"
        is_playing = state == "playing"

        if room not in room_active or is_playing:
            room_active[room] = {
                "active": is_active,
                "playing": is_playing,
                "app": attrs.get("app_name", ""),
                "title": attrs.get("media_title", ""),
                "state": state,
            }

    return room_active


def analyze_sensors(states):
    """Extract climate and motion data."""
    climate = {}
    motion = {}

    for s in states:
        eid = s["entity_id"]
        attrs = s.get("attributes", {})
        device_class = attrs.get("device_class", "")

        if eid.startswith("binary_sensor.") and device_class == "motion":
            detected = s["state"] == "on"
            motion[eid] = detected
            # Feed the outdoor Hue motion sensor into the climate row
            # (climate is the hue_outdoor / outdoor_front device); otherwise
            # write_climate always inserts motion=False.
            if "hue_outdoor" in eid:
                climate["motion_detected"] = detected

        elif eid.startswith("sensor."):
            if device_class == "temperature" and "hue_outdoor" in eid:
                try:
                    climate["temperature_f"] = float(s["state"])
                except (ValueError, TypeError):
                    pass
            elif device_class == "illuminance" and "hue_outdoor" in eid:
                try:
                    climate["illuminance_lux"] = float(s["state"])
                except (ValueError, TypeError):
                    pass
            elif device_class == "battery" and "hue_outdoor" in eid:
                try:
                    climate["motion_sensor_battery"] = int(s["state"])
                except (ValueError, TypeError):
                    pass

    return climate, motion


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    return _pool


async def write_climate(climate):
    if not climate.get("temperature_f"):
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO telemetry.climate (ts, room, source, temp_f, humidity, light_lux, motion)
            VALUES (now(), 'outdoor_front', 'ha_hue_sensor', $1, NULL, $2, $3)
        """, climate.get("temperature_f", 0),
            climate.get("illuminance_lux"),
            climate.get("motion_detected", False))


async def write_presence_from_lights(room_lit):
    """Write light-inferred presence to telemetry."""
    global _prev_light_state
    pool = await get_pool()

    for room, lit in room_lit.items():
        prev = _prev_light_state.get(room)
        if prev == lit:
            continue

        _prev_light_state[room] = lit
        confidence = 0.6 if lit else 0.0

        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)
                VALUES (now(), 'light_inferred', $1, $2, 'ha_lights', $3)
            """, room, confidence, json.dumps({"lights_on": lit}))

        if lit and prev is False:
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO shared_observations (observer, category, subject, observation, severity)
                    VALUES ('ha_poller', 'presence', $1, $2, 'info')
                """, f"room_{room}", f"Lights turned on in {room}")


async def write_presence_from_media(room_media):
    """Write media-inferred presence to telemetry."""
    global _prev_media_state
    pool = await get_pool()

    for room, data in room_media.items():
        is_active = data["active"]
        prev_active = _prev_media_state.get(room, {}).get("active")

        if prev_active == is_active:
            continue

        _prev_media_state[room] = data
        confidence = 0.5 if is_active else 0.0

        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)
                VALUES (now(), 'media_inferred', $1, $2, 'ha_media', $3)
            """, room, confidence, json.dumps(data))


async def write_motion(motion):
    """Write motion sensor events."""
    global _prev_motion_state
    pool = await get_pool()

    for sensor_id, detected in motion.items():
        prev = _prev_motion_state.get(sensor_id)
        if prev == detected:
            continue

        _prev_motion_state[sensor_id] = detected
        if detected:
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)
                    VALUES (now(), 'motion', 'outdoor_front', 0.8, 'ha_motion', $1)
                """, json.dumps({"sensor": sensor_id}))


_prev_tracker_state = {}  # person -> home/not_home


async def write_device_tracker(states):
    """Write device_tracker (phone GPS) as definitive home/away signal."""
    global _prev_tracker_state
    pool = await get_pool()

    for s in states:
        eid = s["entity_id"]
        if not eid.startswith("device_tracker."):
            continue

        state = s["state"]  # "home" or "not_home"
        attrs = s.get("attributes", {})
        friendly = attrs.get("friendly_name", "")

        # Map device tracker to person
        person = None
        if "jordan" in eid.lower():
            person = "jordan"
        elif "amy" in eid.lower() or "tricia" in eid.lower():
            person = "amy"
        else:
            continue

        prev = _prev_tracker_state.get(person)
        if prev == state:
            continue
        _prev_tracker_state[person] = state

        is_home = state == "home"
        confidence = 0.99 if is_home else 0.0
        metadata = {
            "source": eid,
            "battery": attrs.get("battery_level"),
            "gps_accuracy": attrs.get("gps_accuracy"),
        }

        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)
                VALUES (now(), $1, 'home', $2, 'gps_tracker', $3)
            """, person, confidence, json.dumps(metadata))

        event = "arrived home" if is_home else "left home"
        log(f"Device tracker: {person} {event}")
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO shared_observations (observer, category, subject, observation, severity)
                VALUES ('ha_poller', 'presence', 'home', $1, 'info')
            """, f"GPS: {person} {event}")


async def poll_loop():
    await asyncio.sleep(5)
    log(f"HA poller started (interval={POLL_INTERVAL}s)")

    while not _shutdown:
        try:
            states = ha_get_states()
            if not states:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            room_lit = analyze_lights(states)
            room_media = analyze_media(states)
            climate, motion = analyze_sensors(states)

            await write_climate(climate)
            await write_presence_from_lights(room_lit)
            await write_presence_from_media(room_media)
            await write_motion(motion)
            await write_device_tracker(states)

        except Exception as e:
            log(f"Poll error: {e}", "ERROR")

        await asyncio.sleep(POLL_INTERVAL)


async def health_reporter():
    while not _shutdown:
        await asyncio.sleep(300)
        lit_rooms = [r for r, v in _prev_light_state.items() if v]
        active_media = [r for r, v in _prev_media_state.items() if v.get("active")]
        log(f"Health: up {int(time.time() - _start_time)}s, "
            f"lit rooms: {lit_rooms}, active media: {active_media}")


def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


async def main():
    global _shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova HA Poller v{VERSION} starting...")
    log(f"Polling {HA_URL} every {POLL_INTERVAL}s")
    log(f"Tracking: {len(LIGHT_ROOMS)} rooms via lights, {len(MEDIA_ROOMS)} media players")

    token = get_ha_token()
    if token:
        log("HA authentication successful")
    else:
        log("HA authentication failed — will retry", "WARN")

    tasks = [
        asyncio.create_task(poll_loop()),
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
