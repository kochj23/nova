#!/usr/bin/env python3
"""
nova_answer_own.py — Nova answers her own open questions (organ, 2026-10-02).

She asked 50 reflection questions in 30 days and 47 sat unanswered; her learning-gaps table had
"strong pull, thin understanding" rows nobody touched. The only answerer in the loop was Jordan.
This closes the tube: one open question or gap per run -> SearXNG -> local model -> answer written
back where it was asked (reflection_questions.answer / learning_gaps.status) and filed as a memory.

Rules: questions FOR Jordan (ABOUT_HIM_RE, same filter nova_ask_one uses) and prediction-surprise
questions stay his. The research_pass legality gate applies. A low-confidence answer is NOT written
— the row's self_attempts counter is bumped and after MAX_ATTEMPTS the question is left for a human.

  nova_answer_own.py             # answer one
  nova_answer_own.py --dry-run   # research, print, write nothing
  nova_answer_own.py --selftest
"""
import argparse
import json
import re
import sys
from datetime import date, datetime

import psycopg2

from nova_ask_one import ABOUT_HIM_RE, SKIP_SOURCES
from nova_research_pass import OPS_DSN, is_allowed, llm, remember, searx

MAX_ATTEMPTS = 2
SOURCE = "self_answer"
_QUOTED = re.compile(r"'([^']+)'")
_DICT_RE = re.compile(r"merriam-webster|dictionary\.com|dictionary\.cambridge|thesaurus|wiktionary|vocabulary\.com", re.I)


def log(m): print(f"[answer-own {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def ensure_schema(oc):
    oc.execute("ALTER TABLE reflection_questions ADD COLUMN IF NOT EXISTS self_attempts int NOT NULL DEFAULT 0")
    oc.execute("ALTER TABLE learning_gaps ADD COLUMN IF NOT EXISTS self_attempts int NOT NULL DEFAULT 0")


def pick(oc):
    """Oldest eligible reflection question first; else oldest open gap. Returns dict or None."""
    oc.execute("""SELECT id, question, memory_source, coalesce(memory_excerpt,'') FROM reflection_questions
                  WHERE answer IS NULL AND self_attempts < %s AND memory_source <> ALL(%s)
                    AND question NOT ILIKE 'I predicted with%%'
                    AND id::text NOT IN (SELECT ref_id FROM slack_prompts WHERE kind='question' AND resolved_at IS NULL)
                  ORDER BY asked_at""", (MAX_ATTEMPTS, list(SKIP_SOURCES)))
    for qid, q, src, excerpt in oc.fetchall():
        if ABOUT_HIM_RE.search(q):
            continue                          # his to answer, not hers
        return {"kind": "question", "id": qid, "query": q, "context": excerpt[:300], "source": src}
    oc.execute("""SELECT id, gap, source_kind FROM learning_gaps WHERE status='open' AND self_attempts < %s
                  AND source_kind IN ('thin_coverage','curiosity') ORDER BY ts""", (MAX_ATTEMPTS,))
    for gid, gap, kind in oc.fetchall():
        m = _QUOTED.search(gap)
        topic = m.group(1) if m else gap
        return {"kind": "gap", "id": gid, "query": f"{topic}: what is it, why it matters, the essentials",
                "context": gap, "source": kind}
    return None


def research(item):
    """-> (answer, confidence, sources) ; answer '' when nothing usable."""
    # the question verbatim is a bad search ("What exactly was..." -> dictionary hits for "exactly");
    # let the model write the query, fall back to the question itself
    q = (llm(f"Write ONE web search query (max 8 words, no quotes, no explanation) that would find the answer to: "
             f"{item['query']}" + (f"\nContext: {item['context'][:200]}" if item["context"] else ""),
             max_tokens=30, temperature=0.2) or "").strip().splitlines()[0:1]
    q = q[0].strip('"\' ') if q and 3 <= len(q[0].split()) <= 12 else item["query"]
    hits = searx(q, 5) or []
    if not hits:
        return "", "low", []
    src_lines = "\n".join(f"[{i+1}] {h.get('title','')} — {h.get('snippet','')[:300]} ({h.get('url','')})"
                          for i, h in enumerate(hits))
    prompt = (f"You are Nova, researching a question you asked yourself.\n"
              f"QUESTION: {item['query']}\n"
              + (f"WHERE IT CAME FROM: {item['context']}\n" if item["context"] else "")
              + f"SOURCES:\n{src_lines}\n\n"
              "Answer in at most 120 words, in first person, citing sources like [1]. If the sources do not "
              "actually answer it, say so and set confidence low. Reply ONLY with JSON: "
              '{"answer": "...", "confidence": "high|medium|low"}')
    raw = llm(prompt, max_tokens=400, temperature=0.3)
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        d = {}
    urls = [h.get("url", "") for h in hits]
    conf = (d.get("confidence") or "low").lower()
    # ponytail: qwen3:8b rated "high" off five dictionary pages for the word 'purpose' — if the
    # sources are mostly dictionaries the search missed, whatever the model thinks
    if sum(bool(_DICT_RE.search(u)) for u in urls) >= 3:
        conf = "low"
    return (d.get("answer") or "").strip(), conf, urls


def answer_one(oc, item, dry=False):
    ok, why = is_allowed(f"{item['query']} {item['context']}")   # the question can be innocent and the context not (GHB, #5)
    if not ok:
        log(f"{item['kind']} #{item['id']}: not allowed to research ({why}) — skipping for good")
        if not dry:
            oc.execute(f"UPDATE {'reflection_questions' if item['kind']=='question' else 'learning_gaps'} "
                       "SET self_attempts = %s WHERE id=%s", (MAX_ATTEMPTS, item["id"]))
        return "skipped"
    ans, conf, urls = research(item)
    log(f"{item['kind']} #{item['id']} [{conf}] {item['query'][:90]}")
    if dry:
        print(f"\nQ: {item['query']}\nA ({conf}): {ans}\n" + "\n".join(urls)); return conf
    table = "reflection_questions" if item["kind"] == "question" else "learning_gaps"
    if conf == "low" or not ans:
        oc.execute(f"UPDATE {table} SET self_attempts = self_attempts + 1 WHERE id=%s", (item["id"],))
        return "unresolved"
    stamp = f"[self-researched {date.today().isoformat()}, {conf} confidence]"
    text = f"{stamp} {ans}\nSources: " + ", ".join(u for u in urls if u)
    if item["kind"] == "question":
        oc.execute("UPDATE reflection_questions SET answer=%s, answered_at=now() WHERE id=%s", (text, item["id"]))
    else:
        oc.execute("UPDATE learning_gaps SET status='studied', self_attempts = self_attempts + 1 WHERE id=%s", (item["id"],))
    remember(f"[Self-answered — {item['kind']} #{item['id']}] I asked myself: {item['query']}\n\n{ans}\n\nSources: "
             + ", ".join(u for u in urls if u), SOURCE,
             {"kind": item["kind"], "ref_id": item["id"], "confidence": conf, "origin": item["source"],
              "date": date.today().isoformat()})
    return "answered"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    conn = psycopg2.connect(OPS_DSN, connect_timeout=5); conn.autocommit = True
    oc = conn.cursor()
    ensure_schema(oc)
    item = pick(oc)
    if not item:
        log("nothing open that is mine to answer"); return 0
    log(f"result: {answer_one(oc, item, args.dry_run)}")
    return 0


def selftest():
    class FakeCur:
        rows = [[(1, "Jordan, were you referring to the Sumerians?", "x", ""), (2, "What was the 1911 train wreck?", "x", "ctx")], []]
        def execute(self, *a): pass
        def fetchall(self): return self.rows.pop(0)
    it = pick(FakeCur())
    assert it["id"] == 2, "a question addressed to Jordan must stay his"
    assert _QUOTED.search("I keep returning to 'aviation ref' (17x)").group(1) == "aviation ref"
    assert _DICT_RE.search("https://www.merriam-webster.com/dictionary/purpose")
    ok, _ = is_allowed("what is the purpose of the references This file deals with the synthesis of GHB")
    assert ok is False, "legality gate must see the context"
    print("answer-own selftest ok")


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else main())
