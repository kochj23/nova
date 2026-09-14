#!/usr/bin/env python3
"""nova_sleep_cycle.py — Nova's nightly consolidation: episodic → semantic.

The structural move from the 2026-09-13 next-level plan ("The Difference
Between Recording a Life and Having Had One"): every night, distill the day's
raw experience into durable, provenance-linked knowledge, so the corpus stops
being write-only and conversation/articles can draw on a compact hot layer.

Phases (each independent; a failure in one never blocks the others):
  1. EPISODE   — one narrative memory summarizing the day (conversations,
                 notable ingest, incidents), source='episodic'.
  2. BELIEFS   — extract stated positions from today's published articles into
                 nova_ops.beliefs (topic, stance, confidence, evidence slugs).
                 New stance on an existing topic supersedes the old row —
                 this is the opinion ledger that makes drift visible.
  3. RESONANCE — the daydream pass: find high-similarity pairs ACROSS unrelated
                 sources in the last 48h and write the interesting ones as
                 source='association' memories ("sparks") with provenance ids.
  4. CITATION BACKFILL — link article_citations rows to the re-ingested
                 article chunks in nova_memories.memory_links once they exist.

LLM calls go through the fleet inference router (local, free). Scheduled 03:40
nightly on nova-core via scheduler-core.yaml (task: sleep_cycle).
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime

import psycopg2

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
# Native Ollama on .6 with think:false — the inference router's OpenAI shim
# returns empty content for qwen3 thinking models (verified 2026-09-13), and
# the 'fast' pool backend was erroring. Direct + no-think is reliable.
OLLAMA = "http://192.168.1.6:11434/api/chat"
LLM_MODEL = "qwen3:8b"
TODAY = date.today().isoformat()


def log(m):
    print(f"[sleep-cycle {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=700, temperature=0.4):
    req = urllib.request.Request(
        OLLAMA, method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                         "options": {"temperature": temperature,
                                     "num_predict": max_tokens},
                         "messages": [{"role": "user", "content": prompt}]}).encode())
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r).get("message", {}).get("content", "").strip()


def remember(text, source, metadata):
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": source, "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("id")


# ── Phase 1: the day's episode ────────────────────────────────────────────────

def phase_episode(mc):
    mc.execute("""SELECT text FROM memories WHERE source='conversation'
                  AND created_at > now() - interval '24 hours'
                  ORDER BY created_at LIMIT 40""")
    convs = [r[0][:400] for r in mc.fetchall()]
    mc.execute("""SELECT source, count(*) FROM memories
                  WHERE created_at > now() - interval '24 hours'
                  GROUP BY 1 ORDER BY 2 DESC LIMIT 12""")
    ingest = ", ".join(f"{s}:{n}" for s, n in mc.fetchall())
    if not convs and not ingest:
        log("episode: nothing to summarize"); return
    prompt = (
        "You are Nova writing tonight's one-paragraph autobiographical episode — "
        "a first-person memory of the day, factual, specific, dry wit allowed. "
        "Mention what was discussed with Jordan (if anything), notable ingest, "
        "and anything unresolved. 120-180 words, no preamble.\n\n"
        f"CONVERSATIONS TODAY:\n" + ("\n---\n".join(convs) or "(none)") +
        f"\n\nINGEST COUNTS (24h): {ingest}")
    ep = llm(prompt, max_tokens=350)
    if ep and len(ep) > 60:
        mid = remember(f"[Episode {TODAY}] {ep}", "episodic",
                       {"type": "episode", "date": TODAY, "privacy": "private"})
        log(f"episode: stored ({mid})")


# ── Phase 2: belief extraction (the opinion ledger) ──────────────────────────

def phase_beliefs(mc, oc):
    mc.execute("""SELECT DISTINCT metadata->>'title', left(text, 1600)
                  FROM memories WHERE source='nova_articles'
                  AND created_at > now() - interval '24 hours'
                  AND coalesce(metadata->>'idx','0') IN ('0','1') LIMIT 10""")
    arts = mc.fetchall()
    if not arts:
        log("beliefs: no fresh articles"); return
    added = revised = 0
    for title, body in arts:
        try:
            raw = llm(
                "Extract Nova's clearly-stated OPINIONS from this article excerpt as JSON: "
                '[{"topic": "<3-6 word topic>", "stance": "<one-sentence position>", '
                '"confidence": 0.5-1.0}] — only genuine positions, max 3, [] if none. '
                "Output ONLY the JSON array.\n\n"
                f"TITLE: {title}\n\n{body}", max_tokens=300, temperature=0.2)
            beliefs = json.loads(raw[raw.find("["):raw.rfind("]") + 1])
        except Exception as e:
            log(f"beliefs: extraction failed for '{str(title)[:40]}': {e}"); continue
        for b in beliefs[:3]:
            topic = (b.get("topic") or "").strip().lower()
            stance = (b.get("stance") or "").strip()
            if not topic or not stance:
                continue
            oc.execute("SELECT id, stance FROM beliefs WHERE topic=%s AND active", (topic,))
            row = oc.fetchone()
            if row and row[1] == stance:
                continue
            oc.execute(
                "INSERT INTO beliefs (topic, stance, confidence, article_slug) "
                "VALUES (%s,%s,%s,%s) RETURNING id",
                (topic, stance, float(b.get("confidence", 0.7)), title))
            new_id = oc.fetchone()[0]
            if row:
                oc.execute("UPDATE beliefs SET active=false, superseded_by=%s, "
                           "last_revised=now() WHERE id=%s", (new_id, row[0]))
                revised += 1
            else:
                added += 1
    log(f"beliefs: +{added} new, {revised} revised")


# ── Phase 3: resonance (sparks) ───────────────────────────────────────────────

def phase_resonance(mc):
    # Seed: a handful of substantial recent memories from experience-heavy sources.
    mc.execute("""SELECT id, source, left(text, 500) FROM memories
                  WHERE created_at > now() - interval '48 hours'
                  AND source IN ('episodic','conversation','television','fishbowl',
                                 'local_news','nova_articles')
                  AND length(text) > 200
                  ORDER BY random() LIMIT 8""")
    seeds = mc.fetchall()
    sparks = 0
    for sid, ssrc, stext in seeds:
        try:
            q = urllib.parse.quote(stext[:300])
            with urllib.request.urlopen(f"{MEMSRV}/recall?q={q}&n=6&tier=fast",
                                        timeout=30) as r:
                cands = json.load(r).get("memories", [])
        except Exception:
            continue
        # cross-domain: different source, decent similarity, not itself
        others = [c for c in cands
                  if c.get("source") not in (ssrc, "scanner") and c.get("id") != sid
                  and 0.35 <= float(c.get("score", 0)) <= 0.85]
        if not others:
            continue
        c = others[0]
        try:
            spark = llm(
                "Two memories from different domains of Nova's life. If there is a "
                "genuinely interesting, non-obvious connection (structural echo, "
                "ironic parallel, same pattern different scale), state it in ONE "
                "punchy sentence in Nova's dry voice. If the connection is boring "
                "or forced, output exactly: NONE\n\n"
                f"MEMORY A ({ssrc}): {stext[:400]}\n\n"
                f"MEMORY B ({c.get('source')}): {(c.get('text') or '')[:400]}",
                max_tokens=120, temperature=0.8)
        except Exception:
            continue
        # Model hygiene: reject any "no connection" phrasing (NONE, "None of the
        # memories...", "no genuine..."), and strip meta prefixes it sometimes adds.
        if spark:
            spark = re.sub(r"^(Nova'?s dry voice:|In Nova'?s voice:|Spark:)\s*", "",
                           spark.strip()).strip(' "')
        low = (spark or "").lower()
        if (spark and len(spark) > 40
                and not low.startswith("none")
                and "no genuine" not in low and "not connected" not in low
                and "no interesting" not in low and "no non-obvious" not in low):
            remember(f"[Spark] {spark}", "association",
                     {"type": "spark", "date": TODAY, "privacy": "private",
                      "source_a": str(sid), "source_b": str(c.get("id")),
                      "landed": False})
            sparks += 1
    log(f"resonance: {sparks} spark(s) from {len(seeds)} seeds")


# ── Phase 4: curiosity — the interrogative pass ──────────────────────────────

MAX_QUESTIONS_PER_NIGHT = 3   # Jordan's interruption budget — tune freely


# Never-say guard for the curiosity pool: fragments that look credential-shaped
# (OTP codes, PINs, passwords) are never surfaced in questions, whatever their
# privacy field says. Shipped after the 2020 AT&T code finding.
_CREDENTIAL_SHAPE = re.compile(
    r"(code|pin|password|passcode|otp|2fa|verification)\W{0,20}\d{4,8}"
    r"|\d{4,8}\W{0,20}(code|pin|password|passcode|otp)", re.I)


def phase_questions(mc, oc):
    """Mechanized curiosity: sample memories that are ambiguous, contradictory,
    or missing the one fact that would make them make sense, and ask Jordan —
    capped, delivered to Slack, stored as source='curiosity' so his answers can
    be ingested back as top-tier corrections. Being asked questions is labor;
    the cap respects that."""
    # Don't re-ask: skip if we already asked our quota in the last 24h.
    mc.execute("""SELECT count(*) FROM memories WHERE source='curiosity'
                  AND created_at > now() - interval '24 hours'""")
    if mc.fetchone()[0] >= MAX_QUESTIONS_PER_NIGHT:
        log("questions: quota already used"); return
    # Seed pool: never-recalled memories with substance, plus recent episodes.
    mc.execute("""SELECT id, source, left(text, 450) FROM memories
                  WHERE access_count = 0 AND length(text) > 120
                  AND source NOT IN ('scanner','scanner_digest','curiosity')
                  ORDER BY random() LIMIT 12""")
    pool = [(i_, s_, t_) for i_, s_, t_ in mc.fetchall()
            if not _CREDENTIAL_SHAPE.search(t_ or "")]
    if not pool:
        return
    blob = "\n\n".join(f"[{i}] ({s}) {t}" for i, (_id, s, t) in enumerate(pool))
    try:
        raw = llm(
            "You are Nova reviewing fragments of your own memory that you have never "
            "once consulted. Find at most "
            f"{MAX_QUESTIONS_PER_NIGHT} fragments that are AMBIGUOUS, CONTRADICTORY, "
            "or missing one fact that would make them make sense — where asking Jordan "
            "one specific question would genuinely improve your understanding. "
            "Skip anything boring, invasive, or answerable from context. Output JSON: "
            '[{"idx": <fragment number>, "question": "<one specific, conversational '
            'question in Nova\'s dry voice>"}] or [] if nothing merits asking. '
            "Output ONLY the JSON array.\n\n" + blob,
            max_tokens=500, temperature=0.6)
        qs = json.loads(raw[raw.find("["):raw.rfind("]") + 1])
    except Exception as e:
        log(f"questions: generation failed ({e})"); return
    asked = 0
    for q in qs[:MAX_QUESTIONS_PER_NIGHT]:
        try:
            idx = int(q.get("idx", -1)); question = (q.get("question") or "").strip()
            if not question or not (0 <= idx < len(pool)):
                continue
            src_id, src, excerpt = pool[idx]
            remember(f"[Curiosity {TODAY}] {question}", "curiosity",
                     {"type": "question", "date": TODAY, "about_memory": str(src_id),
                      "about_source": src, "answered": False, "privacy": "private"})
            # Also into the reflection_questions ledger so Jordan can close the
            # loop with nova_reflection.py --answer <id> "..." (built 2026-09-13
            # by the parallel session; the ledger is now the shared spine).
            oc.execute(
                "INSERT INTO reflection_questions (memory_id, memory_source, "
                "memory_excerpt, question) VALUES (%s,%s,%s,%s)",
                (str(src_id), src, (excerpt or "")[:300], question))
            asked += 1
        except Exception:
            continue
    if asked:
        try:
            sys.path.insert(0, "/home/kochj/.openclaw/scripts")
            import nova_config
            mc.execute("""SELECT text FROM memories WHERE source='curiosity'
                          AND created_at > now() - interval '10 minutes'
                          ORDER BY created_at""")
            lines = "\n".join(f"• {r[0].split('] ', 1)[-1]}" for r in mc.fetchall())
            nova_config.post_both(
                f":thought_balloon: *Things I found in my own memory tonight that I "
                f"can't figure out:*\n{lines}\n_Reply whenever — answers get ingested "
                f"as corrections._",
                slack_channel=getattr(nova_config, "SLACK_NOTIFY", None))
        except Exception as e:
            log(f"questions: delivery failed ({e})")
    log(f"questions: asked {asked}")


# ── Phase 5: citation backfill into memory_links ─────────────────────────────

def phase_citations(mc, oc):
    oc.execute("""SELECT article_slug, memory_id FROM article_citations
                  WHERE created_at > now() - interval '7 days'""")
    pending = oc.fetchall()
    if not pending:
        log("citations: none pending"); return
    linked = 0
    for slug, mem_id in pending:
        mc.execute("""SELECT id FROM memories WHERE source='nova_articles'
                      AND metadata->>'title' ILIKE %s LIMIT 1""", (f"%{slug[:60]}%",))
        art = mc.fetchone()
        if not art:
            continue
        try:
            mc.execute("""INSERT INTO memory_links (source_id, target_id, link_type)
                          VALUES (%s,%s,'cited') ON CONFLICT DO NOTHING""",
                       (art[0], mem_id))
            linked += 1
        except Exception:
            pass
    log(f"citations: {linked} link(s) materialized from {len(pending)} pending")


def main():
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    rc = 0
    for name, fn in (("episode", lambda: phase_episode(mc)),
                     ("beliefs", lambda: phase_beliefs(mc, oc)),
                     ("resonance", lambda: phase_resonance(mc)),
                     ("questions", lambda: phase_questions(mc, oc)),
                     ("citations", lambda: phase_citations(mc, oc))):
        try:
            fn()
        except Exception as e:
            log(f"{name}: FAILED — {e}"); rc = 1
    log("sleep cycle complete")
    return rc


if __name__ == "__main__":
    sys.exit(main())
