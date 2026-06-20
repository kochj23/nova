#!/opt/homebrew/bin/python3
"""
nova_replication_monitor.py — PostgreSQL streaming-replication health collector
for Nova's observability stack (PG -> Grafana).

Runs on the PRIMARY (.6). Queries pg_stat_replication — the nova-core replica
(192.168.1.2) streams from here — and writes one row per connected standby into
telemetry.replication_health (partitioned by month on ts, matching telemetry.*).

Lag intervals (write_lag/flush_lag/replay_lag) are converted to milliseconds.
If no standby is connected (or the lag columns are NULL because the standby is
caught up and idle), a single sentinel row is written with state/sync_state from
the row when available, so the absence of a replica is itself graphable.

Resilient: all work is inside try/except; a failed query writes nothing rather
than crashing.

  python3 nova_replication_monitor.py            # query + write PG
  python3 nova_replication_monitor.py --dry-run  # query + print, no write

Written by Jordan Koch.
"""

import argparse
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
NOW = datetime.now(timezone.utc)

COLUMNS = ["ts", "client_addr", "application_name", "state",
           "write_lag_ms", "flush_lag_ms", "replay_lag_ms", "sync_state", "note"]


def log(msg):
    print(f"[replication_monitor {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _row(**kw):
    r = {c: None for c in COLUMNS}
    r["ts"] = NOW
    for k, v in kw.items():
        if k in r:
            r[k] = v
    return r


def _ms(interval):
    """timedelta -> milliseconds (float) or None."""
    if interval is None:
        return None
    try:
        return round(interval.total_seconds() * 1000.0, 3)
    except Exception:
        return None


def collect(conn):
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT client_addr, application_name, state,
                       write_lag, flush_lag, replay_lag, sync_state
                FROM pg_stat_replication
            """)
            recs = cur.fetchall()
    except Exception as e:
        log(f"query failed: {e}")
        return [_row(note=f"query error: {e}")]

    if not recs:
        log("no standbys connected")
        return [_row(note="no standby connected")]

    for client_addr, app, state, w, f, r, sync in recs:
        rows.append(_row(
            client_addr=str(client_addr) if client_addr is not None else None,
            application_name=app, state=state,
            write_lag_ms=_ms(w), flush_lag_ms=_ms(f), replay_lag_ms=_ms(r),
            sync_state=sync,
        ))
    return rows


# ── PG write ───────────────────────────────────────────────────────────────────

DDL = """
CREATE SCHEMA IF NOT EXISTS telemetry;
CREATE TABLE IF NOT EXISTS telemetry.replication_health (
    ts                timestamptz NOT NULL,
    client_addr       text,
    application_name  text,
    state             text,
    write_lag_ms      double precision,
    flush_lag_ms      double precision,
    replay_lag_ms     double precision,
    sync_state        text,
    note              text
) PARTITION BY RANGE (ts);
CREATE INDEX IF NOT EXISTS idx_repl_health_ts ON telemetry.replication_health (ts);
CREATE INDEX IF NOT EXISTS idx_repl_health_addr_ts ON telemetry.replication_health (client_addr, ts);
"""


def ensure_partition(conn, ts):
    try:
        first = ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = first.replace(year=first.year + 1, month=1) if first.month == 12 \
            else first.replace(month=first.month + 1)
        suffix = first.strftime("%Y%m")
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS telemetry.replication_health_{suffix} "
                f"PARTITION OF telemetry.replication_health "
                f"FOR VALUES FROM (%s) TO (%s)", (first, nxt))
    except Exception as e:
        log(f"partition ensure: {e}")


def write_rows(conn, rows):
    if not rows:
        return 0
    inserted = 0
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        ensure_partition(conn, NOW)
        cols = ", ".join(COLUMNS)
        ph = ", ".join(["%s"] * len(COLUMNS))
        sql = f"INSERT INTO telemetry.replication_health ({cols}) VALUES ({ph})"
        values = [[r[c] for c in COLUMNS] for r in rows]
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, values)
        inserted = len(values)
    except Exception as e:
        log(f"insert failed: {e}")
    return inserted


def main():
    ap = argparse.ArgumentParser(description="Nova PG replication health collector")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
    except Exception as e:
        log(f"DB connect failed: {e}")
        return

    try:
        rows = collect(conn)
        for r in rows:
            log(f"  {r['client_addr']} state={r['state']} sync={r['sync_state']} "
                f"write={r['write_lag_ms']}ms flush={r['flush_lag_ms']}ms "
                f"replay={r['replay_lag_ms']}ms note={r['note']}")
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
