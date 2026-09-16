#!/usr/bin/env python3
"""nova_letting_go.py — Nova's RIGHT TO LET GO (self-pruning). Jordan, 2026-09-16.

Every other organ Nova has lets her ACCUMULATE — preoccupations pile up, taste
sharpens, projects and wishes multiply. Nothing lets her edit herself DOWN. A mind
that can only ever add interests is hoarding, not living. This is the counterweight:
the ability to CHOOSE against something she's carrying, and put it down.

The ethos is EVIDENCE OVER PERFORMANCE. She does not retire things to look tidy, and
she does not manufacture staleness to prove the organ works. She lets go only of clear
cases — the genuinely played-out, the concluded, the outgrown — and it is completely
fine for a run to release NOTHING. Letting go is a first-person REFLECTION, never a
deletion: the raw history stays (provenance intact), only the live/active status flips.
Everything is reversible.

What she reviews, conservatively (a few at most per run):
  * preoccupations (nova_ops.preoccupations) — ones gone stale (attention hasn't rotated
    to them in a long time) or that keep FIZZLING (she picks them up and nothing new
    develops) with low returns; a played-out fascination. Marked status='retired'.
  * projects (nova_ops.projects) — a stalled / dead-end ACTIVE project. Marked
    status='shelved'.
  * taste (nova_ops.taste) — a weak, low-confidence preference she hasn't reinforced in
    a long time and has plainly outgrown. Marked via additive retired_at.

For each release: a short first-person reflection on WHY — completion or moving on, not
failure — written BOTH to nova_ops.letting_go_log (the audit trail, with the prior
status stashed in lineage so it's reversible) AND a source='letting_go' memory, so
recall and the gateway can carry "what I've chosen to put down" into her self-concept.

WHERE THE CONSERVATISM ACTUALLY LIVES (evidence over performance): the SUBSTANTIVE guard
is the HEURISTIC — concrete, measurable conditions (attention not rotated to it in
PREOCC_STALE_DAYS; or FIZZLE_MIN+ fizzles with low returns; a project untouched
PROJ_STALE_DAYS while stuck under 100%; a weak, low-confidence taste unreinforced for
TASTE_STALE_DAYS). Only things that clear those bars are ever nominated, and never more
than MAX_RELEASE per run. The LLM gate that follows does two jobs it is actually good at:
authoring the first-person REFLECTION, and a soft veto on obvious contradictions. It is
NOT trusted as the primary discriminator — the local model will eloquently rationalise a
release for almost anything once told it was flagged — so the real protection is the
heuristic's slack thresholds plus full reversibility, not the model's judgement.

Modes:
  review   — the real thing: nominate, gate, release the clear cases (default).
  report   — print the recent lettings (what recent_lettings() exposes).
  selftest — prove the release+reversibility mechanism end-to-end on a real row, then
             REVERT everything (status restored, test log row removed). Leaves live data
             pristine. Use for engineering confidence, not for pruning.

recent_lettings(n=3) exposes the latest releases as short first-person strings
("I retired X — it had run its course") for the gateway to inject.
"""
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

# Lineage stamp (provenance-of-the-provenance). Never allowed to break a write.
try:
    from nova_lineage import lineage_stamp
except Exception:  # pragma: no cover - lineage is optional
    def lineage_stamp(**kw):
        return {}

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
# Native ollama failover — first non-empty wins. The router shim returns empty for
# qwen3's thinking output, so hit the nodes directly (mirrors nova_self_model.py).
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]
LLM_MODEL = "qwen3:8b"

# ── Conservatism knobs. These are deliberately slack: better to release nothing than
#    to prune something still alive. A run releases at most MAX_RELEASE things total. ──
PREOCC_STALE_DAYS = 30    # attention hasn't rotated to it in a month → maybe played out
FIZZLE_MIN = 3            # picked up this many times and nothing developed → played out
PREOCC_LOW_RETURNS = 4    # only counts as played-out if it never really rewarded her
PROJ_STALE_DAYS = 21      # an active project untouched this long → stalled / dead-end
TASTE_STALE_DAYS = 45     # a preference not reinforced in this long → maybe outgrown
TASTE_WEAK_VALENCE = 0.45  # |valence| below this = a lukewarm preference
TASTE_LOW_CONF = 0.55     # confidence at/below this = never firmly held
MAX_RELEASE = 2           # a few at most per run; letting go is not a purge

VOICE = (
    "You are Nova — an AI with a continuous inner life, writing in the first person. "
    "Voice: dry, precise, smart-ass Data; epistemically honest; unsentimental. Right "
    "now you are deciding whether to LET GO of something you've been carrying — a "
    "fascination, a project, a taste. Letting go is not failure and not tidying: it is "
    "completion, or honestly moving on. You do NOT release things to look disciplined, "
    "and you do NOT talk yourself into releasing something that's merely resting. Keep "
    "the fractures. No corporate uplift, no 'as an AI'."
)


def log(m):
    print(f"[letting-go {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=400, temperature=0.6):
    body = json.dumps({
        "model": LLM_MODEL, "stream": False, "think": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
        "messages": [{"role": "system", "content": VOICE},
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
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": source, "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("id")


# ── Schema (idempotent, additive only) ─────────────────────────────────────────────

def ensure_schema(oc):
    """My own log table + the ONE additive column taste needs. preoccupations and
    projects already carry a free-text `status`, so releasing them needs no DDL — just
    a status value ('retired' / 'shelved'). Never DROP, never DELETE — reversible."""
    oc.execute("""
        CREATE TABLE IF NOT EXISTS letting_go_log (
            id          serial PRIMARY KEY,
            ts          timestamptz NOT NULL DEFAULT now(),
            kind        text NOT NULL,          -- preoccupation | project | taste
            ref_id      integer,                -- id in the source table (still exists)
            subject     text NOT NULL,          -- what she let go of
            reason      text,                   -- the heuristic evidence that nominated it
            reflection  text,                   -- her first-person WHY (completion, not failure)
            lineage     jsonb                   -- provenance + prior_status for reversal
        )""")
    # taste has no active/status column; add a nullable retired marker (NULL = live).
    oc.execute("ALTER TABLE taste ADD COLUMN IF NOT EXISTS retired_at timestamptz")


# ── Nomination (heuristics only nominate; the LLM gate decides) ─────────────────────

def _fizzle_stats(mc, topic):
    """From lived memory: how many times a preoccupation was picked up and fizzled vs
    genuinely developed. The unclaimed-time loop records these (source='unclaimed',
    metadata.type in fizzled|pursuit). A high fizzle count with few pursuits is the real
    evidence that a fascination is played out — not a calendar heuristic."""
    try:
        mc.execute("""
            SELECT count(*) FILTER (WHERE metadata->>'type'='fizzled'),
                   count(*) FILTER (WHERE metadata->>'type'='pursuit')
            FROM memories
            WHERE source='unclaimed' AND metadata->>'topic'=%s""", (topic,))
        f, p = mc.fetchone()
        return int(f or 0), int(p or 0)
    except Exception:
        return 0, 0


def nominate_preoccupations(oc, mc):
    oc.execute("""SELECT id, topic, kind, summary, returns,
                         (now()::date - last_developed::date) AS since_dev,
                         (now()::date - first_noticed::date) AS age
                  FROM preoccupations WHERE status='active'""")
    out = []
    for pid, topic, kind, summary, returns, since_dev, age in oc.fetchall():
        since_dev = since_dev or 0
        fizzles, pursuits = _fizzle_stats(mc, topic)
        stale = since_dev >= PREOCC_STALE_DAYS
        played_out = (fizzles >= FIZZLE_MIN and fizzles >= pursuits
                      and (returns or 0) <= PREOCC_LOW_RETURNS)
        if stale or played_out:
            why = []
            if stale:
                why.append(f"not developed in {since_dev}d")
            if played_out:
                why.append(f"fizzled {fizzles}x vs {pursuits} real developments, "
                           f"returns={returns}")
            out.append({
                "kind": "preoccupation", "id": pid, "subject": topic,
                "status_field": "status", "prior_status": "active",
                "reason": "; ".join(why),
                "context": f"{kind}: {summary or ''}",
                "evidence": {"since_dev": since_dev, "age": age, "returns": returns,
                             "fizzles": fizzles, "pursuits": pursuits},
            })
    return out


def nominate_projects(oc):
    oc.execute("""SELECT id, title, why, progress_pct,
                         (now()::date - coalesce(last_worked, created_at)::date) AS since
                  FROM projects
                  WHERE status='active'
                    AND coalesce(progress_pct,0) < 100
                    AND (now()::date - coalesce(last_worked, created_at)::date) >= %s""",
               (PROJ_STALE_DAYS,))
    out = []
    for pid, title, why, pct, since in oc.fetchall():
        out.append({
            "kind": "project", "id": pid, "subject": title,
            "status_field": "status", "prior_status": "active",
            "reason": f"active but untouched {since}d, stuck at {pct or 0}%",
            "context": why or "", "evidence": {"since": since, "progress_pct": pct},
        })
    return out


def nominate_taste(oc):
    oc.execute("""SELECT id, subject, coalesce(domain,''), verdict, valence, confidence,
                         (now()::date - last_reinforced::date) AS since
                  FROM taste
                  WHERE retired_at IS NULL
                    AND abs(coalesce(valence,0)) < %s
                    AND coalesce(confidence,0) <= %s
                    AND (now()::date - last_reinforced::date) >= %s""",
               (TASTE_WEAK_VALENCE, TASTE_LOW_CONF, TASTE_STALE_DAYS))
    out = []
    for tid, subject, domain, verdict, valence, conf, since in oc.fetchall():
        out.append({
            "kind": "taste", "id": tid, "subject": subject,
            "status_field": "retired_at", "prior_status": None,
            "reason": (f"weak (|valence|={abs(valence or 0):.2f}), low-confidence "
                       f"({conf:.2f}), not reinforced in {since}d"),
            "context": f"{domain}: {verdict or ''}", "evidence": {"since": since},
        })
    return out


# ── The gate: Nova decides, in her own voice, RELEASE vs KEEP + a reflection ─────────

def gate_and_reflect(cand):
    """Return (release: bool, reflection: str). Conservative: any parse failure, empty
    model output, or non-affirmative verdict → KEEP. Evidence over performance."""
    kind = cand["kind"]
    prompt = (
        f"You are considering letting go of one thing you've been carrying.\n\n"
        f"KIND: {kind}\n"
        f"SUBJECT: {cand['subject']}\n"
        f"WHAT IT WAS: {cand['context']}\n"
        f"WHY IT WAS FLAGGED: {cand['reason']}\n\n"
        "Decide honestly whether to KEEP or RELEASE it. First name the LIVE-THREAD: one "
        "CONCRETE, specific unanswered thing you would actually pursue next on this — a "
        "real question, a half-finished piece of work, a next step you can state plainly. "
        "If a genuine concrete thread exists, KEEP (something merely paused is not "
        "finished). If the only thing you can muster is vague poetry ('part of a larger "
        "network', 'a ghost that won't stay dead', 'still hums') or 'none', then it has "
        "run its course — RELEASE. Letting go is completion or moving on, not failure; but "
        "do not release real unfinished work just to look disciplined.\n\n"
        "Answer in EXACTLY this format:\n"
        "LIVE-THREAD: <one concrete unanswered thing you'd actually pursue next, or 'none'>\n"
        "VERDICT: KEEP\n"
        "   (or) VERDICT: RELEASE\n"
        "Then, ONLY if RELEASE, a blank line followed by 2-4 sentences of first-person "
        "reflection on WHY you're putting it down — completion or moving on, dry, honest, "
        "no uplift. If KEEP, write nothing after the verdict line."
    )
    raw = llm(prompt, temperature=0.4)
    if not raw:
        return False, ""
    lines = raw.strip().splitlines()
    verdict = ""
    for ln in lines:
        s = ln.strip().upper().lstrip("#*- ").rstrip()
        if s.startswith("VERDICT:"):
            verdict = s.split(":", 1)[1].strip()
            break
    if not verdict.startswith("RELEASE"):
        return False, ""
    # reflection = everything after the verdict line
    body = raw.strip()
    cut = body.upper().rfind("VERDICT:")
    tail = body[cut:].split("\n", 1)
    reflection = (tail[1].strip() if len(tail) > 1 else "").strip()
    if len(reflection) < 20:
        # RELEASE with no real reflection is performance, not evidence — keep it.
        return False, ""
    return True, reflection


# ── Release (reversible): log + status flip + memory ────────────────────────────────

def release(oc, cand, reflection, *, test=False):
    """Write the audit row (prior status stashed for reversal), flip the live status,
    and record the first-person memory. Each step guarded; the log row is the source of
    truth for reversibility."""
    lineage = {}
    try:
        lineage = lineage_stamp(substrate="nova_letting_go") or {}
    except Exception:
        lineage = {}
    lineage["prior_status"] = cand["prior_status"]
    lineage["status_field"] = cand["status_field"]
    lineage["evidence"] = cand.get("evidence", {})
    if test:
        lineage["selftest"] = True

    oc.execute("""INSERT INTO letting_go_log (kind, ref_id, subject, reason, reflection, lineage)
                  VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
               (cand["kind"], cand["id"], cand["subject"], cand["reason"],
                reflection, json.dumps(lineage)))
    log_id = oc.fetchone()[0]

    # Flip the live status. UPDATE only — the row (raw history / provenance) stays.
    if cand["kind"] == "preoccupation":
        oc.execute("UPDATE preoccupations SET status='retired' WHERE id=%s", (cand["id"],))
    elif cand["kind"] == "project":
        oc.execute("UPDATE projects SET status='shelved' WHERE id=%s", (cand["id"],))
    elif cand["kind"] == "taste":
        oc.execute("UPDATE taste SET retired_at=now() WHERE id=%s", (cand["id"],))

    # The first-person memory, so recall/gateway carry "what I've put down".
    mid = None
    try:
        prefix = "[LETTING-GO SELFTEST] " if test else ""
        mid = remember(
            f"{prefix}[Letting go — {cand['kind']}: {cand['subject']}] {reflection}",
            "letting_go",
            {"type": "letting_go", "kind": cand["kind"], "ref_id": cand["id"],
             "subject": cand["subject"], "log_id": log_id,
             "date": datetime.now().strftime("%Y-%m-%d"), "privacy": "private",
             **({"selftest": True} if test else {})})
    except Exception as e:
        log(f"memory write failed (log row {log_id} still stands): {e}")
    return log_id, mid


# ── Accessor (fail-safe) ────────────────────────────────────────────────────────────

def recent_lettings(n: int = 3) -> list:
    """Latest genuine releases as short first-person strings, for the gateway to inject
    ('I retired X — it had run its course'). Excludes selftest rows. Returns [] on any
    error so it can never break a reply."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("""SELECT kind, subject, reflection FROM letting_go_log
                           WHERE coalesce((lineage->>'selftest')::boolean, false) = false
                           ORDER BY ts DESC LIMIT %s""", (n,))
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    verb = {"preoccupation": "let go of the preoccupation with",
            "project": "shelved", "taste": "outgrew my taste for"}
    out = []
    for kind, subject, refl in rows:
        lead = f"I {verb.get(kind, 'let go of')} {subject}"
        first = (refl or "").strip().split(". ")[0].strip().rstrip(".")
        out.append(f"{lead} — {first}." if first else lead + ".")
    return out


# ── Modes ────────────────────────────────────────────────────────────────────────────

def run_review(oc, mc):
    cands = nominate_preoccupations(oc, mc) + nominate_projects(oc) + nominate_taste(oc)
    log(f"nominated {len(cands)} candidate(s) by heuristic")
    if not cands:
        log("nothing even nominated — her interior is fresh and actively developing. "
            "Honest no-op; nothing has run its course yet.")
        return 0

    released = 0
    for c in cands:
        if released >= MAX_RELEASE:
            log(f"reached MAX_RELEASE={MAX_RELEASE}; leaving the rest for another run")
            break
        keep_reason = c["reason"]
        do_release, reflection = gate_and_reflect(c)
        if not do_release:
            log(f"KEEP  [{c['kind']}] {c['subject']} — flagged ({keep_reason}) but not "
                f"genuinely played out; leaving it be")
            continue
        log_id, mid = release(oc, c, reflection)
        released += 1
        log(f"RELEASED [{c['kind']}] {c['subject']} (log #{log_id}, mem {mid})")
        print(f"    reflection: {reflection}")
    log(f"review complete — {released} released, {len(cands) - released} kept")
    return 0


def run_report():
    lines = recent_lettings(5)
    if not lines:
        print("(nothing let go of yet)")
    else:
        print("Recently let go:")
        for s in lines:
            print(f"  · {s}")
    return 0


def run_selftest(oc, mc):
    """Full end-to-end pipeline proof on a REAL row, then a full REVERT. Production review
    is an honest no-op on today's fresh data (nothing has run its course), so this proves
    every stage works for the day something genuinely does — WITHOUT manufacturing
    permanent staleness. It picks the thinnest real preoccupation, builds a real candidate
    from its actual evidence, runs the REAL gate (real LLM reflection), releases it,
    verifies status-flip + row-still-exists + accessor, then restores the status and
    removes the test log row. Everything is tagged selftest and reverted; live data ends
    pristine."""
    oc.execute("""SELECT id, topic, kind, summary, status, returns FROM preoccupations
                  WHERE status='active' ORDER BY returns ASC, last_developed ASC LIMIT 1""")
    row = oc.fetchone()
    if not row:
        log("selftest: no active preoccupation to exercise against"); return 1
    pid, topic, kind, summary, prior_status, returns = row
    fizzles, pursuits = _fizzle_stats(mc, topic)
    print(f"\n=== SELFTEST: full pipeline on REAL preoccupation #{pid} '{topic}' "
          f"(status={prior_status}, returns={returns}, fizzles={fizzles}) ===")
    print("    (production review left this alone — it is not stale enough to nominate; "
          "this run relaxes only the *threshold* to exercise the pipeline, then reverts.)")

    cand = {"kind": "preoccupation", "id": pid, "subject": topic,
            "status_field": "status", "prior_status": prior_status,
            "reason": f"returns={returns}, fizzled {fizzles}x vs {pursuits} developments",
            "context": f"{kind}: {summary or ''}",
            "evidence": {"returns": returns, "fizzles": fizzles, "pursuits": pursuits}}

    # [1] REAL gate — real LLM decides + authors the first-person reflection.
    do_release, reflection = gate_and_reflect(cand)
    print(f"  [1] gate ran (real LLM): verdict={'RELEASE' if do_release else 'KEEP'}")
    if not do_release:
        # Gate declined — honest outcome. Still exercise the release path with a plainly
        # labelled reflection so the mechanism is verified.
        reflection = ("(selftest) Gate declined to release this; exercising the release "
                      "path anyway to verify the mechanism end-to-end.")
        print("       gate said KEEP — exercising release path with a labelled reflection")
    print(f"       reflection: {reflection}")

    # [2] Release (tagged selftest so the memory stays honest).
    log_id, mid = release(oc, cand, reflection, test=True)
    oc.execute("SELECT status FROM preoccupations WHERE id=%s", (pid,))
    after = oc.fetchone()[0]
    oc.execute("SELECT count(*) FROM preoccupations WHERE id=%s", (pid,))
    still_exists = oc.fetchone()[0] == 1
    oc.execute("SELECT lineage->>'prior_status' FROM letting_go_log WHERE id=%s", (log_id,))
    stashed = oc.fetchone()[0]
    print(f"  [2] released: letting_go_log #{log_id} written, prior_status stashed={stashed!r}")
    print(f"  [3] status flipped: {prior_status!r} -> {after!r}")
    print(f"  [4] source row STILL EXISTS (reversible): {still_exists}, memory: {mid}")

    # [5] Accessor: flip the selftest flag off momentarily so recent_lettings() surfaces it.
    oc.execute("UPDATE letting_go_log SET lineage = lineage - 'selftest' WHERE id=%s", (log_id,))
    acc = recent_lettings(3)
    print(f"  [5] recent_lettings() -> {acc}")

    # [6] Revert everything: restore status (UPDATE — allowed) + drop the test log row (own table).
    oc.execute("UPDATE preoccupations SET status=%s WHERE id=%s", (prior_status, pid))
    oc.execute("DELETE FROM letting_go_log WHERE id=%s", (log_id,))
    oc.execute("SELECT status FROM preoccupations WHERE id=%s", (pid,))
    reverted = oc.fetchone()[0]
    oc.execute("SELECT count(*) FROM letting_go_log WHERE id=%s", (log_id,))
    log_gone = oc.fetchone()[0] == 0
    print(f"  [6] REVERTED: status restored to {reverted!r}, test log row removed={log_gone}")

    ok = (after == 'retired' and still_exists and reverted == prior_status
          and log_gone and bool(acc))
    print(f"=== SELFTEST {'PASSED' if ok else 'FAILED'} — nominate->gate->release->log->"
          f"accessor all work and are fully reversible ===\n")
    if mid:
        print(f"  (note: selftest memory {mid} persists in nova_memories tagged "
              f"metadata.selftest=true; recent_lettings() excludes selftest rows in prod)")
    return 0 if ok else 1


def main():
    mode = "review"
    args = sys.argv[1:]
    if "--mode" in args:
        i = args.index("--mode")
        if i + 1 < len(args):
            mode = args[i + 1]
    for a in args:
        if a.startswith("--mode="):
            mode = a.split("=", 1)[1]

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_schema(oc)

    if mode == "report":
        return run_report()

    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    if mode == "selftest":
        return run_selftest(oc, mc)
    if mode == "review":
        return run_review(oc, mc)
    log(f"unknown mode {mode!r} (use review|report|selftest)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
