#!/usr/bin/env python3
"""
nova_soil_monitor.py — alert when soil moisture drops too low.

Sensors (Ambient Weather WH51-style, relative %):
  soil1 = First raised bed   (tomatoes/chilies/basil/strawberries)
  soil2 = Second raised bed  (same veggie mix)
  soil3 = Patio potted plant

Veggie beds want 40-70%; alert below LOW, critical below CRIT.
Runs from launchd every 30 min; nova_notify dedup keeps it from spamming.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2

sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
import nova_notify

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
SOURCE = "nova-soil-monitor"

# sensor -> (label, low_pct, crit_pct)
SENSORS = {
    "soil1": ("First raised bed", 35, 25),
    "soil2": ("Second raised bed", 35, 25),
    "soil3": ("Patio potted plant", 30, 20),
}
STALE_HOURS = 2  # no reading in this window -> sensor offline alert


def check(now=None):
    now = now or datetime.now(timezone.utc)
    issues = []
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (sensor) sensor, moisture_pct, ts "
            "FROM telemetry.soil ORDER BY sensor, ts DESC")
        latest = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    conn.close()

    for sensor, (label, low, crit) in SENSORS.items():
        if sensor not in latest:
            issues.append((sensor, f"{label}: no readings ever", "warning"))
            continue
        pct, ts = latest[sensor]
        if now - ts > timedelta(hours=STALE_HOURS):
            issues.append((sensor, f"{label}: no reading since {ts:%m-%d %H:%M}", "warning"))
        elif pct <= crit:
            issues.append((sensor, f"{label}: soil at {pct}% — water NOW (critical <{crit}%)", "critical"))
        elif pct <= low:
            issues.append((sensor, f"{label}: soil at {pct}% — needs water soon (low <{low}%)", "warning"))
    return issues


def main():
    issues = check()
    for sensor, msg, level in issues:
        nova_notify.notify(
            title=f"Soil moisture: {SENSORS.get(sensor, (sensor,))[0]}",
            body=msg, level=level, category="garden", source=SOURCE,
            dedup_key=f"soil-low:{sensor}")
        print(f"[{level}] {msg}")
    if not issues:
        print("all soil sensors healthy")
    return 0


def _selfcheck():
    # ponytail: threshold logic check against a fake 'now' far from any DB state
    fake = {"soil1": (50, None), "soil2": (30, None), "soil3": (2, None)}
    def classify(sensor, pct):
        label, low, crit = SENSORS[sensor]
        return "critical" if pct <= crit else "warning" if pct <= low else "ok"
    assert classify("soil1", 50) == "ok"
    assert classify("soil2", 30) == "warning"
    assert classify("soil3", 2) == "critical"
    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        sys.exit(main())
