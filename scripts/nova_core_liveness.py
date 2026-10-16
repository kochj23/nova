#!/usr/bin/env python3
"""
nova_core_liveness.py — "Nova watches Nova" core-liveness gate (#510).

The Make-Nova-Better epic was born from pgbouncer + the capacity poller dying
SILENTLY with nobody paged — Big Brother's digest mode + quiet-hours dedup hid
exactly that slow-burn class. This is the un-suppressable backstop:

  1. Independently TCP-probes Nova's KEYSTONE services (does not trust the same
     health_checks pipeline that could itself be wedged).
  2. WATCHES THE WATCHERS — alarms if health_checks or capacity_snapshots go
     stale (i.e. the health writer / capacity poller silently died — the epic's
     exact bug — "detect disabled-ness, not just down-ness").
  3. Cross-checks each keystone's latest health_checks status.
  4. On ANY failure pages via nova_notify at level='critical' (OUTSIDE the digest
     path) and escalates a deduped INCIDENT to claude_queue.

Runs as its OWN launchd job (net.digitalnoise.core-liveness), deliberately NOT
under nova_scheduler — so the scheduler dying can't blind it — and writes its own
heartbeat into health_checks so an off-box watcher can detect THIS dying too.
The heartbeat's status is earned, not a literal (Chanos filter, 2026-10-15): 'up' only after a
full check found nothing, 'degraded' when it found and paged issues, 'down' when the check
itself failed or a page could not be delivered (see heartbeat_status()).
"""
import socket
import sys
import time

import psycopg2

sys.path.insert(0, __file__.rsplit("/", 1)[0])
try:
    from nova_notify import notify
except Exception:
    def notify(*a, **k):
        return False

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
INTERVAL_S = 120
HEALTH_STALE_S = 600       # health_checks not written in 10m -> health writer dead
CAPACITY_STALE_S = 1800    # capacity_snapshots not written in 30m -> capacity poller dead

# Keystones we TCP-probe directly. (name, host, port, health_checks service_name|None)
KEYSTONES = [
    ("PostgreSQL primary", "127.0.0.1", 5432, "postgresql"),
    ("PgBouncer",          "127.0.0.1", 5432, None),            # pgbouncer listen_port=5432 (was wrongly 6432 -> false 'down' alerts)
    ("Redis",              "127.0.0.1", 6379, "redis"),
    ("Memory server",      "127.0.0.1", 18790, "memory_server"),
    ("Gateway",            "192.168.1.2", 18792, "gateway"),   # migrated off .6 -> nova-core .2 (2026-07-14)
    ("Scheduler",          "127.0.0.1", 37460, "scheduler"),
    ("Inference router",   "192.168.1.2", 37475, None),
]


def tcp_up(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def page(conn, key, title, body):
    """Un-suppressable critical page + deduped claude_queue incident.
    Returns True when the page reached the notify bus (the queue row is a second, best-effort copy)."""
    delivered = False
    try:
        delivered = bool(notify(title, body=body, level="critical", category="core-liveness",
                                source="nova_core_liveness.py",
                                dedup_key=f"core-liveness:{key}:{int(time.time() // 3600)}"))
    except Exception as e:
        print(f"[core-liveness] notify failed: {e}", flush=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO claude_queue (session_id, status, priority, description, context)
                   SELECT %s, 'queued', 1, %s, %s
                   WHERE NOT EXISTS (
                       SELECT 1 FROM claude_queue
                       WHERE status IN ('queued','in_progress')
                         AND description LIKE %s)""",
                ("core-liveness", f"CORE LIVENESS: {title}", body, f"CORE LIVENESS: {title}%"))
    except Exception as e:
        print(f"[core-liveness] queue escalation failed: {e}", flush=True)
    return delivered


def ensure_session(conn):
    """claude_queue.session_id has an FK to claude_sessions — keep a stable row."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO claude_sessions (session_id, status) VALUES ('core-liveness','active') "
                "ON CONFLICT (session_id) DO NOTHING")
    except Exception:
        pass


def heartbeat_status(issues, undelivered, check_error=None):
    """What the heartbeat may honestly claim after one cycle -> (status, detail). Pure.
    The row claims "Nova's core is live AND this gate can still raise the alarm", so:
      down      the check itself failed, or a page it owed could not be delivered (a mute gate)
      degraded  the check ran and found keystone issues (all paged)
      up        the check ran end to end and found nothing
    (Chanos filter 2026-10-15: this row used to be a literal 'up' written whatever happened.)"""
    if check_error:
        return "down", f"check failed: {check_error}"[:500]
    if undelivered:
        return "down", f"{issues} issue(s), {undelivered} page(s) NOT delivered"
    if issues:
        return "degraded", f"{issues} issue(s), all paged"
    return "up", None


def heartbeat(conn, status="up", detail=None):
    """Write this gate's own health_checks row. Never raises."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO health_checks (service_name, node_name, checked_by, status, latency_ms, error_message)
                   VALUES ('core_liveness','mac-studio','core_liveness',%s,0,%s)""", (status, detail))
    except Exception:
        pass


def check(conn):
    issues = []

    # 1. Direct TCP probes of keystones (independent of health_checks).
    for name, host, port, _ in KEYSTONES:
        if not tcp_up(host, port):
            issues.append((f"tcp:{name}", f"Keystone DOWN: {name}",
                           f"{name} ({host}:{port}) is not accepting TCP connections."))

    with conn.cursor() as cur:
        # 2a. Watch the watchers — health_checks freshness.
        cur.execute("SELECT EXTRACT(EPOCH FROM now()-max(checked_at)) FROM health_checks")
        age = cur.fetchone()[0]
        if age is None or age > HEALTH_STALE_S:
            issues.append(("stale:health_checks", "health_checks pipeline STALE",
                           f"No health check written in {int(age or -1)}s (>{HEALTH_STALE_S}s) "
                           "— the health writer is likely dead. This is the silent-death class."))

        # 2b. capacity_snapshots freshness (the epic's exact bug).
        cur.execute("SELECT EXTRACT(EPOCH FROM now()-max(ts)) FROM capacity_snapshots")
        cage = cur.fetchone()[0]
        if cage is None or cage > CAPACITY_STALE_S:
            issues.append(("stale:capacity", "capacity poller STALE/dead",
                           f"capacity_snapshots last written {int(cage or -1)}s ago "
                           f"(>{CAPACITY_STALE_S}s) — the capacity poller is silently down (the bug that started the epic)."))

        # 3. Cross-check each keystone's latest health_checks status.
        for name, _h, _p, svc in KEYSTONES:
            if not svc:
                continue
            cur.execute(
                """SELECT status, checked_at FROM health_checks
                   WHERE service_name=%s ORDER BY checked_at DESC LIMIT 1""", (svc,))
            row = cur.fetchone()
            if row and row[0] not in ("up", "ok", "healthy"):
                issues.append((f"status:{svc}", f"Keystone health '{name}' = {row[0]}",
                               f"health_checks reports {svc} status='{row[0]}' (last {row[1]})."))

    return issues


def run_once(conn):
    ensure_session(conn)
    try:
        issues = check(conn)
    except Exception as e:
        # the gate could not look: say so in its own heartbeat (best effort), then let main() reconnect
        try:
            conn.rollback()
        except Exception:
            pass
        heartbeat(conn, *heartbeat_status(0, 0, f"{type(e).__name__}: {e}"))
        try:
            conn.commit()
        except Exception:
            pass
        raise
    undelivered = 0
    for key, title, body in issues:
        if not page(conn, key, title, body):
            undelivered += 1
    heartbeat(conn, *heartbeat_status(len(issues), undelivered))
    conn.commit()
    if issues:
        print(f"[core-liveness] {len(issues)} issue(s): " + "; ".join(t for _, t, _ in issues), flush=True)
    return len(issues)


def main():
    conn = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = psycopg2.connect(DSN)
                conn.autocommit = False
            run_once(conn)
        except Exception as e:
            print(f"[core-liveness] cycle error: {e}", flush=True)
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            conn = None
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    if "--once" in sys.argv:
        c = psycopg2.connect(DSN)
        n = run_once(c)
        print(f"[core-liveness] --once done, {n} issue(s)")
    else:
        main()
