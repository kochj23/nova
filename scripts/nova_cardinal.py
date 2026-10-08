#!/usr/bin/env python3
"""nova_cardinal.py — CARDINAL: every source Nova listens to, graded by its own track record.

Clancy's CARDINAL was trusted because of decades of being right, and the whole service knew
exactly how far to trust him. Nova hears many voices — cameras, face recognition, scanner
talkgroups, news feeds, her LLM tools, her own predictions, presence methods, alert detectors —
and until now trusted them all the same. This organ keeps a ledger of each one and grades
any claim the way an intelligence service does (NATO Admiralty / STANAG 2511):

  Source reliability   A completely reliable  B usually  C fairly  D not usually
                       E unreliable           F cannot be judged (no track record / suspect)
  Information credibility  1 confirmed by INDEPENDENT sources of >= 2 different sensor TYPES
                       2 probably true (independent, same type)  3 possibly true (one good source)
                       4 doubtful  5 improbable (contradicted by an independent source)  6 cannot be judged

Reliability comes from the track record, never from a vibe:
  * hit-rate with a Wilson lower bound (so 3/3 is not "A"), and for probabilistic forecasters
    Brier skill vs. the base rate (nova_soft_certainty.brier_stats — the same arithmetic as the
    "I was wrong" loop). Below MIN_N outcomes a source is F.
  * truth kinds, stated on every row:  labelled (alert_learn's was_real/was_noise, resolved
    predictions, Jordan's approve/reject), cross-sensor (camera vs same-room mmWave),
    fusion-agreement (a presence method vs the fused presence history — partly circular, capped
    at B), corroboration (scanner / news: only "an independent source also saw it"; absence is
    not a miss, so these cap at C and never go below it on corroboration alone).

COMPROMISE DETECTION: a source that suddenly behaves unlike itself — its daily volume collapses
to zero or jumps > 4 sigma above its own 14-day baseline, or its recent hit-rate falls clearly
below its long-run hit-rate — is flagged `compromise_suspect` with the reason, logged to the
Buick 8 Logbook (cause 'unknown' until evidenced), and graded F until it is explained.

Library: grade(item) -> {"code": "B2", "reliability", "credibility", "label", "spinnaker", ...}
         estimative(p) -> ICD-203 words; load_ledger(oc); record_outcome(oc, ...)
CLI:     --score (harvest every track record, write source_ledger + history; daily)
         --show [--json]   --grade '<item json>'   --selftest
Tables:  source_ledger, source_ledger_history, source_outcomes (organ-fed outcomes)
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_spinnaker as SP  # noqa: E402

LETTERS = "ABCDEF"
REL_LABEL = {"A": "completely reliable", "B": "usually reliable", "C": "fairly reliable",
             "D": "not usually reliable", "E": "unreliable", "F": "reliability cannot be judged"}
CRED_LABEL = {1: "confirmed", 2: "probably true", 3: "possibly true", 4: "doubtful",
              5: "improbable", 6: "truth cannot be judged"}
MIN_N = 10
CAPS = {"fusion-agreement": "B", "corroboration": "C"}
# ICD 203 estimative language (US Intelligence Community), lower bound -> words.
ESTIMATIVE = [(0.95, "almost certainly"), (0.80, "very likely"), (0.55, "likely"),
              (0.45, "roughly even chance"), (0.20, "unlikely"), (0.05, "very unlikely"),
              (0.0, "almost no chance")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_ledger (
  source_id text PRIMARY KEY,
  source_type text NOT NULL,
  upstream text[] NOT NULL DEFAULT '{}',
  truth_kind text NOT NULL,
  n int NOT NULL DEFAULT 0,
  hits real NOT NULL DEFAULT 0,
  hit_rate real,
  wilson_lo real,
  brier real,
  skill real,
  reliability char(1) NOT NULL DEFAULT 'F',
  baseline jsonb NOT NULL DEFAULT '{}',
  compromise_suspect boolean NOT NULL DEFAULT false,
  compromise_reason text,
  detail jsonb NOT NULL DEFAULT '{}',
  scored_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS source_ledger_history (
  id bigserial PRIMARY KEY,
  source_id text NOT NULL,
  scored_at timestamptz NOT NULL DEFAULT now(),
  reliability char(1) NOT NULL,
  n int, hit_rate real, wilson_lo real, skill real,
  compromise_suspect boolean NOT NULL DEFAULT false);
CREATE INDEX IF NOT EXISTS source_ledger_history_sid ON source_ledger_history (source_id, scored_at DESC);
CREATE TABLE IF NOT EXISTS source_outcomes (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  source_id text NOT NULL,
  source_type text NOT NULL,
  p real,
  outcome real NOT NULL CHECK (outcome >= 0 AND outcome <= 1),
  ref text,
  recorded_by text NOT NULL);
CREATE INDEX IF NOT EXISTS source_outcomes_sid ON source_outcomes (source_id, ts DESC);
"""


def log(m: str) -> None:
    print(f"[cardinal {datetime.now():%H:%M:%S}] {m}", flush=True)


# ── pure scoring ────────────────────────────────────────────────────────────

def wilson(hits: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval for a hit-rate. (0,1) when n == 0."""
    if n <= 0:
        return 0.0, 1.0
    p = hits / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def letter_from_hits(wlo: float) -> str:
    return "A" if wlo >= 0.90 else "B" if wlo >= 0.75 else "C" if wlo >= 0.55 else "D" if wlo >= 0.35 else "E"


def letter_from_skill(skill: float) -> str:
    return "A" if skill >= 0.5 else "B" if skill >= 0.25 else "C" if skill >= 0.05 else "D" if skill >= -0.05 else "E"


def cap(letter: str, ceiling: str | None) -> str:
    """Never better than the ceiling (A is best)."""
    if not ceiling:
        return letter
    return letter if LETTERS.index(letter) >= LETTERS.index(ceiling) else ceiling


def reliability(n: int, hits: float, truth_kind: str, skill: float | None = None,
                corroborated: int | None = None, min_n: int = MIN_N) -> str:
    """Admiralty letter from a track record. Pure."""
    if truth_kind == "corroboration":
        return "C" if (corroborated or 0) >= 3 else "F"
    if n < min_n:
        return "F"
    lo, _hi = wilson(hits, n)
    letter = letter_from_hits(lo)
    if skill is not None:          # probabilistic forecaster: worse of hit-rate and Brier skill
        ls = letter_from_skill(skill)
        letter = max(letter, ls, key=LETTERS.index)
    return cap(letter, CAPS.get(truth_kind))


def score_record(source_id: str, source_type: str, truth_kind: str, outcomes: list,
                 corroborated: int | None = None, min_n: int = MIN_N, detail: dict | None = None) -> dict:
    """outcomes: [(p_or_None, outcome 0..1)]. -> a source_ledger row dict. Pure."""
    n = len(outcomes)
    hits = float(sum(o for _p, o in outcomes))
    lo, hi = wilson(hits, n)
    probs = [(p, o) for p, o in outcomes if p is not None]
    bs = None
    if len(probs) >= 5:
        try:
            from nova_soft_certainty import brier_stats
            bs = brier_stats(probs)
        except Exception:  # noqa: BLE001
            bs = None
    rel = reliability(n, hits, truth_kind, (bs or {}).get("skill"), corroborated, min_n)
    return {"source_id": source_id, "source_type": source_type,
            "upstream": sorted(SP.upstreams({"id": source_id, "type": source_type})),
            "truth_kind": truth_kind, "n": n, "hits": round(hits, 2),
            "hit_rate": round(hits / n, 4) if n else None, "wilson_lo": round(lo, 4), "wilson_hi": round(hi, 4),
            "brier": (bs or {}).get("brier"), "skill": (bs or {}).get("skill"), "reliability": rel,
            "detail": dict(detail or {}, corroborated=corroborated) if corroborated is not None else dict(detail or {})}


def volume_anomaly(daily: list, today: float, min_mean: float = 5.0, z_max: float = 4.0) -> str | None:
    """daily = counts for the previous days (oldest first); today = last 24h. -> reason or None."""
    if len(daily) < 7:
        return None
    mean = sum(daily) / len(daily)
    if mean < min_mean:
        return None
    sd = math.sqrt(sum((d - mean) ** 2 for d in daily) / len(daily)) or 1.0
    if today == 0 and min(daily) > 0:
        return f"went silent: 0 in the last 24h vs {mean:.0f}/day baseline (never silent in {len(daily)}d)"
    z = (today - mean) / sd
    if z > z_max and today > 3 * mean:
        return f"volume jumped to {today:.0f} in 24h vs {mean:.0f}±{sd:.0f}/day (z={z:.1f})"
    return None


def accuracy_drift(long_hits: float, long_n: int, recent_hits: float, recent_n: int) -> str | None:
    """A trusted source whose recent hit-rate is clearly below its own long-run rate."""
    if long_n < 30 or recent_n < 8:
        return None
    llo, _ = wilson(long_hits, long_n)
    _, rhi = wilson(recent_hits, recent_n)
    if llo >= 0.55 and rhi < llo and recent_hits / recent_n <= long_hits / long_n - 0.15:
        return (f"recent hit-rate {recent_hits / recent_n:.0%} (n={recent_n}) is below its long-run "
                f"floor {llo:.0%} (n={long_n})")
    return None


def estimative(p: float | None) -> str:
    """ICD-203 words for a probability."""
    if p is None:
        return "cannot estimate"
    p = max(0.0, min(1.0, float(p)))
    for lo, words in ESTIMATIVE:
        if p >= lo:
            return words
    return "almost no chance"


def estimative_line(p: float | None, oc=None, domain: str | None = None) -> tuple[str, float | None]:
    """Calibrate (nova_soft_certainty, per domain) then map to words. -> (words, calibrated p)."""
    if p is None:
        return "cannot estimate", None
    q = p
    if oc is not None:
        try:
            from nova_soft_certainty import calibrate
            q = float(calibrate(p, oc, domain))
        except Exception:  # noqa: BLE001
            q = p
    return f"{estimative(q)} (~{q:.0%})", q


# ── grading ─────────────────────────────────────────────────────────────────

def _lookup(ledger: dict, sid: str) -> dict:
    r = ledger.get(sid)
    if r:
        return r
    # family fallback: 'detector:nova_security_organ.py' vs 'detector:nova_security_organ'
    base = re.sub(r"\.py$", "", sid)
    for k, v in ledger.items():
        if re.sub(r"\.py$", "", k) == base:
            return v
    return {}


def source_letter(ledger: dict, src: dict) -> tuple[str, bool]:
    if src.get("reliability") in tuple(LETTERS):
        return src["reliability"], False
    r = _lookup(ledger, src.get("id", ""))
    if not r:
        return "F", False
    if r.get("compromise_suspect"):
        return "F", True
    return (r.get("reliability") or "F"), False


def grade(item: dict, oc=None, ledger: dict | None = None) -> dict:
    """Admiralty grade for a claim. item = {claim, sources:[{id, type?, upstream?, motive?, text?,
    reliability?}], contradicted_by?, expected?, p?, domain?}. Pure when `ledger` is given."""
    if ledger is None:
        ledger = load_ledger(oc) if oc is not None else {}
    a = SP.assess(item)
    sources = [s for s in (item.get("sources") or []) if s and s.get("id")]
    per = []
    for s in sources:
        letter, comp = source_letter(ledger, s)
        per.append({"id": s["id"], "type": SP.stype(s), "reliability": letter, "compromise_suspect": comp})
    sensors = [p for p in per if p["type"] not in SP.MOTIVE_TYPES]
    pool = sensors or per
    rel = min((p["reliability"] for p in pool), key=LETTERS.index) if pool else "F"
    contra = [s for s in (item.get("contradicted_by") or []) if s and s.get("id")]
    if a["verdict"] == "CONTESTED":
        cbest = min((source_letter(ledger, c)[0] for c in contra), key=LETTERS.index)
        cred = 5 if LETTERS.index(cbest) <= LETTERS.index(rel) and cbest != "F" else 4
    elif not sensors:
        cred = 6
    elif a["independent"] >= 2 and len(a["independent_types"]) >= 2 and a["verdict"] == "CORROBORATED":
        cred = 1
    elif a["independent"] >= 2:
        cred = 2
    elif a["verdict"] == "UNCORROBORATED":
        cred = 4
    elif rel in ("A", "B", "C"):
        cred = 3
    elif rel in ("D", "E"):
        cred = 4
    else:
        cred = 6
    out = {"code": f"{rel}{cred}", "reliability": rel, "credibility": cred,
           "label": f"{REL_LABEL[rel]} / {CRED_LABEL[cred]}", "sources": per,
           "spinnaker": a, "may_trigger_max": a["max_rung"]}
    if item.get("p") is not None:
        words, q = estimative_line(item["p"], oc, item.get("domain"))
        out["estimative"], out["p_calibrated"] = words, q
    return out


# ── ledger I/O ──────────────────────────────────────────────────────────────

def _connect():
    import nova_watch_common as W
    return W.connect()


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def load_ledger(oc=None) -> dict:
    """{source_id: row} — never raises; {} if PG is unreachable (everything grades F)."""
    own = None
    try:
        if oc is None:
            own = _connect()
            oc = own.cursor()
        oc.execute("SELECT source_id, source_type, truth_kind, n, hit_rate, wilson_lo, skill, reliability, "
                   "compromise_suspect, compromise_reason FROM source_ledger")
        cols = ("source_id", "source_type", "truth_kind", "n", "hit_rate", "wilson_lo", "skill",
                "reliability", "compromise_suspect", "compromise_reason")
        return {r[0]: dict(zip(cols, r)) for r in oc.fetchall()}
    except Exception:  # noqa: BLE001
        try:
            oc.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {}
    finally:
        if own is not None:
            own.close()


def record_outcome(oc, source_id: str, source_type: str, outcome: float, p: float | None = None,
                   ref: str | None = None, recorded_by: str = "unknown") -> None:
    """Organs feed truth here (e.g. Jordan said 'that was nothing'; a hotwash found a false alarm).
    Organ-fed outcomes join the harvested record on the next --score."""
    ensure_schema(oc)
    oc.execute("INSERT INTO source_outcomes (source_id, source_type, p, outcome, ref, recorded_by) "
               "VALUES (%s,%s,%s,%s,%s,%s)", (source_id, source_type, p, float(outcome), ref, recorded_by))


def write_rows(cur, rows: list) -> int:
    ensure_schema(cur)
    for r in rows:
        cur.execute(
            "INSERT INTO source_ledger (source_id, source_type, upstream, truth_kind, n, hits, hit_rate, wilson_lo, "
            "brier, skill, reliability, baseline, compromise_suspect, compromise_reason, detail, scored_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb, now()) "
            "ON CONFLICT (source_id) DO UPDATE SET source_type=EXCLUDED.source_type, upstream=EXCLUDED.upstream, "
            "truth_kind=EXCLUDED.truth_kind, n=EXCLUDED.n, hits=EXCLUDED.hits, hit_rate=EXCLUDED.hit_rate, "
            "wilson_lo=EXCLUDED.wilson_lo, brier=EXCLUDED.brier, skill=EXCLUDED.skill, "
            "reliability=EXCLUDED.reliability, baseline=EXCLUDED.baseline, "
            "compromise_suspect=EXCLUDED.compromise_suspect, compromise_reason=EXCLUDED.compromise_reason, "
            "detail=EXCLUDED.detail, scored_at=now()",
            (r["source_id"], r["source_type"], r.get("upstream") or [], r["truth_kind"], r["n"], r["hits"],
             r.get("hit_rate"), r.get("wilson_lo"), r.get("brier"), r.get("skill"), r["reliability"],
             json.dumps(r.get("baseline") or {}), bool(r.get("compromise_suspect")), r.get("compromise_reason"),
             json.dumps(r.get("detail") or {}, default=str)))
        cur.execute("INSERT INTO source_ledger_history (source_id, reliability, n, hit_rate, wilson_lo, skill, "
                    "compromise_suspect) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (r["source_id"], r["reliability"], r["n"], r.get("hit_rate"), r.get("wilson_lo"),
                     r.get("skill"), bool(r.get("compromise_suspect"))))
    return len(rows)


# ── harvesters: each returns [row] built with score_record (DB in, pure scoring) ──

def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — one broken harvester never sinks the others
        log(f"harvest query failed: {e}")
        try:
            cur.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def harvest_predictions(cur) -> list:
    rows = _q(cur, "SELECT domain, confidence, outcome FROM predictions WHERE status='resolved' "
                   "AND outcome IN ('correct','incorrect','partial') AND confidence IS NOT NULL") or []
    hv = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}
    by: dict = {}
    for d, c, o in rows:
        by.setdefault(d, []).append((float(c), hv[o]))
    out = [score_record(f"nova:prediction:{d}", "prediction", "labelled", v) for d, v in by.items()]
    if rows:
        out.append(score_record("nova:prediction:all", "prediction", "labelled",
                                [x for v in by.values() for x in v]))
    return out


def harvest_detectors(cur, days: int = 90) -> list:
    """alert_learn grades every alert was_real / was_noise. Each emitting source is a detector;
    the AI triage that decided page/suppress is an LLM tool with its own record."""
    rows = _q(cur, "SELECT source, decision, outcome, ts FROM alert_triage_log WHERE ts > now() - make_interval(days => %s) "
                   "AND outcome IN ('was_real','was_noise') ORDER BY ts", (days,)) or []
    by: dict = {}
    tri = []
    for src, dec, out, ts in rows:
        hit = 1.0 if out == "was_real" else 0.0
        by.setdefault(src or "unknown", []).append((None, hit, ts))
        tri.append((None, 1.0 if (dec == "page") == (out == "was_real") else 0.0, ts))
    res = []
    for src, v in by.items():
        r = score_record(f"detector:{src}", "detector", "labelled", [(p, o) for p, o, _t in v])
        r["_series"] = v
        res.append(r)
    if tri:
        r = score_record("llm:alert_triage", "llm", "labelled", [(p, o) for p, o, _t in tri],
                         detail={"meaning": "page when real, suppress/downgrade when noise"})
        r["_series"] = tri
        res.append(r)
    return res


def harvest_llm_tools(cur) -> list:
    out = []
    rl = _q(cur, "SELECT status FROM reach_log WHERE status IN ('sent','filed','dropped','journaled')") or []
    if rl:
        out.append(score_record("llm:reach_writer", "llm", "labelled",
                                [(None, 0.0 if s == "dropped" else 1.0) for (s,) in rl],
                                detail={"meaning": "reach drafts that survived ground_reach (dropped = unsupported claims)"}))
    vc = _q(cur, "SELECT (value_check->>'allowed')::boolean, status FROM coagency_proposals "
                 "WHERE status IN ('approved','rejected','executed','declined') AND value_check ? 'allowed'") or []
    pairs = [(None, 1.0 if bool(a) == (s in ("approved", "executed")) else 0.0) for a, s in vc if a is not None]
    if pairs:
        out.append(score_record("llm:value_check", "llm", "labelled", pairs,
                                detail={"meaning": "value_check verdict agreed with Jordan's decision"}))
    return out


def harvest_presence(cur, days: int = 21) -> list:
    """Each identity method's claim 'Jordan is home' vs the fused history (embodiment_state).
    Fusion includes the method itself, so this is agreement, not truth: capped at B."""
    emb = _q(cur, "SELECT computed_at, occupancy->'residents_home' FROM embodiment_state "
                  "WHERE computed_at > now() - make_interval(days => %s) ORDER BY computed_at", (days,)) or []
    if not emb:
        return []
    ets = [t for t, _r in emb]
    home = [bool(r and "jordan" in r) for _t, r in emb]
    rows = _q(cur, "SELECT ts, method, coalesce(metadata->>'source',''), room FROM telemetry.presence "
                   "WHERE person='jordan' AND method IN ('ble_rssi','wifi_rssi','gps_tracker') "
                   "AND ts > now() - make_interval(days => %s) ORDER BY ts", (days,)) or []
    by: dict = {}
    for ts, method, src, room in rows:
        i = bisect.bisect_right(ets, ts)
        if not i or (ts - ets[i - 1]).total_seconds() > 3600:
            continue
        if room in (None, "", "unknown", "nearby"):
            continue
        claims_home = room not in ("away", "not_home")
        sid = f"presence:{method}" + (f":{src}" if method == "gps_tracker" and src else "")
        by.setdefault(sid, []).append((None, 1.0 if claims_home == home[i - 1] else 0.0))
    return [score_record(k, "gps" if "gps" in k else "rf_presence", "fusion-agreement", v) for k, v in by.items()]


def mmwave_health(per_room: dict, min_readings: int = 500) -> dict:
    """per_room = {room: {minute: occupied_bool}}. -> {room: reason} for rooms that cannot be
    trusted as a reference: STUCK (one value for every reading) or DUPLICATE (minute-for-minute
    identical to another room, including occupied minutes — one sensor reported as two rooms). Pure."""
    bad = {}
    for room, ser in per_room.items():
        vals = set(ser.values())
        if len(ser) >= min_readings and len(vals) == 1:
            bad[room] = f"stuck: {len(ser)} readings, every one '{next(iter(vals))}'"
    rooms = sorted(per_room)
    for i, a in enumerate(rooms):
        for b in rooms[i + 1:]:
            common = set(per_room[a]) & set(per_room[b])
            if len(common) < 200:
                continue
            same = sum(per_room[a][m] == per_room[b][m] for m in common)
            occ = sum(1 for m in common if per_room[a][m])
            if occ >= 10 and same / len(common) >= 0.99:
                bad.setdefault(b, f"duplicate of {a}: identical in {same}/{len(common)} minutes "
                                  f"({occ} occupied) — shared upstream, not a second sensor")
    return bad


def harvest_mmwave(cur, days: int = 3) -> tuple[list, dict]:
    rows = _q(cur, "SELECT date_trunc('minute', ts), room, bool_or(metadata->>'occupied'='true') FROM telemetry.presence "
                   "WHERE method='mmwave' AND ts > now() - make_interval(days => %s) GROUP BY 1,2", (days,)) or []
    per: dict = {}
    for t, r, o in rows:
        per.setdefault(r, {})[t] = bool(o)
    bad = mmwave_health(per)
    out = []
    for room, ser in per.items():
        r = score_record(f"presence:mmwave:{room}", "mmwave", "none", [],
                         detail={"readings": len(ser), "occupied_minutes": sum(ser.values())})
        if room in bad:
            r["_flag"] = bad[room]
        out.append(r)
    return out, bad


def harvest_cameras(cur, days: int = 14, bad_rooms: dict | None = None) -> list:
    """Interior camera person detections vs the same room's mmWave (a different sensor type).
    Exterior cameras have no independent ground truth: they stay F, with volume baselines only."""
    mm = _q(cur, "SELECT date_trunc('minute', ts), room, bool_or(metadata->>'occupied'='true') FROM telemetry.presence "
                 "WHERE method='mmwave' AND ts > now() - make_interval(days => %s) GROUP BY 1,2", (days,)) or []
    occ = {(t, r): o for t, r, o in mm}
    cams = _q(cur, "SELECT DISTINCT date_trunc('minute', ts), metadata->>'camera', room FROM telemetry.presence "
                   "WHERE metadata->>'source'='frigate' AND metadata->>'label'='person' "
                   "AND ts > now() - make_interval(days => %s)", (days,)) or []
    by: dict = {}
    seen: set = set()
    for t, cam, room in cams:
        seen.add(cam)
        if not cam or not cam.startswith("interior_") or room in (bad_rooms or {}):
            continue
        for dt in (0, -1, 1):
            o = occ.get((t + timedelta(minutes=dt), room))
            if o is not None:
                by.setdefault(cam, []).append((None, 1.0 if o else 0.0))
                break
    out = [score_record(f"camera:{c}", "camera", "cross-sensor", v, detail={"truth": "same-room mmWave"})
           for c, v in by.items()]
    for c in sorted(seen - set(by)):
        if c:
            out.append(score_record(f"camera:{c}", "camera", "none", [],
                                    detail={"truth": "no independent ground truth for this view"}))
    return out


def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']{4,}", (s or "").lower())}


def harvest_news(mcur, days: int = 14) -> list:
    """Per feed: how often a story is independently corroborated by a DIFFERENT outlet (title
    word Jaccard >= 0.5 within 48h) that is not the same wire copy."""
    rows = _q(mcur, "SELECT created_at, metadata->>'feed', coalesce(metadata->>'title', left(text, 160)), left(text, 600) "
                    "FROM memories WHERE source IN ('news','local_news') AND created_at > now() - make_interval(days => %s) "
                    "AND metadata->>'feed' IS NOT NULL ORDER BY created_at LIMIT 5000", (days,)) or []
    items = [(t, f, _tokens(ti), SP.wire_of(tx)) for t, f, ti, tx in rows]
    by: dict = {}
    corr: dict = {}
    for i, (t, f, tk, w) in enumerate(items):
        by[f] = by.get(f, 0) + 1
        if len(tk) < 3:
            continue
        for j in range(i + 1, len(items)):
            t2, f2, tk2, w2 = items[j]
            if (t2 - t).total_seconds() > 172800:
                break
            if f2 == f or (w and w == w2) or len(tk2) < 3:
                continue
            if len(tk & tk2) / len(tk | tk2) >= 0.5:
                corr[f] = corr.get(f, 0) + 1
                corr[f2] = corr.get(f2, 0) + 1
                break
    return [score_record(f"news:{f}", "news", "corroboration", [], corroborated=corr.get(f, 0),
                         detail={"items": n, "corroboration_rate": round(corr.get(f, 0) / n, 3) if n else None})
            for f, n in by.items()]


def harvest_scanner(cur, mcur, days: int = 30) -> list:
    """Per talkgroup: near-home transmissions independently corroborated (CHP incident or a
    low helicopter within 30 min). Absence of corroboration is not counted as a miss."""
    rows = _q(mcur, "SELECT created_at, coalesce(metadata->>'channel', metadata->>'talkgroup', '?') FROM memories "
                    "WHERE source='scanner' AND created_at > now() - make_interval(days => %s) "
                    "AND (metadata->'geo'->>'nearest_mi')::float <= 1.5", (days,)) or []
    if not rows:
        return []
    start = datetime.now(timezone.utc) - timedelta(days=days)
    chp = sorted(t for (t,) in (_q(cur, "SELECT min(ts) FROM telemetry.chp_incidents WHERE ts > %s "
                                        "GROUP BY incident_id", (start,)) or []))
    heli = sorted(t for (t,) in (_q(cur, "SELECT ts FROM telemetry.overhead_flights WHERE ts > %s AND is_helicopter "
                                         "AND alt_ft < 2500 AND dist_nm < 1.5", (start,)) or []))
    by: dict = {}
    corr: dict = {}
    w = timedelta(minutes=30)
    for t, ch in rows:
        by[ch] = by.get(ch, 0) + 1
        hit = any(bisect.bisect_left(s, t + w) - bisect.bisect_left(s, t - w) > 0 for s in (chp, heli))
        corr[ch] = corr.get(ch, 0) + (1 if hit else 0)
    return [score_record(f"scanner:{ch}", "radio", "corroboration", [], corroborated=corr[ch],
                         detail={"near_home": n, "corroboration_rate": round(corr[ch] / n, 3)}) for ch, n in by.items()]


def harvest_face(cur, mcur, days: int = 120) -> list:
    """Named HOUSEHOLD sightings vs the fused presence history at that hour. Non-household
    identities are never scored (purpose limitation: we don't keep track records on visitors)."""
    hh = household_names(cur)
    rows = _q(mcur, "SELECT created_at, metadata->>'person' FROM memories WHERE source='face_recognition' "
                    "AND created_at > now() - make_interval(days => %s) AND metadata->>'person' IS NOT NULL", (days,)) or []
    emb = _q(cur, "SELECT computed_at, occupancy->'residents_home' FROM embodiment_state ORDER BY computed_at") or []
    ets = [t for t, _r in emb]
    outs = []
    for t, person in rows:
        key = hh.get((person or "").strip().lower())
        if not key:
            continue
        i = bisect.bisect_right(ets, t)
        if not i or (t - ets[i - 1]).total_seconds() > 3600:
            continue
        outs.append((None, 1.0 if key in (emb[i - 1][1] or []) else 0.0))
    return [score_record("face:recognition", "face", "fusion-agreement", outs,
                         detail={"scored": "household sightings only"})]


def household_names(cur) -> dict:
    """face_people display name (lower) -> resident key. service_config face_retention/household
    overrides; default = the presence residents (jordan, amy)."""
    try:
        import nova_watch_common as W
        hh = W.get_config(cur, "face_retention", "household", None)
    except Exception:  # noqa: BLE001
        hh = None
    hh = hh or {"jordan koch": "jordan", "amy mccaine": "amy", "amy": "amy"}
    return {k.lower(): v for k, v in hh.items()}


def harvest_organ_fed(cur) -> list:
    rows = _q(cur, "SELECT source_id, source_type, p, outcome FROM source_outcomes WHERE ts > now() - interval '180 days'") or []
    by: dict = {}
    for sid, st, p, o in rows:
        by.setdefault((sid, st), []).append((p, float(o)))
    return [score_record(sid, st, "labelled", v) for (sid, st), v in by.items()]


# ── volume baselines (compromise detection) ─────────────────────────────────

def daily_volumes(cur, mcur, days: int = 15) -> dict:
    """{source_id: [count per day, oldest first, last element = last 24h]}"""
    out: dict = {}

    def fold(rows, prefix):
        for sid, d, n in rows or []:
            out.setdefault(f"{prefix}{sid}", {})[int(d)] = int(n)

    fold(_q(cur, "SELECT metadata->>'camera', floor(extract(epoch from now()-ts)/86400), count(*) FROM telemetry.presence "
                 "WHERE metadata->>'source'='frigate' AND ts > now() - make_interval(days => %s) GROUP BY 1,2", (days,)), "camera:")
    fold(_q(cur, "SELECT source, floor(extract(epoch from now()-ts)/86400), count(*) FROM alert_triage_log "
                 "WHERE ts > now() - make_interval(days => %s) GROUP BY 1,2", (days,)), "detector:")
    fold(_q(cur, "SELECT method, floor(extract(epoch from now()-ts)/86400), count(*) FROM telemetry.presence "
                 "WHERE person='jordan' AND ts > now() - make_interval(days => %s) GROUP BY 1,2", (days,)), "presence:")
    fold(_q(mcur, "SELECT coalesce(metadata->>'channel','?'), floor(extract(epoch from now()-created_at)/86400), count(*) "
                  "FROM memories WHERE source='scanner' AND created_at > now() - make_interval(days => %s) GROUP BY 1,2",
            (days,)), "scanner:")
    fold(_q(mcur, "SELECT metadata->>'feed', floor(extract(epoch from now()-created_at)/86400), count(*) FROM memories "
                  "WHERE source IN ('news','local_news') AND created_at > now() - make_interval(days => %s) "
                  "AND metadata->>'feed' IS NOT NULL GROUP BY 1,2", (days,)), "news:")
    return {sid: [d.get(i, 0) for i in range(days - 1, -1, -1)] for sid, d in out.items()}


def detect_compromise(row: dict, series: list | None) -> str | None:
    """series: daily counts oldest..today. Plus accuracy drift when the row has a dated record."""
    reasons = []
    if series and len(series) >= 8:
        v = volume_anomaly(series[:-1], series[-1])
        if v:
            reasons.append(v)
    s = row.get("_series")
    if s:
        cut = datetime.now(timezone.utc) - timedelta(days=7)
        old = [o for _p, o, t in s if t < cut]
        new = [o for _p, o, t in s if t >= cut]
        d = accuracy_drift(sum(old), len(old), sum(new), len(new))
        if d:
            reasons.append(d)
    return "; ".join(reasons) or None


def score_all(dry: bool = False) -> list:
    import nova_watch_common as W
    conn = W.connect()
    mconn = W.connect(W.MEM_DSN)
    cur, mcur = conn.cursor(), mconn.cursor()
    rows, bad_rooms = [], {}
    try:
        mm, bad_rooms = harvest_mmwave(cur)
        rows.extend(mm)
        log(f"mmwave: {len(mm)} room(s), {len(bad_rooms)} untrustworthy as a reference")
    except Exception as e:  # noqa: BLE001
        log(f"mmwave harvester failed: {e}")
    for name, fn in (("predictions", lambda: harvest_predictions(cur)), ("detectors", lambda: harvest_detectors(cur)),
                     ("llm", lambda: harvest_llm_tools(cur)), ("presence", lambda: harvest_presence(cur)),
                     ("cameras", lambda: harvest_cameras(cur, bad_rooms=bad_rooms)), ("news", lambda: harvest_news(mcur)),
                     ("scanner", lambda: harvest_scanner(cur, mcur)), ("face", lambda: harvest_face(cur, mcur)),
                     ("organ-fed", lambda: harvest_organ_fed(cur))):
        try:
            got = fn()
            log(f"{name}: {len(got)} source(s)")
            rows.extend(got)
        except Exception as e:  # noqa: BLE001
            log(f"{name} harvester failed: {e}")
    vols = daily_volumes(cur, mcur)
    prior = load_ledger(cur)
    flagged = []
    for r in rows:
        series = vols.get(r["source_id"]) or vols.get(re.sub(r"\.py$", "", r["source_id"]))
        r["baseline"] = {"daily": series} if series else {}
        why = "; ".join(x for x in (r.pop("_flag", None), detect_compromise(r, series)) if x) or None
        r["compromise_suspect"], r["compromise_reason"] = bool(why), why
        r.pop("_series", None)
        if why and not (prior.get(r["source_id"]) or {}).get("compromise_suspect"):
            flagged.append(r)
    if dry:
        return rows
    ensure_schema(cur)
    write_rows(cur, rows)
    for r in flagged:
        try:
            from nova_buick8_log import log_unexplained
            log_unexplained("source_behaviour_change", r["source_id"],
                            f"{r['source_id']} is behaving unlike itself: {r['compromise_reason']}",
                            evidence={"reliability_before": (prior.get(r["source_id"]) or {}).get("reliability"),
                                      "reason": r["compromise_reason"]},
                            occurrence_key=f"{r['source_id']}:{datetime.now():%Y-%m-%d}", source="nova_cardinal",
                            cur=cur)
        except Exception as e:  # noqa: BLE001
            log(f"buick8 log failed for {r['source_id']}: {e}")
    log(f"scored {len(rows)} sources; {len(flagged)} newly suspect")
    return rows


def show(as_json: bool = False) -> int:
    led = load_ledger()
    if as_json:
        print(json.dumps(led, default=str, indent=1))
        return 0
    for sid, r in sorted(led.items(), key=lambda kv: (kv[1]["reliability"], kv[0])):
        flag = f"  SUSPECT: {r['compromise_reason']}" if r["compromise_suspect"] else ""
        hr = f"{r['hit_rate']:.0%}" if r["hit_rate"] is not None else "-"
        print(f"{r['reliability']}  {sid:<48} n={r['n']:<5} hit={hr:<5} truth={r['truth_kind']}{flag}")
    return 0


def selftest() -> int:
    assert reliability(5, 5, "labelled") == "F"
    assert reliability(200, 196, "labelled") == "A"
    assert reliability(100, 30, "labelled") == "E" and reliability(100, 50, "labelled") == "D"
    assert reliability(500, 495, "fusion-agreement") == "B"
    assert reliability(0, 0, "corroboration", corroborated=4) == "C"
    led = {"camera:front_door": {"reliability": "C"}, "scanner:Burbank PD": {"reliability": "C"},
           "detector:x": {"reliability": "A", "compromise_suspect": True}}
    g = grade({"sources": [{"id": "camera:front_door"}, {"id": "scanner:Burbank PD"}]}, ledger=led)
    assert g["code"] == "C1", g
    g2 = grade({"sources": [{"id": "camera:front_door"}, {"id": "camera:front_yard"}]}, ledger=led)
    assert g2["credibility"] == 3, g2
    g3 = grade({"sources": [{"id": "detector:x"}]}, ledger=led)
    assert g3["reliability"] == "F" and g3["credibility"] == 6, g3
    g4 = grade({"sources": [{"id": "nova:reasoning"}]}, ledger=led)
    assert g4["credibility"] == 6
    assert estimative(0.97) == "almost certainly" and estimative(0.5) == "roughly even chance"
    assert volume_anomaly([10] * 14, 0) and not volume_anomaly([10] * 14, 11)
    assert accuracy_drift(90, 100, 2, 10) and not accuracy_drift(90, 100, 9, 10)
    m0 = {i: (i % 7 == 0) for i in range(600)}
    hb = mmwave_health({"a": m0, "b": dict(m0), "c": {i: False for i in range(600)}})
    assert "duplicate" in hb["b"] and "stuck" in hb["c"] and "a" not in hb, hb
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--score", action="store_true", help="harvest track records and write source_ledger")
    ap.add_argument("--dry-run", action="store_true", help="with --score: print, write nothing")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--grade", help="item JSON to grade against the live ledger")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.score:
        rows = score_all(dry=a.dry_run)
        if a.dry_run:
            for r in sorted(rows, key=lambda r: (r["reliability"], r["source_id"])):
                print(r["reliability"], r["source_id"], r["n"], r.get("hit_rate"), r["truth_kind"],
                      r.get("compromise_reason") or "")
        return 0
    if a.grade:
        print(json.dumps(grade(json.loads(a.grade), ledger=load_ledger()), indent=1, default=str))
        return 0
    return show(a.json)


if __name__ == "__main__":
    sys.exit(main())
