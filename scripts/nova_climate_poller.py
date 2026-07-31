#!/opt/homebrew/bin/python3
"""
nova_climate_poller.py — Aggregate per-room climate data from multiple sources.

Sources:
  1. Hue Bridge (outdoor motion sensor: temperature, light level)
  2. Weather station (telemetry.weather table in nova_ops)
  3. HomePod room sensors via Shortcuts CLI proxy (port 37432)

Polls every 2 minutes, inserts into telemetry.climate with source attribution.
Gracefully skips unavailable sources.

Written by Jordan Koch.
"""

import os
import sys
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))

import datetime
import json
import logging
import os
import re
import signal
import time
import urllib.request
import urllib.error
from typing import Optional

import psycopg2
import psycopg2.extras

import nova_config

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_PATH = os.path.expanduser("~/.openclaw/logs/climate_poller.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("climate_poller")

# ── Constants ────────────────────────────────────────────────────────────────

POLL_INTERVAL = 120  # seconds (2 minutes)
DB_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

HUE_BRIDGE_SUBNET = "192.168.1"
HUE_BRIDGE_RANGE = range(20, 51)  # .20 through .50
SHORTCUTS_PROXY = "http://127.0.0.1:37432"

# ── Helpers ──────────────────────────────────────────────────────────────────

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("Received signal %d, shutting down gracefully.", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _http_get(url: str, timeout: int = 10) -> Optional[dict]:
    """Simple HTTP GET returning parsed JSON or None on failure."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.debug("HTTP GET %s failed: %s", url, e)
        return None


def _normalize_room(device_name: str) -> str:
    """
    Derive a room slug from a device name.
    'Living Room HomePod' -> 'living_room'
    'Office Hue Motion' -> 'office'
    """
    name = device_name.lower().strip()
    # Strip common suffixes
    for suffix in ("homepod", "homepod mini", "hue motion sensor", "hue motion",
                   "motion sensor", "sensor", "mini"):
        name = re.sub(rf"\s*{re.escape(suffix)}\s*$", "", name)
    name = name.strip()
    # Normalize whitespace to underscores
    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = name.strip("_")
    return name or "unknown"


def _get_hue_username() -> str:
    """Retrieve Hue Bridge username from macOS Keychain."""
    return nova_config._keychain("nova-hue-api-key", required=False)


def _discover_hue_bridge() -> Optional[str]:
    """
    Discover the Hue Bridge IP.
    Try mDNS first (Philips hue bridge advertises _hue._tcp),
    then scan common IPs in the .20-.50 range.
    """
    # Known bridge first — same pinned IP nova_hue_history.py uses successfully.
    # (Cloud discovery is flaky and the .20-.50 scan below misses .152 entirely.)
    known = "192.168.1.152"
    try:
        req = urllib.request.Request(f"http://{known}/api/config")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if json.loads(resp.read().decode()).get("bridgeid"):
                return known
    except Exception:
        pass

    # Try mDNS discovery endpoint
    meethue = _http_get("https://discovery.meethue.com", timeout=5)
    if meethue and isinstance(meethue, list) and len(meethue) > 0:
        ip = meethue[0].get("internalipaddress")
        if ip:
            log.info("Discovered Hue Bridge via meethue: %s", ip)
            return ip

    # Scan common range
    for octet in HUE_BRIDGE_RANGE:
        ip = f"{HUE_BRIDGE_SUBNET}.{octet}"
        try:
            req = urllib.request.Request(f"http://{ip}/api/config")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                if data.get("bridgeid") or data.get("modelid", "").startswith("BSB"):
                    log.info("Found Hue Bridge at %s", ip)
                    return ip
        except Exception:
            continue

    return None


# ── Source: Hue Bridge ───────────────────────────────────────────────────────

def poll_hue(bridge_ip: str, username: str) -> list[dict]:
    """
    Poll Hue Bridge sensors for temperature and light level readings.
    Returns list of climate reading dicts.
    """
    readings = []
    url = f"http://{bridge_ip}/api/{username}/sensors"
    data = _http_get(url, timeout=10)
    if not data:
        log.warning("Hue Bridge at %s returned no sensor data.", bridge_ip)
        return readings

    for sensor_id, sensor in data.items():
        if not isinstance(sensor, dict):
            continue

        sensor_type = sensor.get("type", "")
        name = sensor.get("name", "")
        state = sensor.get("state", {})

        if sensor_type == "ZLLTemperature" and "temperature" in state:
            # Hue reports temp in hundredths of a degree C (e.g. 2143 = 21.43C)
            temp_c = state["temperature"] / 100.0
            room = _normalize_room(name)
            readings.append({
                "room": room,
                "metric": "temperature_c",
                "value": round(temp_c, 2),
                "source": "hue_bridge",
                "device_name": name,
            })

        elif sensor_type == "ZLLLightLevel" and "lightlevel" in state:
            # Hue light level in 10000 * log10(lux) + 1
            light_level = state["lightlevel"]
            lux = round(10 ** ((light_level - 1) / 10000.0), 1)
            room = _normalize_room(name)
            readings.append({
                "room": room,
                "metric": "light_lux",
                "value": lux,
                "source": "hue_bridge",
                "device_name": name,
            })

        elif sensor_type == "ZLLPresence" and "presence" in state:
            room = _normalize_room(name)
            readings.append({
                "room": room,
                "metric": "presence",
                "value": 1.0 if state["presence"] else 0.0,
                "source": "hue_bridge",
                "device_name": name,
            })

    return readings


# ── Source: Weather Station (PG) ─────────────────────────────────────────────

def poll_weather_station(conn) -> list[dict]:
    """
    Read latest weather station data from telemetry.weather for outdoor reference.
    """
    readings = []
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT temp_f, humidity, pressure_in, wind_speed_mph, ts
                FROM telemetry.weather
                ORDER BY ts DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                ts = row["ts"]
                # Only use if less than 10 minutes old
                age = (datetime.datetime.now(datetime.timezone.utc) - ts.replace(
                    tzinfo=datetime.timezone.utc if ts.tzinfo is None else ts.tzinfo
                )).total_seconds()
                if age < 600:
                    if row["temp_f"] is not None:
                        readings.append({
                            "room": "outdoor",
                            "metric": "temp_f",
                            "value": float(row["temp_f"]),
                            "source": "weather_station",
                            "device_name": "weather_station",
                        })
                    if row["humidity"] is not None:
                        readings.append({
                            "room": "outdoor",
                            "metric": "humidity",
                            "value": float(row["humidity"]),
                            "source": "weather_station",
                            "device_name": "weather_station",
                        })
                else:
                    log.debug("Weather station data too old (%.0fs), skipping.", age)
    except Exception as e:
        log.warning("Failed to read weather station: %s", e)
        conn.rollback()

    return readings


# ── Source: HomePod Sensors via Shortcuts Proxy ──────────────────────────────

def poll_homepod_sensors() -> list[dict]:
    """
    Query Shortcuts CLI proxy at port 37432 for HomeKit-exposed climate sensors
    on HomePod devices (temperature, humidity).
    """
    readings = []

    # Get device list
    devices_data = _http_get(f"{SHORTCUTS_PROXY}/devices", timeout=10)
    if not devices_data:
        log.warning("Shortcuts proxy at %s unavailable.", SHORTCUTS_PROXY)
        return readings

    devices = devices_data if isinstance(devices_data, list) else devices_data.get("devices", [])

    for device in devices:
        if not isinstance(device, dict):
            continue

        name = device.get("name", "")
        services = device.get("services", [])
        characteristics = device.get("characteristics", services if isinstance(services, list) else [])

        # Look for temperature/humidity in device characteristics
        temp_value = None
        humidity_value = None

        for char in (characteristics if isinstance(characteristics, list) else []):
            if not isinstance(char, dict):
                continue
            char_type = char.get("type", "").lower()
            value = char.get("value")

            if value is None:
                continue

            if "temperature" in char_type or char_type == "current temperature":
                try:
                    temp_value = float(value)
                except (ValueError, TypeError):
                    pass
            elif "humidity" in char_type or char_type == "current relative humidity":
                try:
                    humidity_value = float(value)
                except (ValueError, TypeError):
                    pass

        room = _normalize_room(name)

        if temp_value is not None:
            readings.append({
                "room": room,
                "metric": "temperature_c",
                "value": round(temp_value, 2),
                "source": "homekit_homepod",
                "device_name": name,
            })

        if humidity_value is not None:
            readings.append({
                "room": room,
                "metric": "humidity_pct",
                "value": round(humidity_value, 1),
                "source": "homekit_homepod",
                "device_name": name,
            })

    return readings


# ── Database: Insert Readings ────────────────────────────────────────────────

def ensure_schema(conn):
    """Schema already created by telemetry_schema.sql — verify connectivity."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM telemetry.climate LIMIT 0")
    conn.commit()


def insert_readings(conn, readings: list[dict]):
    """Batch insert climate readings into telemetry.climate.

    Readings come in as {room, metric, value, source, device_name}.
    We aggregate per room+source and insert one row with temp_f/humidity/light_lux/motion.
    """
    if not readings:
        return

    grouped = {}
    for r in readings:
        key = (r["room"], r["source"])
        if key not in grouped:
            grouped[key] = {"temp_f": None, "humidity": None, "light_lux": None, "motion": None}
        metric = r["metric"]
        if metric in ("temperature", "temp_f"):
            grouped[key]["temp_f"] = r["value"]
        elif metric in ("humidity",):
            grouped[key]["humidity"] = int(r["value"]) if r["value"] is not None else None
        elif metric in ("light", "light_lux", "lux"):
            grouped[key]["light_lux"] = r["value"]
        elif metric in ("motion", "presence"):
            grouped[key]["motion"] = bool(r["value"])

    with conn.cursor() as cur:
        for (room, source), vals in grouped.items():
            cur.execute("""
                INSERT INTO telemetry.climate (ts, room, source, temp_f, humidity, light_lux, motion)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s)
            """, (room, source, vals["temp_f"], vals["humidity"], vals["light_lux"], vals["motion"]))
    conn.commit()
    log.info("Inserted %d climate readings (%d rooms).", len(readings), len(grouped))


# ── Main Loop ────────────────────────────────────────────────────────────────

def main():
    log.info("nova_climate_poller starting up.")

    # Get Hue credentials
    hue_username = _get_hue_username()
    if not hue_username:
        log.warning("No Hue API key in Keychain (nova-hue-api-key). Hue source disabled.")

    # Discover Hue Bridge (cache the IP)
    hue_bridge_ip = None
    if hue_username:
        hue_bridge_ip = _discover_hue_bridge()
        if not hue_bridge_ip:
            log.warning("Could not discover Hue Bridge. Will retry each poll cycle.")

    # Connect to PostgreSQL
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = False
        ensure_schema(conn)
        log.info("Connected to PostgreSQL, schema verified.")
    except Exception as e:
        log.error("Failed to connect to PostgreSQL: %s", e)
        sys.exit(1)

    # Main polling loop
    while not _shutdown:
        cycle_start = time.time()
        all_readings = []

        # 1. Hue Bridge sensors
        if hue_username:
            if not hue_bridge_ip:
                hue_bridge_ip = _discover_hue_bridge()
            if hue_bridge_ip:
                try:
                    hue_readings = poll_hue(hue_bridge_ip, hue_username)
                    all_readings.extend(hue_readings)
                    log.info("Hue: %d readings collected.", len(hue_readings))
                except Exception as e:
                    log.warning("Hue polling error: %s", e)
                    # Bridge may have changed IP
                    hue_bridge_ip = None

        # 2. Weather station (PG)
        try:
            weather_readings = poll_weather_station(conn)
            all_readings.extend(weather_readings)
            if weather_readings:
                log.info("Weather station: %d readings collected.", len(weather_readings))
        except Exception as e:
            log.warning("Weather station polling error: %s", e)
            # Reconnect if needed
            try:
                conn.rollback()
            except Exception:
                pass

        # 3. HomePod sensors via Shortcuts proxy
        try:
            homepod_readings = poll_homepod_sensors()
            all_readings.extend(homepod_readings)
            if homepod_readings:
                log.info("HomePod sensors: %d readings collected.", len(homepod_readings))
        except Exception as e:
            log.warning("HomePod sensor polling error: %s", e)

        # Insert all readings
        if all_readings:
            try:
                insert_readings(conn, all_readings)
            except Exception as e:
                log.error("Failed to insert readings: %s", e)
                try:
                    conn.rollback()
                except Exception:
                    pass
                # Try to reconnect
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    conn = psycopg2.connect(DB_DSN)
                    conn.autocommit = False
                    log.info("Reconnected to PostgreSQL.")
                except Exception as e2:
                    log.error("Reconnection failed: %s. Exiting.", e2)
                    sys.exit(1)
        else:
            log.info("No readings collected this cycle.")

        # Sleep until next poll
        elapsed = time.time() - cycle_start
        sleep_time = max(0, POLL_INTERVAL - elapsed)
        if sleep_time > 0 and not _shutdown:
            time.sleep(sleep_time)

    # Cleanup
    log.info("Shutting down. Closing DB connection.")
    try:
        conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
