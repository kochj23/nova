#!/usr/bin/env python3
"""
nova_incident_escalation.py — escalate CHRONIC unresolved incidents differently.

The failure mode this fixes (from the ops postmortem): a DSM issue paged 17x in
a week, on-call ack'd it every time, and nothing ever got permanently fixed — it
just aged off the 7-day window and came back. The existing recurrence detector
(nova_incident_lifecycle.detect_recurrence) already fires a *warning* once per
key per day ("this keeps happening"), but a warning is snoozable: it looks like
the same page, so it gets the same reflexive ack and the root cause never dies.

This module adds a LOUDER, DISTINCT escalation on top of that. When a
recurrence_key has paged N+ times over M+ days and is STILL active, but keeps
recurring anyway (auto-closed and/or ack'd each time, yet never actually fixed),
we emit ONE *critical* notify in its own category `incident_escalation`:

    "UNRESOLVED x17: {key} — needs a PERMANENT fix, not another ack"

so it reads as a different, higher-severity signal that demands a real fix rather
than one more snooze.

Deliberately conservative:
  - READ-ONLY. It never opens/closes/acks/edits an incident and never
    auto-remediates. It only reads telemetry.incidents and emits a notify.
  - Higher bar than the warning-level recurrence detector (default 8 pages over
    >=2 distinct days), so it fires for genuinely chronic problems, not one-off
    bursts.
  - It excludes its own meta-notifications (recurrence_key ending in
    ':incident_recurring' or ':incident_escalation') so escalations can't
    escalate themselves into a loop.
  - Dedup key is per-key-per-day and the page count is in the title, so a chronic
    problem produces at most one distinct critical page per day and the number
    visibly climbs — it is never the same snoozable page.

CLI:
  nova_incident_escalation.py --scan       # detect + emit critical escalations
  nova_incident_escalation.py --dry-run    # detect + print, emit nothing
  nova_incident_escalation.py --list       # print all recurring keys (any count)
  nova_incident_escalation.py --selftest   # synthetic chronic incident, no notify

Written by Jordan Koch.
"""
import argparse
import datetime
import sys

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# A recurrence_key must page at least this many times to escalate.
ESCALATE_THRESHOLD = 8
# ...spread across at least this many distinct calendar days (chronic, not a burst).
MIN_SPAN_DAYS = 2
# ...with its most recent page no older than this (still an active problem).
RECENT_DAYS = 3
# Lookback window for counting pages.
LOOKBACK_DAYS = 7
# recurrence_key suffixes we must never escalate (our own meta-notifications).
_META_SUFFIXES = (":incident_recurring", ":incident_escalation")


def _connect():
    try:
        import psycopg2
        return psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:
        print(f"incident_escalation: DB connect failed: {e}", file=sys.stderr)
        return None


def _notify(*a, **kw):
    try:
        from nova_notify import notify
        return notify(*a, **kw)
    except Exception:
        return False


# ── Pure classification (no DB, so it is trivially testable) ────────────────

def classify(rows,
             threshold: int = ESCALATE_THRESHOLD,
             min_span_days: int = MIN_SPAN_DAYS,
             recent_days: int = RECENT_DAYS,
             now: datetime.datetime | None = None) -> list[dict]:
    """Given aggregated per-recurrence_key rows, return the ones to escalate.

    Each input row is a dict with keys:
      recurrence_key, pages, resolved, acked, open_now,
      distinct_days, first_open, last_open
    (first_open/last_open are tz-aware datetimes.)

    An escalation is returned when the key is chronic AND still active:
      pages >= threshold  AND  distinct_days >= min_span_days
      AND last_open >= now - recent_days
    Meta-notification keys and NULL keys are excluded. Never raises on a bad row.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    recent_cutoff = now - datetime.timedelta(days=recent_days)
    out = []
    for r in rows:
        try:
            key = r.get("recurrence_key")
            if not key:
                continue
            if any(key.endswith(sfx) for sfx in _META_SUFFIXES):
                continue
            pages = int(r.get("pages") or 0)
            distinct_days = int(r.get("distinct_days") or 0)
            last_open = r.get("last_open")
            if pages < threshold:
                continue
            if distinct_days < min_span_days:
                continue
            if last_open is None or last_open < recent_cutoff:
                continue
            out.append(dict(r))
        except Exception:
            # a single malformed row must never sink the whole scan
            continue
    # loudest first
    out.sort(key=lambda x: int(x.get("pages") or 0), reverse=True)
    return out


def _reason(row: dict) -> str:
    """Human 'why this is unresolved' line for the notify body."""
    pages = int(row.get("pages") or 0)
    resolved = int(row.get("resolved") or 0)
    acked = int(row.get("acked") or 0)
    open_now = int(row.get("open_now") or 0)
    span = int(row.get("distinct_days") or 0)
    bits = [f"paged {pages}x over {span} day(s)"]
    if resolved:
        bits.append(f"auto-resolved {resolved}x but recurred anyway")
    if acked:
        bits.append(f"ack'd {acked}x — the ack isn't fixing it")
    if open_now:
        bits.append(f"{open_now} still open")
    return "; ".join(bits)


# ── DB read (read-only) ─────────────────────────────────────────────────────

def find_recurring(conn, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Read per-recurrence_key aggregates over the lookback window. Read-only."""
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT recurrence_key,
                       count(*)                                   AS pages,
                       count(*) FILTER (WHERE status='resolved')  AS resolved,
                       count(*) FILTER (WHERE acked_at IS NOT NULL) AS acked,
                       count(*) FILTER (WHERE status='open')      AS open_now,
                       count(DISTINCT date(opened_at))            AS distinct_days,
                       min(opened_at)                             AS first_open,
                       max(opened_at)                             AS last_open,
                       max(severity)                              AS severity,
                       (array_agg(host ORDER BY opened_at DESC))[1] AS host
                FROM telemetry.incidents
                WHERE recurrence_key IS NOT NULL
                  AND opened_at > now() - make_interval(days => %s)
                GROUP BY recurrence_key
                ORDER BY pages DESC
                """,
                (lookback_days,),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"incident_escalation: find_recurring failed: {e}", file=sys.stderr)
        return []


def scan(conn, emit: bool = True, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Find chronic-unresolved keys and (optionally) emit one critical page each.
    Returns the list of escalated rows. Read-only against the DB either way."""
    rows = find_recurring(conn, lookback_days)
    hits = classify(rows)
    if not emit:
        return hits
    today = datetime.date.today().isoformat()
    for row in hits:
        key = row["recurrence_key"]
        pages = int(row.get("pages") or 0)
        host = row.get("host")
        _notify(
            f"UNRESOLVED x{pages}: {key} needs a PERMANENT fix",
            body=(f"Recurring incident '{key}'"
                  + (f" on {host}" if host else "")
                  + f" has {_reason(row)}. This is not a one-off — it keeps "
                    f"coming back and the current response (ack/auto-close) is "
                    f"not fixing the root cause. Escalating: needs a permanent "
                    f"fix, not another snoozed page."),
            level="critical",
            category="incident_escalation",
            source="nova_incident_escalation.py",
            # distinct per key per day; the climbing count in the title keeps it
            # from ever being the identical snoozable page.
            dedup_key=f"escalation-{key}-{today}",
            meta={"recurrence_key": key, "pages": pages,
                  "resolved": int(row.get("resolved") or 0),
                  "acked": int(row.get("acked") or 0),
                  "open_now": int(row.get("open_now") or 0),
                  "distinct_days": int(row.get("distinct_days") or 0)},
        )
    return hits


# ── Self-test (creates + cleans up synthetic rows; emits NOTHING) ───────────

def _selftest(conn) -> bool:
    ok = True
    key = "selftest-escalation-host:selftest_cat"
    ids = []
    try:
        with conn.cursor() as cur:
            # 9 synthetic opens spread across 4 distinct days, newest = today.
            for d in (5, 4, 3, 2, 2, 1, 1, 0, 0):
                cur.execute(
                    """INSERT INTO telemetry.incidents
                       (status, severity, host, title, recurrence_key,
                        member_count, opened_at, updated_at, resolved_at)
                       VALUES ('resolved','warning','selftest-escalation-host',
                               'SELFTEST chronic', %s, 1,
                               now() - make_interval(days => %s),
                               now() - make_interval(days => %s),
                               now() - make_interval(days => %s))
                       RETURNING id""",
                    (key, d, d, d),
                )
                ids.append(cur.fetchone()[0])
        conn.commit()
        print(f"  created {len(ids)} synthetic chronic incidents for key {key}")

        rows = find_recurring(conn, LOOKBACK_DAYS)
        hits = classify(rows)
        mine = [h for h in hits if h["recurrence_key"] == key]
        if not mine:
            print("  FAIL: chronic synthetic key was not escalated"); ok = False
        else:
            h = mine[0]
            print(f"  escalated: pages={h['pages']} distinct_days={h['distinct_days']} "
                  f"reason='{_reason(h)}'")
            if int(h["pages"]) < ESCALATE_THRESHOLD:
                print("  FAIL: page count below threshold"); ok = False
            else:
                print("  PASS")
    except Exception as e:
        print(f"  selftest error: {e}"); ok = False
    finally:
        try:
            with conn.cursor() as cur:
                for i in ids:
                    cur.execute("DELETE FROM telemetry.incidents WHERE id=%s", (i,))
            conn.commit()
            print("  cleaned up synthetic rows")
        except Exception as e:
            conn.rollback()
            print(f"  cleanup error: {e}")
    return ok


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Escalate chronic unresolved incidents")
    ap.add_argument("--scan", action="store_true", help="detect + emit critical escalations")
    ap.add_argument("--dry-run", action="store_true", help="detect + print, emit nothing")
    ap.add_argument("--list", action="store_true", help="print every recurring key")
    ap.add_argument("--days", type=int, default=LOOKBACK_DAYS, help="lookback window (days)")
    ap.add_argument("--selftest", action="store_true", help="synthetic chronic test (no notify)")
    a = ap.parse_args()

    conn = _connect()
    if conn is None:
        sys.exit(1)
    try:
        if a.selftest:
            sys.exit(0 if _selftest(conn) else 1)
        if a.list:
            for r in find_recurring(conn, a.days):
                print(f"{int(r['pages']):>4}x  {r['recurrence_key']:<40} "
                      f"days={r['distinct_days']} resolved={r['resolved']} "
                      f"acked={r['acked']} open={r['open_now']} last={r['last_open']}")
            sys.exit(0)
        hits = scan(conn, emit=a.scan and not a.dry_run, lookback_days=a.days)
        if hits:
            verb = "WOULD escalate" if (a.dry_run or not a.scan) else "escalated"
            print(f"{verb} {len(hits)} chronic-unresolved incident pattern(s):")
            for h in hits:
                print(f"  x{h['pages']}  {h['recurrence_key']}  ({_reason(h)})")
        else:
            print("no chronic-unresolved incident patterns crossed the escalation bar")
        sys.exit(0)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
