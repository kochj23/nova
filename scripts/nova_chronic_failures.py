#!/usr/bin/env python3
"""nova_chronic_failures.py — wish #63: chronic scheduler failures become queue items.

Any task with more than FAIL_PER_DAY non-success runs on each of the last DAYS full days (scheduler_runs is
shared by both schedulers) becomes ONE open claude_queue item: "Chronic failure: <task> — fix or retire",
carrying the per-day counts and the last error tail. One open item per task; a closed item lets a new one
form if the task keeps failing. Nothing else is written. --dry-run --selftest.
Jordan 2026-10-04: approved wish #63 ("approve all the wishes and make them happen").
"""
import sys, os
from datetime import date, timedelta
import psycopg2

DSN = os.environ.get("NOVA_OPS_DSN", "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
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
    dry = "--dry-run" in sys.argv
    conn = psycopg2.connect(DSN, connect_timeout=8); conn.autocommit = True; cur = conn.cursor()
    cur.execute("""SELECT task_id, (to_timestamp(ended_at/1000.0) AT TIME ZONE 'America/Los_Angeles')::date, count(*)
                   FROM scheduler_runs WHERE status <> 'success'
                   AND ended_at > extract(epoch from now() - interval '%s days') * 1000 GROUP BY 1, 2""", (DAYS + 1,))
    found = chronic(cur.fetchall(), date.today())
    made = 0
    for task, counts in sorted(found.items()):
        cur.execute("SELECT 1 FROM claude_queue WHERE description LIKE %s AND status IN %s LIMIT 1", (PREFIX + task + " %", OPEN))
        if cur.fetchone():
            log(f"{task}: already queued"); continue
        cur.execute("SELECT status, left(coalesce(error_tail, stdout_tail, ''), 400) FROM scheduler_runs WHERE task_id=%s "
                    "AND status <> 'success' ORDER BY ended_at DESC LIMIT 1", (task,))
        st, tail = cur.fetchone() or ("?", "")
        avg = sum(counts.values()) / len(counts)
        desc = f"{PREFIX}{task} — {avg:.0f} non-success runs/day for {DAYS} days: fix or retire (wish #63)"
        ctx = ("per-day: " + ", ".join(f"{d}={n}" for d, n in sorted(counts.items())) +
               f"\nlast status: {st}\nlast error tail: {tail}\n"
               "Decide: fix the cause, or retire the task (disable in scheduler yaml with a one-line reason). "
               "Do not just suppress the alert.")
        if dry:
            log(f"would queue: {desc}"); continue
        cur.execute("SELECT session_id FROM claude_sessions ORDER BY started_at DESC LIMIT 1")
        sid = (cur.fetchone() or ["chronic-failures"])[0]
        cur.execute("INSERT INTO claude_queue (session_id, created_at, updated_at, status, priority, description, context) "
                    "VALUES (%s, now(), now(), 'queued', 4, %s, %s)", (sid, desc, ctx))
        made += 1; log(f"queued: {desc}")
    log(f"{len(found)} chronic task(s), {made} new queue item(s)")

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
