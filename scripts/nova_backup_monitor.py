#!/usr/bin/env python3
"""
nova_backup_monitor.py — the "service" half of nova-backup (runs on .6).

The Synology agent does the actual diff/sync and writes telemetry.backup_runs.
This monitor reads that table and makes the backup *observable*:
  - STALENESS: if a job hasn't had a successful run within its window, emit a
    warning (or critical, if very stale) to the notification bus. This is the
    gap that let the NAS backup silently fail for days — a backup that stops
    should scream.
  - DIGEST: when all jobs are healthy, post a quiet digest to #nova-info.
  - FAILURE: surface the most recent run's rc if it failed.

PG-only (no filesystem access needed). Routes via nova_notify; never raises.
Restore-verification lives in the agent's weekly full run (it has src+dest access).
"""
import sys
from datetime import datetime, timezone

import psycopg2

try:
    import nova_notify
except Exception:
    nova_notify = None

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
SOURCE = "nova-backup-monitor"

# job-name prefix -> max hours since last SUCCESS before we alert
JOBS = {
    "nova-backup:nas": 36,        # nightly incremental: stale after 36h
    "nova-backup:external": 36,
}


def emit(title, body, level, dedup_key):
    if nova_notify:
        try:
            nova_notify.notify(title=title, body=body, level=level,
                               category="backup", source=SOURCE, dedup_key=dedup_key)
            return
        except Exception:
            pass
    print(f"[{level}] {title}: {body}")


def check():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    issues, healthy = [], []
    with conn.cursor() as cur:
        for prefix, max_age_h in JOBS.items():
            cur.execute(
                "SELECT max(ts) FROM telemetry.backup_runs WHERE job LIKE %s AND ok = true",
                (prefix + "%",))
            last = cur.fetchone()[0]
            if last is None:
                issues.append((prefix, "no successful run ever recorded", "warning"))
                continue
            age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
            if age_h > max_age_h:
                level = "critical" if age_h > max_age_h * 2 else "warning"
                issues.append((prefix, f"last success {age_h:.1f}h ago (limit {max_age_h}h)", level))
            else:
                # surface a recent FAILED run even if an older success exists
                cur.execute(
                    "SELECT rc, ok FROM telemetry.backup_runs WHERE job LIKE %s "
                    "ORDER BY ts DESC LIMIT 1", (prefix + "%",))
                rc, ok = cur.fetchone()
                if not ok:
                    issues.append((prefix, f"most recent run FAILED (rc={rc})", "warning"))
                else:
                    healthy.append((prefix, age_h))
    conn.close()

    for prefix, msg, level in issues:
        emit(f"Backup stale/failed: {prefix.split(':')[-1]}", msg, level,
             dedup_key=f"backup-stale:{prefix}")
    if not issues and healthy:
        body = ", ".join(f"{p.split(':')[-1]}: {a:.1f}h ago" for p, a in healthy)
        emit("Backups healthy", body, "info", dedup_key="backup-digest")
    return issues


if __name__ == "__main__":
    sys.exit(1 if check() else 0)
