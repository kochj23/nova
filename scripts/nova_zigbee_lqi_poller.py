#!/usr/bin/env python3
"""
nova_zigbee_lqi_poller.py — snapshot Zigbee per-device link quality (LQI) into
telemetry.zigbee_link, so mesh strength can be graphed over time.

Runs every few minutes (launchd). Reads the Z2M device roster + each device's
last-reported linkquality from the local Mosquitto broker (anonymous, 127.0.0.1)
and writes one row per device. No third-party deps — shells to mosquitto_sub.
"""
import json
import subprocess
from datetime import datetime, timezone

import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MQTT = ["mosquitto_sub", "-h", "127.0.0.1", "-p", "1883"]


def _sub(args, timeout):
    return subprocess.run(MQTT + args, capture_output=True, text=True,
                          timeout=timeout).stdout


def get_devices() -> dict:
    """friendly_name -> {ieee, type}, excluding the coordinator."""
    out = _sub(["-t", "zigbee2mqtt/bridge/devices", "-C", "1", "-W", "5"], 15)
    try:
        devs = json.loads(out)
    except Exception:
        return {}
    return {x["friendly_name"]: {"ieee": x.get("ieee_address"), "type": x.get("type")}
            for x in devs if x.get("type") != "Coordinator" and x.get("friendly_name")}


def get_lqi() -> dict:
    """device -> latest linkquality, from retained per-device state topics."""
    out = _sub(["-t", "zigbee2mqtt/+", "-v", "-W", "6"], 20)
    lqi = {}
    for line in out.splitlines():
        try:
            topic, payload = line.split(" ", 1)
        except ValueError:
            continue
        name = topic.split("/", 1)[1] if "/" in topic else ""
        if not name or name == "bridge" or "/" in name:
            continue
        try:
            p = json.loads(payload)
        except Exception:
            continue
        if isinstance(p, dict) and p.get("linkquality") is not None:
            lqi[name] = p["linkquality"]
    return lqi


def main() -> int:
    devs = get_devices()
    lqi = get_lqi()
    ts = datetime.now(timezone.utc)
    rows = [(ts, name, devs.get(name, {}).get("ieee"), devs.get(name, {}).get("type"), q)
            for name, q in lqi.items()]
    if not rows:
        print("[lqi-poller] no link-quality data captured this cycle")
        return 0
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO telemetry.zigbee_link (ts, device, ieee, type, lqi) "
                "VALUES (%s, %s, %s, %s, %s)", rows)
    finally:
        conn.close()
    print(f"[lqi-poller] wrote {len(rows)} link-quality rows "
          f"(avg LQI {sum(r[4] for r in rows)//len(rows)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
