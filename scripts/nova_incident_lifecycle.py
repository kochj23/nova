#!/usr/bin/env python3
"""
nova_incident_lifecycle.py — close the loop on Nova's incidents.

The correlator (nova_correlator.py) OPENS incidents and folds symptoms under a
root cause, but nothing ever CLOSES them or LEARNS from them. An incident stays
'open' forever once its alert storm dies down, and the same nightly failure
(e.g. the GPU wedge) reopens as a brand-new surprise every single time.

This module adds the back half of the lifecycle, all deterministic so it works
even when the LLM/GPU it diagnoses is down:

  - SCHEMA          idempotent ALTERs for ack/resolution/recurrence/MTTA/MTTR columns.
  - auto_close_resolved(conn)   — an open incident with no new member events in the
                    last N minutes is considered over: resolve it, stamp MTTR, and
                    emit ONE info notify per incident (deduped). Returns count closed.
  - acknowledge(conn, id, who)  — stamp acked_at + MTTA when an on-call ack's it.
  - detect_recurrence(conn, id) — build recurrence_key "{host}:{root_category}";
                    if that key has opened >= N times in 7d, flag it recurring and
                    warn ONCE per key/day so a chronic problem reads as a PATTERN
                    that needs a permanent fix, not a fresh page each night.
  - stats(conn)     — MTTA/MTTR rollups for a digest.

Standalone module: it never edits the notifier, correlator, notify shim, or the
scheduler. It emits via nova_notify.notify (never hardcodes Slack) and never
raises into a caller — failures degrade to no-op.

CLI:
  nova_incident_lifecycle.py --migrate            # apply schema (idempotent)
  nova_incident_lifecycle.py --sweep [--minutes N]# auto-close + recurrence scan
  nova_incident_lifecycle.py --ack <id> [--who x] # acknowledge an incident
  nova_incident_lifecycle.py --stats              # print MTTA/MTTR rollup
  nova_incident_lifecycle.py --selftest           # synthetic stale-incident test
"""
import argparse
import sys

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# An open incident whose newest member event is older than this is "over".
DEFAULT_IDLE_MINUTES = 30
# How many opens of the same recurrence_key within the lookback flag a PATTERN.
RECURRENCE_THRESHOLD = 3
RECURRENCE_LOOKBACK_DAYS = 7


def _connect():
    """Return a psycopg2 connection, or None (never raises)."""
    try:
        import psycopg2
        return psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:
        print(f"incident_lifecycle: DB connect failed: {e}", file=sys.stderr)
        return None


def _notify(*a, **kw):
    """Emit via the central bus; tolerate nova_notify being unimportable."""
    try:
        from nova_notify import notify
        return notify(*a, **kw)
    except Exception:
        return False


# ── Schema ────────────────────────────────────────────────────────────────

def migrate(conn) -> bool:
    """Add lifecycle columns. Idempotent (IF NOT EXISTS). Returns True on success."""
    ddl = """
    ALTER TABLE telemetry.incidents
        ADD COLUMN IF NOT EXISTS acked_at      timestamptz,
        ADD COLUMN IF NOT EXISTS acked_by      text,
        ADD COLUMN IF NOT EXISTS resolution    text,
        ADD COLUMN IF NOT EXISTS recurrence_key text,
        ADD COLUMN IF NOT EXISTS mtta_s        int,
        ADD COLUMN IF NOT EXISTS mttr_s        int;
    -- index to make the per-key/per-7d recurrence count cheap
    CREATE INDEX IF NOT EXISTS incidents_recurrence
        ON telemetry.incidents (recurrence_key, opened_at)
        WHERE recurrence_key IS NOT NULL;
    """
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"incident_lifecycle: migrate failed: {e}", file=sys.stderr)
        return False


# ── Auto-close ────────────────────────────────────────────────────────────

def auto_close_resolved(conn, idle_minutes: int = DEFAULT_IDLE_MINUTES) -> int:
    """Resolve open incidents that have gone quiet.

    An incident is "quiet" when no member event has arrived in `idle_minutes`.
    We look at the newest member event in telemetry.events (falling back to the
    incident's own updated_at when it somehow has no members). For each, set
    status='resolved', resolved_at=now(), mttr_s = resolved_at - opened_at, and
    emit ONE info notify (deduped per incident so a re-run can't double-page).

    Returns the number of incidents closed.
    """
    closed = 0
    try:
        with conn.cursor() as cur:
            # Newest member-event time per open incident; coalesce to updated_at
            # so an incident with no surviving member rows can still age out.
            cur.execute(
                """
                SELECT i.id, i.host, i.title, i.opened_at,
                       EXTRACT(EPOCH FROM (now() - i.opened_at))::int AS mttr_s
                FROM telemetry.incidents i
                LEFT JOIN LATERAL (
                    SELECT max(ts) AS last_ts
                    FROM telemetry.events e
                    WHERE e.incident_id = i.id
                ) m ON true
                WHERE i.status = 'open'
                  AND COALESCE(m.last_ts, i.updated_at)
                      < now() - make_interval(mins => %s)
                ORDER BY i.opened_at ASC
                """,
                (idle_minutes,),
            )
            stale = cur.fetchall()

            for inc_id, host, title, opened_at, mttr_s in stale:
                cur.execute(
                    """
                    UPDATE telemetry.incidents
                    SET status = 'resolved',
                        resolved_at = now(),
                        mttr_s = EXTRACT(EPOCH FROM (now() - opened_at))::int,
                        updated_at = now()
                    WHERE id = %s AND status = 'open'
                    RETURNING mttr_s
                    """,
                    (inc_id,),
                )
                row = cur.fetchone()
                if not row:  # lost a race; already resolved elsewhere
                    continue
                mttr = row[0] if row[0] is not None else (mttr_s or 0)
                conn.commit()
                closed += 1
                mins = round(mttr / 60.0, 1)
                _notify(
                    f"Incident #{inc_id} resolved after {mins}m",
                    body=(f"{title}" + (f" (host {host})" if host else "")
                          + f" — auto-closed: no new events in {idle_minutes}m. "
                            f"MTTR {mins}m."),
                    level="info",
                    category="incident",
                    source="nova_incident_lifecycle.py",
                    dedup_key=f"incident-resolved-{inc_id}",
                    meta={"incident_id": inc_id, "host": host, "mttr_s": mttr},
                )
        return closed
    except Exception as e:
        conn.rollback()
        print(f"incident_lifecycle: auto_close_resolved failed: {e}", file=sys.stderr)
        return closed


# ── Acknowledge ───────────────────────────────────────────────────────────

def acknowledge(conn, incident_id: int, who: str) -> bool:
    """Record that `who` acknowledged the incident; stamp acked_at + MTTA.

    MTTA = time from open to first ack. Idempotent on acked_at: a second ack
    does not overwrite the first (so MTTA stays the time-to-FIRST-ack).
    Returns True if this call set the ack.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE telemetry.incidents
                SET acked_at = now(),
                    acked_by = %s,
                    mtta_s   = EXTRACT(EPOCH FROM (now() - opened_at))::int,
                    updated_at = now()
                WHERE id = %s AND acked_at IS NULL
                RETURNING mtta_s
                """,
                (who, incident_id),
            )
            row = cur.fetchone()
        conn.commit()
        if row:
            _notify(
                f"Incident #{incident_id} acknowledged by {who}",
                body=f"MTTA {round((row[0] or 0) / 60.0, 1)}m.",
                level="info",
                category="incident",
                source="nova_incident_lifecycle.py",
                dedup_key=f"incident-acked-{incident_id}",
                meta={"incident_id": incident_id, "acked_by": who},
            )
            return True
        return False
    except Exception as e:
        conn.rollback()
        print(f"incident_lifecycle: acknowledge failed: {e}", file=sys.stderr)
        return False


# ── Recurrence detection ──────────────────────────────────────────────────

def detect_recurrence(conn, incident_id: int) -> str | None:
    """Stamp a recurrence_key on the incident and flag chronic patterns.

    recurrence_key = "{host}:{root_category}" (root category looked up from the
    incident's root_event). If that key has opened >= RECURRENCE_THRESHOLD times
    in the last RECURRENCE_LOOKBACK_DAYS, it's not a one-off — it's a pattern.
    We warn ONCE per key per day (dedup_key includes today's date) so the chronic
    nightly GPU wedge reads as "this keeps happening, fix it for real" instead of
    a fresh surprise every night.

    Returns the recurrence_key (or None if it couldn't be derived).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.host,
                       (SELECT category FROM telemetry.events WHERE id = i.root_event)
                FROM telemetry.incidents i
                WHERE i.id = %s
                """,
                (incident_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            host, root_cat = row
            if root_cat in ("incident_recurring", "incident"):
                # 2026-10-01: an incident rooted in one of OUR OWN notifications is the loop
                # (detector -> warning -> event -> incident -> detector). Never key on it.
                return None
            key = f"{host or 'unknown'}:{root_cat or 'uncategorized'}"

            cur.execute(
                "UPDATE telemetry.incidents SET recurrence_key = %s WHERE id = %s",
                (key, incident_id),
            )

            # How many incidents share this key in the lookback window?
            cur.execute(
                """
                SELECT count(*)
                FROM telemetry.incidents
                WHERE recurrence_key = %s
                  AND opened_at > now() - make_interval(days => %s)
                """,
                (key, RECURRENCE_LOOKBACK_DAYS),
            )
            n = cur.fetchone()[0]
        conn.commit()

        if n >= RECURRENCE_THRESHOLD:
            import datetime
            today = datetime.date.today().isoformat()
            _notify(
                f"Recurring incident pattern: {key}",
                body=(f"Pattern: {key} has recurred {n} times in "
                      f"{RECURRENCE_LOOKBACK_DAYS}d — needs a permanent fix, "
                      f"not another page."),
                level="warning",
                category="incident_recurring",
                source="nova_incident_lifecycle.py",
                # one warning per key per day
                dedup_key=f"recurring-{key}-{today}",
                meta={"recurrence_key": key, "count_7d": n,
                      "incident_id": incident_id},
            )
        return key
    except Exception as e:
        conn.rollback()
        print(f"incident_lifecycle: detect_recurrence failed: {e}", file=sys.stderr)
        return None


# ── Stats / digest ────────────────────────────────────────────────────────

def stats(conn) -> dict:
    """MTTA/MTTR rollups for a digest. Returns a dict (empty on failure)."""
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                  count(*) FILTER (WHERE status = 'open')                  AS open_now,
                  count(*) FILTER (WHERE status = 'resolved'
                       AND resolved_at > now() - interval '24 hours')      AS resolved_24h,
                  count(*) FILTER (WHERE opened_at > now() - interval '24 hours') AS opened_24h,
                  round(avg(mtta_s) FILTER (WHERE acked_at IS NOT NULL
                       AND opened_at > now() - interval '7 days') / 60.0, 1) AS avg_mtta_min_7d,
                  round(avg(mttr_s) FILTER (WHERE status = 'resolved'
                       AND opened_at > now() - interval '7 days') / 60.0, 1) AS avg_mttr_min_7d,
                  count(DISTINCT recurrence_key) FILTER (WHERE recurrence_key IS NOT NULL
                       AND opened_at > now() - interval '7 days')          AS distinct_patterns_7d
                FROM telemetry.incidents
                """
            )
            row = cur.fetchone() or {}

            cur.execute(
                """
                SELECT recurrence_key, count(*) AS n
                FROM telemetry.incidents
                WHERE recurrence_key IS NOT NULL
                  AND opened_at > now() - make_interval(days => %s)
                GROUP BY recurrence_key
                HAVING count(*) >= %s
                ORDER BY n DESC
                LIMIT 10
                """,
                (RECURRENCE_LOOKBACK_DAYS, RECURRENCE_THRESHOLD),
            )
            top = [{"key": r["recurrence_key"], "count": r["n"]} for r in cur.fetchall()]
        result = dict(row)
        result["top_recurring"] = top
        return result
    except Exception as e:
        print(f"incident_lifecycle: stats failed: {e}", file=sys.stderr)
        return {}


def auto_close_public(conn) -> int:
    """Age-close stale rows in public.incidents (Big Brother / Wazuh / postmortem
    write here; nothing was closing them, so #508 zombies piled up). This table
    has no updated_at and Big Brother creates a NEW row per occurrence, so an old
    started_at == stale. Policy (Jordan-approved, #508):
      - operational (non-security): critical 24h, else 6h
      - security (title ~ 'security'): a long 7-day review window, so genuinely
        stale ones eventually clear while fresh ones stay visible for ack.
    """
    closed = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.incidents
                   SET status='resolved', resolved_at=now(),
                       root_cause = COALESCE(root_cause,'') ||
                           ' [auto-closed by incident_lifecycle: stale past policy window]'
                   WHERE status='open' AND (
                       (title ILIKE '%security%' AND started_at < now() - interval '7 days')
                    OR (title NOT ILIKE '%security%' AND severity='critical'
                            AND started_at < now() - interval '24 hours')
                    OR (title NOT ILIKE '%security%' AND COALESCE(severity,'warning') <> 'critical'
                            AND started_at < now() - interval '6 hours')
                   )""")
            closed = cur.rowcount or 0
        conn.commit()
    except Exception as e:
        print(f"incident_lifecycle: auto_close_public failed: {e}", file=sys.stderr)
        conn.rollback()
    return closed


def sweep(conn, idle_minutes: int = DEFAULT_IDLE_MINUTES) -> dict:
    """One periodic pass: detect recurrence on still-open incidents, then close
    the quiet ones. Recurrence runs first so a closing incident still gets its
    key stamped. Returns a small summary dict. This is the function a scheduler
    task or the daemon loop should call."""
    summary = {"closed": 0, "recurrence_scanned": 0, "public_closed": 0}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM telemetry.incidents WHERE status='open'")
            open_ids = [r[0] for r in cur.fetchall()]
        for inc_id in open_ids:
            detect_recurrence(conn, inc_id)
            summary["recurrence_scanned"] += 1
        summary["closed"] = auto_close_resolved(conn, idle_minutes)
        summary["public_closed"] = auto_close_public(conn)
    except Exception as e:
        print(f"incident_lifecycle: sweep failed: {e}", file=sys.stderr)
    return summary


# ── Self-test ─────────────────────────────────────────────────────────────

def _selftest(conn) -> bool:
    """Create a synthetic stale incident, run auto_close, confirm it closes,
    then clean everything up. Returns True on pass."""
    ok = True
    test_host = "selftest-lifecycle-host"
    inc_id = None
    ev_id = None
    try:
        with conn.cursor() as cur:
            # a root event whose ts is old enough to be 'quiet'
            cur.execute(
                """
                INSERT INTO telemetry.events
                  (ts, source, level, category, title, body, status)
                VALUES (now() - interval '90 minutes',
                        'nova_incident_lifecycle.py', 'warning', 'gpu',
                        'SELFTEST synthetic root', 'self-test', 'sent')
                RETURNING id
                """
            )
            ev_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO telemetry.incidents
                  (status, severity, host, title, root_event, member_count,
                   opened_at, updated_at)
                VALUES ('open', 'warning', %s, 'SELFTEST stale incident', %s, 1,
                        now() - interval '90 minutes',
                        now() - interval '90 minutes')
                RETURNING id
                """,
                (test_host, ev_id),
            )
            inc_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE telemetry.events SET incident_id=%s, corr_role='root' WHERE id=%s",
                (inc_id, ev_id),
            )
        conn.commit()
        print(f"  created synthetic incident #{inc_id} (event #{ev_id}), 90m old")

        # recurrence stamp + close
        key = detect_recurrence(conn, inc_id)
        print(f"  recurrence_key = {key}")
        n = auto_close_resolved(conn, idle_minutes=30)
        print(f"  auto_close_resolved closed {n} incident(s)")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, resolved_at IS NOT NULL, mttr_s, recurrence_key "
                "FROM telemetry.incidents WHERE id=%s", (inc_id,))
            status, has_resolved, mttr_s, rkey = cur.fetchone()
        print(f"  -> status={status} resolved_set={has_resolved} mttr_s={mttr_s} key={rkey}")
        if status != "resolved" or not has_resolved or mttr_s is None:
            print("  FAIL: incident did not close correctly")
            ok = False
        elif mttr_s < 60 * 80:  # ~90m old, expect well over 80m
            print(f"  FAIL: mttr_s={mttr_s} too small for a 90m incident")
            ok = False
        else:
            print("  PASS")
    except Exception as e:
        print(f"  selftest error: {e}")
        ok = False
    finally:
        # cleanup — remove synthetic rows no matter what
        try:
            with conn.cursor() as cur:
                if inc_id:
                    cur.execute("DELETE FROM telemetry.incidents WHERE id=%s", (inc_id,))
                if ev_id:
                    cur.execute("DELETE FROM telemetry.events WHERE id=%s", (ev_id,))
            conn.commit()
            print("  cleaned up synthetic rows")
        except Exception as e:
            conn.rollback()
            print(f"  cleanup error: {e}")
    return ok


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Nova incident lifecycle ops")
    ap.add_argument("--migrate", action="store_true", help="apply schema (idempotent)")
    ap.add_argument("--sweep", action="store_true", help="recurrence scan + auto-close")
    ap.add_argument("--minutes", type=int, default=DEFAULT_IDLE_MINUTES,
                    help="idle minutes before auto-close")
    ap.add_argument("--ack", type=int, metavar="ID", help="acknowledge incident ID")
    ap.add_argument("--who", default="cli", help="who is acknowledging")
    ap.add_argument("--stats", action="store_true", help="print MTTA/MTTR rollup")
    ap.add_argument("--selftest", action="store_true", help="synthetic stale-incident test")
    a = ap.parse_args()

    conn = _connect()
    if conn is None:
        sys.exit(1)

    try:
        # always ensure schema exists before any op that needs the new columns
        if not migrate(conn):
            sys.exit(1)

        if a.selftest:
            sys.exit(0 if _selftest(conn) else 1)
        if a.ack is not None:
            ok = acknowledge(conn, a.ack, a.who)
            print("acked" if ok else "no-op (already acked or missing)")
            sys.exit(0 if ok else 1)
        if a.stats:
            import json
            print(json.dumps(stats(conn), indent=2, default=str))
            sys.exit(0)
        if a.sweep:
            s = sweep(conn, a.minutes)
            print(f"sweep: closed {s['closed']}, recurrence-scanned "
                  f"{s['recurrence_scanned']}, public-closed {s.get('public_closed', 0)}")
            sys.exit(0)
        if a.migrate:
            print("schema applied")
            sys.exit(0)
        ap.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
