#!/usr/bin/env python3
"""nova_problems.py — PROBLEM records: one per recurrence key, owned end to end by Nova.

Little Mister, 2026-10-15 (plan item #38): "Problem records per recurrence key." 2,811 incidents in
90 days carried a recurrence_key; 66 keys were 98% of them and the top five were half. Every one of
those was a fresh incident, a fresh page and (since 10-13) a fresh postmortem about the SAME thing.
ITIL's answer is the problem record, and this is it — Nova's, not Jordan's: she opens it, decides
the workaround, links the fix and closes it. Jordan only reads about it in the daily summary.

States (problem_log keeps every transition with a reason):

  candidate      a key that opened >= CANDIDATE_MIN incidents in LOOKBACK_DAYS
  known_error    a workaround is known. Derived from the data, never invented:
                   - an incident of this key was resolved with a written resolution (last 30 days), or
                   - a remediation runbook executed OK on one of its incidents, or
                   - it self-heals: >= SELF_HEAL_MIN incidents, all auto-closed, median MTTR
                     <= SELF_HEAL_MTTR_MIN minutes
                 or Nova set it herself (--known-error KEY --workaround TEXT).
                 From here on the correlator LINKS new events of the key to the problem's anchor
                 incident instead of opening a new incident, and nothing pages for it — unless
                 nova_alert_triage.page_class() says the event is an SLO / Tier-0 / house page.
  fix_in_change  an executed co-agency proposal or a git commit names the key (or "problem #id")
  closed         zero recurrence (no incident, no linked event) for CLOSE_QUIET_DAYS. A closed
                 problem that recurs reopens as candidate; a fix_in_change that recurs more than a
                 day after its fix goes back to known_error ("the fix did not hold").

Rows are never deleted. Incidents are stamped with problem_id (annotation only).

CLI:  --sync [--dry-run]                      upsert + advance every problem (scheduler, hourly)
      --list [--state S]                      print problems
      --known-error KEY --workaround TEXT     Nova records a workaround she decided on
      --fix KEY --ref REF                     Nova links the change that fixes it
Tables: nova_ops.problems, nova_ops.problem_log (+ telemetry.incidents.problem_id).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LOOKBACK_DAYS = 90
CANDIDATE_MIN = 3
SELF_HEAL_MIN = 5
SELF_HEAL_MTTR_MIN = 120
CLOSE_QUIET_DAYS = 14
FIX_GRACE_H = 24
RESOLUTION_FRESH_DAYS = 30
STATES = ("candidate", "known_error", "fix_in_change", "closed")
# keys rooted in Nova's own reports about incidents are artefacts, not problems
SELF_REFERENTIAL = ("incident", "incident_recurring", "incident_escalation")

SCHEMA = """
CREATE TABLE IF NOT EXISTS problems (
  id bigserial PRIMARY KEY,
  recurrence_key text NOT NULL UNIQUE,
  host text,
  category text,
  state text NOT NULL DEFAULT 'candidate'
        CHECK (state IN ('candidate','known_error','fix_in_change','closed')),
  title text,
  workaround text,
  workaround_source text,
  fix_ref text,
  fix_at timestamptz,
  suppress_paging boolean NOT NULL DEFAULT true,
  anchor_incident_id bigint,
  incident_count int NOT NULL DEFAULT 0,
  linked_events int NOT NULL DEFAULT 0,
  first_seen timestamptz,
  last_seen timestamptz,
  state_changed_at timestamptz NOT NULL DEFAULT now(),
  closed_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS problems_state ON problems (state);
CREATE TABLE IF NOT EXISTS problem_log (
  id bigserial PRIMARY KEY,
  problem_id bigint NOT NULL REFERENCES problems(id),
  ts timestamptz NOT NULL DEFAULT now(),
  from_state text,
  to_state text NOT NULL,
  reason text NOT NULL,
  actor text NOT NULL DEFAULT 'nova');
CREATE INDEX IF NOT EXISTS problem_log_problem ON problem_log (problem_id, ts);
ALTER TABLE telemetry.incidents ADD COLUMN IF NOT EXISTS problem_id bigint;
"""


def log(m: str) -> None:
    print(f"[problems {datetime.now():%H:%M:%S}] {m}", flush=True)


def _clip(s, n=300) -> str:
    return " ".join(str(s or "").split())[:n]


def split_key(key: str) -> tuple[str, str]:
    """'host:cat' -> (host, cat); hosts may contain ':' (https://x:5001:storage). Pure."""
    host, _, cat = (key or "").rpartition(":")
    return host, cat


# ── pure: decisions ─────────────────────────────────────────────────────────

def derive_workaround(facts: dict) -> tuple[str, str] | None:
    """facts: {n, resolutions[], remediations_ok[], auto_closed, mttr_minutes[]} -> (text, source) or None.
    Only what the record shows; never a guess. Pure."""
    res = [r for r in facts.get("resolutions") or [] if r and r.strip()]
    if res:
        return _clip(res[-1], 500), "incident_resolution"
    ok = sorted(set(facts.get("remediations_ok") or []))
    if ok:
        return f"Runbook {', '.join(ok)} recovers it (executed OK on an incident of this key).", "runbook"
    n, mttrs = int(facts.get("n") or 0), [m for m in facts.get("mttr_minutes") or [] if m is not None]
    if (n >= SELF_HEAL_MIN and mttrs and facts.get("auto_closed", 0) >= n
            and statistics.median(mttrs) <= SELF_HEAL_MTTR_MIN):
        return (f"Self-heals: {n} incidents, every one auto-closed, median MTTR "
                f"{statistics.median(mttrs):.0f} min. No action while the fix is pending; new occurrences "
                f"are linked here instead of paging.", "self_healing")
    return None


def mentions(text: str, key: str, problem_id: int | None = None) -> bool:
    """Does a change description name this problem? Pure."""
    t = (text or "").lower()
    if not t:
        return False
    if key and key.lower() in t:
        return True
    return bool(problem_id) and re.search(rf"\bproblem\s*#?{int(problem_id)}\b", t) is not None


def next_state(p: dict, facts: dict, now: datetime) -> tuple[str, str, dict] | None:
    """One lifecycle step. p: problem row; facts: {last_seen, workaround, fix}. -> (state, reason, updates)
    or None when nothing changes. Pure."""
    st, last = p["state"], facts.get("last_seen") or p.get("last_seen")
    quiet = last is not None and now - last >= timedelta(days=CLOSE_QUIET_DAYS)
    if st == "closed":
        if last is not None and p.get("state_changed_at") and last > p["state_changed_at"]:
            why = "recurred after close"
            if p.get("fix_ref"):
                why += f"; the fix {p['fix_ref']} did not hold"
            return "candidate", why, {"closed_reason": None}
        return None
    if quiet:
        return "closed", f"zero recurrence for {CLOSE_QUIET_DAYS} days (last seen {last:%Y-%m-%d %H:%M})", \
            {"closed_reason": f"no recurrence since {last:%Y-%m-%d}"}
    if st == "fix_in_change":
        fa = p.get("fix_at")
        if fa and last and last > fa + timedelta(hours=FIX_GRACE_H):
            return "known_error", f"fix {p.get('fix_ref')} did not hold: recurred {last:%Y-%m-%d %H:%M}", {}
        return None
    fix = facts.get("fix")
    if fix and (not p.get("fix_ref") or fix["ref"] != p.get("fix_ref")):
        return "fix_in_change", f"fix in change: {fix['ref']}", {"fix_ref": fix["ref"], "fix_at": fix["at"]}
    if st == "candidate":
        wa = facts.get("workaround")
        if wa:
            return "known_error", f"workaround known ({wa[1]})", {"workaround": wa[0], "workaround_source": wa[1]}
    return None


def link_allowed(problem: dict | None, page_cls: str) -> bool:
    """Link an event to a problem instead of opening an incident? Never for a real page. Pure."""
    return bool(problem) and problem.get("state") in ("known_error", "fix_in_change") \
        and bool(problem.get("suppress_paging", True)) and page_cls != "page"


# ── DB side ─────────────────────────────────────────────────────────────────

def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def _transition(cur, pid: int, frm: str, to: str, reason: str, updates: dict, actor: str = "nova") -> None:
    sets = ", ".join(f"{k}=%s" for k in updates)
    cur.execute(f"UPDATE problems SET state=%s, state_changed_at=now(), updated_at=now()"
                f"{', ' + sets if sets else ''} WHERE id=%s", (to, *updates.values(), pid))
    cur.execute("INSERT INTO problem_log (problem_id, from_state, to_state, reason, actor) VALUES (%s,%s,%s,%s,%s)",
                (pid, frm, to, reason[:500], actor))


def upsert_candidates(cur) -> int:
    """One problem per key with >= CANDIDATE_MIN incidents in LOOKBACK_DAYS. Returns #new."""
    cur.execute(
        "SELECT i.recurrence_key, count(*), min(i.opened_at), max(i.opened_at), "
        "(array_agg(i.id ORDER BY i.opened_at DESC))[1], (array_agg(i.title ORDER BY i.opened_at DESC))[1] "
        "FROM telemetry.incidents i WHERE i.recurrence_key IS NOT NULL "
        "AND i.opened_at > now() - make_interval(days => %s) GROUP BY 1 HAVING count(*) >= %s",
        (LOOKBACK_DAYS, CANDIDATE_MIN))
    new = 0
    for key, n, first, last, anchor, title in cur.fetchall():
        host, cat = split_key(key)
        if cat in SELF_REFERENTIAL:
            continue
        cur.execute(
            "INSERT INTO problems (recurrence_key, host, category, title, anchor_incident_id, incident_count, "
            "first_seen, last_seen) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (recurrence_key) DO UPDATE SET incident_count=EXCLUDED.incident_count, "
            "anchor_incident_id=EXCLUDED.anchor_incident_id, title=EXCLUDED.title, "
            "first_seen=least(problems.first_seen, EXCLUDED.first_seen), "
            "last_seen=greatest(problems.last_seen, EXCLUDED.last_seen), updated_at=now() "
            "RETURNING (xmax = 0)", (key, host, cat, _clip(title, 200), anchor, n, first, last))
        if cur.fetchone()[0]:
            new += 1
            cur.execute("SELECT id FROM problems WHERE recurrence_key=%s", (key,))
            pid = cur.fetchone()[0]
            cur.execute("INSERT INTO problem_log (problem_id, from_state, to_state, reason) VALUES (%s,NULL,"
                        "'candidate',%s)", (pid, f"{n} incidents in {LOOKBACK_DAYS}d"))
        cur.execute("UPDATE telemetry.incidents i SET problem_id=p.id FROM problems p "
                    "WHERE p.recurrence_key=%s AND i.recurrence_key=p.recurrence_key AND i.problem_id IS NULL",
                    (key,))
    return new


def facts_for(cur, p: dict) -> dict:
    key = p["recurrence_key"]
    # a written resolution is a workaround only while it is fresh: an old "fixed on <date>" on a key that
    # still recurs describes a fix that did not hold (studio:crash_storm, July), not a workaround
    cur.execute("SELECT count(*), array_agg(resolution ORDER BY resolved_at) FILTER (WHERE resolution IS NOT NULL "
                "AND resolved_at > now() - make_interval(days => %s)), "
                "count(*) FILTER (WHERE resolution IS NULL AND status='resolved'), "
                "array_agg(mttr_s / 60.0) FILTER (WHERE mttr_s IS NOT NULL), max(opened_at), array_agg(id) "
                "FROM telemetry.incidents WHERE recurrence_key=%s AND opened_at > now() - make_interval(days => %s)",
                (RESOLUTION_FRESH_DAYS, key, LOOKBACK_DAYS))
    n, res, auto, mttrs, last_inc, ids = cur.fetchone()
    rem_ok = []
    cur.execute("SELECT to_regclass('telemetry.remediations')")
    if cur.fetchone()[0] and ids:
        cur.execute("SELECT DISTINCT action FROM telemetry.remediations WHERE incident_id = ANY(%s) "
                    "AND status='executed' AND NOT dry_run", (ids,))
        rem_ok = [r[0] for r in cur.fetchall()]
    last = max([x for x in (last_inc, p.get("last_seen")) if x is not None], default=None)
    return {"n": n or 0, "resolutions": res or [], "auto_closed": auto or 0, "mttr_minutes": mttrs or [],
            "remediations_ok": rem_ok, "last_seen": last}


def find_fix(cur, p: dict, commits: list) -> dict | None:
    """An executed co-agency proposal or a commit that names the problem -> {ref, at}."""
    since = p.get("first_seen") or datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    try:
        cur.execute("SAVEPOINT problems_fix")
        cur.execute("SELECT id, executed_at, coalesce(proposed_action,'') || ' ' || coalesce(rationale,'') "
                    "FROM coagency_proposals WHERE status='executed' AND executed_at > %s ORDER BY executed_at DESC",
                    (since,))
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT problems_fix")
        for pid, at, text in rows:
            if mentions(text, p["recurrence_key"], p["id"]):
                return {"ref": f"coagency_proposal #{pid}", "at": at}
    except Exception as e:  # noqa: BLE001 — table optional
        try:
            cur.execute("ROLLBACK TO SAVEPOINT problems_fix")
        except Exception:  # noqa: BLE001
            pass
        log(f"coagency lookup skipped ({e})")
    for c in commits:
        if c["at"] >= since and mentions(c["subject"], p["recurrence_key"], p["id"]):
            return {"ref": f"commit {c['sha'][:10]}", "at": c["at"]}
    return None


def git_commits(days: int = LOOKBACK_DAYS) -> list:
    """Shared git-log reader (nova_change_class). Fails open to []."""
    try:
        import nova_change_class
        return nova_change_class.git_commits(days)
    except Exception:  # noqa: BLE001
        return []


def advance(cur, now: datetime | None = None, commits: list | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    commits = git_commits() if commits is None else commits
    cols = ("id", "recurrence_key", "state", "fix_ref", "fix_at", "last_seen", "state_changed_at",
            "first_seen", "suppress_paging")
    cur.execute(f"SELECT {', '.join(cols)} FROM problems")
    moved = {}
    for row in cur.fetchall():
        p = dict(zip(cols, row))
        f = facts_for(cur, p)
        f["workaround"] = derive_workaround(f) if p["state"] == "candidate" else None
        f["fix"] = find_fix(cur, p, commits) if p["state"] in ("candidate", "known_error") else None
        step = next_state(p, f, now)
        if f["last_seen"] and (p["last_seen"] is None or f["last_seen"] > p["last_seen"]):
            cur.execute("UPDATE problems SET last_seen=%s WHERE id=%s", (f["last_seen"], p["id"]))
        if step:
            to, why, upd = step
            _transition(cur, p["id"], p["state"], to, why, upd)
            moved[to] = moved.get(to, 0) + 1
    return moved


def sync(cur, dry: bool = False) -> dict:
    ensure_schema(cur)
    new = upsert_candidates(cur)
    moved = advance(cur)
    cur.execute("SELECT state, count(*), sum(linked_events) FROM problems GROUP BY 1")
    by = {s: (n, int(le or 0)) for s, n, le in cur.fetchall()}
    log(f"sync: new={new} moved={moved} states={by}")
    return {"new": new, "moved": moved, "states": by}


# ── used by nova_correlator: link instead of opening ───────────────────────

def known_error_for(cur, host: str | None, category: str | None) -> dict | None:
    if not host or not category:
        return None
    try:
        # savepoint: the notifier's drain runs in one transaction; a missing table must not abort it
        cur.execute("SAVEPOINT problems_ke")
        cur.execute("SELECT id, state, anchor_incident_id, suppress_paging, workaround FROM problems "
                    "WHERE recurrence_key=%s", (f"{host}:{category}",))
        r = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT problems_ke")
    except Exception:  # noqa: BLE001 — table absent: behave as before
        try:
            cur.execute("ROLLBACK TO SAVEPOINT problems_ke")
        except Exception:  # noqa: BLE001
            pass
        return None
    if not r:
        return None
    return dict(zip(("id", "state", "anchor_incident_id", "suppress_paging", "workaround"), r))


def link_event(cur, ev: dict, host: str | None) -> dict | None:
    """If ev belongs to a known error, fold it into the problem's anchor incident and return the
    correlator result; else None (the caller opens an incident as before). Never raises."""
    try:
        prob = known_error_for(cur, host, ev.get("category"))
        if not prob or not prob.get("anchor_incident_id"):
            return None
        try:
            import nova_alert_triage
            cls = nova_alert_triage.page_class(ev.get("title"), ev.get("level"), ev.get("category"), ev.get("meta"))[0]
        except Exception:  # noqa: BLE001 — cannot tell: do not fold
            return None
        if not link_allowed(prob, cls):
            return None
        anchor = prob["anchor_incident_id"]
        cur.execute("UPDATE problems SET linked_events=linked_events+1, last_seen=now(), updated_at=now() "
                    "WHERE id=%s", (prob["id"],))
        cur.execute("UPDATE telemetry.events SET incident_id=%s, corr_role='problem', status='suppressed' "
                    "WHERE id=%s", (anchor, ev["id"]))
        return {"action": "attached", "incident_id": anchor, "role": "problem", "suppress": True,
                "problem_id": prob["id"]}
    except Exception:  # noqa: BLE001
        return None


# ── Nova's own decisions ────────────────────────────────────────────────────

def set_known_error(cur, key: str, workaround: str, actor: str = "nova") -> bool:
    ensure_schema(cur)
    cur.execute("SELECT id, state FROM problems WHERE recurrence_key=%s", (key,))
    r = cur.fetchone()
    if not r or not workaround.strip():
        return False
    _transition(cur, r[0], r[1], "known_error", "workaround set by Nova",
                {"workaround": workaround.strip()[:1000], "workaround_source": actor}, actor)
    return True


def set_fix(cur, key: str, ref: str, actor: str = "nova") -> bool:
    ensure_schema(cur)
    cur.execute("SELECT id, state FROM problems WHERE recurrence_key=%s", (key,))
    r = cur.fetchone()
    if not r or not ref.strip():
        return False
    _transition(cur, r[0], r[1], "fix_in_change", f"fix in change: {ref}",
                {"fix_ref": ref.strip()[:200], "fix_at": datetime.now(timezone.utc)}, actor)
    return True


def list_rows(cur, state: str | None = None) -> int:
    q = ("SELECT id, state, recurrence_key, incident_count, linked_events, last_seen, "
         "coalesce(workaround_source,''), coalesce(fix_ref,'') FROM problems")
    cur.execute(q + (" WHERE state=%s" if state else "") + " ORDER BY incident_count DESC", (state,) if state else ())
    for r in cur.fetchall():
        print(f"#{r[0]:<4} {r[1]:<13} {r[2]:<40} inc={r[3]:<4} linked={r[4]:<4} last={r[5]:%m-%d} {r[6]} {r[7]}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--state", choices=STATES)
    ap.add_argument("--known-error", metavar="KEY")
    ap.add_argument("--workaround", default="")
    ap.add_argument("--fix", metavar="KEY")
    ap.add_argument("--ref", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not (a.sync or a.list or a.known_error or a.fix):
        ap.print_help()
        return 0
    import psycopg2
    import nova_dsn
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=8)
    try:
        cur = conn.cursor()
        if a.list:
            return list_rows(cur, a.state)
        ok = True
        if a.sync:
            sync(cur, a.dry_run)
        if a.known_error:
            ok = set_known_error(cur, a.known_error, a.workaround)
        if a.fix:
            ok = set_fix(cur, a.fix, a.ref) and ok
        conn.rollback() if a.dry_run else conn.commit()
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
