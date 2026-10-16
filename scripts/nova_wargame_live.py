#!/usr/bin/env python3
"""nova_wargame_live.py — the hands of nova_wargame's chaos experiments. Runs ONE approved experiment, safely.

Little Mister, 2026-10-15 ("Do it all"; chaos engineering for Nova). Live chaos is a red line: Nova proposes
(nova_wargame.py --propose), a Claude session or Jordan approves (--approve --by claude:<session>|jordan), and a
human or Claude starts it here. Nothing schedules a live run: --live is refused under --scheduled.

Safety copies the Haven Drill:
  * every gate in nova_wargame.gate_check() is re-read live immediately before the fault goes in;
  * a PG advisory lock means one experiment at a time;
  * rollback runs in `finally`, from atexit, on SIGTERM/SIGINT/SIGHUP, and from --watchdog (every 5 min on the
    Studio), which rolls back any experiment still 'running' past its deadline + 5 minutes;
  * the abort checker re-reads the gates every 10 s while the fault is in (kill switch, an SLO paging, a new
    critical incident, a new Comet Tail detection, the deadline) and stops early on any of them;
  * a failed rollback is a critical notification and the row is marked rollback_failed.
Afterwards it files a hotwash (kind 'experiment') and records the verdict.

Method/rollback steps are data in wargame_experiments: {"type": "ssh", "host", "cmd"} or
{"type": "psql_standby", "host", "sql"} (never the primary). "manual" steps are refused here.

Usage: nova_wargame_live.py --live --confirm ID [--observe-minutes N] | --watchdog | --selftest
Written by Jordan Koch (via Claude).
"""
import argparse
import atexit
import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_dsn  # noqa: E402
import nova_wargame as W  # noqa: E402

LOCK_KEY = 0x7761726761   # "warga"
CHECK_EVERY_S = 10
_ROLLED = {"done": False}


def run_step(step: dict, timeout=120) -> tuple[bool, str]:
    """Run one method/rollback step. Returns (ok, output)."""
    t = step.get("type")
    if t == "ssh":
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", step["host"], step["cmd"]]
    elif t == "psql_standby":
        if "primary" in step["host"] or step["host"].startswith("nova-core."):
            return False, "refused: psql steps may only target a standby"
        cmd = ["psql", "-h", step["host"], "-U", "kochj", "-d", "postgres", "-tAc", step["sql"]]
    else:
        return False, f"refused: step type {t!r} is not runnable here"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-500:]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def rollback(exp: dict, cur=None) -> bool:
    if _ROLLED["done"]:
        return True
    ok = True
    for step in exp.get("rollback") or []:
        good, out = run_step(step)
        ok = ok and good
        print(f"[wargame-live] rollback {step.get('type')} -> {'ok' if good else 'FAILED'}: {out[:160]}", flush=True)
    _ROLLED["done"] = True
    if not ok:
        try:
            from nova_notify import notify
            notify(f"Chaos experiment {exp['id']} rollback FAILED", body=json.dumps(exp.get("rollback")),
                   level="critical", category="wargame", dedup_key=f"wargame:rollback:{exp['id']}")
        except Exception:  # noqa: BLE001
            pass
        if cur is not None:
            cur.execute("UPDATE wargame_experiments SET state='rollback_failed', ended_at=now() WHERE id=%s", (exp["id"],))
    return ok


def abort_reason(cur, exp, started_ctx, deadline) -> str | None:
    """Re-read the live gates; return why to stop now, or None."""
    if datetime.now(timezone.utc) >= deadline:
        return "deadline"
    ctx = W.load_ctx(cur, exp["id"])
    if ctx["kill"]:
        return "kill switch engaged"
    if ctx["slo_pages"]:
        return "an SLO started paging"
    if ctx["open_critical"] and not started_ctx["open_critical"]:
        return "a critical incident opened"
    try:
        cur.execute("SELECT count(*) FROM comet_tail WHERE ts > %s", (started_ctx["t0"],))
        if (cur.fetchone() or [0])[0]:
            return "Comet Tail detected a shared-cause failure"
    except Exception:  # noqa: BLE001
        cur.connection.rollback()
    return None


def run_live(exp_id: str, observe_minutes: float | None = None) -> int:
    conn = nova_dsn.pg_connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
    if not cur.fetchone()[0]:
        print("refused: another experiment holds the lock"); return 2
    exp = W._row(cur, exp_id)
    if not exp:
        print("no such experiment"); return 2
    why = W.gate_check(exp, W.load_ctx(cur, exp_id))
    if why:
        print("refused:\n  " + "\n  ".join(why)); return 2
    dur = exp["max_duration"]
    cur.execute("SELECT %s::interval", (dur,))
    max_dur = cur.fetchone()[0]
    observe = timedelta(minutes=observe_minutes) if observe_minutes else max_dur
    deadline = datetime.now(timezone.utc) + min(observe, max_dur)
    t0 = datetime.now(timezone.utc)
    started_ctx = dict(W.load_ctx(cur, exp_id), t0=t0)

    atexit.register(rollback, exp, None)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: (rollback(exp, cur), sys.exit(3)))

    cur.execute("UPDATE wargame_experiments SET state='running', started_at=now(), ended_at=NULL WHERE id=%s", (exp_id,))
    verdict = {"aborted": None, "method": [], "observed_s": 0}
    state = "passed"
    try:
        for step in exp.get("method") or []:
            ok, out = run_step(step)
            verdict["method"].append({"ok": ok, "out": out[:200]})
            if not ok:
                state, verdict["aborted"] = "aborted", "method step failed"
                break
        while state == "passed":
            reason = abort_reason(cur, exp, started_ctx, deadline)
            if reason == "deadline":
                break
            if reason:
                state, verdict["aborted"] = "aborted", reason
                break
            time.sleep(CHECK_EVERY_S)
        verdict["observed_s"] = int((datetime.now(timezone.utc) - t0).total_seconds())
    finally:
        rolled = rollback(exp, cur)
    if not rolled:
        state = "rollback_failed"
    elif state == "passed":
        state = "rolled_back" if verdict["aborted"] else "passed"
    verdict["note"] = "hypothesis must be judged from the steady-state signals; 'passed' means the run completed and rolled back"
    cur.execute("UPDATE wargame_experiments SET state=%s, ended_at=now(), verdict=%s WHERE id=%s",
                (state, json.dumps(verdict), exp_id))
    try:
        import nova_hotwash
        hid = nova_hotwash.file_experiment(cur, dict(exp, state=state, verdict=verdict))
        if hid:
            cur.execute("UPDATE wargame_experiments SET hotwash_id=%s WHERE id=%s", (hid, exp_id))
    except Exception as e:  # noqa: BLE001
        print(f"[wargame-live] hotwash not filed: {e}")
    cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    print(f"[wargame-live] {exp_id}: {state} ({verdict['aborted'] or 'ran to deadline'})")
    return 0 if state in ("passed", "rolled_back") else 1


def watchdog() -> int:
    """Roll back any experiment still 'running' 5 minutes past its max duration."""
    conn = nova_dsn.pg_connect()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM wargame_experiments WHERE state='running' "
                    "AND started_at + max_duration + interval '5 minutes' < now()")
    except Exception:  # noqa: BLE001
        return 0
    for (exp_id,) in cur.fetchall():
        exp = W._row(cur, exp_id)
        _ROLLED["done"] = False
        ok = rollback(exp, cur)
        cur.execute("UPDATE wargame_experiments SET state=%s, ended_at=now() WHERE id=%s",
                    ("rolled_back" if ok else "rollback_failed", exp_id))
        print(f"[wargame-live] watchdog rolled back {exp_id}: {'ok' if ok else 'FAILED'}")
    return 0


def selftest() -> int:
    assert run_step({"type": "manual"})[0] is False
    assert run_step({"type": "psql_standby", "host": "pg-primary.x", "sql": "select 1"})[0] is False
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--confirm", metavar="ID")
    ap.add_argument("--observe-minutes", type=float)
    ap.add_argument("--watchdog", action="store_true")
    ap.add_argument("--scheduled", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.watchdog:
        return watchdog()
    if a.live:
        if a.scheduled:
            print("refused: a live experiment is never started by the scheduler"); return 2
        if not a.confirm:
            print("refused: --live needs --confirm ID"); return 2
        return run_live(a.confirm, a.observe_minutes)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
