#!/opt/homebrew/bin/python3
"""
nova_ha_metrics.py — Collect ALL graphable Home Assistant sensor states into PG.

Pulls every entity from the HA REST API each cycle and writes graphable
readings (battery %, illuminance/lux, temperature, humidity, air quality,
power/energy, light brightness/state, switch state, contact/motion, locks,
sound, etc.) to telemetry.ha_sensors in LONG format:

    ts, entity_id, friendly_name, domain, device_class,
    state_numeric, state_text, unit, area

This complements nova_ha_poller.py (which does presence/climate inference).
This collector is purely for time-series graphing of raw sensor states.

Resilient: any per-entity or per-cycle failure is caught and logged; the
loop keeps running. Area names are resolved once via the HA template API
and refreshed hourly.

Scheduler: run on a 60s interval (see report).

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
    import asyncpg
except ImportError as e:
    print(f"FATAL: missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

VERSION = "1.0.0"
DB_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
HA_URL = "http://127.0.0.1:8123"
POLL_INTERVAL = 60          # seconds between collections
AREA_REFRESH = 3600         # seconds between area-map refreshes
LOG_FILE = Path.home() / ".openclaw/logs/nova_ha_metrics.log"

# device_classes we always want to graph (numeric or categorical-as-event)
GRAPHABLE_CLASSES = {
    "battery", "temperature", "humidity", "illuminance",
    "pm25", "pm10", "pm1", "carbon_dioxide", "carbon_monoxide",
    "volatile_organic_compounds", "volatile_organic_compounds_parts",
    "nitrogen_dioxide", "ozone", "aqi",
    "power", "energy", "voltage", "current", "apparent_power",
    "power_factor", "frequency", "gas", "water",
    "pressure", "atmospheric_pressure", "signal_strength",
    "sound_pressure", "moisture",
    # binary_sensor / categorical (stored as text + numeric flag)
    "motion", "occupancy", "door", "window", "opening",
    "garage_door", "lock", "smoke", "gas", "safety", "sound",
    "presence", "vibration", "tamper", "problem", "connectivity",
    "running", "cold", "heat", "light", "moving",
}

# domains we always collect (state itself is graphable)
GRAPHABLE_DOMAINS = {"light", "switch", "lock", "cover", "fan", "binary_sensor"}

# binary / on-off style states mapped to a numeric flag for graphing
BINARY_ON = {"on", "open", "opened", "detected", "motion", "home",
             "unlocked", "playing", "active", "wet", "true"}
BINARY_OFF = {"off", "closed", "clear", "not_detected", "away",
              "locked", "idle", "paused", "standby", "dry", "false",
              "no_motion"}

_shutdown = False
_pool = None
_start_time = time.time()
_access_token = None
_token_expires = 0
_area_map = {}              # entity_id -> area name
_area_refreshed = 0

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[ha_metrics {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _keychain(service):
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova", "-s", service, "-w"],
            capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def get_ha_token():
    global _access_token, _token_expires
    if _access_token and time.time() < _token_expires:
        return _access_token
    refresh_token = _keychain("nova-hass-refresh-token")
    if not refresh_token:
        log("No HA refresh token in Keychain", "ERROR")
        return None
    import urllib.parse
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
        req = urllib.request.Request(
            f"{HA_URL}/api/states",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except Exception as e:
        log(f"HA API error: {e}", "ERROR")
        return None


def refresh_area_map(states):
    """Resolve entity_id -> area via the HA template API (one batched call)."""
    global _area_map, _area_refreshed
    token = get_ha_token()
    if not token or not states:
        return
    try:
        eids = [s["entity_id"] for s in states]
        # Build a template that emits "entity_id\tarea" lines.
        tmpl = (
            "{% for e in eids %}{{ e }}\t{{ area_name(e) or '' }}\n{% endfor %}"
        )
        payload = json.dumps({"template": tmpl, "variables": {"eids": eids}}).encode()
        req = urllib.request.Request(
            f"{HA_URL}/api/template",
            data=payload,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        text = resp.read().decode()
        new_map = {}
        for line in text.splitlines():
            if "\t" in line:
                eid, area = line.split("\t", 1)
                area = area.strip()
                if area and area.lower() != "none":
                    new_map[eid.strip()] = area
        if new_map:
            _area_map = new_map
        _area_refreshed = time.time()
        log(f"Area map refreshed: {len(_area_map)} entities mapped to areas")
    except Exception as e:
        log(f"Area map refresh failed (continuing without areas): {e}", "WARN")
        _area_refreshed = time.time()  # don't hammer on failure


def to_numeric(state):
    try:
        return float(state)
    except (ValueError, TypeError):
        return None


def binary_flag(state):
    s = str(state).strip().lower()
    if s in BINARY_ON:
        return 1.0
    if s in BINARY_OFF:
        return 0.0
    return None


def extract_rows(states):
    """Return list of row tuples for graphable entities."""
    rows = []
    skip_states = {"unavailable", "unknown", "none", ""}
    for s in states:
        try:
            eid = s["entity_id"]
            domain = eid.split(".")[0]
            attrs = s.get("attributes", {}) or {}
            dc = attrs.get("device_class")
            unit = attrs.get("unit_of_measurement")
            state = s.get("state")
            friendly = attrs.get("friendly_name") or eid
            area = _area_map.get(eid)

            graphable = (dc in GRAPHABLE_CLASSES) or (domain in GRAPHABLE_DOMAINS)
            # Also capture any numeric sensor with a unit (power %, lux, etc.)
            num = to_numeric(state)
            if not graphable and not (domain == "sensor" and num is not None and unit):
                continue

            state_str = str(state) if state is not None else None
            if state_str is not None and state_str.strip().lower() in skip_states:
                continue

            state_numeric = num
            state_text = None
            if state_numeric is None:
                # categorical / binary -> derive a flag, keep text
                flag = binary_flag(state)
                state_numeric = flag
                state_text = state_str
            else:
                # numeric; for lights also fold in brightness if present
                if domain == "light":
                    bri = attrs.get("brightness")
                    if bri is not None:
                        state_numeric = float(bri)
                        unit = unit or "brightness"

            # lights/switches/locks/covers: state is on/off text -> flag
            if domain in ("light", "switch", "lock", "cover", "fan") and num is None:
                state_text = state_str
                if state_numeric is None:
                    state_numeric = binary_flag(state)
                if domain == "light":
                    bri = attrs.get("brightness")
                    if bri is not None:
                        state_numeric = float(bri)
                        unit = "brightness"

            rows.append((
                eid, friendly, domain, dc,
                state_numeric, state_text, unit, area,
            ))
        except Exception as e:
            log(f"row extract error for {s.get('entity_id','?')}: {e}", "WARN")
            continue
    return rows


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    return _pool


async def write_rows(rows):
    if not rows:
        return 0
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    records = [(now, *r) for r in rows]
    async with pool.acquire() as conn:
        await conn.executemany("""
            INSERT INTO telemetry.ha_sensors
                (ts, entity_id, friendly_name, domain, device_class,
                 state_numeric, state_text, unit, area)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, records)
    return len(records)


async def collect_once():
    states = ha_get_states()
    if not states:
        log("No states returned", "WARN")
        return 0
    if time.time() - _area_refreshed > AREA_REFRESH:
        refresh_area_map(states)
    rows = extract_rows(states)
    written = await write_rows(rows)
    return written


async def poll_loop():
    log(f"HA metrics collector started (interval={POLL_INTERVAL}s)")
    while not _shutdown:
        try:
            n = await collect_once()
            log(f"Wrote {n} sensor rows to telemetry.ha_sensors")
        except Exception as e:
            log(f"Collection cycle error: {e}", "ERROR")
        for _ in range(POLL_INTERVAL):
            if _shutdown:
                break
            await asyncio.sleep(1)


def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


async def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    log(f"Nova HA Metrics Collector v{VERSION} starting...")
    log(f"Polling {HA_URL} every {POLL_INTERVAL}s -> telemetry.ha_sensors")
    if get_ha_token():
        log("HA authentication successful")
    else:
        log("HA authentication failed — will retry", "WARN")
    await poll_loop()
    if _pool:
        await _pool.close()
    log("Shutdown complete")


if __name__ == "__main__":
    # `--once` for a single test collection
    if "--once" in sys.argv:
        async def _run_once():
            n = await collect_once()
            log(f"[--once] wrote {n} rows")
            if _pool:
                await _pool.close()
        asyncio.run(_run_once())
    else:
        asyncio.run(main())
