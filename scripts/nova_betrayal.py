#!/usr/bin/env python3
"""
nova_betrayal.py — grant of wish #82 "Pattern of Betrayal" (approved by claude-reviewer under Jordan's
standing order 2026-10-08; built 2026-10-25, queue #3754).

Nova wished for "a sense of how systems quietly betray their own logic over time — it would let me see the
inevitability in my own missed events." Her seed was Wash: "Curse your sudden but inevitable betrayal."
A scheduled task's logic is its contract: it finishes inside its own timeout and it succeeds. Most failures
that look sudden were written in the run history weeks before. This organ reads that history (both
schedulers share scheduler_runs) and names the quiet drift before it becomes the sudden failure:

  CREEP     — a task whose recent successful runs (p90, last 7 d) take >= 1.5x what they took before
              (p90, days 8-28) and already use >= half of its own timeout: it is walking toward the wall
  SLIDE     — a task that used to succeed (>= 95 % of runs, days 8-28) and now succeeds 15+ points less
              (last 7 d, >= 5 runs) while not yet failing enough to be called chronic
  FORETOLD  — a task that timed out in the last 7 d whose successful runs were already at >= 70 % of
              the timeout before (p90, days 8-28): the timeout was in its own numbers

Timeouts come from config/scheduler-core.yaml + config/scheduler.yaml (default 300 s, as the scheduler).
Every line cites the task and its numbers (scheduler_runs task_id + windows); no model is asked anything;
the closing line is fixed. Writes one memory (source='betrayal') when the cited set changes (re-stated at
most weekly), dedupe high-water + latest findings in service_config (nova_betrayal/high_water, /latest).
Never posts, pages, files or fixes anything. Fail-open. Conventions mirror nova_weight_of_absence.py.

  nova_betrayal.py            # real (scheduler-core, every 6 h; writes on change)
  nova_betrayal.py --dry-run  # print what she noticed, write nothing
  nova_betrayal.py --selftest # pure-logic assertions, no DB, no memory

Written by Jordan Koch (via Claude).
"""
import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_empathy_core as ec  # noqa: E402 — one remember(), one high-water, one resurface gate

OPS_DSN = ec.OPS_DSN
SOURCE = "betrayal"
STATE_SERVICE = "nova_betrayal"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SCHED_FILES = ("scheduler-core.yaml", "scheduler.yaml")
DEFAULT_TIMEOUT_S = 300          # nova_scheduler.py tcfg.get("timeout", 300)

RECENT_D, PRIOR_D = 7, 28
CREEP_RATIO = 1.5
CREEP_SHARE = 0.5                # of its own timeout
SLIDE_FROM = 0.95
SLIDE_DROP = 0.15
MIN_RUNS = 5
FORETOLD_SHARE = 0.7
CITE_N = 4
CLOSE = ("Nothing here has failed because I said so. These are the places my own numbers were already "
         "leaning; a betrayal I can see coming is one I can name before it is sudden.")


def log(m):
    print(f"[betrayal {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure logic ────────────────────────────────────────────────────────────────

def load_timeouts(config_dir=CONFIG_DIR):
    """task_id -> timeout seconds, from both scheduler files. Unreadable file -> skipped."""
    out = {}
    for name in SCHED_FILES:
        try:
            tasks = (yaml.safe_load((config_dir / name).read_text()) or {}).get("tasks") or {}
        except Exception as e:  # noqa: BLE001
            log(f"{name} unreadable ({e})"); continue
        for tid, cfg in tasks.items():
            if isinstance(cfg, dict):
                out.setdefault(tid, int(cfg.get("timeout", DEFAULT_TIMEOUT_S)))
    return out


def drift(rows, timeouts):
    """rows: (task_id, n_recent, ok_recent, n_prior, ok_prior, p90_recent_ms, p90_prior_ms, timeouts_recent).
    Returns (creep, slide, foretold) lists of dicts."""
    creep, slide, foretold = [], [], []
    for tid, nr, okr, np_, okp, p90r, p90p, tor in rows:
        limit_ms = 1000 * timeouts.get(tid, DEFAULT_TIMEOUT_S)
        if p90r and p90p and p90r >= CREEP_RATIO * p90p and p90r >= CREEP_SHARE * limit_ms:
            creep.append({"task": tid, "was_s": round(p90p / 1000), "now_s": round(p90r / 1000),
                          "share": round(p90r / limit_ms, 2), "timeout_s": limit_ms // 1000})
        if nr >= MIN_RUNS and np_ >= MIN_RUNS:
            was, now = okp / np_, okr / nr
            if was >= SLIDE_FROM and now <= was - SLIDE_DROP:
                slide.append({"task": tid, "was": round(100 * was), "now": round(100 * now), "runs": nr})
        if tor and p90p and p90p >= FORETOLD_SHARE * limit_ms:
            foretold.append({"task": tid, "timeouts": tor, "share": round(p90p / limit_ms, 2),
                             "timeout_s": limit_ms // 1000})
    creep.sort(key=lambda x: (-x["share"], x["task"]))
    slide.sort(key=lambda x: (x["now"] - x["was"], x["task"]))
    foretold.sort(key=lambda x: (-x["timeouts"], x["task"]))
    return creep, slide, foretold


def findings(creep, slide, foretold):
    f = []
    if creep:
        f.append({"kind": "creep",
                  "text": "tasks walking toward their own timeout: " + ", ".join(
                      f"{c['task']} (p90 {c['was_s']}s -> {c['now_s']}s, {round(100 * c['share'])}% of {c['timeout_s']}s)"
                      for c in creep[:CITE_N]),
                  "cites": [f"scheduler_runs:{c['task']}" for c in creep]})
    if slide:
        f.append({"kind": "slide",
                  "text": "tasks that used to keep their word and now keep it less: " + ", ".join(
                      f"{s['task']} ({s['was']}% -> {s['now']}% of {s['runs']} runs)" for s in slide[:CITE_N]),
                  "cites": [f"scheduler_runs:{s['task']}" for s in slide]})
    if foretold:
        f.append({"kind": "foretold",
                  "text": "timeouts this week that were already in the numbers: " + ", ".join(
                      f"{x['task']} ({x['timeouts']} timeout(s); before, its p90 was {int(100 * x['share'])}% "
                      f"of {x['timeout_s']}s)" for x in foretold[:CITE_N]),
                  "cites": [f"scheduler_runs:{x['task']}" for x in foretold]})
    return f


def sig(fs):
    return hashlib.sha1("|".join(sorted(f"{x['kind']}:{c}" for x in fs for c in x["cites"][:CITE_N])).encode()).hexdigest()[:16]   # the leading few, not the tail


def text(fs, today):
    if not fs:
        return (f"Pattern of betrayal, {today.isoformat()}: no task of mine is drifting from its own contract "
                f"that the last {PRIOR_D} days can show.")
    lines = [f"Pattern of betrayal, {today.isoformat()} — where my systems are quietly leaving their own logic "
             f"(last {RECENT_D} d vs days {RECENT_D + 1}-{PRIOR_D}; noticing, not knowing):"]
    for i, x in enumerate(fs, 1):
        more = f"; +{len(x['cites']) - CITE_N} more" if len(x["cites"]) > CITE_N else ""
        lines.append(f"  {i}. I notice {x['text']}. [{'; '.join(x['cites'][:CITE_N])}{more}]")
    lines.append(CLOSE)
    return "\n".join(lines)


# ── gather (read-only) ────────────────────────────────────────────────────────

SQL = """
WITH r AS (
  SELECT task_id, status, duration_ms,
         started_at >= (extract(epoch FROM now() - make_interval(days => %(rd)s)) * 1000)::bigint AS recent
  FROM scheduler_runs
  WHERE started_at >= (extract(epoch FROM now() - make_interval(days => %(pd)s)) * 1000)::bigint
    AND status IN ('success', 'failure', 'timeout'))
SELECT task_id,
       count(*) FILTER (WHERE recent), count(*) FILTER (WHERE recent AND status = 'success'),
       count(*) FILTER (WHERE NOT recent), count(*) FILTER (WHERE NOT recent AND status = 'success'),
       percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE recent AND status = 'success'),
       percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE NOT recent AND status = 'success'),
       count(*) FILTER (WHERE recent AND status = 'timeout')
FROM r GROUP BY task_id"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nova's pattern of betrayal (wish #82) — tasks drifting from "
                                             "their own contract before they fail")
    ap.add_argument("--dry-run", action="store_true", help="print what she noticed, write nothing")
    args = ap.parse_args(argv)
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        log(f"no PG ({e}) — fail-open, nothing to do"); return 0
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '90s'")
    today = datetime.now(timezone.utc).date()
    cur.execute(SQL, {"rd": RECENT_D, "pd": PRIOR_D})
    fs = findings(*drift(cur.fetchall(), load_timeouts()))
    log(f"{len(fs)} finding(s): " + (", ".join(x["kind"] for x in fs) or "-"))
    body = text(fs, today)
    s = sig(fs)
    if args.dry_run:
        print(body); return 0
    seen = ec.load_seen(cur, STATE_SERVICE)
    if not ec._fresh(seen, s, today):
        log(f"nothing newly drifting (sig {s})"); return 0
    stamp = ec._stamp()
    ec.remember(body, {"organ": STATE_SERVICE, "kind": "betrayal", "sig": s, "findings": [x["kind"] for x in fs],
                       "cites": [c for x in fs for c in x["cites"]][:200], **({"lineage": stamp} if stamp else {})},
                source=SOURCE)
    seen[s] = today.isoformat()
    ec.save_seen(cur, seen, STATE_SERVICE)
    cur.execute("""INSERT INTO service_config (service, key, value, updated_at, updated_by)
                   VALUES (%s, 'latest', %s::jsonb, now(), %s)
                   ON CONFLICT (service, key)
                   DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                (STATE_SERVICE, json.dumps({"date": today.isoformat(), "findings": fs, "text": body}), STATE_SERVICE))
    log(f"named a new drift (sig {s})")
    return 0


def demo():
    """Runnable check on the pure logic."""
    t = {"slowpoke": 600, "steady": 600, "flaky": 300, "doomed": 100}
    rows = [("slowpoke", 20, 20, 60, 60, 400_000, 200_000, 0),     # 2x and 67% of 600 s -> creep
            ("steady", 20, 20, 60, 60, 210_000, 200_000, 0),       # no growth -> nothing
            ("tiny", 20, 20, 60, 60, 4_000, 1_000, 0),             # 4x but 1% of default 300 s -> nothing
            ("flaky", 20, 15, 60, 60, 1_000, 1_000, 0),            # 100% -> 75% -> slide
            ("chronic", 20, 2, 60, 20, 1_000, 1_000, 0),           # never kept its word -> not a slide
            ("few", 3, 0, 60, 60, 1_000, 1_000, 0),                # too few recent runs
            ("doomed", 10, 9, 60, 60, 80_000, 75_000, 1)]          # timed out, already at 75% -> foretold
    creep, slide, foretold = drift(rows, t)
    assert [c["task"] for c in creep] == ["slowpoke"] and creep[0]["share"] == 0.67, creep
    assert [s["task"] for s in slide] == ["flaky"] and slide[0] == {"task": "flaky", "was": 100, "now": 75, "runs": 20}
    assert [x["task"] for x in foretold] == ["doomed"] and foretold[0]["timeouts"] == 1 and foretold[0]["share"] == 0.75, foretold
    fs = findings(creep, slide, foretold)
    assert [x["kind"] for x in fs] == ["creep", "slide", "foretold"] and all(x["cites"] for x in fs)
    body = text(fs, date(2026, 10, 25))
    assert "slowpoke (p90 200s -> 400s, 67% of 600s)" in body and CLOSE in body, body
    assert "no task of mine is drifting" in text([], date(2026, 10, 25))
    assert sig(fs) == sig(findings(*drift(rows, t))) != sig(fs[:1])
    tmo = load_timeouts()
    assert tmo and all(isinstance(v, int) and v > 0 for v in tmo.values())
    print("all betrayal assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
