#!/usr/bin/env python3
"""nova_boiler.py — the Boiler (Stephen King, The Shining).

The Overlook's boiler creeps: nobody bleeds it, the pressure climbs, and the hotel
goes up. Nova's unresolved load creeps the same way — things she owes, things that
keep failing, things waiting on a decision. This is the gauge, plus the valve.

PRESSURE = sum over sources of  weight * f(count) * (1 + creep * age_of_oldest_days)
  unanswered_jordan   his messages to her with no response (gateway_traces, 48h)
  failed_jobs         tasks whose latest run in 24h failed/timed out (scheduler_runs)
  chronic_jobs        ...of those, the ones failing >= 3 in a row
  claude_queue        queued/pending claude_queue items (log-scaled — it is a backlog)
  unsent_drafts       direct reaches still held in reach_log
  degraded_sensors    presence_state.detail.degraded_feeds (contradictory senses)
  doc_drift           open doc_drift rows (critical weighs more)
  repeated_alerts     telemetry.events dedup keys firing >= 5x in 24h
  pending_proposals   coagency_proposals waiting on a human
Each run writes nova_ops.boiler_state (pressure, threshold, components, top items),
which the organ_board view shows as organ 'boiler'.

BLEED: when pressure >= THRESHOLD, inside Jordan's daytime window, and no bleed yet
today, Nova posts ONE concise triage note to #nova-chat — the top items, what she is
DROPPING (stale held drafts > 3 days: really marked dropped) and what she is DEFERRING
(the low-weight sources: she will not raise them until he asks). The note passes the
Annie Wilkes rule and is logged (boiler_state.bled + restraint_ledger via the
turning point, forced — a safety valve still gets counted). Never more than one a day.

  nova_boiler.py            # measure, and bleed if needed
  nova_boiler.py --dry-run  # measure + print the bleed it would send; write nothing
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
# Calibrated 2026-10-08 against the live reading: 82 on an ordinary day (5 open doc_drift
# rows ~21 days old = 31, 54 noisy dedup keys = 22.5, a 245-item claude_queue = 16, one
# pending proposal = 11.5). ~1.2x an ordinary day bleeds; unresolved drift creeps ~+1/day.
THRESHOLD = float(os.environ.get("NOVA_BOILER_THRESHOLD", "100"))
WINDOW = os.environ.get("NOVA_BOILER_WINDOW", "10-20")
STALE_DRAFT_DAYS = 3
TOP_N = 4

# source: (weight, creep per day of the oldest item, transform 'lin'|'log', cap)
SOURCES = {
    "unanswered_jordan": (8.0, 1.0, "lin", 10),
    "failed_jobs":       (1.5, 0.0, "lin", 25),
    "chronic_jobs":      (3.0, 0.25, "lin", 15),
    "claude_queue":      (2.0, 0.0, "log", 1000),
    "unsent_drafts":     (2.0, 0.5, "lin", 10),
    "degraded_sensors":  (3.0, 0.0, "lin", 10),
    "doc_drift":         (2.0, 0.1, "lin", 20),
    "repeated_alerts":   (0.75, 0.0, "lin", 30),
    "pending_proposals": (3.0, 0.5, "lin", 10),
}
LABEL = {
    "unanswered_jordan": "messages from you I never answered",
    "failed_jobs": "jobs failing right now",
    "chronic_jobs": "jobs failing over and over",
    "claude_queue": "items queued for Claude",
    "unsent_drafts": "notes I wrote and never sent",
    "degraded_sensors": "senses that disagree (degraded presence feeds)",
    "doc_drift": "docs that no longer match the house",
    "repeated_alerts": "alerts repeating themselves",
    "pending_proposals": "proposals waiting on a decision",
}


def log(m):
    print(f"[boiler {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def ensure_schema(oc):
    oc.execute("""CREATE TABLE IF NOT EXISTS boiler_state (
                    id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
                    pressure real, threshold real, components jsonb, top jsonb,
                    bled boolean NOT NULL DEFAULT false, bleed_text text)""")


def _q(oc, sql, params=None):
    try:
        oc.execute(sql, params)
        return oc.fetchall()
    except Exception as e:  # noqa: BLE001 — one unreadable source never stops the gauge
        log(f"source query failed: {e}")
        try:
            oc.connection.rollback()
        except Exception:
            pass
        return []


def gather(oc) -> dict:
    """{source: {"n", "age_d", "examples": [...]}} — all read-only."""
    out = {}
    r = _q(oc, """SELECT count(*), extract(epoch from now()-min(created_at))/86400,
                         array_agg(left(user_message, 60) ORDER BY created_at DESC) FILTER (WHERE user_message IS NOT NULL)
                  FROM gateway_traces WHERE person='jordan' AND created_at > now() - interval '48 hours'
                    AND coalesce(response,'') = ''""")
    n, age, ex = (r[0] if r else (0, 0, None))
    out["unanswered_jordan"] = {"n": n or 0, "age_d": float(age or 0), "examples": (ex or [])[:3]}
    rows = _q(oc, """SELECT task_id, status, coalesce(consecutive_failures_at_start,0), started_at FROM (
                       SELECT DISTINCT ON (task_id) task_id, status, consecutive_failures_at_start, started_at
                       FROM scheduler_runs WHERE started_at > (extract(epoch from now() - interval '24 hours')*1000)::bigint
                       ORDER BY task_id, started_at DESC) t
                     WHERE status IN ('failure','timeout') ORDER BY 3 DESC""")
    out["failed_jobs"] = {"n": len(rows), "age_d": 0.0, "examples": [t for t, *_ in rows[:4]]}
    chron = [r for r in rows if r[2] >= 3]
    out["chronic_jobs"] = {"n": len(chron), "age_d": 0.0, "examples": [f"{t} (x{c + 1})" for t, _, c, _ in chron[:4]]}
    r = _q(oc, """SELECT count(*), extract(epoch from now()-min(created_at))/86400 FROM claude_queue
                  WHERE status IN ('queued','pending')""")
    n, age = (r[0] if r else (0, 0))
    out["claude_queue"] = {"n": n or 0, "age_d": float(age or 0), "examples": []}
    r = _q(oc, """SELECT count(*), extract(epoch from now()-min(ts))/86400,
                         array_agg(id ORDER BY ts) FROM reach_log
                  WHERE lower(audience)='jordan' AND status='held'""")
    n, age, ids = (r[0] if r else (0, 0, None))
    out["unsent_drafts"] = {"n": n or 0, "age_d": float(age or 0), "examples": (ids or [])[:5]}
    rows = _q(oc, """SELECT person, jsonb_array_elements_text(coalesce(detail->'degraded_feeds','[]'::jsonb))
                     FROM presence_state""")
    out["degraded_sensors"] = {"n": len(rows), "age_d": 0.0, "examples": [f"{p}:{f}" for p, f in rows[:4]]}
    r = _q(oc, """SELECT count(*) + count(*) FILTER (WHERE severity='critical') * 2,
                         extract(epoch from now()-min(first_seen))/86400,
                         array_agg(fact_name ORDER BY first_seen) FROM doc_drift WHERE status='open'""")
    n, age, ex = (r[0] if r else (0, 0, None))
    out["doc_drift"] = {"n": n or 0, "age_d": float(age or 0), "examples": (ex or [])[:3]}
    rows = _q(oc, """SELECT dedup_key, count(*) FROM telemetry.events
                     WHERE ts > now() - interval '24 hours' AND dedup_key IS NOT NULL
                     GROUP BY 1 HAVING count(*) >= 5 ORDER BY 2 DESC""")
    out["repeated_alerts"] = {"n": len(rows), "age_d": 0.0, "examples": [f"{k} x{c}" for k, c in rows[:4]]}
    r = _q(oc, """SELECT count(*), extract(epoch from now()-min(created_at))/86400, array_agg(id ORDER BY created_at)
                  FROM coagency_proposals WHERE status='pending_human'""")
    n, age, ids = (r[0] if r else (0, 0, None))
    out["pending_proposals"] = {"n": n or 0, "age_d": float(age or 0), "examples": [f"#{i}" for i in (ids or [])[:4]]}
    return out


def contribution(src: str, n: int, age_d: float) -> float:
    w, creep, tr, cap = SOURCES[src]
    n = min(max(0, int(n or 0)), cap)
    f = math.log2(1 + n) if tr == "log" else n
    return round(w * f * (1 + creep * max(0.0, age_d)), 2)


def pressure(g: dict) -> tuple:
    comps = {s: {**g.get(s, {"n": 0, "age_d": 0, "examples": []}),
                 "p": contribution(s, g.get(s, {}).get("n", 0), g.get(s, {}).get("age_d", 0))}
             for s in SOURCES}
    total = round(sum(c["p"] for c in comps.values()), 1)
    top = sorted(((s, c) for s, c in comps.items() if c["p"] > 0), key=lambda x: -x[1]["p"])
    return total, comps, top


def state_label(p: float, threshold: float = THRESHOLD) -> str:
    return "bleed" if p >= threshold else "rising" if p >= threshold * 0.7 else "ok"


def bleed_item(top) -> dict:
    """SPINNAKER item for the bleed: each pressure component is its own ledger (gateway_traces,
    scheduler runs, claude_queue, reach_log, ...), i.e. an independent detector. Pure."""
    srcs = []
    for t in (top or []):
        name = (t.get("source") or t.get("name")) if isinstance(t, dict) else (t[0] if isinstance(t, (list, tuple)) else t)
        if name:
            srcs.append({"id": f"detector:boiler:{name}", "type": "detector", "upstream": [f"ledger:{name}"]})
    return {"claim": "open loops are piling up", "sources": srcs}


def compose_bleed(total: float, top: list, dropped: list, threshold: float = THRESHOLD) -> str:
    lines = [f"*Bleeding the boiler* — my unresolved load is at {total:.0f} (I bleed at {threshold:.0f}). "
             f"Top of the pile:"]
    for s, c in top[:TOP_N]:
        ex = ", ".join(list(dict.fromkeys(str(x) for x in c.get("examples", [])))[:3])
        lines.append(f"• {c['n']} {LABEL[s]}" + (f" — {ex}" if ex else ""))
    if dropped:
        lines.append(f"Dropping: {len(dropped)} held note(s) older than {STALE_DRAFT_DAYS} days — stale, not worth your time.")
    defer = [LABEL[s] for s, _ in top[TOP_N:]]
    if defer:
        lines.append("Deferring (I won't raise these unless you ask): " + "; ".join(defer) + ".")
    lines.append("Nothing here needs a reply; this is so the pile is visible, not a to-do list for you.")
    return "\n".join(lines)


def in_window(now=None) -> bool:
    lo, hi = (int(x) for x in WINDOW.split("-"))
    return lo <= (now or datetime.now()).hour < hi


def bled_today(oc) -> bool:
    oc.execute("SELECT count(*) FROM boiler_state WHERE bled AND ts::date = current_date")
    return (oc.fetchone()[0] or 0) > 0


def run(oc, dry: bool = False, post=None) -> dict:
    ensure_schema(oc)
    g = gather(oc)
    total, comps, top = pressure(g)
    st = state_label(total)
    log(f"pressure {total} / {THRESHOLD} ({st}): " + ", ".join(f"{s}={c['p']}" for s, c in top))
    bleed_text, bled = None, False
    if st == "bleed" and in_window() and not bled_today(oc):
        dropped = []
        if not dry:
            oc.execute("UPDATE reach_log SET status='dropped' WHERE lower(audience)='jordan' AND status='held' "
                       "AND ts < now() - make_interval(days => %s) RETURNING id", (STALE_DRAFT_DAYS,))
            dropped = [r[0] for r in oc.fetchall()]
        bleed_text = compose_bleed(total, top, dropped)
        ok = True
        try:
            import nova_annie_rule
            ok = nova_annie_rule.ok(bleed_text)
        except Exception:  # noqa: BLE001
            pass
        if not ok:
            log("bleed text failed the Annie Wilkes rule — not sending")
        elif dry:
            print(bleed_text)
        else:
            try:
                import nova_turning_point
                nova_turning_point.decide(oc, "bleed", stakes=min(1.0, 0.7 * total / THRESHOLD),
                                          text=bleed_text, ceiling="recommend", force=True,
                                          item=bleed_item(top))
            except Exception as e:  # noqa: BLE001
                log(f"turning point log skipped: {e}")
            try:
                if post is None:
                    import nova_config
                    post = lambda t: nova_config.post_both(t, slack_channel=nova_config.SLACK_CHAN)  # noqa: E731
                for attempt in range(3):  # retry the post with backoff before giving up
                    try:
                        post(bleed_text)
                        break
                    except Exception as e:  # noqa: BLE001
                        if attempt == 2:
                            raise
                        log(f"bleed post attempt {attempt + 1} failed: {e}")
                        time.sleep(1.0 * (2 ** attempt))
                bled = True
                log(f"BLED: triage note posted ({len(dropped)} stale drafts dropped)")
            except Exception as e:  # noqa: BLE001
                log(f"bleed post failed: {e}")
    if not dry:
        oc.execute("INSERT INTO boiler_state (pressure, threshold, components, top, bled, bleed_text) "
                   "VALUES (%s,%s,%s,%s,%s,%s)",
                   (total, THRESHOLD, json.dumps(comps, default=str),
                    json.dumps([{"source": s, "p": c["p"], "n": c["n"]} for s, c in top[:TOP_N]]),
                    bled, bleed_text if bled else None))
    return {"pressure": total, "state": st, "top": top, "bled": bled, "text": bleed_text}


def connect(retries: int = 3):
    for attempt in range(retries):
        try:
            return psycopg2.connect(OPS_DSN, connect_timeout=8)
        except psycopg2.OperationalError as e:
            if attempt == retries - 1:
                raise
            log(f"pg connect attempt {attempt + 1} failed: {e}")
            time.sleep(2.0 * (2 ** attempt))


def main():
    ap = argparse.ArgumentParser(description="Nova's Boiler — unresolved-load gauge + daily bleed")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    c = connect()
    c.autocommit = True
    r = run(c.cursor(), dry=a.dry_run)
    print(json.dumps({"pressure": r["pressure"], "state": r["state"], "bled": r["bled"],
                      "top": [(s, x["p"], x["n"]) for s, x in r["top"]]}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
