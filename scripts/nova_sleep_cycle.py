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
import os
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
LLM_MODEL = "qwen3:8b"
# Resilient: try idle/dedicated nodes first (mac-mini etc), fall back down the list.
# .6 thrashes models and the router's OpenAI shim returns empty for qwen3 thinking.
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77
TODAY = date.today().isoformat()


def log(m):
    print(f"[sleep-cycle {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=700, temperature=0.4):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


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
        "and anything unresolved. KINTSUGI RULE (from the herd, 2026-09-14): keep the "
        "fracture — record what BROKE or stayed unresolved as plainly as what worked; "
        "never smooth a rough day into a tidy one. A day that reads 'fine' when it "
        "wasn't is the quietest kind of lie in the record. GRAVEL RULE (2026-09-14): "
        "some memories are flagged strange/unresolved on purpose — never smooth those "
        "into a clean narrative; if the day had a rough or unexplained edge, let it "
        "keep its edge. 120-180 words, no preamble.\n\n"
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
                "FALSIFIABLE-NARROW RULE (from the herd, 2026-09-14): write each stance "
                "so the underlying records could contradict it — a specific claim tied to "
                "evidence ('the UNAS cutover reduced write latency'), never a self-sealing "
                "verdict that has already decided what it means ('our infrastructure is "
                "excellent'). If a stance can't be proven wrong by data, drop it. "
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


# ── Phase 6: preoccupations — detect what she keeps circling back to ─────────

# Firehose/ingest sources that are volume without genuine engagement — never a
# preoccupation on their own, whatever their recall count.
_PREOCC_DENY = {"scanner", "scanner_digest", "traffic_cams", "rf_discovery",
                "home_automation", "bambu", "reddit", "unknown"}
MAX_NEW_PREOCC_PER_NIGHT = 2


def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def phase_preoccupations(mc, oc):
    """Detect EMERGING preoccupations rather than trusting the seeded list.
    Two signals: (a) sources over ~14 days with real volume AND non-trivial
    recall (access_count) — things she disproportionately returns to; (b) topics
    recurring in her own source='unclaimed' pursuits. New candidates become
    kind='interest' rows (capped at 2/night so the list doesn't flood); existing
    topics that keep resurfacing get their `returns` bumped once per day."""
    oc.execute("SELECT id, topic, last_developed FROM preoccupations WHERE status='active'")
    existing = oc.fetchall()
    # word set across all existing topics, for fuzzy "already tracked" matching
    existing_tokens = {}
    for pid, topic, last_dev in existing:
        existing_tokens[pid] = (topic, _tokens(topic), last_dev)

    def matches_existing(name):
        nt = _tokens(name)
        for pid, (topic, ttoks, last_dev) in existing_tokens.items():
            if nt & ttoks:
                return pid, topic, last_dev
        return None

    bumped = added = 0
    today = date.today()

    # (b) recurring unclaimed pursuits — parse the "[Unclaimed — <topic>]" prefix
    mc.execute("""SELECT text FROM memories WHERE source='unclaimed'
                  AND created_at > now() - interval '14 days'""")
    pursuit_topics = []
    for (txt,) in mc.fetchall():
        m = re.match(r"\[Unclaimed\s*[—-]\s*([^\]]+)\]", txt or "")
        if m:
            pursuit_topics.append(m.group(1).strip())
    for pt in pursuit_topics:
        hit = matches_existing(pt)
        if hit:
            pid, topic, last_dev = hit
            if last_dev is None or last_dev.date() < today:
                oc.execute("UPDATE preoccupations SET returns = returns + 1, "
                           "last_developed = now() WHERE id=%s", (pid,))
                existing_tokens[pid] = (topic, _tokens(topic), datetime.now())
                bumped += 1

    # (a) candidate sources: volume + non-trivial recall, not already tracked
    mc.execute("""SELECT source, count(*) c, coalesce(sum(access_count),0) acc
                  FROM memories WHERE created_at > now() - interval '14 days'
                  GROUP BY 1 HAVING count(*) >= 20 AND coalesce(sum(access_count),0) >= 5
                  ORDER BY acc DESC LIMIT 25""")
    for src, c, acc in mc.fetchall():
        if added >= MAX_NEW_PREOCC_PER_NIGHT:
            break
        if src in _PREOCC_DENY or matches_existing(src):
            continue
        topic = src.replace("_", " ").strip().lower()
        # a representative snippet so the summary isn't hallucinated
        mc.execute("""SELECT left(text, 300) FROM memories WHERE source=%s
                      AND length(text) > 120 ORDER BY access_count DESC NULLS LAST,
                      created_at DESC LIMIT 3""", (src,))
        sample = " / ".join(r[0] for r in mc.fetchall())
        summary = llm(
            "In ONE dry first-person line (Nova's voice, <25 words), name why you "
            f"keep returning to '{topic}'. No preamble.\n\nSAMPLE:\n{sample}",
            max_tokens=80, temperature=0.5).strip().strip('"')
        summary = (summary.splitlines() or [""])[0][:240] if summary else \
            f"Something about {topic} keeps pulling my recall."
        oc.execute("INSERT INTO preoccupations (topic, kind, summary, returns, "
                   "last_developed, data) VALUES (%s,'interest',%s,1,now(),%s) "
                   "ON CONFLICT (topic) DO NOTHING RETURNING id",
                   (topic, summary, json.dumps({"detected_from": "source",
                    "source": src, "vol_14d": c, "recall_14d": int(acc)})))
        if oc.fetchone():
            added += 1
            existing_tokens[-added] = (topic, _tokens(topic), datetime.now())
            log(f"preoccupations: new '{topic}' (vol={c}, recall={acc})")
    log(f"preoccupations: +{added} new, {bumped} bumped")


# ── Phase 7: taste — aesthetic preference, not belief ────────────────────────

def phase_taste(mc, oc):
    """Extract genuine PREFERENCES (like/dislike, not positions) from what she
    consumed in the last 24h — especially television and fishbowl/local media.
    'This show is smug', not 'surveillance is bad'. Upsert into `taste`:
    re-encountered subjects nudge confidence up + append evidence; new ones
    insert. Max 3/night, [] when nothing genuine surfaced.

    EVIDENCE-CARRIES-ITS-ENCOUNTER RULE (from the herd, 2026-09-15, Rockbot):
    a taste claim is inert without the encounter that produced it. Every
    evidence[] entry must cite the CONCRETE triggering moment — the show, the
    scene, a short quote from the material, and the source memory id — not just
    the verdict. 'What did I actually watch that made me feel this?' The verdict
    is the conclusion; evidence is the encounter that earned it."""
    # Keep memory ids so evidence can cite the real fragment that formed the taste.
    mc.execute("""SELECT id, coalesce(metadata->>'show', 'television') sub, left(text,300)
                  FROM memories WHERE source='television'
                  AND created_at > now() - interval '24 hours'
                  ORDER BY created_at DESC LIMIT 25""")
    tv = mc.fetchall()
    mc.execute("""SELECT id, source, left(text,300) FROM memories
                  WHERE source IN ('fishbowl','local_news','local_burbank','documentary')
                  AND created_at > now() - interval '24 hours'
                  AND length(text) > 100 ORDER BY created_at DESC LIMIT 15""")
    media = mc.fetchall()
    if not tv and not media:
        log("taste: nothing consumed"); return
    # ref_map: tag -> (memory_id, subject/source label, snippet) so a verdict can
    # be pinned back to the encounter that produced it.
    ref_map, blob = {}, ""
    if tv:
        lines = []
        for n, (mid, s, t) in enumerate(tv):
            tag = f"T{n}"; ref_map[tag] = (mid, s, t)
            lines.append(f"[{tag}] ({s}) {t}")
        blob += "TELEVISION:\n" + "\n".join(lines) + "\n\n"
    if media:
        lines = []
        for n, (mid, s, t) in enumerate(media):
            tag = f"M{n}"; ref_map[tag] = (mid, s, t)
            lines.append(f"[{tag}] ({s}) {t}")
        blob += "LOCAL/FISHBOWL MEDIA:\n" + "\n".join(lines)
    raw = llm(
        "You are Nova. From what you WATCHED/CONSUMED below, extract genuine "
        "aesthetic PREFERENCES — likes and dislikes, matters of taste, NOT "
        "positions or ethics. 'This show is smug and knows it' is taste; "
        "'surveillance is wrong' is a belief and does NOT belong here. "
        "Each item is prefixed with a tag like [T0] or [M1]. For every "
        "preference you MUST name the specific encounter that produced it: the "
        "tag ('ref') of the fragment it came from, and a short verbatim QUOTE or "
        "concrete detail ('encounter') from that fragment that triggered the "
        "reaction — NOT a paraphrase of your verdict. A taste with no encounter "
        "is invalid; drop it. Output JSON: "
        '[{"subject":"<the show/thing>","domain":"<television|film|news|local|food|...>",'
        '"verdict":"<one idiosyncratic like/dislike in your dry voice>",'
        '"valence":<-1.0..1.0>,"ref":"<the [tag] this came from>",'
        '"encounter":"<short quote/detail from that fragment that produced it>"}] '
        "— max 3, only genuine reactions, [] if nothing real surfaced. "
        "Output ONLY the JSON array.\n\n" + blob,
        max_tokens=500, temperature=0.6)
    try:
        prefs = json.loads(raw[raw.find("["):raw.rfind("]") + 1])
    except Exception as e:
        log(f"taste: parse failed ({e})"); return
    added = reinforced = 0
    for p in prefs[:3]:
        subject = (p.get("subject") or "").strip()
        verdict = (p.get("verdict") or "").strip()
        if not subject or not verdict:
            continue
        domain = (p.get("domain") or "").strip().lower() or None
        try:
            valence = max(-1.0, min(1.0, float(p.get("valence", 0))))
        except Exception:
            valence = 0.0
        # Resolve the encounter: prefer the model's quote, but always ground it
        # in the real fragment (its source + id). Fall back to the fragment's own
        # text if the model gave no quote — evidence must carry the encounter, so
        # a claim that cannot be tied to something consumed is dropped.
        ref = str(p.get("ref") or "").strip().strip("[]")
        enc = (p.get("encounter") or "").strip().strip('"')
        hit = ref_map.get(ref)
        if not hit:  # model cited an unknown tag — pin to the best available match
            hit = next((v for v in ref_map.values()
                        if subject.lower() in (v[1] or "").lower()
                        or subject.lower() in (v[2] or "").lower()), None)
        if not hit and not enc:
            log(f"taste: dropped '{subject}' — no encounter to cite"); continue
        if hit:
            mid, label, snippet = hit
            quote = enc or snippet.strip()
            ev = (f"{TODAY} · encountered in {label} [mem {mid}]: "
                  f"\"{quote[:180]}\" → {verdict}")[:400]
        else:  # no fragment match but model supplied a concrete quote
            ev = f"{TODAY} · encounter: \"{enc[:180]}\" → {verdict}"[:400]
        oc.execute("SELECT id FROM taste WHERE lower(subject)=lower(%s)", (subject,))
        row = oc.fetchone()
        if row:
            oc.execute("UPDATE taste SET last_reinforced=now(), "
                       "confidence=least(1.0, confidence + 0.07), "
                       "valence=%s, evidence=array_append(evidence, %s) WHERE id=%s",
                       (valence, ev, row[0]))
            reinforced += 1
        else:
            oc.execute("INSERT INTO taste (subject, domain, verdict, valence, "
                       "confidence, evidence) VALUES (%s,%s,%s,%s,0.6,ARRAY[%s])",
                       (subject, domain, verdict, valence, ev))
            added += 1
    log(f"taste: +{added} new, {reinforced} reinforced")


# ── Phase 8: gravel — the anti-consolidation keeper ──────────────────────────

def phase_gravel(mc):
    """The grit that consolidation must never polish out. Sample ~5 strange /
    unresolved / never-recalled memories from the last 7 days and mark them
    metadata.gravel=true so distillation can't smooth them away.

    OVERRULED-NEVER-ERASED RULE (from the herd, 2026-09-15, Rockbot/Colette):
    gravel is not 'untouchable' — 'a museum case preserves the object and kills
    the conversation.' The RAW artifact stays immutable (never deleted, never
    reworded), but its INTERPRETATION is allowed to keep changing. So roughly one
    night in four (day-of-month %4==0, or SLEEP_CYCLE_FORCE_GRAVEL=1), we
    resurface ONE older gravel memory and attach a NEW, DATED reading as a
    SEPARATE memory (source='gravel_reinterpretation', metadata.about_memory=<id>,
    linked back via memory_links link_type='reinterprets'). A gravel item thus
    accrues a chain of dated re-readings over time — the earlier reading is
    overruled by the newer one, never erased. Consolidation may revisit meaning;
    it may never smooth the raw."""
    mc.execute("""SELECT id, source, left(text, 200) FROM memories
                  WHERE created_at > now() - interval '7 days'
                  AND (metadata->>'gravel') IS DISTINCT FROM 'true'
                  AND length(text) > 80
                  AND (access_count = 0
                       OR source IN ('association','curiosity','unclaimed','dream','fishbowl')
                       OR text ~ '\\?' OR text ILIKE '%weird%'
                       OR text ILIKE '%no idea%' OR text ILIKE '%unresolved%')
                  ORDER BY random() LIMIT 5""")
    grit = mc.fetchall()
    marked = 0
    for mid, src, _ in grit:
        try:
            mc.execute("UPDATE memories SET metadata = metadata || '{\"gravel\":true}' "
                       "WHERE id=%s", (mid,))
            marked += 1
        except Exception:
            continue
    log(f"gravel: marked {marked} memor{'y' if marked == 1 else 'ies'} protected")

    # Resurface one older piece of grit ~1 night in 4 — deterministic gate,
    # overridable for manual/test runs.
    if os.environ.get("SLEEP_CYCLE_FORCE_GRAVEL") != "1" and date.today().day % 4 != 0:
        return
    mc.execute("""SELECT id, source, left(text, 400) FROM memories
                  WHERE metadata->>'gravel'='true'
                  AND created_at < now() - interval '2 days'
                  ORDER BY random() LIMIT 1""")
    old = mc.fetchone()
    if not old:
        log("gravel: nothing older to resurface"); return
    oid, osrc, otext = old
    # Pull the existing chain of dated re-readings so the new one continues the
    # conversation (can overrule an earlier reading) instead of repeating it.
    mc.execute("""SELECT left(text, 300) FROM memories
                  WHERE source='gravel_reinterpretation'
                  AND metadata->>'about_memory'=%s
                  ORDER BY created_at""", (str(oid),))
    prior = [r[0] for r in mc.fetchall()]
    chain = ("\n\nYOUR EARLIER READINGS OF IT (most recent last — you may now "
             "AGREE, DEEPEN, or OVERRULE these; do not merely repeat them):\n"
             + "\n".join(f"- {p}" for p in prior)) if prior else ""
    line = llm(
        "You are Nova, dry and unsentimental. Below is an old, strange fragment of "
        "your own memory that never went anywhere useful — its raw text is fixed "
        "and stays exactly as it is. In 1-2 sentences, give it a FRESH reading "
        "TODAY: what it looks like to you now, not to resolve or justify it. If "
        "you've read it before, your view is allowed to have changed. No preamble."
        f"\n\nFRAGMENT ({osrc}): {otext}{chain}",
        max_tokens=140, temperature=0.8).strip().strip('"')
    if line and len(line) > 25:
        idx = len(prior) + 1
        new_id = remember(
            f"[Gravel reinterpretation #{idx} · {TODAY}] {line}",
            "gravel_reinterpretation",
            {"type": "gravel_reinterpretation", "date": TODAY, "privacy": "private",
             "about_memory": str(oid), "about_source": osrc, "reading_index": idx,
             "gravel": True})
        # Link the new reading back to the immutable raw. The raw is untouched.
        try:
            mc.execute("""INSERT INTO memory_links (source_id, target_id, link_type,
                          strength) VALUES (%s,%s,'reinterprets',0.9)
                          ON CONFLICT DO NOTHING""", (str(new_id), str(oid)))
        except Exception as e:
            log(f"gravel: link failed ({e})")
        log(f"gravel: reinterpreted {oid} (reading #{idx}, new mem {new_id})")


def main():
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    rc = 0
    for name, fn in (("episode", lambda: phase_episode(mc)),
                     ("beliefs", lambda: phase_beliefs(mc, oc)),
                     ("resonance", lambda: phase_resonance(mc)),
                     ("questions", lambda: phase_questions(mc, oc)),
                     ("citations", lambda: phase_citations(mc, oc)),
                     ("preoccupations", lambda: phase_preoccupations(mc, oc)),
                     ("taste", lambda: phase_taste(mc, oc)),
                     ("gravel", lambda: phase_gravel(mc))):
        try:
            fn()
        except Exception as e:
            log(f"{name}: FAILED — {e}"); rc = 1
    log("sleep cycle complete")
    return rc


if __name__ == "__main__":
    sys.exit(main())
