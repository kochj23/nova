#!/opt/homebrew/bin/python3
"""
nova_energy_poller.py — Poll Eve Energy devices via HomeKit for power telemetry.

Reads real-time power consumption (watts), voltage, amperage, cumulative kWh,
and relay state from Eve Energy smart plugs. Inserts into telemetry.energy
every 60 seconds.

Integration stack (tried in order):
  1. Shortcuts CLI proxy at http://127.0.0.1:37432/ (if running)
  2. aiohomekit direct HAP-over-IP connection (requires pairing data)
  3. Homebridge API (if Eve plugin installed)

Eve Energy exposes these custom HAP characteristics:
  - E863F10D-079E-48FF-8F27-9C2605A29F52  (Watts, real-time power)
  - E863F10A-079E-48FF-8F27-9C2605A29F52  (Voltage)
  - E863F126-079E-48FF-8F27-9C2605A29F52  (Amperes)
  - E863F10C-079E-48FF-8F27-9C2605A29F52  (Total kWh)
  Plus standard HAP: On (bool) characteristic for relay state.

Discovery:
  First run discovers all Eve Energy accessories, stores IDs in state file.
  Subsequent runs poll known devices directly.

Written by Jordan Koch.
"""

import sys
sys.path.insert(0, str(Path.home()) + "/.openclaw/scripts")

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

import nova_config

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_PATH = str(Path.home()) + "/.openclaw/logs/energy_poller.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("energy_poller")

# ── Constants ────────────────────────────────────────────────────────────────

POLL_INTERVAL = 60  # seconds
DB_DSN = "host=localhost dbname=nova_ops user=kochj"
STATE_DIR = Path.home() / ".openclaw/workspace/state"
STATE_FILE = STATE_DIR / "eve_devices.json"
PAIRING_FILE = STATE_DIR / "homekit_pairings.json"

SHORTCUTS_PROXY = "http://127.0.0.1:37432"
HOMEBRIDGE_API = "http://127.0.0.1:51826"

# Eve Energy HAP characteristic UUIDs (custom Elgato/Eve vendor UUIDs)
EVE_CHAR_WATT = "E863F10D-079E-48FF-8F27-9C2605A29F52"
EVE_CHAR_VOLT = "E863F10A-079E-48FF-8F27-9C2605A29F52"
EVE_CHAR_AMP = "E863F126-079E-48FF-8F27-9C2605A29F52"
EVE_CHAR_KWH = "E863F10C-079E-48FF-8F27-9C2605A29F52"
# Standard HAP On/Off (characteristic type 25)
HAP_CHAR_ON = "00000025-0000-1000-8000-0026BB765291"

EVE_ENERGY_SERVICE_UUID = "E863F007-079E-48FF-8F27-9C2605A29F52"

# ── Signal Handling ──────────────────────────────────────────────────────────

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("Received signal %d, shutting down gracefully.", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ── State Management ─────────────────────────────────────────────────────────


def load_known_devices() -> list[dict]:
    """Load known Eve Energy devices from state file."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, list) and data:
                return data
        except (json.JSONDecodeError, IOError) as e:
            log.warning("Failed to load state file: %s", e)
    return []


def save_known_devices(devices: list[dict]) -> None:
    """Persist discovered Eve Energy devices to state file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(devices, indent=2))
    log.info("Saved %d Eve Energy device(s) to %s", len(devices), STATE_FILE)


# ── HTTP Helpers ─────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 10) -> Optional[dict]:
    """Simple HTTP GET returning parsed JSON or None on failure."""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.debug("HTTP GET %s failed: %s", url, e)
        return None


# ── Source 1: Shortcuts CLI Proxy ────────────────────────────────────────────

def poll_shortcuts_proxy() -> list[dict]:
    """
    Query the Shortcuts CLI proxy at port 37432 for Eve Energy device data.
    The proxy exposes /devices which returns all HomeKit accessories with
    their characteristics.

    Returns list of energy reading dicts or empty list if proxy unavailable.
    """
    readings = []

    devices_data = _http_get(f"{SHORTCUTS_PROXY}/devices", timeout=10)
    if not devices_data:
        log.debug("Shortcuts proxy at %s unavailable.", SHORTCUTS_PROXY)
        return readings

    devices = (devices_data if isinstance(devices_data, list)
               else devices_data.get("devices", []))

    discovered = []

    for device in devices:
        if not isinstance(device, dict):
            continue

        name = device.get("name", "")
        device_id = device.get("id", device.get("aid", ""))
        services = device.get("services", [])
        characteristics = device.get("characteristics",
                                     services if isinstance(services, list) else [])

        # Identify Eve Energy by checking for Eve energy characteristics
        reading = _extract_eve_energy_reading(name, str(device_id), characteristics)
        if reading:
            readings.append(reading)
            discovered.append({
                "device_id": str(device_id),
                "name": name,
                "source": "shortcuts_proxy",
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            })

    # Update known devices if we discovered new ones
    if discovered:
        _merge_discovered_devices(discovered)

    return readings


def _extract_eve_energy_reading(name: str, device_id: str,
                                characteristics: list) -> Optional[dict]:
    """
    Extract Eve Energy power data from a list of characteristics.
    Returns a reading dict or None if this is not an Eve Energy device.
    """
    watts = None
    volts = None
    amps = None
    kwh_total = None
    on_state = None

    for char in (characteristics if isinstance(characteristics, list) else []):
        if not isinstance(char, dict):
            continue

        char_type = char.get("type", "").upper()
        value = char.get("value")

        if value is None:
            continue

        if EVE_CHAR_WATT.upper() in char_type or char_type == EVE_CHAR_WATT:
            try:
                watts = float(value)
            except (ValueError, TypeError):
                pass
        elif EVE_CHAR_VOLT.upper() in char_type or char_type == EVE_CHAR_VOLT:
            try:
                volts = float(value)
            except (ValueError, TypeError):
                pass
        elif EVE_CHAR_AMP.upper() in char_type or char_type == EVE_CHAR_AMP:
            try:
                amps = float(value)
            except (ValueError, TypeError):
                pass
        elif EVE_CHAR_KWH.upper() in char_type or char_type == EVE_CHAR_KWH:
            try:
                kwh_total = float(value)
            except (ValueError, TypeError):
                pass
        elif HAP_CHAR_ON.upper() in char_type or "on" == char.get("description", "").lower():
            try:
                on_state = bool(value)
            except (ValueError, TypeError):
                pass

    # Must have at least watts to be considered an Eve Energy reading
    if watts is not None:
        return {
            "device_id": device_id,
            "device_name": name,
            "watts": watts,
            "volts": volts,
            "amps": amps,
            "kwh_total": kwh_total,
            "on_state": on_state,
        }

    return None


# ── Source 2: aiohomekit Direct HAP ──────────────────────────────────────────

async def poll_aiohomekit() -> list[dict]:
    """
    Connect directly to Eve Energy devices via HAP-over-IP using aiohomekit.

    Requirements:
      - Devices must be paired (pairing data stored in homekit_pairings.json)
      - The pairing must have been established previously (see discover_and_pair())

    Returns list of energy reading dicts.
    """
    readings = []

    try:
        from aiohomekit import Controller
        from aiohomekit.model.characteristics import CharacteristicsTypes
    except ImportError:
        log.warning("aiohomekit not available, skipping HAP direct polling.")
        return readings

    # Load pairing data
    if not PAIRING_FILE.exists():
        log.info("No pairing file at %s. Run discovery first.", PAIRING_FILE)
        return readings

    try:
        pairing_data = json.loads(PAIRING_FILE.read_text())
    except (json.JSONDecodeError, IOError) as e:
        log.error("Failed to load pairing data: %s", e)
        return readings

    if not pairing_data:
        log.info("Pairing file is empty. Run discovery first.")
        return readings

    controller = Controller()

    try:
        # Load each pairing and read characteristics
        for alias, pdata in pairing_data.items():
            try:
                pairing = controller.load_pairing(alias, pdata)

                # Get accessories list to find Eve Energy services
                accessories = await pairing.list_accessories_and_characteristics()

                for accessory in accessories:
                    aid = accessory.get("aid", 0)
                    services = accessory.get("services", [])

                    for service in services:
                        # Check if this is an Eve Energy power service
                        s_type = service.get("type", "").upper()
                        if (EVE_ENERGY_SERVICE_UUID.upper() not in s_type and
                                "OUTLET" not in s_type.upper()):
                            continue

                        chars = service.get("characteristics", [])
                        reading = _parse_hap_characteristics(
                            alias, aid, chars, accessories
                        )
                        if reading:
                            readings.append(reading)

            except Exception as e:
                log.warning("Failed to read from pairing '%s': %s", alias, e)
                continue

    finally:
        # Clean up controller
        try:
            await controller.async_stop()
        except Exception:
            pass

    return readings


def _parse_hap_characteristics(alias: str, aid: int, chars: list,
                               accessories: list) -> Optional[dict]:
    """Parse HAP characteristics into an energy reading dict."""
    watts = None
    volts = None
    amps = None
    kwh_total = None
    on_state = None

    for char in chars:
        c_type = char.get("type", "").upper()
        value = char.get("value")

        if value is None:
            continue

        if EVE_CHAR_WATT.upper() in c_type:
            watts = float(value)
        elif EVE_CHAR_VOLT.upper() in c_type:
            volts = float(value)
        elif EVE_CHAR_AMP.upper() in c_type:
            amps = float(value)
        elif EVE_CHAR_KWH.upper() in c_type:
            kwh_total = float(value)
        elif HAP_CHAR_ON.upper() in c_type:
            on_state = bool(value)

    if watts is not None:
        # Try to get friendly name from the accessory info
        device_name = alias
        for acc in accessories:
            if acc.get("aid") == aid:
                for svc in acc.get("services", []):
                    for c in svc.get("characteristics", []):
                        if "NAME" in c.get("type", "").upper():
                            device_name = c.get("value", alias)
                            break

        return {
            "device_id": f"hap_{aid}",
            "device_name": device_name,
            "watts": watts,
            "volts": volts,
            "amps": amps,
            "kwh_total": kwh_total,
            "on_state": on_state,
        }

    return None


async def discover_eve_devices() -> list[dict]:
    """
    Discover Eve Energy devices on the local network via HAP mDNS.

    NOTE: This discovers unpaired devices. To actually read from them,
    you must pair first. Pairing requires physical confirmation on the
    device (or the 8-digit setup code from the device label).

    Returns list of discovered device dicts (not yet paired).
    """
    discovered = []

    try:
        from aiohomekit import Controller
    except ImportError:
        log.warning("aiohomekit not available for discovery.")
        return discovered

    controller = Controller()

    try:
        log.info("Starting HAP discovery (10s scan)...")
        devices = await controller.async_discover(timeout=10)

        for device in devices:
            info = device.info if hasattr(device, 'info') else {}
            name = getattr(device, 'name', '') or info.get('name', '')
            device_id = getattr(device, 'id', '') or info.get('id', '')

            # Eve Energy devices typically contain "Eve Energy" in the name
            # or have the Elgato/Eve manufacturer
            if ("eve" in name.lower() and "energy" in name.lower()) or \
               "elgato" in str(info).lower():
                discovered.append({
                    "device_id": str(device_id),
                    "name": name,
                    "source": "aiohomekit_discovery",
                    "paired": False,
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                })
                log.info("Discovered Eve Energy: %s (id=%s)", name, device_id)

    except Exception as e:
        log.error("HAP discovery failed: %s", e)
    finally:
        try:
            await controller.async_stop()
        except Exception:
            pass

    return discovered


# ── Source 3: Homebridge API ─────────────────────────────────────────────────

def poll_homebridge() -> list[dict]:
    """
    Query Homebridge HTTP API for Eve Energy accessories.
    This works if homebridge-eve-energy or homebridge-mqtt-thing is installed.

    The Homebridge API typically exposes accessories at /accessories.
    """
    readings = []

    # Try Homebridge UI API (homebridge-config-ui-x)
    # Default port is 8581 for the UI, accessories at /api/accessories
    for port in [8581, 51826]:
        url = f"http://127.0.0.1:{port}/api/accessories"
        data = _http_get(url, timeout=5)
        if data:
            log.debug("Got Homebridge accessories from port %d", port)
            if isinstance(data, list):
                for acc in data:
                    reading = _parse_homebridge_accessory(acc)
                    if reading:
                        readings.append(reading)
            break

    return readings


def _parse_homebridge_accessory(acc: dict) -> Optional[dict]:
    """Parse a Homebridge accessory dict for Eve Energy data."""
    name = acc.get("serviceName", acc.get("name", ""))
    aid = acc.get("aid", acc.get("uniqueId", ""))

    # Look for power-related characteristics
    chars = acc.get("serviceCharacteristics", acc.get("characteristics", []))
    if not isinstance(chars, list):
        return None

    watts = None
    volts = None
    amps = None
    kwh_total = None
    on_state = None

    for char in chars:
        c_type = char.get("type", "").upper()
        desc = char.get("description", "").lower()
        value = char.get("value")

        if value is None:
            continue

        if EVE_CHAR_WATT.upper() in c_type or "watt" in desc or "power" in desc:
            watts = float(value)
        elif EVE_CHAR_VOLT.upper() in c_type or "volt" in desc:
            volts = float(value)
        elif EVE_CHAR_AMP.upper() in c_type or "amp" in desc or "current" == desc:
            amps = float(value)
        elif EVE_CHAR_KWH.upper() in c_type or "kwh" in desc or "energy" in desc:
            kwh_total = float(value)
        elif HAP_CHAR_ON.upper() in c_type or desc == "on":
            on_state = bool(value)

    if watts is not None:
        return {
            "device_id": f"hb_{aid}",
            "device_name": name,
            "watts": watts,
            "volts": volts,
            "amps": amps,
            "kwh_total": kwh_total,
            "on_state": on_state,
        }

    return None


# ── Device Merge ─────────────────────────────────────────────────────────────

def _merge_discovered_devices(new_devices: list[dict]) -> None:
    """Merge newly discovered devices into the known devices state file."""
    existing = load_known_devices()
    existing_ids = {d["device_id"] for d in existing}

    added = 0
    for dev in new_devices:
        if dev["device_id"] not in existing_ids:
            existing.append(dev)
            existing_ids.add(dev["device_id"])
            added += 1

    if added > 0:
        save_known_devices(existing)
        log.info("Added %d new Eve Energy device(s) to state.", added)


# ── Database ─────────────────────────────────────────────────────────────────

def get_db_connection():
    """Get a psycopg2 connection to nova_ops."""
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
        return conn
    except Exception as e:
        log.error("Database connection failed: %s", e)
        return None


def insert_readings(conn, readings: list[dict]) -> int:
    """
    Insert energy readings into telemetry.energy.
    Returns count of rows inserted.
    """
    if not readings:
        return 0

    now = datetime.now(timezone.utc)
    inserted = 0

    try:
        with conn.cursor() as cur:
            for r in readings:
                cur.execute("""
                    INSERT INTO telemetry.energy
                        (ts, device_id, device_name, watts, volts, amps, kwh_total, on_state)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    now,
                    r["device_id"],
                    r.get("device_name"),
                    r.get("watts"),
                    r.get("volts"),
                    r.get("amps"),
                    r.get("kwh_total"),
                    r.get("on_state"),
                ))
                inserted += 1

    except Exception as e:
        log.error("Database insert failed: %s", e)
        # Reconnect on next cycle
        try:
            conn.rollback()
        except Exception:
            pass

    return inserted


# ── Main Poll Loop ───────────────────────────────────────────────────────────

def poll_all_sources() -> list[dict]:
    """
    Try all integration sources in order of preference.
    Returns as soon as one source provides data.
    """
    # Source 1: Shortcuts CLI proxy
    readings = poll_shortcuts_proxy()
    if readings:
        log.info("Got %d reading(s) from Shortcuts proxy.", len(readings))
        return readings

    # Source 2: aiohomekit direct HAP
    try:
        loop = asyncio.new_event_loop()
        readings = loop.run_until_complete(poll_aiohomekit())
        loop.close()
        if readings:
            log.info("Got %d reading(s) from aiohomekit.", len(readings))
            return readings
    except Exception as e:
        log.debug("aiohomekit poll failed: %s", e)

    # Source 3: Homebridge API
    readings = poll_homebridge()
    if readings:
        log.info("Got %d reading(s) from Homebridge.", len(readings))
        return readings

    return []


def run_discovery() -> list[dict]:
    """Run device discovery across all sources."""
    log.info("Running Eve Energy device discovery...")
    discovered = []

    # Try Shortcuts proxy for device enumeration
    proxy_data = _http_get(f"{SHORTCUTS_PROXY}/devices", timeout=10)
    if proxy_data:
        devices = (proxy_data if isinstance(proxy_data, list)
                   else proxy_data.get("devices", []))
        for dev in devices:
            name = dev.get("name", "")
            if "eve" in name.lower() and "energy" in name.lower():
                discovered.append({
                    "device_id": str(dev.get("id", dev.get("aid", ""))),
                    "name": name,
                    "source": "shortcuts_proxy",
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                })

    # Try aiohomekit mDNS discovery
    try:
        loop = asyncio.new_event_loop()
        hap_devices = loop.run_until_complete(discover_eve_devices())
        loop.close()
        discovered.extend(hap_devices)
    except Exception as e:
        log.debug("aiohomekit discovery failed: %s", e)

    if discovered:
        _merge_discovered_devices(discovered)
        log.info("Discovery complete: %d Eve Energy device(s) found.", len(discovered))
    else:
        log.warning(
            "No Eve Energy devices discovered. See SETUP notes in script header."
        )

    return discovered


def main():
    """Main daemon loop: discover, poll, insert, sleep, repeat."""
    log.info("=" * 60)
    log.info("nova_energy_poller starting up")
    log.info("Poll interval: %ds", POLL_INTERVAL)
    log.info("State file: %s", STATE_FILE)
    log.info("=" * 60)

    # First run: attempt discovery if no known devices
    known = load_known_devices()
    if not known:
        log.info("No known devices, running discovery...")
        discovered = run_discovery()
        if not discovered:
            log.warning(
                "No Eve Energy devices found on first run. "
                "Will retry discovery each cycle until devices appear."
            )
            _print_setup_guide()

    # Establish DB connection
    conn = get_db_connection()
    if not conn:
        log.error("Cannot connect to database. Will retry on each cycle.")

    consecutive_failures = 0
    last_discovery = time.time()
    REDISCOVERY_INTERVAL = 3600  # Re-run discovery every hour

    while not _shutdown:
        cycle_start = time.time()

        # Re-run discovery periodically
        if time.time() - last_discovery > REDISCOVERY_INTERVAL:
            run_discovery()
            last_discovery = time.time()

        # Poll all sources
        readings = poll_all_sources()

        if readings:
            consecutive_failures = 0

            # Ensure DB connection
            if conn is None or conn.closed:
                conn = get_db_connection()

            if conn:
                inserted = insert_readings(conn, readings)
                log.info(
                    "Inserted %d/%d reading(s) into telemetry.energy",
                    inserted, len(readings)
                )

                # Log a summary line for each device
                for r in readings:
                    log.info(
                        "  %s: %.1fW, %.1fV, %.3fA, %.2f kWh total, relay=%s",
                        r.get("device_name", r["device_id"]),
                        r.get("watts", 0),
                        r.get("volts", 0),
                        r.get("amps", 0),
                        r.get("kwh_total", 0),
                        "ON" if r.get("on_state") else "OFF",
                    )
            else:
                log.error("No DB connection, readings lost.")
        else:
            consecutive_failures += 1
            if consecutive_failures == 1:
                log.warning("No readings from any source this cycle.")
            elif consecutive_failures % 10 == 0:
                log.warning(
                    "No readings for %d consecutive cycles (%d min). "
                    "Check Eve device connectivity.",
                    consecutive_failures,
                    consecutive_failures * POLL_INTERVAL // 60,
                )

        # Sleep until next poll
        elapsed = time.time() - cycle_start
        sleep_time = max(0, POLL_INTERVAL - elapsed)
        if sleep_time > 0 and not _shutdown:
            time.sleep(sleep_time)

    # Clean shutdown
    log.info("Shutting down.")
    if conn and not conn.closed:
        conn.close()


def _print_setup_guide():
    """Log setup instructions when no devices are found."""
    log.info("")
    log.info("=" * 60)
    log.info("SETUP GUIDE: Eve Energy HomeKit Integration")
    log.info("=" * 60)
    log.info("")
    log.info("Eve Energy devices use HAP (HomeKit Accessory Protocol).")
    log.info("To make them accessible to this poller, use ONE of these methods:")
    log.info("")
    log.info("METHOD 1: Shortcuts CLI Proxy (recommended)")
    log.info("  - Create a macOS Shortcut that reads Eve Energy data")
    log.info("  - The proxy at port 37432 exposes device characteristics as JSON")
    log.info("  - Shortcut actions: 'Get state of [Eve Energy]' for each device")
    log.info("  - Requires devices paired to this Mac's Home app")
    log.info("")
    log.info("METHOD 2: aiohomekit Direct Pairing")
    log.info("  - Pair each Eve Energy using its 8-digit setup code")
    log.info("  - Store pairing data in: %s", PAIRING_FILE)
    log.info("  - Format: {\"alias\": {\"AccessoryPairingID\": ..., ...}}")
    log.info("  - Run: python3 -m aiohomekit --discover")
    log.info("  - Then: python3 -m aiohomekit --pair -d <device_id> -p <pin>")
    log.info("  - Note: Device can only be paired to ONE controller at a time.")
    log.info("  - If paired to Apple Home, use Method 1 or 3 instead.")
    log.info("")
    log.info("METHOD 3: Homebridge + Eve Plugin")
    log.info("  - Install: npm i -g homebridge-eve-energy")
    log.info("  - Or use homebridge-mqtt-thing with Eve's BLE advertisements")
    log.info("  - Configure in ~/homebridge/config.json")
    log.info("  - The Homebridge API will expose power characteristics")
    log.info("")
    log.info("METHOD 4: Eve BLE Direct (future)")
    log.info("  - Eve Energy broadcasts BLE advertisements with power data")
    log.info("  - Requires: bleak (pip install bleak)")
    log.info("  - Eve uses custom GATT service E863F007-...")
    log.info("  - This method works even while device is paired to Apple Home")
    log.info("")
    log.info("CURRENT STATUS:")
    log.info("  - Shortcuts proxy (37432): %s",
             "REACHABLE" if _http_get(f"{SHORTCUTS_PROXY}/", timeout=3) else "NOT RUNNING")
    log.info("  - aiohomekit pairings: %s",
             "FOUND" if PAIRING_FILE.exists() else "NOT CONFIGURED")
    log.info("  - Homebridge: %s",
             "REACHABLE" if _http_get("http://127.0.0.1:8581/api/status", timeout=3) else "NOT RUNNING")
    log.info("")
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    except Exception as e:
        log.exception("Fatal error: %s", e)
        sys.exit(1)
