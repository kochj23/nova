#!/usr/bin/env python3
"""nova_chronic_failures.py — wish #63: chronic scheduler failures become queue items.

Any task with more than FAIL_PER_DAY non-success runs on each of the last DAYS full days (scheduler_runs is
shared by both schedulers) becomes ONE open claude_queue item: "Chronic failure: <task> — fix or retire",
carrying the per-day counts and the last error tail. One open item per task; a closed item lets a new one
form if the task keeps failing. Nothing else is written. --dry-run --selftest.
Jordan 2026-10-04: approved wish #63 ("approve all the wishes and make them happen").

MERGED 2026-10-09 (organ-audit merge M2): the daily run is now `nova_task_sentinel.py --daily`
(same thresholds, query, wording and queue session). main() is a thin wrapper for it; chronic()
and the thresholds stay here because nova_task_sentinel and nova_busab import them.
"""
import nova_dsn as _nova_dsn  # noqa: E402
import sys, os
from datetime import date, timedelta
import psycopg2

DSN = os.environ.get("NOVA_OPS_DSN", _nova_dsn.pg_dsn("nova_ops"))
FAIL_PER_DAY = 5
DAYS = 3
PREFIX = "Chronic failure: "
OPEN = ("queued", "pending", "in_progress", "claimed")

def log(m): print(f"[chronic-failures] {m}", flush=True)

def chronic(rows, today, fail_per_day=FAIL_PER_DAY, days=DAYS):
    """rows: (task_id, day, failures). Pure. -> {task: {day: n}} for tasks over the bar on EVERY one of the
    last `days` full days (yesterday back)."""
    want = {today - timedelta(days=i) for i in range(1, days + 1)}
    per = {}
    for task, d, n in rows:
        if d in want:
            per.setdefault(task, {})[d] = n
    return {t: c for t, c in per.items() if len(c) == len(want) and all(n > fail_per_day for n in c.values())}

def main():
    log("merged into nova_task_sentinel.py (--daily) on 2026-10-09; running that mode")
    import nova_task_sentinel
    nova_task_sentinel.run_daily(dry_run="--dry-run" in sys.argv)

def selftest():
    t = date(2026, 10, 4); y = lambda i: t - timedelta(days=i)
    rows = [("prober", y(1), 17), ("prober", y(2), 11), ("prober", y(3), 9),      # chronic
            ("flaky", y(1), 17), ("flaky", y(2), 2), ("flaky", y(3), 9),            # one quiet day -> not chronic
            ("today_only", t, 40),                                                 # today is partial -> ignored
            ("exact", y(1), 5), ("exact", y(2), 5), ("exact", y(3), 5)]            # 5 is not "more than 5"
    assert set(chronic(rows, t)) == {"prober"}, chronic(rows, t)
    assert chronic([], t) == {}
    print("selftest ok")

if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
