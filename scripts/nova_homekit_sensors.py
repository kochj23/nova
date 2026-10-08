#!/usr/bin/env python3
"""
nova_homekit_sensors.py — ingest the untapped HomeKit sensor streams.

NovaHomeKit (:37433) exposes climate + air-quality + light-level characteristics
that nothing was consuming. This polls them and writes to the existing telemetry
tables so they show up on dashboards alongside the Zigbee/weather data:

  - Current Temperature / Relative Humidity / Light Level -> telemetry.climate
  - VOC Density / Air Quality (Eve Room)                  -> telemetry.air_quality

Battery is handled by nova_homekit_battery_monitor.py; occupancy by
nova_fp2_presence.py. Read-only against HomeKit. Run on a schedule (~2 min).
"""
import json
import sys
import urllib.request
from pathlib import Path

import psycopg2

import nova_homekit_client as hk  # Bearer token (NovaHomeKit 51e7a91)

HOMEKIT_URL = "http://127.0.0.1:37433/api/accessories"
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
SOURCE = "homekit"

# HomeKit room name -> Nova canonical room. Anything unmapped is slugified.
ROOM_MAP = {
    "Office": "server_rack",  # this HomeKit "Office" accessory is physically IN the rack
                              # (~94F constant); real office temp comes from office_presence (FP2)
    "Outdoor": "patio", "Living Room": "living_room",
    "Master Bedroom": "master_bedroom", "Kitchen": "kitchen", "Garage": "garage",
    "Dining Room": "dining", "Dylan’s Room": "dylans_room", "Front Porch": "front_porch",
    "WTF": "server_rack",   # the Eve Room lives in the rack area
}


def slug(room):
    if room in ROOM_MAP:
        return ROOM_MAP[room]
    return (room or "unknown").lower().replace(" ", "_").replace("’", "")


def fetch(retries=6):
    import time as _t
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(HOMEKIT_URL, headers=hk.auth_headers()), timeout=15) as r:
                raw = r.read()
            if raw and len(raw) > 1000:
                return json.loads(raw)
        except Exception as e:
            print(f"[hk-sensors] fetch {i+1} failed: {e}", flush=True)
        _t.sleep(4)
    return None


def c_to_f(c):
    try:
        return round(float(c) * 9 / 5 + 32, 2)
    except (TypeError, ValueError):
        return None


def collect(acc):
    """Pull the sensor values out of one accessory's characteristics."""
    v = {}
    for s in acc.get("services") or []:
        for c in s.get("characteristics") or []:
            t, val = c.get("type"), c.get("value")
            if val is None:
                continue
            if t == "Current Temperature":
                v["temp_f"] = c_to_f(val)
            elif t == "Current Relative Humidity":
                v["humidity"] = round(float(val), 1)
            elif t == "Current Light Level":
                v["lux"] = round(float(val), 1)
            elif t == "Volatile Organic Compound Density":
                v["voc"] = round(float(val), 1)
            elif t == "Air Quality":
                v["aqi"] = float(val)
    return v


def main():
    acc = fetch()
    if not acc:
        print("[hk-sensors] HomeKit unreachable — skipping", flush=True)
        return 1
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()

    n_climate = n_aq = 0
    for a in acc:
        room = slug(a.get("room"))
        v = collect(a)
        if not v:
            continue
        # climate: any of temp/humidity/lux
        if any(k in v for k in ("temp_f", "humidity", "lux")):
            cur.execute(
                "INSERT INTO telemetry.climate (ts, room, source, temp_f, humidity, light_lux) "
                "VALUES (now(), %s, %s, %s, %s, %s)",
                (room, SOURCE, v.get("temp_f"), v.get("humidity"), v.get("lux")))
            n_climate += 1
        # air quality: VOC and/or HomeKit AQ index
        if "voc" in v or "aqi" in v:
            cur.execute(
                "INSERT INTO telemetry.air_quality (ts, source, room, voc, aqi) "
                "VALUES (now(), %s, %s, %s, %s)",
                (SOURCE, room, v.get("voc"), v.get("aqi")))
            n_aq += 1
        print(f"[hk-sensors] {room}/{a.get('name')}: {v}", flush=True)

    print(f"[hk-sensors] wrote {n_climate} climate + {n_aq} air_quality rows.", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
