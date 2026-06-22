#!/usr/bin/env python3
"""
nova_homekit_battery_monitor.py — watch battery levels on HomeKit sensors.

NovaHomeKit (:37433) already exposes Battery Level + Status Low Battery for every
battery accessory, but nothing was consuming it — so sensors could die silently.
This polls those characteristics, records them to telemetry.battery, and fires a
Slack warning (deduped per device/day) when a device is low.

Run on a schedule (launchd, every 6h). Read-only against HomeKit.
"""
import json
import sys
import urllib.request
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
try:
    from nova_notify import notify
except Exception:
    notify = None

HOMEKIT_URL = "http://127.0.0.1:37433/api/accessories"
DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
LOW_THRESHOLD = 20          # percent — warn at or below this
WARN_THRESHOLD = 40         # info-level heads-up band (logged, not alerted)


def fetch_accessories(retries=6):
    """The large /api/accessories response is occasionally empty on a cold read;
    retry until we get valid JSON."""
    import time as _t
    for i in range(retries):
        try:
            with urllib.request.urlopen(HOMEKIT_URL, timeout=15) as r:
                raw = r.read()
            if raw and len(raw) > 1000:
                return json.loads(raw)
        except Exception as e:
            print(f"[battery] fetch attempt {i+1} failed: {e}", flush=True)
        _t.sleep(4)
    return None


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telemetry.battery (
            ts          timestamptz DEFAULT now(),
            device      text,
            room        text,
            level       int,
            low_battery boolean)""")


def extract_batteries(accessories):
    """Yield (device, room, level, low_battery) for every accessory reporting battery."""
    for a in accessories:
        level = None
        low = None
        for s in a.get("services") or []:
            for c in s.get("characteristics") or []:
                t = c.get("type")
                if t == "Battery Level" and "value" in c:
                    try:
                        level = int(c["value"])
                    except (TypeError, ValueError):
                        pass
                elif t == "Status Low Battery" and "value" in c:
                    low = bool(c["value"]) if c["value"] in (0, 1, True, False) else None
        if level is not None or low is not None:
            yield a.get("name"), a.get("room"), level, low


def main():
    accessories = fetch_accessories()
    if not accessories:
        print("[battery] no accessory data (HomeKit unreachable) — skipping", flush=True)
        return 1

    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    ensure_table(cur)

    rows = list(extract_batteries(accessories))
    low_devices = []
    for device, room, level, low in rows:
        cur.execute(
            "INSERT INTO telemetry.battery (device, room, level, low_battery) VALUES (%s,%s,%s,%s)",
            (device, room, level, low))
        flagged = (low is True) or (level is not None and level <= LOW_THRESHOLD)
        if flagged:
            low_devices.append((device, room, level))
        print(f"[battery] {room}/{device}: level={level} low={low}"
              f"{'  <-- LOW' if flagged else ''}", flush=True)

    # Alert on low devices (deduped per device per day via the notification bus).
    if low_devices and notify is not None:
        for device, room, level in low_devices:
            lvl = f"{level}%" if level is not None else "low-battery flag set"
            try:
                notify(f"Low battery — {device}",
                       body=f"{device} ({room or 'unknown room'}) is at {lvl}. Replace/charge it before it dies.",
                       level="warning", category="battery",
                       source="nova_homekit_battery_monitor.py",
                       dedup_key=f"battery-low:{device}", meta={"room": room, "level": level})
            except Exception as e:
                print(f"[battery] notify failed for {device}: {e}", flush=True)

    print(f"[battery] checked {len(rows)} battery devices, {len(low_devices)} low.", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
