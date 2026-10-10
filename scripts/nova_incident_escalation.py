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

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")

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

# ── Re-escalation throttle (the noise fix) ──────────────────────────────────
# A still-unresolved chronic pattern must NOT re-page every hourly run. Once it
# has been escalated, the next page is withheld until this much time has elapsed
# since the last page — the gap grows with each escalation: 1st page is free,
# then +1h, then +4h, then +24h and daily thereafter. Per-pattern state lives in
# public.escalation_state (nova_ops), so the backoff is crash-safe and shared by
# every host that runs this scan.
BACKOFF_STEPS_HOURS = (1, 4, 24)  # required gap before the 2nd, 3rd, 4th+ page
# Once an incident is acknowledged (telemetry.incidents.acked_at, or an ack
# recorded directly on escalation_state), stop re-paging every cycle — drop to at
# most one quiet daily reminder until it changes state or resolves.
ACK_REMINDER_HOURS = 24
# ...UNLESS it materially worsens while acked (page count climbs this much since
# the last page): a stale ack must not silence a problem that is blowing up.
ACK_WORSEN_DELTA = 10
# Name of the crash-safe per-pattern throttle table (central, in nova_ops).
STATE_TABLE = "public.escalation_state"


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


def _required_gap_hours(escalate_count: int) -> int:
    """Hours that must pass since the last page before the next one is allowed.
    escalate_count is how many times we have ALREADY paged this pattern (>=1)."""
    idx = min(max(escalate_count, 1), len(BACKOFF_STEPS_HOURS)) - 1
    return BACKOFF_STEPS_HOURS[idx]


def escalation_decision(state: dict | None, hit: dict,
                        now: datetime.datetime | None = None) -> dict:
    """Decide whether an ALREADY-classified chronic pattern may page right now.

    This is the noise throttle: classify() still decides WHAT is a chronic
    unresolved pattern; this only decides how OFTEN the same pattern re-pages.
    Pure (no DB, no notify) so it is trivially testable.

      state : the pattern's row from escalation_state, or None if never paged.
      hit   : one classify() row (may carry 'last_acked_at' from the DB).

    Returns dict(emit: bool, kind: str, new_count: int, clear_ack: bool):
      kind ∈ {first, backoff-due, backoff-hold,
              ack-daily-reminder, ack-hold, worsened-despite-ack}
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    pages = int(hit.get("pages") or 0)

    # Never paged before -> this is a genuinely new escalation: always emit.
    if not state:
        return {"emit": True, "kind": "first", "new_count": 1, "clear_ack": False}

    count = int(state.get("escalate_count") or 1)
    last_ts = state.get("last_escalated_ts")
    last_pages = int(state.get("last_pages") or 0)
    first_ts = state.get("first_escalated_ts")

    # Acknowledged?  Either an ack recorded on our own state row, OR the
    # underlying incident was acked (telemetry.incidents.acked_at) at/after we
    # first escalated it — i.e. on-call has seen and owned this escalation.
    inc_acked_at = hit.get("last_acked_at")
    acked = bool(state.get("acked")) or (
        inc_acked_at is not None and first_ts is not None and inc_acked_at >= first_ts)

    if acked:
        # A stale ack must not muzzle a problem that is materially worsening.
        if pages - last_pages >= ACK_WORSEN_DELTA:
            return {"emit": True, "kind": "worsened-despite-ack",
                    "new_count": count + 1, "clear_ack": True}
        # Otherwise: at most one quiet daily reminder while acknowledged.
        if last_ts is None or (now - last_ts) >= datetime.timedelta(hours=ACK_REMINDER_HOURS):
            return {"emit": True, "kind": "ack-daily-reminder",
                    "new_count": count, "clear_ack": False}
        return {"emit": False, "kind": "ack-hold", "new_count": count, "clear_ack": False}

    # Not acked -> escalating backoff since the last page.
    gap = datetime.timedelta(hours=_required_gap_hours(count))
    if last_ts is None or (now - last_ts) >= gap:
        return {"emit": True, "kind": "backoff-due",
                "new_count": count + 1, "clear_ack": False}
    return {"emit": False, "kind": "backoff-hold", "new_count": count, "clear_ack": False}


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
                       max(acked_at)                              AS last_acked_at,
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


# ── Backoff state (the only thing this module ever WRITES) ──────────────────
# public.escalation_state records, per recurrence_key, when we last paged and how
# many times, plus an optional ack. It is the ONLY write this module makes; it
# never touches telemetry.incidents. Central in nova_ops so backoff/ack survive a
# crash and are shared by every host running the scan.

def _ensure_state_table(conn) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
                    recurrence_key     text PRIMARY KEY,
                    first_escalated_ts timestamptz NOT NULL DEFAULT now(),
                    last_escalated_ts  timestamptz NOT NULL DEFAULT now(),
                    escalate_count     integer     NOT NULL DEFAULT 1,
                    last_pages         integer     NOT NULL DEFAULT 0,
                    acked              boolean     NOT NULL DEFAULT false,
                    acked_at           timestamptz,
                    acked_by           text,
                    updated_at         timestamptz NOT NULL DEFAULT now()
                )""")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"incident_escalation: ensure_state_table failed: {e}", file=sys.stderr)


def _load_states(conn, keys) -> dict:
    """recurrence_key -> state row. Tolerant of a missing table (dry-run)."""
    keys = list(keys)
    if not keys:
        return {}
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {STATE_TABLE} WHERE recurrence_key = ANY(%s)", (keys,))
            return {r["recurrence_key"]: dict(r) for r in cur.fetchall()}
    except Exception as e:
        conn.rollback()
        print(f"incident_escalation: load_states failed: {e}", file=sys.stderr)
        return {}


def _record_escalation(conn, key: str, now, new_count: int, pages: int,
                       clear_ack: bool) -> None:
    """Upsert the pattern's throttle row after we page it. Clears ack when the
    page fired because the problem worsened past a stale ack."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {STATE_TABLE}
                        (recurrence_key, first_escalated_ts, last_escalated_ts,
                         escalate_count, last_pages, updated_at)
                    VALUES (%(k)s, %(now)s, %(now)s, %(cnt)s, %(pages)s, now())
                    ON CONFLICT (recurrence_key) DO UPDATE SET
                        last_escalated_ts = EXCLUDED.last_escalated_ts,
                        escalate_count    = %(cnt)s,
                        last_pages        = EXCLUDED.last_pages,
                        acked    = CASE WHEN %(clr)s THEN false
                                        ELSE {STATE_TABLE}.acked END,
                        acked_at = CASE WHEN %(clr)s THEN NULL
                                        ELSE {STATE_TABLE}.acked_at END,
                        acked_by = CASE WHEN %(clr)s THEN NULL
                                        ELSE {STATE_TABLE}.acked_by END,
                        updated_at = now()""",
                {"k": key, "now": now, "cnt": new_count, "pages": pages,
                 "clr": clear_ack})
    except Exception as e:
        conn.rollback()
        print(f"incident_escalation: record_escalation failed: {e}", file=sys.stderr)


def _forget_absent(conn, present_keys) -> None:
    """Drop throttle rows for patterns that no longer cross the escalation bar —
    they resolved / went quiet, so a future recurrence starts fresh (and any old
    ack is cleared). This is how ack-hold ends 'when it changes state or resolves'."""
    try:
        present_keys = list(present_keys)
        with conn.cursor() as cur:
            if present_keys:
                cur.execute(
                    f"DELETE FROM {STATE_TABLE} WHERE NOT (recurrence_key = ANY(%s))",
                    (present_keys,))
            else:
                cur.execute(f"DELETE FROM {STATE_TABLE}")
    except Exception as e:
        conn.rollback()
        print(f"incident_escalation: forget_absent failed: {e}", file=sys.stderr)


def _emit_escalation(row: dict, decision: dict, now) -> None:
    """Fire ONE critical page for a chronic pattern, phrased for the decision kind."""
    key = row["recurrence_key"]
    pages = int(row.get("pages") or 0)
    host = row.get("host")
    kind = decision["kind"]
    on = f" on {host}" if host else ""
    if kind == "worsened-despite-ack":
        title = f"WORSENING x{pages} (ack'd but still growing): {key}"
        lead = (f"Acknowledged recurring incident '{key}'{on} has WORSENED since "
                f"it was ack'd ({_reason(row)}). The ack is stale — re-escalating.")
    elif kind == "ack-daily-reminder":
        title = f"STILL UNRESOLVED x{pages} (ack'd, daily reminder): {key}"
        lead = (f"Acknowledged recurring incident '{key}'{on} is still unresolved "
                f"({_reason(row)}). Daily reminder until it is permanently fixed "
                f"or resolves — not re-paging every cycle.")
    else:  # first / backoff-due
        title = f"UNRESOLVED x{pages}: {key} needs a PERMANENT fix"
        lead = (f"Recurring incident '{key}'{on} has {_reason(row)}. This is not a "
                f"one-off — it keeps coming back and the current response "
                f"(ack/auto-close) is not fixing the root cause. Escalating: needs "
                f"a permanent fix, not another snoozed page.")
    today = now.date().isoformat()
    _notify(
        title,
        body=lead,
        level="critical",
        category="incident_escalation",
        source="nova_incident_escalation.py",
        # Distinct per pattern, per day, per escalation-count so each backoff step
        # (and each daily reminder) is its own page — never the identical snoozable
        # one, and never the every-cycle flood the throttle now prevents.
        dedup_key=f"escalation-{key}-{today}-{decision['new_count']}",
        meta={"recurrence_key": key, "pages": pages,
              "resolved": int(row.get("resolved") or 0),
              "acked": int(row.get("acked") or 0),
              "open_now": int(row.get("open_now") or 0),
              "distinct_days": int(row.get("distinct_days") or 0),
              "escalation_kind": kind,
              "escalation_count": decision["new_count"]},
    )


def scan(conn, emit: bool = True, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Find chronic-unresolved patterns and page each one on a BACKOFF, not every
    run. Detection (classify) is unchanged; this only throttles REPEAT pages of
    the same still-open pattern and honours acks. Returns every classified hit,
    each annotated with '_decision' and '_emitted'. The only DB write is to
    escalation_state (never to telemetry.incidents)."""
    rows = find_recurring(conn, lookback_days)
    hits = classify(rows)
    keys = [h["recurrence_key"] for h in hits]
    now = datetime.datetime.now(datetime.timezone.utc)

    if emit:
        _ensure_state_table(conn)
    states = _load_states(conn, keys)

    for row in hits:
        decision = escalation_decision(states.get(row["recurrence_key"]), row, now)
        row["_decision"] = decision
        row["_emitted"] = bool(decision["emit"])
        if emit and decision["emit"]:
            _emit_escalation(row, decision, now)
            _record_escalation(conn, row["recurrence_key"], now,
                               decision["new_count"], int(row.get("pages") or 0),
                               clear_ack=decision["clear_ack"])

    if emit:
        # Patterns that no longer cross the bar have resolved/gone quiet: forget
        # them so a future recurrence starts fresh and any stale ack is cleared.
        _forget_absent(conn, keys)
        conn.commit()
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
            live = a.scan and not a.dry_run
            paged = [h for h in hits if h.get("_emitted")]
            held = [h for h in hits if not h.get("_emitted")]
            verb = "PAGED" if live else "WOULD page"
            print(f"{len(hits)} chronic-unresolved pattern(s): "
                  f"{verb} {len(paged)}, throttled {len(held)}")
            for h in hits:
                d = h.get("_decision") or {}
                mark = "PAGE" if h.get("_emitted") else "hold"
                print(f"  [{mark}] x{h['pages']:<4} {h['recurrence_key']:<40} "
                      f"{d.get('kind','?'):<20} ({_reason(h)})")
        else:
            print("no chronic-unresolved incident patterns crossed the escalation bar")
        sys.exit(0)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
