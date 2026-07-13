#!/opt/homebrew/bin/python3
"""
nova_pg_stat_poller.py — PostgreSQL throughput snapshot collector for Nova's
observability stack (PG -> Grafana).

Runs on the PRIMARY (.6). Reads the cumulative counters from pg_stat_database
(one row per real DB, template/system DBs skipped) and INSERTs a snapshot row
per DB into telemetry.pg_stat_db. Grafana computes per-second rates by diffing
consecutive snapshots ($__timeGroup + delta), turning "avg since boot" counters
into genuine live throughput timeseries.

Resilient: all work is inside try/except; a failed query writes nothing rather
than crashing.

  python3 nova_pg_stat_poller.py            # query + write PG
  python3 nova_pg_stat_poller.py --dry-run  # query + print, no write
  python3 nova_pg_stat_poller.py --self-check  # verify a fresh row landed

Written by Jordan Koch.
"""

import argparse
import sys
from datetime import datetime

import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=nova_ops user=kochj"

# Columns pulled straight from pg_stat_database (ts defaults to now()).
COLUMNS = ["datname", "xact_commit", "xact_rollback", "tup_inserted",
           "tup_updated", "tup_deleted", "tup_returned", "tup_fetched",
           "blks_read", "blks_hit", "temp_files", "temp_bytes", "deadlocks"]


def log(msg):
    print(f"[pg_stat_poller {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def collect(conn):
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT {", ".join(COLUMNS)}
                FROM pg_stat_database
                WHERE datname IS NOT NULL
                  AND datname NOT IN ('template0', 'template1')
            """)
            return cur.fetchall()
    except Exception as e:
        log(f"query failed: {e}")
        return []


def write_rows(conn, rows):
    if not rows:
        return 0
    try:
        cols = ", ".join(COLUMNS)
        ph = ", ".join(["%s"] * len(COLUMNS))
        sql = f"INSERT INTO telemetry.pg_stat_db ({cols}) VALUES ({ph})"
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows)
        return len(rows)
    except Exception as e:
        log(f"insert failed: {e}")
        return 0


def self_check(conn):
    """One-line self-check: confirm a row landed in the last 5 minutes."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), max(ts) FROM telemetry.pg_stat_db "
                        "WHERE ts > now() - interval '5 minutes'")
            n, latest = cur.fetchone()
        log(f"self-check: {n} row(s) in last 5m, latest={latest}")
        return n > 0
    except Exception as e:
        log(f"self-check failed: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description="Nova PG throughput snapshot collector")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-check", action="store_true", help="only verify recent rows exist")
    args = ap.parse_args()

    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
    except Exception as e:
        log(f"DB connect failed: {e}")
        return

    try:
        if args.self_check:
            sys.exit(0 if self_check(conn) else 1)
        rows = collect(conn)
        log(f"collected {len(rows)} db(s): {', '.join(r[0] for r in rows)}")
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
