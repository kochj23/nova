#!/usr/bin/env python3
"""nova_hue_history.py — append-only Hue time-series poller (color + brightness +
on-state + estimated power) into telemetry.hue_light_history.

The old nova_hue_poller.py upserted current state only (no history) and dropped
all color. This captures a full sample per light per run so we can graph the
circadian color-temp curve, color usage, duty cycles, and estimated energy.

Hue bulbs have NO power meter, so est_watts is an estimate: rated watts (by type)
× brightness fraction, 0 when off. Runs from .6; API key from Keychain.
Written by Jordan Koch (via Claude).
"""
import json
import subprocess
import sys
import urllib.request

import psycopg2

BRIDGE = "192.168.1.152"
DSN = "host=192.168.1.138 dbname=nova_ops user=kochj"


def api_key():
    # macOS Keychain first (when running on the Mac Studio .6).
    for s in ("nova-hue-api-key", "nova-hue-api-token"):
        for args in (["-a", "nova", "-s", s, "-w"], ["-s", s, "-w"]):
            try:
                r = subprocess.run(["security", "find-generic-password", *args],
                                   capture_output=True, text=True)
            except FileNotFoundError:
                r = None  # not macOS (Linux cluster node) — no `security` binary
            if r and r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
    # Portable fallback: the fleet PG secret store (lets this poller run on the Linux
    # cluster nodes, which have no Keychain). #650 migration enabler.
    try:
        import nova_secrets
        return nova_secrets.get_secret("nova-hue-api-key")
    except Exception:
        pass
    raise RuntimeError("no Hue API key in Keychain or fleet secret store")


def get(key, path):
    with urllib.request.urlopen(f"http://{BRIDGE}/api/{key}/{path}", timeout=10) as r:
        return json.load(r)


def rated_watts(t):
    t = (t or "").lower()
    if "strip" in t or "gradient" in t:
        return 20.0
    if "color" in t:
        return 10.0
    if "white" in t:
        return 9.0
    return 8.0


def room_map(key):
    """light_id -> room name, from Hue groups of type Room/Zone."""
    m = {}
    try:
        for g in get(key, "groups").values():
            if g.get("type") in ("Room", "Zone"):
                for lid in g.get("lights", []):
                    m.setdefault(lid, g.get("name"))
    except Exception:
        pass
    return m


def main():
    key = api_key()
    lights = get(key, "lights")
    rooms = room_map(key)
    rows = []
    for lid, l in lights.items():
        s = l.get("state", {})
        on = bool(s.get("on"))
        bri = s.get("bri")
        ct = s.get("ct")
        kelvin = round(1_000_000 / ct) if ct else None
        est = round(rated_watts(l.get("type")) * ((bri or 0) / 254.0), 2) if on else 0.0
        rows.append((lid, l.get("name"), rooms.get(lid), on, bri,
                     round((bri or 0) / 2.54, 1) if bri is not None else None,
                     s.get("hue"), s.get("sat"), ct, kelvin,
                     s.get("colormode"), bool(s.get("reachable")), est))
    c = psycopg2.connect(DSN); c.autocommit = True
    with c.cursor() as cur:
        cur.execute("""
            CREATE SCHEMA IF NOT EXISTS telemetry;
            CREATE TABLE IF NOT EXISTS telemetry.hue_light_history (
                ts timestamptz NOT NULL DEFAULT now(),
                light_id text NOT NULL, name text, room text,
                is_on boolean, bri int, bri_pct real,
                hue int, sat int, ct_mired int, ct_kelvin int,
                colormode text, reachable boolean, est_watts real);
            CREATE INDEX IF NOT EXISTS idx_huehist_ts ON telemetry.hue_light_history(ts);
            CREATE INDEX IF NOT EXISTS idx_huehist_name_ts ON telemetry.hue_light_history(name, ts);
        """)
        cur.executemany(
            "INSERT INTO telemetry.hue_light_history "
            "(light_id,name,room,is_on,bri,bri_pct,hue,sat,ct_mired,ct_kelvin,colormode,reachable,est_watts) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    c.close()
    on_n = sum(1 for r in rows if r[3])
    print(f"[hue-history] wrote {len(rows)} lights ({on_n} on)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
