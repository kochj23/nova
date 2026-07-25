#!/opt/homebrew/bin/python3
"""
nova_ble_monitor.py — BLE presence detection, battery monitoring, and device scanning.

Uses the Mac's Bluetooth radio to:
1. Scan for all BLE advertisements (30s cycle)
2. Track RSSI of known devices (iPhone, AirPods, etc.)
3. Triangulate room presence using HomePod RSSI signatures
4. Monitor battery levels on AirPods/peripherals
5. Alert on unknown BLE devices appearing
6. Feed data into telemetry.bluetooth and telemetry.presence

Also polls system_profiler SPBluetoothDataType for connected device battery/RSSI
(more reliable than BLE scanning for Apple devices which hide from generic scans).

Written by Jordan Koch (via Claude).
"""

import asyncio
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 required — pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)

try:
    from bleak import BleakScanner
except ImportError:
    BleakScanner = None

import nova_config
from nova_notify import notify

# ── Config ────────────────────────────────────────────────────────────────────

DB_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
LOG_FILE = Path.home() / ".openclaw/logs/ble_monitor.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/ble_known_devices.json"
SCAN_INTERVAL = 30
BLE_SCAN_DURATION = 10

# Known devices — MAC → (name, type, owner)
KNOWN_DEVICES = {
    "28:EA:2D:19:4E:E3": ("Jordan's iPhone", "phone", "jordan"),
    "14:C8:8B:C6:F7:55": ("kochj's AirPods", "headphones", "jordan"),
    "90:9C:4A:EA:DA:AA": ("kochj's AirPods Max", "headphones", "jordan"),
    "F0:04:E1:DF:AC:88": ("kochj's AirPods Pro", "headphones", "jordan"),
    "1C:1D:D3:81:0C:42": ("kochj's Magic Keyboard", "peripheral", "jordan"),
    "EC:2C:E2:EF:5E:7E": ("Magic Trackpad 2", "peripheral", "jordan"),
    "DC:07:DF:4B:6B:21": ("Jordan's Mac mini", "computer", "jordan"),
    "FB:82:AD:79:D6:05": ("Apple TV (TV-Movies)", "appletv", "home"),
    "6C:4A:85:21:32:BA": ("Apple TV (unknown)", "appletv", "home"),
}

# HomePods — MAC → room name (used for triangulation)
HOMEPOD_ROOMS = {
    "D4:90:9C:E5:6A:57": "back_door",
    "40:ED:CF:A5:06:63": "dylans_room",
    "C4:F7:C1:39:D2:89": "dylans_room",
    "F0:B3:EC:78:CD:0C": "garage",
    "58:D3:49:4F:00:64": "garage",
    "F4:34:F0:2F:D6:40": "guest_bathroom",
    "64:D2:C4:BA:44:AF": "kitchen",
    "F0:B3:EC:1F:53:67": "living_room",
    "40:ED:CF:BC:29:CF": "master_bedroom",
    "8C:26:AA:DA:6F:37": "master_bedroom",
    "58:D3:49:2B:C7:90": "master_bathroom",
    "C4:F7:C1:55:6D:25": "office",
    "D4:90:9C:E7:0A:3F": "office",
    "58:D3:49:28:8A:48": "outside",
}

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ble_monitor")

# ── Shutdown ──────────────────────────────────────────────────────────────────

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log.info(f"Received signal {signum}, shutting down...")


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ── Database ──────────────────────────────────────────────────────────────────

_conn = None


def get_db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DB_DSN)
        _conn.autocommit = True
    return _conn


def insert_bluetooth(rows):
    """Insert BLE scan results into telemetry.bluetooth."""
    if not rows:
        return
    conn = get_db()
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO telemetry.bluetooth (ts, device_mac, device_name, rssi, battery_pct, device_type, is_connected, metadata, fingerprint)
               VALUES %s""",
            rows,
            template="(NOW(), %s, %s, %s, %s, %s, %s, %s, %s)",
        )


def insert_presence(person, room, confidence, method="ble_rssi", metadata=None):
    """Insert presence estimate into telemetry.presence."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)
               VALUES (NOW(), %s, %s, %s, %s, %s)""",
            (person, room, confidence, method, json.dumps(metadata) if metadata else None),
        )


# ── System Profiler Scanning ──────────────────────────────────────────────────

def scan_system_profiler() -> list[dict]:
    """Parse system_profiler SPBluetoothDataType for all visible devices."""
    try:
        result = subprocess.run(
            ["system_profiler", "SPBluetoothDataType", "-json"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log.warning(f"system_profiler failed: {result.stderr[:100]}")
            return []
        data = json.loads(result.stdout)
    except Exception as e:
        log.warning(f"system_profiler error: {e}")
        return []

    devices = []
    try:
        bt_data = data.get("SPBluetoothDataType", [{}])[0]
        for section_key in ("device_connected", "device_not_connected"):
            section = bt_data.get(section_key, [])
            if isinstance(section, list):
                for item in section:
                    if isinstance(item, dict):
                        for name, info in item.items():
                            devices.append(_parse_bt_device(name, info, section_key == "device_connected"))
            elif isinstance(section, dict):
                for name, info in section.items():
                    devices.append(_parse_bt_device(name, info, section_key == "device_connected"))
    except Exception as e:
        log.warning(f"Error parsing BT data: {e}")

    return [d for d in devices if d is not None]


def _parse_bt_device(name: str, info: dict, connected: bool) -> dict | None:
    """Parse a single device from system_profiler output."""
    if not isinstance(info, dict):
        return None
    addr = info.get("device_address", "")
    if not addr:
        return None

    rssi = info.get("device_rssi")
    if isinstance(rssi, str):
        try:
            rssi = int(rssi)
        except ValueError:
            rssi = None

    battery = None
    for key in ("device_batteryLevel", "device_batteryLevelMain", "device_batteryLevelCase",
                "device_batteryLevelLeft", "device_batteryLevelRight"):
        val = info.get(key)
        if val is not None:
            try:
                battery = int(str(val).rstrip("%"))
                break
            except ValueError:
                pass

    device_type = "unknown"
    minor = info.get("device_minorType", "").lower()
    if "headphone" in minor or "airpod" in name.lower():
        device_type = "headphones"
    elif "keyboard" in minor or "keyboard" in name.lower():
        device_type = "peripheral"
    elif "trackpad" in minor or "mouse" in minor:
        device_type = "peripheral"
    elif "phone" in name.lower() or "iphone" in name.lower():
        device_type = "phone"
    elif "homepod" in name.lower() or addr.upper() in HOMEPOD_ROOMS:
        device_type = "homepod"
    elif "mac" in name.lower():
        device_type = "computer"
    elif "tv" in name.lower() or "apple tv" in name.lower():
        device_type = "appletv"

    known = KNOWN_DEVICES.get(addr.upper())
    if known:
        name = known[0]
        device_type = known[1]

    if addr.upper() in HOMEPOD_ROOMS:
        device_type = "homepod"
        room = HOMEPOD_ROOMS[addr.upper()]
        name = f"HomePod ({room})"

    return {
        "mac": addr.upper(),
        "name": name,
        "rssi": rssi,
        "battery": battery,
        "type": device_type,
        "connected": connected,
        "metadata": {
            k: v for k, v in {
                "firmware": info.get("device_firmwareVersion"),
                "product_id": info.get("device_productID"),
                "vendor_id": info.get("device_vendorID"),
            }.items() if v
        },
    }


# ── BLE Active Scanning ──────────────────────────────────────────────────────

# Bluetooth SIG-assigned company identifiers -> a coarse, human-useful category.
# Not exhaustive -- covers the manufacturers actually seen in this house's scan
# history plus the biggest global players. Unrecognized IDs classify as "other".
BLE_COMPANY_CATEGORIES = {
    0x004C: ("Apple", "phone/wearable"),
    0x0075: ("Samsung", "phone/wearable"),
    0x00E0: ("Google", "phone/wearable"),
    0x0006: ("Microsoft", "peripheral"),
    0x0087: ("Garmin", "wearable"),
    0x0157: ("Anhui Huami / Amazfit", "wearable"),
    0x038F: ("Xiaomi", "iot"),
    0x0059: ("Nordic Semiconductor", "iot"),  # extremely common generic BLE chip vendor
    0x02E5: ("August Home", "iot"),
    0x005D: ("Nike", "wearable"),
    0x004F: ("Polar Electro", "wearable"),
    0x0171: ("Amazon", "iot"),
    0x00D2: ("AbTemp / misc sensor vendors", "iot"),
    0x0499: ("Ruuvi", "iot"),
    0x0958: ("Chipolo", "tracker"),
    0x0842: ("Tile", "tracker"),
}


def classify_manufacturer(manufacturer_data: dict) -> tuple[str, str]:
    """manufacturer_data: {company_id: bytes} from bleak's AdvertisementData.
    Returns (vendor_label, category) -- both 'unknown' if no data or no match."""
    if not manufacturer_data:
        return ("unknown", "unknown")
    company_id = next(iter(manufacturer_data.keys()))
    vendor, category = BLE_COMPANY_CATEGORIES.get(company_id, (f"0x{company_id:04X}", "other"))
    return (vendor, category)


def compute_ble_fingerprint(name, service_uuids, company_ids, tx_power):
    """A stable device identity that survives BLE MAC/UUID rotation.

    MAC randomization is a software identifier swap; it does NOT touch these advertising
    fields, which a device keeps constant across rotations:
      - local name (most stable when present)
      - the SET of advertised service UUIDs (per model/firmware)
      - the manufacturer company IDs (the vendor's assigned ID — NOT the payload bytes,
        which rotate, e.g. Apple Continuity counters)
      - tx power level (semi-stable)
    Track/dedupe on this instead of device_mac. A bare randomizing phone (no name, no
    service UUIDs, generic company 0x004C) collapses to a weak shared fingerprint — that
    population genuinely needs RF/PHY capture (Ubertooth/SDR) to separate; software can't.
    Returns None when there's nothing stable to hash (avoid a meaningless all-empty key).
    """
    uuids = ",".join(sorted(str(u).lower() for u in (service_uuids or [])))
    cids = ",".join(sorted(f"{c:#06x}" for c in (company_ids or [])))
    nm = (name or "").strip().lower()
    if not (nm or uuids or cids):
        return None
    parts = f"{nm}|{uuids}|{cids}|{tx_power if tx_power is not None else ''}"
    return hashlib.sha1(parts.encode()).hexdigest()[:16]


async def scan_ble() -> list[dict]:
    """Perform an active BLE scan using bleak."""
    if BleakScanner is None:
        return []

    devices = []
    try:
        discovered = await BleakScanner.discover(timeout=BLE_SCAN_DURATION, return_adv=True)
        for mac_raw, (d, adv) in discovered.items():
            mac = d.address.upper() if d.address else ""
            if not mac or len(mac) < 12:
                continue
            rssi = adv.rssi if adv is not None else None
            name = d.name or ""
            mfr = getattr(adv, "manufacturer_data", None) or {}
            vendor, mfr_category = classify_manufacturer(mfr)
            # Advertising fields that survive MAC/UUID rotation -> the stable fingerprint.
            service_uuids = list(getattr(adv, "service_uuids", None) or [])
            company_ids = list(mfr.keys())
            tx_power = getattr(adv, "tx_power", None)
            fingerprint = compute_ble_fingerprint(name, service_uuids, company_ids, tx_power)

            device_type = "ble_device"
            known = KNOWN_DEVICES.get(mac)
            if known:
                name = known[0]
                device_type = known[1]
            elif mac in HOMEPOD_ROOMS:
                device_type = "homepod"
                name = f"HomePod ({HOMEPOD_ROOMS[mac]})"

            devices.append({
                "mac": mac,
                "name": name,
                "rssi": rssi,
                "battery": None,
                "type": device_type,
                "connected": False,
                "fingerprint": fingerprint,
                "metadata": {"vendor": vendor, "mfr_category": mfr_category,
                             "service_uuids": service_uuids,
                             "company_ids": [f"{c:#06x}" for c in company_ids],
                             "tx_power": tx_power, "fingerprint": fingerprint},
            })
    except Exception as e:
        log.warning(f"BLE scan error: {e}")

    return devices


# ── Presence Estimation ───────────────────────────────────────────────────────

_last_presence = {}


def estimate_presence(devices: list[dict]):
    """Estimate room presence based on iPhone RSSI relative to HomePods."""
    global _last_presence

    iphone = None
    homepod_rssi = {}

    for d in devices:
        if d["mac"] == "28:EA:2D:19:4E:E3":
            iphone = d
        if d["mac"] in HOMEPOD_ROOMS and d.get("rssi") is not None:
            room = HOMEPOD_ROOMS[d["mac"]]
            if room not in homepod_rssi or d["rssi"] > homepod_rssi[room]:
                homepod_rssi[room] = d["rssi"]

    if not iphone or iphone.get("rssi") is None:
        return

    iphone_rssi = iphone["rssi"]

    if iphone_rssi > -50:
        location = "office"
        confidence = 0.9
    elif iphone_rssi > -65:
        location = "nearby"
        confidence = 0.6
    else:
        location = "away"
        confidence = 0.4

    if homepod_rssi:
        strongest_room = max(homepod_rssi, key=homepod_rssi.get)
        strongest_val = homepod_rssi[strongest_room]
        if strongest_val > -60:
            location = strongest_room
            confidence = min(0.95, 0.5 + (strongest_val + 60) * 0.02)

    location_changed = location != _last_presence.get("jordan")
    if location_changed:
        _last_presence["jordan"] = location
        log.info(f"Presence: jordan → {location} (confidence={confidence:.2f}, iPhone RSSI={iphone_rssi})")

    # Optimization: only write a presence row when the estimate meaningfully
    # changes (location, or confidence crossing a 0.1 bucket boundary) instead
    # of every 30s cycle (~2880 rows/day while stationary). Mirrors the existing
    # _last_presence log-on-change pattern.
    # Tradeoff: drops per-cycle RSSI granularity in telemetry.presence — the
    # iphone_rssi/homepod_rssi metadata is snapshotted only on change, not every
    # scan. All presence transitions and confidence-bucket shifts still recorded.
    conf_bucket = round(confidence, 1)
    if location_changed or conf_bucket != _last_presence.get("jordan_conf"):
        _last_presence["jordan_conf"] = conf_bucket
        insert_presence("jordan", location, confidence, metadata={
            "iphone_rssi": iphone_rssi,
            "homepod_rssi": homepod_rssi,
        })


# ── Unknown Device Detection ─────────────────────────────────────────────────

_known_macs = set(KNOWN_DEVICES.keys()) | set(HOMEPOD_ROOMS.keys())
_alerted_macs = set()


def check_unknown_devices(devices: list[dict]):
    """Alert on new unknown BLE devices."""
    global _alerted_macs

    for d in devices:
        mac = d["mac"]
        if mac in _known_macs or mac in _alerted_macs:
            continue
        if d.get("rssi") is not None and d["rssi"] > -80:
            _alerted_macs.add(mac)
            name = d.get("name") or "unnamed"
            rssi = d.get("rssi")
            log.warning(f"Unknown BLE device: {mac} ({name}) RSSI={rssi}")

            try:
                conn = get_db()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
                        VALUES ('nova', 'security', 'ble-unknown-device', %s, 'warning', %s)
                    """, (
                        f"Unknown BLE device detected: {mac} ({name}) RSSI={rssi}",
                        json.dumps({"mac": mac, "name": name, "rssi": rssi, "type": d.get("type")}),
                    ))
            except Exception:
                pass


# ── Vulnerable BLE Device Watchlist ───────────────────────────────────────────
# Passive-only: read the advertised BLE name, match it, log it. No connecting,
# no touching a device's auth protocol -- that's the exploit itself, not
# detection, and it's out of scope regardless of framing.
#
# General-purpose so the next publicly-disclosed vulnerable BLE device (KARR
# won't be the last) is a one-entry addition, not a new pipeline. Each entry's
# `confidence` is honest about how the pattern was derived -- see individual
# notes.
VULNERABLE_BLE_WATCHLIST = [
    {
        "key": "karr-vulnerable-device-detected",
        "label": "KARR/SWDS vulnerable car alarm",
        "patterns": ("karr", "swds"),
        "confidence": "probable-name-match",
        # "KARR"/"SWDS" are the brand strings on the physical window sticker and in
        # the manufacturer's own FCC filing title ("SWDS_KARR Security Systems") --
        # consumer BLE accessories commonly advertise their own brand name, but no
        # public source (FCC docs, WIRED's coverage, UCSD's writeup, or static
        # analysis of the official app binary) confirms the literal advertised
        # string byte-for-byte. Treat matches as PROBABLE, not certain.
        "description": "Unpatched units are remotely unlockable/immobilizable -- "
                        "see the July 2026 UCSD/WIRED disclosure.",
    },
]
_watchlist_alerted = set()


def check_watchlist_devices(devices: list[dict]):
    """Flag BLE devices matching VULNERABLE_BLE_WATCHLIST. Every raw sighting is
    logged to vulnerable_ble_sightings (for persistence analysis -- a MAC seen
    on multiple distinct days is probably a resident's device, not a passerby's);
    the Slack/local page fires once per MAC per process lifetime to avoid spam."""
    global _watchlist_alerted

    for d in devices:
        mac = d["mac"]
        name = (d.get("name") or "").lower()
        if not name:
            continue
        for entry in VULNERABLE_BLE_WATCHLIST:
            if not any(p in name for p in entry["patterns"]):
                continue
            rssi = d.get("rssi")

            try:
                conn = get_db()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO vulnerable_ble_sightings (watchlist_key, device_mac, device_name, rssi)
                        VALUES (%s, %s, %s, %s)
                    """, (entry["key"], mac, d.get("name"), rssi))
            except Exception:
                pass

            already_alerted = (entry["key"], mac) in _watchlist_alerted
            if already_alerted:
                continue
            _watchlist_alerted.add((entry["key"], mac))
            log.warning(f"Possible {entry['label']}: {mac} ({d.get('name')}) RSSI={rssi}")

            try:
                conn = get_db()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
                        VALUES ('nova', 'security', %s, %s, 'warning', %s)
                    """, (
                        entry["key"],
                        f"Possible {entry['label']} detected: {d.get('name')} ({mac}) RSSI={rssi}. "
                        f"{entry['description']}",
                        json.dumps({"mac": mac, "name": d.get("name"), "rssi": rssi,
                                    "confidence": entry["confidence"]}),
                    ))
            except Exception:
                pass

            # Page immediately rather than waiting for the daily rollup -- a nearby
            # remotely-stealable vehicle is worth interrupting for. critical level
            # auto-routes to #nova-critical (see nova_notifier.py's level->channel map).
            try:
                notify(
                    f"Possible {entry['label']} nearby",
                    body=f"{d.get('name')} ({mac}) RSSI={rssi} -- {entry['confidence']}",
                    level="critical", category="security", source="nova_ble_monitor.py",
                    dedup_key=f"{entry['key']}-{mac}",
                )
            except Exception:
                pass
            try:
                nova_config.notify_local(
                    f"Possible {entry['label']} nearby",
                    f"{d.get('name')} ({mac}) RSSI={rssi}",
                    sound="Sosumi", critical=True,
                )
            except Exception:
                pass


# ── Battery Alerts ────────────────────────────────────────────────────────────

_battery_alerted = set()


def check_batteries(devices: list[dict]):
    """Alert on low battery devices."""
    for d in devices:
        if d.get("battery") is not None and d["battery"] <= 15:
            mac = d["mac"]
            if mac in _battery_alerted:
                continue
            _battery_alerted.add(mac)
            name = d.get("name", mac)
            log.warning(f"Low battery: {name} at {d['battery']}%")
            try:
                notify(
                    "Low Battery Alert",
                    body=f"{name}: {d['battery']}%",
                    level="warning",
                    category="health",
                    dedup_key=f"ble-low-battery-{mac}",
                    meta={"host": "mac-studio", "device": name, "mac": mac},
                )
            except Exception:
                pass
        elif d.get("battery") is not None and d["battery"] > 30:
            _battery_alerted.discard(d["mac"])


# ── Main Loop ─────────────────────────────────────────────────────────────────

async def poll_cycle():
    """Run one poll cycle: system_profiler + BLE scan → insert + analyze."""
    sp_devices = scan_system_profiler()
    ble_devices = await scan_ble()

    merged = {}
    for d in sp_devices + ble_devices:
        mac = d["mac"]
        if mac not in merged or (d.get("rssi") is not None and merged[mac].get("rssi") is None):
            merged[mac] = d
        else:
            if d.get("battery") and not merged[mac].get("battery"):
                merged[mac]["battery"] = d["battery"]
            if d.get("connected"):
                merged[mac]["connected"] = True

    all_devices = list(merged.values())

    rows = []
    for d in all_devices:
        rows.append((
            d["mac"], d.get("name"), d.get("rssi"), d.get("battery"),
            d.get("type", "unknown"), d.get("connected", False),
            json.dumps(d.get("metadata")) if d.get("metadata") else None,
            d.get("fingerprint"),
        ))

    try:
        insert_bluetooth(rows)
    except Exception as e:
        log.error(f"DB insert failed: {e}")
        global _conn
        _conn = None

    estimate_presence(all_devices)
    check_unknown_devices(all_devices)
    check_watchlist_devices(all_devices)
    check_batteries(all_devices)

    log.info(f"Scan: {len(all_devices)} devices ({len(sp_devices)} profiler, {len(ble_devices)} BLE)")


async def main():
    log.info("nova_ble_monitor starting — scanning every %ds", SCAN_INTERVAL)
    log.info("Known devices: %d, HomePod rooms: %d", len(KNOWN_DEVICES), len(HOMEPOD_ROOMS))

    while not _shutdown:
        try:
            await poll_cycle()
        except Exception as e:
            log.error(f"Poll cycle error: {e}")

        for _ in range(SCAN_INTERVAL):
            if _shutdown:
                break
            await asyncio.sleep(1)

    log.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
