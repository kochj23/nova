#!/usr/bin/env python3
"""
nova_output_drift.py — the constant-output detector (six-month build #2, 2026-09-28).

Three of Nova's worst episodes were one bug wearing different clothes: the weekly
reliability report posting "0.0% success rate, 0 runs" for ten Sundays; the proactive
digest posting "NOTHING" five mornings running; nova-core6 answering health checks for
months while every generate returned 500. In each case a job kept RUNNING and kept
producing an empty or unchanging result, and nothing noticed because the job was "up".
A mind that says nothing looks identical to a mind that has nothing to say — unless
something hashes the saying.

Probes, all read-only, each raising a state-change-deduped warning on the event bus:
  1. unchanged output — a communicator task whose last N stdout_tails normalise to the same
     hash (timestamps stripped, numbers KEPT: "posted 12 items" vs "posted 13 items" is change,
     "**Nothing**" five times is not).
  2. empty output — a communicator task whose last N runs printed nothing at all (silent
     flush/aggregate jobs are normal; a silent report is not).
  3. chronic down — a (node, service) whose health checks have been 'down' for every check
     in the last CHRONIC_HOURS with no 'up' at all: the voiceless-node case.
  4. stuck loop (2026-10-02) — an approved co-agency proposal re-failing identically every
     executor run (reach #116 failed 20 times with an empty error). Retrying is not progress;
     same failure N times is a wall. The scheduler-task half of this probe (a task failing
     identically STUCK_N times) moved to nova_task_sentinel.py on 2026-10-09 (organ-audit
     merge M2: one scheduler-failure organ).
  5. missed deliveries (from nova_dead_mans_switch.py, 2026-10-09, merge M1) — did today's
     scheduled deliveries (morning brief, mail summaries) actually succeed, read from
     scheduler_runs. One warning per distinct set of misses per day, dedup key
     'dead-mans-switch-recovery'. Read-only: it no longer re-runs the delivery script.

  nova_output_drift.py              # run (raises warnings via nova_notify)
  nova_output_drift.py --dry-run    # print findings, notify nothing
  nova_output_drift.py --deliveries # only the missed-delivery check (the dead man's switch)
  nova_output_drift.py --selftest   # pure-logic assertions
"""
import argparse
import hashlib
import re
import sys
from datetime import datetime

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# ── tunables ──────────────────────────────────────────────────────────────────
RUNS_N = 5                  # consecutive runs that must agree before we call it constant
WINDOW_DAYS = 10            # how far back to look for those runs
CHRONIC_HOURS = 24          # a service down for every check this long is chronic, not flapping
CHRONIC_MIN_CHECKS = 6      # ...but only if it was actually checked at least this often
STUCK_N = 5                 # identical consecutive failures before a retry loop is called stuck
DELIVERY_TZ = "America/Los_Angeles"
DELIVERY_HISTORY_DAYS = 14  # a delivery task with no run at all in this long is retired: not checked
# Deliveries to verify (carried over from nova_dead_mans_switch.py): (task_id, check_after_hour, label)
DELIVERIES = [
    ("morning_brief", 9, "Morning Brief (7am)"),
    ("mail_deliver_am", 9, "Morning Mail Summary (8am)"),
    ("mail_deliver_pm", 19, "Evening Mail Summary (6pm)"),
]
# tasks whose job is to SAY something; a constant answer from these is the bug we hunt
# an UNCHANGED tail only counts when it is the shape of a non-answer; "Checking state..." repeating
# is a log line, "detections=0" / "LLM generation failed" / "**Nothing**" repeating is the bug.
ZEROISH_RE = re.compile(r"nothing|skip|fail|error|refused|unavailable|\b0 (runs|items|messages|emails|"
                        r"records|memories)|=0\b|no (new|scene|activations|activity|items|data|candidates)|"
                        r"\b0\.0%|not publishing|nothing to", re.I)
COMMUNICATOR_RE = re.compile(r"digest|report|brief|journal|summary|proactive|scoreboard|column|"
                             r"newsletter|postmortem|weekly|daily|essay|opinion|notify|email", re.I)


def log(m):
    print(f"[output-drift {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure logic (unit-tested in demo()) ────────────────────────────────────────

_TS_RE = re.compile(r"\[[^\]]*\d{2}:\d{2}:\d{2}[^\]]*\]")   # "[proactive-digest 08:00:13]"
_CLOCK_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]?\d{0,2}:?\d{0,2}:?\d{0,2}|\b\d{1,2}:\d{2}(:\d{2})?\b")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s%]")


def normalise(text):
    """Strip the parts of an output that legitimately differ run to run (timestamps, numbers,
    whitespace) so only the *shape* of what was said remains."""
    t = _TS_RE.sub("", text or "")
    t = _CLOCK_RE.sub("", t)
    t = _PUNCT_RE.sub("", t)                 # "**Nothing.**" == "Nothing" == "nothing"
    t = _WS_RE.sub(" ", t).strip().lower()
    return t


def shape_hash(text):
    return hashlib.sha1(normalise(text).encode()).hexdigest()[:12]


def classify(tails, n=RUNS_N):
    """tails: newest-first list of stdout_tail strings. Returns 'empty' | 'unchanged' | None."""
    if len(tails) < n:
        return None
    recent = tails[:n]
    if all(not (t or "").strip() for t in recent):
        return "empty"
    hashes = {shape_hash(t) for t in recent}
    return "unchanged" if len(hashes) == 1 else None


def stuck(tails, statuses, n=STUCK_N):
    """Newest-first run tails/statuses. True when the last n runs ALL failed with one error shape."""
    if len(tails) < n or len(statuses) < n:
        return False
    if any(st in ("success", "ok", "completed") for st in statuses[:n]):
        return False
    return len({shape_hash(t or "") for t in tails[:n]}) == 1


def chronic(statuses, min_checks=CHRONIC_MIN_CHECKS):
    """statuses: list of health_checks.status over the window. Chronic iff enough checks and
    none of them was up."""
    if len(statuses) < min_checks:
        return False
    return not any(s in ("up", "ok", "healthy", "slow") for s in statuses)


# ── probes (read-only) ────────────────────────────────────────────────────────

def probe_tasks(cur):
    cur.execute(
        "SELECT task_id, task_script, stdout_tail FROM scheduler_runs "
        "WHERE status IN ('success','ok','completed') "
        "AND to_timestamp(started_at/1000.0) > now() - (%s || ' days')::interval "
        "ORDER BY task_id, started_at DESC", (str(WINDOW_DAYS),))
    by_task = {}
    for tid, script, tail in cur.fetchall():
        by_task.setdefault((tid, script), []).append(tail or "")
    out = []
    for (tid, script), tails in by_task.items():
        kind = classify(tails)
        if not COMMUNICATOR_RE.search(f"{tid} {script}"):
            continue
        if kind == "unchanged" and not ZEROISH_RE.search(tails[0] or ""):
            continue   # a constant log line, not a constant non-answer
        if kind:
            out.append({"task": tid, "script": script, "kind": kind, "n": min(len(tails), RUNS_N),
                        "sample": (tails[0] or "")[-160:].strip()})
    return out


def probe_digest(cur):
    """The proactive digest keeps its own log: flag when its last N posts say the same thing."""
    cur.execute("SELECT items->>'digest' FROM proactive_digest_log WHERE posted "
                "ORDER BY ts DESC LIMIT %s", (RUNS_N,))
    tails = [r[0] or "" for r in cur.fetchall()]
    kind = classify(tails)
    if kind == "unchanged" and not ZEROISH_RE.search(tails[0]):
        kind = None
    return [{"task": "proactive_digest(posted)", "script": "nova_proactive_digest.py", "kind": kind,
             "n": len(tails), "sample": tails[0][:160] if tails else ""}] if kind else []


def probe_chronic(cur):
    cur.execute(
        "SELECT node_name, service_name, array_agg(status ORDER BY checked_at DESC) "
        "FROM health_checks WHERE checked_at > now() - (%s || ' hours')::interval "
        "GROUP BY 1, 2", (str(CHRONIC_HOURS),))
    return [{"node": n, "service": s, "checks": len(st)} for n, s, st in cur.fetchall() if chronic(st)]


def probe_stuck(cur):
    """Approved co-agency proposals the executor keeps re-failing identically (every 15 min,
    forever). Failing SCHEDULER tasks are nova_task_sentinel's job since 2026-10-09."""
    out = []
    cur.execute("SELECT event, detail FROM coagency_log WHERE event IN ('executed','execute_failed','execute_gave_up') "
                "AND ts > now() - interval '%s days' ORDER BY ts DESC", (WINDOW_DAYS,))
    byp = {}
    for ev, detail in cur.fetchall():
        m = re.match(r"#(\d+)", detail or "")
        if m:
            evs, tails = byp.setdefault(m.group(1), ([], []))
            evs.append(ev); tails.append(detail)
    for pid, (evs, tails) in byp.items():
        if len(evs) >= STUCK_N and all(e == "execute_failed" for e in evs[:STUCK_N]) and stuck(tails, ["failure"]*STUCK_N):
            out.append({"task": f"coagency#{pid}", "script": "nova_coagency.py --mode execute-approved", "n": STUCK_N,
                        "host": "nova-core", "sample": normalise(tails[0])[-160:]})
    return out


def delivered(rows, hour, today, deliveries=None):
    """Pure. rows: (task_id, delivered_today bool, runs_in_history int). Returns
    (missed [(task_id, label)], skipped [(task_id, why)]). A delivery is checked only once
    its check hour has passed, and only if it ran at all in the history window."""
    seen = {t: (bool(ok), n) for t, ok, n in rows}
    missed, skipped = [], []
    for task_id, min_hour, label in (deliveries or DELIVERIES):
        if hour < min_hour:
            skipped.append((task_id, f"too early (now={hour}h, check after {min_hour}h)"))
        elif task_id not in seen or not seen[task_id][1]:
            skipped.append((task_id, f"no runs in {DELIVERY_HISTORY_DAYS}d (retired?)"))
        elif not seen[task_id][0]:
            missed.append((task_id, label))
    return missed, skipped


def probe_deliveries(cur):
    """Read-only: which deliveries had no successful run today (local time) past their hour."""
    cur.execute("SELECT extract(hour from now() AT TIME ZONE %s)::int, (now() AT TIME ZONE %s)::date",
                (DELIVERY_TZ, DELIVERY_TZ))
    hour, today = cur.fetchone()
    cur.execute(
        "SELECT task_id, bool_or(status IN ('success','ok','completed') AND "
        "(to_timestamp(started_at/1000.0) AT TIME ZONE %s)::date = %s), count(*) "
        "FROM scheduler_runs WHERE task_id = ANY(%s) "
        "AND started_at > (extract(epoch from now()) - %s*86400)*1000 GROUP BY 1",
        (DELIVERY_TZ, today, [d[0] for d in DELIVERIES], DELIVERY_HISTORY_DAYS))
    return delivered(cur.fetchall(), hour, today) + (hour, today)


def run_deliveries(cur, dry_run=False, notify=None):
    """The dead man's switch: one warning listing today's missed deliveries. Self-deduped:
    the same set of misses is raised once per local day. Returns the missed list."""
    missed, skipped, hour, today = probe_deliveries(cur)
    for task_id, why in skipped:
        log(f"  delivery {task_id}: skipped, {why}")
    for task_id, label in missed:
        log(f"  delivery {task_id}: MISSING (no successful run on {today})")
    if not missed:
        log(f"deliveries: all checked deliveries confirmed for {today}")
        return missed
    key = ",".join(sorted(t for t, _ in missed))
    title = "Dead Man's Switch — Missed Deliveries"
    body = "\n".join(f"`{label}` ({t}): no successful run today ({today})" for t, label in missed)
    if dry_run:
        print(f"• [warning] {title}\n    {body}")
        return missed
    cur.execute("SELECT 1 FROM telemetry.events WHERE dedup_key='dead-mans-switch-recovery' "
                "AND meta->>'missing' = %s AND ts > (%s::date)::timestamp AT TIME ZONE %s LIMIT 1",
                (key, today, DELIVERY_TZ))
    if cur.fetchone():
        log(f"deliveries: '{key}' already raised today")
        return missed
    if notify is None:
        from nova_notify import notify
    notify(title, body=body, level="warning", category="scheduler", source="nova_output_drift",
           dedup_key="dead-mans-switch-recovery", meta={"missing": key, "day": str(today)})
    return missed


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nova's constant-output detector")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--deliveries", action="store_true",
                    help="only the missed-delivery check (what nova_dead_mans_switch did)")
    args = ap.parse_args(argv)
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        log(f"no PG ({e}) — fail-open"); return 0
    conn.autocommit = True
    cur = conn.cursor()

    try:
        run_deliveries(cur, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f"probe_deliveries failed ({e})")
    if args.deliveries:
        return 0

    findings = []
    for probe in (probe_tasks, probe_digest):
        try:
            for f in probe(cur):
                findings.append(("warning", f"output_drift:{f['task']}:{f['kind']}",
                                 f"Constant output: {f['task']} ({f['kind']} for {f['n']} runs)",
                                 f"{f['script']} has printed {'nothing' if f['kind']=='empty' else 'the same thing'} "
                                 f"for its last {f['n']} runs. A job that keeps running and keeps saying the same "
                                 f"thing is usually a broken feed, not a quiet one. Last: {f['sample'] or '(empty)'}",
                                 None))
        except Exception as e:  # noqa: BLE001
            log(f"{probe.__name__} failed ({e})")
    try:
        for f in probe_stuck(cur):
            findings.append(("warning", f"output_drift:stuck:{f['task']}",
                             f"Stuck loop: {f['task']} failed identically {f['n']}x in a row",
                             f"{f['script']} has failed its last {f['n']} runs with the same error. Retrying is "
                             f"not progress; this needs a different action, not another attempt. Last: {f['sample']}",
                             f["host"]))
    except Exception as e:  # noqa: BLE001
        log(f"probe_stuck failed ({e})")
    try:
        for c in probe_chronic(cur):
            findings.append(("warning", f"output_drift:chronic:{c['node']}:{c['service']}",
                             f"Chronic down: {c['service']} on {c['node']} ({CHRONIC_HOURS}h, never up)",
                             f"{c['checks']} health checks in {CHRONIC_HOURS}h and not one was up. This is not "
                             f"flapping; it is a node answering roll call with no voice. Needs hands.", None))
    except Exception as e:  # noqa: BLE001
        log(f"probe_chronic failed ({e})")

    log(f"{len(findings)} finding(s)")
    if args.dry_run:
        for lvl, key, title, body, host in findings:
            print(f"• [{lvl}] {title} ({host or '?'})\n    {body}")
        return 0
    if findings:
        import nova_notify
        # self-dedup on the bus: one event per key per day (the notifier dedups delivery, but the
        # hourly run was still writing ~9 rows an hour into telemetry.events)
        cur.execute("SELECT dedup_key FROM telemetry.events WHERE source='nova_output_drift' "
                    "AND ts > now() - interval '24 hours'")
        seen = {r[0] for r in cur.fetchall()}
        for lvl, key, title, body, host in findings:
            if key in seen:
                continue
            nova_notify.notify(title, body, level=lvl, category="output_drift",
                               source="nova_output_drift", dedup_key=key,
                               meta={"host": host} if host else None)   # host -> correlator opens an incident
    return 0


def demo():
    assert normalise("[proactive-digest 08:00:13] posted 12 items") == normalise("[x 09:00:00] posted 12 items")
    assert normalise("posted 12 items") != normalise("posted 13 items")
    assert shape_hash("**Nothing**") == shape_hash("**NOTHING**  ") == shape_hash("Nothing.")
    assert classify(["**Nothing**"] * 5) == "unchanged"
    assert classify(["", "  ", "\n", "", ""]) == "empty"
    assert classify(["a", "b", "a", "a", "a"]) is None
    assert classify(["same"] * 4) is None                       # not enough runs yet
    assert classify(["0.0% success rate, 0 runs"] * 5) == "unchanged"
    assert stuck(["mount error: EPERM at 09:01"] * 5, ["failure"] * 5)
    assert not stuck(["mount error: EPERM"] * 5, ["failure"] * 4 + ["success"])   # it recovered
    assert not stuck(["err A", "err B", "err A", "err A", "err A"], ["failure"] * 5)  # different walls
    assert not stuck(["same"] * 4, ["failure"] * 4)                               # not enough runs yet
    rows = [("morning_brief", True, 3), ("mail_deliver_pm", False, 3)]
    assert delivered(rows, 20, None) == ([("mail_deliver_pm", "Evening Mail Summary (6pm)")],
                                         [("mail_deliver_am", "no runs in 14d (retired?)")])
    assert delivered(rows, 10, None)[0] == []                                      # pm not due yet
    assert delivered([("morning_brief", False, 3)], 8, None)[0] == []              # too early
    assert classify(["reported 12 nearby incidents"] * 3 + ["reported 9 nearby incidents"] * 2) is None
    assert ZEROISH_RE.search("[karr_report 2026-09-28] detections=0") and ZEROISH_RE.search("**Nothing**")
    assert ZEROISH_RE.search("0.0% success rate, 0 runs") and ZEROISH_RE.search("LLM generation failed")
    assert not ZEROISH_RE.search("[nova_peace 11:36:43] Checking Jordan's state...")
    assert not ZEROISH_RE.search("[scanner-digest 11:10:18] 1 digest(s) from 1 group(s)")
    assert chronic(["down"] * 6) and not chronic(["down"] * 5) and not chronic(["down", "up"] + ["down"] * 5)
    assert not chronic(["down"] * 5 + ["slow"])
    print("all output-drift assertions passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        demo()
    else:
        sys.exit(main())
