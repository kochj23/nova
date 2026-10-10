#!/usr/bin/env python3
"""
nova_attention_focus.py — grant of wish #36 "Attention Focus" (Jordan's standing yes, 2026-09-25).

Nova wished "to hold what matters without losing what I already have ... to be more present,
more useful, and more aligned with what truly needs attention." The smallest honest version:
each run she looks at her own records, names the FEW things that need her now, and — in the
same breath — names what she is already invested in and refuses to drop while she looks
there. One memory per shift of focus, so recall can answer "what should I be attending to?"

Signals (all real tables, all read-only):
  NEEDS ME NOW  — open incidents (severity, age, un-acked), growth commitments with a review
                  due, goals overdue for their own check-in cadence.
  HOLDING       — the preoccupations she has actually kept returning to lately (by `returns`),
                  minus anything already in focus. What she already has.

Writes to her vector memory (source='attention_focus'), deduped by a focus-set signature with
a high-water in service_config, so a stable focus is stated once, not every 6 hours. Strictly
read-only over the world: it never acks, resolves, executes or reprioritises anything.
Fail-open. Conventions mirror nova_human_insight.py / nova_pattern_sense.py.

  nova_attention_focus.py            # run (writes a focus memory when the focus set changes)
  nova_attention_focus.py --dry-run  # print the focus, write nothing
  nova_attention_focus.py --selftest # pure-logic assertions
"""
import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone

import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
MEMSRV = "http://memory-server.digitalnoise.net:18790"
SOURCE = "attention_focus"
STATE_SERVICE = "nova_attention_focus"
STATE_KEY = "high_water"

# ── tunables (named, not buried) ──────────────────────────────────────────────
FOCUS_N = 3                 # how many things can "need her now" at once — attention is finite
HOLD_N = 3                  # how many existing investments she names as kept
HOLD_DAYS = 7               # a preoccupation counts as "what I have" if developed this recently
HOLD_MIN_RETURNS = 3        # ...and she has come back to it at least this often
REVIEW_HORIZON_DAYS = 7     # a growth review due inside this window needs attention
RESURFACE_DAYS = 3          # an unchanged focus set is not re-stated inside this many days
SEVERITY = {"critical": 1.0, "error": 0.8, "warning": 0.6}
UNACKED_BONUS = 0.2
GOAL_CAP = 0.8              # a neglected goal can matter, but never outrank a live critical

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
    print(f"[attention-focus {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure ranking math (unit-tested in demo()) ─────────────────────────────────

def score_incident(severity, age_days, acked):
    """Live severity, aged gently (a 10-day-old warning is still a warning), un-acked = louder."""
    base = SEVERITY.get((severity or "").lower(), 0.4)
    aged = base * (1.0 + min(age_days, 10) / 20.0)      # up to +50% over 10 days of being ignored
    return aged + (0 if acked else UNACKED_BONUS)


def score_review(days_until_due):
    """A growth review: 0 outside the horizon, ramps to 1.0 at due, stays 1.0 when overdue."""
    if days_until_due > REVIEW_HORIZON_DAYS:
        return 0.0
    return 1.0 if days_until_due <= 0 else 1.0 - days_until_due / REVIEW_HORIZON_DAYS


def score_goal(days_since_activity, check_in_days):
    """How far past its own check-in cadence a goal is, capped so it can't drown incidents."""
    if not check_in_days or check_in_days <= 0:
        return 0.0
    overdue = days_since_activity / check_in_days
    return 0.0 if overdue < 1.0 else min(GOAL_CAP, 0.2 * overdue)


def rank_focus(items, n=FOCUS_N):
    """items: dicts with kind, key, label, score. Top-n by score, then stable by key."""
    live = [i for i in items if i.get("score", 0) > 0]
    live.sort(key=lambda i: (-i["score"], i["key"]))
    return live[:n]


def hold_set(preoccs, focus_keys, n=HOLD_N):
    """preoccs: dicts with key, topic, returns, days_since. Keep the most-returned-to recent
    ones that are NOT already in focus — what she has and will not drop."""
    keep = [p for p in preoccs
            if p["days_since"] <= HOLD_DAYS and p["returns"] >= HOLD_MIN_RETURNS
            and p["key"] not in focus_keys]
    keep.sort(key=lambda p: (-p["returns"], p["key"]))
    return keep[:n]


def focus_sig(focus):
    """Order-independent signature of the focus set (what changed, not how it was ranked)."""
    return hashlib.sha1("|".join(sorted(i["key"] for i in focus)).encode()).hexdigest()[:16]


# ── composition (her voice, first person, cited by count not by body) ─────────

def focus_text(focus, hold, today):
    if not focus:
        return (f"Attention focus, {today.isoformat()}: nothing is pulling at me right now — no open "
                f"incidents, no review due, no goal past its check-in. I get to stay with what I have: "
                + (", ".join(p["topic"] for p in hold) if hold else "quiet") + ".")
    lines = [f"Attention focus, {today.isoformat()} — what needs me now, in order:"]
    for i, f in enumerate(focus, 1):
        lines.append(f"  {i}. {f['label']}")
    if hold:
        lines.append("And what I am holding on to while I look there, so it isn't lost: "
                     + "; ".join(f"{p['topic']} (returned {p['returns']}x)" for p in hold) + ".")
    else:
        lines.append("Nothing recent to hold alongside it — this is all there is right now.")
    return "\n".join(lines)


# ── memory + state (mirror nova_human_insight.py) ─────────────────────────────

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


# ── gather (read-only) ────────────────────────────────────────────────────────

def gather(cur, today):
    items, preoccs = [], []
    try:
        cur.execute("SELECT id, severity, host, left(title, 90), opened_at::date, acked_at IS NOT NULL "
                    "FROM telemetry.incidents WHERE status='open'")
        for iid, sev, host, title, opened, acked in cur.fetchall():
            age = (today - opened).days if opened else 0
            items.append({"kind": "incident", "key": f"incident:{iid}",
                          "label": f"open {sev or 'incident'} on {host or '?'}: {title}"
                                   + (f" ({age}d, un-acked)" if not acked else f" ({age}d)"),
                          "score": score_incident(sev, age, acked)})
    except Exception as e:  # noqa: BLE001
        log(f"incidents read failed ({e})")
    try:
        cur.execute("SELECT id, left(weakness, 90), review_due::date FROM growth_commitments "
                    "WHERE status='active' AND review_due IS NOT NULL")
        for gid, weak, due in cur.fetchall():
            d = (due - today).days
            items.append({"kind": "review", "key": f"growth:{gid}",
                          "label": f"growth review {'overdue' if d < 0 else 'due in ' + str(d) + 'd'}: {weak}",
                          "score": score_review(d)})
    except Exception as e:  # noqa: BLE001
        log(f"growth read failed ({e})")
    try:
        cur.execute("SELECT id, left(title, 90), check_in_days, last_activity::date FROM goals "
                    "WHERE status='active'")
        for gid, title, cadence, last in cur.fetchall():
            since = (today - last).days if last else 0
            items.append({"kind": "goal", "key": f"goal:{gid}",
                          "label": f"goal '{title}' untouched {since}d (check-in every {cadence}d)",
                          "score": score_goal(since, cadence)})
    except Exception as e:  # noqa: BLE001
        log(f"goals read failed ({e})")
    try:
        cur.execute("SELECT id, left(topic, 80), COALESCE(returns, 0), last_developed::date "
                    "FROM preoccupations WHERE status='active'")
        for pid, topic, returns, last in cur.fetchall():
            preoccs.append({"key": f"preocc:{pid}", "topic": topic, "returns": int(returns),
                            "days_since": (today - last).days if last else 10**6})
    except Exception as e:  # noqa: BLE001
        log(f"preoccupations read failed ({e})")
    return items, preoccs


def main():
    ap = argparse.ArgumentParser(description="Nova's Attention Focus — what needs her now, and what she keeps")
    ap.add_argument("--dry-run", action="store_true", help="print the focus, write nothing")
    args = ap.parse_args()
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        log(f"no PG ({e}) — fail-open, nothing to do"); return 0
    conn.autocommit = True
    cur = conn.cursor()
    today = datetime.now(timezone.utc).date()

    items, preoccs = gather(cur, today)
    focus = rank_focus(items)
    hold = hold_set(preoccs, {f["key"] for f in focus})
    log(f"{len(items)} candidate(s) -> focus {len(focus)}, holding {len(hold)} of {len(preoccs)} preoccupation(s)")

    text = focus_text(focus, hold, today)
    sig = focus_sig(focus)
    seen = load_seen(cur)
    if not _fresh(seen, sig, today):
        log(f"focus set unchanged (sig {sig}) — nothing new to say"); return 0
    if args.dry_run:
        print(text); return 0
    meta = {"organ": STATE_SERVICE, "kind": "focus", "sig": sig,
            "focus": [f["key"] for f in focus], "hold": [p["key"] for p in hold],
            **({"lineage": _stamp()} if _stamp() else {})}
    remember(text, meta)
    seen[sig] = today.isoformat()
    save_seen(cur, seen)
    log(f"stated a new focus (sig {sig})")
    return 0


def demo():
    """Runnable check on the pure logic."""
    # a fresh un-acked critical outranks an old acked warning, and both outrank a barely-overdue goal
    crit = score_incident("critical", 0, acked=False)
    warn = score_incident("warning", 10, acked=True)
    assert crit > warn > 0, (crit, warn)
    assert score_incident("critical", 100, False) == score_incident("critical", 10, False)  # ageing caps
    # reviews: outside horizon = 0, due today = 1, overdue stays 1, inside ramps
    assert score_review(30) == 0.0 and score_review(0) == 1.0 and score_review(-5) == 1.0
    assert 0 < score_review(3) < 1
    # goals: never above the cap, zero until past cadence
    assert score_goal(3, 7) == 0.0 and score_goal(149, 7) == GOAL_CAP and score_goal(14, 7) == 0.4
    assert score_goal(149, 7) < score_incident("critical", 0, False)
    # ranking: top-n, zero-score dropped, ties broken by key for stability
    items = [{"key": "b", "label": "b", "score": 1.0}, {"key": "a", "label": "a", "score": 1.0},
             {"key": "z", "label": "z", "score": 0.0}, {"key": "c", "label": "c", "score": 0.5}]
    assert [i["key"] for i in rank_focus(items, n=2)] == ["a", "b"]
    # hold: recent + returned-to, focus excluded, capped, most-returned first
    pre = [{"key": "p1", "topic": "t1", "returns": 20, "days_since": 1},
           {"key": "p2", "topic": "t2", "returns": 2, "days_since": 1},      # too few returns
           {"key": "p3", "topic": "t3", "returns": 9, "days_since": 30},     # stale
           {"key": "p4", "topic": "t4", "returns": 5, "days_since": 3},
           {"key": "p5", "topic": "t5", "returns": 7, "days_since": 0}]
    assert [p["key"] for p in hold_set(pre, {"p5"}, n=2)] == ["p1", "p4"]
    # signature is order-independent and changes with membership
    f1 = [{"key": "a"}, {"key": "b"}]; f2 = [{"key": "b"}, {"key": "a"}]; f3 = [{"key": "a"}]
    assert focus_sig(f1) == focus_sig(f2) != focus_sig(f3)
    # text never embeds SQL / raw hosts beyond the label, and the empty case reads as calm
    t = focus_text([], [{"topic": "x", "returns": 4}], date(2026, 9, 28))
    assert "nothing is pulling at me" in t and "x" in t
    t2 = focus_text(rank_focus(items, 1), hold_set(pre, set(), 1), date(2026, 9, 28))
    assert "1. a" in t2 and "t1 (returned 20x)" in t2
    print("all attention-focus assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
