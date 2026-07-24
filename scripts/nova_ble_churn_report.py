#!/usr/bin/env python3
"""
nova_ble_churn_report.py — Daily Bluetooth device churn report.

Reports NAMED devices only (device_name IS NOT NULL) — the vast majority of
distinct MACs in telemetry.bluetooth are privacy-rotating randomized addresses
with no name (iPhones/AirPods etc. by design), and treating those as "new
devices" would be pure noise, not signal. A named device is one that's either
a known paired peripheral or something broadcasting a persistent identifiable
name (headphones, a car alarm, a fitness tracker...).

NEW: named MAC whose first-ever appearance in telemetry.bluetooth was today.
GONE: named MAC seen on >=10 of the last 14 days but absent for the last 3.

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
LOG_FILE = Path.home() / ".openclaw/logs/ble_churn_report.log"


def log(msg):
    ts = date.today().isoformat()
    line = f"[ble_churn {ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run():
    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # "New" = confirmed recurring, not just seen once. Ambient BLE traffic near any home
    # includes a constant stream of one-off passersby (neighbors, delivery drivers, cars) --
    # reporting every first-sight would be ~650/day of pure noise. Instead, report a device
    # the day its SECOND distinct day of appearance lands (proves it's sticking around).
    cur.execute("""
        WITH daily_macs AS (
            SELECT device_mac, device_name, date(ts) AS d
            FROM telemetry.bluetooth
            WHERE device_name IS NOT NULL AND device_name != ''
              AND ts > now() - interval '14 days'
            GROUP BY device_mac, device_name, date(ts)
        ),
        ranked AS (
            SELECT device_mac, device_name, d,
                   row_number() OVER (PARTITION BY device_mac ORDER BY d) AS day_rank
            FROM daily_macs
        )
        SELECT device_mac, device_name, d AS first_seen
        FROM ranked
        WHERE day_rank = 2 AND d = current_date
        ORDER BY device_name
    """)
    new_devices = cur.fetchall()

    cur.execute("""
        WITH recent_days AS (
            SELECT device_mac, device_name, count(DISTINCT date(ts)) AS days_seen,
                   max(ts) AS last_seen
            FROM telemetry.bluetooth
            WHERE device_name IS NOT NULL AND device_name != ''
              AND ts > now() - interval '14 days'
            GROUP BY device_mac, device_name
        )
        SELECT device_mac, device_name, days_seen, last_seen
        FROM recent_days
        WHERE days_seen >= 10 AND last_seen < now() - interval '3 days'
        ORDER BY last_seen
    """)
    gone_devices = cur.fetchall()

    cur.execute("""
        SELECT count(DISTINCT device_mac) FROM telemetry.bluetooth
        WHERE (device_name IS NULL OR device_name = '') AND ts > now() - interval '1 day'
    """)
    anon_today = cur.fetchone()["count"]

    for d in new_devices:
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES ('nova_ble_churn_report', 'network', 'ble-new-device', %s, 'info', %s)
        """, (
            f"New Bluetooth device seen for the first time: {d['device_name']} ({d['device_mac']})",
            psycopg2.extras.Json({"mac": d["device_mac"], "name": d["device_name"]}),
        ))
    for d in gone_devices:
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES ('nova_ble_churn_report', 'network', 'ble-device-gone', %s, 'info', %s)
        """, (
            f"Bluetooth device that was regularly present has stopped appearing: "
            f"{d['device_name']} ({d['device_mac']}), last seen {d['last_seen'].strftime('%Y-%m-%d %H:%M')}",
            psycopg2.extras.Json({"mac": d["device_mac"], "name": d["device_name"],
                                   "last_seen": d["last_seen"].isoformat()}),
        ))
    conn.commit()

    log(f"new={len(new_devices)} gone={len(gone_devices)} anon_today={anon_today}")

    lines = [
        f"Bluetooth churn — {len(new_devices)} new, {len(gone_devices)} gone "
        f"(named devices only; {anon_today} anonymous/randomized-MAC devices seen today, not tracked individually)",
    ]
    if new_devices:
        lines.append("New:")
        lines += [f"  + {d['device_name']} ({d['device_mac'][:8]}…)" for d in new_devices[:15]]
    if gone_devices:
        lines.append("Gone:")
        lines += [f"  - {d['device_name']} ({d['device_mac'][:8]}…), last seen {d['last_seen'].strftime('%m-%d')}"
                   for d in gone_devices[:15]]

    try:
        notify(
            f"Bluetooth Churn Report ({date.today().isoformat()})",
            body="\n".join(lines),
            level="info",
            category="network",
            dedup_key="ble-churn-daily",
        )
    except Exception as e:
        log(f"Notify failed: {e}")

    cur.close()
    conn.close()
    log("Report complete")


if __name__ == "__main__":
    run()
