#!/usr/bin/env python3
"""nova_scheduler_reaper.py — reap zombie scheduler_runs rows stuck in 'running'.

WHY THIS EXISTS:
The scheduler writes a scheduler_runs row with status='running' at task start and
flips it to success/failure/timeout at exit. If the scheduler process dies mid-run
(crash, host reboot, launchd kill, OOM) the row is NEVER closed — it sits 'running'
forever. Over months these pile up (351 zombies going back to May were found and
reaped by hand once). Every consumer that trusts status='running' as "live" is then
lied to: dashboards over-count active tasks, and any "is this task already running?"
guard can wedge.

WHAT IT DOES:
Marks any scheduler_runs row with status='running' whose started_at is older than a
safe threshold as status='orphaned'. It does NOT touch fresh 'running' rows (real
long-runners like identity_graph sit in 'running' legitimately) and never touches a
row that already reached a terminal status. Idempotent: re-running reaps nothing new.

THRESHOLD (safety first):
No task legitimately runs for a full day. We still verify that empirically each run:
threshold = max(24h, 3 x observed max successful-run duration). The 3x margin on the
real observed ceiling means a genuinely-long task can run 3x its historical worst and
still not be reaped. started_at is epoch MILLISECONDS (bigint).

Cadence: hourly via launchd (net.digitalnoise.nova-scheduler-reaper, StartInterval
3600). Logs how many it reaped to stdout and to nova_ops.claude_actions. Never raises
out of run_once — a reaper must not crash.

Written by Jordan Koch.
"""
from __future__ import annotations
import sys
from typing import Optional

DSN = "host=localhost dbname=nova_ops user=kochj"
SESSION_ID = "nova-scheduler-reaper"

FLOOR_H = 24        # absolute floor: nothing legit runs a full day
FACTOR = 3          # safety margin on the observed max legit duration
_MS_PER_H = 3_600_000


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def compute_threshold_ms(max_success_ms: Optional[int],
                         floor_h: int = FLOOR_H, factor: int = FACTOR) -> int:
    """Reap threshold in ms: max(floor, factor x observed max successful duration).

    Pure. If there's no/degenerate duration history, fall back to the floor so we
    never reap using a nonsense (tiny/negative) empirical value."""
    floor = floor_h * _MS_PER_H
    if not max_success_ms or max_success_ms <= 0:
        return floor
    return max(floor, int(factor * max_success_ms))


def fetch_max_success_ms(conn) -> int:
    """Largest duration_ms among successful runs — the empirical 'longest legit run'."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(duration_ms) FROM scheduler_runs "
            "WHERE status='success' AND duration_ms IS NOT NULL")
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def reap(conn, now_ms: Optional[int] = None) -> dict:
    """Flip stale 'running' rows to 'orphaned'. Returns a summary dict.

    Only rows with status='running' AND started_at < (now - threshold) are touched,
    so it is inherently idempotent and never disturbs terminal or fresh rows."""
    now_ms = now_ms if now_ms is not None else _now_ms()
    max_ok = fetch_max_success_ms(conn)
    threshold_ms = compute_threshold_ms(max_ok)
    cutoff_ms = now_ms - threshold_ms
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scheduler_runs SET status='orphaned' "
            "WHERE status='running' AND started_at < %s",
            (cutoff_ms,))
        reaped = cur.rowcount
    conn.commit()
    return {
        "reaped": reaped,
        "threshold_ms": threshold_ms,
        "threshold_h": round(threshold_ms / _MS_PER_H, 2),
        "cutoff_ms": cutoff_ms,
        "max_success_ms": max_ok,
    }


def ensure_session(conn) -> None:
    """Register our session row so claude_actions' FK is satisfiable. Idempotent."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') "
                "ON CONFLICT (session_id) DO NOTHING", (SESSION_ID,))
        conn.commit()
    except Exception:
        conn.rollback()


def _log_action(conn, summary: dict) -> None:
    """Record the reap outcome in claude_actions (best-effort, savepoint-guarded)."""
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp_act")
        try:
            cur.execute(
                """INSERT INTO claude_actions (session_id, action_type, target, description, outcome)
                   VALUES (%s,'reap','scheduler_runs',%s,%s)""",
                (SESSION_ID,
                 f"reaped {summary['reaped']} stale 'running' rows "
                 f"(threshold {summary['threshold_h']}h)",
                 f"reaped={summary['reaped']}"))
            cur.execute("RELEASE SAVEPOINT sp_act")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_act")
    conn.commit()


def run_once(conn) -> dict:
    ensure_session(conn)
    summary = reap(conn)
    _log_action(conn, summary)
    return summary


def main() -> int:
    import psycopg2
    conn = None
    try:
        conn = psycopg2.connect(DSN)
        summary = run_once(conn)
    except Exception as e:
        print(f"scheduler_reaper: run failed: {e}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
    print(f"scheduler_reaper: reaped {summary['reaped']} orphaned run(s) "
          f"(threshold {summary['threshold_h']}h, "
          f"max legit success {summary['max_success_ms'] / _MS_PER_H:.2f}h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
