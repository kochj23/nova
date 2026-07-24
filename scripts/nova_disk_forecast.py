#!/opt/homebrew/bin/python3
"""
nova_disk_forecast.py — Disk-growth forecasting collector for Nova's
observability stack (PG -> Grafana).

Reads existing storage history, fits a simple linear trend per host/volume, and
projects days-until-full (default: until 95% used). Writes timestamped rows into
telemetry.disk_forecast (partitioned by month on ts, matching telemetry.*).

History sources (best-effort, both guarded):
  1. telemetry.storage_metrics — per-volume `used_pct` over time (Synology,
     UNAS, etc.). This is the richest series and the primary forecast input.
  2. snmp_metrics — hrStorage used/size (disk_storage_used.N / disk_storage_size.N)
     paired by index per device, when present, giving used_pct over time.

A snapshot of the current node_status.disk_percent is also recorded each run so
a per-host series accumulates here even for hosts with no other history; once
>= 2 of OUR snapshots exist, those become forecastable too.

Trend math: ordinary least-squares slope of used_pct vs. time(days). With slope
> 0 we project days = (target_pct - current_pct) / slope. Already-at/over-target
or flat/shrinking volumes record days_until_full = NULL with an explanatory note.

Resilient: each source/series is independently guarded; insufficient history
(< 2 points) records a row with NULL growth + note rather than crashing.

  python3 nova_disk_forecast.py            # forecast + write PG
  python3 nova_disk_forecast.py --dry-run  # forecast + print, no write
  python3 nova_disk_forecast.py --target 90

Written by Jordan Koch.
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

DB_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
NOW = datetime.now(timezone.utc)

DEFAULT_TARGET_PCT = 95.0
LOOKBACK_DAYS = 30          # history window for the trend fit
MIN_POINTS = 2
MIN_SLOPE = 1e-4           # below this pct/day, treat as flat (avoids absurd ETAs)
MAX_DAYS = 36500.0         # cap projection at ~100 years; beyond = "not soon"

COLUMNS = ["ts", "source", "host", "volume", "used_pct",
           "growth_pct_per_day", "days_until_full", "target_pct",
           "n_points", "note"]


def log(msg):
    print(f"[disk_forecast {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _row(source, host, volume, **kw):
    r = {c: None for c in COLUMNS}
    r["ts"] = NOW
    r["source"] = source
    r["host"] = host
    r["volume"] = volume
    for k, v in kw.items():
        if k in r:
            r[k] = v
    return r


def _ols_slope(points):
    """points = [(t_days, pct), ...]. Returns slope (pct/day) or None."""
    n = len(points)
    if n < MIN_POINTS:
        return None
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return (n * sxy - sx * sy) / denom


def _forecast_row(source, host, volume, series, target_pct):
    """series = [(datetime, used_pct), ...] sorted ascending by time."""
    series = sorted(series, key=lambda x: x[0])
    n = len(series)
    current = series[-1][1] if series else None
    if n < MIN_POINTS:
        return _row(source, host, volume, used_pct=current, n_points=n,
                    target_pct=target_pct,
                    note="insufficient history (<2 points); forecast skipped")

    t0 = series[0][0]
    pts = [((dt - t0).total_seconds() / 86400.0, pct) for dt, pct in series]
    slope = _ols_slope(pts)
    if slope is None:
        return _row(source, host, volume, used_pct=current, n_points=n,
                    target_pct=target_pct, note="degenerate trend (no time span)")

    growth = round(slope, 6)
    # Clamp reported used_pct to a sane 0..100 (some SNMP entries report >100%
    # due to allocation-unit rounding / overcommit).
    cur_clamped = round(min(max(current, 0.0), 100.0), 3)
    if current is not None and current >= target_pct:
        return _row(source, host, volume, used_pct=cur_clamped,
                    growth_pct_per_day=growth, days_until_full=0.0,
                    target_pct=target_pct, n_points=n,
                    note=f"already at/over target {target_pct}%")
    if slope < MIN_SLOPE:
        return _row(source, host, volume, used_pct=cur_clamped,
                    growth_pct_per_day=growth, days_until_full=None,
                    target_pct=target_pct, n_points=n,
                    note="flat or shrinking; not projected to fill")

    days = (target_pct - current) / slope
    if days > MAX_DAYS:
        return _row(source, host, volume, used_pct=cur_clamped,
                    growth_pct_per_day=growth, days_until_full=None,
                    target_pct=target_pct, n_points=n,
                    note=f"growth negligible; ETA >{int(MAX_DAYS)}d (not soon)")
    return _row(source, host, volume, used_pct=cur_clamped,
                growth_pct_per_day=growth, days_until_full=round(days, 1),
                target_pct=target_pct, n_points=n)


# ── Source 1: telemetry.storage_metrics volumes ───────────────────────────────

def from_storage_metrics(conn, target_pct):
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT host, COALESCE(component_id, component_name) AS vol, ts, used_pct
                FROM telemetry.storage_metrics
                WHERE component_type IN ('volume', 'share', 'pool')
                  AND used_pct IS NOT NULL
                  AND ts >= now() - interval '%s days'
                ORDER BY host, vol, ts
            """ % LOOKBACK_DAYS)
            recs = cur.fetchall()
    except Exception as e:
        log(f"storage_metrics source: {e}")
        return rows

    series = defaultdict(list)
    for host, vol, ts, pct in recs:
        series[(host, vol)].append((ts, float(pct)))
    for (host, vol), pts in series.items():
        rows.append(_forecast_row("storage_metrics", host, vol, pts, target_pct))
    return rows


# ── Source 2: snmp_metrics hrStorage (used/size by index) ──────────────────────

def from_snmp(conn, target_pct):
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT device_name, device_ip::text, metric_name, timestamp, metric_value
                FROM snmp_metrics
                WHERE (metric_name LIKE 'disk_storage_used.%%'
                       OR metric_name LIKE 'disk_storage_size.%%')
                  AND timestamp >= now() - interval '%s days'
                ORDER BY device_ip, timestamp
            """ % LOOKBACK_DAYS)
            recs = cur.fetchall()
    except Exception as e:
        log(f"snmp source: {e}")
        return rows

    # bucket: (dev, idx) -> {'used': [(ts,v)], 'size': [(ts,v)]}
    buckets = defaultdict(lambda: {"used": {}, "size": {}})
    names = {}
    for dev_name, dev_ip, metric, ts, val in recs:
        try:
            kind, idx = metric.split(".", 1)
        except ValueError:
            continue
        key = (dev_ip, idx)
        names[key] = dev_name or dev_ip
        if kind == "disk_storage_used":
            buckets[key]["used"][ts] = float(val)
        elif kind == "disk_storage_size":
            buckets[key]["size"][ts] = float(val)

    for (dev_ip, idx), data in buckets.items():
        series = []
        for ts, used in data["used"].items():
            size = data["size"].get(ts)
            if size and size > 0:
                series.append((ts, used / size * 100.0))
        if not series:
            continue
        rows.append(_forecast_row("snmp", names[(dev_ip, idx)],
                                  f"hrStorage.{idx}", series, target_pct))
    return rows


# ── Source 3: node_status snapshot (point-in-time; record for future trend) ────

def node_status_snapshots(conn, target_pct):
    """Record one row per node from the current live disk_percent. With a single
    live point there is no trend yet, so these are recorded as snapshots; over
    successive runs the disk_forecast table itself becomes the history."""
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_name, node_ip::text, disk_percent
                FROM node_status WHERE disk_percent IS NOT NULL
            """)
            live = cur.fetchall()
    except Exception as e:
        log(f"node_status source: {e}")
        return rows

    # Pull prior snapshots we wrote for these hosts to build a real trend.
    prior = defaultdict(list)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT host, used_pct, ts FROM telemetry.disk_forecast
                WHERE source = 'node_status' AND used_pct IS NOT NULL
                  AND ts >= now() - interval '%s days'
                ORDER BY host, ts
            """ % LOOKBACK_DAYS)
            for host, pct, ts in cur.fetchall():
                prior[host].append((ts, float(pct)))
    except Exception:
        pass  # table may not exist yet on first run

    for name, ip, pct in live:
        series = list(prior.get(name, []))
        series.append((NOW, float(pct)))
        rows.append(_forecast_row("node_status", name, "root", series, target_pct))
    return rows


# ── PG ────────────────────────────────────────────────────────────────────────

DDL = """
CREATE SCHEMA IF NOT EXISTS telemetry;
CREATE TABLE IF NOT EXISTS telemetry.disk_forecast (
    ts                  timestamptz NOT NULL,
    source              text        NOT NULL,
    host                text,
    volume              text,
    used_pct            real,
    growth_pct_per_day  double precision,
    days_until_full     real,
    target_pct          real,
    n_points            integer,
    note                text
) PARTITION BY RANGE (ts);
CREATE INDEX IF NOT EXISTS idx_disk_forecast_ts ON telemetry.disk_forecast (ts);
CREATE INDEX IF NOT EXISTS idx_disk_forecast_hv_ts ON telemetry.disk_forecast (host, volume, ts);
"""


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(DDL)


def ensure_partition(conn, ts):
    try:
        first = ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = first.replace(year=first.year + 1, month=1) if first.month == 12 \
            else first.replace(month=first.month + 1)
        suffix = first.strftime("%Y%m")
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS telemetry.disk_forecast_{suffix} "
                f"PARTITION OF telemetry.disk_forecast "
                f"FOR VALUES FROM (%s) TO (%s)", (first, nxt))
    except Exception as e:
        log(f"partition ensure: {e}")


def write_rows(conn, rows):
    if not rows:
        return 0
    inserted = 0
    try:
        ensure_partition(conn, NOW)
        cols = ", ".join(COLUMNS)
        ph = ", ".join(["%s"] * len(COLUMNS))
        sql = f"INSERT INTO telemetry.disk_forecast ({cols}) VALUES ({ph})"
        values = [[r[c] for c in COLUMNS] for r in rows]
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, values)
        inserted = len(values)
    except Exception as e:
        log(f"insert failed: {e}")
    return inserted


def main():
    ap = argparse.ArgumentParser(description="Nova disk-growth forecast collector")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--target", type=float, default=DEFAULT_TARGET_PCT,
                    help="full-threshold pct (default 95)")
    args = ap.parse_args()

    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
    except Exception as e:
        log(f"DB connect failed: {e}")
        return

    try:
        # Ensure the table exists first — node_status source reads prior rows.
        ensure_table(conn)

        rows = []
        rows += from_storage_metrics(conn, args.target)
        rows += from_snmp(conn, args.target)
        rows += node_status_snapshots(conn, args.target)

        for r in rows:
            log(f"  {r['source']}/{r['host']}/{r['volume']}: "
                f"used={r['used_pct']}% growth={r['growth_pct_per_day']}%/day "
                f"days_until_{r['target_pct']}%={r['days_until_full']} "
                f"(n={r['n_points']}) {r['note'] or ''}")

        if args.dry_run:
            log(f"DRY RUN — would insert {len(rows)} row(s)")
            return

        n = write_rows(conn, rows)
        log(f"inserted {n} row(s)")
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
