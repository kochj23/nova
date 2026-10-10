#!/usr/bin/env python3
"""nova_tracker_watch.py — unwanted-tracker detection from raw BLE.

WHAT THIS CAN AND CANNOT DO, up front:

CAN: tell that a Find My network tracker is nearby, and whether it is WITH ITS OWNER or
SEPARATED from them. Apple's manufacturer data subtype 0x12 carries a short payload when the
owner's device is present and a long one (with a rotating public key) when the tag is
separated and begging passing iPhones to relay its position. A separated tag that keeps
turning up near you, hour after hour, is the unwanted-tracker case — this is the same signal
Apple's own Item Safety Alerts use.

CANNOT: tell you WHICH tag. The identifiers rotate roughly every 15 minutes and the keys are
derived from a secret only the owner's iCloud account holds. That is deliberate privacy
design, and it is also why "is this one of mine?" is unanswerable from the air. There is no
Find My API to ask — Apple publishes none, and the local Find My cache on macOS is encrypted.

So the honest question this answers is not "whose tag is that" but "has a tag that is NOT with
its owner been sitting near us for an unusual length of time". For a household that owns
several AirTags, your own tags normally show as owner_nearby while you are home; a persistent
SEPARATED tag is the anomaly worth a look.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")


def log(m):
    print(f"[tracker-watch] {m}", flush=True)


def main(hours, min_hours_present, alert):
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()

    # Current picture
    cur.execute(f"""
        SELECT metadata->>'findmy_state', count(*), count(DISTINCT device_mac)
        FROM telemetry.bluetooth
        WHERE ts > now() - interval '{hours} hours'
          AND metadata->>'apple_subtype' = 'findmy'
        GROUP BY 1""")
    state = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    for k, (n, macs) in sorted(state.items(), key=lambda x: str(x[0])):
        log(f"{str(k or 'unclassified'):14} sightings={n:<6} distinct MACs={macs}")

    # Persistence: how many distinct HOURS did separated trackers appear in? Because the
    # address rotates ~15min, an individual tag cannot be followed across a day — so measure
    # the POPULATION's continuity instead. A tag that leaves with its owner produces a short
    # burst; one riding in your car produces an unbroken run of hours.
    cur.execute(f"""
        SELECT date_trunc('hour', ts) AS hr,
               count(DISTINCT device_mac) AS separated_macs,
               max(rssi) AS strongest
        FROM telemetry.bluetooth
        WHERE ts > now() - interval '{hours} hours'
          AND metadata->>'findmy_state' = 'separated'
        GROUP BY 1 ORDER BY 1""")
    rows = cur.fetchall()
    hours_present = len(rows)
    log(f"separated trackers seen in {hours_present} of the last {hours} hours")
    for hr, macs, rssi in rows[-12:]:
        bar = "#" * min(macs, 30)
        log(f"  {hr:%m-%d %H:00}  macs={macs:<3} strongest={rssi if rssi is not None else '?':>4}dBm {bar}")

    if hours_present >= min_hours_present:
        msg = (f"A Find My tracker separated from its owner has been detected near the house in "
               f"{hours_present} of the last {hours} hours. Own tags normally report "
               f"'owner_nearby' while you are home. Worth checking bags, coats and the car — "
               f"or confirming one of yours is simply out of range of its paired phone.")
        log("ALERT: " + msg)
        if alert:
            try:
                from nova_notify import notify
                notify(f"Unwanted tracker watch: {msg}", level="warning", category="security")
                log("alerted")
            except Exception as e:
                log(f"notify failed: {e}")
    else:
        log(f"below alert threshold ({min_hours_present}h) — nothing to report")

    conn.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--min-hours-present", type=int, default=6,
                    help="separated tags seen in at least this many distinct hours -> alert")
    ap.add_argument("--alert", action="store_true", help="send a notification when triggered")
    a = ap.parse_args()
    sys.exit(main(a.hours, a.min_hours_present, a.alert))
