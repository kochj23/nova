#!/usr/bin/env python3
"""nova_chanos_filter.py — THE CHANOS FILTER: a monitor that has never gone red has never shown it can.

Little Mister, 2026-10-15 (after Nova watched ENRON: The Smartest Guys in the Room): "Add all 9."

Jim Chanos shorted Enron because its numbers were always good. Nobody could explain how the money
was made: "it's a black box. You have to take it on faith." And the numbers "are always going to
be good until they're not." A green that never flickers is not evidence of health; it can just as
well be a check that cannot fail: a heartbeat whose status is a string literal, a port probe on a
hung process, a ratio the measured thing reports about itself.

Every run reads every series Nova monitors and computes, over the last 90 days:
  readings, bad (share of non-green readings), flips (green<->red transitions), last_bad (all time)
Series:
  health:<service>@<node>/<checked_by>   health_checks          bad = status not up/ok/healthy
  probe:<probe>                          telemetry.probe_results bad = not ok
  slo:<name>                             slo_status             bad = state <> 'ok'
  task:<task_id>                         scheduler_runs         bad = failure/timeout/orphaned
  alerts:<category>                      telemetry.events       one reading per calendar day; bad = a warning/critical that day
  organ:<organ>                          organ_board            latest row only: a BLACK BOX (no history to judge)
Verdicts:
  never_red_unproven  green for 90 days, never red in its whole history and no test/drill/injection row:
                      "never shown able to go red". These are the Enrons.
  never_red_proven    green for 90 days but it has gone red before (or a test/drill row exists)
  always_red          every reading bad: stuck, or nobody believes it any more
  live                it goes both ways; it works as a monitor
  black_box           no history kept at all
  too_young           green, never red, but fewer than MIN_READINGS readings: not yet a track record
For the top unproven CRITICAL monitors it proposes one concrete fault-injection idea each.
It never injects anything: read, record, notify.

Measured for the monthly continuity review (--report): the count of never-red unproven series
among critical monitors (target: falling toward zero as faults get injected and proven).

CLI:   --scheduled   daily: compute, upsert this month's rows, notify on new unproven critical series
       --dry-run     compute and print, write nothing, notify nothing
       --report [N]  this month's critical never-red list (top N) + monthly measure
       --selftest    pure-logic assertions, no I/O
Table: chanos_filter (month, series) one row per series per month, refreshed by each run.
Schedule: scheduler-core, daily 04:50, timeout 300, overlap skip.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "nova_chanos_filter"
WINDOW_DAYS = 90
TOP_IDEAS = 10
MIN_READINGS = 30                       # fewer green readings than this is too young to call never-red
STATEMENT_TIMEOUT_MS = 180000           # health_checks is ~6M rows; the windowed scan takes ~20 s
TEST_RE = r"\m(test|drill|inject|injected|chaos|gameday|fault[- ]injection)\M"
GOOD_STATUS = ("up", "ok", "healthy")
ORGAN_RED = {"down", "fail", "failed", "critical", "error", "degraded", "stale"}

# what makes a series a CRITICAL monitor
CRIT_RE = re.compile(r"memory|gateway|chat|ollama|inference|llm|sched|postgres|\bpg|database|journal|http|web|"
                     r"liveness|sentinel|security_organ|embedding|alert_delivery|redis|haproxy|tunnel|cloudflared|blood",
                     re.I)
CRIT_TASK_RE = re.compile(r"prober|slo\b|^slo|sentinel|liveness|selfcheck|health|service_down|backup|blood|chanos|"
                          r"comet|notifier|watchdog|replica", re.I)
CRIT_ALERTS = {"probe", "core-liveness", "task-sentinel", "slo", "backup", "tunnel", "blood_test", "freshness",
               "storage", "fleet", "crash_storm"}
CRIT_ORGANS = {"core_liveness", "security_organ"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chanos_filter (
  month date NOT NULL,
  series text NOT NULL,
  source text NOT NULL,
  readings bigint NOT NULL,
  bad bigint NOT NULL,
  bad_share double precision,
  flips integer NOT NULL DEFAULT 0,
  last_bad timestamptz,
  verdict text NOT NULL,
  critical boolean NOT NULL DEFAULT false,
  idea text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (month, series));
CREATE INDEX IF NOT EXISTS chanos_filter_verdict ON chanos_filter (month, verdict) WHERE critical;
"""

# Each returns: series, source, readings, bad, flips, last_bad, ever_bad, test_rows  (window from %(since)s)
_GOOD_SQL = "status IN ('up', 'ok', 'healthy')"
SOURCES = {
    "health": f"""
        SELECT 'health:' || service_name || '@' || node_name || '/' || checked_by, 'health_checks',
               count(*) FILTER (WHERE checked_at >= %(since)s),
               count(*) FILTER (WHERE checked_at >= %(since)s AND NOT good),
               count(*) FILTER (WHERE checked_at >= %(since)s AND prev IS NOT NULL AND prev <> good),
               max(checked_at) FILTER (WHERE NOT good),
               count(*) FILTER (WHERE NOT good),
               count(*) FILTER (WHERE error_message ~* %(test_re)s)
        FROM (SELECT service_name, node_name, checked_by, checked_at, error_message, {_GOOD_SQL} AS good,
                     lag({_GOOD_SQL}) OVER (PARTITION BY service_name, node_name, checked_by ORDER BY checked_at) AS prev
              FROM health_checks) x
        GROUP BY 1, 2""",
    "probe": """
        SELECT 'probe:' || probe, 'telemetry.probe_results',
               count(*) FILTER (WHERE ts >= %(since)s),
               count(*) FILTER (WHERE ts >= %(since)s AND NOT ok),
               count(*) FILTER (WHERE ts >= %(since)s AND prev IS NOT NULL AND prev <> ok),
               max(ts) FILTER (WHERE NOT ok),
               count(*) FILTER (WHERE NOT ok),
               count(*) FILTER (WHERE detail ~* %(test_re)s)
        FROM (SELECT probe, ts, ok, detail, lag(ok) OVER (PARTITION BY probe ORDER BY ts) AS prev
              FROM telemetry.probe_results) x
        GROUP BY 1, 2""",
    "slo": """
        SELECT 'slo:' || name, 'slo_status',
               count(*) FILTER (WHERE at >= %(since)s),
               count(*) FILTER (WHERE at >= %(since)s AND NOT good),
               count(*) FILTER (WHERE at >= %(since)s AND prev IS NOT NULL AND prev <> good),
               max(at) FILTER (WHERE NOT good),
               count(*) FILTER (WHERE NOT good),
               0
        FROM (SELECT name, at, state = 'ok' AS good, lag(state = 'ok') OVER (PARTITION BY name ORDER BY at) AS prev
              FROM slo_status) x
        GROUP BY 1, 2""",
    "task": """
        SELECT 'task:' || task_id, 'scheduler_runs',
               count(*) FILTER (WHERE t >= %(since)s),
               count(*) FILTER (WHERE t >= %(since)s AND NOT good),
               count(*) FILTER (WHERE t >= %(since)s AND prev IS NOT NULL AND prev <> good),
               max(t) FILTER (WHERE NOT good),
               count(*) FILTER (WHERE NOT good),
               0
        FROM (SELECT task_id, to_timestamp(started_at / 1000.0) AS t, status = 'success' AS good,
                     lag(status = 'success') OVER (PARTITION BY task_id ORDER BY started_at) AS prev
              FROM scheduler_runs WHERE status IN ('success', 'failure', 'timeout', 'orphaned')) x
        GROUP BY 1, 2""",
    "alerts": """
        WITH daily AS (
            SELECT category, date_trunc('day', ts) AS d, bool_or(level IN ('warning', 'critical')) AS red,
                   count(*) FILTER (WHERE title ~* %(test_re)s OR meta ? 'test') AS tests
            FROM telemetry.events WHERE category IS NOT NULL GROUP BY 1, 2),
        span AS (SELECT category, min(d) AS first_d FROM daily GROUP BY 1),
        days AS (SELECT s.category, g.d, coalesce(dl.red, false) AS red, coalesce(dl.tests, 0) AS tests
                 FROM span s CROSS JOIN LATERAL generate_series(s.first_d, date_trunc('day', now()), interval '1 day') g(d)
                 LEFT JOIN daily dl ON dl.category = s.category AND dl.d = g.d)
        SELECT 'alerts:' || category, 'telemetry.events',
               count(*) FILTER (WHERE d >= %(since)s),
               count(*) FILTER (WHERE d >= %(since)s AND red),
               count(*) FILTER (WHERE d >= %(since)s AND prev IS NOT NULL AND prev <> red),
               max(d) FILTER (WHERE red),
               count(*) FILTER (WHERE red),
               sum(tests)
        FROM (SELECT category, d, red, tests, lag(red) OVER (PARTITION BY category ORDER BY d) AS prev FROM days) x
        GROUP BY 1, 2""",
}


def log(m: str) -> None:
    print(f"[chanos {datetime.now():%H:%M:%S}] {m}", flush=True)


# --------------------------------------------------------------- pure helpers

def verdict(readings: int, bad: int, ever_bad: int, test_rows: int, black_box: bool = False) -> str:
    """The Chanos verdict for one series. Pure."""
    if black_box:
        return "black_box"
    if not readings:
        return "silent"
    if bad >= readings:
        return "always_red"
    if bad == 0:
        if ever_bad or test_rows:
            return "never_red_proven"
        return "too_young" if readings < MIN_READINGS else "never_red_unproven"
    return "live"


def is_critical(series: str) -> bool:
    """Is this series a critical monitor? Pure."""
    kind, _, name = series.partition(":")
    if kind in ("probe", "slo"):
        return True
    if kind == "task":
        return bool(CRIT_TASK_RE.search(name))
    if kind == "alerts":
        return name in CRIT_ALERTS
    if kind == "organ":
        return name in CRIT_ORGANS
    if kind == "health":
        return bool(CRIT_RE.search(name.split("@")[0]))
    return False


def injection_idea(series: str) -> str:
    """One concrete fault-injection proposal for a series. Never executed here. Pure."""
    kind, _, name = series.partition(":")
    svc = name.split("@")[0]
    if kind == "health" and name.endswith("/core_liveness"):
        return ("nova_core_liveness.heartbeat() writes status 'up' as a literal, so this series cannot go red. "
                "Run it once with one KEYSTONES entry pointed at a closed port and assert it pages and writes "
                "a 'down' row; then derive the heartbeat status from the run's result.")
    if kind == "health" and name.endswith("/nova_task_sentinel"):
        return ("the sentinel's heartbeat writes 'up' even when it lists degraded tasks. Run it against a scratch "
                "copy of scheduler_runs holding one task with 5 straight failures; assert an escalation and make "
                "the heartbeat status 'degraded' when the list is non-empty.")
    if kind == "health" and "/mac-studio" in name:
        return (f"the mesh agent's check for {svc} is a TCP connect or HTTP <500. Register a decoy "
                f"service_registry row ('{svc}_decoy') on a port with nothing listening for one reconcile cycle; "
                "assert a 'down' row lands and service_down_watch fires (then delete the decoy).")
    if kind == "health":
        return (f"stop or firewall {svc} on one node for a single check interval in a maintenance window "
                "(or point the checker at a black-hole address in a test run) and assert a non-up row.")
    if kind == "probe":
        return (f"run nova_prober once with the {name} probe's target replaced by a black-hole address "
                "(monkeypatched in a one-off run) and assert an ok=false row plus a PROBE FAIL event.")
    if kind == "slo":
        return (f"feed the {name} SLI query a CTE of synthetic bad events (no table writes) and assert "
                "nova_slo.evaluate() returns 'page'; then check _alert() routes it in a dry run.")
    if kind == "task":
        return (f"trigger one manual run of {name} with NOVA_PG_HOST=invalid.invalid and assert "
                "scheduler_runs records a failure and nova_task_sentinel escalates it.")
    if kind == "alerts":
        return (f"emit one notify(level='warning', category='{name}', title='TEST: chanos drill', "
                "meta={'test': True}) and confirm the notifier routes it; the test flag counts as proof here.")
    if kind == "organ":
        return (f"give the {name} organ a history table (or log its state changes): a board that only "
                "shows its latest row can never be shown to go red.")
    return "inject one known-bad reading in a test run and assert the monitor reports it"


def build_rows(raw: list[tuple], organs: list[tuple]) -> list[dict]:
    """raw: (series, source, readings, bad, flips, last_bad, ever_bad, test_rows); organs: (organ, state). Pure."""
    out = []
    for series, source, n, bad, flips, last_bad, ever_bad, tests in raw:
        n, bad = int(n or 0), int(bad or 0)
        if not n:
            continue                                  # nothing in the window: not monitored any more
        v = verdict(n, bad, int(ever_bad or 0), int(tests or 0))
        out.append({"series": series, "source": source, "readings": n, "bad": bad,
                    "bad_share": round(bad / n, 6), "flips": int(flips or 0), "last_bad": last_bad,
                    "verdict": v, "critical": is_critical(series)})
    for organ, state in organs:
        series = f"organ:{organ}"
        bad = int((state or "").lower() in ORGAN_RED)
        out.append({"series": series, "source": "organ_board", "readings": 1, "bad": bad, "bad_share": float(bad),
                    "flips": 0, "last_bad": None, "verdict": verdict(1, bad, 0, 0, black_box=True),
                    "critical": is_critical(series)})
    for r in out:
        r["idea"] = injection_idea(r["series"]) if r["critical"] and r["verdict"] == "never_red_unproven" else None
    return out


def top_unproven(rows: list[dict], n: int = TOP_IDEAS) -> list[dict]:
    """Critical never-red unproven series, most-trusted (most readings) first. Pure."""
    c = [r for r in rows if r["critical"] and r["verdict"] == "never_red_unproven"]
    return sorted(c, key=lambda r: (-r["readings"], r["series"]))[:n]


def measure(rows: list[dict]) -> dict:
    """The monthly number and its context. Pure."""
    crit = [r for r in rows if r["critical"]]
    by = {}
    for r in rows:
        by[r["verdict"]] = by.get(r["verdict"], 0) + 1
    return {"series": len(rows), "critical": len(crit),
            "critical_never_red_unproven": sum(1 for r in crit if r["verdict"] == "never_red_unproven"),
            "critical_never_red_proven": sum(1 for r in crit if r["verdict"] == "never_red_proven"),
            "critical_always_red": sum(1 for r in crit if r["verdict"] == "always_red"),
            "verdicts": by}


def month_of(d: datetime | date) -> date:
    return date(d.year, d.month, 1)


# --------------------------------------------------------------- I/O

def connect():
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=10"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = True
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def gather(cur, now: datetime) -> list[dict]:
    """Read every source (read-only). A source that fails is reported and skipped, not fatal."""
    since = now - timedelta(days=WINDOW_DAYS)
    raw = []
    for name, sql in SOURCES.items():
        t = time.monotonic()
        try:
            cur.execute(sql, {"since": since, "test_re": TEST_RE})
            got = cur.fetchall()
            raw += got
            log(f"{name}: {len(got)} series ({time.monotonic() - t:.1f}s)")
        except Exception as e:
            log(f"{name}: source failed, skipped: {type(e).__name__}: {e}")
    try:
        cur.execute("SELECT organ, state FROM organ_board")
        organs = cur.fetchall()
    except Exception as e:
        log(f"organ_board: failed, skipped: {e}")
        organs = []
    return build_rows(raw, organs)


def previous_unproven(cur, month: date) -> set | None:
    """Unproven critical series as of the latest month on record (this one or the one before). None: first run."""
    cur.execute("SELECT max(month) FROM chanos_filter WHERE month <= %s", (month,))
    last = (cur.fetchone() or (None,))[0]
    if last is None:
        return None
    cur.execute("SELECT series FROM chanos_filter WHERE month = %s AND critical AND verdict = 'never_red_unproven'",
                (last,))
    return {r[0] for r in cur.fetchall()}


def write(cur, rows: list[dict], month: date) -> int:
    for r in rows:
        cur.execute("INSERT INTO chanos_filter (month, series, source, readings, bad, bad_share, flips, last_bad, "
                    "verdict, critical, idea, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
                    "ON CONFLICT (month, series) DO UPDATE SET source = EXCLUDED.source, readings = EXCLUDED.readings, "
                    "bad = EXCLUDED.bad, bad_share = EXCLUDED.bad_share, flips = EXCLUDED.flips, "
                    "last_bad = EXCLUDED.last_bad, verdict = EXCLUDED.verdict, critical = EXCLUDED.critical, "
                    "idea = EXCLUDED.idea, updated_at = now()",
                    (month, r["series"], r["source"], r["readings"], r["bad"], r["bad_share"], r["flips"],
                     r["last_bad"], r["verdict"], r["critical"], r["idea"]))
    return len(rows)


def alert(new: list[dict], m: dict, month: date) -> bool:
    from nova_notify import notify
    lines = [f"- {r['series']} ({r['readings']} green readings): {r['idea']}" for r in new[:8]]
    return notify(f"Chanos filter: {len(new)} critical monitor(s) newly never-red and unproven",
                  body=("Green for 90 days and never once shown able to go red. "
                        f"{m['critical_never_red_unproven']} critical series are unproven in all.\n" + "\n".join(lines)),
                  level="info", category="chanos", dedup_key=f"chanos:{month:%Y-%m}:{len(new)}",
                  meta={"new": [r["series"] for r in new], "measure": m["critical_never_red_unproven"]})


def print_rows(rows: list[dict], n: int) -> None:
    m = measure(rows)
    print(f"\n{m['series']} series, {m['critical']} critical. Verdicts: "
          + ", ".join(f"{k} {v}" for k, v in sorted(m["verdicts"].items())))
    print(f"MEASURE: critical never-red unproven = {m['critical_never_red_unproven']}  "
          f"(proven {m['critical_never_red_proven']}, always-red {m['critical_always_red']})")
    print(f"\nTop {n} unproven critical monitors (most-trusted first) and a fault-injection idea each:")
    for r in top_unproven(rows, n):
        print(f"  {r['series']:<58} {r['readings']:>7} readings, 0 bad, {r['flips']} flips")
        print(f"      idea: {r['idea']}")
    ar = [r for r in rows if r["critical"] and r["verdict"] == "always_red"]
    if ar:
        print("\nAlways red (critical): " + ", ".join(r["series"] for r in ar))
    bb = [r["series"] for r in rows if r["verdict"] == "black_box"]
    if bb:
        print(f"Black boxes (no history kept): {', '.join(bb)}")


def report(cur, n: int) -> None:
    cur.execute("SELECT to_regclass('chanos_filter') IS NOT NULL")
    if not (cur.fetchone() or (False,))[0]:
        print("No Chanos filter runs yet (table not created; the first scheduled run creates it).")
        return
    cur.execute("SELECT max(month) FROM chanos_filter")
    month = cur.fetchone()[0]
    cur.execute("SELECT series, source, readings, bad, bad_share, flips, last_bad, verdict, critical, idea "
                "FROM chanos_filter WHERE month = %s", (month,))
    cols = ("series", "source", "readings", "bad", "bad_share", "flips", "last_bad", "verdict", "critical", "idea")
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"Chanos filter, {month:%Y-%m}:")
    print_rows(rows, n)
    cur.execute("SELECT to_char(month, 'YYYY-MM'), count(*) FILTER (WHERE critical AND verdict = 'never_red_unproven'), "
                "count(*) FILTER (WHERE critical), count(*) FROM chanos_filter GROUP BY 1 ORDER BY 1 DESC LIMIT 12")
    print("\nMonthly (continuity review): critical never-red unproven / critical / all series")
    for mth, u, c, a in cur.fetchall():
        print(f"  {mth}  {u:>4} / {c:>4} / {a:>4}")


def selftest() -> int:
    assert verdict(100, 0, 0, 0) == "never_red_unproven"
    assert verdict(100, 0, 3, 0) == "never_red_proven" and verdict(100, 0, 0, 1) == "never_red_proven"
    assert verdict(10, 10, 10, 0) == "always_red" and verdict(10, 2, 2, 0) == "live"
    assert verdict(1, 0, 0, 0, black_box=True) == "black_box" and verdict(0, 0, 0, 0) == "silent"
    assert verdict(MIN_READINGS - 1, 0, 0, 0) == "too_young"
    assert is_critical("probe:embedding") and is_critical("health:postgresql@mac-studio/mac-studio")
    assert not is_critical("health:plex@nova-core/mac-studio") and is_critical("task:prober")
    rows = build_rows([("health:core_liveness@mac-studio/core_liveness", "health_checks", 50, 0, 0, None, 0, 0),
                       ("probe:postgres", "telemetry.probe_results", 50, 2, 4, None, 2, 0)], [("affect", "calm")])
    assert [r["verdict"] for r in rows] == ["never_red_unproven", "live", "black_box"]
    assert "literal" in rows[0]["idea"] and rows[1]["idea"] is None
    assert measure(rows)["critical_never_red_unproven"] == 1
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="daily: compute, upsert this month, notify new unproven")
    ap.add_argument("--dry-run", action="store_true", help="compute and print; write and notify nothing")
    ap.add_argument("--report", nargs="?", const=TOP_IDEAS, type=int, metavar="N", help="this month's list + monthly measure")
    ap.add_argument("--selftest", action="store_true", help="pure-logic assertions, no I/O")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.scheduled or a.dry_run or a.report is not None):
        ap.print_help()
        return 0
    t0 = time.monotonic()
    conn = connect()
    cur = conn.cursor()
    try:
        if a.report is not None:
            report(cur, a.report)
            return 0
        now = datetime.now(timezone.utc)
        rows = gather(cur, now)
        if a.dry_run:
            print_rows(rows, TOP_IDEAS)
            print(f"\n[dry-run] {len(rows)} series over {WINDOW_DAYS} days, {time.monotonic() - t0:.1f}s — nothing written")
            return 0
        ensure_schema(cur)
        month = month_of(now)
        before = previous_unproven(cur, month)
        write(cur, rows, month)
        m = measure(rows)
        new = [r for r in top_unproven(rows, 10_000) if before is not None and r["series"] not in before]
        sent = False
        if new:                         # the very first run seeds silently; later runs report entrants
            try:
                sent = alert(new, m, month)
            except Exception as e:
                log(f"notify failed (rows still written): {e}")
        log(f"{len(rows)} series; critical never-red unproven: {m['critical_never_red_unproven']}; "
            f"{len(new)} new{' (notified)' if sent else ''} ({time.monotonic() - t0:.1f}s)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
