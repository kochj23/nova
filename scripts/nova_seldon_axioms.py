#!/usr/bin/env python3
"""nova_seldon_axioms.py — SELDON'S AXIOMS: was a forecast that came true foresight, or Nova's own doing?

From Asimov's Foundation: Hari Seldon's psychohistory forecasts the future of a galaxy, but only
under two conditions: the population must be enormous, and it must not know it is being
predicted. When one man outside the model, the Mule, appears, the Plan breaks, and the
recorded Seldon of the Time Vault plays a forecast that no longer fits what is happening.
In "All the Troubles of the World", Multivac's own interventions turn out to feed the threat
it predicted; in Herbert's Dune Messiah, Paul's acting on his visions closes them around him.
A forecaster that is told, or that acts, is part of what it forecasts.

Nova's version tags every resolved prediction three ways and scores each bucket apart, so a
"right" forecast she made come true does not count as skill:
  * Axiom 1 (n_class): the population it is about: little_mister, household, fleet,
    neighbourhood, else world (keyword rules on the statement);
  * Axiom 2 (disclosed): shown to Little Mister before it resolved (its "#id by" line or its
    statement in a watch turnover / the PDB, or in a reach to him);
  * Paul's clause (self_touched): one of her own actions, from the SAME lookup Charles uses
    (nova_charles.own_actions: Big Brother's raw log, remediations, claude_actions, autonomy
    ledger, reaches), names the predicted variable between creation and resolves_by. NULL when
    the statement names no variable that can be searched for.
Brier per bucket uses the same arithmetic as Soft Certainty and CARDINAL; "clean" is undisclosed
and untouched. Sample sizes are always shown: small buckets are noisy. The tag undercounts
self-influence (unlogged actions are invisible), which flatters the clean bucket. No silent
control set until Little Mister has agreed to one.

CLI:      --run [--dry-run]   --show   --selftest
Table:    prediction_axioms (one row per resolved prediction; a side table, predictions is not altered)
Config:   service_config ('seldon_axioms','latest') -> per-bucket Brier for CARDINAL / the PDB
Schedule: weekly, Monday 05:40 (before CARDINAL's 05:50 run).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_soft_certainty as SC  # noqa: E402
import nova_watch_common as W  # noqa: E402
from nova_charles import _q, own_actions  # noqa: E402
from nova_predictions import extract_check  # noqa: E402

SERVICE = "seldon_axioms"
HIT = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}
# First match wins. ponytail: keyword rules; a field on the prediction itself would be exact.
N_CLASSES = (("little_mister", r"\b(jordan|little mister|he|his|him)\b"),
             ("neighbourhood", r"scanner|lapd|chp|traffic|neighbo|street|adsb|aircraft|helicopter|burbank"),
             ("household", r"\b(house|home|printer|bambu|kitchen|garage|thermostat|door|pool|power)\b"),
             ("fleet", r"stream|scheduler|service|node|ollama|backup|autonomy|ping|unifi|telemetry|gpu|disk|memor"))
STREAM_RX = re.compile(r"\bthe '?([\w-]+(?: [\w-]+)??)'? (?:memory |telemetry )?stream", re.I)
QUOTED_RX = re.compile(r"'([^']{4,60})'")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prediction_axioms (
  prediction_id int PRIMARY KEY,
  tagged_at timestamptz NOT NULL DEFAULT now(),
  n_class text NOT NULL,
  disclosed boolean NOT NULL,
  disclosed_via text,
  self_touched boolean,
  touched_by jsonb NOT NULL DEFAULT '[]');
"""


def log(m: str) -> None:
    print(f"[seldon {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


# ── tagging (pure) ──────────────────────────────────────────────────────────

def n_class(statement: str) -> str:
    s = (statement or "").lower()
    return next((name for name, rx in N_CLASSES if re.search(rx, s)), "world")


def needles(statement: str, criteria: str) -> set:
    """Words a touching action would name: the check's source / action class, the stream name,
    or a quoted term."""
    chk = extract_check(criteria) or {}
    out = {str(chk.get("source") or ""), str(chk.get("action_class") or "").removeprefix("observe:").strip(":")}
    out |= {m.group(1) for m in STREAM_RX.finditer(statement or "")}
    out |= set(QUOTED_RX.findall(statement or ""))
    out = {n.lower().strip() for n in out}
    return {v for n in out if len(n) >= 4 for v in (n, n.replace(" ", "_"), n.replace("_", " "), n.replace("_", "-"))}


def touching(ns: set, acts: list) -> list:
    """Actions whose text names a needle as a whole word ('fire' never matches 'firewall')."""
    # ponytail: naming is not touching; a shell command that only reads the stream counts too.
    # A structured "variable touched" field on each ledger row would make it exact.
    if not ns:
        return []
    rx = re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, sorted(ns))) + r")(?![a-z0-9])")
    return [a for a in acts if rx.search((a["text"] or "").lower())]


def disclosure(pid: int, statement: str, start, end, texts: list):
    """The first place it was shown to Little Mister in [start, end], or None. texts = [(ts, via, text)]."""
    probe = (statement or "").lower()[:60]
    for ts, via, text in texts:
        t = (text or "").lower()
        if start <= ts <= end and (f"#{pid} by" in t or (len(probe) >= 20 and probe in t)):
            return via
    return None


def tag(pred: tuple, acts: list, ts_list: list, texts: list) -> dict:
    pid, created, resolves_by, statement, criteria = pred[:5]
    ns = needles(statement, criteria)
    window = acts[bisect.bisect_left(ts_list, created):bisect.bisect_right(ts_list, resolves_by)]
    hits = touching(ns, window)
    via = disclosure(pid, statement, created, resolves_by, texts)
    return {"prediction_id": pid, "n_class": n_class(statement), "disclosed": via is not None,
            "disclosed_via": via, "self_touched": bool(hits) if ns else None,
            "touched_by": [{"ts": a["ts"].isoformat(), "source": a["source"], "producer": a["producer"]}
                           for a in hits[:5]]}


def score(preds: list, tags: list) -> dict:
    """Brier per bucket. preds rows end with (confidence, outcome); tags in the same order."""
    buckets: dict = {}
    for p, t in zip(preds, tags):
        pair = (float(p[5]), HIT[p[6]])
        names = ["total", "disclosed" if t["disclosed"] else "undisclosed",
                 {True: "self_touched", False: "untouched", None: "untestable"}[t["self_touched"]],
                 f"n:{t['n_class']}"]
        if not t["disclosed"] and t["self_touched"] is False:
            names.append("clean")
        for b in names:
            buckets.setdefault(b, []).append(pair)
    return {b: SC.brier_stats(v, min_n=1) for b, v in sorted(buckets.items())}


# ── PG ──────────────────────────────────────────────────────────────────────

def disclosures(cur, since, until) -> list:
    rows = [(ts, "watch_turnover", t) for ts, t in _q(cur, "SELECT ts, text FROM watch_turnover WHERE ts BETWEEN %s AND %s",
                                                     (since, until))]
    rows += [(ts, "reach", t) for ts, t in _q(cur, "SELECT ts, message FROM reach_log WHERE audience='jordan' "
                                                  "AND status='sent' AND ts BETWEEN %s AND %s", (since, until))]
    return sorted(rows, key=lambda r: r[0])


def run(dry: bool = False) -> dict:
    conn = W.connect()
    try:
        cur = conn.cursor()
        # ponytail: re-tags every resolved prediction each run (~200 rows); window it past a few thousand.
        preds = _q(cur, "SELECT id, created_at, resolves_by, statement, resolution_criteria, confidence, outcome "
                        "FROM predictions WHERE status='resolved' AND outcome IN ('correct','incorrect','partial') "
                        "AND confidence IS NOT NULL ORDER BY created_at")
        if not preds:
            log("no resolved predictions")
            return {}
        since, until = min(p[1] for p in preds), max(p[2] for p in preds)
        acts = own_actions(cur, since, until)
        ts_list = [a["ts"] for a in acts]
        texts = disclosures(cur, since, until)
        tags = [tag(p, acts, ts_list, texts) for p in preds]
        buckets = score(preds, tags)
        log(f"{'DRY RUN ' if dry else ''}{len(preds)} resolved predictions, {len(acts)} own actions, "
            f"{len(texts)} disclosures")
        for b, s in buckets.items():
            print(f"  {b:<18} n={s['n']:>4}  Brier {s['brier']:.3f}  skill {s['skill']:+.3f}")
        if dry:
            return buckets
        ensure_schema(cur)
        for t in tags:
            cur.execute("INSERT INTO prediction_axioms (prediction_id, n_class, disclosed, disclosed_via, self_touched, "
                        "touched_by) VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (prediction_id) DO UPDATE SET "
                        "tagged_at=now(), n_class=EXCLUDED.n_class, disclosed=EXCLUDED.disclosed, "
                        "disclosed_via=EXCLUDED.disclosed_via, self_touched=EXCLUDED.self_touched, "
                        "touched_by=EXCLUDED.touched_by",
                        (t["prediction_id"], t["n_class"], t["disclosed"], t["disclosed_via"], t["self_touched"],
                         json.dumps(t["touched_by"])))
        W.set_config(cur, SERVICE, "latest", {"at": datetime.now(timezone.utc).isoformat(), "buckets": buckets},
                     by="nova_seldon_axioms")
        log(f"tagged {len(tags)}; wrote service_config ({SERVICE}, latest)")
        return buckets
    finally:
        conn.close()


def show() -> int:
    conn = W.connect()
    try:
        latest = W.get_config(conn.cursor(), SERVICE, "latest") or {}
        print(f"as of {latest.get('at', 'never')}")
        for b, s in (latest.get("buckets") or {}).items():
            print(f"  {b:<18} n={s['n']:>4}  Brier {s['brier']:.3f}  skill {s['skill']:+.3f}")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    crit = 'x ```check\n{"type": "mem_activity", "source": "fire", "expect": "active", "min": 1}\n```'
    assert needles("The fire memory stream will stay active.", crit) >= {"fire"}
    assert "automotive rebuilds" in needles("The automotive rebuilds memory stream will grow.", "")
    assert "horology" in needles("The horology memory stream will be silent.", "")
    acts = [{"ts": t0 + timedelta(hours=1), "source": "big_brother", "producer": "bb", "text": "Restarted fire_ingest"},
            {"ts": t0 + timedelta(hours=2), "source": "claude_actions", "producer": "c", "text": "firewall rule"}]
    assert len(touching({"fire"}, acts)) == 1
    pred = (7, t0, t0 + timedelta(hours=6), "The fire memory stream will stay active.", crit, 0.8, "correct")
    t = tag(pred, acts, [a["ts"] for a in acts], [(t0 + timedelta(hours=3), "watch_turnover", "#7 by 06:00: ...")])
    assert t["self_touched"] and t["disclosed_via"] == "watch_turnover" and t["n_class"] == "fleet", t
    assert tag((8,) + pred[1:3] + ("Rain tomorrow.", ""), acts, [], [])["self_touched"] is None
    assert n_class("Little Mister will reply by noon") == "little_mister"
    s = score([pred, (8, t0, t0, "s", "", 0.3, "incorrect")], [t, dict(t, disclosed=False, self_touched=False)])
    assert s["total"]["n"] == 2 and s["clean"]["n"] == 1 and s["self_touched"]["n"] == 1, s
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="tag resolved predictions, score each bucket, write")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print the buckets, write nothing")
    ap.add_argument("--show", action="store_true", help="latest stored bucket scores")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(dry=a.dry_run)
        return 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
