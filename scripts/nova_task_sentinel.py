#!/usr/bin/env python3
"""nova_task_sentinel.py — cross-host, exit-code-based escalation for scheduler tasks.

WHY THIS EXISTS (and why it is NOT a duplicate of the two things that look like it):

Task-failure detection already had two owners, and BOTH have a structural blind spot:
  • nova_big_brother.py — the real-time enforcer, but it detects failures by
    pattern-matching error TEXT in .6's local scheduler.log (kqueue tail). It has zero
    references to scheduler_runs. So it is blind to (a) every task on the .2
    scheduler-core (different host, different log) and (b) SILENT non-zero exits that
    write no matchable error line — e.g. config_drift failing 7x in a row with an empty
    error_tail, unpaged.
  • nova_maintenance_advisor.py — reads scheduler_runs, but only the narrow 1–2
    consecutive-failure "pre-escalation" band, WEEKLY, as a priority-4 advisory. A task
    can be 100% dead for up to 7 days before it says anything.

Neither escalates, in near-real-time, on the AUTHORITATIVE signal:
scheduler_runs.exit_code / status — which is written for BOTH schedulers regardless of
what any log file says. This sentinel is exactly that missing piece and nothing more:
a 15-minute poll of scheduler_runs that classifies each task and escalates the dead
ones through the SHARED notify() bus (nova_notifier owns routing/dedup/correlation — so
this adds no parallel notification path, no new Slack wiring, no new state table).

That is how local_burbank "died every day" invisibly and how 11 tasks sat failing the
day this was written: the process was up, the log was quiet, and the one place that
knew the truth (exit_code) was never escalated cross-host. See
operations/2026-08-06-the-hoarder-s-reckoning.

Design choices:
- CADENCE IS SELF-LEARNED, not configured. A task's expected interval is the median
  gap between its *successful* runs over the window. This means no per-task config to
  maintain (the whole point — the graveyard was config no one maintained) and it
  covers BOTH schedulers (.6 scheduler.yaml + .2 scheduler-core.yaml) since it reads
  the shared table, not any one yaml.
- 'running' is NEUTRAL. Long-runners (identity_graph et al.) sit in 'running'; that is
  not a failure and must never count as one.
- FAIL-QUIET on too little data. A brand-new task with <MIN_RUNS history is not judged
  — we alert on regressions, not on newborns.

Pages at level:
- warning  → a task is failing or has gone stale (actionable, state-change).
- critical → a task is failing HARD (>=CRIT_STREAK straight, or 0 success in window
             across many attempts) — and gets a deduped claude_queue row so it lands
             in the next session's context to actually get fixed.

Run cadence: every 15m (task failures are not a second-by-second concern). One
instance, on .6. Never raises out of run_once — a watchdog must not crash.

Written by Jordan Koch.
"""
from __future__ import annotations
import sys
import statistics
from typing import Optional

DSN = "host=localhost dbname=nova_ops user=kochj"

# ── classification thresholds ────────────────────────────────────────────────
WINDOW_DAYS      = 7      # history horizon for judging a task
MIN_RUNS         = 3      # below this, task is too new/rare to judge -> healthy
FAIL_STREAK      = 3      # >= this many consecutive failures -> FAILING
CRIT_STREAK      = 6      # >= this many -> CRITICAL (and queued for a human)
STALE_FACTOR     = 3.0    # last_run older than FACTOR x learned-interval -> STALE
STALE_FLOOR_S    = 3 * 3600   # never call something stale inside 3h regardless of cadence
RECENT_GAPS      = 5      # learned cadence uses only the most recent N successful-run gaps
NOW_MS_ENV       = None   # injected in tests; None -> real clock at call site

# statuses that mean "this run did not succeed" (running/None are neutral, not failures)
_BAD = ("failure", "error", "timeout", "killed")


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def classify_task(runs: list[dict], now_ms: Optional[int] = None,
                  expected_interval_s: Optional[float] = None) -> dict:
    """Pure health classifier for one task. `runs` = list of run dicts newest-first,
    each {started_at (ms int), status (str), exit_code (int|None)}. Returns
    {state, consecutive_failures, last_run_age_s, last_ok_age_s, interval_s, reason}.
    state in {healthy, failing, critical, stale, unknown}. No IO — fully testable.
    """
    now_ms = now_ms if now_ms is not None else _now_ms()
    runs = [r for r in runs if r.get("started_at") is not None]
    if len(runs) < MIN_RUNS:
        return {"state": "unknown", "consecutive_failures": 0, "reason": "insufficient history"}

    runs = sorted(runs, key=lambda r: r["started_at"], reverse=True)  # newest first

    def is_success(r: dict) -> bool:
        st = (r.get("status") or "").lower()
        if st == "success":
            return True
        if st in _BAD:
            return False
        # unknown/blank status -> fall back to exit_code (0 == ok); 'running'/None -> neutral
        if st == "running":
            return None  # neutral sentinel handled below
        ec = r.get("exit_code")
        if ec is None:
            return None
        return ec == 0

    def is_failure(r: dict) -> bool:
        st = (r.get("status") or "").lower()
        if st in _BAD:
            return True
        if st in ("success", "running"):
            return False
        ec = r.get("exit_code")
        return ec is not None and ec != 0

    # consecutive failures from the top, skipping neutral 'running' rows
    streak = 0
    for r in runs:
        if (r.get("status") or "").lower() == "running":
            continue
        if is_failure(r):
            streak += 1
        else:
            break

    last_run_age_s = (now_ms - runs[0]["started_at"]) / 1000.0

    ok_runs = [r for r in runs if is_success(r) is True]
    last_ok_age_s = (now_ms - ok_runs[0]["started_at"]) / 1000.0 if ok_runs else None

    # learned cadence: median gap between successful runs, RE-DERIVED every run from the most
    # recent RECENT_GAPS gaps only (a rolling window) — NOT a median over every gap in the 7d
    # window, and NOT a one-time learned/cached value. This is the fix for the schedule-
    # memorization false alarm: when a task's cron changes (e.g. hourly -> every 6h), a
    # whole-window median stays anchored to the OLD cadence for up to WINDOW_DAYS, so the task
    # trips STALE deterministically on every sweep even though it is running exactly on its new
    # schedule. Using only the trailing gaps means the derived interval tracks the CURRENT cadence
    # within a few runs of the change and the false alarm self-clears. (The configured-yaml
    # interval, when parseable, still takes precedence below — this governs the fallback path,
    # which is exactly where the stale false positives were coming from.)
    interval_s = None
    if len(ok_runs) >= 2:
        ts = sorted(r["started_at"] for r in ok_runs)
        gaps = [(ts[i + 1] - ts[i]) / 1000.0 for i in range(len(ts) - 1)]
        gaps = [g for g in gaps if g > 0]
        if gaps:
            interval_s = statistics.median(gaps[-RECENT_GAPS:])

    # The CONFIGURED schedule interval (from the scheduler yaml) is the truth for staleness; the
    # learned median-gap is only a fallback. A weekly cron ran a few times clustered on its day
    # and the learned "cadence" came out ~2h, so a healthy Wednesday task looked 50h "stale" by
    # Friday. Prefer the real configured interval when we have it. (Computed before the decision
    # chain so the stale test stays a proper elif — a standalone if/else here would clobber the
    # failing/critical verdict with 'healthy'.)
    eff_interval = expected_interval_s if expected_interval_s else interval_s

    # --- decide state ---
    attempts = sum(1 for r in runs if is_failure(r) or is_success(r) is True)
    successes = len(ok_runs)

    if streak >= CRIT_STREAK or (successes == 0 and attempts >= 5):
        state, reason = "critical", (
            f"{streak} consecutive failures" if streak >= CRIT_STREAK
            else f"0 successes in {attempts} attempts over {WINDOW_DAYS}d")
    elif streak >= FAIL_STREAK or (successes == 0 and attempts >= 2):
        state, reason = "failing", (
            f"{streak} consecutive failures" if streak >= FAIL_STREAK
            else f"0 successes in {attempts} attempts")
    elif eff_interval is not None and last_run_age_s > max(STALE_FACTOR * eff_interval, STALE_FLOOR_S):
        hrs = last_run_age_s / 3600.0
        exp = eff_interval / 3600.0
        cadence = f"; expected roughly every {exp:.1f}h" if eff_interval >= 300 else ""
        state, reason = "stale", f"last run {hrs:.1f}h ago{cadence}"
    else:
        state, reason = "healthy", "ok"

    return {
        "state": state,
        "consecutive_failures": streak,
        "last_run_age_s": round(last_run_age_s, 1),
        "last_ok_age_s": round(last_ok_age_s, 1) if last_ok_age_s is not None else None,
        "interval_s": round(interval_s, 1) if interval_s is not None else None,
        "reason": reason,
    }


# ── IO layer ─────────────────────────────────────────────────────────────────
def fetch_task_runs(conn, window_days: int = WINDOW_DAYS) -> dict:
    """Return {task_id: [run dicts newest-first]} for all tasks seen in the window."""
    cutoff = f"(extract(epoch from now())-{window_days * 86400})*1000"
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT task_id, started_at, status, exit_code
                  FROM scheduler_runs
                 WHERE started_at > {cutoff}
                 ORDER BY task_id, started_at DESC""")
        for task_id, started_at, status, exit_code in cur.fetchall():
            out.setdefault(task_id, []).append(
                {"started_at": int(started_at) if started_at is not None else None,
                 "status": status, "exit_code": exit_code})
    return out


def _page(conn, task_id: str, health: dict):
    """Emit a warning/critical notification (deduped via nova_notifier) and, for
    critical, a deduped claude_queue row so it reaches the next session's context."""
    try:
        from nova_notify import notify
    except Exception:
        def notify(*a, **k):  # degrade gracefully if the helper moved
            return False

    state = health["state"]
    level = "critical" if state == "critical" else "warning"
    title = f"Scheduled task '{task_id}' is {state.upper()}"
    body = (f"{health['reason']}. "
            f"last run {health.get('last_run_age_s', '?')}s ago, "
            f"last success {health.get('last_ok_age_s')}s ago.")
    # Re-notify window 6h (not the default 1h): a task that's been failing all day doesn't need
    # 23 hourly pings — the first tells you, ~4/day is a reminder, more is just noise burying the
    # NEW failures. (2026-08-11 S/N pass: task-sentinel sent 77 alerts/night for 9 conditions.)
    notify(title, body=body, level=level, category="task-sentinel",
           source="nova_task_sentinel", dedup_key=f"task-sentinel:{task_id}",
           meta={"dedup_window_s": 21600})

    if state == "critical":
        # SAVEPOINT so a failed insert (e.g. FK/constraint) rolls back only THIS write
        # instead of aborting the whole sweep's transaction and poisoning the heartbeat.
        desc = f"TASK FAILING: {task_id} — {health['reason']}"
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_queue")
            try:
                # priority is an integer column (2 = high; BB uses 3, advisor 4). A hard,
                # cross-host task failure the log-watcher missed is genuinely high-urgency.
                cur.execute(
                    """INSERT INTO claude_queue (session_id, status, priority, description, context)
                       SELECT 'task-sentinel', 'queued', 2, %s, %s
                       WHERE NOT EXISTS (
                         SELECT 1 FROM claude_queue
                          WHERE description = %s AND status IN ('queued','in_progress'))""",
                    (desc, f"nova_task_sentinel: {title}. {body}", desc))
                cur.execute("RELEASE SAVEPOINT sp_queue")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_queue")


def load_configured_tasks() -> dict:
    """Return {task_id: schedule_str} for every ENABLED task across BOTH schedulers (.6 local
    + .2 scheduler-core via ssh). A task that isn't in either config is RETIRED — we must not
    alert on it forever just because scheduler_runs still holds its old rows (the daily_digest /
    output_watchdog phantom-stale spam). Returns {} on total failure -> falls back to judging
    everything (fail-safe: better a stray alert than silence)."""
    import os
    import subprocess
    import yaml
    out = {}
    # .6 — read locally
    try:
        d = yaml.safe_load(open(os.path.expanduser("~/.openclaw/config/scheduler.yaml")))
        for k, v in (d.get("tasks") or {}).items():
            if (v or {}).get("enabled", True):
                out[k] = (v or {}).get("schedule", "")
    except Exception:
        pass
    # .2 — read over ssh (best-effort)
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                            "kochj@192.168.1.2", "cat ~/.openclaw/config/scheduler-core.yaml"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout:
            d = yaml.safe_load(r.stdout)
            for k, v in (d.get("tasks") or {}).items():
                if (v or {}).get("enabled", True):
                    out.setdefault(k, (v or {}).get("schedule", ""))
    except Exception:
        pass
    return out


def schedule_interval_s(sched: str) -> Optional[float]:
    """Expected seconds between fires, parsed from a schedule string. Handles 'every Nm/Nh/Nd',
    'daily', and cron (approximated by fires-per-week). None if unknown."""
    import re as _re
    s = (sched or "").strip().lower()
    if not s:
        return None
    m = _re.match(r"every\s+(\d+)\s*([smhd])", s)
    if m:
        n = int(m.group(1)); unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return n * unit
    if s.startswith("daily"):
        return 86400.0
    if s.startswith("cron"):
        parts = s.split()
        if len(parts) >= 6:
            minute, hour, dom, mon, dow = parts[1:6]
            # count fires per week by expanding minute+hour over the day(s)-of-week it runs
            def _n(field, span):
                if field == "*":
                    return span
                if field.startswith("*/"):
                    step = int(field[2:]); return max(1, span // step)
                return len([p for p in field.replace("-", ",").split(",") if p])
            days = 7 if dow == "*" else _n(dow, 7)
            hours = _n(hour, 24)
            mins = 1 if minute != "*" and "/" not in minute else _n(minute, 60)
            fires_per_week = max(1, days * hours * mins)
            return 604800.0 / fires_per_week
    return None


def ensure_session(conn):
    """Register the 'task-sentinel' session row so claude_queue's FK is satisfiable.
    Mirrors nova_core_liveness.ensure_session — idempotent."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO claude_sessions (session_id, status) VALUES ('task-sentinel','active') "
                "ON CONFLICT (session_id) DO NOTHING")
        conn.commit()
    except Exception:
        conn.rollback()


def _heartbeat(conn, ok_count: int, bad: list):
    """Dogfood: the sentinel writes its own health_checks row so it, too, is watched."""
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp_hb")
        try:
            cur.execute(
                """INSERT INTO health_checks (service_name, node_name, checked_by, status, error_message)
                   VALUES ('task_sentinel','mac-studio','nova_task_sentinel',%s,%s)""",
                ("up", f"{ok_count} healthy, {len(bad)} degraded: "
                       + ",".join(b[0] for b in bad[:20])))
            cur.execute("RELEASE SAVEPOINT sp_hb")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_hb")


def run_once(conn) -> list:
    """Sweep every task, page the degraded ones, heartbeat. Returns the degraded list."""
    now = _now_ms()
    ensure_session(conn)
    runs_by_task = fetch_task_runs(conn)
    configured = load_configured_tasks()   # {task_id: schedule_str} across both schedulers
    degraded, healthy, retired = [], 0, 0
    for task_id, runs in runs_by_task.items():
        # RETIRED: a task with run-history but no longer in ANY scheduler config was removed;
        # never alert on it. (Only skip when we actually loaded a config — an empty dict means
        # config-load failed, and we fall back to judging everything.)
        if configured and task_id not in configured:
            retired += 1
            continue
        exp_interval = schedule_interval_s(configured.get(task_id, "")) if configured else None
        h = classify_task(runs, now_ms=now, expected_interval_s=exp_interval)
        if h["state"] in ("failing", "critical", "stale"):
            degraded.append((task_id, h))
            _page(conn, task_id, h)
        elif h["state"] == "healthy":
            healthy += 1
    # worst first for the log
    order = {"critical": 0, "failing": 1, "stale": 2}
    degraded.sort(key=lambda d: order.get(d[1]["state"], 9))
    _heartbeat(conn, healthy, degraded)
    conn.commit()
    return degraded


def main() -> int:
    import psycopg2
    conn = None
    try:
        conn = psycopg2.connect(DSN)
        degraded = run_once(conn)
    except Exception as e:
        print(f"task_sentinel: sweep failed: {e}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
    if not degraded:
        print("task_sentinel: all tasks healthy")
        return 0
    print(f"task_sentinel: {len(degraded)} degraded task(s):")
    for task_id, h in degraded:
        print(f"  [{h['state']:8}] {task_id:32} {h['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
