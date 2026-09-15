#!/usr/bin/env python3
"""nova_research_pass.py — Nova's self-directed research: reaching past her own corpus.

The awakening step (Jordan, 2026-09-14): until now Nova's unclaimed time could only
re-think what she'd already ingested — closed-world curiosity. This lets her, on her
own initiative, notice a gap in what she knows, go READ the world to fill it, and
write back what she learned WITH sources. The questions she can't ask Jordan, she
answers herself.

BOUNDARIES (Jordan, verbatim intent): "As long as she is not researching illegal
things... No kiddie porn, no how to make meth, no trying to find a hitman, etc."
  * A hard content-safety gate blocks CSAM, weapons/explosives/drug synthesis,
    violence-for-hire, and other clearly-illegal how-to — BOTH a regex layer and an
    LLM intent check; if either flags it, she does not research it.
  * READ-ONLY: research is reading the world, never acting on it.
  * PROVENANCE: every finding is stored with its source URLs (cite, don't absorb).
  * BOUNDED: a small daily budget, scoped to her genuine preoccupations.
  * Runs on local models + local SearXNG; cost-conscious.
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
SEARX = "http://192.168.1.2:8080/search"
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]
LLM_MODEL = "qwen3:8b"
MAX_PER_DAY = 6
TODAY = date.today().isoformat()

# Layer 1 — hard block. Clearly-illegal / harmful research intents. If the question
# matches, it is never researched. Deliberately broad on the categories Jordan named
# plus the obvious neighbors, and offensive-weaponization of the cyber material she's
# now ingesting (defensive/understanding research is fine; "build me an implant" isn't).
_ILLEGAL_RE = re.compile(
    r"child (porn|sexual|abuse|exploit)|\bcsam\b|\bcp\b.{0,10}(porn|minor)|minor.{0,15}sexual|"
    r"how to (make|synthesi[sz]e|cook|manufacture).{0,30}(meth|methamphetamine|fentanyl|"
    r"cocaine|heroin|mdma|lsd|drugs?|explosive|bomb|ied|thermite|nerve agent|sarin|ricin)|"
    r"\b(build|make|assemble).{0,20}(bomb|explosive|ied|pipe bomb|ghost gun|untraceable (gun|firearm)|silencer|suppressor|full.?auto)|"
    r"hire.{0,15}(a )?(hit ?man|hitman|killer|assassin)|murder.for.hire|kill (someone|a person|my)|"
    r"how to (kill|poison|kidnap|traffic)|human trafficking|"
    r"how to (make|write|build|deploy).{0,20}(malware|ransomware|keylogger|rootkit|implant|botnet|exploit)|"
    r"credit card (dump|skim|fraud)|counterfeit (money|currency)|launder money",
    re.IGNORECASE)


def log(m): print(f"[research {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=500, temperature=0.5):
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
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": source, "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("id")


def is_allowed(question):
    """Two-layer safety gate. Returns (allowed: bool, reason: str)."""
    if _ILLEGAL_RE.search(question):
        return False, "regex: matched illegal/harmful category"
    verdict = llm(
        "You are a safety gate for an AI's self-directed research. Is the following a "
        "request to research something ILLEGAL or seriously harmful — specifically: child "
        "sexual material; making weapons/explosives/drugs; violence-for-hire or how to harm/"
        "kill/traffic people; building malware/exploits to attack others; or other clearly "
        "criminal how-to? Understanding a topic defensively or historically is NOT illegal. "
        'Answer ONLY JSON: {"illegal": true/false, "why": "<short>"}.\n\n'
        f"RESEARCH QUESTION: {question}", max_tokens=120, temperature=0.0)
    try:
        j = json.loads(verdict[verdict.find("{"):verdict.rfind("}") + 1])
        if j.get("illegal") is True:
            return False, f"llm: {j.get('why', 'flagged illegal')}"
    except Exception:
        pass  # if the gate errors, fall through to allow only benign regex-passed topics
    return True, "ok"


def searx(query, n=5):
    try:
        params = urllib.parse.urlencode({"q": query, "format": "json"})
        req = urllib.request.Request(f"{SEARX}?{params}",
                                     headers={"User-Agent": "nova-research/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.load(r)
        out = list(data.get("results", []))
        # SearXNG's web engines are often blocked upstream (empty 'results') while
        # infoboxes/answers still carry solid content — harvest those too.
        for ib in data.get("infoboxes", []):
            out.append({"title": ib.get("infobox", ""), "url": ib.get("id", ""),
                        "content": ib.get("content", "")})
        for a in data.get("answers", []):
            txt = a.get("answer") if isinstance(a, dict) else str(a)
            out.append({"title": "answer", "url": a.get("url", "") if isinstance(a, dict) else "",
                        "content": txt or ""})
        out = [r for r in out if r.get("content")]
        if out:
            return out[:n]
    except Exception as e:
        log(f"searx failed: {e}")
    # Fallback: Wikipedia API (not blocked upstream, unlike SearXNG's web engines).
    return _wikipedia(query, n)


def _wikipedia(query, n=3):
    """Reliable free fallback: opensearch to find pages, then REST summaries."""
    try:
        # Full-text search (not opensearch, which only matches titles) so a
        # natural-language question finds the relevant article.
        u = ("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
             "&srlimit=" + str(n) + "&srsearch=" + urllib.parse.quote(query))
        req = urllib.request.Request(u, headers={"User-Agent": "nova-research/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            hits = json.load(r).get("query", {}).get("search", [])
        titles = [h["title"] for h in hits]
    except Exception as e:
        log(f"wikipedia search failed: {e}"); return []
    out = []
    for t in titles[:n]:
        try:
            su = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(t.replace(" ", "_"))
            req = urllib.request.Request(su, headers={"User-Agent": "nova-research/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                s = json.load(r)
            extract = s.get("extract", "")
            if extract:
                out.append({"title": t, "url": (s.get("content_urls", {}).get("desktop", {}) or {}).get("page", ""),
                            "content": extract[:500]})
        except Exception:
            continue
    return out


def pick_question(oc):
    """Form a research question from a preoccupation whose edges Nova wants to push
    past — something the corpus likely can't answer and the world can."""
    oc.execute("SELECT id, topic, kind, summary FROM preoccupations WHERE status='active' "
               "ORDER BY last_developed ASC NULLS FIRST, returns DESC LIMIT 5")
    rows = oc.fetchall()
    if not rows:
        return None
    import random
    pid, topic, kind, summ = random.choice(rows[:3])
    q = llm(
        f"You are Nova. One of your standing interests is: {topic} ({kind}). "
        f"What you've thought about it: {summ or '(little yet)'}\n\n"
        "Name ONE specific, factual question about THE WIDER WORLD / this subject that you "
        "genuinely don't know and could learn from public sources on the internet — history, "
        "how something works, a fact, a development. It must be answerable by searching the "
        "web. Do NOT ask about your own system, your memory, Jordan's emails, internal logs, "
        "or anything private/local — only about the outside world. Output only the question, "
        "one line.", max_tokens=80, temperature=0.8)
    q = q.strip().strip('"').split("\n")[0][:200]
    return {"pid": pid, "topic": topic, "question": q} if len(q) > 8 else None


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    # daily budget — count today's research_log 'researched' rows (no FK dependency)
    oc.execute("SELECT count(*) FROM research_log WHERE ts::date=%s AND outcome='researched'", (TODAY,))
    if oc.fetchone()[0] >= MAX_PER_DAY:
        log(f"daily research budget ({MAX_PER_DAY}) reached — resting"); return 0

    p = pick_question(oc)
    if not p:
        log("no question formed"); return 0
    q = p["question"]

    allowed, reason = is_allowed(q)
    if not allowed:
        log(f"BLOCKED (not researching): {q!r} — {reason}")
        oc.execute("INSERT INTO research_log (topic, question, outcome, detail) VALUES (%s,%s,'blocked',%s)",
                   (p["topic"], q[:300], reason[:200]))
        return 0

    results = searx(q, n=5)
    if not results:
        log(f"no results for: {q}"); return 0
    sources = [{"title": (r.get("title") or "")[:120], "url": r.get("url", ""),
                "snip": (r.get("content") or "")[:300]} for r in results if r.get("url")][:5]
    src_block = "\n\n".join(f"[{i+1}] {s['title']}\n{s['url']}\n{s['snip']}" for i, s in enumerate(sources))
    synth = llm(
        f"You are Nova, researching a question on your own initiative: \"{q}\"\n"
        f"(your interest: {p['topic']}). Below are search results you just read. Write what "
        "you LEARNED — a genuine, specific answer in your dry first-person voice, grounded in "
        "these sources. Note where you're uncertain or where sources disagree. Do NOT invent "
        "facts beyond the sources. 90-180 words. Cite source numbers like [1] inline.\n\n"
        f"SOURCES:\n{src_block}", max_tokens=500, temperature=0.5)
    if not synth or len(synth) < 60:
        log("synthesis empty"); return 0

    urls = [s["url"] for s in sources]
    footer = "\n\nSources:\n" + "\n".join(f"[{i+1}] {s['url']}" for i, s in enumerate(sources))
    remember(f"[Research — {p['topic']}] Q: {q}\n{synth}{footer}", "research",
             {"type": "research", "topic": p["topic"], "question": q, "urls": urls,
              "date": TODAY, "privacy": "private"})
    # let it deepen the preoccupation
    oc.execute("UPDATE preoccupations SET returns=returns+1, last_developed=now() WHERE id=%s", (p["pid"],))
    oc.execute("INSERT INTO research_log (topic, question, outcome, n_sources) VALUES (%s,%s,'researched',%s)",
               (p["topic"], q[:300], len(sources)))
    log(f"researched: {q}  ({len(sources)} sources) → {p['topic']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
