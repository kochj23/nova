#!/usr/bin/env python3
"""nova_wifi_presence.py — resolve WiFi clients to PEOPLE and feed the presence engine.

Why this exists: telemetry.presence fuses eight signals (mmWave, camera, vehicle, BLE,
lights, media, motion, GPS) and yet the `person` column is almost entirely placeholders —
'occupant', 'camera_detected', 'motion', 'unknown' — with exactly ONE real name in it.
The house knows somebody is here; it rarely knows who.

Meanwhile the single most reliable indoor signal was being collected and thrown away:
UniFi reports, continuously, every wireless client's associated AP and its signal strength.
Every phone in the house is a named, authenticated, already-known device. That is a free
identity source, and identity is the prerequisite for everything else — you cannot detect
"a tracker that moves with Jordan" until "Jordan" is more than one hand-labelled row.

Room resolution is only as good as the AP layout: with three APs (Kitchen, Garage, Office)
this is a coarse zone, not a coordinate. Association is also STICKY — a phone can hold onto
the Office AP while its owner stands in the kitchen — so confidence is scaled by signal
strength and this is published as one voice in the fusion, never as ground truth.
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
CONTROLLER = "https://192.168.1.1"
STA_URL = f"{CONTROLLER}/proxy/network/api/s/default/stat/sta"
DEV_URL = f"{CONTROLLER}/proxy/network/api/s/default/stat/device"
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

# A client is only "present" if UniFi saw it recently. A stale association means the
# device left without deauthenticating, which is the normal case for phones.
STALE_SECONDS = 600


def log(m):
    print(f"[wifi-presence {time.strftime('%H:%M:%S')}] {m}", flush=True)


def api_key():
    r = subprocess.run(["security", "find-generic-password", "-a", "nova",
                        "-s", "nova-unifi-api-key", "-w"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    try:
        import nova_secrets
        return nova_secrets.get_secret("nova-unifi-api-key") or ""
    except Exception:
        return ""


def fetch(url, key):
    req = urllib.request.Request(url, headers={"X-API-KEY": key})
    with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
        return json.loads(r.read()).get("data", [])


def confidence_from_signal(dbm):
    """Signal strength -> proximity confidence. -40dBm is next to the AP, -80 is far
    or through walls. Deliberately capped below 1.0: a WiFi association is evidence of
    presence in a ZONE, never proof of standing in a room."""
    if dbm is None:
        return 0.35
    return round(max(0.25, min(0.9, (dbm + 90) / 55.0)), 2)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS telemetry.device_owner (
                mac          text PRIMARY KEY,
                person       text NOT NULL,
                device_label text,
                device_kind  text,
                source       text,
                updated_at   timestamptz NOT NULL DEFAULT now()
            )""")
    conn.commit()


def load_owners(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT lower(mac), person, device_label FROM telemetry.device_owner")
        return {m: (p, l) for m, p, l in cur.fetchall()}


def main():
    key = api_key()
    if not key:
        log("FATAL: no UniFi API key")
        return 2
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    ensure_schema(conn)
    owners = load_owners(conn)
    if not owners:
        log("no device_owner rows yet — nothing can resolve to a person (see --seed)")
        return 0

    aps = {d["mac"].lower(): (d.get("name") or d["mac"])
           for d in fetch(DEV_URL, key) if d.get("type") == "uap"}
    clients = [c for c in fetch(STA_URL, key) if not c.get("is_wired")]
    now = time.time()

    rows, seen_people = [], {}
    for c in clients:
        mac = (c.get("mac") or "").lower()
        who = owners.get(mac)
        if not who:
            continue
        if c.get("last_seen") and now - c["last_seen"] > STALE_SECONDS:
            continue
        person, label = who
        ap_room = aps.get((c.get("ap_mac") or "").lower(), "unknown")
        # Strip the hardware model out of the AP name: "Office U6 Enterprise" -> "office"
        room = ap_room.split(" U6")[0].split(" UAP")[0].strip().lower().replace(" ", "_")
        sig = c.get("signal")
        conf = confidence_from_signal(sig)
        # Strongest signal wins when someone carries several devices.
        prev = seen_people.get(person)
        if prev is None or conf > prev[1]:
            seen_people[person] = (room, conf, label, sig, mac)

    for person, (room, conf, label, sig, mac) in seen_people.items():
        rows.append((person, room, conf, "wifi_rssi",
                     json.dumps({"device": label, "mac": mac, "signal_dbm": sig,
                                 "ap_room": room, "source": "unifi",
                                 "note": "zone-level; AP association is sticky"})))

    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata) "
                "VALUES (NOW(), %s, %s, %s, %s, %s)", rows)
        log("presence: " + ", ".join(f"{p}@{r}({c})" for p, (r, c, _, _, _) in seen_people.items()))
    else:
        # Minimum grain: "nobody home" and "the poller is broken" must not look identical.
        log(f"no known devices present ({len(clients)} wireless clients seen, "
            f"{len(owners)} owned MACs known)")
    conn.close()
    return 0


def seed():
    """One-shot: populate device_owner from UniFi client names that clearly identify a
    person. Conservative on purpose — a wrong mapping produces confident wrong presence,
    which is worse than none. Everything else stays unowned until named by hand."""
    import re
    key = api_key()
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    ensure_schema(conn)
    # name pattern -> person. Only unambiguous personal devices.
    RULES = [(re.compile(r"jordan", re.I), "jordan"),
             (re.compile(r"\bamy", re.I), "amy"),
             (re.compile(r"dylan", re.I), "dylan")]
    added = 0
    for c in fetch(STA_URL, key):
        if c.get("is_wired"):
            continue
        label = c.get("name") or c.get("hostname") or ""
        # Cameras and fixed hardware carry room/exterior names — never a person.
        if re.search(r"exterior|interior|camera|cam-|room-\d", label, re.I):
            continue
        for pat, person in RULES:
            if pat.search(label):
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO telemetry.device_owner
                        (mac, person, device_label, device_kind, source)
                        VALUES (%s,%s,%s,%s,'unifi-name-seed')
                        ON CONFLICT (mac) DO UPDATE SET person=EXCLUDED.person,
                          device_label=EXCLUDED.device_label, updated_at=now()""",
                        (c["mac"].lower(), person, label,
                         "phone" if re.search(r"iphone|pixel|galaxy", label, re.I) else "computer"))
                added += 1
                print(f"  {person:8} <- {label} ({c['mac']})")
                break
    print(f"seeded {added} device->person mappings")
    conn.close()


if __name__ == "__main__":
    sys.exit(seed() if "--seed" in sys.argv else main())
