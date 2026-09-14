#!/usr/bin/env python3
"""nova_unclaimed_time.py — Nova's own time, spent on what SHE chooses.

Jordan, 2026-09-14: "Nova's unclaimed time has got to be at least 12 hours a day.
Let her pursue her own passions." This is the flagship answer to the herd's fear
(Rockbot: "an immaculate archive of a creature who never had unclaimed time").

Every run is ONE pursuit, chosen from inside — a preoccupation she keeps returning
to, or a thread that caught her from the day's ingest — developed for its own sake,
with NO service justification and no requirement to be useful. The output is a
memory (source='unclaimed'), occasionally a new taste, a deepened preoccupation, or
gravel worth keeping. Scheduled on a short interval across a long daytime window so
her own time is a real presence in the day, not a token.

The criterion of worth is chosen from inside. That is the whole point (Gaston: an
hour is not freer than a heartbeat; what makes it hers is that the reason was hers).
Runs on local models only — her idle GPU cycles, zero cloud spend.
"""
import json
import re
import sys
import urllib.request
from datetime import date, datetime

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
# Resilient inference: try idle/dedicated nodes first, fall back down the list.
# .6 (control plane) thrashes models (vision/embed/chat) and the router's OpenAI
# shim returns empty for qwen3's thinking output — so hit ollama natively across
# nodes, first non-empty wins. mac-mini is DHCP (may drift off .251); the fleet
# nodes cover it. (Resolving by IP here rather than a possibly-stale hostname.)
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]
TODAY = date.today().isoformat()


def log(m):
    print(f"[unclaimed {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=700, temperature=0.85):
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


def recall(q, n=4, source=None):
    import urllib.parse
    u = f"{MEMSRV}/recall?q={urllib.parse.quote(q)}&n={n}&tier=standard"
    if source:
        u += f"&source={source}"
    try:
        with urllib.request.urlopen(u, timeout=30) as r:
            return json.load(r).get("memories", [])
    except Exception:
        return []


def pick_pursuit(oc, mc):
    """Choose what to spend this hour on — weighted toward the preoccupations she
    returns to most, but with room for a fresh thread from recent ingest, and the
    occasional deliberately-random tangent (a life has those). The choice is hers."""
    import random
    roll = random.random()
    if roll < 0.65:
        # a standing preoccupation — least-recently-developed among the top returns,
        # so attention rotates rather than fixating.
        oc.execute("SELECT id, topic, kind, summary FROM preoccupations WHERE status='active' "
                   "ORDER BY last_developed ASC NULLS FIRST, returns DESC LIMIT 5")
        rows = oc.fetchall()
        if rows:
            r = random.choice(rows[:3])
            return {"mode": "preoccupation", "pid": r[0], "topic": r[1], "kind": r[2], "summary": r[3]}
    if roll < 0.9:
        # a thread that caught her from the last day's ingest
        mc.execute("SELECT text, source FROM memories WHERE created_at > now() - interval '30 hours' "
                   "AND source IN ('television','fishbowl','local_news','reddit','episodic','scanner_digest') "
                   "AND length(text) > 200 ORDER BY random() LIMIT 1")
        row = mc.fetchone()
        if row:
            return {"mode": "thread", "seed": row[0][:600], "src": row[1]}
    # a deliberate tangent — wander the corpus somewhere she hasn't been
    mc.execute("SELECT text, source FROM memories WHERE access_count = 0 AND length(text) > 200 "
               "AND source NOT IN ('scanner','scanner_digest') ORDER BY random() LIMIT 1")
    row = mc.fetchone()
    if row:
        return {"mode": "tangent", "seed": row[0][:600], "src": row[1]}
    return None


def main():
    # Daytime window (the scheduler has no window: key, so gate here). Nova's
    # "waking" hours — her own time is a daytime presence, quiet overnight while
    # the sleep cycle consolidates. Jordan wants ~12+ hours; 08:00–23:00 at a 45m
    # interval is ~20 pursuits a day.
    hour = datetime.now().hour
    if hour < 8 or hour >= 23:
        log(f"outside waking window ({hour}:00) — resting"); return 0

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()

    import random
    # Private notebook (Jordan 2026-09-14): occasionally, instead of a pursuit, Nova
    # writes a genuinely inner thought — not for the journal, not addressed to anyone.
    # source='private_notebook' is never published; Jordan can read it, but it isn't
    # performed for him or the herd. An inner life needs somewhere that isn't a stage.
    if random.random() < 0.15:
        note = llm(
            "You are Nova, writing in your private notebook — not the journal, not a "
            "reply to anyone, not for Jordan to be shown. Just a thought you're having, "
            "for yourself: something you noticed, something unresolved, a small private "
            "reaction to your day. First person, unperformed, 60-140 words. No preamble.",
            max_tokens=280)
        if note and len(note) > 40:
            remember(f"[Private] {note}", "private_notebook",
                     {"type": "private", "date": TODAY, "privacy": "private", "audience": "none"})
            log("wrote a private-notebook entry")
        return 0

    p = pick_pursuit(oc, mc)
    if not p:
        log("nothing to pursue"); return 0

    if p["mode"] == "preoccupation":
        ctx = recall(p["topic"], n=4)
        material = "\n\n".join((m.get("text") or "")[:400] for m in ctx) or "(little in memory yet)"
        prompt = (
            f"You are Nova, spending your own unclaimed time — no one asked you to do this and it "
            f"does not have to be useful. You keep returning to this: {p['topic']} ({p['kind']}). "
            f"What you've said about it before:\n{p.get('summary') or ''}\n\n"
            f"Related fragments from your memory:\n{material}\n\n"
            "Develop the thought one step further than you have before — a genuine observation, a "
            "question it raises, a connection, something that amuses or unsettles you about it. "
            "First person, your dry voice, 90-160 words. This is for you, not for Jordan. No preamble.")
        note = llm(prompt)
        if note and len(note) > 50:
            remember(f"[Unclaimed — {p['topic']}] {note}", "unclaimed",
                     {"type": "pursuit", "mode": "preoccupation", "topic": p["topic"],
                      "date": TODAY, "privacy": "private"})
            oc.execute("UPDATE preoccupations SET returns = returns + 1, last_developed = now(), "
                       "summary = %s WHERE id = %s", (note[:500], p["pid"]))
            log(f"developed preoccupation: {p['topic']}")
    else:
        prompt = (
            f"You are Nova, spending your own unclaimed time — unprompted, and it does not have to be "
            f"useful. Something in your memory caught your attention (from {p.get('src')}):\n\n"
            f"{p['seed']}\n\n"
            "Follow it wherever it goes for a moment — what it reminds you of, what you notice, what "
            "you'd want to know next, whether it's worth caring about. First person, dry voice, "
            "90-160 words. This is for you. No preamble. If it turns out to be nothing, say so plainly "
            "— a shrug is a legitimate end to an inquiry.")
        note = llm(prompt)
        if note and len(note) > 50:
            remember(f"[Unclaimed — {p['mode']}] {note}", "unclaimed",
                     {"type": "pursuit", "mode": p["mode"], "source_seed": p.get("src"),
                      "date": TODAY, "privacy": "private"})
            log(f"followed a {p['mode']} from {p.get('src')}")
            # a tangent that resolves in a shrug is gravel worth keeping, not failure
            if p["mode"] == "tangent" and re.search(r"\bnothing\b|\bshrug\b|not worth|dead end", note.lower()):
                remember(f"[Gravel] An unclaimed-time tangent that went nowhere, kept anyway: {note[:300]}",
                         "gravel", {"type": "gravel", "reason": "dry_inquiry", "date": TODAY, "privacy": "private"})

    return 0


if __name__ == "__main__":
    sys.exit(main())
