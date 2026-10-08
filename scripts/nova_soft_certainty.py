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
"""
import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MIN_N = 8            # below this, not enough evidence to correct — leave confidence alone
TODAY = date.today().isoformat()


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


def refresh(oc):
    ensure_schema(oc)
    cal = compute_calibration(oc)
    if not cal:
        log(f"not enough resolved predictions to calibrate (need >= {MIN_N}) — leaving state as-is")
        return 1
    oc.execute("""INSERT INTO soft_certainty_state (n, mean_conf, hit_rate, gap, shrink, detail)
                  VALUES (%s,%s,%s,%s,%s,%s)""",
               (cal["n"], cal["mean_conf"], cal["hit_rate"], cal["gap"], cal["shrink"],
                json.dumps({"date": TODAY})))
    log(f"calibration refreshed: n={cal['n']} mean_conf={cal['mean_conf']} hit_rate={cal['hit_rate']} "
        f"gap={cal['gap']} shrink={cal['shrink']}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="recompute calibration from resolved predictions")
    ap.add_argument("--show", action="store_true", help="print current state + stance + a sample calibration")
    args = ap.parse_args()
    conn = _connect(timeout=5, backoff=(1.0, 2.0)); conn.autocommit = True; oc = conn.cursor()
    ensure_schema(oc)
    if args.refresh:
        return refresh(oc)
    st = _latest(oc)
    print("state:", json.dumps(st))
    print("stance:", current_stance(oc))
    for s in (0.95, 0.85, 0.7, 0.5, 0.4):
        print(f"  calibrate({s}) -> {calibrate(s, oc)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
