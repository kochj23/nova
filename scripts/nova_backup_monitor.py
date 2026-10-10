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
import time
from datetime import datetime, timezone

import psycopg2

try:
    import nova_notify
except Exception:
    nova_notify = None

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
SOURCE = "nova-backup-monitor"

# job-name prefix -> max hours since last SUCCESS before we alert
JOBS = {
    "nova-backup:nas": 36,        # nightly incremental: stale after 36h
    "nova-backup:external": 36,
}


def emit(title, body, level, dedup_key, attempts=3):
    """Notify via the bus, retried (2 s, 4 s) — a stale-backup alert must not die on one bus blip.
    Falls back to stdout (the launchd log) with the reason if every attempt fails."""
    err = "nova_notify unavailable"
    if nova_notify:
        for attempt in range(attempts):
            try:
                nova_notify.notify(title=title, body=body, level=level,
                                   category="backup", source=SOURCE, dedup_key=dedup_key)
                return
            except Exception as e:
                err = e
                if attempt < attempts - 1:
                    time.sleep(2 * (attempt + 1))
        print(f"[backup-monitor] notify failed after {attempts} tries: {err}")
    print(f"[{level}] {title}: {body}")


def _connect(attempts=3):
    """PG connect with retry (5 s, 10 s); re-raises so launchd sees a non-zero exit, never a false 'healthy'."""
    for attempt in range(attempts):
        try:
            return psycopg2.connect(DSN, connect_timeout=10)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(5 * 2 ** attempt)


def _describe(prefix, age_h, job, elapsed_s, files, nbytes, errors):
    """'nas (incremental): 11.8h ago · 90 files, 286.0 MB in 36m33s · 0 errors'"""
    kind = job.rsplit(":", 1)[-1]
    m, sec = divmod(elapsed_s or 0, 60)
    h, m = divmod(m, 60)
    dur = f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"
    return (f"{prefix.split(':')[-1]} ({kind}): {age_h:.1f}h ago · {files or 0:,} files, "
            f"{(nbytes or 0) / 1e6:,.1f} MB in {dur} · {errors or 0} errors")


def check():
    conn = _connect()
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
                    "SELECT rc, ok, job, elapsed_s, files, bytes, errors FROM telemetry.backup_runs "
                    "WHERE job LIKE %s ORDER BY ts DESC LIMIT 1", (prefix + "%",))
                rc, ok, job, elapsed_s, files, nbytes, errors = cur.fetchone()
                if not ok:
                    issues.append((prefix, f"most recent run FAILED (rc={rc})", "warning"))
                else:
                    healthy.append((prefix, age_h, job, elapsed_s, files, nbytes, errors))
    conn.close()

    for prefix, msg, level in issues:
        emit(f"Backup stale/failed: {prefix.split(':')[-1]}", msg, level,
             dedup_key=f"backup-stale:{prefix}")
    if not issues and healthy:
        body = "\n".join(_describe(*h) for h in healthy)
        emit("Backups healthy", body, "info", dedup_key="backup-digest")
    return issues


if __name__ == "__main__":
    sys.exit(1 if check() else 0)
