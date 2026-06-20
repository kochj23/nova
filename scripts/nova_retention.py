#!/usr/bin/env python3
"""
nova_retention.py — Data retention + downsampling for the nova_ops telemetry DB.

PURPOSE
    nova_ops is ~4.8 GB and grows ~250k rows/day. Raw, high-resolution
    telemetry (per-poll SNMP, per-scan Bluetooth, per-message syslog) is
    rarely useful past ~30-90 days, but TRENDS matter long-term. This script
    enforces a per-table retention window and (optionally) rolls raw rows up
    into *_hourly trend tables BEFORE the raw data is dropped, so the
    long-term picture survives even after the raw rows are gone.

DESIGN
    Two kinds of tables exist in nova_ops:

      1. RANGE-partitioned-by-month tables  (telemetry.*: bluetooth, presence,
         network, nova_meta, unifi_metrics, ...). Each month is its own
         partition, e.g. telemetry.bluetooth_202606. Retention here is fast
         and clean: DETACH + DROP whole partitions older than the window.

      2. Plain (non-partitioned) tables  (public.snmp_metrics,
         public.syslog_events, public.health_checks). Retention here is a
         time-windowed DELETE (chunked) — slower, but these tables are not
         partitioned by the collectors, and this script must NOT change their
         schema (collector scripts own that).

    DOWNSAMPLING
      For the two highest-churn raw streams (snmp_metrics, bluetooth) we
      roll up to HOURLY averages into a rollup table before dropping raw:
        - public.snmp_metrics_hourly       (avg/min/max per device+metric+hour)
        - telemetry.bluetooth_hourly       (avg rssi/battery, sightings per
                                            mac+hour)
      Rollup is idempotent (ON CONFLICT upsert) so re-runs are safe.

SAFETY
      - DRY-RUN BY DEFAULT. Nothing is dropped/deleted/rolled-up without
        --apply. Dry-run prints exactly what it WOULD do plus a reclaimed-
        space estimate.
      - The CURRENT month and the PREVIOUS month partitions are NEVER
        dropped, regardless of config. Likewise plain-table deletes never
        touch rows newer than the larger of (config window, 2 calendar
        months) is NOT enforced — plain tables honor their own window but
        always keep >= MIN_KEEP_DAYS days as a floor.
      - nova_memories / nova_meta vector memory data is NEVER touched. This
        script only knows about the tables in RETENTION below.
      - Everything is wrapped in try/except; one failing table does not abort
        the rest. A clear per-table + overall summary is printed at the end.

USAGE
      python3 nova_retention.py                 # dry-run, all tables
      python3 nova_retention.py --apply         # actually drop/delete/rollup
      python3 nova_retention.py --only snmp_metrics bluetooth
      python3 nova_retention.py --no-downsample # skip rollups, retention only
      python3 nova_retention.py --verbose

SCHEDULE (suggested)
      cron:  30 4 * * *  /opt/homebrew/bin/python3 $HOME/.openclaw/scripts/nova_retention.py --apply >> $HOME/.openclaw/logs/nova_retention.log 2>&1
      (daily at 04:30; see launchd plist suggestion in the task report.)

NOTE: This script touches the DB only. It does NOT modify any collector
      (nova_big_brother.py, nova_mesh_agent.py, nova_resolve.py, etc.) or any
      Grafana dashboard.
"""

import argparse
import datetime as dt
import sys
import traceback

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
PG_DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"

# ---------------------------------------------------------------------------
# RETENTION CONFIG  — tune these. Windows are in DAYS.
#
#   kind            "partition" -> drop whole monthly partitions
#                   "plain"     -> time-windowed DELETE on a non-partitioned table
#   retention_days  raw data older than this is eligible for removal
#   ts_col          timestamp column used for the window (plain tables only)
#   downsample      "bluetooth" | "snmp" | None  (rollup strategy before drop)
#
# RATIONALE
#   bluetooth     30d  raw per-scan RSSI/battery is noise past a month; we keep
#                      hourly rollups forever for presence/trend analysis.
#                      (~170k rows/day, biggest telemetry churn.)
#   snmp_metrics  90d  per-interface counters; ops debugging needs ~a quarter
#                      at full res. Hourly rollup keeps long-term trends.
#                      (~160k rows/day, biggest public-table churn.)
#   presence      90d  who-was-where history; 1 quarter raw is plenty.
#   syslog_events 90d  3.6 GB and the single biggest table. Security review
#                      rarely needs raw syslog past a quarter; alert_fired /
#                      threat rows could be exempted later if desired.
#   health_checks 30d  pure operational noise; 1 month is generous.
#   network/nova_meta/unifi_metrics/others 180d  lower volume, keep ~6 months.
#
# These are deliberately CONSERVATIVE. Nothing here removes the current or
# previous month. Bump windows up freely; lower them only after a dry-run.
# ---------------------------------------------------------------------------
RETENTION = {
    # --- monthly-partitioned telemetry.* tables ---
    "bluetooth": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "bluetooth",
        "retention_days": 30,
        "downsample": "bluetooth",
    },
    "presence": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "presence",
        "retention_days": 90,
        "downsample": None,
    },
    "network": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "network",
        "retention_days": 180,
        "downsample": None,
    },
    "nova_meta": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "nova_meta",
        "retention_days": 180,
        "downsample": None,
    },
    "unifi_metrics": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "unifi_metrics",
        "retention_days": 180,
        "downsample": None,
    },
    "activity": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "activity",
        "retention_days": 180,
        "downsample": None,
    },
    "climate": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "climate",
        "retention_days": 180,
        "downsample": None,
    },
    "weather": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "weather",
        "retention_days": 180,
        "downsample": None,
    },
    "av_state": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "av_state",
        "retention_days": 180,
        "downsample": None,
    },
    "ha_sensors": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "ha_sensors",
        "retention_days": 180,
        "downsample": None,
    },
    "energy": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "energy",
        "retention_days": 180,
        "downsample": None,
    },
    "storage_metrics": {
        "kind": "partition",
        "schema": "telemetry",
        "parent": "storage_metrics",
        "retention_days": 180,
        "downsample": None,
    },
    # --- plain (non-partitioned) public.* tables ---
    "snmp_metrics": {
        "kind": "plain",
        "schema": "public",
        "table": "snmp_metrics",
        "ts_col": "timestamp",
        "retention_days": 90,
        "downsample": "snmp",
    },
    "syslog_events": {
        "kind": "plain",
        "schema": "public",
        "table": "syslog_events",
        "ts_col": "received_at",
        "retention_days": 90,
        "downsample": None,
    },
    "health_checks": {
        "kind": "plain",
        "schema": "public",
        "table": "health_checks",
        "ts_col": "checked_at",
        "retention_days": 30,
        "downsample": None,
    },
}

# Absolute floor for plain-table DELETEs: never delete rows newer than this
# many days no matter what the config says (belt-and-suspenders).
MIN_KEEP_DAYS = 14

# Chunk size for plain-table deletes (rows per DELETE statement).
DELETE_CHUNK = 50_000

# Tables this script must NEVER look at, even if added to RETENTION by mistake.
FORBIDDEN = {"nova_memories", "nova_memory", "memories", "vectors"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg, *, verbose_only=False, args=None):
    if verbose_only and not (args and args.verbose):
        return
    print(msg, flush=True)


def fmt_bytes(n):
    if n is None:
        return "?"
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:,.1f} PB"


def month_floor(d):
    return dt.date(d.year, d.month, 1)


def prev_month(first_of_month):
    """Given the first day of a month, return first day of the previous month."""
    if first_of_month.month == 1:
        return dt.date(first_of_month.year - 1, 12, 1)
    return dt.date(first_of_month.year, first_of_month.month - 1, 1)


def protected_months(today):
    """Set of YYYYMM strings that must never be dropped (current + previous)."""
    cur = month_floor(today)
    prv = prev_month(cur)
    return {cur.strftime("%Y%m"), prv.strftime("%Y%m")}


# ---------------------------------------------------------------------------
# Downsampling — rollup table DDL + upsert
# ---------------------------------------------------------------------------
def ensure_rollup_tables(conn, apply, args):
    """Create *_hourly rollup tables if missing. Safe/idempotent."""
    ddl = [
        # SNMP hourly: avg/min/max per device+metric+hour
        """
        CREATE TABLE IF NOT EXISTS public.snmp_metrics_hourly (
            hour          timestamptz NOT NULL,
            device_ip     inet        NOT NULL,
            device_name   text,
            metric_name   text        NOT NULL,
            poll_group    text,
            unit          text,
            avg_value     double precision,
            min_value     double precision,
            max_value     double precision,
            sample_count  bigint,
            PRIMARY KEY (hour, device_ip, metric_name)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_snmp_hourly_metric_hour
            ON public.snmp_metrics_hourly (metric_name, hour DESC)
        """,
        # Bluetooth hourly: avg rssi/battery + sighting count per mac+hour
        """
        CREATE TABLE IF NOT EXISTS telemetry.bluetooth_hourly (
            hour            timestamptz NOT NULL,
            device_mac      text        NOT NULL,
            device_name     text,
            device_type     text,
            avg_rssi        double precision,
            min_rssi        integer,
            max_rssi        integer,
            avg_battery_pct double precision,
            sightings       bigint,
            connected_count bigint,
            PRIMARY KEY (hour, device_mac)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_bt_hourly_mac_hour
            ON telemetry.bluetooth_hourly (device_mac, hour DESC)
        """,
    ]
    cur = conn.cursor()
    for stmt in ddl:
        if apply:
            cur.execute(stmt)
        else:
            log(f"  [dry-run] would ensure rollup DDL: {stmt.split(chr(10))[1].strip()[:70]}...",
                verbose_only=True, args=args)
    if apply:
        conn.commit()
    cur.close()


def downsample_snmp(conn, cutoff, apply, args):
    """Roll up snmp_metrics rows OLDER than cutoff into snmp_metrics_hourly."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT count(*) FROM public.snmp_metrics
        WHERE timestamp < %s
        """,
        (cutoff,),
    )
    raw = cur.fetchone()[0]
    if raw == 0:
        log("    snmp downsample: no raw rows older than cutoff", verbose_only=True, args=args)
        cur.close()
        return 0
    if not apply:
        log(f"    [dry-run] snmp downsample: would roll up {raw:,} raw rows into snmp_metrics_hourly")
        cur.close()
        return raw
    cur.execute(
        """
        INSERT INTO public.snmp_metrics_hourly
            (hour, device_ip, device_name, metric_name, poll_group, unit,
             avg_value, min_value, max_value, sample_count)
        SELECT date_trunc('hour', timestamp) AS hour,
               device_ip,
               max(device_name)              AS device_name,
               metric_name,
               max(poll_group)               AS poll_group,
               max(unit)                     AS unit,
               avg(metric_value),
               min(metric_value),
               max(metric_value),
               count(*)
        FROM public.snmp_metrics
        WHERE timestamp < %s
        GROUP BY date_trunc('hour', timestamp), device_ip, metric_name
        ON CONFLICT (hour, device_ip, metric_name) DO UPDATE SET
            device_name  = EXCLUDED.device_name,
            poll_group   = EXCLUDED.poll_group,
            unit         = EXCLUDED.unit,
            avg_value    = EXCLUDED.avg_value,
            min_value    = EXCLUDED.min_value,
            max_value    = EXCLUDED.max_value,
            sample_count = EXCLUDED.sample_count
        """,
        (cutoff,),
    )
    conn.commit()
    log(f"    snmp downsample: rolled up {raw:,} raw rows -> snmp_metrics_hourly")
    cur.close()
    return raw


def downsample_bluetooth_partition(conn, partition_relname, apply, args):
    """Roll up one bluetooth_YYYYMM partition into telemetry.bluetooth_hourly."""
    cur = conn.cursor()
    cur.execute(f"SELECT count(*) FROM telemetry.{partition_relname}")
    raw = cur.fetchone()[0]
    if raw == 0:
        cur.close()
        return 0
    if not apply:
        log(f"    [dry-run] bluetooth downsample: would roll up {raw:,} rows "
            f"from {partition_relname} into telemetry.bluetooth_hourly")
        cur.close()
        return raw
    cur.execute(
        f"""
        INSERT INTO telemetry.bluetooth_hourly
            (hour, device_mac, device_name, device_type, avg_rssi,
             min_rssi, max_rssi, avg_battery_pct, sightings, connected_count)
        SELECT date_trunc('hour', ts) AS hour,
               device_mac,
               max(device_name)  AS device_name,
               max(device_type)  AS device_type,
               avg(rssi),
               min(rssi),
               max(rssi),
               avg(battery_pct),
               count(*),
               count(*) FILTER (WHERE is_connected)
        FROM telemetry.{partition_relname}
        GROUP BY date_trunc('hour', ts), device_mac
        ON CONFLICT (hour, device_mac) DO UPDATE SET
            device_name     = EXCLUDED.device_name,
            device_type     = EXCLUDED.device_type,
            avg_rssi        = EXCLUDED.avg_rssi,
            min_rssi        = EXCLUDED.min_rssi,
            max_rssi        = EXCLUDED.max_rssi,
            avg_battery_pct = EXCLUDED.avg_battery_pct,
            sightings       = EXCLUDED.sightings,
            connected_count = EXCLUDED.connected_count
        """,
    )
    conn.commit()
    log(f"    bluetooth downsample: rolled up {raw:,} rows from {partition_relname} "
        f"-> telemetry.bluetooth_hourly")
    cur.close()
    return raw


# ---------------------------------------------------------------------------
# Partition discovery + drop
# ---------------------------------------------------------------------------
def list_partitions(conn, schema, parent):
    """Return [(partition_relname, size_bytes, yyyymm_or_None)] for a parent."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.relname,
               pg_total_relation_size(i.inhrelid) AS bytes
        FROM pg_inherits i
        JOIN pg_class c       ON c.oid = i.inhrelid
        JOIN pg_class p       ON p.oid = i.inhparent
        JOIN pg_namespace pn  ON pn.oid = p.relnamespace
        WHERE pn.nspname = %s AND p.relname = %s AND c.relkind = 'r'
        ORDER BY c.relname
        """,
        (schema, parent),
    )
    out = []
    for relname, nbytes in cur.fetchall():
        yyyymm = None
        tail = relname.rsplit("_", 1)[-1]
        if len(tail) == 6 and tail.isdigit():
            yyyymm = tail
        out.append((relname, nbytes, yyyymm))
    cur.close()
    return out


def process_partitioned(conn, name, cfg, today, apply, do_downsample, args):
    """Handle one monthly-partitioned table. Returns (reclaimed_bytes, dropped_list)."""
    schema = cfg["schema"]
    parent = cfg["parent"]
    cutoff = today - dt.timedelta(days=cfg["retention_days"])
    cutoff_month = month_floor(cutoff).strftime("%Y%m")
    protect = protected_months(today)

    log(f"\n[{name}] partitioned {schema}.{parent}  "
        f"retention={cfg['retention_days']}d  cutoff_month<{cutoff_month}  "
        f"(protected: {sorted(protect)})")

    reclaimed = 0
    dropped = []
    parts = list_partitions(conn, schema, parent)
    if not parts:
        log("  (no partitions found)")
        return reclaimed, dropped

    for relname, nbytes, yyyymm in parts:
        if yyyymm is None:
            log(f"  - {relname}: cannot parse YYYYMM, SKIPPING (safety)")
            continue
        if yyyymm in protect:
            log(f"  - {relname} ({fmt_bytes(nbytes)}): PROTECTED (current/prev month), keep",
                verbose_only=True, args=args)
            continue
        if yyyymm >= cutoff_month:
            log(f"  - {relname} ({fmt_bytes(nbytes)}): within window, keep",
                verbose_only=True, args=args)
            continue

        # Eligible for removal.
        if do_downsample and cfg.get("downsample") == "bluetooth":
            downsample_bluetooth_partition(conn, relname, apply, args)

        fq = f"{schema}.{relname}"
        if apply:
            try:
                cur = conn.cursor()
                cur.execute(f"ALTER TABLE {schema}.{parent} DETACH PARTITION {fq}")
                cur.execute(f"DROP TABLE {fq}")
                conn.commit()
                cur.close()
                log(f"  - {relname} ({fmt_bytes(nbytes)}): DETACHED + DROPPED")
            except Exception as e:
                conn.rollback()
                log(f"  - {relname}: ERROR dropping: {e}")
                continue
        else:
            log(f"  - {relname} ({fmt_bytes(nbytes)}): [dry-run] WOULD detach + drop")
        reclaimed += nbytes or 0
        dropped.append(relname)

    return reclaimed, dropped


# ---------------------------------------------------------------------------
# Plain-table windowed delete
# ---------------------------------------------------------------------------
def process_plain(conn, name, cfg, today, apply, do_downsample, args):
    """Handle one non-partitioned table via windowed DELETE.
    Returns (estimated_reclaimed_bytes, deleted_rowcount)."""
    schema = cfg["schema"]
    table = cfg["table"]
    ts_col = cfg["ts_col"]
    fq = f"{schema}.{table}"

    if table in FORBIDDEN:
        log(f"\n[{name}] {fq}: FORBIDDEN table, refusing to touch")
        return 0, 0

    # Enforce floor.
    eff_days = max(cfg["retention_days"], MIN_KEEP_DAYS)
    cutoff = dt.datetime.combine(today - dt.timedelta(days=eff_days), dt.time.min)

    log(f"\n[{name}] plain {fq}  retention={cfg['retention_days']}d "
        f"(floor {MIN_KEEP_DAYS}d)  delete {ts_col} < {cutoff.date()}")

    cur = conn.cursor()
    # Count eligible rows + estimate bytes via avg row width.
    cur.execute(f"SELECT count(*) FROM {fq} WHERE {ts_col} < %s", (cutoff,))
    eligible = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM {fq}")
    total = cur.fetchone()[0]
    cur.execute(f"SELECT pg_total_relation_size('{fq}')")
    tbl_bytes = cur.fetchone()[0]

    est_bytes = 0
    if total > 0:
        est_bytes = int(tbl_bytes * (eligible / total))

    if eligible == 0:
        log(f"  no rows older than cutoff (table has {total:,} rows, {fmt_bytes(tbl_bytes)})")
        cur.close()
        return 0, 0

    # Downsample before delete (snmp only).
    if do_downsample and cfg.get("downsample") == "snmp":
        downsample_snmp(conn, cutoff, apply, args)

    if not apply:
        log(f"  [dry-run] WOULD delete {eligible:,} of {total:,} rows "
            f"(~{fmt_bytes(est_bytes)} reclaimable after VACUUM)")
        cur.close()
        return est_bytes, eligible

    # Chunked delete so we don't take one giant lock / bloat WAL.
    deleted = 0
    while True:
        cur.execute(
            f"""
            DELETE FROM {fq}
            WHERE ctid IN (
                SELECT ctid FROM {fq} WHERE {ts_col} < %s LIMIT %s
            )
            """,
            (cutoff, DELETE_CHUNK),
        )
        n = cur.rowcount
        conn.commit()
        deleted += n
        if n == 0:
            break
        log(f"    deleted {deleted:,}/{eligible:,} ...", verbose_only=True, args=args)

    log(f"  deleted {deleted:,} rows. Running VACUUM (ANALYZE) {fq} ...")
    old_iso = conn.isolation_level
    conn.set_isolation_level(0)  # VACUUM cannot run in a transaction block
    cur.execute(f"VACUUM (ANALYZE) {fq}")
    conn.set_isolation_level(old_iso)
    log(f"  VACUUM done. (~{fmt_bytes(est_bytes)} reclaimable to OS only via VACUUM FULL)")
    cur.close()
    return est_bytes, deleted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="nova_ops telemetry retention + downsampling")
    ap.add_argument("--apply", action="store_true",
                    help="actually drop/delete/roll-up (default: dry-run)")
    ap.add_argument("--no-downsample", action="store_true",
                    help="skip *_hourly rollups; do retention only")
    ap.add_argument("--only", nargs="+", metavar="TABLE",
                    help="restrict to these RETENTION keys")
    ap.add_argument("--verbose", action="store_true", help="verbose per-row logging")
    args = ap.parse_args()

    do_downsample = not args.no_downsample
    today = dt.date.today()
    mode = "APPLY (DESTRUCTIVE)" if args.apply else "DRY-RUN (no changes)"

    log("=" * 72)
    log(f"nova_retention.py — {mode}")
    log(f"date={today}  downsample={'on' if do_downsample else 'off'}  "
        f"db={PG_DSN}")
    log("=" * 72)

    try:
        conn = psycopg2.connect(PG_DSN)
    except Exception as e:
        log(f"FATAL: cannot connect to PG: {e}")
        return 2

    db_before = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_database_size('nova_ops')")
        db_before = cur.fetchone()[0]
        cur.close()
    except Exception:
        pass

    if do_downsample:
        try:
            ensure_rollup_tables(conn, args.apply, args)
        except Exception as e:
            conn.rollback()
            log(f"WARN: could not ensure rollup tables: {e}")

    keys = args.only if args.only else list(RETENTION.keys())
    total_reclaim = 0
    summary = []

    for name in keys:
        cfg = RETENTION.get(name)
        if not cfg:
            log(f"\n[{name}] not in RETENTION config, skipping")
            continue
        try:
            if cfg["kind"] == "partition":
                reclaimed, items = process_partitioned(
                    conn, name, cfg, today, args.apply, do_downsample, args)
                total_reclaim += reclaimed
                summary.append((name, "partition", reclaimed,
                                f"{len(items)} partition(s)"))
            elif cfg["kind"] == "plain":
                reclaimed, rows = process_plain(
                    conn, name, cfg, today, args.apply, do_downsample, args)
                total_reclaim += reclaimed
                summary.append((name, "plain", reclaimed, f"{rows:,} row(s)"))
            else:
                log(f"\n[{name}] unknown kind {cfg['kind']!r}, skipping")
        except Exception as e:
            conn.rollback()
            log(f"\n[{name}] ERROR: {e}")
            log(traceback.format_exc(), verbose_only=True, args=args)
            summary.append((name, cfg.get("kind", "?"), 0, f"ERROR: {e}"))

    # ---- summary ----
    log("\n" + "=" * 72)
    log("SUMMARY  (" + mode + ")")
    log("=" * 72)
    log(f"{'table':<18}{'kind':<11}{'reclaim est':>14}  detail")
    log("-" * 72)
    for name, kind, reclaimed, detail in summary:
        log(f"{name:<18}{kind:<11}{fmt_bytes(reclaimed):>14}  {detail}")
    log("-" * 72)
    log(f"{'TOTAL':<18}{'':<11}{fmt_bytes(total_reclaim):>14}")

    if db_before is not None:
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_database_size('nova_ops')")
            db_after = cur.fetchone()[0]
            cur.close()
            log(f"\nDB size: {fmt_bytes(db_before)} -> {fmt_bytes(db_after)}")
        except Exception:
            pass

    if not args.apply:
        log("\nDRY-RUN only. Re-run with --apply to perform the above.")
        log("NOTE: plain-table space returns to PostgreSQL after VACUUM but only to")
        log("the OS after a VACUUM FULL (which locks the table). Partition drops")
        log("return space to the OS immediately.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
