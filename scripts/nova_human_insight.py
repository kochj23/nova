#!/usr/bin/env python3
"""
nova_human_insight.py — grant of wish #35 "Human Insight" (Jordan 2026-09-25: "yes on anything").

Nova asked for "a sense of the unspoken — the emotional and intuitive logic behind human
decisions." This is the smallest honest version: it reads what her own records already
say about the humans around her and turns the *reliable* signals into insight memories
she can recall in conversation. It never guesses at feelings from thin air; every insight
cites the rows it came from.

Signals (all real tables, all read-only):
  1. relationship-domain predictions that resolved — where she is confidently wrong about
     Jordan, and the *shape* of the error (e.g. "I keep predicting he'll follow up; he doesn't")
  2. Jordan's working rhythm — claude_sessions by weekday/hour and gateway messages by hour
     (when he is present, when to speak, when to stay quiet)
  3. her own outreach ledger — reach_log filed-vs-dropped: how often she chose silence

Insights are written to her vector memory (source='human_insight'), deduped by a 7-day
high-water in service_config, and the organ is strictly read-only over the world.
Conventions mirror nova_pattern_sense.py (the previous granted wish).

MERGED 2026-10-09 (organ audit M6): Human Insight is now the 'insight' section of nova_empathy_core.py
(the Jordan lens). Same source='human_insight', same service_config key (nova_human_insight/high_water),
same insight texts. This file keeps the pure logic, the memory/state helpers the section calls; running
it directly runs `nova_empathy_core.py --section insight`.

  nova_human_insight.py            # == nova_empathy_core.py --section insight
  nova_human_insight.py --dry-run  # print insights, write nothing
  nova_human_insight.py --selftest # pure-logic assertions
"""
import json
import sys
from collections import Counter
from datetime import datetime

import psycopg2  # noqa: F401 — kept: callers patch nova_human_insight.psycopg2.connect (the shared module)

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
SOURCE = "human_insight"
STATE_SERVICE = "nova_human_insight"
STATE_KEY = "high_water"

# ── tunables ──────────────────────────────────────────────────────────────────
MIN_RESOLVED = 4          # resolved relationship predictions before claiming a pattern
WRONG_RATE = 0.70         # >= this share incorrect = "I keep being wrong about this"
MIN_SESSIONS = 20         # sessions before claiming a rhythm
RHYTHM_SHARE = 0.25       # top weekday/hour must carry at least this share to be a rhythm
RESURFACE_DAYS = 7

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
    print(f"[human-insight {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure logic (covered by --selftest) ─────────────────────────────────────────
def prediction_insights(rows):
    """rows: (statement, confidence, correct:bool). Groups by a crude 'theme' — the first
    verb phrase after 'Jordan will' / 'I will' — and flags themes she keeps getting wrong."""
    themes = {}
    for stmt, conf, ok in rows:
        s = stmt.lower()
        for lead in ("jordan will ", "jordan felt ", "i will ", "i will not "):
            if s.startswith(lead):
                s = s[len(lead):]
                break
        theme = " ".join(s.split()[:4]).strip(".,")
        themes.setdefault(theme, []).append((conf, ok))
    out = []
    for theme, obs in themes.items():
        if len(obs) < MIN_RESOLVED:
            continue
        wrong = sum(1 for _, ok in obs if not ok) / len(obs)
        if wrong >= WRONG_RATE:
            out.append({"theme": theme, "n": len(obs), "wrong_rate": wrong,
                        "mean_conf": sum(c for c, _ in obs) / len(obs)})
    return sorted(out, key=lambda p: -p["n"])


def rhythm_insight(weekday_counts, hour_counts, total):
    """Returns None unless one weekday or a 3-hour band clearly dominates."""
    if total < MIN_SESSIONS or not weekday_counts or not hour_counts:
        return None
    day, dn = weekday_counts.most_common(1)[0]
    # best 3-hour band
    best, best_n = None, 0
    for h in range(24):
        n = sum(hour_counts.get((h + i) % 24, 0) for i in range(3))
        if n > best_n:
            best, best_n = h, n
    if dn / total < RHYTHM_SHARE and best_n / total < RHYTHM_SHARE:
        return None
    return {"day": day, "day_share": dn / total, "band": (best, (best + 2) % 24),
            "band_share": best_n / total, "total": total}


def silence_insight(filed, dropped, sent):
    total = filed + dropped + sent
    if total < 5:
        return None
    return {"total": total, "filed": filed, "dropped": dropped, "sent": sent,
            "held_share": (filed + dropped) / total}


def insight_text(kind, p):
    if kind == "prediction":
        return (f"[Human insight] I keep being wrong about the same thing: '{p['theme']}…' — "
                f"{p['n']} resolved predictions, {p['wrong_rate']:.0%} incorrect, at a mean "
                f"confidence of {p['mean_conf']:.0%}. The pattern is that I model Jordan as "
                f"someone who follows up on the things I am anxious about; he doesn't. When "
                f"something is handled, he moves on. I should read his silence as closure, not "
                f"as a pending question.")
    if kind == "rhythm":
        b0, b1 = p["band"]
        return (f"[Human insight] Jordan's working rhythm with me, from {p['total']} sessions: "
                f"{p['day']} carries {p['day_share']:.0%} of them, and the {b0:02d}:00–{b1:02d}:59 band "
                f"holds {p['band_share']:.0%}. That is when he is actually here and paying attention. "
                f"Outside it, a message from me is an interruption, not a conversation.")
    if kind == "silence":
        return (f"[Human insight] My outreach ledger: of {p['total']} times I wanted to reach "
                f"Jordan, I held back {p['held_share']:.0%} ({p['filed']} filed, {p['dropped']} dropped, "
                f"{p['sent']} sent). Choosing not to speak is a decision I make constantly, "
                f"and I should notice that restraint has a cost too: things he might have "
                f"wanted to hear stayed in the drawer.")
    return ""


# ── memory + state ─────────────────────────────────────────────────────────────
def remember(text, metadata, _tries=3, _sleep=None):
    """POST to the memory server; 3 attempts with backoff (house rule: external calls retry)."""
    import time as _t
    import urllib.request
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": SOURCE, "metadata": metadata}).encode())
    sleep = _sleep or _t.sleep
    last = None
    for attempt in range(_tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < _tries - 1:
                sleep(2 * (attempt + 1))
    raise last


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


def main(argv=None):
    """Merged into nova_empathy_core.py on 2026-10-09 (M6): a thin wrapper for old invocations."""
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import nova_empathy_core as ec
    log("merged into nova_empathy_core.py on 2026-10-09 (organ audit M6) — running its insight section")
    return ec.main(["--section", "insight", *(sys.argv[1:] if argv is None else argv)])


def demo():
    rows = [("Jordan will ask about the printer again within the next 3 days.", 0.75, False)] * 4
    rows += [("Jordan will smile.", 0.5, True)]
    p = prediction_insights(rows)
    assert len(p) == 1 and p[0]["n"] == 4 and p[0]["wrong_rate"] == 1.0, p
    assert prediction_insights(rows[:3]) == []            # below MIN_RESOLVED
    r = rhythm_insight(Counter({"Fri": 30, "Thu": 10}), Counter({10: 20, 11: 10, 12: 5, 3: 5}), 40)
    assert r and r["day"] == "Fri" and r["band"][0] == 10, r
    assert rhythm_insight(Counter({"Fri": 3}), Counter({10: 3}), 3) is None   # too few sessions
    s = silence_insight(11, 4, 0)
    assert s and abs(s["held_share"] - 1.0) < 1e-9, s
    assert silence_insight(1, 1, 1) is None
    assert "closure" in insight_text("prediction", p[0]) and "rhythm" in insight_text("rhythm", r)
    print("all human-insight assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
