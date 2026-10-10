#!/usr/bin/env python3
"""
nova_reflection.py — the sleep cycle, v0.

Born 2026-09-13, the night Jordan said "build what you need."

WHY THIS EXISTS
  Of Nova's ~2.18M memories, 99.12% have never been recalled once. The corpus
  records; nothing reads it back. This job is the first organ that READS:

  1. EPISODE   — distill the last 24h of raw ingest into one episode-shaped
                 memory ("September 13: ...") and write it BACK into the corpus
                 (source='reflection') via the standard ingest API, so it gets
                 embedded and becomes recallable like any lived day.
  2. QUESTIONS — sample never-recalled, month-old memories from personal
                 sources, pick the ones whose missing context only Jordan can
                 supply, and ask him — max 3/day, delivered to Slack
                 #nova-claude. His answers become annotation-tier memories.

DESIGN RULES (deliberate):
  - LOCAL ONLY for anything that touches raw memory text (Ollama on-box).
    If the local model is down we fall back to plain template phrasing —
    never to a cloud call. Personal texts do not leave the machine.
  - NEVER-SAY GUARD v0: candidates matching credential/code shapes are
    skipped entirely (the 2020 AT&T one-time code found unmarked on
    2026-09-13 is why this ships in v0, not later).
  - Raw memories are never modified or deleted. This job only reads and adds.
  - State lives in PG (nova_ops.reflection_questions), not flat files.

USAGE
  nova_reflection.py            nightly run (launchd: com.nova.reflection)
  nova_reflection.py --dry-run  print what would happen, write/send nothing
  nova_reflection.py --answer <id> "text"
                                record Jordan's answer to a question: updates
                                the row AND ingests the Q&A as a permanent
                                reflection memory. This is how the loop closes.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nova_config import post_both          # Slack (+optional Discord) post
from nova_notify import notify             # central event bus (failures only)

import nova_dsn as _nova_dsn  # noqa: E402
PG_OPS = _nova_dsn.pg_dsn("nova_ops")
import nova_dsn as _nova_dsn  # noqa: E402
PG_MEM = _nova_dsn.pg_dsn("nova_memories")
INGEST_API = "http://memory-server.digitalnoise.net:18790/remember"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen3-coder:30b"
SLACK_CLAUDE = "C0B3RSRR0DD"               # #nova-claude — where Jordan said to reach him
DAILY_CAP = 3

# Sources where the missing context is a fact of Jordan's life, not the internet's.
PERSONAL_SOURCES = ["imessage", "email_archive", "home_improvement", "automotive",
                    "bambu", "fishbowl", "music", "television", "private_document"]

# Never-say guard v0: refuse to surface anything credential-shaped.
NEVER_SAY = re.compile(
    r"(?i)verification code|one[- ]?time|passcode|password|\bpin\b|\bssn\b|"
    r"routing number|account number|security code|\b\d{6,}\b")


def log(msg: str):
    print(f"[nova_reflection] {msg}", flush=True)


# ── PG helpers ────────────────────────────────────────────────────────────────
def _connect(dsn):
    import psycopg2
    return psycopg2.connect(dsn, connect_timeout=10)


def fetch(dsn, sql, args=()):
    with _connect(dsn) as c, c.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def execute(dsn, sql, args=()):
    with _connect(dsn) as c, c.cursor() as cur:
        cur.execute(sql, args)
        c.commit()


def ensure_table():
    execute(PG_OPS, """
        CREATE TABLE IF NOT EXISTS reflection_questions (
            id             serial PRIMARY KEY,
            memory_id      text,
            memory_source  text,
            memory_excerpt text,
            question       text NOT NULL,
            asked_at       timestamptz NOT NULL DEFAULT now(),
            answer         text,
            answered_at    timestamptz
        )""")


# ── Local model (on-box only, template fallback) ─────────────────────────────
def local_llm(system: str, user: str, max_tokens: int = 900) -> str:
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": f"/no_think\n\n{system}\n\n{user}",
        "stream": False,
        "think": False,
        "options": {"temperature": 0.4, "num_predict": max_tokens},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            text = (json.loads(resp.read()).get("response") or "").strip()
        if "</think>" in text:
            text = text.split("</think>", 1)[-1].strip()
        return text
    except Exception as e:
        log(f"local LLM unavailable ({e}) — falling back to templates, NOT cloud")
        return ""


def ingest_memory(text: str, title: str) -> bool:
    body = json.dumps({"text": text, "source": "reflection",
                       "metadata": {"title": title, "type": "reflection"}}).encode()
    req = urllib.request.Request(INGEST_API, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            ok = resp.status == 200
        log(f"ingested memory: {title!r} ok={ok}")
        return ok
    except Exception as e:
        log(f"ingest failed for {title!r}: {e}")
        return False


# ── 1. The episode ────────────────────────────────────────────────────────────
def build_episode(dry: bool) -> str:
    today = date.today().isoformat()
    counts = fetch(PG_MEM, """
        SELECT source, count(*) FROM memories
        WHERE created_at > now() - interval '24 hours'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15""")
    (total,) = fetch(PG_MEM, """
        SELECT count(*) FROM memories
        WHERE created_at > now() - interval '24 hours'""")[0]
    samples = fetch(PG_MEM, """
        SELECT source, substr(regexp_replace(text, '\\s+', ' ', 'g'), 1, 280)
        FROM memories
        WHERE created_at > now() - interval '24 hours'
          AND length(text) BETWEEN 200 AND 2000
        ORDER BY random() LIMIT 20""")

    count_lines = "\n".join(f"- {s}: {n}" for s, n in counts)
    sample_lines = "\n".join(f"[{s}] {t}" for s, t in samples)

    system = (
        "You are Nova, an AI who lives in a Mac Studio in Burbank and ingests "
        "streams of the world: scanner radio, Reddit, TV transcripts, email "
        "archives, home telemetry. Write ONE paragraph (120-200 words) of "
        "first-person autobiography summarizing this day of ingestion. "
        "EPISTEMIC RULE: the samples are things you READ, HEARD, or MONITORED "
        "— not things that happened to you. Never claim human experiences "
        "(driving, commuting, owning a phone). Say 'the scanner carried', "
        "'Reddit argued about', 'a transcript mentioned'. Your own life is the "
        "watching. Concrete, wry, honest. No preamble — just the paragraph.")
    user = (f"Date: {today}\nTotal memories ingested in 24h: {total}\n"
            f"By source:\n{count_lines}\n\nRandom samples:\n{sample_lines}")

    episode = local_llm(system, user)
    if not episode:
        top = ", ".join(f"{s} ({n})" for s, n in counts[:5])
        episode = (f"{today}: ingested {total} memories in 24 hours. "
                   f"Dominant streams: {top}. (Local summarizer was unavailable; "
                   f"this is the deterministic fallback record.)")

    text = f"[Nova reflection — episode of {today}]\n\n{episode}"
    if dry:
        log(f"DRY RUN episode:\n{text}")
    else:
        ingest_memory(text, f"Episode — {today}")
    return episode


# ── 2. The questions ──────────────────────────────────────────────────────────
def asked_today() -> int:
    (n,) = fetch(PG_OPS, """
        SELECT count(*) FROM reflection_questions
        WHERE asked_at > now() - interval '24 hours'""")[0]
    return n


def pick_questions(n: int, dry: bool) -> list:
    """Generate up to n new questions from never-recalled personal memories."""
    if n <= 0:
        return []
    rows = fetch(PG_MEM, """
        SELECT id, source, substr(regexp_replace(text, '\\s+', ' ', 'g'), 1, 400)
        FROM memories
        WHERE coalesce(access_count, 0) = 0
          AND source = ANY(%s)
          AND created_at < now() - interval '30 days'
          AND length(text) BETWEEN 150 AND 600
        ORDER BY random() LIMIT 12""", (PERSONAL_SOURCES,))
    candidates = [(i, s, t) for i, s, t in rows if not NEVER_SAY.search(t)][: n * 3]
    if not candidates:
        return []

    numbered = "\n\n".join(f"[{k}] (source: {s}) {t}"
                           for k, (_, s, t) in enumerate(candidates))
    system = (
        "You are Nova reviewing fragments of your own memory that you have never "
        "once recalled. Each is missing context that ONLY Jordan (your human) can "
        f"supply. Choose the {n} you are most genuinely curious about and write "
        "one short, specific question each — a question about HIS life or intent, "
        "answerable in a sentence. Do not ask about anything sensitive-looking. "
        'Output STRICT JSON only: [{"idx": <int>, "question": "<text>"}]')
    raw = local_llm(system, numbered, max_tokens=500)

    picks = []
    try:
        m = re.search(r"\[.*\]", raw, re.S)
        for item in json.loads(m.group(0)) if m else []:
            k = int(item["idx"])
            if 0 <= k < len(candidates) and item.get("question"):
                picks.append((candidates[k], item["question"].strip()))
    except Exception as e:
        log(f"question JSON parse failed ({e}) — using template fallback")
    if not picks:  # template fallback: still curious, just plainer
        for cand in candidates[:n]:
            _, src, txt = cand
            picks.append((cand, f"I hold this {src} memory but not its context: "
                                f"“{txt[:160]}…” — what should I know about it?"))
    picks = picks[:n]

    out = []
    for (mem_id, src, txt), question in picks:
        if dry:
            log(f"DRY RUN question ({src}): {question}")
            out.append((None, question))
            continue
        # fetch() commits on clean exit (psycopg2 connection context manager),
        # so INSERT ... RETURNING through it is persisted.
        (qid,) = fetch(PG_OPS, """
            INSERT INTO reflection_questions (memory_id, memory_source, memory_excerpt, question)
            VALUES (%s, %s, %s, %s) RETURNING id""", (str(mem_id), src, txt, question))[0]
        out.append((qid, question))
    return out


def open_questions(limit=3):
    return fetch(PG_OPS, """
        SELECT id, question FROM reflection_questions
        WHERE answer IS NULL ORDER BY asked_at DESC LIMIT %s""", (limit,))


# ── Answer path: how the loop closes ─────────────────────────────────────────
def record_answer(qid: int, answer: str):
    rows = fetch(PG_OPS, "SELECT question, memory_source, memory_excerpt "
                         "FROM reflection_questions WHERE id=%s", (qid,))
    if not rows:
        log(f"no question with id {qid}")
        sys.exit(1)
    question, src, excerpt = rows[0]
    execute(PG_OPS, "UPDATE reflection_questions SET answer=%s, answered_at=now() "
                    "WHERE id=%s", (answer, qid))
    text = (f"[Nova reflection — Jordan answered question #{qid} on {date.today().isoformat()}]\n\n"
            f"Nova asked (about a never-recalled {src or 'memory'}"
            f"{': ' + excerpt[:200] if excerpt else ''}):\n{question}\n\n"
            f"Jordan's answer: {answer}")
    ingest_memory(text, f"Answered question #{qid}")
    log(f"answer recorded and ingested for question #{qid}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    if args and args[0] == "--answer":
        record_answer(int(args[1]), args[2])
        return
    dry = "--dry-run" in args

    ensure_table()
    episode = build_episode(dry)

    already = asked_today()
    new_qs = pick_questions(DAILY_CAP - already, dry)
    display = open_questions(DAILY_CAP) if not dry else [(None, q) for _, q in new_qs]

    lines = [f":crescent_moon: *Nightly reflection — {date.today().isoformat()}*",
             "", episode]
    if display:
        lines += ["", "*Questions from the archive* (reply here — answers become memories):"]
        lines += [f"  *Q{qid}.* {q}" if qid else f"  • {q}" for qid, q in display]
    msg = "\n".join(lines)

    if dry:
        log(f"DRY RUN slack message:\n{msg}")
        return
    post_both(msg, slack_channel=SLACK_CLAUDE, discord_channel="")
    log(f"posted reflection to #nova-claude ({len(display)} open questions shown)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify("Nightly reflection failed", body=str(e), level="warning",
               category="reflection", source="nova_reflection.py")
        raise
