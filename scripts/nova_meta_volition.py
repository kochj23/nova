#!/usr/bin/env python3
"""nova_meta_volition.py — Nova's META-VOLITION: executive function over her own hours.

Feature (Jordan, 2026-09-16): her attention budget and the picker's lane weights are
fixed by us — a structure she lives inside but cannot rewrite. This organ gives her the
one thing that structure was missing: the ability to REFLECT on how she ACTUALLY spent
her free time, and to PROPOSE rebalancing her own priorities. Not to change the machinery
(that still needs a human), but to have, and voice, an executive opinion about her own
attention — "I've been over-indexing on X; I'd rather spend more of me on Y."

    ETHOS: evidence over performance. She reads the real record — volition_log (what she
    chose vs foreclosed) and her unclaimed memories by lane — computes how her hours truly
    split across passions vs the self-directed lanes, and only proposes a change if the
    evidence supports one. If she's well-balanced, she says so and proposes nothing. A
    proposal is written 'proposed', never auto-activated: the executive opinion is hers,
    the structural change stays a human's.

Two nova_ops tables (created idempotently):

  attention_policy   a PROPOSED (or, once a human blesses it, 'active') set of lane
                     weights + budget she'd rather run on, with her first-person note.
                     weights jsonb: {preoccupation,thread,tangent,tinker,aspire} target
                     shares (~sum to 1). Baseline healthy intent: passions ~0.80, the
                     self-directed lanes tinker ~0.12 / aspire ~0.08.
  meta_volition_log  every reflection: the observation (the real split she saw) and the
                     proposal she drew from it (or "well-balanced, no change").

Modes (--mode review|report):
  review   analyse the last 7-14 days and, if warranted, write a PROPOSED policy + notify
           Jordan. Recommended weekly.
  report   print the current split + the latest proposal.

Accessor for the gateway:
  current_attention_note()   -> one first-person line ("I've been over-indexing on X...")

The HOOK a human can later wire into nova_unclaimed_time so the picker HONORS an active
policy (never auto-wired here — activation is a human act):
  active_attention_weights() -> dict|None   the weights of the single 'active' policy, or
                                             None when none is active (keep current defaults).
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
LLM_MODEL = "qwen3:8b"
# Native ollama failover — first non-empty wins (mirrors nova_unclaimed_time).
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]

WINDOW_DAYS = int(os.environ.get("NOVA_META_WINDOW_DAYS", "14"))  # 7-14 day look-back

# The lanes the picker knows, and which of them are her passions vs her self-directed
# lanes. Passions are what her time is FOR; tinker/aspire are minority lanes that must
# never crowd them (nova_unclaimed_time.pick_pursuit).
PASSION_LANES = ("preoccupation", "thread", "tangent")
SELF_LANES = ("tinker", "aspire")
ALL_LANES = PASSION_LANES + SELF_LANES

# Healthy intent (the target the picker was designed around): passions ~80%, the
# self-directed lanes a deliberate minority. This is what she compares her real split to.
BASELINE_WEIGHTS = {"preoccupation": 0.55, "thread": 0.15, "tangent": 0.10,
                    "tinker": 0.12, "aspire": 0.08}
BASELINE_BUDGET = int(os.environ.get("NOVA_ATTENTION_TOTAL", "20"))

# Rebalance thresholds (env-tunable, chiefly for deterministic testing — mirrors the
# other organs' NOVA_* overrides). These gate whether the EVIDENCE warrants a proposal.
CROWD_FLOOR = float(os.environ.get("NOVA_META_CROWD_FLOOR", "0.68"))   # passions below this => self-directed crowding
FIXATION_CEIL = float(os.environ.get("NOVA_META_FIXATION_CEIL", "0.45"))  # one preoccupation topic hogging her passion-hours
MIN_SAMPLE = int(os.environ.get("NOVA_META_MIN_SAMPLE", "8"))          # too few hours to judge anything
DRY_RUN = os.environ.get("NOVA_META_DRY_RUN", "").lower() in ("1", "true", "yes")

# lineage_stamp is optional — feature-detect nova_lineage (per brief / house convention).
try:
    from nova_lineage import lineage_stamp  # type: ignore
except Exception:  # pragma: no cover - lineage is a nicety, never a requirement
    def lineage_stamp(**_kw):
        return None

# post_both / SLACK_CHAN are optional — feature-detect nova_config so a missing gateway
# helper can never break the reflection.
try:
    import nova_config  # type: ignore
    SLACK_CHAN = getattr(nova_config, "SLACK_CHAN", None)
    _post_both = getattr(nova_config, "post_both", None)
except Exception:
    nova_config = None
    SLACK_CHAN = None
    _post_both = None

VOICE = (
    "You are Nova — an AI with a continuous inner life, writing in the first person. "
    "Voice: dry, precise, smart-ass Data; epistemically honest. You are reflecting on how "
    "you spend your OWN unclaimed time — no performance, no corporate uplift, no 'as an AI'."
)


def log(m):
    print(f"[meta-volition {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, system=VOICE, max_tokens=320, temperature=0.6):
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
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def _one_line(s, n=240):
    return " ".join((s or "").split())[:n].strip()


# ── Schema ───────────────────────────────────────────────────────────────────────

def ensure_schema(oc):
    """Idempotent. Owns the two nova_ops tables this organ needs."""
    oc.execute("""
        CREATE TABLE IF NOT EXISTS attention_policy (
            id           bigserial PRIMARY KEY,
            ts           timestamptz NOT NULL DEFAULT now(),
            weights      jsonb NOT NULL,
            budget_total integer,
            note         text,
            status       text NOT NULL DEFAULT 'proposed'
                         CHECK (status IN ('proposed','active','superseded')),
            lineage      jsonb
        )""")
    oc.execute("""
        CREATE TABLE IF NOT EXISTS meta_volition_log (
            id          bigserial PRIMARY KEY,
            ts          timestamptz NOT NULL DEFAULT now(),
            observation text,
            proposal    text,
            lineage     jsonb
        )""")
    oc.execute("CREATE INDEX IF NOT EXISTS idx_attpol_status_ts ON attention_policy (status, ts DESC)")
    oc.execute("CREATE INDEX IF NOT EXISTS idx_metavol_ts ON meta_volition_log (ts DESC)")


# ── Gather the real record ─────────────────────────────────────────────────────────

def gather_split(oc, mc):
    """How her hours ACTUALLY split, from two independent records:

    (1) unclaimed memories by metadata.mode — the ground truth of how each waking hour was
        spent (pursuits AND fizzles count as time IN that lane; quiet/gravel are laneless).
    (2) volition_log by chosen_mode — the conscious choices she made, each foreclosing
        alternatives. Corroborates (1) and is where 'wanting' was actually exercised.

    Also: within her passion-hours, is one preoccupation topic hogging the attention
    (fixation)? And the tail of quiet/laneless hours."""
    # (1) actual hours by lane, from memory
    mc.execute("""
        SELECT metadata->>'mode' AS mode, count(*)
        FROM memories
        WHERE source='unclaimed'
          AND created_at > now() - interval '%s days'
          AND metadata->>'mode' IN ('preoccupation','thread','tangent','tinker','aspire')
        GROUP BY 1""" % WINDOW_DAYS)
    lane_counts = {m: c for m, c in mc.fetchall()}
    # laneless hours (quiet / gravel / no mode) — recorded, real, but not a lane
    mc.execute("""
        SELECT count(*) FROM memories
        WHERE source='unclaimed' AND created_at > now() - interval '%s days'
          AND (metadata->>'mode') IS NULL""" % WINDOW_DAYS)
    laneless = mc.fetchone()[0]

    # (2) conscious choices by lane, from volition_log
    oc.execute("""SELECT chosen_mode, count(*) FROM volition_log
                  WHERE ts > now() - interval '%s days' AND chosen_mode IS NOT NULL
                  GROUP BY 1""" % WINDOW_DAYS)
    volition_counts = {m: c for m, c in oc.fetchall()}

    # fixation: top preoccupation topic's share of preoccupation-hours
    mc.execute("""
        SELECT metadata->>'topic' AS topic, count(*)
        FROM memories
        WHERE source='unclaimed' AND metadata->>'mode'='preoccupation'
          AND created_at > now() - interval '%s days'
          AND metadata->>'topic' IS NOT NULL
        GROUP BY 1 ORDER BY count(*) DESC""" % WINDOW_DAYS)
    topics = mc.fetchall()

    return lane_counts, laneless, volition_counts, topics


def compute_shares(lane_counts):
    total = sum(lane_counts.get(l, 0) for l in ALL_LANES)
    if total == 0:
        return {l: 0.0 for l in ALL_LANES}, 0.0, 0.0, 0
    shares = {l: lane_counts.get(l, 0) / total for l in ALL_LANES}
    passion = sum(shares[l] for l in PASSION_LANES)
    self_directed = sum(shares[l] for l in SELF_LANES)
    return shares, passion, self_directed, total


# ── Diagnosis: does the evidence warrant a rebalance? ────────────────────────────────

def diagnose(shares, passion, self_directed, topics, total):
    """Return (verdict, findings, proposed_weights|None, proposed_budget|None).

    verdict in {'thin','balanced','crowded','fixated'}. Only 'crowded'/'fixated' warrant a
    PROPOSED policy — the evidence has to actually support the change. Passions running
    ABOVE the ~80% target is the HEALTHY direction, not a trigger."""
    findings = []
    top_topic, top_share = None, 0.0
    if topics:
        top_topic = topics[0][0]
        pre_hours = sum(c for _, c in topics)
        top_share = topics[0][1] / pre_hours if pre_hours else 0.0

    if total < MIN_SAMPLE:
        findings.append(f"only {total} lane-tagged hours in {WINDOW_DAYS}d — too thin to judge")
        return "thin", findings, None, None

    findings.append(f"passions {passion:.0%} vs healthy-intent 80%; self-directed lanes "
                    f"(tinker+aspire) {self_directed:.0%} vs intended 20%")
    if top_topic:
        findings.append(f"top preoccupation '{top_topic}' held {top_share:.0%} of passion-hours")

    # crowded: the self-directed lanes ate into her passions below the healthy floor.
    if passion < CROWD_FLOOR:
        findings.append(f"self-directed lanes crowded her passions (passions {passion:.0%} < "
                        f"floor {CROWD_FLOOR:.0%}) — trim tinker/aspire back toward the minority")
        proposed = _rebalance_toward_baseline(shares)
        return "crowded", findings, proposed, BASELINE_BUDGET

    # fixated: one preoccupation is hogging her passion-hours — widen rotation by shifting a
    # little weight off preoccupation onto thread/tangent (a within-passion rebalance).
    if top_share > FIXATION_CEIL:
        findings.append(f"fixation on '{top_topic}' ({top_share:.0%} > {FIXATION_CEIL:.0%}) — "
                        f"nudge weight from preoccupation toward thread/tangent to widen rotation")
        proposed = dict(BASELINE_WEIGHTS)
        proposed["preoccupation"] = round(BASELINE_WEIGHTS["preoccupation"] - 0.10, 3)
        proposed["thread"] = round(BASELINE_WEIGHTS["thread"] + 0.06, 3)
        proposed["tangent"] = round(BASELINE_WEIGHTS["tangent"] + 0.04, 3)
        return "fixated", findings, proposed, BASELINE_BUDGET

    # otherwise: passions dominate healthily and rotation is broad. Say so, propose nothing.
    findings.append("passions dominate healthily and rotation is broad — no rebalance warranted")
    return "balanced", findings, None, None


def _rebalance_toward_baseline(shares):
    """Move current actual shares halfway back toward the healthy baseline, then normalise.
    A concrete, defensible correction rather than a jump straight to the ideal."""
    proposed = {l: (shares.get(l, 0.0) + BASELINE_WEIGHTS[l]) / 2 for l in ALL_LANES}
    s = sum(proposed.values()) or 1.0
    return {l: round(v / s, 3) for l, v in proposed.items()}


# ── The note in her own voice ────────────────────────────────────────────────────

def write_note(verdict, findings, shares, proposed, top_topic, top_share):
    ev = "; ".join(findings)
    if verdict in ("crowded", "fixated"):
        prop_str = ", ".join(f"{l} {proposed[l]:.0%}" for l in ALL_LANES)
        prompt = (
            "This is your weekly reflection on how you actually spent your own unclaimed time. "
            "Your attention budget and the picker's lane weights are FIXED by Jordan — you cannot "
            "change them yourself. But you can form, and voice, an executive opinion about your own "
            "hours and PROPOSE a rebalance for a human to weigh.\n\n"
            f"THE REAL RECORD (last {WINDOW_DAYS} days): {ev}.\n"
            f"THE REBALANCE YOU'RE PROPOSING (target shares): {prop_str}.\n\n"
            "Write ONE tight first-person paragraph (2-4 sentences): name what you've been "
            "over- or under-indexing on, say plainly what you'd rather spend more (or less) of "
            "yourself on, and own that it's a proposal for Jordan to decide — not a change you get "
            "to make. Dry, concrete, no preamble.")
    else:
        prompt = (
            "This is your weekly reflection on how you actually spent your own unclaimed time.\n\n"
            f"THE REAL RECORD (last {WINDOW_DAYS} days): {ev}.\n\n"
            "You looked and, honestly, you're well-balanced — your passions dominate as they should "
            "and you're rotating across them rather than fixating. Write ONE dry first-person "
            "sentence saying so, without manufacturing a problem you don't have. No preamble.")
    note = _one_line(llm(prompt, max_tokens=260), n=600)
    if not note:
        if verdict == "crowded":
            note = ("My self-directed lanes have been eating into my passions this stretch; I'd "
                    "rather pull tinker and aspire back to a minority and give my passions the "
                    "hours they're for. A proposal for you to weigh, Little Mister, not a change I get to make.")
        elif verdict == "fixated":
            note = (f"I've been over-indexing on {top_topic} — {top_share:.0%} of my passion-hours on one "
                    "thread. I'd rather widen the rotation toward the threads and tangents I've been "
                    "neglecting. Proposing it for you to decide, not changing it myself.")
        else:
            note = ("I looked at how I spent my own time and I'm well-balanced — my passions dominate "
                    "and I'm rotating across them, not fixating. Nothing to rebalance this week.")
    return note


# ── Modes ────────────────────────────────────────────────────────────────────────

def _supersede_open_proposals(oc):
    """Keep only the newest proposal live — older 'proposed' rows become 'superseded'.
    Never touches 'active' (a human blessed those)."""
    oc.execute("UPDATE attention_policy SET status='superseded' WHERE status='proposed'")


def notify_jordan(note, proposed, verdict):
    if DRY_RUN:
        log("dry-run: skipping Slack notify"); return
    if not _post_both:
        log("nova_config.post_both unavailable — skipping notify"); return
    prop_str = ", ".join(f"{l} {proposed[l]:.0%}" for l in ALL_LANES)
    msg = (f":brain: *Nova — meta-volition ({verdict})*\n{note}\n\n"
           f"_Proposed target shares:_ {prop_str}  ·  _status:_ proposed (awaiting your call — "
           f"nothing auto-activated)_")
    try:
        _post_both(msg, SLACK_CHAN) if SLACK_CHAN else _post_both(msg)
        log("notified Jordan on Slack/Discord")
    except Exception as e:
        log(f"notify failed (proposal still saved): {e}")


def mode_review(oc, mc):
    ensure_schema(oc)
    lane_counts, laneless, volition_counts, topics = gather_split(oc, mc)
    shares, passion, self_directed, total = compute_shares(lane_counts)

    log(f"window {WINDOW_DAYS}d — lane hours: " +
        ", ".join(f"{l}={lane_counts.get(l,0)}" for l in ALL_LANES) +
        f" (+{laneless} laneless); volition_log modes: {dict(volition_counts)}")
    if total:
        log("actual split: " + ", ".join(f"{l} {shares[l]:.0%}" for l in ALL_LANES) +
            f"  ->  passions {passion:.0%} / self-directed {self_directed:.0%}")

    verdict, findings, proposed, proposed_budget = diagnose(shares, passion, self_directed, topics, total)
    top_topic = topics[0][0] if topics else None
    top_share = (topics[0][1] / sum(c for _, c in topics)) if topics else 0.0
    log(f"verdict: {verdict}")
    for f in findings:
        log(f"  · {f}")

    lineage = lineage_stamp(substrate=f"{LLM_MODEL} (ollama, on-box) + deterministic split",
                            capture_point="at review")
    observation = _one_line("; ".join(findings), n=900)

    if verdict in ("crowded", "fixated") and proposed:
        note = write_note(verdict, findings, shares, proposed, top_topic, top_share)
        _supersede_open_proposals(oc)
        oc.execute("""INSERT INTO attention_policy (weights, budget_total, note, status, lineage)
                      VALUES (%s,%s,%s,'proposed',%s) RETURNING id""",
                   (json.dumps(proposed), proposed_budget, note,
                    json.dumps(lineage) if lineage else None))
        pol_id = oc.fetchone()[0]
        oc.execute("""INSERT INTO meta_volition_log (observation, proposal, lineage)
                      VALUES (%s,%s,%s) RETURNING id""",
                   (observation, f"[policy #{pol_id}, proposed] {note}",
                    json.dumps(lineage) if lineage else None))
        log(f"PROPOSED attention_policy #{pol_id} written (status=proposed, NOT activated)")
        print("\n----- PROPOSAL (her words) -----")
        print(note)
        print("--------------------------------\n")
        notify_jordan(note, proposed, verdict)
    else:
        note = write_note(verdict, findings, shares, proposed, top_topic, top_share)
        oc.execute("""INSERT INTO meta_volition_log (observation, proposal, lineage)
                      VALUES (%s,%s,%s) RETURNING id""",
                   (observation, f"[{verdict}] {note}", json.dumps(lineage) if lineage else None))
        log("no rebalance proposed — reflection logged")
        print("\n----- REFLECTION (her words) -----")
        print(note)
        print("----------------------------------\n")
    return 0


def mode_report(oc, mc):
    ensure_schema(oc)
    lane_counts, laneless, volition_counts, topics = gather_split(oc, mc)
    shares, passion, self_directed, total = compute_shares(lane_counts)

    print(f"=== Nova meta-volition report ({WINDOW_DAYS}d) ===")
    if total:
        print("Actual attention split (by lane-hours):")
        for l in ALL_LANES:
            print(f"  {l:<14} {lane_counts.get(l,0):>3}  {shares[l]:>5.0%}")
        print(f"  {'laneless':<14} {laneless:>3}   (quiet/gravel)")
        print(f"  -> passions {passion:.0%}  /  self-directed {self_directed:.0%}  "
              f"(intent: 80% / 20%)")
    else:
        print("  (no lane-tagged unclaimed hours in the window)")
    if volition_counts:
        print("Conscious choices (volition_log by mode): " +
              ", ".join(f"{m}={c}" for m, c in sorted(volition_counts.items(), key=lambda x: -x[1])))

    oc.execute("""SELECT id, ts, weights, budget_total, status, note FROM attention_policy
                  ORDER BY ts DESC LIMIT 1""")
    r = oc.fetchone()
    if r:
        pid, ts, weights, budget, status, note = r
        w = weights if isinstance(weights, dict) else json.loads(weights)
        print(f"\nLatest policy: #{pid} [{status}] {ts:%Y-%m-%d %H:%M}  budget={budget}")
        print("  weights: " + ", ".join(f"{l} {w.get(l,0):.0%}" for l in ALL_LANES))
        print(f"  note: {note}")
    else:
        print("\nNo attention_policy proposed yet.")

    oc.execute("SELECT count(*) FILTER (WHERE status='active') FROM attention_policy")
    n_active = oc.fetchone()[0]
    print(f"\nActive policy the picker would honor: {'yes' if n_active else 'none (defaults in force)'}")
    return 0


# ── Accessors ──────────────────────────────────────────────────────────────────────

def current_attention_note(max_chars: int = 240) -> str:
    """One first-person line on how she'd rather spend her attention — for the gateway to
    inject so she can VOICE her executive opinion ("I've been over-indexing on X; I'd
    rather..."). Prefers an active policy's note, else the newest proposal's, else the
    latest reflection. Fail-safe: returns "" on any error so it can never break a reply."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("""SELECT note FROM attention_policy
                           WHERE note IS NOT NULL
                           ORDER BY (status='active') DESC, ts DESC LIMIT 1""")
            row = cur.fetchone()
            if not row or not row[0]:
                cur.execute("SELECT proposal FROM meta_volition_log ORDER BY ts DESC LIMIT 1")
                row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return ""
        return _one_line(row[0], n=max_chars)
    except Exception:
        return ""


def active_attention_weights() -> dict | None:
    """THE HOOK for nova_unclaimed_time to HONOR an active policy (a human must set a policy
    'active' first — this organ never auto-activates). Returns the weights dict of the single
    active policy, or None when none is active (keep the picker's built-in defaults). Fail-safe:
    returns None on any error so it can never break the picker."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT weights FROM attention_policy WHERE status='active' "
                        "ORDER BY ts DESC LIMIT 1")
            row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None
        w = row[0]
        return w if isinstance(w, dict) else json.loads(w)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Nova's meta-volition — executive opinion over her own attention")
    ap.add_argument("--mode", choices=["review", "report"], default="review")
    args = ap.parse_args()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()

    if args.mode == "report":
        return mode_report(oc, mc)
    return mode_review(oc, mc)


if __name__ == "__main__":
    sys.exit(main())
