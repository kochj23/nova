#!/usr/bin/env python3
"""nova_take_four.py — TAKE FOUR: the runs that only worked on a retake.

Little Mister, 2026-10-15 (after Nova watched Dead Kennedys studio-session footage): "Add all 9."

In the footage the band plays the same song take after take. "Hold it. Too slow." Again.
Nobody outside the room ever hears takes one to three; the record only has the one that
worked, and someone says "that was the best take we'd gotten." The scheduler does the same:
when a run fails it retries, the retry succeeds, the task looks green, and the failed first
take disappears. scheduler_runs has recorded it all along (was_retry, retry_recovered) and
nothing read those columns. On 2026-10-15 that was ~315 retry-recovered runs a week across
19 tasks, cluster_render recovering about 1 run in 10.

Daily, per task, over the last 7 days (and the 7 before, for the trend):
  * runs        = scheduled first takes (was_retry = false),
  * recovered   = runs that only succeeded on a retry (retry_recovered = true),
  * share       = recovered / runs,
  * retry_failed = retries that did not save the run either,
  * top first-take errors: for each recovered run, the failed first take before it, reduced
    to one template-normalised line (numbers, hex, uuids, quoted strings and paths folded),
    so "timed out after 30 s" and "timed out after 31 s" are the same fingerprint.
A task with share > SHARE_MIN (and at least MIN_RECOVERED_FOR_SHARE recoveries, so 1 in 8
is not a case) or more than COUNT_MIN recoveries in the week opens a case: one take_four
row per (task, week), upserted by each daily run. Status: new (the prior 7 days were under
threshold), worsening (recoveries up by >= 25% and >= 5, share not down), improving, open, cleared. Each
case is also logged in the Buick 8 Logbook (kind 'retry_flakiness', cause unknown; one
occurrence per task per week) because a run that fails and then passes with no change is,
by definition, unexplained.

Notify: one info digest on Mondays, only when a case is new or worsening and has not been
reported. Read-only toward the fleet: it proposes nothing to restart, changes no retry
policy, edits no code.

Measured (--report): weekly hidden-flakiness total (retry-recovered runs) and tasks over
threshold, per week and per month.

CLI:   --scheduled     the daily run (write rows, Buick 8, Monday digest)
       --dry-run       compute and print; write nothing, notify nothing
       --digest        send the digest now, whatever the weekday (with --scheduled)
       --report [N]    last N weeks + monthly totals
       --selftest      offline check of the pure helpers
Table: take_four (task_id, week, runs, recovered, share, top_first_take_errors jsonb, status, ...)
Schedule: scheduler-core, daily 07:20, timeout 300, overlap skip.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "nova_take_four"
WINDOW_DAYS = 7
SHARE_MIN = 0.05               # > 5% of first takes needed a retake ...
MIN_RECOVERED_FOR_SHARE = 3    # ... counted only from 3 recoveries up (1 of 8 weekly backups is not a case)
COUNT_MIN = 20                 # or > 20 recovered runs in the week, whatever the share
WORSEN_FACTOR = 1.25
WORSEN_ABS = 5
IMPROVE_FACTOR = 0.75
FIRST_TAKE_LOOKBACK_MS = 2 * 3600 * 1000   # the first take is the failed non-retry run within 2h before
TOP_ERRORS = 3
DIGEST_WEEKDAY = 0             # Monday
BUICK8_KIND = "retry_flakiness"
STATEMENT_TIMEOUT_MS = 60000

SCHEMA = """
CREATE TABLE IF NOT EXISTS take_four (
  task_id text NOT NULL,
  week date NOT NULL,
  runs integer NOT NULL,
  recovered integer NOT NULL,
  share real NOT NULL,
  top_first_take_errors jsonb NOT NULL DEFAULT '[]',
  status text NOT NULL,
  PRIMARY KEY (task_id, week));
ALTER TABLE take_four ADD COLUMN IF NOT EXISTS retry_failed integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS prior_runs integer, ADD COLUMN IF NOT EXISTS prior_recovered integer,
  ADD COLUMN IF NOT EXISTS prior_share real, ADD COLUMN IF NOT EXISTS buick8_id bigint,
  ADD COLUMN IF NOT EXISTS notified boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS opened_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS take_four_week ON take_four (week);
"""

_SKIP_LINE = re.compile(r"^(File \"|Traceback|Is the server running|\^|~|\.\.\.|During handling|The above exception|[\s~^]*$)")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I)
_BRACKET = re.compile(r"\[[^\[\]]{0,400}['\"][^\[\]]{0,400}\]")   # argv lists, not "[tag]" prefixes
_QUOTED = re.compile(r"'[^']{0,400}'|\"[^\"]{0,400}\"")
_PATH = re.compile(r"(?<![\w.])(?:/[\w.@+-]+){2,}/?")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def log(m: str) -> None:
    print(f"[take-four {datetime.now():%H:%M:%S}] {m}", flush=True)


# ---------------------------------------------------------------- pure helpers

def normalise(line: str) -> str:
    """Template-normalise one error line: uuids, hex, quoted strings, paths, numbers folded. Pure."""
    s = _UUID.sub("<uuid>", line)
    s = _HEX.sub("<hex>", s)
    s = _BRACKET.sub("[…]", s)
    s = _QUOTED.sub("'…'", s)
    s = _PATH.sub("<path>", s)
    s = _NUM.sub("#", s)
    return re.sub(r"\s+", " ", s).strip()[:140]


def fingerprint(error_tail: str | None, stdout_tail: str | None = None, exit_code=None,
                status: str | None = None) -> str:
    """One stable line for a failed first take. Error tail first, else the last stdout line. Pure."""
    for text in (error_tail, stdout_tail):
        if not text:
            continue
        lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
        for ln in reversed(lines):
            if not _SKIP_LINE.match(ln):
                return normalise(ln)
    return f"{status or 'failure'}: exit {exit_code if exit_code is not None else '?'}, no output"


def week_start(d: date) -> date:
    """Monday of d's ISO week. Pure."""
    return d - timedelta(days=d.weekday())


def over_threshold(runs: int, recovered: int) -> bool:
    """A case: > SHARE_MIN of first takes needed a retake (from MIN_RECOVERED_FOR_SHARE up), or > COUNT_MIN. Pure."""
    if recovered > COUNT_MIN:
        return True
    return recovered >= MIN_RECOVERED_FOR_SHARE and runs > 0 and recovered / runs > SHARE_MIN


def classify(rec: int, runs: int, prior_rec: int, prior_runs: int) -> str:
    """new | worsening | improving | open (only called for tasks over threshold now). Pure."""
    if not over_threshold(prior_runs, prior_rec):
        return "new"
    share, prior_share = (rec / runs if runs else 0.0), (prior_rec / prior_runs if prior_runs else 0.0)
    # more retakes only because the task ran more often is not worse: the share must hold up too
    if rec >= prior_rec * WORSEN_FACTOR and rec - prior_rec >= WORSEN_ABS and share >= prior_share * 0.9:
        return "worsening"
    if rec <= prior_rec * IMPROVE_FACTOR:
        return "improving"
    return "open"


def sticky_status(existing: str | None, computed: str) -> str:
    """Within one week a case keeps the loudest status it reached, so the Monday digest sees it. Pure."""
    if existing in ("new", "worsening") and computed in ("open", "improving"):
        return existing
    if existing == "new" and computed == "worsening":
        return "new"
    return computed


def top_errors(fps: list[str], n: int = TOP_ERRORS) -> list[dict]:
    """[{fingerprint, count}] most common first. Pure."""
    return [{"fingerprint": f, "count": c} for f, c in Counter(fps).most_common(n)]


def build_cases(stats: list[tuple], first_takes: dict[str, list[str]]) -> list[dict]:
    """stats rows (task, runs, recovered, retry_failed, prior_runs, prior_recovered) -> case dicts
    for tasks over threshold, worst first. Pure."""
    out = []
    for task, runs, rec, rfail, p_runs, p_rec in stats:
        runs, rec, p_runs, p_rec = int(runs or 0), int(rec or 0), int(p_runs or 0), int(p_rec or 0)
        if not over_threshold(runs, rec):
            continue
        out.append({"task_id": task, "runs": runs, "recovered": rec, "retry_failed": int(rfail or 0),
                    "share": round(rec / runs, 4) if runs else 0.0,
                    "prior_runs": p_runs, "prior_recovered": p_rec,
                    "prior_share": round(p_rec / p_runs, 4) if p_runs else 0.0,
                    "status": classify(rec, runs, p_rec, p_runs),
                    "top_first_take_errors": top_errors(first_takes.get(task, []))})
    out.sort(key=lambda c: (-c["recovered"], -c["share"]))
    return out


def totals(stats: list[tuple]) -> dict:
    """Week totals for the measurement line. Pure."""
    rec = sum(int(r[2] or 0) for r in stats)
    prior = sum(int(r[5] or 0) for r in stats)
    return {"recovered": rec, "prior_recovered": prior,
            "tasks_with_recoveries": sum(1 for r in stats if int(r[2] or 0) > 0),
            "tasks_over_threshold": sum(1 for r in stats if over_threshold(int(r[1] or 0), int(r[2] or 0)))}


def render_digest(cases: list[dict], tot: dict) -> tuple[str, str]:
    """(title, body) for the info digest. Pure."""
    title = (f"Take Four: {len(cases)} task(s) newly or increasingly passing only on a retake "
             f"({tot['recovered']} hidden retakes this week)")
    lines = [f"Retry-recovered runs, last 7 days: {tot['recovered']} (prior 7 days {tot['prior_recovered']}), "
             f"{tot['tasks_over_threshold']} task(s) over threshold. These runs show green on the dashboard.", ""]
    for c in cases:
        lines.append(f"• {c['task_id']} [{c['status']}] {c['recovered']}/{c['runs']} runs "
                     f"({c['share'] * 100:.1f}%) needed a retake; prior week {c['prior_recovered']}/{c['prior_runs']} "
                     f"({c['prior_share'] * 100:.1f}%); {c['retry_failed']} retake(s) failed too")
        for e in c["top_first_take_errors"]:
            lines.append(f"    {e['count']}× {e['fingerprint']}")
    lines += ["", "Proposal only: nothing was restarted or changed. Cause unknown until someone shows it "
                  "(logged in the Buick 8 Logbook as retry_flakiness)."]
    return title, "\n".join(lines)


def selftest() -> int:
    assert normalise("Command '['/x/y']' timed out after 30.5 seconds") == "Command '…' timed out after # seconds"
    assert fingerprint("Traceback\n  File \"a.py\", line 3\nValueError: bad 12") == "ValueError: bad #"
    assert fingerprint("", "[x] render failed (18683 B)\n") == "[x] render failed (# B)"
    assert fingerprint(None, None, 1, "failure") == "failure: exit 1, no output"
    assert over_threshold(100, 6) and not over_threshold(8, 1) and over_threshold(10000, 21)
    assert classify(30, 100, 0, 100) == "new" and classify(40, 300, 20, 300) == "worsening"
    assert classify(200, 1800, 140, 980) == "open"      # more runs, lower share: not worse
    assert sticky_status("new", "open") == "new"
    print("selftest ok")
    return 0


# ---------------------------------------------------------------- database

def connect():
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=10"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = True
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_stats(cur, now: datetime) -> list[tuple]:
    """(task, runs, recovered, retry_failed, prior_runs, prior_recovered) for tasks with any retry. Read-only."""
    w0, p0 = now - timedelta(days=WINDOW_DAYS), now - timedelta(days=2 * WINDOW_DAYS)
    cur.execute(
        "SELECT task_id, "
        "count(*) FILTER (WHERE NOT was_retry AND started_at >= %(w0)s), "
        "count(*) FILTER (WHERE retry_recovered AND started_at >= %(w0)s), "
        "count(*) FILTER (WHERE was_retry AND NOT retry_recovered AND status IN ('failure','timeout') "
        "AND started_at >= %(w0)s), "
        "count(*) FILTER (WHERE NOT was_retry AND started_at < %(w0)s), "
        "count(*) FILTER (WHERE retry_recovered AND started_at < %(w0)s) "
        "FROM scheduler_runs WHERE started_at >= %(p0)s AND started_at < %(now)s "
        "GROUP BY task_id HAVING count(*) FILTER (WHERE was_retry) > 0",
        {"w0": _ms(w0), "p0": _ms(p0), "now": _ms(now)})
    return cur.fetchall()


def fetch_first_takes(cur, now: datetime, tasks: list[str]) -> dict[str, list[str]]:
    """task -> fingerprints of the failed first take before each recovered run in the window. Read-only."""
    if not tasks:
        return {}
    cur.execute(
        "SELECT r.task_id, f.error_tail, f.stdout_tail, f.exit_code, f.status "
        "FROM scheduler_runs r LEFT JOIN LATERAL ("
        "  SELECT error_tail, stdout_tail, exit_code, status FROM scheduler_runs f "
        "  WHERE f.task_id = r.task_id AND NOT f.was_retry AND f.status IN ('failure','timeout') "
        "    AND f.started_at < r.started_at AND f.started_at >= r.started_at - %s "
        "  ORDER BY f.started_at DESC LIMIT 1) f ON true "
        "WHERE r.retry_recovered AND r.started_at >= %s AND r.started_at < %s AND r.task_id = ANY(%s)",
        (FIRST_TAKE_LOOKBACK_MS, _ms(now - timedelta(days=WINDOW_DAYS)), _ms(now), list(tasks)))
    out: dict[str, list[str]] = {}
    for task, err, so, code, status in cur.fetchall():
        fp = fingerprint(err, so, code, status) if status else "first take not found (older than 2h or not recorded)"
        out.setdefault(task, []).append(fp)
    return out


def evaluate(cur, now: datetime | None = None) -> tuple[list[dict], dict]:
    now = now or datetime.now(timezone.utc)
    stats = fetch_stats(cur, now)
    over = [r[0] for r in stats if over_threshold(int(r[1] or 0), int(r[2] or 0))]
    cases = build_cases(stats, fetch_first_takes(cur, now, over))
    return cases, totals(stats)


def buick8_log(cur, case: dict, week: date):
    """Log the case in the Buick 8 Logbook (cause unknown). Returns the row id or None; never raises."""
    try:
        import nova_buick8_log
        desc = (f"{case['task_id']} passed only on a retake in {case['recovered']} of {case['runs']} runs "
                f"({case['share'] * 100:.1f}%) in the 7 days to {date.today()}")
        return nova_buick8_log.log_unexplained(
            BUICK8_KIND, case["task_id"], desc,
            evidence={"week": str(week), "recovered": case["recovered"], "runs": case["runs"],
                      "share": case["share"], "top_first_take_errors": case["top_first_take_errors"]},
            occurrence_key=f"{case['task_id']}:{week}", source=SERVICE, cur=cur)
    except Exception as e:  # noqa: BLE001 — the logbook is a courtesy copy; the case row is the record
        log(f"buick8 log skipped for {case['task_id']}: {e}")
        return None


def record(cur, cases: list[dict], week: date) -> int:
    """Upsert this week's case rows; cases that dropped under threshold become 'cleared'. -> rows written."""
    cur.execute("SELECT task_id, status FROM take_four WHERE week = %s", (week,))
    existing = dict(cur.fetchall())
    for c in cases:
        c["status"] = sticky_status(existing.get(c["task_id"]), c["status"])
        bid = buick8_log(cur, c, week)
        cur.execute(
            "INSERT INTO take_four (task_id, week, runs, recovered, share, top_first_take_errors, status, "
            "retry_failed, prior_runs, prior_recovered, prior_share, buick8_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (task_id, week) DO UPDATE SET runs = EXCLUDED.runs, recovered = EXCLUDED.recovered, "
            "share = EXCLUDED.share, top_first_take_errors = EXCLUDED.top_first_take_errors, "
            "status = EXCLUDED.status, retry_failed = EXCLUDED.retry_failed, prior_runs = EXCLUDED.prior_runs, "
            "prior_recovered = EXCLUDED.prior_recovered, prior_share = EXCLUDED.prior_share, "
            "buick8_id = coalesce(EXCLUDED.buick8_id, take_four.buick8_id), updated_at = now()",
            (c["task_id"], week, c["runs"], c["recovered"], c["share"], json.dumps(c["top_first_take_errors"]),
             c["status"], c["retry_failed"], c["prior_runs"], c["prior_recovered"], c["prior_share"], bid))
    gone = [t for t in existing if t not in {c["task_id"] for c in cases}]
    if gone:
        cur.execute("UPDATE take_four SET status = 'cleared', updated_at = now() WHERE week = %s "
                    "AND task_id = ANY(%s)", (week, gone))
    return len(cases)


def digest(cur, tot: dict, week: date) -> bool:
    """One info notification for new/worsening, not-yet-reported cases of this and last week."""
    cur.execute("SELECT DISTINCT ON (task_id) task_id, runs, recovered, share, retry_failed, prior_runs, "
                "prior_recovered, prior_share, status, top_first_take_errors FROM take_four "
                "WHERE week >= %s AND NOT notified AND status IN ('new', 'worsening') "
                "AND task_id NOT IN (SELECT task_id FROM take_four WHERE week = %s AND status = 'cleared') "
                "ORDER BY task_id, week DESC", (week - timedelta(days=7), week))
    rows = cur.fetchall()
    if not rows:
        log("digest: no new or worsening cases — nothing sent")
        return False
    cases = [{"task_id": r[0], "runs": r[1], "recovered": r[2], "share": float(r[3]), "retry_failed": r[4],
              "prior_runs": r[5] or 0, "prior_recovered": r[6] or 0, "prior_share": float(r[7] or 0),
              "status": r[8], "top_first_take_errors": r[9] or []} for r in rows]
    cases.sort(key=lambda c: -c["recovered"])
    title, body = render_digest(cases, tot)
    from nova_notify import notify
    ok = notify(title, body=body, level="info", category="take_four", source=SERVICE,
                dedup_key=f"take_four:{week}", meta={"tasks": [c["task_id"] for c in cases],
                                                     "recovered_week": tot["recovered"]})
    if ok:
        cur.execute("UPDATE take_four SET notified = true WHERE week >= %s AND task_id = ANY(%s) "
                    "AND status IN ('new', 'worsening')", (week - timedelta(days=7), [c["task_id"] for c in cases]))
    return ok


def print_cases(cases: list[dict], tot: dict) -> None:
    print(f"Retry-recovered runs, last 7 days: {tot['recovered']} across {tot['tasks_with_recoveries']} task(s) "
          f"(prior 7 days {tot['prior_recovered']}); over threshold: {tot['tasks_over_threshold']}")
    for c in cases:
        print(f"  {c['task_id']:<28} {c['status']:<10} {c['recovered']:>4}/{c['runs']:<5} {c['share'] * 100:5.1f}%  "
              f"prior {c['prior_recovered']:>4}/{c['prior_runs']:<5} {c['prior_share'] * 100:5.1f}%  "
              f"retake-failed {c['retry_failed']}")
        for e in c["top_first_take_errors"]:
            print(f"        {e['count']:>3}× {e['fingerprint']}")


def report(cur, n: int) -> None:
    """Weekly and monthly hidden-flakiness numbers, straight from scheduler_runs (same rule every time)."""
    cur.execute(
        "SELECT to_char(date_trunc('week', to_timestamp(started_at / 1000)), 'YYYY-MM-DD'), task_id, "
        "count(*) FILTER (WHERE NOT was_retry), count(*) FILTER (WHERE retry_recovered) "
        "FROM scheduler_runs WHERE started_at >= %s GROUP BY 1, 2",
        (_ms(datetime.combine(week_start(date.today()) - timedelta(weeks=n - 1), datetime.min.time()).astimezone()),))
    weeks: dict[str, list[int]] = {}
    for wk, _task, runs, rec in cur.fetchall():
        w = weeks.setdefault(wk, [0, 0])
        w[0] += int(rec or 0)
        w[1] += 1 if over_threshold(int(runs or 0), int(rec or 0)) else 0
    print(f"Hidden flakiness, last {n} week(s) (calendar weeks Mon-Sun, local; the current week is partial):")
    for wk in sorted(weeks):
        print(f"  week of {wk}  retry-recovered runs {weeks[wk][0]:>5}  tasks over threshold {weeks[wk][1]:>3}")
    cur.execute("SELECT to_char(date_trunc('month', to_timestamp(started_at / 1000)), 'YYYY-MM'), "
                "count(*) FILTER (WHERE retry_recovered), count(DISTINCT task_id) FILTER (WHERE retry_recovered) "
                "FROM scheduler_runs WHERE started_at >= %s GROUP BY 1 ORDER BY 1 DESC",
                (_ms(datetime.now(timezone.utc) - timedelta(days=366)),))
    months = cur.fetchall()
    cases = {}
    cur.execute("SELECT to_regclass('take_four') IS NOT NULL")
    if (cur.fetchone() or (False,))[0]:
        cur.execute("SELECT to_char(week, 'YYYY-MM'), count(DISTINCT task_id) FROM take_four "
                    "WHERE status <> 'cleared' GROUP BY 1")
        cases = dict(cur.fetchall())
    print("\nMonthly (continuity review):")
    for m, rec, tasks in months:
        print(f"  {m}  retry-recovered runs {rec:>6}  tasks recovering {tasks:>3}  "
              f"tasks with a case {cases.get(m, '-')}")


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="daily run: write cases, Buick 8, Monday digest")
    ap.add_argument("--dry-run", action="store_true", help="print only; write and notify nothing")
    ap.add_argument("--digest", action="store_true", help="send the digest today regardless of weekday")
    ap.add_argument("--report", nargs="?", const=8, type=int, metavar="N", help="last N weeks + monthly totals")
    ap.add_argument("--selftest", action="store_true", help="offline check of the pure helpers")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.report is not None and not 1 <= a.report <= 104:
        ap.error("--report N must be 1..104")
    conn = connect()
    cur = conn.cursor()
    try:
        if a.report is not None:
            report(cur, a.report)
            return 0
        now = datetime.now(timezone.utc)
        cases, tot = evaluate(cur, now)
        week = week_start(now.astimezone().date())
        if a.dry_run:
            print_cases(cases, tot)
            print(f"\n[dry-run] {len(cases)} case(s), {tot['recovered']} hidden retakes this week — nothing written")
            return 0
        ensure_schema(cur)
        n = record(cur, cases, week)
        sent = False
        if a.digest or now.astimezone().weekday() == DIGEST_WEEKDAY:
            sent = digest(cur, tot, week)
        log(f"{tot['recovered']} retry-recovered runs in 7 days; {n} case(s) over threshold; digest "
            f"{'sent' if sent else 'not sent'}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
