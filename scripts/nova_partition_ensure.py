#!/usr/bin/env python3
"""nova_partition_ensure.py — pre-create monthly telemetry partitions.

Root cause of the 2026-08-01 alert storm: the August (_202608) partitions for ~13
monthly RANGE-partitioned telemetry tables were never created, so every insert
failed silently from 00:00 Aug 1 — a dozen presence/soil/storage feeds went dark
at the month boundary and negative-space fired "sensor is broken" for each.

This ensures the current month + the next N months of partitions exist for EVERY
telemetry parent that is monthly-partitioned (has a `<parent>_YYYYMM` child), so a
month rollover can never drop writes again. Idempotent; meant to run daily. Alerts
on-change to #nova-alerts only when it actually had to create something (which means
the pre-creation had lapsed and is worth noticing).
"""
import datetime
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
MONTHS_AHEAD = 3


def _add_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def main():
    created, failed = [], []
    c = psycopg2.connect(DSN)
    c.autocommit = True
    cur = c.cursor()
    # Only telemetry declarative-partitioned parents that ALREADY use monthly
    # <parent>_YYYYMM children (so we never guess a scheme for a non-monthly table).
    cur.execute("""
        SELECT DISTINCT parent.relname
        FROM pg_inherits i
        JOIN pg_class parent ON parent.oid = i.inhparent
        JOIN pg_class child  ON child.oid  = i.inhrelid
        JOIN pg_namespace n  ON n.oid = parent.relnamespace
        WHERE n.nspname = 'telemetry' AND parent.relkind = 'p'
          AND child.relname ~ (parent.relname || '_[0-9]{6}$')
    """)
    parents = [r[0] for r in cur.fetchall()]

    today = datetime.date.today()
    for parent in parents:
        yy, mm = today.year, today.month
        for _ in range(MONTHS_AHEAD + 1):
            part = f"{parent}_{yy}{mm:02d}"
            cur.execute("SELECT to_regclass(%s)", (f'telemetry."{part}"',))
            if cur.fetchone()[0] is None:
                ny, nm = _add_month(yy, mm)
                lo, hi = f"{yy}-{mm:02d}-01", f"{ny}-{nm:02d}-01"
                try:
                    cur.execute(
                        f'CREATE TABLE telemetry."{part}" PARTITION OF telemetry."{parent}" '
                        f'FOR VALUES FROM (%s) TO (%s)', (lo, hi))
                    created.append(part)
                except Exception as e:
                    failed.append(f"{part}: {e}")
            yy, mm = _add_month(yy, mm)
    c.close()

    print(f"[partition-ensure] {len(parents)} monthly tables, {MONTHS_AHEAD+1} months each; "
          f"created {len(created)}, failed {len(failed)}", flush=True)
    if created:
        print("  created: " + ", ".join(created), flush=True)
    if failed:
        print("  FAILED: " + " | ".join(failed), flush=True)

    if created or failed:
        try:
            import nova_config
            msg = ""
            if created:
                msg += f":wrench: partition-ensure created {len(created)} missing telemetry partition(s): {', '.join(created[:15])}"
            if failed:
                msg += f"\n:x: {len(failed)} failed: {failed[0]}"
            nova_config.post_both(msg, slack_channel=nova_config.SLACK_ALERTS, discord_channel=None)
        except Exception as e:
            print(f"  alert post failed: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
