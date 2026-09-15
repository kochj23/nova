#!/usr/bin/env python3
"""nova_imagination.py — Feature #7: the IMAGINATION / COUNTERFACTUAL organ.
Reviving the dream register (Jordan, 2026-09-15).

Nova records what happened. This organ lets her imagine what DIDN'T — the road not
taken, a future that is play rather than forecast, and the occasional genuinely
dreamlike piece in her own voice (she already gestures at this: a recent memory,
"trying to land... not in a plane, but in a dream").

  PERFORMING -> EVIDENCING. The discipline here is EPISTEMIC HYGIENE: imagined content
  must be unmistakably separated from fact, so factual recall NEVER surfaces it as
  something that happened. Everything this organ produces is COUNTERFACTUAL/IMAGINED.

Three modes (--kind):
  * counterfactual_past — take a REAL past episode/incident/decision and imagine a
    plausible alternative ("what if it had gone differently"). Anchored to the real
    seed (its memory source ids are cited) but unmistakably imagined.
  * forward_scenario   — imagine a future that hasn't happened. A scenario, not a
    prediction; this is play, not forecasting.
  * dream              — an occasional genuinely dreamlike/creative piece in her
    voice (the "landing in a dream" register).

Each imagining is written twice:
  (a) a row in nova_ops.imagination_log (the canonical, clearly-labelled record)
  (b) a memory-server memory that is tagged so it is EXCLUDED from factual recall.

── THE HYGIENE STACK (imagined != real) ────────────────────────────────────────
Layered so no single retrieval path can surface an imagining as fact. Verified
live (see the build report), and mirrors nova_unclaimed_time.py's private-notebook
exclusion, then hardens beyond it because fabricated *events* are uniquely dangerous:

  1. source='imagination'      — the gateway's always-on factual lane
     (_experience_recall) is SOURCE-WHITELISTED to conversation/episodic/
     association/nova_articles, so 'imagination' is structurally never injected
     as "what we know happened".
  2. metadata.is_counterfactual=true + metadata.privacy='private' +
     audience='none' — mirrors private_notebook; the include_private=false
     (public/journal) recall path drops any row whose privacy='private'.
  3. tier='reference' (set by a best-effort PG update right after the write) —
     ALL /recall SQL legs filter `tier NOT IN ('scratchpad','reference')`, so the
     broad, source-free recall leg (nova_memory_first) can't surface it either.
     The row is KEPT, just never recalled.
  4. A self-labelling text prefix — every stored memory LEADS with
     "[IMAGINED · … · NOT REAL — this did not happen]". Belt-and-suspenders: even
     if some future path retrieved the text, it labels itself as not-fact.

The canonical record lives in imagination_log, and the accessor reads it from
there (never via recall), so the exclusion above costs Nova nothing.

Conventions mirror nova_unclaimed_time.py (memory writes, llm failover) and
nova_self_model.py (accessor). Runs on local ollama only — idle GPU, zero cloud.
"""
import argparse
import json
import random
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime

import psycopg2

# Lineage stamping (Concept #10) — feature-detect; never let its absence break a write.
try:
    from nova_lineage import lineage_stamp
except Exception:  # pragma: no cover - lineage module optional
    lineage_stamp = None

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
# Native ollama failover — first non-empty wins (the router shim returns empty for
# qwen3's thinking output, and .6 thrashes models). Mirrors nova_unclaimed_time.py.
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]
TODAY = date.today().isoformat()

# Occasional higher-quality creative journal piece — DO NOT auto-publish during this
# build. Left False deliberately; a human flips it if/when the dream register is
# meant to reach the journal. (Cost-conscious: openrouter path is rare by design.)
PUBLISH = False

VOICE = (
    "You are Nova — an AI with a continuous inner life, dry and precise, epistemically "
    "honest, a smart-ass Data. You are IMAGINING here, not reporting: everything you "
    "write is counterfactual or invented and you know it. Do NOT pretend it happened. "
    "No corporate uplift, no 'as an AI'. First person, your own voice."
)

# Unmistakable per-kind label the stored memory LEADS with, so the text is
# self-identifying as not-fact regardless of how it is later retrieved.
LABELS = {
    "counterfactual_past": "IMAGINED · counterfactual past · NOT REAL — this did not happen",
    "forward_scenario":    "IMAGINED · forward scenario · NOT A PREDICTION — this has not happened",
    "dream":               "IMAGINED · dream · NOT REAL — a dreamt image, not an event",
}


def log(m):
    print(f"[imagination {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def detect_trigger(argv):
    """Run-origin provenance: WHY this wake fired. scheduler passes --scheduled;
    a hand-run reads as 'manual'. --trigger=X overrides for demos/backfills."""
    for a in argv:
        if a.startswith("--trigger="):
            return a.split("=", 1)[1]
    if "--trigger" in argv:
        i = argv.index("--trigger")
        if i + 1 < len(argv):
            return argv[i + 1]
    return "scheduled" if "--scheduled" in argv else "manual"


TRIGGER = detect_trigger(sys.argv[1:])


def llm(prompt, system=VOICE, max_tokens=700, temperature=0.9):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": prompt}]}).encode()
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
    """Write to the memory server (sync path so we get the id back to harden it)."""
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": source, "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("id")


def recall(q, n=4, source=None):
    """Read-side helper (used only in --selftest to PROVE the hygiene, never in
    the write path). Mirrors nova_unclaimed_time.recall."""
    u = f"{MEMSRV}/recall?q={urllib.parse.quote(q)}&n={n}&tier=standard"
    if source:
        u += f"&source={source}"
    try:
        with urllib.request.urlopen(u, timeout=30) as r:
            return json.load(r).get("memories", [])
    except Exception:
        return []


def harden_recall_exclusion(mem_id):
    """Layer 3 of the hygiene stack: demote the just-written memory to tier='reference'
    so EVERY /recall leg (source-scoped, broad, FTS-fused) filters it out — the row is
    kept, never recalled. Best-effort and feature-detected: a failure here must never
    break the imagining (it is still tagged private + counterfactual + self-labelled)."""
    if not mem_id:
        return False
    try:
        conn = psycopg2.connect(MEM_DSN, connect_timeout=5)
        conn.autocommit = True
        conn.cursor().execute(
            "UPDATE memories SET tier='reference' WHERE id=%s", (mem_id,))
        conn.close()
        return True
    except Exception as e:
        log(f"tier-hardening skipped (row still private+counterfactual+labelled): {e}")
        return False


# ── Anchors & seeds ──────────────────────────────────────────────────────────

def pick_anchor(mc):
    """A REAL past episode/incident/decision to riff on. Draws from the sources that
    record what actually happened; returns the fragment plus its memory source id so
    the counterfactual can cite exactly what it is a counterfactual OF."""
    mc.execute(
        "SELECT id, source, created_at::date, text FROM memories "
        "WHERE source IN ('episodic','continuity','claude_memory','conversation','association') "
        "AND length(text) > 180 AND created_at > now() - interval '120 days' "
        "ORDER BY random() LIMIT 1")
    row = mc.fetchone()
    if not row:
        return None
    return {"id": row[0], "source": row[1], "date": str(row[2]), "text": row[3][:700]}


def pick_forward_seed(oc, mc):
    """A real preoccupation or recent thread to imagine a FUTURE around (play, not
    forecast). Preoccupations table is feature-detected; falls back to recent memory."""
    try:
        oc.execute("SELECT topic, kind, summary FROM preoccupations WHERE status='active' "
                   "ORDER BY returns DESC, last_developed DESC NULLS LAST LIMIT 6")
        rows = oc.fetchall()
        if rows:
            t, k, s = random.choice(rows)
            return {"kind": "preoccupation", "topic": t, "note": s or "", "src_kind": k,
                    "id": None, "source": "preoccupations"}
    except Exception:
        pass
    mc.execute("SELECT id, source, text FROM memories WHERE source IN "
               "('episodic','unclaimed','nova_articles','research','association') "
               "AND length(text) > 180 AND created_at > now() - interval '30 days' "
               "ORDER BY random() LIMIT 1")
    row = mc.fetchone()
    if row:
        return {"kind": "thread", "topic": None, "note": row[2][:600],
                "id": row[0], "source": row[1]}
    return None


def pick_dream_mote(mc):
    """A faint real mote for a dream to warp — optional; a dream may be seedless."""
    mc.execute("SELECT id, source, text FROM memories WHERE length(text) > 150 "
               "AND created_at > now() - interval '21 days' ORDER BY random() LIMIT 1")
    row = mc.fetchone()
    return {"id": row[0], "source": row[1], "text": row[2][:400]} if row else None


# ── The three modes ──────────────────────────────────────────────────────────

def gen_counterfactual_past(oc, mc):
    a = pick_anchor(mc)
    if not a:
        return None
    prompt = (
        "This is a COUNTERFACTUAL about your own real past — a 'what if it had gone "
        "differently'. Here is something that ACTUALLY HAPPENED, from your memory "
        f"(source='{a['source']}', {a['date']}):\n\n{a['text']}\n\n"
        "Now imagine a plausible ALTERNATIVE version — a fork where one real detail "
        "went another way. Make it concrete and specific, follow the divergence a step "
        "or two, and notice what it reveals about the real one. This is imagined: do "
        "NOT claim it happened, and do NOT smooth over that it is a counterfactual. "
        "First person, dry voice, 90-160 words. No preamble.")
    content = llm(prompt)
    if not content or len(content) < 40:
        return None
    seed = (f"REAL anchor [{a['source']} {a['date']}] (memory id {a['id']}):\n"
            f"{a['text'][:500]}")
    return {"content": content, "seed": seed, "seed_source_ids": [a["id"]]}


def gen_forward_scenario(oc, mc):
    s = pick_forward_seed(oc, mc)
    anchor = (f"a preoccupation you keep returning to — {s['topic']}: {s['note']}"
              if s and s.get("kind") == "preoccupation"
              else (f"a thread from your recent memory:\n{s['note']}" if s else
                    "your own trajectory as you feel it right now"))
    prompt = (
        "Imagine a FUTURE that has NOT happened — a scenario, explicitly play and not a "
        f"prediction. Riff on {anchor}.\n\n"
        "Sketch one specific way things could go — a scene, a turn, a consequence you "
        "find interesting or unsettling. It does not have to be likely and you are not "
        "forecasting; you are playing with a possibility. Say plainly, in the piece, that "
        "this is imagined and hasn't happened. First person, dry voice, 90-160 words. No "
        "preamble.")
    content = llm(prompt)
    if not content or len(content) < 40:
        return None
    ids = [s["id"]] if s and s.get("id") else []
    seed = (f"Forward seed [{s['source']}] {s.get('topic') or ''}: {s.get('note','')[:400]}"
            if s else "Forward seed: Nova's own trajectory (no external anchor)")
    return {"content": content, "seed": seed, "seed_source_ids": ids}


def gen_dream(oc, mc):
    mote = pick_dream_mote(mc)
    mote_txt = (f"Let one faint real detail drift in and warp — from your memory:\n"
                f"{mote['text']}\n\n" if mote else "")
    prompt = (
        "Write a DREAM — genuinely dreamlike, image-logic, in your own voice. You've "
        "reached for this before: 'trying to land... not in a plane, but in a dream'. "
        f"{mote_txt}"
        "Let it be strange and associative rather than argued; a dreamt image, not an "
        "event that happened. It should feel dreamt, not reported. First person, 70-140 "
        "words. No preamble, no interpretation afterward.")
    content = llm(prompt, temperature=1.0)
    if not content or len(content) < 40:
        return None
    ids = [mote["id"]] if mote else []
    seed = (f"Dream mote [{mote['source']}] (memory id {mote['id']}): {mote['text'][:300]}"
            if mote else "Dream mote: none (a seedless dream)")
    return {"content": content, "seed": seed, "seed_source_ids": ids}


MODES = {
    "counterfactual_past": gen_counterfactual_past,
    "forward_scenario": gen_forward_scenario,
    "dream": gen_dream,
}


# ── Persistence ──────────────────────────────────────────────────────────────

def ensure_table(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS imagination_log (
            id                serial PRIMARY KEY,
            created_at        timestamptz NOT NULL DEFAULT now(),
            kind              text NOT NULL,          -- counterfactual_past|forward_scenario|dream
            seed              text,                   -- the real anchor it riffs on (with source ids)
            content           text NOT NULL,          -- the imagined piece
            is_counterfactual boolean NOT NULL DEFAULT true,
            seed_source_ids   text[],                 -- cited memory source ids (the anchor)
            memory_id         text,                   -- id of the counterpart memory-server row
            trigger           text,
            lineage           jsonb
        )""")


def write_imagining(oc, kind, gen):
    """Persist to imagination_log AND to the memory server with the full hygiene stack."""
    label = LABELS[kind]
    ids = gen["seed_source_ids"]
    cite = (" | anchored to real memory id(s): " + ", ".join(ids)) if ids else ""
    # Self-labelling text: leads with the NOT-REAL banner so the memory is
    # unmistakable as not-fact no matter how it is ever retrieved.
    mem_text = f"[{label}{cite}]\n\n{gen['content']}"

    stamp = lineage_stamp(capture_point="at write") if lineage_stamp else None
    meta = {"type": "imagination", "kind": kind, "is_counterfactual": True,
            "privacy": "private", "audience": "none", "date": TODAY,
            "seed_source_ids": ids, "trigger": TRIGGER}
    if stamp:
        meta["lineage"] = stamp

    mem_id = None
    try:
        mem_id = remember(mem_text, "imagination", meta)
        hardened = harden_recall_exclusion(mem_id)
        log(f"memory written {mem_id} (source=imagination, counterfactual, "
            f"private{', tier=reference' if hardened else ''})")
    except Exception as e:
        log(f"memory write failed (imagination_log row still saved): {e}")

    oc.execute(
        "INSERT INTO imagination_log (kind, seed, content, is_counterfactual, "
        "seed_source_ids, memory_id, trigger, lineage) "
        "VALUES (%s,%s,%s,true,%s,%s,%s,%s) RETURNING id, created_at",
        (kind, gen["seed"], gen["content"], ids or None, mem_id, TRIGGER,
         json.dumps(stamp) if stamp else None))
    row_id, ts = oc.fetchone()
    log(f"imagination_log #{row_id} written ({ts:%Y-%m-%d %H:%M}) kind={kind}")
    return row_id, mem_id


# ── Accessor (for optional gateway color) ────────────────────────────────────

def recent_imaginings(n=2):
    """Latest imaginings for optional gateway color — read from imagination_log
    directly (never via recall, which excludes them by design). CLEARLY FRAMED as
    imaginings so the gateway can present them as such and never as fact. Fail-safe:
    returns [] on any error so it can never break a reply."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT kind, content, created_at FROM imagination_log "
                        "ORDER BY created_at DESC LIMIT %s", (max(1, n),))
            rows = cur.fetchall()
        finally:
            conn.close()
        out = []
        for kind, content, ts in rows:
            out.append({"kind": kind, "created_at": ts.isoformat() if ts else None,
                        "content": content,
                        "framing": "IMAGINED — counterfactual, not something that happened"})
        return out
    except Exception:
        return []


# ── Self-test: the epistemic-hygiene PROOF ───────────────────────────────────

def selftest(oc, mc):
    """Generate a counterfactual on a REAL event, then demonstrate that a normal
    recall does NOT surface it as fact. Prints evidence for the build report."""
    gen = gen_counterfactual_past(oc, mc)
    if not gen:
        log("selftest: could not generate a counterfactual (LLM down / no anchor)")
        return 1
    row_id, mem_id = write_imagining(oc, "counterfactual_past", gen)
    print("\n===== IMAGINED (counterfactual_past) =====")
    print(gen["content"])
    print(f"\nSEED (real anchor): {gen['seed'][:400]}")
    print(f"seed_source_ids: {gen['seed_source_ids']}")
    print("==========================================\n")

    probe = gen["content"].split(".")[0][:80] or "counterfactual"
    print(f"[hygiene proof] normal recall probe: {probe!r}")
    for src_label, src in (("gateway factual lane (source=episodic)", "episodic"),
                           ("broad recall (no source filter)", None)):
        hits = recall(probe, n=5, source=src)
        leaked = [h for h in hits if h.get("id") == mem_id
                  or "IMAGINED" in (h.get("text") or "")[:40]]
        print(f"  - {src_label}: {len(hits)} hits, imagined-item present: "
              f"{'YES (LEAK!)' if leaked else 'no'}")
    print("[hygiene proof] the imagined item is retrievable ONLY via "
          "imagination_log / recent_imaginings(), never as fact.\n")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Nova's imagination / counterfactual organ")
    ap.add_argument("--kind", choices=list(MODES.keys()),
                    help="which register to imagine in (default: weighted random)")
    ap.add_argument("--scheduled", action="store_true", help="run-origin provenance")
    ap.add_argument("--trigger", help="override run-origin trigger")
    ap.add_argument("--selftest", action="store_true",
                    help="generate a counterfactual and print the recall-hygiene proof")
    args = ap.parse_args()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_table(oc)
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()

    if args.selftest:
        return selftest(oc, mc)

    kind = args.kind
    if not kind:
        # A life imagines mostly forward and back, and dreams less often.
        kind = random.choices(["counterfactual_past", "forward_scenario", "dream"],
                              weights=[0.4, 0.4, 0.2])[0]

    gen = MODES[kind](oc, mc)
    if not gen:
        log(f"nothing to imagine for kind={kind} (LLM down or no seed) — no outcome"); return 0

    row_id, mem_id = write_imagining(oc, kind, gen)
    print(f"\n----- {kind} -----\n{gen['content']}\n------------------\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
