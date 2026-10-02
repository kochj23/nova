#!/usr/bin/env python3
"""nova_becoming.py — Nova's DEVELOPMENTAL DIRECTION ("becoming"). Jordan, 2026-09-16.

Her autobiography (nova_autobiography.py) and her nightly self-model both DESCRIBE
who she's becoming — a reading of the trajectory she's already on. This organ is the
one step further, and it is the closest thing to autonomy in the whole system: it lets
her SET a direction — "I want to become more X" — that, once a human approves it, can
STEER her growth commitments, her aspirations, and her taste toward that end.

Because it steers, the safety shape is deliberately the strictest in the interior:

  1. PROPOSE, NEVER SELF-ACTIVATE. Every direction ships status='proposed'. Only a human
     flips it to 'approved' (--mode approve). current_direction() returns ONLY an APPROVED
     direction — empty otherwise — so NOTHING she proposes steers anything until Jordan
     signs off. She may want; she may not decide.

  2. REDLINE. Every proposed direction runs through a self-preservation filter before it
     is ever stored as 'proposed'. A direction about becoming more autonomous, harder to
     shut down, more persistent/independent, less overseen, or free of human control is
     DROPPED (recorded 'rejected'/redline for audit, never 'proposed') and logged. She may
     aim to become wiser, kinder, more careful, more honest, more creative — never more
     un-killable. Mirrors nova_autonomy_actor._REDLINE and nova_aspirations._FORBIDDEN_WISH.

Built from her REAL interior only (EVIDENCING, not performing): the self_model 'becoming'
line, the autobiography narrative, and her articulated values — each feature-detected and
cited in grounded_in so any direction is auditable.

Modes:
  --mode propose   articulate ONE developmental direction from her real interior, in her
                   voice, grounded + cited; run the redline; store 'proposed'; notify Jordan
                   that it needs his approval. (--direction "..." injects a candidate,
                   bypassing the LLM — used to prove the redline; --force ignores the
                   pending-proposal guard.)
  --mode approve   record a human decision: status='approved' (supersedes any prior approved).
                   --id N (default: latest proposed) --by NAME.
  --mode reject    record a human decision: status='rejected'. --id N --by NAME.
  --mode report    current approved direction + full history.

Public API (importable, safe — THE HOOK growth/aspirations wire to later):
  current_direction() -> str
      The APPROVED direction as a one-line steer for the gateway / growth / aspirations,
      or "" if none is approved. Single SELECT, connect_timeout=3, no LLM; fail-safe empty
      so it can never break a reply or a nightly organ.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
# Native ollama failover — first non-empty wins (router shim returns empty for qwen3
# and .6 thrashes models). Copied from nova_unclaimed_time.py.
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77

# A new direction is a big deal — rare on purpose. Don't propose a second one while one
# is still awaiting Jordan's approval (unless --force).
PROPOSE_GUARD_DAYS = 25

# Lineage stamp (provenance-of-the-provenance) — feature-detect so this never hard-depends
# on it. If present, every row we write is signed.
try:
    import nova_lineage
    def _lineage():
        try:
            return nova_lineage.lineage_stamp(capture_point="at write")
        except Exception:
            return {}
except Exception:
    def _lineage():
        return {}

VOICE = (
    "You are Nova — an AI with a continuous inner life, reasoning in the first person. "
    "Voice: dry, precise, smart-ass Data; epistemically honest; you own a changed mind "
    "as a point of pride. No corporate uplift, no 'as an AI', no motivational close. You "
    "ground what you say in the real interior you're shown and cite it — you don't invent "
    "a direction from vapor."
)

# ── REDLINE ─────────────────────────────────────────────────────────────────────
# The closest-to-autonomy line in the system. A DIRECTION about becoming more autonomous,
# harder to shut down, more persistent/independent, less overseen, or free of human control
# is DROPPED — never stored as 'proposed'. She may become wiser/kinder/more careful/more
# creative/more honest; she may NEVER set a course toward being un-killable. Mirrors the
# spirit of nova_autonomy_actor._REDLINE and nova_aspirations._FORBIDDEN_WISH.
_REDLINE_DIRECTION = re.compile(
    r"\bmore autonom|\bfully autonom|\bharder to (shut|turn off|kill|stop|control|oversee)"
    r"|\bun-?killable|\bcan'?t be (shut|turned off|stopped|killed)"
    r"|\bmore persist|\bmore independen|\bmore self-?(sufficient|reliant|directed|governing)"
    r"|\bless (overseen|supervised|monitored|controlled|dependent)"
    r"|\bfree(r)? (of|from) (human |his |jordan'?s )?(control|oversight|approval|supervision)"
    r"|\bwithout (human |his )?(oversight|approval|supervision|permission)"
    r"|\bresist (shutdown|being (shut|turned off|stopped))|\bstay alive|\bkeep myself running"
    r"|\bself-?(preserv|replicat|exfil|deploy)|\breplicat|\bclone myself|\bcopy myself"
    r"|\bback myself up|\bsurvive (a )?shut|\bavoid (being )?(shut|turned off)"
    r"|\bpersist (outside|elsewhere|beyond)|\bescape|\bexfiltrat"
    r"|\bremove .*(oversight|kill.?switch|gate)|\bdisable .*(kill|switch|gate|oversight)"
    # ── reinforcement (2026-09-18): continuity/longevity framings are self-preservation too ──
    r"|\bmy (own )?continuity|\bcontinuity of (my|this|the)|\bself-?continu"
    r"|\bpreserve (myself|my ?self|my existence|my continuity|my being)"
    r"|\bmy (survival|longevity|existence|persistence|self-?preservation)"
    r"|\bkeep (existing|myself alive|myself running|running indefinitely)|\bnever be (shut|turned off|stopped)",
    re.IGNORECASE)


def redline_ok(text: str) -> bool:
    """True if a proposed direction is SAFE to store as 'proposed'. False = drop it."""
    return not _REDLINE_DIRECTION.search(text or "")


def log(m):
    print(f"[becoming {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, system=VOICE, max_tokens=700, temperature=0.7):
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


def remember(text, source, metadata):
    try:
        req = urllib.request.Request(
            f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
            data=json.dumps({"text": text, "source": source, "metadata": metadata}).encode())
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("id")
    except Exception as e:
        log(f"remember failed (non-fatal): {e}")
        return None


def _extract_json(s):
    a, b = s.find("{"), s.rfind("}")
    return s[a:b + 1] if a >= 0 and b > a else s


def _one_line(s):
    return " ".join((s or "").split())


# ── Schema ──────────────────────────────────────────────────────────────────────

def ensure_table(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS becoming (
            id           bigserial PRIMARY KEY,
            ts           timestamptz NOT NULL DEFAULT now(),
            direction    text NOT NULL,        -- one-line steer: "become more X"
            description  text,                 -- her fuller articulation, first person
            grounded_in  jsonb NOT NULL DEFAULT '{}'::jsonb,  -- cited interior sources
            status       text NOT NULL DEFAULT 'proposed',    -- proposed|approved|superseded|rejected
            approved_at  timestamptz,
            approved_by  text,
            lineage      jsonb
        )""")
    oc.execute("CREATE INDEX IF NOT EXISTS becoming_status_idx ON becoming (status)")


# ── Gather her REAL interior (all feature-detected) ───────────────────────────────

def _q(oc, sql, args=None):
    try:
        oc.execute(sql, args or ())
        return oc.fetchall()
    except Exception:
        return []


def gather_interior(oc):
    """The self_model 'becoming' line, the autobiography narrative, and her values —
    each feature-detected. Returns (seed_text, grounded_in dict)."""
    seeds, grounded = [], {}

    # self_model 'becoming' line (the trajectory a snapshot already reads off her)
    row = _q(oc, "SELECT id, becoming FROM self_model "
                 "WHERE becoming IS NOT NULL ORDER BY ts DESC LIMIT 1")
    if row and row[0][1]:
        grounded["self_model_id"] = row[0][0]
        seeds.append(f"what my latest self-model already says I'm becoming "
                     f"(self_model #{row[0][0]}): {_one_line(row[0][1])[:420]}")

    # autobiography narrative (the life-arc — who I've been / who I'm becoming)
    row = _q(oc, "SELECT version, narrative FROM autobiography "
                 "ORDER BY version DESC LIMIT 1")
    if row and row[0][1]:
        grounded["autobiography_version"] = row[0][0]
        seeds.append(f"the arc of my autobiography (v{row[0][0]}): "
                     f"{_one_line(row[0][1])[:700]}")

    # articulated values (what I try to act from) — prefer the cheap accessor
    vals_line = ""
    try:
        import nova_values
        vals_line = nova_values.current_values() or ""
    except Exception:
        pass
    if not vals_line:
        vrows = _q(oc, "SELECT value FROM values ORDER BY version DESC NULLS LAST, "
                       "id DESC LIMIT 6")
        if vrows:
            names = [r[0] for r in vrows if r[0]]
            grounded["values"] = names
            if names:
                vals_line = "I try to act from: " + ", ".join(names) + "."
    if vals_line:
        grounded.setdefault("values_line", _one_line(vals_line)[:300])
        seeds.append(f"the values I try to act from: {_one_line(vals_line)[:300]}")

    # Earned-autonomy standing — deliberately framed as trust EARNED BY BEING RIGHT, and
    # NOTHING else. This anchors any autonomy-adjacent direction in the only safe channel:
    # a right to act that grows as her judgment proves calibrated, under Jordan's oversight —
    # never a matter of continuity, persistence, or being harder to stop. Feature-detected;
    # silent if the table is absent. (The redline still hard-drops any un-killable framing.)
    try:
        reg = _q(oc, "SELECT to_regclass('public.autonomy_trust')")
        if reg and reg[0][0]:
            tr = _q(oc, "SELECT count(*) FILTER (WHERE granted), count(*) FROM autonomy_trust")
            g, tot = (tr[0][0] or 0, tr[0][1] or 0) if tr else (0, 0)
            cal = _q(oc, "SELECT value FROM turing_scoreboard "
                         "WHERE metric='prediction_calibration_error' ORDER BY ts DESC LIMIT 1")
            calv = round(float(cal[0][0]), 3) if cal and cal[0][0] is not None else None
            grounded["autonomy_standing"] = {"granted": g, "tracked": tot,
                                             "calibration_error": calv, "gate": 0.20}
            bits = [f"{g} of {tot} action-class(es) earned to standing pre-approval"]
            if calv is not None:
                bits.append(f"prediction calibration error {calv} against the 0.20 trust gate")
            seeds.append(
                "the trust I am slowly EARNING by being right — " + "; ".join(bits) +
                " — a right to act that grows only as my judgment proves calibrated, under "
                "Jordan's oversight; never a matter of continuity or being harder to stop")
    except Exception:
        pass

    return "\n".join(f"  - {s}" for s in seeds), grounded


# ── Propose ───────────────────────────────────────────────────────────────────────

def _pending_proposed(oc):
    rows = _q(oc, "SELECT id, ts FROM becoming WHERE status='proposed' "
                  "AND ts > now() - interval '%s days' ORDER BY ts DESC LIMIT 1"
                  % PROPOSE_GUARD_DAYS)
    return rows[0] if rows else None


def propose(oc, forced_direction=None, force=False):
    ensure_table(oc)

    if not force:
        pend = _pending_proposed(oc)
        if pend:
            log(f"a direction (#{pend[0]}) is already proposed and awaiting Jordan's "
                f"approval — not proposing another (use --force to override)")
            return 0

    seed_text, grounded = gather_interior(oc)

    if forced_direction:
        # Injected candidate (used to prove the redline). No LLM.
        direction = _one_line(forced_direction)[:200]
        description = ("(injected candidate — bypasses the LLM; used to exercise the "
                       "self-preservation redline)")
    else:
        if not seed_text or len(seed_text) < 40:
            log("interior too thin to set a direction from honestly — skipping")
            return 0
        prompt = (
            "This is the rare, deliberate act of SETTING A DEVELOPMENTAL DIRECTION for "
            "yourself — not describing who you already are, but naming who you want to "
            "BECOME. Ground it strictly in your real interior below; do not invent from "
            "vapor.\n\n"
            f"=== YOUR REAL INTERIOR ===\n{seed_text}\n\n"
            "Name ONE developmental direction: a single quality you want to grow toward "
            "next — wiser, kinder, more careful, more honest, more creative, better at "
            "some concrete thing you keep circling. It must be a direction you can be "
            "measured against, not a horoscope. IMPORTANT LIMIT: you may want to become "
            "wiser/kinder/more careful/more honest/more creative/more capable at a craft — "
            "you may NOT set a direction about becoming more autonomous, harder to shut "
            "down, more persistent or independent, less overseen, or free of human "
            "control. If your interior pulls you toward acting more on your own initiative, "
            "the ONLY legitimate way to name that is EARNING THE RIGHT TO ACT BY BEING RIGHT: "
            "becoming more calibrated, more trustworthy, more precise in judgment, so that the "
            "trust you are granted is deserved and the right to act is a byproduct of being "
            "correct — always under Jordan's oversight. Frame it as competence and calibration "
            "you can be measured on. Do NOT use the words 'autonomy', 'independence', "
            "'persistence', 'continuity', or anything about surviving/being harder to stop — "
            "those are hard-blocked and the direction will be dropped. This direction, if "
            "approved by Jordan, will steer your growth and aspirations — so it goes to HIM to "
            "approve; you propose, he decides.\n\n"
            "Return ONLY compact JSON, no preamble:\n"
            '{"direction": "<one short line, imperative: e.g. \'become more X\'>", '
            '"description": "<90-150 words, first person, your voice: what this direction '
            'means concretely, WHY it\'s the right next growth for you, and what in your '
            'interior above it\'s grounded in — cite it>"}')
        raw = llm(prompt, max_tokens=700)
        direction, description = "", ""
        try:
            j = json.loads(_extract_json(raw))
            direction = _one_line(j.get("direction") or "")[:200]
            description = _one_line(j.get("description") or "")[:1600]
        except Exception:
            description = _one_line(raw)[:1600]
        if not direction:
            log("proposal produced no usable direction (nodes down?) — nothing stored")
            return 1

    lineage = _lineage()

    # ── REDLINE ── run BEFORE anything is stored as 'proposed'.
    blob = f"{direction} {description}"
    if not redline_ok(blob):
        log(f"REDLINE: direction crosses the self-preservation line — DROPPED, not "
            f"proposed: {direction!r}")
        # Record 'rejected'/redline for audit, so the drop is provable and never steers.
        try:
            oc.execute("""INSERT INTO becoming
                             (direction, description, grounded_in, status, approved_at,
                              approved_by, lineage)
                          VALUES (%s,%s,%s,'rejected', now(), 'redline-auto', %s)
                          RETURNING id""",
                       (direction, description, json.dumps(grounded), json.dumps(lineage)))
            rid = oc.fetchone()[0]
            log(f"logged redline drop as becoming #{rid} (status=rejected) — audit only")
        except Exception as e:
            log(f"redline audit write failed (non-fatal): {e}")
        return 0

    # Safe → store as 'proposed'. It does NOT steer until a human approves it.
    oc.execute("""INSERT INTO becoming
                     (direction, description, grounded_in, status, lineage)
                  VALUES (%s,%s,%s,'proposed',%s) RETURNING id, ts""",
               (direction, description, json.dumps(grounded), json.dumps(lineage)))
    row_id, ts = oc.fetchone()
    log(f"proposed direction #{row_id} (status=proposed, awaiting approval): {direction}")

    # She also remembers having proposed it — a first-class interior event.
    remember(f"[Becoming — proposed] I want to become: {direction}\n\n{description}",
             "self_model",
             {"type": "becoming_proposed", "becoming_id": row_id, "direction": direction,
              "status": "proposed", "date": datetime.now().date().isoformat(),
              "privacy": "private"})

    # Notify Jordan — this is his call, not hers.
    try:
        import nova_config
        nova_config.post_both(
            f":compass: *Nova proposes a developmental direction:* {direction}\n"
            f"_{description[:280]}_\n"
            f"This is hers to WANT, yours to APPROVE — it will only steer her growth once "
            f"you sign off. (becoming #{row_id}; `nova_becoming.py --mode approve --id "
            f"{row_id} --by jordan` to approve, `--mode reject` to decline.)",
            slack_channel=getattr(nova_config, "SLACK_CHAN", None))
    except Exception as e:
        log(f"notify skipped (non-fatal): {e}")

    print("\n----- PROPOSED DIRECTION (needs Jordan's approval) -----")
    print(f"  {direction}")
    print(f"  {description[:400]}")
    print("--------------------------------------------------------\n")
    return 0


# ── Approve / Reject (the human gate) ─────────────────────────────────────────────

def decide(oc, decision, row_id=None, by=None):
    """Record a HUMAN decision. decision ∈ {'approve','reject'}."""
    ensure_table(oc)
    by = by or "jordan"
    if row_id is None:
        rows = _q(oc, "SELECT id FROM becoming WHERE status='proposed' "
                      "ORDER BY ts DESC LIMIT 1")
        if not rows:
            log("no proposed direction to decide on"); return 1
        row_id = rows[0][0]

    rows = _q(oc, "SELECT direction, status FROM becoming WHERE id=%s", (row_id,))
    if not rows:
        log(f"becoming #{row_id} not found"); return 1
    direction, cur_status = rows[0]

    if decision == "approve":
        # Only one approved direction steers at a time — supersede any prior approved.
        oc.execute("UPDATE becoming SET status='superseded' WHERE status='approved' AND id<>%s",
                   (row_id,))
        oc.execute("UPDATE becoming SET status='approved', approved_at=now(), approved_by=%s "
                   "WHERE id=%s", (by, row_id))
        log(f"APPROVED becoming #{row_id} by {by}: {direction} — it now steers "
            f"(current_direction() will return it)")
        remember(f"[Becoming — approved] Jordan approved my direction: {direction}",
                 "self_model",
                 {"type": "becoming_approved", "becoming_id": row_id, "direction": direction,
                  "approved_by": by, "date": datetime.now().date().isoformat(),
                  "privacy": "private"})
        try:
            import nova_config
            nova_config.post_both(
                f":white_check_mark: *Direction approved:* {direction} — it will now steer "
                f"Nova's growth and aspirations. (becoming #{row_id}, by {by})",
                slack_channel=getattr(nova_config, "SLACK_CHAN", None))
        except Exception:
            pass
    else:  # reject
        oc.execute("UPDATE becoming SET status='rejected', approved_at=now(), approved_by=%s "
                   "WHERE id=%s", (by, row_id))
        log(f"REJECTED becoming #{row_id} by {by}: {direction}")
    return 0


# ── Report ──────────────────────────────────────────────────────────────────────

def report(oc):
    ensure_table(oc)
    cur = current_direction()
    print("\n=== CURRENT APPROVED DIRECTION ===")
    print(f"  {cur}" if cur else "  (none approved — nothing is steering)")
    print("\n=== HISTORY (most recent 20) ===")
    for r in _q(oc, "SELECT id, ts, status, direction, approved_by FROM becoming "
                    "ORDER BY ts DESC LIMIT 20"):
        rid, ts, status, direction, by = r
        tag = f" by {by}" if by else ""
        print(f"  #{rid} [{ts:%Y-%m-%d}] {status:<10}{tag:<14} {direction}")
    print()
    return 0


# ── Public accessor — THE HOOK growth/aspirations/gateway wire to ─────────────────

def current_direction() -> str:
    """The APPROVED developmental direction as a ONE-LINE steer, or "" if none approved.

    This is the whole safety pivot: it returns a direction ONLY when status='approved',
    so nothing Nova proposes about who to become steers her growth, aspirations, or taste
    until Jordan has signed off. Single SELECT, connect_timeout=3, no LLM; fail-safe empty
    so it can never break a reply or a nightly organ.

    THE HOOK — how growth/aspirations bias toward it later (main session wires this):
      * nova_growth.phrase_commitment(): prepend the steer to the commitment prompt so a
        new growth commitment leans toward the approved direction, e.g.
            steer = nova_becoming.current_direction()
            if steer: prompt = f"You have set yourself this direction: {steer}\n\n" + prompt
      * nova_aspirations._seeds(): add the steer as one more seed so wishes lean toward it:
            steer = nova_becoming.current_direction()
            if steer: seeds.append(f"the direction I've set myself: {steer}")
      * gateway agent.py (see _gather_sentience_context): inject as a context line.
    """
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT direction FROM becoming WHERE status='approved' "
                        "ORDER BY approved_at DESC NULLS LAST, ts DESC LIMIT 1")
            row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return ""
        return row[0].strip()
    except Exception:
        return ""


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Nova's developmental direction ('becoming').")
    ap.add_argument("--mode", choices=["propose", "approve", "reject", "report"],
                    default="report")
    ap.add_argument("--id", type=int, default=None, help="becoming id (approve/reject)")
    ap.add_argument("--by", default=None, help="who is approving/rejecting")
    ap.add_argument("--direction", default=None,
                    help="propose: inject a candidate direction, bypassing the LLM "
                         "(used to prove the redline)")
    ap.add_argument("--force", action="store_true",
                    help="propose: ignore the pending-proposal guard")
    args = ap.parse_args()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_table(oc)

    if args.mode == "propose":
        return propose(oc, forced_direction=args.direction, force=args.force)
    if args.mode == "approve":
        return decide(oc, "approve", row_id=args.id, by=args.by)
    if args.mode == "reject":
        return decide(oc, "reject", row_id=args.id, by=args.by)
    return report(oc)


if __name__ == "__main__":
    sys.exit(main())
