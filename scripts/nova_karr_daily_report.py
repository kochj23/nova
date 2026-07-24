#!/usr/bin/env python3
"""
nova_karr_daily_report.py — Daily digest of vulnerable-BLE-device watchlist
detections (see nova_ble_monitor.py's VULNERABLE_BLE_WATCHLIST / check_watchlist_devices()).

Passive BLE name-match only. Detections are labeled with a confidence level --
see the watchlist entry's own notes in nova_ble_monitor.py for how each
pattern was derived; treat as PROBABLE unless stated otherwise.

Persistence classification (from vulnerable_ble_sightings, every raw sighting
not just first-alert): a device seen on 2+ distinct days is probably a
resident vehicle nearby -- worth actually flagging to a person. A device
seen once is more likely a passerby.

Runs daily via scheduler. Writes to shared_observations (feeds the shared
"one voice" context every ops-article script already reads) and posts to
Slack via nova_notify.

Written by Jordan Koch (via Claude).
"""

import sys
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
LOG_FILE = Path.home() / ".openclaw/logs/karr_daily_report.log"


def log(msg):
    line = f"[karr_report {date.today().isoformat()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run():
    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT observed_at, subject, observation, metadata
        FROM shared_observations
        WHERE observer = 'nova' AND category = 'security'
          AND metadata ? 'confidence'
          AND observed_at > now() - interval '1 day'
        ORDER BY observed_at DESC
    """)
    hits = cur.fetchall()

    persistence = {}
    if hits:
        macs = list({(h["metadata"] or {}).get("mac") for h in hits if (h["metadata"] or {}).get("mac")})
        cur.execute("""
            SELECT device_mac, count(DISTINCT date(ts)) AS days_seen, min(ts) AS first_seen
            FROM vulnerable_ble_sightings
            WHERE device_mac = ANY(%s)
            GROUP BY device_mac
        """, (macs,))
        for r in cur.fetchall():
            persistence[r["device_mac"]] = r

    cur.close()
    conn.close()

    log(f"detections={len(hits)}")

    if not hits:
        try:
            notify("Vulnerable BLE Watchlist Report",
                   body="No watchlist matches (KARR/SWDS or otherwise) detected in the last 24h.",
                   level="info", category="security", dedup_key="karr-daily-report")
        except Exception as e:
            log(f"Notify failed: {e}")
        return

    lines = [f"{len(hits)} watchlist detection(s) in the last 24h -- already paged individually "
             f"to #nova-critical at detection time, this is just the rollup:"]
    for h in hits:
        meta = h["metadata"] or {}
        mac = meta.get("mac", "?")
        rssi = meta.get("rssi", "?")
        p = persistence.get(mac)
        if p and p["days_seen"] >= 2:
            tag = f"RECURRING, {p['days_seen']} distinct days, first seen {p['first_seen'].strftime('%m-%d')} -- probably a resident vehicle"
        else:
            tag = "seen once so far -- probably a passerby"
        lines.append(f"  {h['observed_at'].strftime('%H:%M')} — {h['subject']} — {mac} RSSI={rssi} — {tag}")

    try:
        notify(
            f"Vulnerable BLE Watchlist Report ({date.today().isoformat()})",
            body="\n".join(lines),
            level="info",
            category="security",
            dedup_key="karr-daily-report",
        )
    except Exception as e:
        log(f"Notify failed: {e}")

    log("Report complete")


if __name__ == "__main__":
    run()
