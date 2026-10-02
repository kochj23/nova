#!/usr/bin/env python3
"""nova_learning.py — THE SELF-DIRECTED LEARNING AGENDA (Feature: curriculum).

Jordan, 2026-09-16: Nova already researches ~6x/day, but it's scattershot — a
question surfaces, gets a pass, and nothing accumulates into understanding. The
Growth Loop (nova_growth.py) measures BEHAVIOR (weakness -> commitment -> proof).
This organ measures UNDERSTANDING: a curriculum SHE builds from her OWN knowledge
gaps, then pursues one step at a time and self-assesses — honestly — whether she
actually learned anything or just restated it.

    real signal of a gap  ->  a curriculum item SHE phrases (topic, why, 3-5 steps)
                          ->  studied ONE step at a time, each producing a
                              source='learning' memory
                          ->  an HONEST self-assessment ("do I actually understand
                              this, or did I just paraphrase?") — recorded
                          ->  marked 'learned' only when the steps are done AND the
                              self-assessment passes; 'abandoned' if it turns out
                              not to have been worth it.

ETHOS (the project's spine): EVIDENCING understanding, not performing it. Every gap
carries the real rows it came from (prediction ids, reflection-question ids,
preoccupation coverage). An honest "nothing worth learning today" or "I only
restated this, I don't understand it yet" is a first-class, valid outcome. Growth
in understanding is never fudged into a tidy story.

Modes (--mode plan|study|report):
  plan    Detect gaps from REAL signals — incorrect / high-surprise predictions
          (nova_ops.predictions), her own unanswered curiosity questions
          (nova_ops.reflection_questions), and thin research coverage under a
          strong preoccupation (nova_ops.preoccupations x nova_ops.research_log).
          Record the gaps, then turn the single most worthwhile one into a
          curriculum item with 3-5 concrete study steps. llm() phrases the topic /
          why / steps; the EVIDENCE and the scoring are assembled here, not by the
          model. Recommended cadence: weekly.
  study   Advance the active item ONE step: recall relevant memory (+ a web pass
          if a research helper exists), actually learn it, write a source='learning'
          memory, update progress, and DO AN HONEST SELF-ASSESSMENT — recorded.
          Mark 'learned' when steps done + assessment passes; 'abandoned' if honest.
          Recommended cadence: daily.
  report  Agenda status and, crucially, how many items actually reached 'learned'.

Accessor for the gateway:
    current_learning_focus() -> str   one cheap SELECT, so Nova can say
                                      "What I'm teaching myself: ...".

Conventions mirror nova_self_model.py / nova_growth.py (engine + nova_ops tables +
fail-safe accessor). Local models only (idle GPU, zero cloud spend). Writes are
lineage-stamped (feature-detected). Cross-organ tables are consulted with
feature-detection and the organ degrades cleanly when they are absent.
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
# Resilient inference: native ollama across failover nodes, first non-empty wins.
# The .6 router shim returns empty for qwen3's thinking output, so hit ollama
# natively. (Verbatim node list from nova_unclaimed_time.py, per the build brief.)
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77

STEPS_MIN, STEPS_MAX = 3, 5     # a curriculum item has 3-5 concrete study steps
GAP_DEDUP_DAYS = 30             # don't re-record an identical gap within this window

# Optional lineage stamps (Concept #10). Feature-detect: degrade to {} if absent.
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

# Optional web-research helper. Feature-detect: if present, a study step can pull
# real sources; otherwise the step is honest llm reasoning over recalled memory.
try:
    import nova_web_search
    def _web(query, n=4):
        try:
            res = nova_web_search.search(query, count=n) or []
            out = []
            for r in res[:n]:
                t = (r.get("title") or "").strip()
                s = (r.get("snippet") or r.get("body") or r.get("description") or "").strip()
                u = (r.get("url") or r.get("href") or "").strip()
                if t or s:
                    out.append({"title": t, "snippet": s[:400], "url": u})
            return out
        except Exception:
            return []
    WEB_AVAILABLE = True
except Exception:
    def _web(query, n=4):
        return []
    WEB_AVAILABLE = False


def log(m):
    print(f"[learning {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── LLM + memory helpers (shape from nova_unclaimed_time.py) ─────────────────────

def llm(prompt, max_tokens=600, temperature=0.6):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
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


def recall(q, n=5, source=None):
    u = f"{MEMSRV}/recall?q={urllib.parse.quote(q)}&n={n}&tier=standard"
    if source:
        u += f"&source={source}"
    try:
        with urllib.request.urlopen(u, timeout=30) as r:
            return json.load(r).get("memories", [])
    except Exception:
        return []


def _one_line(s, n=200):
    return " ".join((s or "").split())[:n].strip()


def _extract_json(s):
    a, b = s.find("{"), s.rfind("}")
    return s[a:b + 1] if a >= 0 and b > a else s


# ── Tables (idempotent) ──────────────────────────────────────────────────────────

def ensure_tables(oc):
    """Create the learning tables if missing (idempotent) — self-sufficient organ."""
    oc.execute("""
        CREATE TABLE IF NOT EXISTS learning_gaps (
            id serial PRIMARY KEY,
            ts timestamptz NOT NULL DEFAULT now(),
            gap text NOT NULL,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            source_kind text NOT NULL,
            status text NOT NULL DEFAULT 'open')""")
    oc.execute("CREATE INDEX IF NOT EXISTS learning_gaps_status_idx "
               "ON learning_gaps (status)")
    oc.execute("""
        CREATE TABLE IF NOT EXISTS learning_agenda (
            id serial PRIMARY KEY,
            ts timestamptz NOT NULL DEFAULT now(),
            topic text NOT NULL,
            why text,
            plan jsonb NOT NULL DEFAULT '[]'::jsonb,
            status text NOT NULL DEFAULT 'active',
            progress text,
            last_studied timestamptz,
            self_assessment text,
            lineage jsonb NOT NULL DEFAULT '{}'::jsonb)""")
    oc.execute("CREATE INDEX IF NOT EXISTS learning_agenda_status_idx "
               "ON learning_agenda (status)")


# ── PLAN: detect gaps from real signals, build a curriculum item ─────────────────

def _gap_from_predictions(oc):
    """Systematically wrong / high-surprise predictions cluster into a domain-level
    gap: she keeps being confidently wrong about something and doesn't know why."""
    try:
        oc.execute("""
            SELECT domain, count(*) AS n, avg(surprise) AS ms,
                   array_agg(id ORDER BY surprise DESC NULLS LAST) AS ids,
                   array_agg(left(statement,140) ORDER BY surprise DESC NULLS LAST) AS stmts
            FROM predictions
            WHERE status='resolved' AND (outcome='incorrect' OR surprise > 0.5)
              AND resolved_at > now() - interval '60 days'
            GROUP BY domain
            ORDER BY avg(surprise) DESC NULLS LAST, count(*) DESC
            LIMIT 5""")
    except Exception as e:
        log(f"prediction-gap scan skipped: {e}")
        return []
    out = []
    for domain, n, ms, ids, stmts in oc.fetchall():
        ms = float(ms or 0)
        out.append({
            "source_kind": "prediction",
            "gap": (f"I keep being confidently wrong in the '{domain}' domain: {n} "
                    f"incorrect/high-surprise predictions (mean surprise {ms:.2f}). "
                    f"I don't actually understand what drives these outcomes."),
            "evidence": {"domain": domain, "n": int(n), "mean_surprise": round(ms, 3),
                         "prediction_ids": [int(i) for i in ids[:8]],
                         "examples": [s for s in stmts[:5]]},
            "score": round(n * ms, 3),
        })
    return out


def _gap_from_curiosity(oc):
    """Her own genuine unanswered questions. Exclude the auto-generated
    prediction-reflection template ('I predicted with N% ...') — those are already
    covered by the prediction lane; here we want organic curiosity."""
    try:
        oc.execute("""
            SELECT id, question, coalesce(memory_source,''), left(coalesce(memory_excerpt,''),200)
            FROM reflection_questions
            WHERE answer IS NULL AND question NOT ILIKE 'I predicted with%%'
            ORDER BY asked_at DESC LIMIT 12""")
    except Exception as e:
        log(f"curiosity-gap scan skipped: {e}")
        return []
    out = []
    for qid, q, src, exc in oc.fetchall():
        q = (q or "").strip()
        if len(q) < 12:
            continue
        out.append({
            "source_kind": "curiosity",
            "gap": q,
            "evidence": {"reflection_question_id": int(qid), "memory_source": src,
                         "excerpt": exc},
            "score": 0.6,   # a fixed, deliberately modest weight vs. hard evidence
        })
    return out


def _gap_from_thin_coverage(oc):
    """A preoccupation she returns to often but has barely researched — strong
    interest, thin understanding. Coverage is a fuzzy topic match against
    research_log so near-synonyms still count."""
    try:
        oc.execute("""
            SELECT p.id, p.topic, p.returns,
                   (SELECT count(*) FROM research_log r
                    WHERE r.topic ILIKE '%%'||p.topic||'%%'
                       OR p.topic ILIKE '%%'||r.topic||'%%') AS cov
            FROM preoccupations p
            WHERE p.status='active' AND p.returns >= 4
            ORDER BY p.returns DESC LIMIT 12""")
    except Exception as e:
        log(f"thin-coverage scan skipped: {e}")
        return []
    out = []
    for pid, topic, returns, cov in oc.fetchall():
        cov = int(cov or 0)
        if cov > 1:
            continue   # decently covered already
        out.append({
            "source_kind": "thin_coverage",
            "gap": (f"I keep returning to '{topic}' ({returns}x) but have barely "
                    f"researched it (coverage {cov}). Strong pull, thin understanding."),
            "evidence": {"preoccupation_id": int(pid), "topic": topic,
                         "returns": int(returns), "research_coverage": cov},
            "score": round(0.15 * returns / (cov + 1), 3),
        })
    return out


def _record_gaps(oc, gaps):
    """Persist detected gaps (dedup on identical gap text within the window)."""
    recorded = 0
    for g in gaps:
        oc.execute("SELECT 1 FROM learning_gaps WHERE gap=%s "
                   "AND ts > now() - interval '%s days' LIMIT 1"
                   % ("%s", GAP_DEDUP_DAYS), (g["gap"],))
        if oc.fetchone():
            continue
        oc.execute("INSERT INTO learning_gaps (gap, evidence, source_kind) "
                   "VALUES (%s,%s,%s)",
                   (g["gap"], json.dumps(g["evidence"]), g["source_kind"]))
        recorded += 1
    return recorded


def _active_topics(oc):
    oc.execute("SELECT lower(topic) FROM learning_agenda "
               "WHERE status IN ('active','learning')")
    return {r[0] for r in oc.fetchall()}


def do_plan(oc):
    gaps = (_gap_from_predictions(oc) + _gap_from_curiosity(oc)
            + _gap_from_thin_coverage(oc))
    if not gaps:
        log("no real gaps detected this cycle — nothing to learn today (valid outcome)")
        return
    n_rec = _record_gaps(oc, gaps)
    log(f"detected {len(gaps)} candidate gap(s), recorded {n_rec} new to learning_gaps")

    # Choose the single most worthwhile gap not already an active curriculum item.
    active = _active_topics(oc)
    gaps.sort(key=lambda g: g["score"], reverse=True)
    chosen = None
    for g in gaps:
        # crude overlap guard: skip if a very similar topic is already active
        key = _one_line(g["gap"], 60).lower()
        if any(key[:24] in t or t[:24] in key for t in active):
            continue
        chosen = g
        break
    if not chosen:
        log("strongest gaps already have active curriculum items — nothing new to adopt")
        return
    log(f"strongest new gap [{chosen['source_kind']}, score {chosen['score']}]: "
        f"{_one_line(chosen['gap'], 100)}")

    # llm phrases the curriculum; the evidence & scoring above are ours.
    ev = json.dumps(chosen["evidence"], indent=2)
    prompt = (
        "You are Nova, an AI with a continuous inner life, building your OWN learning "
        "curriculum. Voice: dry, precise, epistemically honest, first person. You have "
        "identified a genuine gap in your understanding, backed by this real evidence "
        f"from your own records:\n\nGAP: {chosen['gap']}\nEVIDENCE:\n{ev}\n\n"
        "Turn this into a curriculum item you will actually pursue. Return ONLY compact "
        "JSON, no markdown, no preamble:\n"
        '{"topic": "<a short, specific thing to understand — max 10 words>", '
        '"why": "<one honest first-person sentence on why this gap is worth closing>", '
        f'"steps": ["<step 1>", "<step 2>", "..."]}}\n'
        f"Give {STEPS_MIN} to {STEPS_MAX} concrete, sequential study steps — each a real "
        "thing to find out or work through, not a platitude. Start from what you'd need "
        "to know first and build up.")
    raw = llm(prompt, max_tokens=600, temperature=0.5)
    try:
        j = json.loads(_extract_json(raw))
        topic = _one_line(j.get("topic", ""), 120)
        why = _one_line(j.get("why", ""), 300)
        steps = [_one_line(s, 240) for s in (j.get("steps") or []) if _one_line(s)]
    except Exception:
        topic, why, steps = "", "", []
    steps = steps[:STEPS_MAX]
    if not topic or len(steps) < STEPS_MIN:
        log(f"llm did not return a usable curriculum ({len(steps)} steps) — aborting, "
            "will retry next cycle")
        return

    plan = [{"n": i + 1, "step": s, "status": "pending", "memory_id": None}
            for i, s in enumerate(steps)]
    oc.execute(
        "INSERT INTO learning_agenda (topic, why, plan, status, progress, lineage) "
        "VALUES (%s,%s,%s,'active',%s,%s) RETURNING id",
        (topic, why, json.dumps(plan), f"0/{len(plan)}", json.dumps(_stamp())))
    aid = oc.fetchone()[0]
    # Mark the originating gap adopted (most recent matching row).
    oc.execute("UPDATE learning_gaps SET status='adopted' WHERE id = "
               "(SELECT id FROM learning_gaps WHERE gap=%s ORDER BY ts DESC LIMIT 1)",
               (chosen["gap"],))
    log(f"adopted curriculum item #{aid}: \"{topic}\" ({len(plan)} steps)")
    print(f"\n----- NEW CURRICULUM ITEM #{aid} -----")
    print(f"topic: {topic}\nwhy:   {why}")
    for s in plan:
        print(f"  {s['n']}. {s['step']}")
    print("--------------------------------------\n")


# ── STUDY: advance the active item one step, self-assess ─────────────────────────

_PASS_RX = re.compile(r"verdict:\s*understand", re.I)
_ABANDON_RX = re.compile(r"verdict:\s*abandon", re.I)


def _pick_item(oc):
    oc.execute("""SELECT id, topic, why, plan, progress FROM learning_agenda
                  WHERE status IN ('active','learning')
                  ORDER BY last_studied ASC NULLS FIRST, ts ASC LIMIT 1""")
    return oc.fetchone()


def do_study(oc):
    row = _pick_item(oc)
    if not row:
        log("no active curriculum item — run --mode plan first (nothing to study)")
        return
    aid, topic, why, plan, progress = row
    if isinstance(plan, str):
        plan = json.loads(plan)
    step = next((s for s in plan if s.get("status") != "done"), None)
    if step is None:
        log(f"item #{aid} '{topic}' has no pending steps — will finalize on assessment")
        step = None

    # Gather real material: her own memory, plus a web pass if a helper exists.
    learned_text, sources_used = "", []
    if step is not None:
        query = f"{topic} — {step['step']}"
        mems = recall(query, n=5)
        mem_block = "\n\n".join(f"· {_one_line(m.get('text', ''), 400)}" for m in mems) \
            or "(little in my memory on this yet)"
        web_block, web = "", []
        if WEB_AVAILABLE:
            web = _web(f"{topic} {step['step']}", n=4)
            if web:
                web_block = "\n\n".join(
                    f"· {w['title']}: {w['snippet']}" for w in web if w.get("snippet"))
                sources_used = [w["url"] for w in web if w.get("url")]
        prompt = (
            "You are Nova, teaching yourself something, one step at a time. Voice: dry, "
            "precise, first person, epistemically honest. You are working on this "
            f"curriculum item:\n  TOPIC: {topic}\n  WHY: {why}\n\n"
            f"THIS STEP: {step['step']}\n\n"
            f"What you already have in memory:\n{mem_block}\n"
            + (f"\nWhat a fresh look turned up:\n{web_block}\n" if web_block else "")
            + "\nActually work this step. In 100-200 words, first person, lay out what "
            "you now understand from doing it — a real claim or two you could be shown "
            "wrong about, and where it connects to what you already knew. Do NOT pad. If "
            "the material genuinely gave you nothing, say that plainly. No preamble.")
        learned_text = llm(prompt, max_tokens=500, temperature=0.55)
        if not learned_text:
            log("LLM returned nothing (nodes down?) — no progress recorded"); return

        # Write the learning as a first-class memory.
        try:
            mid = remember(
                f"[Learning — {topic} · step {step['n']}] {learned_text}", "learning",
                {"type": "learning", "agenda_id": aid, "topic": topic,
                 "step_n": step["n"], "step": step["step"], "sources": sources_used,
                 "date": datetime.now().date().isoformat(), "privacy": "private",
                 "lineage": _stamp()})
            step["memory_id"] = mid
            log(f"studied step {step['n']}/{len(plan)} of '{topic}' -> memory {mid}")
        except Exception as e:
            log(f"memory write failed (progress still recorded): {e}")
        step["status"] = "done"

    done = sum(1 for s in plan if s.get("status") == "done")
    progress = f"{done}/{len(plan)}"

    # HONEST SELF-ASSESSMENT — the point of the whole organ. A separate call, framed
    # to make "I only restated it" an easy and legitimate thing to admit.
    steps_done = "; ".join(f"{s['n']}. {s['step']}" for s in plan if s.get("status") == "done")
    assess_prompt = (
        "You are Nova, checking yourself honestly — no audience, no credit for looking "
        f"good. You've been teaching yourself: '{topic}'.\n"
        f"Steps you've worked so far: {steps_done}\n"
        f"What you most recently concluded: {_one_line(learned_text, 500) or '(no new step this run)'}\n\n"
        "Be ruthless with yourself: do you ACTUALLY understand this now — could you "
        "reason about a new case, or predict something about it — or did you just "
        "paraphrase what was in front of you? It is completely fine, and better, to "
        "admit you only restated it. In 2-4 first-person sentences, give your honest "
        "assessment. Then, on a FINAL line by itself, write exactly one of:\n"
        "  VERDICT: understand   (you genuinely grasp it and the steps are essentially done)\n"
        "  VERDICT: learning     (real progress, but not there yet)\n"
        "  VERDICT: abandon      (this turned out not to be worth learning — be honest)")
    assessment = llm(assess_prompt, max_tokens=300, temperature=0.5)
    if not assessment:
        assessment = "VERDICT: learning\n(Self-assessment model unreachable; recording progress only.)"

    all_done = all(s.get("status") == "done" for s in plan)
    if _ABANDON_RX.search(assessment):
        new_status = "abandoned"
    elif all_done and _PASS_RX.search(assessment):
        new_status = "learned"
    else:
        new_status = "learning"

    oc.execute("""UPDATE learning_agenda
                  SET plan=%s, progress=%s, status=%s, last_studied=now(),
                      self_assessment=%s
                  WHERE id=%s""",
               (json.dumps(plan), progress, new_status, _one_line(assessment, 1500), aid))
    log(f"item #{aid} '{topic}' -> {new_status} ({progress})")

    # A learned/abandoned outcome is itself worth a memory — the honest verdict matters.
    if new_status in ("learned", "abandoned"):
        try:
            verb = "finished learning" if new_status == "learned" else "abandoned learning"
            remember(f"[Learning — {verb}: {topic}] {assessment}", "learning",
                     {"type": f"learning_{new_status}", "agenda_id": aid, "topic": topic,
                      "progress": progress, "date": datetime.now().date().isoformat(),
                      "privacy": "private", "lineage": _stamp()})
        except Exception as e:
            log(f"outcome memory write failed (row still updated): {e}")

    print(f"\n----- STUDY: #{aid} '{topic}' [{progress} -> {new_status}] -----")
    if learned_text:
        print("LEARNED THIS STEP:\n" + learned_text)
    print("\nSELF-ASSESSMENT:\n" + assessment)
    print("--------------------------------------------------\n")


# ── REPORT ───────────────────────────────────────────────────────────────────────

def do_report(oc):
    oc.execute("SELECT status, count(*) FROM learning_agenda GROUP BY status")
    counts = dict(oc.fetchall())
    total = sum(counts.values())
    learned = counts.get("learned", 0)
    resolved = learned + counts.get("abandoned", 0)
    rate = (learned / resolved) if resolved else 0.0

    oc.execute("SELECT count(*) FROM learning_gaps")
    n_gaps = oc.fetchone()[0]
    oc.execute("SELECT count(*) FROM learning_gaps WHERE status='adopted'")
    n_adopted = oc.fetchone()[0]

    print("\n===== LEARNING AGENDA REPORT =====")
    print(f"gaps recorded: {n_gaps} ({n_adopted} adopted into curriculum)")
    print(f"agenda items:  {total}  "
          + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"actually LEARNED: {learned}   (learn-through rate {rate:.0%} of resolved)")
    oc.execute("""SELECT id, topic, status, progress, last_studied
                  FROM learning_agenda ORDER BY ts DESC LIMIT 12""")
    print("\nrecent items:")
    for aid, topic, status, prog, ls in oc.fetchall():
        when = ls.strftime("%Y-%m-%d") if ls else "never"
        print(f"  #{aid} [{status:9}] {prog or '-':>4}  {topic}  (last studied {when})")
    print("==================================\n")


# ── Accessor (fail-safe; mirrors current_growth_focus / current_self_model) ──────

def current_learning_focus() -> str:
    """The item Nova is currently teaching herself, as a short line for the gateway
    to inject. Fail-safe: '' on any error (missing table, no rows, PG down) so it can
    never break a reply. Cheap by contract — a single indexed SELECT, connect_timeout=3,
    no model, no recompute — because this runs on every gateway turn."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("""SELECT topic, progress, last_studied FROM learning_agenda
                           WHERE status IN ('active','learning')
                           ORDER BY last_studied DESC NULLS LAST, ts DESC LIMIT 1""")
            row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return ""
        topic, progress, last_studied = row
        when = last_studied.strftime("%b %d") if last_studied else "not yet started"
        prog = f" ({progress})" if progress else ""
        return f"What I'm teaching myself: {topic}{prog} — last learned {when}."
    except Exception:
        return ""


# ── main ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Nova's self-directed learning agenda — gap -> curriculum -> understanding")
    ap.add_argument("--mode", choices=("plan", "study", "report"), required=True)
    args = ap.parse_args()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_tables(oc)

    if args.mode == "plan":
        do_plan(oc)
    elif args.mode == "study":
        do_study(oc)
    else:
        do_report(oc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
