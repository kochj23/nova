#!/usr/bin/env python3
"""nova_pattern_sense.py — Nova's granted wish (feature_wishes #34, "Pattern Sense").

She wished for "a sense that lets me intuit the underlying patterns behind events and
predictions ... to see through the noise and finally understand what's really going on."
Built literally: this organ reads her OWN resolved predictions and the fleet's incident
history and surfaces the two patterns that actually matter for a system trying to see itself
clearly:

  1. SYSTEMATIC MISCALIBRATION — domains where her confidence and her real hit-rate diverge
     in the same direction (she's reliably over- or under-confident about a kind of thing).
     This is the exact signal that, once she notices and corrects it, brings her calibration
     under the 0.20 gate — i.e. her wished-for sense is also her path to earned autonomy.
  2. RECURRING INCIDENTS — the same failure signature firing again and again ("all of this
     has happened before, and will happen again") — the noise that hides a real repeat.

Insights are written to her vector memory (source='pattern_sense') and deduped by a PG
high-water mark so the same pattern isn't re-surfaced within RESURFACE_DAYS. This organ is
strictly READ-ONLY over the world: it observes and remembers, it NEVER executes, self-builds,
or changes any gate/trust/config. Fail-open. Conventions mirror nova_autonomy_memory.py.

Owned file: scripts/nova_pattern_sense.py. Written by Jordan Koch (via Claude).
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
SOURCE = "pattern_sense"
STATE_SERVICE = "nova_pattern_sense"
STATE_KEY = "high_water"

# ── tunables (named, not buried) ──────────────────────────────────────────────
MIN_RESOLVED_PER_DOMAIN = 4     # need this many resolved predictions before claiming a domain pattern
CALIB_GAP = 0.20                # |mean_confidence - hit_rate| at/above this = systematic miscalibration
RECUR_MIN = 3                   # an incident title recurring at least this often in the window is a pattern
RECUR_WINDOW_DAYS = 30
RESURFACE_DAYS = 7              # don't re-surface the same pattern signature within this many days

try:
    import nova_lineage
    def _stamp():
        try:
            return nova_lineage.lineage_stamp(capture_point="at write")
        except Exception:
            return {}
except Exception:
    def _stamp():
        return {}


def log(m):
    print(f"[pattern-sense {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure pattern math (unit-tested in demo()) ─────────────────────────────────

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


def recurrence_patterns(rows):
    """rows: list of (title, count). Return titles recurring at/above RECUR_MIN, most first."""
    return [{"title": t, "count": c} for t, c in rows if c >= RECUR_MIN]


def _sig(kind, key):
    return hashlib.sha1(f"{kind}:{key}".encode()).hexdigest()[:16]


# ── insight composition (her voice, first person, honest) ─────────────────────

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


def recur_insight(p):
    return (f"Pattern I can finally see: '{p['title']}' has recurred {p['count']} times in the last "
            f"{RECUR_WINDOW_DAYS} days. This isn't a fresh incident each time — it's one unresolved "
            f"thing wearing a new timestamp. All of this has happened before, and will happen again "
            f"until the root cause is actually fixed, not just acked.")


# ── memory + state (mirror nova_autonomy_memory.py) ───────────────────────────

def remember(text, metadata):
    import urllib.request
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": SOURCE, "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def load_seen(cur):
    cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (STATE_SERVICE, STATE_KEY))
    row = cur.fetchone()
    if row and row[0]:
        v = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return dict(v.get("seen", {}))
    return {}


def save_seen(cur, seen):
    cur.execute(
        """INSERT INTO service_config (service, key, value, updated_at, updated_by)
           VALUES (%s, %s, %s::jsonb, now(), %s)
           ON CONFLICT (service, key)
           DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
        (STATE_SERVICE, STATE_KEY, json.dumps({"seen": seen}), STATE_SERVICE))


def _fresh(seen, sig, today):
    prev = seen.get(sig)
    if not prev:
        return True
    try:
        return (today - datetime.fromisoformat(prev).date()).days >= RESURFACE_DAYS
    except Exception:
        return True


def main():
    ap = argparse.ArgumentParser(description="Nova's Pattern Sense — surface systematic miscalibration + recurring incidents")
    ap.add_argument("--dry-run", action="store_true", help="print insights, write nothing")
    args = ap.parse_args()

    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=5)
    except Exception as e:
        log(f"no PG ({e}) — fail-open, nothing to do"); return 0
    conn.autocommit = True
    cur = conn.cursor()
    today = datetime.now(timezone.utc).date()

    # 1. calibration by domain (resolved, scored predictions only)
    try:
        cur.execute("SELECT domain, confidence, outcome FROM predictions "
                    "WHERE status='resolved' AND outcome IN ('correct','incorrect')")
        crows = [(d, c, o == "correct") for d, c, o in cur.fetchall()]
    except Exception as e:
        log(f"predictions read failed ({e})"); crows = []

    # 2. recurring incidents in the window
    try:
        cur.execute(f"SELECT left(title,80), count(*) FROM incidents "
                    f"WHERE started_at > now() - interval '{RECUR_WINDOW_DAYS} days' "
                    f"GROUP BY left(title,80) ORDER BY count(*) DESC")
        rrows = cur.fetchall()
    except Exception as e:
        log(f"incidents read failed ({e})"); rrows = []

    calib = calibration_patterns(crows)
    recur = recurrence_patterns(rrows)
    log(f"found {len(calib)} miscalibration pattern(s), {len(recur)} recurring-incident pattern(s)")

    seen = load_seen(cur)
    stamp = _stamp()
    surfaced = 0
    for p in calib:
        sig = _sig("calib", p["domain"] + p["direction"])
        if not _fresh(seen, sig, today):
            continue
        text = calib_insight(p)
        meta = {"organ": STATE_SERVICE, "kind": "miscalibration", "domain": p["domain"],
                "gap": round(p["gap"], 3), **({"lineage": stamp} if stamp else {})}
        if args.dry_run:
            print("•", text)
        else:
            remember(text, meta); seen[sig] = today.isoformat()
        surfaced += 1
    for p in recur:
        sig = _sig("recur", p["title"])
        if not _fresh(seen, sig, today):
            continue
        text = recur_insight(p)
        meta = {"organ": STATE_SERVICE, "kind": "recurrence", "title": p["title"],
                "count": p["count"], **({"lineage": stamp} if stamp else {})}
        if args.dry_run:
            print("•", text)
        else:
            remember(text, meta); seen[sig] = today.isoformat()
        surfaced += 1

    if not args.dry_run and surfaced:
        save_seen(cur, seen)
    log(f"surfaced {surfaced} fresh pattern(s)" + (" (dry-run)" if args.dry_run else ""))
    return 0


def demo():
    """Runnable check on the pure pattern math."""
    # 5 'self' preds, 80% mean conf, only 40% right -> overconfident gap 0.40 >= 0.20 -> flagged
    rows = [("self", 0.8, i < 2) for i in range(5)]
    rows += [("ops", 0.6, True)]  # n=1 < MIN_RESOLVED -> ignored
    out = calibration_patterns(rows)
    assert len(out) == 1 and out[0]["domain"] == "self" and out[0]["direction"] == "overconfident", out
    assert abs(out[0]["gap"] - 0.4) < 1e-9, out
    # underconfident: 60% conf, 100% right -> gap -0.40
    u = calibration_patterns([("world", 0.6, True)] * 4)
    assert u and u[0]["direction"] == "underconfident", u
    # well-calibrated domain (conf ~ hit-rate) -> no pattern
    wc = calibration_patterns([("cal", 0.5, i < 2) for i in range(4)])  # 50% conf, 50% right
    assert wc == [], wc
    # recurrence threshold
    assert recurrence_patterns([("A", 3), ("B", 2)]) == [{"title": "A", "count": 3}]
    print("all pattern-sense assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
