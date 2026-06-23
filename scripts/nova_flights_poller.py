#!/usr/bin/env python3
"""
nova_flights_poller.py — what's flying over the house (zip 91506, Burbank).

Polls the free adsb.lol feed for aircraft over the zip, logs everything within
the low/overhead band to telemetry.overhead_flights, and pings Slack (via the
notification bus) for the interesting ones: helicopters, low passes, and any
emergency squawk. Arrival de-dup uses the table itself (no separate state).

Run on a schedule (launchd, every ~30s). Source-agnostic: swap FEED_URL for a
local dump1090 (http://host/data/aircraft.json) later for better coverage.
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

DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
# 91506 (Burbank) centroid + a radius that covers the zip.
LAT, LON, ZIP_RADIUS_NM = 34.169, -118.325, 3.0
ALT_CEILING_FT = 10000          # only low/overhead traffic (Jordan's pick)
LOW_PASS_FT, CLOSE_NM = 4000, 1.5   # a "low pass" = this low AND this close
NEW_GAP_S = 180                 # hex unseen this long => it's a fresh arrival
EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}
FEED_URL = f"https://api.adsb.lol/v2/point/{LAT}/{LON}/{int(ZIP_RADIUS_NM)}"

# Friendly names for the type codes common over Burbank (fallback: the raw code).
TYPE_NAMES = {
    "AS50": "Airbus AS350", "EC30": "Airbus EC130", "B06": "Bell 206", "B407": "Bell 407",
    "B429": "Bell 429", "R44": "Robinson R44", "R66": "Robinson R66", "S76": "Sikorsky S-76",
    "H60": "Sikorsky Black Hawk", "AS65": "Airbus AS365", "EC45": "Airbus EC145", "MD50": "MD 500",
    "C172": "Cessna 172", "C152": "Cessna 152", "C182": "Cessna 182", "SR22": "Cirrus SR22",
    "PA28": "Piper Cherokee", "B738": "Boeing 737-800", "B739": "Boeing 737-900", "A320": "Airbus A320",
    "A319": "Airbus A319", "A321": "Airbus A321", "E75L": "Embraer E175", "CRJ2": "Bombardier CRJ200",
    "CRJ7": "Bombardier CRJ700", "CRJ9": "Bombardier CRJ900", "E145": "Embraer ERJ-145",
}
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass(bearing):
    try:
        return COMPASS[int((float(bearing) + 22.5) % 360 // 45)]
    except (TypeError, ValueError):
        return "?"


def fetch():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "Nova/flights"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()).get("ac", [])


def main():
    try:
        aircraft = fetch()
    except Exception as e:
        print(f"[flights] feed unreachable: {e}", flush=True)
        return 0  # next poll will catch up; no retry loop needed

    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    logged = pinged = 0

    for a in aircraft:
        alt = a.get("alt_baro")
        if not isinstance(alt, (int, float)) or alt >= ALT_CEILING_FT:
            continue
        dst = a.get("dst")
        if not isinstance(dst, (int, float)) or dst > ZIP_RADIUS_NM:
            continue

        hexid = a.get("hex")
        is_heli = a.get("category") == "A7"
        squawk = a.get("squawk")
        tcode = (a.get("t") or "").strip()
        tname = TYPE_NAMES.get(tcode, tcode or "aircraft")
        comp = compass(a.get("dir"))
        callsign = (a.get("flight") or "").strip()
        reg = (a.get("r") or "").strip()

        # fresh arrival? (table is the state — ponytail-approved)
        cur.execute("SELECT 1 FROM telemetry.overhead_flights WHERE hex=%s AND ts > now() - interval %s LIMIT 1",
                    (hexid, f"{NEW_GAP_S} seconds"))
        is_new = cur.fetchone() is None

        emergency = squawk in EMERGENCY_SQUAWKS
        low_pass = isinstance(alt, (int, float)) and alt < LOW_PASS_FT and dst < CLOSE_NM
        should_ping = is_new and (is_heli or low_pass or emergency)

        cur.execute(
            "INSERT INTO telemetry.overhead_flights "
            "(hex,callsign,registration,aircraft_type,type_name,category,is_helicopter,alt_ft,gs_kt,"
            " track_deg,vert_rate,dist_nm,bearing_deg,compass,squawk,is_mlat,notified,raw) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (hexid, callsign, reg, tcode, tname, a.get("category"), is_heli, int(alt),
             a.get("gs"), a.get("track"), a.get("baro_rate"), round(float(dst), 2), a.get("dir"),
             comp, squawk, bool(a.get("mlat")), should_ping, json.dumps(a)))
        logged += 1

        if should_ping and notify is not None:
            who = f"{tname} ({reg or callsign or hexid})"
            where = f"{int(alt)} ft, {round(float(dst),1)} NM {comp}"
            if emergency:
                title = f"🚨 EMERGENCY squawk {squawk} overhead — {who}"
                level = "warning"
            elif is_heli:
                title = f"🚁 {who} overhead — {where}"
                level = "info"
            else:
                title = f"✈️ Low pass — {who} — {where}"
                level = "info"
            try:
                notify(title, body=f"{who} at {where}, {a.get('gs')} kt, heading {a.get('track')}°.",
                       level=level, category="flights", source="nova_flights_poller.py",
                       dedup_key=f"flight:{hexid}", meta={"hex": hexid, "alt": int(alt), "dst": round(float(dst), 1)})
                pinged += 1
            except Exception as e:
                print(f"[flights] notify failed for {hexid}: {e}", flush=True)

    print(f"[flights] {logged} aircraft over 91506 (<{ALT_CEILING_FT}ft), {pinged} pinged.", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
