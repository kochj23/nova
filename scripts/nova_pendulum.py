#!/usr/bin/env python3
"""nova_pendulum.py — THE PENDULUM: how far the blade still has to fall.

From Poe's "The Pit and the Pendulum": the prisoner lies strapped beneath a razor-edged
pendulum that sweeps and descends a little with every swing. The slowness is the horror, and
also the chance: it leaves time to think. He smears the straps with the meat left for him, the
rats gnaw through them, and he slides free before the blade arrives. Matheson's Scott Carey
shrinks a seventh of an inch a day while the house stays the same size. Matheson's "Steel" turns
on a robot boxer whose broken part is no longer made. Shelley's Winzy, in "The Mortal Immortal",
drank only half the alchemist's elixir; at 323 years old he has buried Bertha and still asks
"Am I immortal?", unsure whether the draught gave him eternity or only longevity.

Nova's version reports blade height: projected days until each slow, predictable failure, with
an interval, an acceleration check, and a flag when the impact falls inside the lead time the
fix needs. It never pages; it hands one line to the Watch Bill.

Minimal first version. It reuses what already exists and adds only what was missing:
  * disk   Studio and nova-core root, and the NAS volumes. Series are the used_pct history that
           nova_disk_forecast.py already writes to telemetry.disk_forecast; that collector fits
           plain 30-day OLS, so here the fit is Theil-Sen on 14 daily medians (robust to a bulk
           ingest), with an interquartile slope interval and a recent-vs-earlier acceleration
           check. disk_forecast's own days_until_full is kept in the note for comparison.
  * pg     total size of every database on the PG primary, recorded here each run (nothing else
           keeps that history), against free space on the primary's host (capacity_snapshots).
  * cert   TLS expiry from telemetry.cert_expiry (nova_cert_monitor.py collects it;
           nova_cert_watch.py already alarms at 14 d). Calendared, so no fit.

CLI:      --run [--dry-run]   --line   --show   --selftest
Table:    pendulum_blades
Config:   service_config pendulum/disks      [["node_status","mac-studio"], ["storage_metrics", null], ...]
          service_config pendulum/lead_days  {"disk": 30, "pg": 30, "cert": 21}
Schedule: every 6 h: `nova_pendulum.py --run`; the weekly Watch Bill reads `--line` / watch_bill_line().
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import socket
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

TAG = "pendulum"
DISKS = [["node_status", "mac-studio"], ["node_status", "nova-core"], ["storage_metrics", None]]
LEAD_DAYS = {"disk": 30, "pg": 30, "cert": 21}
FIT_DAYS = 14
MIN_DAYS = 4
MAX_DAYS = 3650.0   # beyond ten years the blade is not falling

SCHEMA = """
CREATE TABLE IF NOT EXISTS pendulum_blades (
  ts timestamptz NOT NULL DEFAULT now(),
  blade text NOT NULL,
  kind text NOT NULL,
  value double precision,
  unit text,
  days real,
  days_lo real,
  days_hi real,
  accelerating boolean NOT NULL DEFAULT false,
  inside_lead boolean NOT NULL DEFAULT false,
  note text);
CREATE INDEX IF NOT EXISTS pendulum_blades_blade_ts ON pendulum_blades (blade, ts);
"""


def log(m: str) -> None:
    W.log(TAG, m)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {str(e).splitlines()[0]}")
        return []


# ── fits (pure) ─────────────────────────────────────────────────────────────

def daily(points: list) -> list:
    """[(ts, value)] -> [(day_index, median value)] oldest first."""
    by: dict = {}
    for ts, v in points:
        if v is not None:
            by.setdefault(ts.date(), []).append(float(v))
    if not by:
        return []
    d0 = min(by)
    return [((d - d0).days, statistics.median(v)) for d, v in sorted(by.items())]


def theil_sen(pts: list) -> tuple:
    """(median slope, q1 slope, q3 slope) per day, or (None,)*3 with < 2 points."""
    s = sorted((y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(pts) for x2, y2 in pts[i + 1:] if x2 != x1)
    if not s:
        return None, None, None
    q = statistics.quantiles(s, n=4) if len(s) > 1 else [s[0]] * 3
    return statistics.median(s), q[0], q[2]


def _days(room: float, slope) -> float | None:
    if slope is None or slope <= 0:
        return None
    return min(max(room, 0.0) / slope, MAX_DAYS)


def blade(pts: list, room: float) -> dict:
    """pts = daily [(x, y)], room = distance left to impact in y units."""
    if len(pts) < MIN_DAYS:
        return {"days": None, "days_lo": None, "days_hi": None, "accelerating": False,
                "note": f"learning: {len(pts)} of {MIN_DAYS} days"}
    m, lo, hi = theil_sen(pts)
    half = len(pts) // 2
    old, new = theil_sen(pts[:half + 1])[0], theil_sen(pts[half:])[0]
    accel = bool(new and new > 0 and new > 1.5 * max(old or 0.0, 1e-9))
    days = _days(room, m)
    return {"days": days, "days_lo": _days(room, hi), "days_hi": _days(room, lo), "accelerating": accel,
            "note": None if days is not None else "flat or shrinking"}


def flag_lead(b: dict, lead: dict) -> dict:
    b["inside_lead"] = b.get("days") is not None and b["days"] <= lead.get(b["kind"], 30)
    return b


def _range(b: dict) -> str:
    """Interquartile range of days; an open top means some fits say the blade is not falling."""
    if b.get("days_lo") is None:
        return ""
    return f" ({b['days_lo']:.0f}-{b['days_hi']:.0f})" if b.get("days_hi") is not None else f" (>= {b['days_lo']:.0f})"


def watch_bill_line(blades: list, lead: dict | None = None) -> str:
    """One Watch Bill line for the nearest blade."""
    lead = lead or LEAD_DAYS
    live = sorted((b for b in blades if b.get("days") is not None), key=lambda b: b["days"])
    if not live:
        return "Pendulum: no blade is falling."
    b = live[0]
    rng = _range(b)
    extra = "".join([", accelerating" if b.get("accelerating") else "",
                     f", inside its {lead.get(b['kind'], 30)}-day lead" if b.get("inside_lead") else ""])
    return f"Pendulum: nearest blade is {b['blade']} ({b['kind']}), about {b['days']:.0f} days{rng}{extra}."


# ── blades from PG ──────────────────────────────────────────────────────────

def names(cur) -> dict:
    return {str(ip): n for ip, n in _q(cur, "SELECT DISTINCT ip::text, name FROM telemetry.net_inventory "
                                            "WHERE name IS NOT NULL")}


def disk_blades(cur, disks: list) -> list:
    since = W.now_utc() - timedelta(days=FIT_DAYS)
    rows = _q(cur, "SELECT source, host, volume, ts, used_pct, target_pct, days_until_full FROM telemetry.disk_forecast "
                   "WHERE source = ANY(%s) AND ts >= %s ORDER BY ts", (sorted({d[0] for d in disks}), since))
    want = {(s, h) for s, h in disks}
    series: dict = {}
    for src, host, vol, ts, pct, target, duf in rows:
        if (src, host) in want or (src, None) in want:
            s = series.setdefault((src, host, vol), {"pts": [], "target": 95.0, "duf": None})
            s["pts"].append((ts, pct))
            s["target"], s["duf"] = float(target or 95.0), duf
    nm = names(cur)
    out = []
    for (src, host, vol), s in sorted(series.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        pts = daily(s["pts"])
        cur_pct = float(s["pts"][-1][1] or 0.0)
        b = blade(pts, s["target"] - cur_pct)
        b.update(blade=f"{nm.get(host, host)}:{vol}", kind="disk", value=cur_pct, unit="pct_used")
        b["note"] = "; ".join(x for x in (b["note"], f"disk_forecast OLS says {s['duf']:.0f} d"
                                          if s["duf"] is not None else None) if x) or None
        out.append(b)
    return out


def pg_blade(cur, prior: list, dsn_host: str | None = None) -> dict | None:
    """prior = [(ts, bytes)] from earlier runs. Impact = free bytes on the primary's host. The
    primary may sit behind a loopback shim, so a 127.x server address falls back to the DSN host."""
    r = _q(cur, "SELECT sum(pg_database_size(datname))::float8, host(inet_server_addr()) FROM pg_database")
    if not r or r[0][0] is None:
        return None
    size, ip = r[0]
    if dsn_host and (not ip or ip.startswith("127.")):
        try:
            ip = socket.gethostbyname(dsn_host)
        except OSError as e:
            log(f"cannot resolve {dsn_host}: {e}")
    free = _q(cur, "SELECT device_name, (SELECT max((d->>'avail_gb')::float8) FROM jsonb_array_elements(disks) d "
                   "WHERE d->>'mount' = '/') FROM capacity_snapshots WHERE host(device_ip) = %s "
                   "ORDER BY ts DESC LIMIT 1", (ip or "",))
    pts = daily(prior + [(W.now_utc(), size)])
    host, avail_gb = (free[0] if free else (ip, None))
    if avail_gb is None:
        b = {"days": None, "days_lo": None, "days_hi": None, "accelerating": False,
             "note": "primary host free space unknown"}
    else:
        b = blade([(x, y / 1e9) for x, y in pts], float(avail_gb))   # ponytail: assumes PG data lives on '/'
    b.update(blade=f"pg:{host}", kind="pg", value=size, unit="bytes")
    return b


def cert_blades(cur) -> list:
    return [{"blade": f"cert:{ep}", "kind": "cert", "value": d, "unit": "days", "days": max(float(d), 0.0),
             "days_lo": None, "days_hi": None, "accelerating": False, "note": None}
            for ep, d in _q(cur, "SELECT DISTINCT ON (endpoint) endpoint, days_until_expiry FROM telemetry.cert_expiry "
                                 "WHERE ts > now() - interval '2 days' AND days_until_expiry IS NOT NULL "
                                 "ORDER BY endpoint, ts DESC")]


def prior_pg(cur) -> list:
    exists = _q(cur, "SELECT to_regclass('pendulum_blades')")
    if not exists or exists[0][0] is None:
        return []
    return [(ts, v) for ts, v in _q(cur, "SELECT ts, value FROM pendulum_blades WHERE kind='pg' AND ts >= %s "
                                         "ORDER BY ts", (W.now_utc() - timedelta(days=FIT_DAYS),))]


def config(cur):
    try:
        return (W.get_config(cur, "pendulum", "disks", DISKS), W.get_config(cur, "pendulum", "lead_days", LEAD_DAYS))
    except Exception as e:  # noqa: BLE001
        log(f"config read failed, using defaults: {e}")
        return DISKS, LEAD_DAYS


def write(cur, ts, blades: list) -> None:
    ensure_schema(cur)
    for b in blades:
        cur.execute("INSERT INTO pendulum_blades (ts, blade, kind, value, unit, days, days_lo, days_hi, accelerating, "
                    "inside_lead, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (ts, b["blade"], b["kind"], b["value"], b["unit"], b["days"], b["days_lo"], b["days_hi"],
                     b["accelerating"], b["inside_lead"], b["note"]))


def _fmt(b: dict) -> str:
    d = "-" if b["days"] is None else f"{b['days']:.0f}"
    rng = _range(b)
    marks = ("ACCEL " if b["accelerating"] else "") + ("LEAD " if b["inside_lead"] else "")
    return f"  {b['kind']:<5} {d:>6}d{rng:<14} {marks:<11} {b['blade']}" + (f"  [{b['note']}]" if b["note"] else "")


def run(dry: bool = False) -> list:
    conn = W.connect()
    try:
        cur = conn.cursor()
        disks, lead = config(cur)
        blades = disk_blades(cur, disks) + cert_blades(cur)
        pg = pg_blade(cur, prior_pg(cur), getattr(getattr(conn, "info", None), "host", None))
        blades += [pg] if pg else []
        for b in blades:
            flag_lead(b, lead)
        blades.sort(key=lambda b: (b["days"] is None, b["days"] or 0))
        log(f"{'DRY RUN ' if dry else ''}{len(blades)} blades; {sum(b['inside_lead'] for b in blades)} inside lead")
        for b in blades:
            print(_fmt(b))
        print(watch_bill_line(blades, lead))
        if not dry:
            write(cur, datetime.now(timezone.utc), blades)
            log(f"wrote {len(blades)} rows")
        return blades
    finally:
        conn.close()


def latest(cur) -> list:
    cols = ("blade", "kind", "value", "unit", "days", "days_lo", "days_hi", "accelerating", "inside_lead", "note")
    rows = _q(cur, "SELECT " + ", ".join(cols) + " FROM pendulum_blades WHERE ts = (SELECT max(ts) FROM pendulum_blades)")
    return [dict(zip(cols, r)) for r in rows]


def show(line_only: bool = False) -> int:
    conn = W.connect()
    try:
        cur = conn.cursor()
        blades = latest(cur)
        if not line_only:
            for b in sorted(blades, key=lambda b: (b["days"] is None, b["days"] or 0)):
                print(_fmt(b))
        print(watch_bill_line(blades, config(cur)[1]))
        return 0
    finally:
        conn.close()


def selftest() -> int:
    assert daily([]) == []
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    pts = daily([(t0 + timedelta(days=i, hours=h), 50 + i + h * 0.01) for i in range(10) for h in (0, 1)])
    assert len(pts) == 10 and pts[0][0] == 0
    m, lo, hi = theil_sen(pts)
    assert abs(m - 1.0) < 0.01, m
    b = blade(pts, 45.0)
    assert 40 < b["days"] < 50 and not b["accelerating"], b
    curve = [(i, 50 + (i * i if i > 5 else i)) for i in range(12)]
    assert blade(curve, 40.0)["accelerating"]
    assert blade([(i, 50.0) for i in range(8)], 40.0)["days"] is None
    assert blade(pts[:2], 10.0)["note"].startswith("learning")
    assert flag_lead({"kind": "cert", "days": 10}, LEAD_DAYS)["inside_lead"]
    line = watch_bill_line([{"blade": "x", "kind": "disk", "days": 12.0, "days_lo": 10.0, "days_hi": 15.0,
                             "accelerating": True, "inside_lead": True}, {"blade": "y", "kind": "disk", "days": None}])
    assert "x" in line and "12 days" in line and "accelerating" in line, line
    assert watch_bill_line([]) == "Pendulum: no blade is falling."
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="measure every blade and write one row each")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print blades, write nothing")
    ap.add_argument("--line", action="store_true", help="the Watch Bill line from the latest run")
    ap.add_argument("--show", action="store_true", help="latest run, nearest blade first")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(dry=a.dry_run)
        return 0
    if a.line or a.show:
        return show(line_only=a.line)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
