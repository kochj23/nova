#!/usr/bin/env python3
"""nova_charles.py — CHARLES: before Nova logs an anomaly as the world's doing, she asks "was this me?"

From Shirley Jackson's "Charles" (1948): Laurie starts kindergarten and comes home every day
with news of a classmate called Charles who hits, shouts, and gets kept after school. The
name becomes the family's word for any naughtiness. At the PTA meeting his mother looks for
Charles's mother, and the teacher tells her there is no Charles in the class. The bad
influence everyone looked for outside the house was Laurie. Kin to it: at Hill House the
investigators come to suspect their most sensitive instrument, Eleanor, of producing what
they record; in Frankenstein, Victor's silence lets Justine hang for his creature's act.

Nova's version: the Buick 8 Logbook records anomalies with cause unknown. Charles joins each
anomaly to Nova's OWN actions nearby in time and asks whether that is more often than chance:
  * anomaly points: each unexplained_events row's first_seen, plus every occurrence_key that
    is a minute-precise local time (a sensor's last report); day/hour keys are too coarse;
  * own actions: Big Brother's raw restart log (via the action audit's observer, so unlogged
    restarts count) and claude_actions (read-only action types left out), within ±5 min;
  * baseline: each anomaly is moved by a whole number of days inside the window (time of day
    kept, so Big Brother's daily rhythm cannot "explain" everything), many times over; p is
    the share of shuffles at least as coincident as reality (Bonferroni across pairs).
Recurring (anomaly kind, own producer) pairs beyond chance become claude_queue QUESTIONS
("was this Charles?"), once per pair per week. Coincidence is not cause: Charles never gives
a verdict, never sets a Buick 8 cause, and its subjects are Nova's processes, never people.

Shared lookup: own_actions(cur, since, until, sources) is Nova's own actions in a time window,
used here and by nova_seldon_axioms.py (predictions that came true: was it her doing?).

CLI:      --run [--dry-run] [--days N]   --show   --selftest
Table:    charles_runs (one row per run: the weekly share, by source, recurring pairs)
Config:   service_config ('charles','settings') -> optional overrides of DEFAULTS
Schedule: weekly, Monday 07:25 (after the 07:15 action audit).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import bisect
import json
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_action_audit as A  # noqa: E402
import nova_watch_common as W  # noqa: E402

SERVICE = "charles"
DEFAULTS = {"days": 30, "window_min": 5, "shuffles": 1000, "min_pair": 3, "alpha": 0.05}
QUEUE_SESSION = "nova-charles"
REQUEUE_DAYS = 7
QUIET_TYPES = ["file_read", "staleness-check", "reap"]     # reading never causes an anomaly
SOURCES = ("big_brother", "remediation", "claude_actions", "autonomy", "reach")

SCHEMA = """
CREATE TABLE IF NOT EXISTS charles_runs (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  days int NOT NULL, window_min int NOT NULL,
  points int NOT NULL, coincident int NOT NULL,
  baseline double precision, p double precision,
  by_source jsonb NOT NULL DEFAULT '{}', pairs jsonb NOT NULL DEFAULT '[]');
"""


def log(m: str) -> None:
    print(f"[charles {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


_q = A._rows   # the action audit's fail-open read: an error degrades to "nothing known"


# ── the shared lookup: Nova's own actions in a time window ──────────────────

def bb_files(since: datetime) -> list:
    """nova.jsonl and its numbered rotations that can hold lines at or after `since`."""
    return [p for p in sorted(A.LOG_DIR.glob("nova.jsonl*"))
            if (p.name == "nova.jsonl" or p.suffix[1:].isdigit()) and p.stat().st_mtime >= since.timestamp()]


def own_actions(cur, since: datetime, until: datetime, sources=SOURCES) -> list:
    """Nova's own actions in [since, until], oldest first: [{"ts","source","producer","text"}].
    Big Brother and remediations come from the action audit's observers (Big Brother from its
    raw log, so actions missing from every ledger still count)."""
    out = []
    if "big_brother" in sources:
        out += [{"ts": o["ts"], "source": "big_brother", "producer": o["producer"], "text": o["text"]}
                for o in A.observe_bb(since, bb_files(since))]
    if "remediation" in sources:
        out += [{"ts": o["ts"], "source": "remediation", "producer": "remediation", "text": o["text"]}
                for o in A.observe_remediations(cur, since)]
    if "claude_actions" in sources:
        out += [{"ts": t, "source": "claude_actions", "producer": f"claude:{k}", "text": x or ""}
                for t, k, x in _q(cur, "SELECT ts, action_type, concat_ws(' ', target, description) FROM claude_actions "
                                       "WHERE ts BETWEEN %s AND %s AND action_type <> ALL(%s)",
                                  (since, until, QUIET_TYPES))]
    if "autonomy" in sources:
        out += [{"ts": t, "source": "autonomy", "producer": f"autonomy:{k}", "text": x or ""}
                for t, k, x in _q(cur, "SELECT ts, split_part(action, ' ', 1), concat_ws(' ', action, target, result) "
                                       "FROM autonomy_ledger WHERE ts BETWEEN %s AND %s", (since, until))]
    if "reach" in sources:
        out += [{"ts": t, "source": "reach", "producer": f"reach:{k}", "text": x or ""}
                for t, k, x in _q(cur, "SELECT ts, audience, concat_ws(' ', topic, message) FROM reach_log "
                                       "WHERE status='sent' AND ts BETWEEN %s AND %s", (since, until))]
    return sorted((a for a in out if since <= a["ts"] <= until), key=lambda a: a["ts"])


# ── the join and its baseline (pure) ────────────────────────────────────────

def anomaly_points(rows, since: datetime, until: datetime, window: timedelta = timedelta(0)) -> list:
    """rows = [(kind, first_seen, evidence)] -> sorted [(ts, kind)] inside the window. Points of one
    kind within `window` of the last kept one are one episode (a feeder logging 25 rows at once
    is one event, not 25 coincidences)."""
    pts, kept, last = set(), [], {}
    for kind, first, ev in rows:
        ts = [first]
        for e in ev if isinstance(ev, list) else []:
            try:   # ponytail: keys are read as local wall time; feeders that store UTC keys would be 7-8 h off
                ts.append(datetime.strptime(str(e.get("occurrence_key")), "%Y-%m-%d %H:%M").replace(tzinfo=W.TZ))
            except (ValueError, AttributeError):
                pass
        pts |= {(t, kind) for t in ts if t and since <= t <= until}
    for t, kind in sorted(pts):
        if kind not in last or t - last[kind] > window:
            kept.append((t, kind))
            last[kind] = t
    return kept


def keys_near(t, kind, ts_list, acts, window) -> set:
    i, j = bisect.bisect_left(ts_list, t - window), bisect.bisect_right(ts_list, t + window)
    if i == j:
        return set()
    return {("all",)} | {("source", a["source"]) for a in acts[i:j]} | {("pair", kind, a["producer"]) for a in acts[i:j]}


def analyse(points, acts, since, until, window, shuffles=1000, min_pair=3, seed=0) -> dict:
    """Observed coincidences vs a day-shifted baseline, overall, per source and per pair."""
    ts_list = [a["ts"] for a in acts]

    def tally(pts):
        c = Counter()
        for t, kind in pts:
            c.update(keys_near(t, kind, ts_list, acts, window))
        return c

    obs = tally(points)
    rng, span, days = random.Random(seed), until - since, max(2, (until - since).days)
    tot, ge = Counter(), Counter()
    for _ in range(shuffles):
        shifted = [(since + (t - since + timedelta(days=rng.randint(1, days - 1))) % span, k) for t, k in points]
        c = tally(shifted)
        for k in obs:
            tot[k] += c[k]
            ge[k] += c[k] >= obs[k]
    stat = {k: {"n": n, "baseline": round(tot[k] / shuffles, 2) if shuffles else None,
                "p": round((ge[k] + 1) / (shuffles + 1), 4)} for k, n in obs.items()}
    allk = stat.get(("all",), {"n": 0, "baseline": None, "p": 1.0})
    pairs = [dict(kind=k[1], producer=k[2], **v) for k, v in stat.items() if k[0] == "pair" and v["n"] >= min_pair]
    return {"points": len(points), "coincident": allk["n"], "baseline": allk["baseline"], "p": allk["p"],
            "by_source": {k[1]: v for k, v in stat.items() if k[0] == "source"},
            "pairs": sorted(pairs, key=lambda r: (r["p"], -r["n"]))}


def question(pair: dict, window_min: int, days: int) -> tuple:
    """(description, context) of a claude_queue question. The description is stable per pair (dedup key)."""
    desc = (f"Was this Charles? '{pair['kind']}' anomalies keep landing within ±{window_min} min "
            f"of my own '{pair['producer']}'")
    ctx = (f"nova_charles.py, last {days} days: {pair['n']} '{pair['kind']}' anomaly points had a "
           f"'{pair['producer']}' action within ±{window_min} min; the same anomalies moved to other days "
           f"(same time of day) got {pair['baseline']} on average (p={pair['p']}).\n"
           "Coincidence is not cause: this is a question, not a verdict. Check whether that action could "
           "produce the anomaly (a restart that silences a feed, a probe that looks like a new device). "
           "If it does, add the hypothesis to the Buick 8 row; never set its cause without evidence.")
    return desc, ctx


# ── PG ──────────────────────────────────────────────────────────────────────

def settings(cur) -> dict:
    try:
        return {**DEFAULTS, **(W.get_config(cur, SERVICE, "settings") or {})}
    except Exception as e:  # noqa: BLE001 — missing config is the defaults
        log(f"settings unreadable, using defaults: {e}")
        return dict(DEFAULTS)


def file_question(cur, pair: dict, window_min: int, days: int):
    desc, ctx = question(pair, window_min, days)
    if _q(cur, "SELECT id FROM claude_queue WHERE description=%s AND created_at > now() - make_interval(days => %s)",
          (desc, REQUEUE_DAYS)):
        return None
    cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') ON CONFLICT DO NOTHING",
                (QUEUE_SESSION,))
    cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) "
                "VALUES (%s,'pending',4,%s,%s) RETURNING id", (QUEUE_SESSION, desc, ctx))
    return cur.fetchone()[0]


def run(dry: bool = False, days: int | None = None) -> dict:
    conn = W.connect()
    try:
        cur = conn.cursor()
        cfg = settings(cur)
        if days:
            cfg["days"] = days
        until = datetime.now(timezone.utc)
        since, win = until - timedelta(days=cfg["days"]), timedelta(minutes=cfg["window_min"])
        pts = anomaly_points(_q(cur, "SELECT kind, first_seen, evidence FROM unexplained_events WHERE last_seen >= %s",
                                (since,)), since, until, win)
        acts = own_actions(cur, since - win, until + win, ("big_brother", "claude_actions"))
        res = analyse(pts, acts, since, until, win, cfg["shuffles"], cfg["min_pair"])
        log(f"{'DRY RUN ' if dry else ''}{res['points']} anomaly points, {len(acts)} own actions, {cfg['days']}d; "
            f"{res['coincident']} within ±{cfg['window_min']} min of my own action (chance {res['baseline']}, "
            f"p={res['p']})")
        for s, v in res["by_source"].items():
            print(f"  source {s:<15} {v['n']:>4} vs chance {v['baseline']}  p={v['p']}")
        flagged = [p for p in res["pairs"] if p["p"] <= cfg["alpha"] / max(1, len(res["pairs"]))]   # Bonferroni
        for p in res["pairs"][:15]:
            print(f"  pair {p['kind']:<22} {p['producer'][:40]:<40} {p['n']:>3} vs {p['baseline']}  p={p['p']}"
                  f"{'  <- was this Charles?' if p in flagged else ''}")
        if dry:
            return res
        ensure_schema(cur)
        cur.execute("INSERT INTO charles_runs (days, window_min, points, coincident, baseline, p, by_source, pairs) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
                    (cfg["days"], cfg["window_min"], res["points"], res["coincident"], res["baseline"], res["p"],
                     json.dumps(res["by_source"]), json.dumps(res["pairs"])))
        filed = [q for q in (file_question(cur, p, cfg["window_min"], cfg["days"]) for p in flagged) if q]
        log(f"wrote charles_runs; {len(filed)} new question(s) to claude_queue {filed}")
        return res
    finally:
        conn.close()


def show() -> int:
    conn = W.connect()
    try:
        cur = conn.cursor()
        for ts, d, pts, co, base, p, pairs in _q(cur, "SELECT ts, days, points, coincident, baseline, p, pairs "
                                                      "FROM charles_runs ORDER BY ts DESC LIMIT 8"):
            print(f"{ts:%Y-%m-%d %H:%M}  {d}d  {co}/{pts} coincident (chance {base}, p={p})  {len(pairs)} pairs")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    since, until, win = t0, t0 + timedelta(days=10), timedelta(minutes=5)
    pts = [(t0 + timedelta(days=d, hours=d), "sensor_silence") for d in range(1, 9)]   # no daily rhythm
    acts = [{"ts": t + timedelta(minutes=2), "source": "big_brother", "producer": "bb restart x", "text": ""}
            for t, _ in pts]
    r = analyse(pts, acts, since, until, win, shuffles=50)
    assert r["coincident"] == 8 and r["pairs"][0]["producer"] == "bb restart x" and r["p"] < 0.05, r
    assert analyse(pts, [], since, until, win, shuffles=5)["coincident"] == 0
    ev = [{"occurrence_key": "2026-10-02 09:00"}, {"occurrence_key": "20261002"}]
    got = anomaly_points([("k", t0 + timedelta(days=1), ev)], since, until)
    assert len(got) == 2 and got[1][0] == datetime(2026, 10, 2, 9, 0, tzinfo=W.TZ), got
    assert "never" in question({"kind": "k", "producer": "p", "n": 3, "baseline": 0.2, "p": 0.01}, 5, 30)[1]
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="join anomalies to my own actions, write, file questions")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print the result, write nothing")
    ap.add_argument("--days", type=int, help="window in days (default from settings, 30)")
    ap.add_argument("--show", action="store_true", help="recent runs")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(dry=a.dry_run, days=a.days)
        return 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
