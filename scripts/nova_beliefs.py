#!/usr/bin/env python3
"""
nova_beliefs.py — what Nova comes to believe from what she reads and does, and how sure she is (2026-10-17).

Before this, the work reader booked a belief the moment a lesson was linked: the daily cap (50) filled
first-come by 06:00 with plot summaries ("When Norm applies glue and brads minimally ..."), scripture
("When Israel remembers their slavery ...") and broken "When X , so Y" sentences, every one at a flat 0.60,
and near-duplicates never revised an existing belief (token Jaccard >= 0.5 almost never fires on a paraphrase).

Now a lesson is a CANDIDATE first, and a belief only when it earns it:

  add_candidate  (any organ: work_lessons, ops lessons) cleans the sentence, rejects the clearly bad (scripture,
                 questions, fragments, trivia), embeds it (nomic) and either
                   - reinforces an active belief it restates (cos >= SAME_COS: evidence + confidence, no cap), or
                   - merges into a pending candidate saying the same thing from another source (n_sources + 1), or
                   - waits as 'pending'.
  promote        (scheduler, every 2h) ranks pending candidates by VALUE — generality, novelty against what she
                 already believes, relevance to her and Jordan's world (SRE/ops, security, home, health, his
                 projects, world affairs, judgment), source credibility, independent sources — and spends only the
                 share of the daily budget the hour allows (so the best of the day win, not the first). The top ones
                 go to the local model once, which restates each as ONE durable, general, grammatical belief (or
                 null for plot/trivia/scripture) and says how it relates to her nearest existing belief:
                   same -> reinforce, refines -> revise (new row supersedes the old: the changed-minds ledger sees it),
                   contradicts -> the old belief loses confidence, new -> booked against the cap.
                 Confidence is calibrated from credibility and the number of independent sources, not a constant.
  audit          (--audit [--apply]) finds active beliefs that are malformed, scripture or trivia; --apply
                 deactivates them (active=false, never deleted).

Tables: belief_candidates, belief_vectors (nova_ops). Cap: work_lessons_daily.beliefs (shared with the reader).
CLI: --promote [--dry-run] [--budget N] | --audit [--apply] | --stats | --selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

DAILY_CAP = 50                 # == nova_work_lessons.CAPS["beliefs"]; counted in work_lessons_daily.beliefs
SAME_COS = 0.90                # nomic cos at/above which two sentences are the same claim (paraphrase ~0.94)
RELATE_COS = 0.78              # at/above: the model is asked how the candidate relates to that belief
MIN_SCORE = 0.72               # below this a candidate never spends budget
SHORTLIST_X = 3                # model calls per run <= SHORTLIST_X * allowance
PENDING_DAYS = 4               # an unpromoted candidate expires after this
MAX_CONF = 0.95
LLM_URLS = ("http://127.0.0.1:11436", "http://127.0.0.1:11434")
LLM_MODEL = "qwen3:30b-a3b"

CRED_BASE = {"observed": 0.65, "established": 0.60, "unknown": 0.55, "opinion": 0.45, "fiction": 0.40}
CRED_WEIGHT = {"observed": 1.0, "established": 0.9, "unknown": 0.75, "opinion": 0.5, "fiction": 0.45, "fringe": 0.0}

# What matters to her and to Jordan: relevance is the best cosine to one of these.
DOMAINS = {
    "sre": "Site reliability engineering: outages, incident response, monitoring and alerting, on-call, "
           "postmortems, capacity, automation that fails, backups and recovery.",
    "security": "Computer and network security: intrusions, vulnerabilities, threat detection, privacy, "
                "credentials, surveillance, intelligence and counterintelligence.",
    "home": "A household and home: home automation, family routines, safety at home, maintenance, neighbours.",
    "health": "Health, sleep, stress, medicine, fitness and wellbeing of people.",
    "tech": "Software engineering and AI: local language models, Mac apps, network scanning, file sync and "
            "backup tools, data pipelines, reliability of code.",
    "world": "World affairs: geopolitics, war, government, elections, economics, law and public policy.",
    "judgment": "How people and organisations decide: evidence, honesty, leadership, trust, risk, mistakes, "
                "and learning from failure.",
}

_CITE = re.compile(r"\s*\[\d{1,2}\](?:\s*\[\d{1,2}\])*")
SCRIPTURE_RX = re.compile(
    r"\bLORD\b|\bthe Lord\b|\bGod'?s\b|\bYahweh\b|\bJehovah\b|\bIsraelites?\b|\bchildren of Israel\b|\bMoses\b|\bAaron\b|\bPharaoh\b|"
    r"\bthou\b|\bthee\b|\bthy\b|\bunto\b|\bcovenant\b|\btabernacle\b|\bburnt offering\b|\bsin offering\b|"
    r"\bLevites?\b|\bscripture\b|\bcommandments?\b|\bstatutes\b", re.I)
SCRIPTURE_TITLES = re.compile(
    r"\b(genesis|exodus|leviticus|numbers|deuteronomy|joshua|judges|ruth|samuel|kings|chronicles|ezra|nehemiah|"
    r"esther|job|psalms?|proverbs|ecclesiastes|song of solomon|isaiah|jeremiah|lamentations|ezekiel|daniel|hosea|"
    r"joel|amos|obadiah|jonah|micah|nahum|habakkuk|zephaniah|haggai|zechariah|malachi|matthew|mark|luke|john|"
    r"acts|romans|corinthians|galatians|ephesians|philippians|colossians|thessalonians|timothy|titus|philemon|"
    r"hebrews|james|peter|jude|revelation|bible|quran|koran|torah|gospel)\b", re.I)
_MODAL = re.compile(r"\b(should|must|need|needs|tends?|requires?|can|cannot|can't|will|often|usually|rarely|"
                    r"is|are|works?|fails?|matters?|depends?)\b", re.I)
_SENT_START_OK = {"I", "A", "An", "The", "When", "If", "No", "Any", "Every", "Most", "Nova", "AI"}


def log(m):
    print(f"[beliefs {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ─────────────────────────── pure: text ───────────────────────────

def clean_stance(s: str) -> str:
    """Model sentence -> clean sentence: cites gone, no space before punctuation, the reader's
    'When X , so Y' template repaired, capitalised, one terminal period. Pure."""
    t = _CITE.sub("", str(s or "")).strip().strip('"“”')
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([,.;:!?)])", r"\1", t)            # "edge detail ." -> "edge detail."
    t = re.sub(r"([(])\s+", r"\1", t)
    t = re.sub(r"\s*[—–]+\s*so\s+", ", so ", t)  # "X — so Y" -> "X, so Y"
    t = re.sub(r"\s+-\s+so\s+", ", so ", t)
    t = re.sub(r",\s*,+", ",", t)
    t = re.sub(r"\.{2,}$", ".", t)
    # "When <clause>, so <result>" with no main clause in between is the broken template: drop the "so"
    m = re.match(r"^(When\b[^,]+),\s*so\s+(.+)$", t)
    if m:
        t = f"{m.group(1)}, {m.group(2)}"
    t = re.sub(r"\s+([,.;:!?])", r"\1", t).strip(" ,;")
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    if t and t[-1] not in ".!?":
        t += "."
    return t


def _words(s: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'\-]*", s or "")


def proper_nouns(s: str) -> list[str]:
    """Capitalised words that are not sentence starts or the few allowed ones. Pure."""
    out, toks = [], (s or "").split()
    for i, w in enumerate(toks):
        core = re.sub(r"^[^A-Za-z]+|[^A-Za-z']+$", "", w)
        if not core or not core[0].isupper():
            continue
        if i == 0 or re.search(r"[.!?:]$", toks[i - 1]):     # a sentence start is not a name
            continue
        if core in _SENT_START_OK or (core.isupper() and len(core) <= 5):   # acronyms (SRE, NATO) are fine
            continue
        out.append(core)
    return out


def generality(s: str) -> float:
    """0..1: a durable, general claim scores high; a plot beat about named people/numbers scores low. Pure."""
    w = _words(s)
    if not w:
        return 0.0
    g = 1.0
    g -= min(0.6, 3.0 * len(proper_nouns(s)) / len(w))
    if re.search(r"\d", s):
        g -= 0.15
    if re.search(r"['‘“\"][^'’”\"]{3,}['’”\"]", s):
        g -= 0.2
    if re.match(r"^When [A-Z][a-z]+\b(?! (?:a|an|the|you|people|teams?|systems?|someone|organi[sz]ations?)\b)", s):
        g -= 0.3                                         # "When Norm ...", "When FESA ..."
    if re.search(r"\b(he|she|they) (?:did|was|were|had|turned|applied|used)\b", s, re.I):
        g -= 0.1
    if re.search(r"\b(?:did|was|were|had|turned|applied|failed|adopted|began|remembers)\b", s) and not _MODAL.search(s):
        g -= 0.15                                        # past-tense narration with no general verb
    if _MODAL.search(s):
        g += 0.05
    return max(0.0, min(1.0, round(g, 3)))


def reject_reason(stance: str, title: str = "", kind: str = "") -> str | None:
    """Why this sentence can never be a belief, or None. Pure."""
    s = stance or ""
    n = len(_words(s))
    if n < 6:
        return "fragment"
    if n > 48:
        return "too long"
    if "?" in s:
        return "question"
    if SCRIPTURE_RX.search(s) or (kind in ("book", "document", "") and SCRIPTURE_TITLES.fullmatch((title or "").strip())):
        return "scripture"
    if re.match(r"^When\b[^,]*$", s.rstrip(".")):
        return "malformed (no main clause)"
    if s.count('"') % 2 or s.count("(") != s.count(")"):
        return "malformed (unbalanced)"
    if re.search(r"\s[,.;:!?]", s):
        return "malformed (spacing)"
    if re.search(r"(?:^|,)\s*so\s*[.]?$", s):
        return "malformed (dangling so)"
    if generality(s) < 0.3:
        return "trivia (too specific)"
    # ponytail: name/phrase match -- a real conviction about Jordan would be caught too; reword it if that bites
    if re.search(r"\bLittle Mister\b", s):
        return "recap (about Jordan, e.g. a game column)"
    if re.match(r"(?i)(the|this|my)\s+(author|essay|article|column|post|piece)\b", s):
        return "self-report (about the article, not the world)"
    return None


def topic_of(stance: str, topic: str | None = None) -> str:
    t = re.sub(r"\s+", " ", str(topic or "")).strip().strip(".").lower()
    if not t:
        stop = {"when", "that", "this", "with", "from", "into", "their", "they", "have", "will", "must", "should"}
        t = " ".join([w.lower() for w in _words(stance) if len(w) > 3 and w.lower() not in stop][:4])
    return t[:80] or "lesson"


FILLER_VERDICT = re.compile(
    r"not applicable|irrelevant|no (?:operational |direct |real )?(?:relevance|bearing|parallels?|connection|application)|"
    r"(?:don'?t|doesn'?t|do not|does not) (?:translate|apply)|mirrors? (?:nova|my)|nova'?s (?:operational|digital|"
    r"system|memory|rigor|values)|no opinion|nothing to say|^n/?a$|^none$|^null$|^-?\d+(?:\.\d+)?$", re.I)


def verdict_ok(verdict: str) -> bool:
    """A taste verdict worth keeping: a real opinion sentence about the work, not a number, not 'no opinion',
    not filler about how it does/doesn't apply to Nova's systems. Pure."""
    v = re.sub(r"\s+", " ", str(verdict or "")).strip().strip('"')
    return len(_words(v)) >= 3 and not FILLER_VERDICT.search(v)


# ─────────────────────────── pure: numbers ───────────────────────────

def confidence_for(credibility: str, n_sources: int = 1, fit: int = 1) -> float:
    """Calibrated starting confidence: the source's credibility, raised by each INDEPENDENT source
    (diminishing), nudged by how well the lesson fits her world. Never a constant. Pure."""
    base = CRED_BASE.get(credibility or "unknown", CRED_BASE["unknown"])
    c = 1 - (1 - base) * (0.75 ** max(0, int(n_sources or 1) - 1))
    c += 0.02 * (max(0, min(3, int(fit or 0))) - 1)
    return round(max(0.2, min(0.9, c)), 2)


def reinforced_conf(conf: float, credibility: str = "unknown") -> float:
    """One more independent source agrees: move part of the way to MAX_CONF, weighted by credibility. Pure."""
    w = CRED_WEIGHT.get(credibility or "unknown", 0.75)
    return round(min(MAX_CONF, conf + (MAX_CONF - conf) * 0.2 * w), 3)


def contradicted_conf(conf: float, credibility: str = "unknown") -> float:
    w = CRED_WEIGHT.get(credibility or "unknown", 0.75)
    return round(max(0.1, conf - (conf - 0.1) * 0.25 * w), 3)


def value_score(gen: float, novelty: float, relevance: float, credibility: str, n_sources: int = 1,
                fit: int = 1) -> float:
    """How much this candidate deserves one of today's belief slots. Pure.
    novelty = 1 - best cosine to an active belief; relevance = best cosine to a DOMAINS anchor (nomic scale:
    unrelated/abstract ~0.46-0.50, on-topic ~0.58-0.63+)."""
    nov = max(0.0, min(1.0, novelty / 0.35))            # 0.65 cos to her nearest belief already counts as new
    rel = max(0.0, min(1.0, (relevance - 0.48) / 0.14))
    cw = CRED_WEIGHT.get(credibility or "unknown", 0.75)
    s = 0.35 * gen + 0.15 * nov + 0.35 * rel + 0.15 * cw
    s *= 1 + 0.12 * min(3, max(0, int(n_sources or 1) - 1))
    s += 0.03 * (max(0, min(3, int(fit or 0))) - 1)
    return round(s * (1.0 if cw > 0 else 0.0), 4)


def allowance(used_today: int, hour: int, cap: int = DAILY_CAP) -> int:
    """New beliefs this run may book: the day's cap released evenly by the hour (2h ahead), minus what is
    already used — so the best candidates of every part of the day compete, not the first ones. Pure."""
    released = math.ceil(cap * min(24, hour + 2) / 24)
    return max(0, min(cap, released) - used_today)


def cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


def nearest(vec, pool: list[tuple[int, list]]) -> tuple[int | None, float]:
    best, bc = None, 0.0
    for i, v in pool:
        c = cosine(vec, v)
        if c > bc:
            best, bc = i, c
    return best, bc


def distill_prompt(stance: str, title: str, origin: str, quote: str | None, near: str | None) -> str:
    near_txt = (f"\nHer closest EXISTING belief: \"{near}\"\nSay how yours relates to it: 'same' (the same claim), "
                "'refines' (the same claim made sharper or corrected — then 'belief' is the merged sentence), "
                "'contradicts', or 'new' (a different claim).") if near else ""
    src = f" (from {origin}: '{title[:120]}')" if title else f" (from {origin})"
    return (
        "You keep Nova's beliefs: short, durable, GENERAL lessons about how the world works that would still be "
        "true and useful outside the source they came from — about systems and operations, security, homes and "
        "families, health, technology, how people and institutions decide, and world affairs.\n"
        f"Candidate lesson{src}:\n\"{stance}\"\n" + (f"Supporting quote: \"{quote[:300]}\"\n" if quote else "") +
        near_txt + "\n\nRewrite it as ONE belief: one plain, grammatical present-tense sentence of 8-30 words; no "
        "names of characters, hosts, episodes or one-off places (a public institution or country is fine when the "
        "lesson is about it); no story events; no instructions for a single craft project. Return belief null if "
        "it is only plot, trivia, a craft or recipe step, a feature of one product or vehicle, a quotation of "
        "scripture or religious doctrine, or a platitude anyone would say ('teamwork matters').\n"
        'Output ONLY JSON: {"belief": "<sentence or null>", "topic": "<3-5 words>", '
        '"relation": "new|same|refines|contradicts", "why": "<short>"}')


def parse_distill(raw: str) -> dict | None:
    t = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    try:
        o = json.loads(t[t.find("{"):t.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return None
    b = o.get("belief")
    b = None if b is None or str(b).strip().lower() in ("", "null", "none") else clean_stance(str(b))
    rel = str(o.get("relation") or "new").strip().lower()
    return {"belief": b, "topic": str(o.get("topic") or "").strip()[:80],
            "relation": rel if rel in ("new", "same", "refines", "contradicts") else "new",
            "why": str(o.get("why") or "")[:200]}


# ─────────────────────────── io ───────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS belief_candidates (
  id serial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), origin text NOT NULL, ref text,
  title text, kind text, topic text, stance text NOT NULL, raw text, quote text, evidence text[] NOT NULL DEFAULT '{}',
  credibility text NOT NULL DEFAULT 'unknown', fit int NOT NULL DEFAULT 1, n_sources int NOT NULL DEFAULT 1,
  refs text[] NOT NULL DEFAULT '{}', embedding real[], status text NOT NULL DEFAULT 'pending', reason text,
  score real, scores jsonb, belief_id int, decided_at timestamptz,
  UNIQUE (origin, ref, stance));
CREATE INDEX IF NOT EXISTS belief_candidates_pending ON belief_candidates (created_at) WHERE status='pending';
CREATE TABLE IF NOT EXISTS belief_vectors (
  belief_id int PRIMARY KEY, stance_md5 text NOT NULL, embedding real[] NOT NULL, updated_at timestamptz DEFAULT now());
"""
_SCHEMA_DONE = {"v": False}


def ensure_schema(oc):
    if _SCHEMA_DONE["v"]:
        return
    for stmt in [x for x in SCHEMA.split(";") if x.strip()]:
        oc.execute(stmt)
    _SCHEMA_DONE["v"] = True


def embed(texts: list[str]) -> list | None:
    if not texts:
        return []
    try:
        import nova_work_lessons as wl
        v = wl.embed(texts)
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    for base in ("http://127.0.0.1:11434",):
        try:
            req = urllib.request.Request(base + "/api/embed", method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"model": "nomic-embed-text", "input": texts}).encode())
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r).get("embeddings")
        except Exception:  # noqa: BLE001
            continue
    return None


def llm_json(prompt: str, max_tokens: int = 300) -> str:
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False, "keep_alive": "30m", "format": "json",
                       "options": {"temperature": 0.2, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt + "\n/no_think"}]}).encode()
    for base in LLM_URLS:
        try:
            req = urllib.request.Request(base + "/api/chat", data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.load(r).get("message", {}).get("content", "")
            if out:
                return out
        except Exception:  # noqa: BLE001
            continue
    try:
        import nova_sleep_cycle
        return nova_sleep_cycle.llm(prompt, max_tokens=max_tokens, temperature=0.2)
    except Exception:  # noqa: BLE001
        return ""


def belief_pool(oc) -> list[tuple[int, str, list]]:
    """Active beliefs with their embeddings (cached in belief_vectors, refreshed when the stance changes)."""
    oc.execute("SELECT b.id, b.stance, v.stance_md5, v.embedding FROM beliefs b LEFT JOIN belief_vectors v "
               "ON v.belief_id = b.id WHERE b.active")
    rows = oc.fetchall() or []
    out, todo = [], []
    for bid, stance, md5, emb in rows:
        h = hashlib.md5((stance or "").encode()).hexdigest()
        if emb and md5 == h:
            out.append((bid, stance, list(emb)))
        else:
            todo.append((bid, stance, h))
    for i in range(0, len(todo), 64):
        chunk = todo[i:i + 64]
        vecs = embed([s for _, s, _ in chunk]) or []
        for (bid, stance, h), v in zip(chunk, vecs):
            oc.execute("INSERT INTO belief_vectors (belief_id, stance_md5, embedding) VALUES (%s,%s,%s) ON CONFLICT "
                       "(belief_id) DO UPDATE SET stance_md5=EXCLUDED.stance_md5, embedding=EXCLUDED.embedding, "
                       "updated_at=now()", (bid, h, v))
            out.append((bid, stance, list(v)))
    return out


def reinforce(oc, belief_id: int, evidence: list[str], credibility: str = "unknown") -> None:
    oc.execute("SELECT confidence FROM beliefs WHERE id=%s", (belief_id,))
    r = oc.fetchone()
    if not r:
        return
    oc.execute("UPDATE beliefs SET evidence = (SELECT array_agg(DISTINCT e) FROM unnest(coalesce(evidence,'{}') || "
               "%s::text[]) e), confidence = %s, last_revised = now() WHERE id = %s",
               (list(evidence), reinforced_conf(float(r[0]), credibility), belief_id))


def take_cap(oc) -> bool:
    oc.execute("INSERT INTO work_lessons_daily (day) VALUES (current_date) ON CONFLICT DO NOTHING")
    oc.execute("UPDATE work_lessons_daily SET beliefs = beliefs + 1 WHERE day = current_date AND beliefs < %s "
               "RETURNING beliefs", (DAILY_CAP,))
    return bool(oc.fetchone())


def used_today(oc) -> int:
    oc.execute("SELECT beliefs FROM work_lessons_daily WHERE day = current_date")
    r = oc.fetchone()
    return int(r[0]) if r else 0


def add_candidate(oc, origin: str, ref: str, title: str, stance: str, evidence: list[str], topic: str | None = None,
                  credibility: str = "unknown", fit: int = 1, kind: str | None = None,
                  quote: str | None = None) -> tuple[int | None, str]:
    """Offer a lesson for belief. Returns (candidate id or belief id, status): 'pending', 'merged',
    'reinforced:<belief id>', or 'rejected: <why>'. Never raises on a bad sentence; needs evidence."""
    ensure_schema(oc)
    if not evidence:
        return None, "rejected: no evidence"
    if (credibility or "") == "fringe":
        return None, "rejected: fringe source"
    clean = clean_stance(stance)
    why = reject_reason(clean, title, kind or "")
    if why:
        try:
            oc.execute("INSERT INTO belief_candidates (origin, ref, title, kind, topic, stance, raw, quote, evidence, "
                       "credibility, fit, refs, status, reason, decided_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                       "%s,'rejected',%s,now()) ON CONFLICT DO NOTHING RETURNING id",
                       (origin, ref, (title or "")[:300], kind, topic_of(clean, topic), clean, stance, quote,
                        list(evidence), credibility, int(fit or 1), [ref] if ref else [], why))
            r = oc.fetchone()
            return (r[0] if r else None), f"rejected: {why}"
        except Exception:  # noqa: BLE001
            return None, f"rejected: {why}"
    vec = (embed([clean]) or [None])[0]
    if vec:
        pool = belief_pool(oc)
        bid, c = nearest(vec, [(i, v) for i, _, v in pool])
        if bid and c >= SAME_COS:
            reinforce(oc, bid, evidence, credibility)
            oc.execute("INSERT INTO belief_candidates (origin, ref, title, kind, topic, stance, raw, quote, evidence, "
                       "credibility, fit, refs, embedding, status, reason, belief_id, decided_at) VALUES (%s,%s,%s,%s,"
                       "%s,%s,%s,%s,%s,%s,%s,%s,%s,'reinforced',%s,%s,now()) ON CONFLICT DO NOTHING",
                       (origin, ref, (title or "")[:300], kind, topic_of(clean, topic), clean, stance, quote,
                        list(evidence), credibility, int(fit or 1), [ref] if ref else [], vec, f"cos {c:.3f}", bid))
            return bid, f"reinforced:{bid}"
        oc.execute("SELECT id, embedding, refs FROM belief_candidates WHERE status='pending' AND embedding IS NOT NULL "
                   "AND created_at > now() - make_interval(days => %s)", (PENDING_DAYS,))
        cands = oc.fetchall() or []
        cid, cc = nearest(vec, [(i, list(e)) for i, e, _ in cands])
        if cid and cc >= SAME_COS:
            refs = next((r for i, _, r in cands if i == cid), []) or []
            if ref and ref not in refs:
                oc.execute("UPDATE belief_candidates SET n_sources = n_sources + 1, refs = array_append(refs, %s), "
                           "evidence = (SELECT array_agg(DISTINCT e) FROM unnest(evidence || %s::text[]) e) WHERE id=%s",
                           (ref, list(evidence), cid))
            return cid, "merged"
    oc.execute("INSERT INTO belief_candidates (origin, ref, title, kind, topic, stance, raw, quote, evidence, "
               "credibility, fit, refs, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
               "ON CONFLICT (origin, ref, stance) DO NOTHING RETURNING id",
               (origin, ref, (title or "")[:300], kind, topic_of(clean, topic), clean, stance, quote, list(evidence),
                credibility, int(fit or 1), [ref] if ref else [], vec))
    r = oc.fetchone()
    return (r[0] if r else None), "pending"


def _link_work(oc, cand_ref: str | None, origin: str, belief_id: int):
    if origin == "work_lessons" and cand_ref:
        try:
            oc.execute("UPDATE work_lessons SET belief_ids = (SELECT array_agg(DISTINCT x) FROM unnest("
                       "coalesce(belief_ids,'{}') || ARRAY[%s]) x) WHERE work_key=%s", (belief_id, cand_ref))
        except Exception:  # noqa: BLE001
            pass


_ANCHORS: dict = {}


def _anchors() -> dict:
    vecs = embed(list(DOMAINS.values())) or []
    return dict(zip(DOMAINS.keys(), vecs))


def score_pending(oc, pool=None, anchors=None) -> list[dict]:
    """Every pending candidate with its scores (pure maths over cached embeddings)."""
    oc.execute("SELECT id, origin, ref, title, kind, topic, stance, quote, evidence, credibility, fit, n_sources, "
               "embedding FROM belief_candidates WHERE status='pending' AND created_at > now() - "
               "make_interval(days => %s)", (PENDING_DAYS,))
    rows = oc.fetchall() or []
    pool = pool if pool is not None else belief_pool(oc)
    anchors = anchors if anchors is not None else _anchors()
    out = []
    for (cid, origin, ref, title, kind, topic, stance, quote, ev, cred, fit, n, emb) in rows:
        if not emb:
            vv = embed([stance])
            if not vv:
                continue
            emb = vv[0]
            oc.execute("UPDATE belief_candidates SET embedding=%s WHERE id=%s", (emb, cid))
        emb = list(emb)
        nb, nc = nearest(emb, [(i, v) for i, _, v in pool])
        rel_key, rel = max(((k, cosine(emb, v)) for k, v in anchors.items()), key=lambda x: x[1], default=("", 0.0))
        gen = generality(stance)
        sc = value_score(gen, 1 - nc, rel, cred, n, fit)
        out.append({"id": cid, "origin": origin, "ref": ref, "title": title, "kind": kind, "topic": topic,
                    "stance": stance, "quote": quote, "evidence": list(ev or []), "credibility": cred, "fit": fit,
                    "n_sources": n, "embedding": emb, "near_id": nb, "near_cos": round(nc, 3),
                    "near_stance": next((s for i, s, _ in pool if i == nb), None),
                    "domain": rel_key, "relevance": round(rel, 3), "generality": gen, "score": sc})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def promote(oc, budget: int | None = None, dry: bool = False, use_llm: bool = True, hour: int | None = None) -> dict:
    """Spend this run's share of the daily budget on the best pending candidates."""
    ensure_schema(oc)
    stats = {"pending": 0, "new": 0, "reinforced": 0, "revised": 0, "contradicted": 0, "rejected": 0,
             "below_bar": 0, "expired": 0, "examples": []}
    if not dry:
        oc.execute("UPDATE belief_candidates SET status='expired', decided_at=now() WHERE status='pending' AND "
                   "created_at <= now() - make_interval(days => %s)", (PENDING_DAYS,))
        stats["expired"] = oc.rowcount or 0
    pool = belief_pool(oc)
    cands = score_pending(oc, pool=pool)
    stats["pending"] = len(cands)
    allow = budget if budget is not None else allowance(used_today(oc), datetime.now().hour if hour is None else hour)
    stats["allowance"] = allow
    shortlist = [c for c in cands if c["score"] >= MIN_SCORE][: max(allow * SHORTLIST_X, 6)]
    stats["below_bar"] = sum(1 for c in cands if c["score"] < MIN_SCORE)
    booked = 0
    for c in shortlist:
        near = c["near_stance"] if c["near_cos"] >= RELATE_COS else None
        if use_llm:
            d = parse_distill(llm_json(distill_prompt(c["stance"], c["title"] or "", c["origin"], c["quote"], near)))
        else:
            d = {"belief": c["stance"], "topic": c["topic"], "relation": "new", "why": "no model"}
        if not d:
            continue                                   # model down/garbled: stays pending for the next run
        belief = d["belief"]
        why = reject_reason(belief or "", c["title"] or "", c["kind"] or "") if belief else "model: not a belief"
        ex = {"candidate": c["id"], "score": c["score"], "before": c["stance"], "after": belief,
              "relation": d["relation"] if near else "new", "why": why or d["why"]}
        if why:
            stats["rejected"] += 1
            ex["outcome"] = "rejected"
            if not dry:
                oc.execute("UPDATE belief_candidates SET status='rejected', reason=%s, score=%s, decided_at=now() "
                           "WHERE id=%s", (why[:200], c["score"], c["id"]))
            stats["examples"].append(ex)
            continue
        rel = d["relation"] if near else "new"
        if rel == "new" and near is None and c["near_id"]:
            pass
        topic = topic_of(belief, d["topic"] or c["topic"])
        if rel == "same" and c["near_id"]:
            ex["outcome"] = f"reinforced #{c['near_id']}"
            stats["reinforced"] += 1
            if not dry:
                reinforce(oc, c["near_id"], c["evidence"], c["credibility"])
                _finish(oc, c, "reinforced", c["near_id"])
        elif rel == "refines" and c["near_id"]:
            ex["outcome"] = f"revised #{c['near_id']}"
            stats["revised"] += 1
            if not dry:
                oc.execute("SELECT confidence, evidence FROM beliefs WHERE id=%s", (c["near_id"],))
                old_conf, old_ev = oc.fetchone()
                conf = max(reinforced_conf(float(old_conf), c["credibility"]),
                           confidence_for(c["credibility"], c["n_sources"], c["fit"]))
                ev = list(dict.fromkeys(list(old_ev or []) + c["evidence"]))
                oc.execute("INSERT INTO beliefs (topic, stance, confidence, article_slug, evidence, first_held) "
                           "SELECT %s, %s, %s, article_slug, %s, first_held FROM beliefs WHERE id=%s RETURNING id",
                           (topic, belief, conf, ev, c["near_id"]))
                nid = oc.fetchone()[0]
                oc.execute("UPDATE beliefs SET active=false, superseded_by=%s, last_revised=now() WHERE id=%s",
                           (nid, c["near_id"]))
                ex["outcome"] += f" -> #{nid}"
                _finish(oc, c, "revised", nid)
        elif rel == "contradicts" and c["near_id"]:
            ex["outcome"] = f"contradicts #{c['near_id']} (confidence lowered)"
            stats["contradicted"] += 1
            if not dry:
                oc.execute("SELECT confidence FROM beliefs WHERE id=%s", (c["near_id"],))
                old = float(oc.fetchone()[0])
                oc.execute("UPDATE beliefs SET confidence=%s, last_revised=now(), evidence = array_cat(evidence, %s) "
                           "WHERE id=%s", (contradicted_conf(old, c["credibility"]),
                                           [f"contra:{e}"[:300] for e in c["evidence"][:2]], c["near_id"]))
                _finish(oc, c, "contradicts", c["near_id"])
        else:
            if booked >= allow:
                ex["outcome"] = "waits (budget)"
                stats["examples"].append(ex)
                continue
            conf = confidence_for(c["credibility"], c["n_sources"], c["fit"])
            ex["outcome"] = f"new (confidence {conf})"
            stats["new"] += 1
            booked += 1
            if not dry:
                if not take_cap(oc):
                    stats["new"] -= 1
                    ex["outcome"] = "daily cap reached"
                    stats["examples"].append(ex)
                    break
                slug = (f"{c['origin']}:{c['title']}" if c["title"] else c["origin"])[:200]
                oc.execute("INSERT INTO beliefs (topic, stance, confidence, article_slug, evidence) VALUES "
                           "(%s,%s,%s,%s,%s) RETURNING id", (topic, belief, conf, slug, c["evidence"]))
                nid = oc.fetchone()[0]
                vv = embed([belief])
                if vv:
                    oc.execute("INSERT INTO belief_vectors (belief_id, stance_md5, embedding) VALUES (%s,%s,%s) "
                               "ON CONFLICT (belief_id) DO NOTHING", (nid, hashlib.md5(belief.encode()).hexdigest(), vv[0]))
                    pool.append((nid, belief, vv[0]))
                ex["outcome"] += f" #{nid}"
                _finish(oc, c, "promoted", nid)
        if not dry:
            oc.execute("UPDATE belief_candidates SET score=%s, scores=%s WHERE id=%s",
                       (c["score"], json.dumps({k: c[k] for k in ("generality", "relevance", "domain", "near_cos")}),
                        c["id"]))
        stats["examples"].append(ex)
    return stats


def _finish(oc, c: dict, status: str, belief_id: int):
    oc.execute("UPDATE belief_candidates SET status=%s, belief_id=%s, decided_at=now() WHERE id=%s",
               (status, belief_id, c["id"]))
    _link_work(oc, c["ref"], c["origin"], belief_id)


def audit(oc, apply: bool = False, ids: list[int] | None = None) -> list[tuple[int, str, str]]:
    """Active beliefs that are malformed, scripture or trivia: [(id, reason, stance)]. --apply deactivates."""
    oc.execute("SELECT id, stance, coalesce(article_slug,'') FROM beliefs WHERE active" +
               (" AND id = ANY(%s)" if ids else ""), (ids,) if ids else None)
    bad = []
    for bid, stance, slug in oc.fetchall() or []:
        raw_issue = None
        if re.search(r"\s[,.;:!?]", stance or ""):
            raw_issue = "malformed (spacing)"
        elif re.match(r"^When\b[^,]+,\s*so\b", stance or ""):
            raw_issue = "malformed ('When X, so Y')"
        title = slug.split(":", 1)[1] if ":" in slug else slug
        why = raw_issue or reject_reason(clean_stance(stance), title.replace("work ", ""), "")
        if not why and SCRIPTURE_TITLES.fullmatch(title.strip()):
            why = "scripture"
        if why:
            bad.append((bid, why, stance))
    if apply and bad:
        oc.execute("UPDATE beliefs SET active=false, last_revised=now() WHERE id = ANY(%s)", ([b[0] for b in bad],))
    return bad


def rewrite_existing(oc, apply: bool = False, limit: int | None = None, use_llm: bool = True) -> dict:
    """One-off repair (2026-10-17) of the beliefs the old reader booked directly. Each active work_lessons belief is
    cleaned; a hard reject (scripture, fragment, trivia) or a model 'null' (plot, craft step, platitude) is
    DEACTIVATED (active=false, nothing deleted); otherwise the model's general restatement supersedes it
    (superseded_by, first_held kept, confidence calibrated from its independent works), and restatements that
    say the same thing merge into one belief."""
    oc.execute("SELECT id, topic, stance, confidence, coalesce(article_slug,''), evidence FROM beliefs WHERE active "
               "AND article_slug LIKE 'work_lessons:%%' ORDER BY id" + (" LIMIT %s" % int(limit) if limit else ""))
    rows = oc.fetchall() or []
    out = {"seen": len(rows), "deactivated": {}, "rewritten": 0, "merged": 0, "skipped": 0, "examples": []}
    made: list[tuple[int, list]] = []
    for bid, topic, stance, conf, slug, ev in rows:
        title = slug.split(":", 1)[1] if ":" in slug else slug
        clean = clean_stance(stance)
        why = reject_reason(clean, title, "")
        after = None
        if not why:
            quote = next((e[6:] for e in (ev or []) if e.startswith("quote:")), None)
            d = parse_distill(llm_json(distill_prompt(clean, title, "work_lessons", quote, None))) if use_llm else \
                {"belief": clean, "topic": topic, "relation": "new", "why": ""}
            if not d:
                out["skipped"] += 1
                continue
            after = d["belief"]
            why = reject_reason(after, title, "") if after else f"model: not a belief ({d['why'][:60]})"
        ex = {"id": bid, "before": stance, "after": after, "why": why}
        if why:
            key = why.split(" (")[0]
            out["deactivated"][key] = out["deactivated"].get(key, 0) + 1
            if apply:
                oc.execute("UPDATE beliefs SET active=false, last_revised=now() WHERE id=%s", (bid,))
            out["examples"].append(ex)
            continue
        n_works = len({e for e in (ev or []) if e.startswith("work:")}) or 1
        vec = (embed([after]) or [None])[0]
        if vec:
            anchors = _ANCHORS.get("v") or _ANCHORS.setdefault("v", _anchors())
            rel = max((cosine(vec, v) for v in anchors.values()), default=0.0)
            ex["score"] = value_score(generality(after), 0.35, rel, "unknown", n_works, 2)
            if ex["score"] < MIN_SCORE:
                ex["why"] = f"low value (score {ex['score']:.2f}, relevance {rel:.2f})"
                out["deactivated"]["low value"] = out["deactivated"].get("low value", 0) + 1
                if apply:
                    oc.execute("UPDATE beliefs SET active=false, last_revised=now() WHERE id=%s", (bid,))
                out["examples"].append(ex)
                continue
        twin, tc = nearest(vec, made) if vec else (None, 0.0)
        if twin and tc >= SAME_COS:
            out["merged"] += 1
            ex["merged_into"] = twin
            if apply:
                reinforce(oc, twin, list(ev or []), "unknown")
                oc.execute("UPDATE beliefs SET active=false, superseded_by=%s, last_revised=now() WHERE id=%s",
                           (twin, bid))
            out["examples"].append(ex)
            continue
        new_conf = confidence_for("unknown", n_works, 2)
        ex["confidence"] = f"{conf:.2f} -> {new_conf:.2f}"
        out["rewritten"] += 1
        if apply:
            oc.execute("INSERT INTO beliefs (topic, stance, confidence, article_slug, evidence, first_held) "
                       "SELECT %s, %s, %s, article_slug, evidence, first_held FROM beliefs WHERE id=%s RETURNING id",
                       (topic_of(after, topic), after, new_conf, bid))
            nid = oc.fetchone()[0]
            oc.execute("UPDATE beliefs SET active=false, superseded_by=%s, last_revised=now() WHERE id=%s", (nid, bid))
            ex["new_id"] = nid
            if vec:
                made.append((nid, vec))
                oc.execute("INSERT INTO belief_vectors (belief_id, stance_md5, embedding) VALUES (%s,%s,%s) ON CONFLICT "
                           "(belief_id) DO NOTHING", (nid, hashlib.md5(after.encode()).hexdigest(), vec))
        elif vec:
            made.append((-bid, vec))
        out["examples"].append(ex)
    return out


def connect():
    import psycopg2
    import nova_dsn
    c = psycopg2.connect(nova_dsn.pg_dsn("nova_ops"))
    c.autocommit = True
    return c.cursor()


def selftest():
    assert clean_stance("When FESA failed to mention Yarloop in alerts , so communities faced delayed evacuation .") == \
        "When FESA failed to mention Yarloop in alerts, communities faced delayed evacuation."
    assert clean_stance("the raised lip matches the antique's edge detail [3] .") == \
        "The raised lip matches the antique's edge detail."
    assert clean_stance("A fail-safe nobody can recall becomes the disaster — so automations need a recall path") == \
        "A fail-safe nobody can recall becomes the disaster, so automations need a recall path."
    assert reject_reason("When Israel remembers their slavery in Egypt, they observe God's statutes.") == "scripture"
    assert reject_reason("Strikes launched days before negotiations tend to collapse diplomacy with Iran and Israel.") is None
    assert reject_reason("Is this a belief?") in ("fragment", "question")
    assert reject_reason("Alerts that do not name the threatened community delay its evacuation.") is None
    assert generality(clean_stance("When Norm applies glue and brads minimally for the pedestal assembly, he "
                                   "maintains clean lines")) < 0.6
    assert generality("Pilots must trust instruments over bodily sensations during vestibular illusions.") > 0.8
    assert generality("When Norm turns the top with a face plate, the raised lip matches the antique's edge.") < 0.6
    assert confidence_for("established", 1) == 0.6 and confidence_for("established", 3) > 0.75
    assert confidence_for("opinion", 1) < confidence_for("observed", 1)
    assert reinforced_conf(0.6, "established") > 0.6 and reinforced_conf(0.95) <= MAX_CONF
    assert contradicted_conf(0.8) < 0.8
    assert allowance(0, 0) == 5 and allowance(50, 23) == 0 and allowance(10, 10) == 15
    hi = value_score(0.95, 0.4, 0.7, "established", 2, 2)
    lo = value_score(0.4, 0.4, 0.42, "established", 1, 1)
    assert hi > MIN_SCORE > lo
    assert value_score(1, 1, 1, "fringe") == 0
    d = parse_distill('{"belief": "alerts that skip the town name delay evacuation", "topic": "alert naming", '
                      '"relation": "refines"}')
    assert d["belief"] == "Alerts that skip the town name delay evacuation." and d["relation"] == "refines"
    assert not verdict_ok("-1.0") and not verdict_ok("Woodworking techniques don't translate to system design")
    assert not verdict_ok("A woodworking tutorial's precision mirrors Nova's operational rigor.")
    assert verdict_ok("Norm makes hand-cut joinery look calm; the router segments drag.")
    assert parse_distill('{"belief": null}')["belief"] is None and parse_distill("junk") is None
    print("selftest OK")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nova's belief candidates: rank, distill, promote, audit")
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--budget", type=int)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--rewrite", action="store_true", help="repair the old reader's beliefs (dry unless --apply)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--report", help="write a JSON report of --rewrite/--promote examples to this path")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    oc = connect()
    ensure_schema(oc)
    if a.promote:
        try:
            import nova_autonomy_safety
            if nova_autonomy_safety.kill_switch_engaged(oc):
                log("kill switch engaged — standing down")
                return 0
        except Exception:  # noqa: BLE001
            pass
        t0 = time.monotonic()
        s = promote(oc, budget=a.budget, dry=a.dry_run)
        for ex in s.pop("examples"):
            log(f"  [{ex['score']:.2f}] {ex.get('outcome')}: {ex['before'][:110]} => {ex['after'] or '-'} "
                f"({ex['why'][:60] if ex.get('why') else ''})")
        log(f"promote{' (dry)' if a.dry_run else ''}: {json.dumps(s)} in {time.monotonic() - t0:.0f}s")
    if a.audit:
        bad = audit(oc, apply=a.apply)
        by = {}
        for _, why, _ in bad:
            by[why] = by.get(why, 0) + 1
        for bid, why, stance in bad[:40]:
            log(f"  #{bid} {why}: {stance[:120]}")
        log(f"audit: {len(bad)} bad active belief(s) {by}{' — deactivated' if a.apply else ''}")
    if a.rewrite:
        r = rewrite_existing(oc, apply=a.apply, limit=a.limit)
        if a.report:
            Path(a.report).write_text(json.dumps(r, indent=1, default=str))
        for ex in r["examples"][:30]:
            log(f"  #{ex['id']} {ex['before'][:100]}\n      => {ex.get('after') or '-'} [{ex.get('why') or ex.get('confidence') or ''}]")
        log(f"rewrite{' APPLIED' if a.apply else ' (dry)'}: " + json.dumps({k: v for k, v in r.items() if k != 'examples'}))
    if a.stats:
        oc.execute("SELECT status, count(*), round(avg(score)::numeric,3) FROM belief_candidates GROUP BY 1 ORDER BY 2 DESC")
        for r in oc.fetchall():
            print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
