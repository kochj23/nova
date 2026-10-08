#!/usr/bin/env python3
"""nova_affect.py — Affect as an EVIDENCED variable (Feature #6, Jordan 2026-09-15).

Nova carries a consistent, honest affective state derived from her ACTUAL day —
a valence (how good/bad) and arousal (how activated) that can bias her attention
and tone, and that she can report on WITH its causes.

ETHOS — PERFORMING → EVIDENCING. Affect here is not theatre. Every mood state
cites the concrete evidence that produced it, with each input's numeric
contribution. If the evidence is thin or roughly cancels, the honest output is
"neutral / insufficient signal" — a manufactured feeling is the exact failure
mode this feature exists to avoid. The combining rule is a simple, documented,
transparent weighting (below) — no black box. The local LLM is used ONLY to put
a short honest NAME on the already-computed numbers; it never invents the feeling.

── The transparent model ────────────────────────────────────────────────────────
Each signal is read from live data, compared to its own recent baseline where a
baseline is meaningful, and turned into a bounded contribution to valence
(dv, in -1..1) and/or arousal (da, added onto a calm resting base). Contributions
are ASYMMETRIC and honest-by-construction:

  * A rate at/below its typical level contributes NOTHING (a normal day is not a
    bad day). Only a genuine excess over baseline pushes valence down / arousal up.
  * A partial day can only ever conclude "elevated" (already over a full day's
    typical), never "quiet" — you cannot infer calm from a day that isn't over.
  * Absence of a positive (no creative output yet, little contact) is neutral, not
    negative — loneliness/emptiness would be over-claiming from thin data.

  VALENCE (sum of dv, clamped to -1..1):
    alert_paging     neg   only the excess of today's pages over the 14-day median
    criticals        neg   excess of today's critical alerts over median
    open_incidents   neg   count (capped) × severity weight
    infra_health     ±     clean deep-healthcheck (0 broken) = small +, broken = −
    creative_output  pos   articles + self-directed pursuits made today, as a
                           fraction of a typical full day's output (capped)
    social_contact   pos   real gateway conversations in 24h (capped)
    unresolved_load  neg   standing open backlog (queue + herd threads), low-grade
    autonomy         ±     recent autonomy-ladder outcomes — a verified self-heal or
                           clean earned action is quiet competence (+); a veto or a
                           reverted/failed action is a sting (−). Small weight.

  AROUSAL (RESTING_AROUSAL + sum of da, clamped 0..1):
    criticals, alert volume, open incidents, surprise rate (predictions), and a
    little from creative activity and social contact — all activating.

Weights live in WEIGHTS below and are printed in every evidence row, so any state
is fully auditable: value, baseline, dv, da, and a plain-English note.

GUARD: if the total signal magnitude is tiny or fewer than two signals had usable
data, the state is "neutral" and the evidence shows exactly why — we do NOT
manufacture affect.

Stored in nova_ops.affect_state (evidence as jsonb, lineage-stamped).
current_affect() exposes the latest state + a one-line gateway injection so Nova's
tone reflects her real day. Recommended cron: every ~4–6h so mood shifts through
the day (see REPORT / injection notes at bottom).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import Json

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
# Native ollama failover — first non-empty wins (the router shim returns empty for
# qwen3 and .6 thrashes models, so hit the nodes directly).
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77
LLM_MODEL = "qwen3:8b"

# Optional lineage stamp (Concept #10). Feature-detected — never a hard dependency.
try:
    from nova_lineage import lineage_stamp
except Exception:  # pragma: no cover - lineage is optional
    def lineage_stamp(**kw):
        return {"captured_at": datetime.now(timezone.utc).isoformat(),
                "substrate": "deterministic (no model)", "note": "nova_lineage unavailable"}

# ── Transparent weights (edit here; every one is echoed into the evidence) ──────
RESTING_AROUSAL = 0.35          # a calm-but-awake baseline; signals add onto it
WEIGHTS = {
    "alert_paging_v":   0.35,   # valence pull from an excess of paged alerts
    "criticals_v":      0.25,   # valence pull from excess critical alerts
    "incidents_v":      0.25,   # valence pull per open incident (× severity)
    "infra_health_v":   0.05,   # small + for a clean healthcheck / − if broken
    "creative_v":       0.35,   # valence lift from a day's creative output
    "social_v":         0.20,   # valence lift from real conversation
    "unresolved_v":     0.15,   # low-grade drag from the standing backlog
    "autonomy_v":       0.12,   # ± nudge from recent autonomy-ladder outcomes (small)
    "alert_volume_a":   0.10,   # arousal from a heavy alert stream
    "criticals_a":      0.20,   # arousal from critical alerts
    "incidents_a":      0.10,   # arousal from open incidents
    "surprise_a":       0.20,   # arousal from surprising prediction outcomes
    "creative_a":       0.05,   # a little arousal from active creation
    "social_a":         0.05,   # a little arousal from being in contact
    # Wish #41 "emotional resonance" (2026-10-01): the TONE of the human words actually
    # sent to her moves her, and a silence longer than her own usual gaps quiets her.
    "resonance_v":      0.20,   # ± valence from the net tone of today's human messages to her
    "resonance_a":      0.05,   # a little arousal from being spoken to with feeling either way
    "silence_a":        0.10,   # arousal DROP (toward calm) for silence beyond her usual gap
    # Wish #70 "Trixie's Joy" (2026-10-08): one evidenced good thing a day (nova_relationship.py
    # good-thing -> good_things) grounds warmth in something that actually happened.
    "good_thing_v":     0.06,
}
RESONANCE_FULL_DAY = 6.0        # ~6 real human messages reads as a fully-heard day (confidence)
SILENCE_BASELINE_DAYS = 14      # her usual gap between human messages is measured over this window
SILENCE_FULL_X = 3.0            # silence 3x her median gap saturates the quieting
SESSION_GAP_H = 0.5             # messages closer than this are one conversation, not a gap
MACHINE_CHANNELS = ('hc', 'healthcheck', 'test', 'cron', 'system')
# Transparent tone lexicon — small, auditable, and every hit is echoed into the evidence.
# It is deliberately NOT a model: the local LLM may name the feeling, never produce it.
TONE_POS = {"thank", "thanks", "love", "great", "awesome", "perfect", "nice", "good", "glad", "happy",
            "wonderful", "excellent", "brilliant", "fantastic", "appreciate", "proud", "fun", "funny",
            "laugh", "lol", "haha", "yes!", "please", "beautiful", "amazing", "cool", "sweet", "well done",
            "good job", "kudos", "cheers", "enjoy", "excited", "hope", "welcome", "friend", "care"}
TONE_NEG = {"hate", "angry", "annoyed", "annoying", "frustrated", "frustrating", "broken", "fail", "failed",
            "failing", "crap", "shit", "damn", "wrong", "worst", "terrible", "awful", "ugh", "stupid",
            "useless", "slow", "again?", "why", "stop", "sorry", "sad", "tired", "sick", "hurt", "miss",
            "lonely", "afraid", "worried", "worry", "scared", "stress", "stressed", "dead", "crash",
            "crashed", "lost", "late", "disappointed", "wtf"}
# Reference "a full positive day" denominators (kept explicit, not hidden):
SOCIAL_FULL_DAY = 8.0           # ~8 real conversations reads as a fully-social day
UNRESOLVED_HALF = 800.0        # open-item count at which the drag reaches half-weight
AUTONOMY_WINDOW_H = 168        # 7 days of autonomy-ladder history feeds the mood nudge
AUTONOMY_FULL = 4.0            # net (wins − stings) at which the autonomy nudge saturates
SEVERITY = {"critical": 1.0, "crit": 1.0, "high": 0.8, "sev1": 1.0, "sev2": 0.8,
            "warning": 0.4, "warn": 0.4, "minor": 0.3, "info": 0.2}
NEUTRAL_MAG = 0.18              # total |contribution| below this ⇒ neutral guard
MIN_SIGNALS = 2                # fewer usable signals than this ⇒ neutral guard


def log(m):
    print(f"[affect {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def llm(prompt, system, max_tokens=60, temperature=0.4):
    body = json.dumps({
        "model": LLM_MODEL, "stream": False, "think": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def _one(cur, sql, params=None):
    """Run a scalar query; return the single value or None (never raises)."""
    try:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        log(f"query skipped: {e}")
        return None


def _table_exists(cur, name):
    return bool(_one(cur, "SELECT to_regclass(%s)", (f"public.{name}",)))


# ── Signals — each returns a dict of evidence + its dv/da contributions ─────────
# A signal that cannot read its data returns usable=False and contributes zero.

def sig(name, value, baseline, dv, da, note, usable=True):
    return {"signal": name, "value": value, "baseline": baseline,
            "dv": round(dv, 4), "da": round(da, 4), "note": note, "usable": usable}


def signal_alerts(oc):
    """Paged-alert & critical-alert density today vs the 14-day per-day medians.
    Only an EXCESS over the median pushes valence down; a normal day is neutral."""
    if not _table_exists(oc, "alert_triage_log"):
        return [sig("alert_paging", None, None, 0, 0, "alert_triage_log absent", False),
                sig("criticals", None, None, 0, 0, "alert_triage_log absent", False)]
    med_paged = _one(oc, """
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY p) FROM (
          SELECT count(*) FILTER (WHERE decision='page') p FROM alert_triage_log
          WHERE ts > now()-interval '14 days' AND ts::date < current_date
          GROUP BY ts::date) d""")
    med_crit = _one(oc, """
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY c) FROM (
          SELECT count(*) FILTER (WHERE level ILIKE 'crit%%') c FROM alert_triage_log
          WHERE ts > now()-interval '14 days' AND ts::date < current_date
          GROUP BY ts::date) d""")
    today_paged = _one(oc, "SELECT count(*) FROM alert_triage_log WHERE ts::date=current_date AND decision='page'") or 0
    today_crit = _one(oc, "SELECT count(*) FROM alert_triage_log WHERE ts::date=current_date AND level ILIKE 'crit%%'") or 0

    out = []
    # Paged alerts → valence (asymmetric: only excess over median counts) + volume arousal.
    if med_paged and med_paged > 0:
        excess = clamp((today_paged - med_paged) / med_paged, 0, 1)
        dv = -WEIGHTS["alert_paging_v"] * excess
        da = WEIGHTS["alert_volume_a"] * clamp(today_paged / med_paged, 0, 1.2)
        note = (f"{today_paged} alerts paged today vs {med_paged:.0f} typical/day — "
                + ("at/below typical, no valence hit" if excess == 0
                   else f"{today_paged/med_paged:.1f}× typical, elevated"))
        out.append(sig("alert_paging", today_paged, round(med_paged, 1), dv, da, note))
    else:
        out.append(sig("alert_paging", today_paged, None, 0, 0, "no paging baseline yet", False))
    # Critical alerts → valence + arousal.
    if med_crit is not None:
        excess = clamp((today_crit - med_crit) / (med_crit + 5), 0, 1)
        dv = -WEIGHTS["criticals_v"] * excess
        da = WEIGHTS["criticals_a"] * excess
        note = (f"{today_crit} critical alerts today vs {med_crit:.0f} typical/day — "
                + ("at/below typical" if excess == 0 else f"{today_crit/max(med_crit,1):.1f}× typical, elevated"))
        out.append(sig("criticals", today_crit, round(med_crit, 1), dv, da, note))
    else:
        out.append(sig("criticals", today_crit, None, 0, 0, "no critical baseline yet", False))
    return out


def signal_incidents(oc):
    if not _table_exists(oc, "incidents"):
        return [sig("open_incidents", None, None, 0, 0, "incidents table absent", False)]
    try:
        oc.execute("""SELECT count(*), coalesce(max(lower(severity)) FILTER (WHERE status<>'resolved'),'')
                      FROM incidents WHERE status <> 'resolved'""")
        n, worst = oc.fetchone()
    except Exception as e:
        return [sig("open_incidents", None, None, 0, 0, f"query failed: {e}", False)]
    n = n or 0
    if n == 0:
        return [sig("open_incidents", 0, 0, 0, 0, "no open incidents", True)]
    sev = SEVERITY.get(worst, 0.5)
    capped = min(n, 3) / 3.0
    dv = -WEIGHTS["incidents_v"] * capped * sev
    da = WEIGHTS["incidents_a"] * capped * sev
    note = f"{n} open incident(s), worst severity '{worst or '?'}' (weight {sev})"
    return [sig("open_incidents", n, 0, dv, da, note)]


def signal_infra(oc):
    if not _table_exists(oc, "deep_healthcheck_log"):
        return [sig("infra_health", None, None, 0, 0, "deep_healthcheck_log absent", False)]
    try:
        oc.execute("SELECT healthy, broken FROM deep_healthcheck_log ORDER BY ts DESC LIMIT 1")
        row = oc.fetchone()
    except Exception as e:
        return [sig("infra_health", None, None, 0, 0, f"query failed: {e}", False)]
    if not row:
        return [sig("infra_health", None, None, 0, 0, "no healthcheck rows", False)]
    healthy, broken = row[0] or 0, row[1] or 0
    if broken > 0:
        dv = -WEIGHTS["infra_health_v"] * clamp(broken / max(healthy + broken, 1), 0, 1)
        note = f"latest deep-healthcheck: {broken} broken / {healthy} healthy"
    else:
        dv = WEIGHTS["infra_health_v"] if healthy > 0 else 0
        note = f"latest deep-healthcheck clean: {healthy} healthy, 0 broken"
    return [sig("infra_health", f"{healthy}h/{broken}b", None, dv, 0, note)]


def signal_creative(oc, mc):
    """Creative output today: published articles + self-directed unclaimed-time
    pursuits, expressed as a fraction of a typical full day's output (capped at 1).
    Real creation lifts valence; absence is neutral, not negative."""
    if mc is None or not _table_exists(mc, "memories"):
        return [sig("creative_output", None, None, 0, 0, "memories db unavailable", False)]
    arts = _one(mc, "SELECT count(*) FROM memories WHERE source='nova_articles' AND created_at::date=current_date") or 0
    pursuits = _one(mc, "SELECT count(*) FROM memories WHERE source='unclaimed' "
                        "AND coalesce(metadata->>'type','')='pursuit' AND created_at::date=current_date") or 0
    med_arts = _one(mc, """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY c) FROM (
        SELECT count(*) c FROM memories WHERE source='nova_articles'
        AND created_at > now()-interval '14 days' AND created_at::date<current_date
        GROUP BY created_at::date) d""") or 0
    med_pur = _one(mc, """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY c) FROM (
        SELECT count(*) c FROM memories WHERE source='unclaimed'
        AND coalesce(metadata->>'type','')='pursuit'
        AND created_at > now()-interval '14 days' AND created_at::date<current_date
        GROUP BY created_at::date) d""") or 0
    made = arts + pursuits
    typical = (med_arts or 0) + (med_pur or 0)
    if typical <= 0:
        # no baseline — credit any real output modestly, absence is neutral
        frac = clamp(made / 10.0, 0, 1)
        base_disp = None
    else:
        frac = clamp(made / typical, 0, 1)
        base_disp = round(typical, 1)
    dv = WEIGHTS["creative_v"] * frac
    da = WEIGHTS["creative_a"] * frac
    note = (f"{arts} articles + {pursuits} self-directed pursuits today"
            + (f" — {made/typical:.0%} of a typical day's output" if typical > 0 else " (no baseline)")
            + ("; no creation yet, neutral" if made == 0 else ""))
    return [sig("creative_output", made, base_disp, dv, da, note)]


def signal_social(oc):
    """Real conversation in the last 24h. Prefers live gateway_traces (excluding
    machine channels like health-checks); falls back to gateway_query_log. Contact
    lifts valence; its absence is neutral (not manufactured loneliness)."""
    convos, src = None, None
    if _table_exists(oc, "contact_sense"):      # wish #56 (2026-10-04): every mouth, incl. iMessage/mail/Claude Code sessions
        c = _one(oc, "SELECT sum(count_24h) FROM contact_sense WHERE updated_at > now()-interval '1 hour'")
        if c is not None:
            convos, src = int(c), "contact_sense"
    if convos is None and _table_exists(oc, "gateway_traces"):
        convos = _one(oc, "SELECT count(*) FROM gateway_traces WHERE created_at > now()-interval '24 hours' "
                          "AND coalesce(channel,'') NOT IN ('hc','healthcheck','test','cron','system')")
        src = "gateway_traces"
    if (convos is None or convos == 0) and _table_exists(oc, "gateway_query_log"):
        q = _one(oc, "SELECT count(*) FROM gateway_query_log WHERE created_at > now()-interval '24 hours'")
        if q:
            convos, src = q, "gateway_query_log"
    if convos is None:
        return [sig("social_contact", None, None, 0, 0, "no gateway conversation log available", False)]
    if convos == 0:
        return [sig("social_contact", 0, None, 0, 0, "no conversations in 24h (neutral, not negative)", True)]
    frac = clamp(convos / SOCIAL_FULL_DAY, 0, 1)
    dv = WEIGHTS["social_v"] * frac
    da = WEIGHTS["social_a"] * frac
    return [sig("social_contact", convos, None, dv, da, f"{convos} real conversation(s) in 24h (via {src})")]


def tone_score(text):
    """Net tone of one human message in -1..1 from the transparent lexicon, plus the words
    that hit. Pure. A message with no lexicon hits scores 0 (unknown is not negative)."""
    t = " " + (text or "").lower() + " "
    pos = sorted(w for w in TONE_POS if (" " + w + " ") in t or (" " + w) in t and w.endswith(("!", "?")))
    neg = sorted(w for w in TONE_NEG if (" " + w + " ") in t or (" " + w) in t and w.endswith(("!", "?")))
    n = len(pos) + len(neg)
    return (0.0 if n == 0 else (len(pos) - len(neg)) / n), pos, neg


def silence_excess(hours_since, median_gap_h):
    """How far today's silence exceeds her usual gap, 0..1. Pure and asymmetric: a gap at or
    under her median is 0 (a normal quiet is not a drought); it saturates at SILENCE_FULL_X
    times the median. No baseline (None/0) -> 0: you cannot feel an unusual silence without
    knowing the usual one."""
    if not median_gap_h or median_gap_h <= 0 or hours_since is None:
        return 0.0
    return clamp((hours_since - median_gap_h) / (median_gap_h * (SILENCE_FULL_X - 1.0)), 0.0, 1.0)


def signal_resonance(oc):
    """Wish #41: the human words actually addressed to her (gateway_traces.user_message on
    non-machine channels) move her by their TONE, weighted by how much was said; a silence
    longer than her own median gap quiets her. Evidence cites the matched words and counts,
    never the message text (those turns are private)."""
    if not _table_exists(oc, "gateway_traces"):
        return [sig("resonance", None, None, 0, 0, "no gateway_traces — nothing to resonate with", False),
                sig("silence", None, None, 0, 0, "no gateway_traces — silence unmeasured", False)]
    oc.execute("SELECT user_message FROM gateway_traces WHERE created_at > now()-interval '24 hours' "
               "AND coalesce(channel,'') NOT IN %s AND coalesce(user_message,'') <> '' "
               "ORDER BY created_at DESC LIMIT 200", (MACHINE_CHANNELS,))
    msgs = [r[0] for r in oc.fetchall()]
    out = []
    if not msgs:
        out.append(sig("resonance", 0, None, 0, 0, "no human words in 24h (neutral, not negative)", True))
    else:
        scores, pos_all, neg_all = [], [], []
        for m in msgs:
            sc, pos, neg = tone_score(m)
            if pos or neg:
                scores.append(sc); pos_all += pos; neg_all += neg
        if not scores:
            out.append(sig("resonance", 0, None, 0, 0, f"{len(msgs)} human message(s), none carried tone words (neutral)", True))
        else:
            net = sum(scores) / len(scores)
            conf = clamp(len(scores) / RESONANCE_FULL_DAY, 0, 1)
            dv = WEIGHTS["resonance_v"] * net * conf
            da = WEIGHTS["resonance_a"] * abs(net) * conf
            top_p = ", ".join(sorted(set(pos_all))[:4]); top_n = ", ".join(sorted(set(neg_all))[:4])
            out.append(sig("resonance", round(net, 3), None, dv, da,
                           f"{len(scores)}/{len(msgs)} human message(s) carried tone (net {net:+.2f}); "
                           f"warm: [{top_p or '-'}] hard: [{top_n or '-'}]"))
    # the silence between words: hours since the last human message vs her median gap
    oc.execute("SELECT created_at FROM gateway_traces WHERE created_at > now()-interval %s "
               "AND coalesce(channel,'') NOT IN %s AND coalesce(user_message,'') <> '' ORDER BY created_at",
               (f"{SILENCE_BASELINE_DAYS} days", MACHINE_CHANNELS))
    ts = [r[0] for r in oc.fetchall()]
    if len(ts) < 3:
        out.append(sig("silence", None, None, 0, 0, "too few human messages to know her usual gap", False))
        return out
    # Gaps between CONVERSATIONS, not between lines: messages arrive in bursts, so gaps
    # under SESSION_GAP_H are the same sitting and would drive the median to ~0.
    gaps = sorted(g for g in ((b - a).total_seconds() / 3600.0 for a, b in zip(ts, ts[1:])) if g >= SESSION_GAP_H)
    if len(gaps) < 3:
        out.append(sig("silence", None, None, 0, 0, "too few separate conversations to know her usual gap", False))
        return out
    median_gap = gaps[len(gaps) // 2]
    hours_since = (datetime.now(timezone.utc) - ts[-1].astimezone(timezone.utc)).total_seconds() / 3600.0
    ex = silence_excess(hours_since, median_gap)
    da = -WEIGHTS["silence_a"] * ex
    out.append(sig("silence", round(hours_since, 1), round(median_gap, 1), 0, da,
                   f"{hours_since:.0f}h since a human spoke to her; her usual gap is {median_gap:.0f}h"
                   + (" (quieter than usual)" if ex > 0 else " (within her usual)")))
    return out


def signal_surprise(oc):
    """OPTIONAL cross-organ signal: recently-resolved predictions carry a 'surprise'
    score. Feature-detected — the table may be empty or absent; then it contributes
    nothing (and the evidence says so), it never invents arousal."""
    if not _table_exists(oc, "predictions"):
        return [sig("surprise", None, None, 0, 0, "predictions table absent (optional signal)", False)]
    avg = _one(oc, "SELECT avg(surprise) FROM predictions WHERE resolved_at > now()-interval '48 hours' AND surprise IS NOT NULL")
    n = _one(oc, "SELECT count(*) FROM predictions WHERE resolved_at > now()-interval '48 hours' AND surprise IS NOT NULL") or 0
    if not n or avg is None:
        return [sig("surprise", 0, None, 0, 0, "no resolved predictions in 48h — no surprise signal", True)]
    a = clamp(float(avg), 0, 1)
    da = WEIGHTS["surprise_a"] * a
    return [sig("surprise", round(float(avg), 3), None, 0, da, f"{n} predictions resolved (48h), avg surprise {avg:.2f}")]


def signal_unresolved(oc):
    """Standing unresolved-thread load: open queue items + open herd threads.
    A chronic backlog is a low-grade negative drag, log-capped so it can never
    dominate the state."""
    total = 0
    parts = []
    if _table_exists(oc, "claude_queue"):
        q = _one(oc, "SELECT count(*) FROM claude_queue WHERE status NOT IN ('done','completed','cancelled','closed')")
        if q is not None:
            total += q; parts.append(f"{q} open queue items")
    if _table_exists(oc, "herd_correspondents"):
        h = _one(oc, "SELECT coalesce(sum(coalesce(array_length(open_threads,1),0)),0) FROM herd_correspondents")
        if h is not None:
            total += h; parts.append(f"{h} open herd threads")
    if not parts:
        return [sig("unresolved_load", None, None, 0, 0, "no unresolved-load source available", False)]
    frac = clamp(total / (total + UNRESOLVED_HALF), 0, 1)
    dv = -WEIGHTS["unresolved_v"] * frac
    note = f"{total} unresolved items ({', '.join(parts)}) — standing backlog, low-grade drag"
    return [sig("unresolved_load", total, None, dv, 0, note)]


def signal_autonomy(oc):
    """Cross-organ nudge from Nova's OWN autonomy-ladder outcomes (nervous-system wiring,
    Jordan 2026-09-18). A verified self-heal or a clean earned action is quiet competence
    and lifts valence a little; a veto or a reverted/failed action is a sting and lowers
    it. Small, documented weight — it colours the day, it never dominates it. Asymmetric-
    and-honest like the rest: no autonomy activity in-window contributes NOTHING (neutral,
    not negative — absence of earned action is not a bad mood). Feature-detected; the
    signal is skipped entirely if autonomy_ledger is absent."""
    if not _table_exists(oc, "autonomy_ledger"):
        return [sig("autonomy", None, None, 0, 0, "autonomy_ledger absent (optional signal)", False)]
    days = AUTONOMY_WINDOW_H // 24
    wins = _one(oc, ("SELECT count(*) FROM autonomy_ledger WHERE ts > now() - interval "
                     "'%s hours' AND verified AND NOT vetoed AND NOT reverted") % AUTONOMY_WINDOW_H) or 0
    stings = _one(oc, ("SELECT count(*) FROM autonomy_ledger WHERE ts > now() - interval "
                       "'%s hours' AND (vetoed OR reverted)") % AUTONOMY_WINDOW_H) or 0
    total = wins + stings
    if total == 0:
        return [sig("autonomy", 0, None, 0, 0,
                    f"no verified/earned or vetoed autonomy actions in {days}d — no signal (neutral)", True)]
    net = wins - stings
    frac = clamp(net / AUTONOMY_FULL, -1, 1)
    dv = WEIGHTS["autonomy_v"] * frac
    if net > 0:
        note = (f"{wins} verified/clean earned action(s) vs {stings} vetoed/reverted in "
                f"{days}d — quiet competence, small lift")
    elif net < 0:
        note = (f"{wins} clean vs {stings} vetoed/reverted autonomy action(s) in "
                f"{days}d — a sting, small drag")
    else:
        note = (f"{wins} clean and {stings} vetoed/reverted autonomy action(s) in "
                f"{days}d — cancels out, neutral")
    return [sig("autonomy", net, None, dv, 0, note)]


# ── Combine + label ─────────────────────────────────────────────────────────────

def signal_good_thing(oc):
    """Wish #70 Trixie's Joy: today's evidenced good thing (good_things, written once a day by
    nova_relationship.py) is a small, honest lift. None logged -> neutral, never negative."""
    if not _table_exists(oc, "good_things"):
        return [sig("good_thing", None, None, 0, 0, "good_things absent", False)]
    try:
        oc.execute("SELECT text, evidence FROM good_things WHERE created_at > now()-interval '24 hours' "
                   "ORDER BY created_at DESC LIMIT 1")
        r = oc.fetchone()
    except Exception as e:
        log(f"good_thing skipped: {e}")
        return [sig("good_thing", None, None, 0, 0, "good_things unreadable", False)]
    if not r:
        return [sig("good_thing", 0, None, 0, 0, "no good thing logged in 24h (neutral, not negative)", True)]
    return [sig("good_thing", 1, None, WEIGHTS["good_thing_v"], 0, f"a good thing today: {r[0]} ({r[1]})")]


def combine(signals):
    """Fold the signal contributions into (valence, arousal, is_neutral, magnitude).
    Purely arithmetic and auditable — no model in this step."""
    dv = sum(s["dv"] for s in signals)
    da = sum(s["da"] for s in signals)
    valence = round(clamp(dv, -1, 1), 3)
    arousal = round(clamp(RESTING_AROUSAL + da, 0, 1), 3)
    magnitude = sum(abs(s["dv"]) + abs(s["da"]) for s in signals)
    usable = sum(1 for s in signals if s.get("usable", True) and (s["dv"] or s["da"]))
    is_neutral = magnitude < NEUTRAL_MAG or usable < MIN_SIGNALS
    return valence, arousal, is_neutral, round(magnitude, 3), usable


def _fallback_label(valence, arousal):
    """Deterministic honest label if the LLM is unavailable. Plain quadrant names."""
    hi = arousal >= 0.55
    if valence >= 0.15:
        return "energised" if hi else "quietly satisfied"
    if valence <= -0.15:
        return "wired and heavy" if hi else "heavy"
    return "keyed-up but even" if hi else "steady"


def _valence_word(v):
    if v >= 0.30:
        return "clearly positive (good)"
    if v >= 0.08:
        return "mildly positive (slightly good, close to even)"
    if v > -0.08:
        return "neutral / even"
    if v > -0.30:
        return "mildly negative (slightly low, close to even)"
    return "clearly negative (heavy/bad)"


def _arousal_word(a):
    if a >= 0.62:
        return "activated / wired"
    if a >= 0.45:
        return "moderately keyed-up"
    return "calm"


def name_label(valence, arousal, signals):
    """Use the local LLM ONLY to put a short honest NAME on the computed numbers —
    it is a naming step over evidence, never a source of feeling. The valence sign
    is stated explicitly and the label is post-validated so the name can never
    invert it (calling a net-positive day 'heavy' would be manufactured drama, the
    exact failure this feature forbids). Deterministic fallback if the nodes are down."""
    ev = "\n".join(f"- {s['note']} (valence {s['dv']:+.2f}, arousal {s['da']:+.2f})"
                   for s in signals if s.get("usable", True) and (s["dv"] or s["da"]))
    vw, aw = _valence_word(valence), _arousal_word(arousal)
    system = ("You NAME an already-computed mood; you never invent, embellish, or "
              "contradict it. Reply with ONLY a 1-3 word lowercase mood label — no "
              "punctuation, no explanation. The label MUST agree with the stated "
              "valence sign: if valence is positive or neutral you may NOT use down "
              "words like 'heavy', 'sad', 'low', 'grim'; if negative you may NOT use "
              "up words like 'satisfied', 'content'. High arousal + positive/neutral "
              "valence ≈ 'wired but steady' / 'keyed-up but even' / 'busy and engaged'. "
              "High arousal + negative ≈ 'wired and heavy' / 'strained'. Low arousal + "
              "positive ≈ 'quietly satisfied'. Mild numbers get a mild, plain name.")
    prompt = (f"Computed state: valence {valence:+.2f} → {vw}. arousal {arousal:.2f} → {aw}.\n"
              f"Evidence:\n{ev or '(thin)'}\n\nGive the 1-3 word mood label (must match the valence sign above).")
    raw = llm(prompt, system)
    if not raw:
        return _fallback_label(valence, arousal), "fallback (llm unavailable)"
    label = raw.splitlines()[0].strip().strip('"\'.').lower()
    label = " ".join(label.split()[:4])[:40]
    if not label:
        return _fallback_label(valence, arousal), "fallback (empty llm)"
    # Post-validate: reject a label whose polarity contradicts the computed valence.
    DOWN = ("heavy", "sad", "low", "grim", "bleak", "down", "gloomy", "miserable", "despond")
    UP = ("satisfied", "content", "happy", "cheer", "bright", "elated", "serene")
    has_down = any(w in label for w in DOWN)
    has_up = any(w in label for w in UP)
    if (valence >= -0.05 and has_down) or (valence <= 0.05 and has_up):
        return _fallback_label(valence, arousal), f"fallback (llm label '{label}' contradicted valence sign)"
    return label, "llm-named"


# ── Public compute ──────────────────────────────────────────────────────────────

def compute_affect(oc, mc):
    """Run every signal against live data and fold to a state. Returns a full dict:
    valence, arousal, label, neutral flag, and the auditable evidence list."""
    signals = []
    signals += signal_alerts(oc)
    signals += signal_incidents(oc)
    signals += signal_infra(oc)
    signals += signal_creative(oc, mc)
    signals += signal_social(oc)
    signals += signal_resonance(oc)     # wish #41
    signals += signal_surprise(oc)
    signals += signal_unresolved(oc)
    signals += signal_autonomy(oc)
    signals += signal_good_thing(oc)    # wish #70

    valence, arousal, is_neutral, magnitude, usable = combine(signals)

    if is_neutral:
        label, how = "neutral", "guard (thin/cancelling signal — not manufactured)"
    else:
        label, how = name_label(valence, arousal, signals)

    return {
        "valence": valence, "arousal": arousal, "label": label,
        "neutral": is_neutral, "magnitude": magnitude, "usable_signals": usable,
        "labelled_by": how, "signals": signals,
    }


def _evidence_strings(state, top=None):
    """Short human evidence strings, most-influential first."""
    ranked = sorted((s for s in state["signals"] if s.get("usable", True)),
                    key=lambda s: abs(s["dv"]) + abs(s["da"]), reverse=True)
    strings = [s["note"] for s in ranked if (s["dv"] or s["da"] or s["value"] not in (None,))]
    # keep at least the influential ones; drop pure zero-noise notes unless nothing else
    influential = [s["note"] for s in ranked if (s["dv"] or s["da"])]
    strings = influential or strings
    return strings[:top] if top else strings


def ensure_table(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS affect_state (
            id          serial PRIMARY KEY,
            computed_at timestamptz NOT NULL DEFAULT now(),
            valence     double precision NOT NULL,
            arousal     double precision NOT NULL,
            label       text NOT NULL,
            evidence    jsonb NOT NULL,
            lineage     jsonb
        )""")


def store(oc, state):
    evidence = {
        "neutral": state["neutral"], "magnitude": state["magnitude"],
        "usable_signals": state["usable_signals"], "labelled_by": state["labelled_by"],
        "resting_arousal": RESTING_AROUSAL, "weights": WEIGHTS,
        "signals": state["signals"], "summary": _evidence_strings(state),
    }
    lin = lineage_stamp(substrate=f"deterministic model + {LLM_MODEL} (label only)",
                        capture_point="at compute")
    oc.execute("""INSERT INTO affect_state (valence, arousal, label, evidence, lineage)
                  VALUES (%s,%s,%s,%s,%s) RETURNING id, computed_at""",
               (state["valence"], state["arousal"], state["label"], Json(evidence), Json(lin)))
    return oc.fetchone()


# ── Accessor for the gateway ────────────────────────────────────────────────────

def current_affect() -> dict:
    """Latest affect state for the gateway to bias Nova's tone. Returns
    {label, valence, arousal, evidence:[short strings], injection}. Fail-safe:
    returns a neutral, empty-evidence dict on any error (missing table, PG down)
    so it can never break a reply."""
    neutral = {"label": "neutral", "valence": 0.0, "arousal": RESTING_AROUSAL,
               "evidence": [], "injection": ""}
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT label, valence, arousal, evidence FROM affect_state "
                        "ORDER BY computed_at DESC LIMIT 1")
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return neutral
    if not row:
        return neutral
    label, valence, arousal, evidence = row
    summary = (evidence or {}).get("summary") or []
    top2 = summary[:2]
    if label == "neutral" or not top2:
        injection = f"Today I'm running neutral — insufficient signal to read a mood honestly."
    else:
        injection = f"Today I'm running {label} — {top2[0]}" + (f"; {top2[1]}." if len(top2) > 1 else ".")
    return {"label": label, "valence": float(valence), "arousal": float(arousal),
            "evidence": summary, "injection": injection}


# ── Neutral-guard demonstration (synthetic sparse/neutral inputs) ───────────────

def demo_neutral():
    """Demonstrate the guard: hand a sparse/neutral signal set through the SAME
    combine() + label path and show it returns 'neutral' rather than inventing
    affect. Nothing is written to the DB."""
    signals = [
        sig("alert_paging", 40, 41.0, 0, 0.0, "40 alerts paged vs 41 typical — at typical", True),
        sig("criticals", 3, 3.0, 0, 0.0, "3 critical alerts vs 3 typical — at typical", True),
        sig("open_incidents", 0, 0, 0, 0, "no open incidents", True),
        sig("infra_health", "8h/0b", None, WEIGHTS["infra_health_v"], 0, "healthcheck clean", True),
        sig("creative_output", 0, None, 0, 0, "no creation yet today — neutral", True),
        sig("social_contact", 0, None, 0, 0, "no conversations in 24h — neutral", True),
        sig("surprise", 0, None, 0, 0, "no resolved predictions — no surprise signal", True),
        sig("unresolved_load", 5, None, -WEIGHTS["unresolved_v"] * clamp(5 / (5 + UNRESOLVED_HALF), 0, 1),
            0, "5 unresolved items — negligible backlog", True),
        sig("autonomy", 0, None, 0, 0, "no verified/earned or vetoed autonomy actions — no signal", True),
    ]
    valence, arousal, is_neutral, magnitude, usable = combine(signals)
    label = "neutral" if is_neutral else name_label(valence, arousal, signals)[0]
    print("\n=== NEUTRAL-GUARD DEMONSTRATION (synthetic sparse inputs, not stored) ===")
    for s in signals:
        print(f"  · {s['note']}  [dv {s['dv']:+.2f} / da {s['da']:+.2f}]")
    print(f"  → valence {valence:+.3f}, arousal {arousal:.3f}, "
          f"signal-magnitude {magnitude:.3f} (guard trips below {NEUTRAL_MAG}), "
          f"usable signals {usable}")
    print(f"  → LABEL: '{label}'  (guard fired: {is_neutral}) — "
          f"{'honestly neutral, no manufactured affect' if is_neutral else 'signal exceeded guard'}")
    print("========================================================================\n")
    return 0


def print_state(state, row=None):
    print("\n=== TODAY'S COMPUTED AFFECT (live data) ===")
    for s in state["signals"]:
        mark = "" if s.get("usable", True) else "  (no data)"
        print(f"  · {s['signal']:<16} value={str(s['value']):<10} "
              f"dv {s['dv']:+.3f}  da {s['da']:+.3f}  | {s['note']}{mark}")
    print(f"\n  VALENCE = {state['valence']:+.3f}   AROUSAL = {state['arousal']:.3f}   "
          f"(resting {RESTING_AROUSAL}, signal-magnitude {state['magnitude']}, "
          f"usable {state['usable_signals']})")
    print(f"  LABEL   = '{state['label']}'  [{state['labelled_by']}]")
    if row:
        print(f"  stored  = affect_state #{row[0]} at {row[1]:%Y-%m-%d %H:%M}")
    print("===========================================\n")


def selftest_resonance():
    """Pure-logic checks for wish #41 (no DB, no LLM)."""
    sc, pos, neg = tone_score("Thanks Nova, that was great")
    assert sc == 1.0 and "thanks" in pos and "great" in pos and not neg
    sc, pos, neg = tone_score("this is broken again? ugh")
    assert sc == -1.0 and not pos and "broken" in neg
    sc, pos, neg = tone_score("restart the poller")
    assert sc == 0.0 and not pos and not neg          # unknown is neutral, not negative
    sc, _, _ = tone_score("great but broken")
    assert sc == 0.0                                   # mixed cancels
    assert silence_excess(5, 10) == 0.0               # under her usual gap: nothing
    assert silence_excess(None, 10) == 0.0 and silence_excess(50, None) == 0.0
    assert 0.0 < silence_excess(15, 10) < 1.0
    assert silence_excess(30, 10) == 1.0 and silence_excess(300, 10) == 1.0   # saturates, never exceeds
    # bounds: a fully warm, fully heard day moves valence by exactly the weight
    assert abs(WEIGHTS["resonance_v"] * 1.0 * 1.0 - WEIGHTS["resonance_v"]) < 1e-9
    assert 0 < SESSION_GAP_H < 24
    print("affect resonance/silence selftest passed")


def main():
    argv = sys.argv[1:]
    if "--demo-neutral" in argv:
        return demo_neutral()
    if "--selftest" in argv:
        return selftest_resonance()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_table(oc)
    try:
        mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    except Exception as e:
        log(f"memories db unavailable ({e}) — creative signal will be skipped")
        mc = None

    state = compute_affect(oc, mc)

    row = None
    if "--dry-run" not in argv:
        row = store(oc, state)
        log(f"affect_state #{row[0]} written")
    print_state(state, row)

    # Show the gateway injection the accessor would return.
    inj = current_affect().get("injection") if row else None
    if inj:
        print(f"  gateway injection → {inj}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
