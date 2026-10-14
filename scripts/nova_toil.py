#!/usr/bin/env python3
"""nova_toil.py — toil accounting: measure the manual, repetitive, interrupt-driven work Nova causes.

SRE book ch.5 ("Eliminating Toil") and Workbook ch.6. Toil is work that is manual, repetitive,
automatable, tactical, without enduring value, and grows with the service. You cannot cut what you do
not measure, so this organ turns the ledgers Nova already keeps into one toil_events table and a weekly
report. Nothing is fabricated: every row points at a row in an existing ledger (source + ref_id).

Collected (idempotent, UNIQUE (source, ref_id); --days lookback, default 35 so the trend has 5 weeks):
  category              human  source                    what counts
  queue_interrupt       yes    claude_queue              items filed by detectors/alerts for a session to handle
                                                         (improvement work — postmortem actions, toil candidates —
                                                         and test pings are excluded)
  manual_fix            yes    claude_actions            action_type fix/remediate/diagnose/investigate/escalate
  manual_rerun          yes    scheduler_runs            a run started > 2 min before its scheduled time and not a
                                                         retry: a /run poke (heuristic; dead-letter clears happen this way)
  manual_incident       yes    telemetry.incidents       incidents someone acknowledged or wrote a resolution for
                               public.incidents          resolved without the lifecycle's auto-close tag
  remediation_approval  yes    telemetry.remediations    a runbook step that stopped at 'proposed' for a human
  auto_restart          no     telemetry.remediations    runbook steps executed/failed by the machine
                               autonomy_ledger           restart:* actions
  auto_restart is toil the machine absorbed (symptom treatment that still recurs); it is reported but not
  counted in the human toil share.

minutes_estimate is a stated per-category default (MINUTES) — the ledgers do not record hands-on time.

Weekly report (--report): top 3 human toil causes for the last 7 days with counts and trend against the
mean of the 4 weeks before, total human toil events and minutes, and a toil share estimate:
  toil share = human toil events / (human toil events + project events)
  project events = claude_actions of type feature/publish/config (engineering work with enduring value).
The SRE book's bar is < 50%. Posted via nova_notify (info / digest, deduped per ISO week). The #1 cause is
filed to claude_queue as an automation candidate, once per cause per week and never while an item for
that cause is still open.

CLI: --collect [--days 35] [--dry-run] | --report [--dry-run] | --selftest
Table: nova_ops.toil_events.  Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SESSION = "sre-toil"
MINUTES = {"queue_interrupt": 15, "manual_fix": 10, "manual_rerun": 5, "manual_incident": 20,
           "remediation_approval": 5, "auto_restart": 0}
HUMAN = {k: k != "auto_restart" for k in MINUTES}
FIX_TYPES = ("fix", "remediate", "diagnose", "investigate", "escalate")
PROJECT_TYPES = ("feature", "publish", "config")
MANUAL_RERUN_MS = 120_000
QUEUE_OPEN = ("queued", "pending", "in_progress", "claimed", "deferred")
EXCLUDE_QUEUE = ("postmortem action", "toil automation candidate", "message:test")
SCHEMA = """
CREATE TABLE IF NOT EXISTS toil_events (
  id bigserial PRIMARY KEY,
  source text NOT NULL,
  ref_id text NOT NULL,
  cause_key text NOT NULL,
  category text NOT NULL CHECK (category IN ('queue_interrupt','manual_fix','manual_rerun','manual_incident',
                                             'remediation_approval','auto_restart')),
  human boolean NOT NULL,
  minutes_estimate numeric,
  at timestamptz NOT NULL,
  detail text,
  collected_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, ref_id));
CREATE INDEX IF NOT EXISTS toil_events_at ON toil_events (at, human);
CREATE INDEX IF NOT EXISTS toil_events_cause ON toil_events (cause_key, at);
"""


def log(m: str) -> None:
    print(f"[toil {datetime.now():%H:%M:%S}] {m}", flush=True)


def week_key(d) -> str:
    y, w, _ = (d.date() if isinstance(d, datetime) else d).isocalendar()
    return f"{y}-W{w:02d}"


# ── pure: classification ────────────────────────────────────────────────────

def normalize(text) -> str:
    """Stable cause text: drop Slack emoji/markup, quoted names, numbers; lowercase; clip. Pure."""
    t = str(text or "")
    t = re.sub(r":[a-z0-9_+-]+:", " ", t)            # :rotating_light:
    t = re.sub(r"\((?:downgraded|auto)\)", " ", t)
    t = re.sub(r"[*`]", "", t)
    t = re.sub(r"'[^']*'|\"[^\"]*\"", "'*'", t)      # task/host names in quotes
    t = re.sub(r"\d[\d:.,%/_-]*", "#", t)
    return " ".join(t.lower().split())[:80].strip(" .:-—")


def cause_of(description: str) -> str:
    """Cause key for a queue item / free-text toil: 'PREFIX: subject — detail' -> 'prefix:subject'. Pure."""
    d = " ".join(str(description or "").split())
    m = re.match(r"^([A-Za-z][A-Za-z ]{1,24}):\s*(.*)$", d)
    prefix, rest = (m.group(1).strip().lower(), m.group(2)) if m else ("", d)
    subj = re.split(r"\s+[—–]\s+|\s+-\s+", rest, maxsplit=1)[0]
    n = normalize(subj) or "unspecified"
    return f"{prefix}:{n}" if prefix else n


def classify_queue(description: str):
    """claude_queue description -> (category, cause_key) or None when it is not toil. Pure."""
    low = str(description or "").strip().lower()
    if not low or any(low.startswith(x) for x in EXCLUDE_QUEUE):
        return None
    return "queue_interrupt", cause_of(description)


def classify_action(action_type: str, target, description):
    """claude_actions row -> (category, cause_key) or None. Pure."""
    if action_type not in FIX_TYPES:
        return None
    return "manual_fix", f"{action_type}:{normalize(target) or cause_of(description)}"


def is_manual_rerun(scheduled_at_ms, started_at_ms, was_retry) -> bool:
    """A run started well before its scheduled slot (and not a retry) was poked by hand. Pure."""
    if was_retry or scheduled_at_ms is None or started_at_ms is None:
        return False
    return scheduled_at_ms - started_at_ms > MANUAL_RERUN_MS


def event(source, ref_id, category, cause_key, at, detail="") -> dict:
    return {"source": source, "ref_id": str(ref_id), "category": category, "cause_key": cause_key[:200],
            "human": HUMAN[category], "minutes_estimate": MINUTES[category], "at": at,
            "detail": " ".join(str(detail or "").split())[:300]}


# ── pure: trend + report ────────────────────────────────────────────────────

def trend(this_week: int, prior: list) -> tuple:
    """(prior-4-week mean, label) — new / up / down / flat. Pure."""
    avg = sum(prior) / len(prior) if prior else 0.0
    if avg == 0:
        return avg, ("new" if this_week else "flat")
    if this_week >= avg * 1.25 and this_week - avg >= 2:
        return avg, "up"
    if this_week <= avg * 0.75 and avg - this_week >= 2:
        return avg, "down"
    return avg, "flat"


def weekly_buckets(rows: list, now: datetime) -> dict:
    """rows: [(cause_key, at, minutes)] human toil over 35 days -> {cause: [w0, w1, w2, w3, w4]} where w0 is
    the last 7 days and w1..w4 the four weeks before. Pure."""
    out: dict = {}
    for cause, at, _m in rows:
        idx = int((now - at).total_seconds() // (7 * 86400))
        if 0 <= idx <= 4:
            out.setdefault(cause, [0] * 5)[idx] += 1
    return out


def top_causes(buckets: dict, n: int = 3) -> list:
    """[(cause, this_week, prior_avg, label)] by this week's count, ties by prior mean. Pure."""
    rows = []
    for cause, w in buckets.items():
        if w[0] == 0:
            continue
        avg, label = trend(w[0], w[1:])
        rows.append((cause, w[0], avg, label))
    rows.sort(key=lambda r: (-r[1], -r[2], r[0]))
    return rows[:n]


def toil_share(human_events: int, project_events: int):
    total = human_events + project_events
    return (human_events / total) if total else None


def render_report(week: str, top: list, human_n: int, human_min: float, machine_n: int, share,
                  by_cat: dict, top_machine: list) -> tuple:
    """(title, body) for nova_notify. Pure."""
    arrow = {"up": "↑", "down": "↓", "flat": "→", "new": "new"}
    lines = [f"Toil accounting {week} (last 7 days, SRE book ch.5)."]
    if top:
        lines.append("Top human toil causes (this week vs prior-4-week mean):")
        for i, (cause, n, avg, label) in enumerate(top, 1):
            lines.append(f"  {i}. {cause} — {n} ({arrow[label]} vs {avg:.1f}/wk)")
    else:
        lines.append("No human toil recorded this week.")
    lines.append(f"Human toil: {human_n} events, ~{human_min:.0f} min (estimated). "
                 + ", ".join(f"{k} {v}" for k, v in sorted(by_cat.items(), key=lambda x: -x[1])))
    if share is not None:
        lines.append(f"Toil share ≈ {share:.0%} of tracked work (target < 50%).")
    if machine_n:
        lines.append(f"Machine-absorbed restarts: {machine_n}"
                     + (f" (top: {top_machine[0][0]} ×{top_machine[0][1]})" if top_machine else "") + ".")
    if top:
        lines.append(f"#1 cause filed to claude_queue as an automation candidate: {top[0][0]}.")
    title = f"Toil {week}: " + (f"#1 {top[0][0]} ×{top[0][1]}" if top else "none") + \
            (f", share ≈ {share:.0%}" if share is not None else "")
    return title, "\n".join(lines)


# ── DB side ─────────────────────────────────────────────────────────────────

def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (table,))
    return bool(cur.fetchone()[0])


def gather(cur, days: int = 35) -> list:
    """Every toil event the ledgers support for the last `days`. Read-only."""
    ev = []
    cur.execute("SELECT id, created_at, description FROM claude_queue WHERE created_at > now() - make_interval(days => %s)",
                (days,))
    for qid, at, desc in cur.fetchall():
        c = classify_queue(desc)
        if c:
            ev.append(event("claude_queue", qid, c[0], c[1], at, desc))
    cur.execute("SELECT id, ts, action_type, target, description FROM claude_actions WHERE action_type = ANY(%s) "
                "AND ts > now() - make_interval(days => %s)", (list(FIX_TYPES), days))
    for aid, at, typ, tgt, desc in cur.fetchall():
        c = classify_action(typ, tgt, desc)
        if c:
            ev.append(event("claude_actions", aid, c[0], c[1], at, desc))
    cur.execute("SELECT run_id, task_id, scheduled_at, started_at, was_retry FROM scheduler_runs "
                "WHERE started_at > extract(epoch from now() - make_interval(days => %s)) * 1000 "
                "AND scheduled_at - started_at > %s AND NOT was_retry", (days, MANUAL_RERUN_MS))
    for rid, task, sched, start, retry in cur.fetchall():
        if is_manual_rerun(sched, start, retry):
            ev.append(event("scheduler_runs", rid, "manual_rerun", f"rerun:{task}",
                            datetime.fromtimestamp(start / 1000, tz=timezone.utc), f"manual run of {task}"))
    cur.execute("SELECT id, coalesce(acked_at, resolved_at, opened_at), recurrence_key, title, "
                "(resolution IS NOT NULL) FROM telemetry.incidents WHERE (acked_at IS NOT NULL OR resolution IS NOT NULL) "
                "AND opened_at > now() - make_interval(days => %s)", (days,))
    for iid, at, rk, title, _res in cur.fetchall():
        ev.append(event("telemetry.incidents", iid, "manual_incident", f"incident:{rk or normalize(title)}", at, title))
    if _exists(cur, "public.incidents"):
        cur.execute("SELECT id, coalesce(resolved_at, started_at), title FROM public.incidents WHERE status='resolved' "
                    "AND coalesce(root_cause,'') NOT ILIKE %s AND started_at > now() - make_interval(days => %s)",
                    ("%auto-closed%", days))
        for iid, at, title in cur.fetchall():
            ev.append(event("public.incidents", iid, "manual_incident", f"incident:{normalize(title)}", at, title))
    if _exists(cur, "telemetry.remediations"):
        cur.execute("SELECT r.id, r.requested_at, r.action, r.status, i.host FROM telemetry.remediations r "
                    "LEFT JOIN telemetry.incidents i ON i.id = r.incident_id WHERE NOT r.dry_run "
                    "AND r.requested_at > now() - make_interval(days => %s)", (days,))
        for rid, at, action, status, host in cur.fetchall():
            cat = "remediation_approval" if status in ("proposed", "approved") else "auto_restart"
            ev.append(event("telemetry.remediations", rid, cat, f"remediation:{action}@{host or '?'}", at,
                            f"{action} {status}"))
    cur.execute("SELECT id, ts, action_class, target FROM autonomy_ledger WHERE action_class LIKE 'restart:%%' "
                "AND executed AND ts > now() - make_interval(days => %s)", (days,))
    for lid, at, cls, tgt in cur.fetchall():
        ev.append(event("autonomy_ledger", lid, "auto_restart", cls, at, tgt))
    return ev


def collect(cur, days: int = 35, dry: bool = False) -> dict:
    ev = gather(cur, days)
    by = {}
    for e in ev:
        by[e["category"]] = by.get(e["category"], 0) + 1
    if dry:
        log(f"collect (dry): {len(ev)} event(s) {by}")
        return {"found": len(ev), "new": 0, "by_category": by}
    ensure_schema(cur)
    new = 0
    for e in ev:
        cur.execute("INSERT INTO toil_events (source, ref_id, cause_key, category, human, minutes_estimate, at, detail) "
                    "VALUES (%(source)s,%(ref_id)s,%(cause_key)s,%(category)s,%(human)s,%(minutes_estimate)s,%(at)s,"
                    "%(detail)s) ON CONFLICT (source, ref_id) DO NOTHING", e)
        new += cur.rowcount or 0
    log(f"collect: {len(ev)} event(s) found, {new} new {by}")
    return {"found": len(ev), "new": new, "by_category": by}


def file_candidate(cur, cause: str, week: str, n: int, avg: float, examples: list) -> int | None:
    """Queue the #1 cause as an automation candidate — once per cause per week, never while one is open."""
    base = f"Toil automation candidate: {cause}"
    cur.execute("SELECT id FROM claude_queue WHERE description LIKE %s AND (status IN %s OR description LIKE %s) LIMIT 1",
                (base + " (%", QUEUE_OPEN, f"{base} ({week})%"))
    if cur.fetchone():
        return None
    cur.execute("INSERT INTO claude_sessions (session_id, status, project) VALUES (%s, 'active', 'nova-sre') "
                "ON CONFLICT (session_id) DO NOTHING", (SESSION,))
    ctx = (f"nova_toil.py weekly report {week}: '{cause}' was the #1 human toil cause — {n} events in the last 7 "
           f"days (prior 4-week mean {avg:.1f}/wk). Toil is manual, repetitive, automatable work (SRE book ch.5). "
           f"Engineer it away: fix the source, automate the response, or retire the alert. Do not just suppress it.\n"
           "Examples:\n" + "\n".join(f"- {x}" for x in examples[:5]))
    cur.execute("INSERT INTO claude_queue (session_id, created_at, updated_at, status, priority, description, context) "
                "VALUES (%s, now(), now(), 'queued', 4, %s, %s) RETURNING id", (SESSION, f"{base} ({week})", ctx))
    return cur.fetchone()[0]


def report(cur, dry: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    week = week_key(now)
    if not _exists(cur, "toil_events"):
        log("report: toil_events does not exist yet — run --collect first")
        return {"week": week, "top": [], "human": 0, "minutes": 0.0, "machine": 0, "share": None, "queued": None}
    cur.execute("SELECT cause_key, at, minutes_estimate, human, category FROM toil_events WHERE at > %s",
                (now - timedelta(days=35),))
    rows = cur.fetchall()
    human = [(c, a, m) for c, a, m, h, _ in rows if h]
    machine = [(c, a, m) for c, a, m, h, _ in rows if not h]
    top = top_causes(weekly_buckets(human, now))
    top_m = top_causes(weekly_buckets(machine, now), 1)
    wk_ago = now - timedelta(days=7)
    this = [r for r in rows if r[1] > wk_ago]
    human_n = sum(1 for r in this if r[3])
    human_min = float(sum((r[2] or 0) for r in this if r[3]))
    machine_n = sum(1 for r in this if not r[3])
    by_cat: dict = {}
    for r in this:
        if r[3]:
            by_cat[r[4]] = by_cat.get(r[4], 0) + 1
    cur.execute("SELECT count(*) FROM claude_actions WHERE action_type = ANY(%s) AND ts > %s",
                (list(PROJECT_TYPES), wk_ago))
    share = toil_share(human_n, cur.fetchone()[0])
    title, body = render_report(week, top, human_n, human_min, machine_n, share, by_cat, top_m)
    print(title + "\n" + body)
    qid = None
    if not dry:
        try:
            from nova_notify import notify
            notify(title, body=body, level="info", category="digest", source="nova_toil.py",
                   dedup_key=f"toil-weekly-{week}", meta={"week": week, "top": [t[0] for t in top], "share": share})
        except Exception as e:  # noqa: BLE001
            log(f"notify failed: {e}")
        if top:
            cur.execute("SELECT detail FROM toil_events WHERE cause_key=%s AND at > %s ORDER BY at DESC LIMIT 5",
                        (top[0][0], wk_ago))
            qid = file_candidate(cur, top[0][0], week, top[0][1], top[0][2], [r[0] for r in cur.fetchall()])
            log(f"#1 cause {top[0][0]}: " + (f"queued #{qid}" if qid else "already queued this week / still open"))
    return {"week": week, "top": top, "human": human_n, "minutes": human_min, "machine": machine_n,
            "share": share, "queued": qid}


def selftest() -> int:
    assert cause_of("INCIDENT: Scheduler — CODE BUG in scheduler task 'x' (nova_x.py)") == "incident:scheduler"
    assert cause_of("TASK FAILING: prober — 5 consecutive failures") == "task failing:prober"
    assert classify_queue("Postmortem action [prevent] h:probe: fix it") is None
    assert classify_queue("message:test_ping_from_tests") is None
    assert trend(10, [2, 2, 2, 2]) == (2.0, "up") and trend(1, [0, 0, 0, 0]) == (0.0, "new")
    assert trend(2, [8, 8, 8, 8])[1] == "down" and trend(5, [5, 4, 6, 5])[1] == "flat"
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    b = weekly_buckets([("a", now - timedelta(days=1), 5), ("a", now - timedelta(days=9), 5)], now)
    assert b == {"a": [1, 1, 0, 0, 0]}
    assert is_manual_rerun(1_000_000, 800_000, False) and not is_manual_rerun(1_000_000, 800_000, True)
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--collect", action="store_true", help="populate toil_events from the ledgers (idempotent)")
    ap.add_argument("--report", action="store_true", help="weekly top-3 + toil share, notify, queue the #1 cause")
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.collect or a.report):
        ap.print_help()
        return 0
    import psycopg2
    import nova_dsn
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"), connect_timeout=8)
    try:
        cur = conn.cursor()
        if a.collect:
            collect(cur, a.days, a.dry_run)
        if a.report:
            report(cur, a.dry_run)
        if a.dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
