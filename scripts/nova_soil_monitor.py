#!/usr/bin/env python3
"""
nova_soil_monitor.py — alert when soil moisture drops too low.

Sensors (Ambient Weather WH51-style, relative %):
  soil1 = First raised bed   (tomatoes/chilies/basil/strawberries)
  soil2 = Second raised bed  (same veggie mix)
  soil3 = Patio potted plant

Veggie beds want 40-70%; alert below LOW, critical below CRIT.
Runs from launchd every 30 min.

NOISE CONTROL (signalnoise): the old code emitted an event every cycle while a
sensor stayed dry — the notifier's 1h dedup still let it re-fire ~once/hour
(soil2 ~22x/day, soil1 ~16x/day). We now DEBOUNCE at the source with a small
per-sensor state machine in public.soil_alert_state (nova_ops db, created
idempotently, crash-safe):
  * alert ONCE when a sensor crosses into a problem state (ok -> low/crit/stale),
  * do NOT re-alert while it stays in that same state,
  * a still-CRITICAL sensor gets at most ONE reminder per DAILY_REMINDER_H,
  * an escalation (warning -> critical) alerts again,
  * once it RECOVERS (back to ok), the state resets so the next dry spell alerts.
Detection + thresholds are unchanged; only the re-alert cadence changed.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2

sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
import nova_notify

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
SOURCE = "nova-soil-monitor"

# sensor -> (label, low_pct, crit_pct)
SENSORS = {
    "soil1": ("First raised bed", 35, 25),
    "soil2": ("Second raised bed", 35, 25),
    "soil3": ("Patio potted plant", 30, 20),
}
STALE_HOURS = 2        # no reading in this window -> sensor offline/stale
DAILY_REMINDER_H = 24  # a still-critical sensor may remind at most this often
PROBLEM_STATES = {"warning", "critical", "stale", "missing"}


def check(now=None):
    """Classify each sensor -> (sensor, state, msg, level). state drives debounce."""
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
            issues.append((sensor, "missing", f"{label}: no readings ever", "warning", None))
            continue
        pct, ts = latest[sensor]
        if now - ts > timedelta(hours=STALE_HOURS):
            issues.append((sensor, "stale", f"{label}: no reading since {ts:%m-%d %H:%M}", "warning", pct))
        elif pct <= crit:
            issues.append((sensor, "critical", f"{label}: soil at {pct}% — water NOW (critical <{crit}%)", "critical", pct))
        elif pct <= low:
            issues.append((sensor, "warning", f"{label}: soil at {pct}% — needs water soon (low <{low}%)", "warning", pct))
        else:
            issues.append((sensor, "ok", f"{label}: soil at {pct}% — healthy", "info", pct))
    return issues


def _ensure_state_table(cur):
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.soil_alert_state ("
        " sensor text PRIMARY KEY, state text NOT NULL,"
        " last_alerted_ts timestamptz, last_pct real,"
        " updated_at timestamptz NOT NULL DEFAULT now())")


def _should_alert(prev_state, prev_alert_ts, cur_state, now):
    """Debounce: alert only on transition into a problem, escalation, or a due
    daily reminder for a sustained critical. Returns (alert?, reason)."""
    if cur_state not in PROBLEM_STATES:
        return False, "ok"
    if prev_state not in PROBLEM_STATES:
        return True, "new"                       # ok/recovered -> problem
    if prev_state != cur_state:
        # escalation (warning -> critical) is worth a fresh ping; a de-escalation
        # (critical -> warning) is still dry, stay quiet until recovery.
        if prev_state == "warning" and cur_state == "critical":
            return True, "escalation"
        return False, "same-problem"
    if cur_state == "critical":                  # sustained critical -> daily reminder
        if prev_alert_ts is None or (now - prev_alert_ts) >= timedelta(hours=DAILY_REMINDER_H):
            return True, "daily-reminder"
    return False, "debounced"


def main(now=None):
    now = now or datetime.now(timezone.utc)
    issues = check(now)
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    _ensure_state_table(cur)

    cur.execute("SELECT sensor, state, last_alerted_ts FROM public.soil_alert_state")
    prev = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    sent = 0
    for sensor, state, msg, level, pct in issues:
        prev_state, prev_alert_ts = prev.get(sensor, (None, None))
        alert, reason = _should_alert(prev_state, prev_alert_ts, state, now)
        if alert:
            nova_notify.notify(
                title=f"Soil moisture: {SENSORS.get(sensor, (sensor,))[0]}",
                body=msg, level=level, category="garden", source=SOURCE,
                dedup_key=f"soil-low:{sensor}")
            sent += 1
            print(f"[{level}] {msg}  ({reason})")
        else:
            print(f"[skip] {sensor} {state} — {reason}")
        # persist current state; bump last_alerted_ts only when we actually alerted
        cur.execute(
            "INSERT INTO public.soil_alert_state (sensor,state,last_alerted_ts,last_pct,updated_at) "
            "VALUES (%s,%s,%s,%s,now()) ON CONFLICT (sensor) DO UPDATE SET "
            "state=EXCLUDED.state, last_pct=EXCLUDED.last_pct, updated_at=now(), "
            "last_alerted_ts=CASE WHEN %s THEN EXCLUDED.last_alerted_ts "
            "ELSE public.soil_alert_state.last_alerted_ts END",
            (sensor, state, now if alert else None, pct, alert))
    conn.close()
    if sent == 0:
        print("no new soil alerts (all healthy or debounced)")
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

    # debounce logic: alert on entry, stay quiet while dry, remind only daily
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _should_alert(None, None, "warning", t0)[0] is True          # fresh dry spell
    assert _should_alert("warning", t0, "warning", t0 + timedelta(minutes=30))[0] is False  # still dry
    assert _should_alert("warning", t0, "critical", t0 + timedelta(minutes=30))[0] is True  # escalation
    assert _should_alert("critical", t0, "critical", t0 + timedelta(hours=1))[0] is False   # sustained, <24h
    assert _should_alert("critical", t0, "critical", t0 + timedelta(hours=25))[0] is True   # daily reminder
    assert _should_alert("warning", t0, "ok", t0 + timedelta(hours=1))[0] is False          # recovery, quiet
    assert _should_alert("ok", None, "warning", t0 + timedelta(hours=2))[0] is True         # re-low after recovery
    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        sys.exit(main())
