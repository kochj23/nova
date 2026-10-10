#!/usr/bin/env python3
"""
nova_homekit_outlets.py — ingest HomeKit "Outlet In Use" signals (#682).

24 Eve Energy Strip sockets across 5 rooms expose an "Outlet In Use"
characteristic but NOT wattage. "Outlet In Use" is a poor-man's usage signal
(is something actually drawing on this socket). This polls the NovaHomeKit app's
local HTTP API, lands every Outlet service into telemetry.homekit_outlets, and
(optionally) raises a "left on" alert for watch-listed sockets that have stayed
on past a threshold.

Source: NovaHomeKit.app  ->  http://127.0.0.1:37433/api/accessories

NOTE on coverage: NovaHomeKit reliably reads each socket's Power State (relay
on/off) but currently only enumerates the "Outlet In Use" characteristic without
reading its value (HomeKit lazy-reads; 1/24 populate today). Full per-socket
In Use coverage needs a one-line NovaHomeKit rebuild that calls readValue on the
Outlet In Use characteristic (Jordan / #630). This ingester already stores in_use
as a nullable column, so every socket's In Use lands automatically the moment the
app is rebuilt — no change here required.

Usage:
  nova_homekit_outlets.py            # poll once, ingest
  nova_homekit_outlets.py --alert    # also evaluate left-on alerts
  nova_homekit_outlets.py --daemon   # poll every POLL_INTERVAL forever
"""
import argparse
import json
import os
import sys
import time
import urllib.request

import psycopg2

import nova_homekit_client as hk  # Bearer token (NovaHomeKit 51e7a91)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from nova_notify import notify
except Exception:  # notify is best-effort
    def notify(*a, **k):
        return False

HK_URL = "http://127.0.0.1:37433/api/accessories"
import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
OUTLET_SERVICE_TYPE = "00000047-0000-1000-8000-0026BB765291"  # HMServiceTypeOutlet
POLL_INTERVAL = 300

# Left-on alerts (optional, OFF by default — empty watchlist = no alerts).
# Map a socket name -> max continuous ON hours before alerting. Populate as
# desired, e.g. {"Iron": 1.0, "Space Heater": 2.0}. Uses in_use when available,
# else power_state. Alerts are deduped per socket+day.
LEFT_ON_WATCHLIST = {}


def fetch():
    req = urllib.request.Request(HK_URL, headers=hk.auth_headers())
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _tb(v):
    """HomeKit characteristic values arrive as bool OR 0/1 int — normalize to bool/None."""
    return None if v is None else bool(v)


def outlets_from(accessories):
    """Yield (room, accessory, outlet, power_state, in_use, status_active) per Outlet service."""
    for acc in accessories:
        room = acc.get("room")
        acc_name = acc.get("name")
        for svc in acc.get("services", []):
            if svc.get("type") != OUTLET_SERVICE_TYPE:
                continue
            ch = {c.get("type"): c for c in svc.get("characteristics", [])}
            yield (
                room,
                acc_name,
                svc.get("name") or acc_name,
                _tb(ch.get("Power State", {}).get("value")),
                _tb(ch.get("Outlet In Use", {}).get("value")),
                _tb(ch.get("Status Active", {}).get("value")),
            )


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telemetry.homekit_outlets (
            ts            timestamptz NOT NULL DEFAULT now(),
            uid           text NOT NULL,
            room          text,
            accessory     text,
            outlet        text,
            power_state   boolean,
            in_use        boolean,
            status_active boolean,
            PRIMARY KEY (ts, uid)
        );
        CREATE INDEX IF NOT EXISTS idx_hk_outlets_uid_ts
            ON telemetry.homekit_outlets (uid, ts DESC);
    """)


def ingest(cur, rows):
    n = 0
    for room, acc, outlet, power, inuse, active in rows:
        uid = f"{room}|{acc}|{outlet}"
        cur.execute(
            """INSERT INTO telemetry.homekit_outlets
                   (uid, room, accessory, outlet, power_state, in_use, status_active)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ts, uid) DO NOTHING""",
            (uid, room, acc, outlet, power, inuse, active))
        n += 1
    return n


def left_on_alerts(cur):
    """For watch-listed sockets, alert if continuously on past the threshold."""
    if not LEFT_ON_WATCHLIST:
        return 0
    fired = 0
    for outlet, max_hours in LEFT_ON_WATCHLIST.items():
        # latest sample for this socket
        cur.execute(
            """SELECT uid, room, COALESCE(in_use, power_state) AS on_now
               FROM telemetry.homekit_outlets
               WHERE outlet = %s ORDER BY ts DESC LIMIT 1""", (outlet,))
        row = cur.fetchone()
        if not row or not row[2]:
            continue
        uid, room, _ = row
        # earliest ts in the current uninterrupted ON streak
        cur.execute(
            """SELECT min(ts) FROM (
                   SELECT ts, COALESCE(in_use, power_state) AS on_now,
                          bool_and(COALESCE(in_use, power_state)) OVER (ORDER BY ts DESC) AS streak
                   FROM telemetry.homekit_outlets WHERE uid = %s
                   ORDER BY ts DESC LIMIT 500
               ) s WHERE streak""", (uid,))
        since = cur.fetchone()[0]
        if not since:
            continue
        on_hours = (time.time() - since.timestamp()) / 3600.0
        if on_hours >= max_hours:
            notify(
                f"Outlet left on: {outlet} ({room})",
                body=f"{outlet} in {room} has been on for {on_hours:.1f}h "
                     f"(threshold {max_hours}h).",
                level="warning", category="home",
                dedup_key=f"hk_left_on:{uid}:{int(time.time() // 86400)}")
            fired += 1
    return fired


def run_once(do_alert):
    accessories = fetch()
    rows = list(outlets_from(accessories))
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    ensure_table(cur)
    conn.commit()
    n = ingest(cur, rows)
    conn.commit()
    have_inuse = sum(1 for r in rows if r[4] is not None)
    fired = left_on_alerts(cur) if do_alert else 0
    conn.commit()
    conn.close()
    print(f"[hk-outlets] ingested {n} sockets ({have_inuse} with In Use value), "
          f"{fired} left-on alert(s)", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ingest HomeKit Outlet In Use signals (#682)")
    ap.add_argument("--alert", action="store_true", help="evaluate left-on alerts")
    ap.add_argument("--daemon", action="store_true", help="poll forever")
    args = ap.parse_args(argv)
    if args.daemon:
        while True:
            try:
                run_once(args.alert)
            except Exception as e:
                print(f"[hk-outlets] error: {e}", flush=True)
            time.sleep(POLL_INTERVAL)
    else:
        run_once(args.alert)


if __name__ == "__main__":
    main()
