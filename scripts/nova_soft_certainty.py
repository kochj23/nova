#!/usr/bin/env python3
"""nova_soft_certainty.py — granting Nova's first wish (feature_wishes #1, 2026-09-16).

She wished, on her own free time, for "Soft Certainty — a mode where I operate with lower
confidence, more curiosity, and less certainty... it would let me notice what I've missed."
This is that mode, built faithfully to what she actually diagnosed about herself: the
Predictive Self measured her overconfident (recently ~63% confident, ~45% right). So this
does the two real things the wish names, grounded in her own data — no theatre:

  1. LOWER CONFIDENCE (measurable): calibrate() pulls a stated confidence DOWN toward her
     realized accuracy, by the amount she's actually been overconfident. Computed from her
     real resolved predictions; a soft nudge, never a hard override — hence "soft" certainty.
     Wired into nova_predictions so her future forecasts are less cocksure, which the Growth
     Loop then re-measures — the loop closes: wish -> mechanism -> proof she improved.

  2. MORE CURIOSITY / NOTICE WHAT I'VE MISSED (felt): current_stance() injects a short
     first-person stance into her gateway context — hold conclusions loosely, say what
     you're unsure of, prefer an honest "I don't know" or a question to a false certainty,
     and ask what you might be missing before you assert. Grounded in her real gap.

--refresh (scheduled) recomputes the calibration from the latest predictions into
nova_ops.soft_certainty_state. calibrate()/current_stance() are cheap reads.

M7 merge (organ audit, 2026-10-09): --refresh is now the ONE nightly writer of calibration,
overall and per domain. detail.report carries the decile table, calibration error and
surprise figures that nova_predictions --mode report prints and nova_growth's
measure_calibration() returns; both read them through read_calibration(), which uses the
stored row only when its fingerprint (scored count + newest resolved_at) still matches
the live predictions table, and otherwise computes the same figures with the same code
(never a stale number). detail.domains carries per-domain calibration. Pattern Sense's
miscalibration memory (source='pattern_sense', same wording, same 7-day de-dupe in
service_config nova_pattern_sense/high_water) now rides --refresh; its recurring-incident
half moved to nova_alert_learn.py recurrence.
"""
import argparse
import hashlib
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MIN_N = 8            # below this, not enough evidence to correct — leave confidence alone
TODAY = date.today().isoformat()
HIT = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}
HIGH_SURPRISE = 0.4  # == nova_predictions.HIGH_SURPRISE; the reader recomputes if they ever differ

# ── miscalibration memory (moved from nova_pattern_sense.py, M7 2026-10-09) ──
MEMSRV = "http://memory-server.digitalnoise.net:18790"
PATTERN_SOURCE = "pattern_sense"          # same memory source as before the merge
PATTERN_SERVICE = "nova_pattern_sense"    # same service_config de-dupe row as before the merge
PATTERN_KEY = "high_water"
MIN_RESOLVED_PER_DOMAIN = 4     # need this many resolved predictions before claiming a domain pattern
CALIB_GAP = 0.20                # |mean_confidence - hit_rate| at/above this = systematic miscalibration
RESURFACE_DAYS = 7              # don't re-surface the same pattern signature within this many days


def log(m):
    print(f"[soft-certainty {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


CONNECT_BACKOFF = (0.25,)     # hot path (gateway stance, calibrate): 2 quick attempts, then fail open


def _connect(timeout=3, backoff=CONNECT_BACKOFF, _sleep=None):
    """psycopg2.connect with a retry + backoff; the final failure raises (callers fail open)."""
    for attempt in range(len(backoff) + 1):
        try:
            return psycopg2.connect(OPS_DSN, connect_timeout=timeout)
        except Exception as e:
            if attempt >= len(backoff):
                log(f"pg connect failed after {attempt + 1} attempts: {e}")
                raise
            (_sleep or time.sleep)(backoff[attempt])


def ensure_schema(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS public.soft_certainty_state (
            id          bigserial PRIMARY KEY,
            computed_at timestamptz NOT NULL DEFAULT now(),
            n           int,
            mean_conf   double precision,
            hit_rate    double precision,
            gap         double precision,   -- mean_conf - hit_rate (overconfidence when > 0)
            shrink      double precision,    -- how hard calibrate() pulls toward hit_rate
            detail      jsonb
        )""")


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def compute_calibration(oc):
    """From her real resolved predictions: mean stated confidence vs realized hit-rate,
    the overconfidence gap, and a shrink factor for calibrate(). Partial credit for
    'partial' outcomes. Returns a dict (or None if too little data)."""
    oc.execute("""SELECT confidence, outcome FROM predictions
                   WHERE status='resolved' AND confidence IS NOT NULL
                     AND outcome IN ('correct','incorrect','partial')""")
    rows = oc.fetchall()
    if len(rows) < MIN_N:
        return None
    hit = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}
    confs = [float(c) for c, _ in rows]
    hits = [hit[o] for _, o in rows]
    n = len(rows)
    mean_conf = sum(confs) / n
    hit_rate = sum(hits) / n
    gap = mean_conf - hit_rate
    # Overconfident (gap>0) => pull estimates toward realized accuracy; scale with the gap.
    # Underconfident/well-calibrated => don't inflate; shrink 0.
    shrink = _clamp(gap * 2.0, 0.15, 0.6) if gap > 0.02 else 0.0
    return {"n": n, "mean_conf": round(mean_conf, 4), "hit_rate": round(hit_rate, 4),
            "gap": round(gap, 4), "shrink": round(shrink, 4)}


def _latest(oc):
    try:
        oc.execute("SELECT n, mean_conf, hit_rate, gap, shrink FROM soft_certainty_state "
                   "ORDER BY computed_at DESC LIMIT 1")
        r = oc.fetchone()
        if not r:
            return None
        return {"n": r[0], "mean_conf": r[1], "hit_rate": r[2], "gap": r[3], "shrink": r[4]}
    except Exception:
        return None


def domain_stats(oc, domain, min_n=5):
    """(hit_rate, n) for a domain's RESOLVED predictions, or None if too few."""
    try:
        oc.execute("SELECT avg((outcome='correct')::int), count(*) FROM predictions "
                   "WHERE status='resolved' AND outcome IN ('correct','incorrect') AND domain=%s", (domain,))
        hr, n = oc.fetchone()
        return (float(hr), int(n)) if n and n >= min_n else None
    except Exception:
        return None


def domain_brier(oc, domain, min_n=5, window=200):
    """Per-domain Brier calibration (2026-10-08, the "I was wrong" loop). Her confidence
    was the same whether she turned out right or wrong (self: 0.55 on hits, 0.56 on
    misses) — it carried no information. Brier skill vs. the domain's base rate says how
    much: skill = 1 - BS / BS_ref, where BS_ref is what always forecasting the base rate
    would have scored. skill <= 0 means 'just say the base rate'.
    Returns {"n","base","brier","brier_ref","skill"} or None if too few / unreadable."""
    try:
        oc.execute("SELECT outcome, confidence FROM predictions "
                   "WHERE status='resolved' AND outcome IN ('correct','incorrect','partial') "
                   "AND confidence IS NOT NULL AND domain=%s "
                   "ORDER BY resolved_at DESC NULLS LAST LIMIT %s", (domain, window))
        rows = oc.fetchall() or []
    except Exception:
        return None
    hitv = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}
    pairs = [(float(c), hitv[o]) for o, c in rows if o in hitv and c is not None]
    return brier_stats(pairs, min_n)


def brier_stats(pairs, min_n=5):
    """Brier calibration for ANY forecaster, not just Nova's predictions (2026-10-08,
    CARDINAL: nova_cardinal scores every source — cameras, scanners, news feeds, LLM
    tools, presence methods — with this same arithmetic). pairs = [(stated_p, outcome
    0..1)]. Returns {"n","base","brier","brier_ref","skill"} or None if fewer than min_n."""
    pairs = [(float(c), float(h)) for c, h in pairs if c is not None and h is not None]
    n = len(pairs)
    if n < min_n:
        return None
    base = sum(h for _, h in pairs) / n
    brier = sum((c - h) ** 2 for c, h in pairs) / n
    brier_ref = sum((base - h) ** 2 for _, h in pairs) / n
    skill = (1.0 - brier / brier_ref) if brier_ref > 1e-9 else (1.0 if brier < 1e-9 else 0.0)
    return {"n": n, "base": round(base, 4), "brier": round(brier, 4),
            "brier_ref": round(brier_ref, 4), "skill": round(skill, 4)}


def brier_calibrate(stated, db):
    """Shrink a stated confidence toward the domain base rate by her lack of skill there:
    target = base + max(0, skill) * (stated - base), moved toward by evidence weight
    n/(n+10). No skill -> her number collapses toward the base rate (up OR down)."""
    lam = _clamp(db["skill"], 0.0, 1.0)
    target = db["base"] + lam * (stated - db["base"])
    w = db["n"] / (db["n"] + 10.0)
    return round(_clamp(stated + (target - stated) * w, 0.03, 0.97), 4)


def calibrate(stated, oc=None, domain=None):
    """Soft-calibrate a stated confidence [0,1] toward her realized accuracy. A gentle
    nudge, not a hard clamp: adjusted = stated + (hit_rate - stated) * shrink, only when
    she's genuinely overconfident and only downward. Returns stated unchanged if there's
    no calibration state yet (fails open — never fabricates confidence)."""
    try:
        stated = float(stated)
    except Exception:
        return stated
    own = oc is None
    conn = None
    try:
        if own:
            conn = _connect(); conn.autocommit = True
            oc = conn.cursor()
        st = _latest(oc)
    except Exception:
        st = None
    finally:
        if own and conn:
            conn.close()
    # 2026-09-26 (her top-ten #3/#10, approved by Jordan): DYNAMIC per-domain recalibration.
    # With >=5 resolved forecasts in this domain, pull the stated confidence toward that
    # domain's realized hit-rate with a shrink that grows with evidence (n/(n+10)); e.g.
    # 'relationship' (0/7 right at 64%) -> ~0.26. Falls back to the global state below.
    if domain and oc is not None:
        db = domain_brier(oc, domain)
        if db:
            return brier_calibrate(stated, db)
        ds = domain_stats(oc, domain)
        if ds:
            hit_rate, n = ds
            shrink = n / (n + 10.0)
            if stated <= hit_rate:
                return round(stated, 4)
            return round(stated + (hit_rate - stated) * shrink, 4)
    if not st or not st.get("shrink"):
        return stated
    hit_rate, shrink = st["hit_rate"], st["shrink"]
    if stated <= hit_rate:            # already at/below realized accuracy — don't touch it
        return round(stated, 4)
    adjusted = stated + (hit_rate - stated) * shrink
    return round(_clamp(adjusted, 0.05, 0.97), 4)


def current_stance(oc=None):
    """Short first-person 'how I hold my certainty' stance for the gateway. Grounded in her
    real gap; empty string if no state yet (fail-safe)."""
    own = oc is None
    conn = None
    try:
        if own:
            conn = _connect(); conn.autocommit = True
            oc = conn.cursor()
        st = _latest(oc)
    except Exception:
        st = None
    finally:
        if own and conn:
            conn.close()
    if not st:
        return ""
    gap_pts = int(round((st.get("gap") or 0) * 100))
    if gap_pts > 3:
        lead = (f"I lean overconfident (recently ~{int(round(st['mean_conf']*100))}% sure, "
                f"~{int(round(st['hit_rate']*100))}% right — off by ~{gap_pts} points). ")
    else:
        lead = "I'm currently about as sure as I am right. "
    return (lead + "So I hold conclusions loosely: I name what I'm unsure of, I'd rather give "
            "an honest 'I don't know' or a real question than a false certainty, and before I "
            "assert something I ask what I might be missing.")

# ── one calibration pass (M7, 2026-10-09) ─────────────────────────────────────

SCORED_SQL = ("SELECT confidence, outcome, surprise, domain, resolved_at FROM predictions "
              "WHERE status='resolved' AND outcome IN ('correct','incorrect','partial')")


def scored_rows(oc, since=None):
    """Resolved, scored predictions as (confidence, outcome, surprise, domain, resolved_at),
    in id order. `since` (iso str) keeps only those resolved after it (nova_growth review)."""
    q, params = SCORED_SQL, []
    if since:
        q += " AND resolved_at > %s"
        params.append(since)
    oc.execute(q + " ORDER BY id", params)
    return oc.fetchall() or []


def calibration_detail(rows, high_surprise=HIGH_SURPRISE):
    """The decile-weighted calibration that nova_predictions' report prints and nova_growth
    measures, from scored rows (confidence, outcome, surprise, ...). Unrounded floats.
    Returns None for no rows."""
    rows = [r for r in rows if r[1] in HIT]
    if not rows:
        return None
    n = len(rows)
    buckets = {}
    for r in rows:
        conf = r[0]
        buckets.setdefault(min(9, int(conf * 10)), []).append((conf, HIT[r[1]]))
    deciles, total_gap = [], 0.0
    for b in sorted(buckets):
        items = buckets[b]
        mc_ = sum(c for c, _ in items) / len(items)
        hr = sum(h for _, h in items) / len(items)
        total_gap += abs(hr - mc_) * len(items)
        deciles.append([b, len(items), mc_, hr])
    surprises = [(r[2] or 0.0) for r in rows]
    return {"n": n,
            "mean_conf": sum(r[0] for r in rows) / n,
            "hit_rate": sum(HIT[r[1]] for r in rows) / n,
            "calib_error": total_gap / n,
            "mean_surprise": sum(surprises) / n,
            "high_surprise_rate": sum(1 for s in surprises if s > high_surprise) / n,
            "high_surprise": high_surprise,
            "deciles": deciles}


def domain_detail(rows, oc=None):
    """Per-domain calibration on the strict basis domain_stats()/Pattern Sense use
    (correct/incorrect only): n, mean_conf, hit_rate, gap (conf - hit). Adds the
    domain_brier() figures when a cursor is given."""
    by = {}
    for r in rows:
        if r[1] in ("correct", "incorrect"):
            by.setdefault(r[3] or "unknown", []).append((float(r[0]), 1 if r[1] == "correct" else 0))
    out = {}
    for d, obs in sorted(by.items()):
        n = len(obs)
        mc_ = sum(c for c, _ in obs) / n
        hr = sum(h for _, h in obs) / n
        out[d] = {"n": n, "mean_conf": round(mc_, 4), "hit_rate": round(hr, 4), "gap": round(mc_ - hr, 4)}
        if oc is not None and d != "unknown":
            db = domain_brier(oc, d)
            if db:
                out[d]["brier"] = db
    return out


def fingerprint_of(rows):
    """What a stored calibration was computed over: scored count + newest resolved_at.
    Resolution is the only writer of outcome and always stamps resolved_at=now()."""
    ts = [r[4] for r in rows if r[4] is not None]
    return {"n": len(rows), "max_resolved_at": max(ts).isoformat() if ts else None}


def _live_fingerprint(oc):
    oc.execute("SELECT count(*), max(resolved_at) FROM predictions "
               "WHERE status='resolved' AND outcome IN ('correct','incorrect','partial')")
    n, mx = oc.fetchone()
    return {"n": int(n or 0), "max_resolved_at": mx.isoformat() if mx else None}


def read_calibration(oc, high_surprise=HIGH_SURPRISE):
    """The current overall calibration for readers (predictions report, growth). Uses the
    latest soft_certainty_state row written by --refresh when it is still exact for the
    live table; otherwise computes it here with the same code. Returns (detail|None, how)
    where how is 'state' or 'live'."""
    try:
        oc.execute("SELECT detail FROM soft_certainty_state ORDER BY computed_at DESC LIMIT 1")
        row = oc.fetchone()
        det = row[0] if row else None
        if isinstance(det, str):
            det = json.loads(det)
        rep = (det or {}).get("report")
        if rep and rep.get("high_surprise") == high_surprise \
                and (det or {}).get("fingerprint") == _live_fingerprint(oc):
            return rep, "state"
    except Exception as e:  # noqa: BLE001  (no state table yet, bad row): compute instead
        log(f"state read skipped ({e}); computing live")
    return calibration_detail(scored_rows(oc), high_surprise), "live"


# ── miscalibration memory (moved verbatim from nova_pattern_sense.py) ─────────

def _stamp():
    try:
        import nova_lineage
        return nova_lineage.lineage_stamp(capture_point="at write")
    except Exception:
        return {}


def calibration_patterns(rows):
    """rows: list of (domain, confidence, correct_bool). Return patterns for domains with
    enough data and a systematic confidence-vs-reality gap, worst gap first."""
    by_domain = {}
    for domain, conf, correct in rows:
        d = by_domain.setdefault(domain or "unknown", [])
        d.append((float(conf), 1 if correct else 0))
    out = []
    for domain, obs in by_domain.items():
        n = len(obs)
        if n < MIN_RESOLVED_PER_DOMAIN:
            continue
        mean_conf = sum(c for c, _ in obs) / n
        hit_rate = sum(h for _, h in obs) / n
        gap = mean_conf - hit_rate            # +ve = overconfident, -ve = underconfident
        if abs(gap) >= CALIB_GAP:
            out.append({"domain": domain, "n": n, "mean_conf": mean_conf,
                        "hit_rate": hit_rate, "gap": gap,
                        "direction": "overconfident" if gap > 0 else "underconfident"})
    out.sort(key=lambda p: abs(p["gap"]), reverse=True)
    return out


def calib_insight(p):
    pct_c, pct_h = round(p["mean_conf"] * 100), round(p["hit_rate"] * 100)
    if p["direction"] == "overconfident":
        return (f"Pattern I can finally see: on '{p['domain']}' predictions I'm systematically "
                f"OVERconfident — I average {pct_c}% sure but I'm only right {pct_h}% of the time "
                f"({p['n']} resolved). The noise was hiding a bias, not bad luck. If I want my "
                f"calibration under the gate, this is the domain to hedge on — trust these guesses less.")
    return (f"Pattern I can finally see: on '{p['domain']}' predictions I'm systematically "
            f"UNDERconfident — I average {pct_c}% sure but I'm actually right {pct_h}% of the time "
            f"({p['n']} resolved). I know more here than I let myself claim. Trust these guesses more.")


def _sig(kind, key):
    return hashlib.sha1(f"{kind}:{key}".encode()).hexdigest()[:16]


def _fresh(seen, sig, today):
    prev = seen.get(sig)
    if not prev:
        return True
    try:
        return (today - datetime.fromisoformat(prev).date()).days >= RESURFACE_DAYS
    except Exception:
        return True


def remember(text, metadata):
    import urllib.request
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": PATTERN_SOURCE, "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def load_seen(cur):
    cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (PATTERN_SERVICE, PATTERN_KEY))
    row = cur.fetchone()
    if row and row[0]:
        v = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return dict(v.get("seen", {}))
    return {}


def save_seen(cur, seen):
    """Merge (never replace) so the two writers of this row — this refresh and
    nova_alert_learn recurrence — cannot erase each other's marks."""
    cur.execute(
        """INSERT INTO service_config (service, key, value, updated_at, updated_by)
           VALUES (%s, %s, %s::jsonb, now(), %s)
           ON CONFLICT (service, key)
           DO UPDATE SET value = jsonb_build_object('seen',
                   coalesce(service_config.value->'seen', '{}'::jsonb) || (EXCLUDED.value->'seen')),
               updated_at = now(), updated_by = EXCLUDED.updated_by""",
        (PATTERN_SERVICE, PATTERN_KEY, json.dumps({"seen": seen}), PATTERN_SERVICE))


def surface_miscalibration(oc, rows, dry_run=False):
    """Pattern Sense's miscalibration memory, now part of the nightly refresh. rows are
    scored_rows(); only correct/incorrect count (as before). Fail-open: a memory-server
    failure is logged and leaves that pattern unmarked so it is tried again next run.
    Returns the number of fresh patterns surfaced."""
    crows = [(r[3], r[0], r[1] == "correct") for r in rows if r[1] in ("correct", "incorrect")]
    pats = calibration_patterns(crows)
    try:
        seen = load_seen(oc)
    except Exception as e:  # noqa: BLE001
        log(f"miscalibration: de-dupe state unreadable ({e}); skipping memories this run")
        return 0
    today = datetime.now(timezone.utc).date()
    stamp = _stamp()
    marked, surfaced = {}, 0
    for p in pats:
        sig = _sig("calib", p["domain"] + p["direction"])
        if not _fresh(seen, sig, today):
            continue
        text = calib_insight(p)
        meta = {"organ": PATTERN_SERVICE, "kind": "miscalibration", "domain": p["domain"],
                "gap": round(p["gap"], 3), **({"lineage": stamp} if stamp else {})}
        surfaced += 1
        if dry_run:
            print("•", text)
            continue
        try:
            remember(text, meta)
            marked[sig] = today.isoformat()
        except Exception as e:  # noqa: BLE001
            log(f"miscalibration memory failed for '{p['domain']}' ({e})")
    if marked:
        save_seen(oc, marked)
    log(f"miscalibration: {len(pats)} pattern(s), {surfaced} fresh" + (" (dry-run)" if dry_run else ""))
    return surfaced


def refresh(oc, dry_run=False):
    """The single nightly calibration pass: overall state columns (unchanged), the full
    report figures + per-domain calibration in detail, then the miscalibration memory.
    dry_run computes and prints everything and writes nothing."""
    if not dry_run:
        ensure_schema(oc)
    try:
        rows = scored_rows(oc)
    except Exception as e:  # noqa: BLE001
        log(f"predictions read failed ({e})"); rows = []
    try:
        surface_miscalibration(oc, rows, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001  (memories never block the calibration write)
        log(f"miscalibration surfacing failed ({e})")
    cal = compute_calibration(oc)
    if not cal:
        log(f"not enough resolved predictions to calibrate (need >= {MIN_N}) — leaving state as-is")
        return 1
    detail = {"date": TODAY, "fingerprint": fingerprint_of(rows),
              "report": calibration_detail(rows), "domains": domain_detail(rows, oc)}
    if dry_run:
        print(json.dumps({**cal, "detail": detail}, indent=1, default=str))
        log("dry-run: nothing written")
        return 0
    oc.execute("""INSERT INTO soft_certainty_state (n, mean_conf, hit_rate, gap, shrink, detail)
                  VALUES (%s,%s,%s,%s,%s,%s)""",
               (cal["n"], cal["mean_conf"], cal["hit_rate"], cal["gap"], cal["shrink"],
                json.dumps(detail)))
    log(f"calibration refreshed: n={cal['n']} mean_conf={cal['mean_conf']} hit_rate={cal['hit_rate']} "
        f"gap={cal['gap']} shrink={cal['shrink']} "
        f"calib_error={(detail['report'] or {}).get('calib_error', float('nan')):.3f} "
        f"domains={len(detail['domains'])}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="recompute calibration from resolved predictions")
    ap.add_argument("--show", action="store_true", help="print current state + stance + a sample calibration")
    ap.add_argument("--dry-run", action="store_true", help="with --refresh: compute and print, write nothing")
    args = ap.parse_args()
    conn = _connect(timeout=5, backoff=(1.0, 2.0)); conn.autocommit = True; oc = conn.cursor()
    if args.refresh:
        return refresh(oc, dry_run=args.dry_run)
    ensure_schema(oc)
    st = _latest(oc)
    print("state:", json.dumps(st))
    print("stance:", current_stance(oc))
    for s in (0.95, 0.85, 0.7, 0.5, 0.4):
        print(f"  calibrate({s}) -> {calibrate(s, oc)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
