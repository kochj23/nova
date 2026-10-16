#!/usr/bin/env python3
"""nova_ledger_aging.py — Nova closes her own never-closing ledgers, always with a recorded reason.

Little Mister, 2026-10-15 (plan item #25): four ledgers only ever grew — hotwash (601 open),
unexplained_events / Buick 8 (118 open), postmortem_actions (93 open) and tinker_log
(31 'pending_human'). Nothing in them was waiting on Jordan; they were waiting on nobody.
Nova owns them, so Nova ages them out — never a delete, every closure carries its reason:

  hotwash             fix_kind='none' rows (the roll-up never turns them into a proposal) close after
                      1 day: "AAR recorded; nothing to roll up". Any row still open after 7 days (the
                      weekly roll-up only reads 8 days, so older rows were orphaned for ever) closes as
                      rolled_up: "aged out". -> status rolled_up + closed_reason.
  unexplained_events  an entry not seen for 7 days goes DORMANT. Its cause stays 'unknown' (the
                      Buick 8 rule: no cause without evidence) and log_unexplained() reopens it if it
                      happens again. -> status dormant + closed_reason.
  postmortem_actions  an action whose claude_queue item is still untouched after 21 days is cancelled
                      and the queue item aged out ("the fact will be re-checked by the next
                      postmortem"); nova_sre_postmortem re-files it only if the fact still holds.
  tinker_log          proposal_status is a copy taken when the proposal was filed; it is refreshed
                      from coagency_proposals.status (all 31 'pending_human' rows had long since
                      been decided there).

CLI: --run [--dry-run]   (scheduler-core, daily)      Prints counts per ledger.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOTWASH_NONE_DAYS = 1
HOTWASH_MAX_DAYS = 7
BUICK8_DORMANT_DAYS = 7
PM_ACTION_MAX_DAYS = 21
QUEUE_UNTOUCHED = ("queued", "pending", "deferred")

SCHEMA = """
ALTER TABLE hotwash ADD COLUMN IF NOT EXISTS closed_reason text;
ALTER TABLE unexplained_events ADD COLUMN IF NOT EXISTS closed_reason text;
ALTER TABLE postmortem_actions ADD COLUMN IF NOT EXISTS close_reason text;
"""


def log(m: str) -> None:
    print(f"[ledger-aging {datetime.now():%H:%M:%S}] {m}", flush=True)


# ── pure: the rules ─────────────────────────────────────────────────────────

def hotwash_reason(fix_kind: str | None, age_days: float) -> str | None:
    """Why an open hotwash row closes now, or None. Pure."""
    if (fix_kind or "none") == "none" and age_days >= HOTWASH_NONE_DAYS:
        return "aged out: AAR recorded; fix_kind=none, nothing for the roll-up to propose"
    if age_days >= HOTWASH_MAX_DAYS:
        return f"aged out: open {age_days:.0f}d, not grouped into a proposal by the weekly roll-up"
    return None


def buick8_reason(idle_days: float) -> str | None:
    """Why an open Buick 8 entry goes dormant, or None. The cause is never touched. Pure."""
    if idle_days >= BUICK8_DORMANT_DAYS:
        return (f"dormant: not seen for {idle_days:.0f}d; cause still unknown (no evidence), reopens if it "
                f"happens again")
    return None


def pm_action_reason(queue_status: str | None, age_days: float) -> str | None:
    """Why an open postmortem action is cancelled, or None. Pure."""
    if age_days >= PM_ACTION_MAX_DAYS and (queue_status is None or queue_status in QUEUE_UNTOUCHED):
        return (f"aged out: untouched {age_days:.0f}d; the next postmortem re-files it only if the fact "
                f"still holds")
    return None


def tinker_status(copied: str | None, actual: str | None) -> str | None:
    """New proposal_status for a tinker_log row, or None when already in sync. Pure."""
    if actual and actual != copied:
        return actual
    return None


# ── DB side ─────────────────────────────────────────────────────────────────

def _exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    return bool(cur.fetchone()[0])


def ensure_schema(cur) -> None:
    import nova_pg_schema
    stmts = [s for s in SCHEMA.strip().splitlines() if s.strip()]
    for st in stmts:                       # each ledger is optional on a fresh box
        table = st.split()[2]
        if _exists(cur, table):
            nova_pg_schema.safe_ddl(cur, st)


def age_hotwash(cur, dry: bool) -> int:
    if not _exists(cur, "hotwash"):
        return 0
    cur.execute("SELECT id, fix_kind, EXTRACT(EPOCH FROM now() - ts) / 86400 FROM hotwash WHERE status='open'")
    n = 0
    for rid, fk, age in cur.fetchall():
        why = hotwash_reason(fk, float(age))
        if why:
            n += 1
            if not dry:
                cur.execute("UPDATE hotwash SET status='rolled_up', closed_reason=%s WHERE id=%s AND status='open'",
                            (why, rid))
    return n


def age_buick8(cur, dry: bool) -> int:
    if not _exists(cur, "unexplained_events"):
        return 0
    cur.execute("SELECT id, EXTRACT(EPOCH FROM now() - last_seen) / 86400 FROM unexplained_events WHERE status='open'")
    n = 0
    for rid, idle in cur.fetchall():
        why = buick8_reason(float(idle))
        if why:
            n += 1
            if not dry:
                cur.execute("UPDATE unexplained_events SET status='dormant', closed_reason=%s "
                            "WHERE id=%s AND status='open'", (why, rid))
    return n


def age_pm_actions(cur, dry: bool) -> int:
    if not _exists(cur, "postmortem_actions"):
        return 0
    cur.execute("SELECT a.id, a.queue_id, q.status, EXTRACT(EPOCH FROM now() - a.created_at) / 86400 "
                "FROM postmortem_actions a LEFT JOIN claude_queue q ON q.id = a.queue_id WHERE a.status='open'")
    n = 0
    for aid, qid, qs, age in cur.fetchall():
        why = pm_action_reason(qs, float(age))
        if why:
            n += 1
            if not dry:
                cur.execute("UPDATE postmortem_actions SET status='cancelled', done_at=now(), close_reason=%s "
                            "WHERE id=%s AND status='open'", (why, aid))
                if qid:
                    cur.execute("UPDATE claude_queue SET status='aged_out', updated_at=now(), outcome=%s "
                                "WHERE id=%s AND status IN %s", (why, qid, QUEUE_UNTOUCHED))
    return n


def sync_tinker(cur, dry: bool) -> int:
    if not (_exists(cur, "tinker_log") and _exists(cur, "coagency_proposals")):
        return 0
    cur.execute("SELECT t.id, t.proposal_status, p.status FROM tinker_log t JOIN coagency_proposals p "
                "ON p.id = t.proposal_id")
    n = 0
    for tid, copied, actual in cur.fetchall():
        new = tinker_status(copied, actual)
        if new:
            n += 1
            if not dry:
                cur.execute("UPDATE tinker_log SET proposal_status=%s WHERE id=%s", (new, tid))
    return n


def run(cur, dry: bool = False) -> dict:
    if not dry:
        ensure_schema(cur)
    out = {"hotwash": age_hotwash(cur, dry), "buick8_dormant": age_buick8(cur, dry),
           "postmortem_actions": age_pm_actions(cur, dry), "tinker_synced": sync_tinker(cur, dry)}
    log(("would close " if dry else "closed ") + str(out))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not (a.run or a.dry_run):
        ap.print_help()
        return 0
    import psycopg2
    import nova_dsn
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=8)
    try:
        run(conn.cursor(), a.dry_run)
        conn.rollback() if a.dry_run else conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
