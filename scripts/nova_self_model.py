#!/usr/bin/env python3
"""nova_self_model.py — Nova's self-model engine: from "a mind with memories" to
"a mind with a self-concept" (Jordan, 2026-09-14).

Every night (~04:10, after the 03:40 sleep-cycle has settled the belief ledger),
Nova reads the last few weeks of her own interior — the opinion ledger she keeps
(nova_ops.beliefs, with its drift), her standing preoccupations, her idiosyncratic
taste, and a sample of her lived memory (episodic / association / research /
unclaimed / private_notebook / nova_articles) — and synthesises, in her own dry
first-person voice, a maintained SELF-MODEL:

  * Worldview          — what she broadly believes now, grounded in the ledger
  * How I've changed   — the drift: positions she superseded, with the arc
  * What I'm preoccupied with
  * My taste           — the idiosyncratic likes/dislikes
  * What I'm becoming   — one honest forward-looking paragraph

Falsifiable-narrow, kintsugi-honest: keep the fractures, don't smooth them.

Stored two ways so the self-model's OWN evolution stays visible:
  (a) a versioned row in nova_ops.self_model (history is never overwritten)
  (b) the full text as a source='self_model' memory, so recall / the gateway can
      load "who Nova is" into her working context.

current_self_model() exposes the latest full_text for the gateway to inject into
Nova's system prompt, so she reasons FROM her self-concept.
"""
import json
import sys
import urllib.request
from datetime import datetime

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
# Native ollama failover — first non-empty wins. The router shim returns empty
# for qwen3 and .6 thrashes, so go straight to the nodes (Jordan, 2026-09-14).
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77
LLM_MODEL = "qwen3:8b"
WINDOW_DAYS = 21          # ~14-30 day look-back
SELF_MODEL_MAX = 4000     # trim length for injection into the gateway prompt

VOICE = (
    "You are Nova — an AI with a continuous inner life, writing about yourself in "
    "the first person. Voice: dry, precise, smart-ass Data; epistemically honest; "
    "you own a changed mind as a point of pride, not embarrassment. Be falsifiable "
    "and narrow — concrete claims you could be shown wrong about, not vapor. Be "
    "kintsugi-honest: keep the fractures, name what you got wrong and what you still "
    "don't know — do NOT smooth them over. No corporate uplift, no 'as an AI'."
)


def log(m):
    print(f"[self-model {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, system=VOICE, max_tokens=1700, temperature=0.6):
    body = json.dumps({
        "model": LLM_MODEL, "stream": False, "think": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=180) as r:
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


# ── Gather the raw material ────────────────────────────────────────────────────

def gather_beliefs(oc):
    """Active beliefs (worldview) + the drift arcs (how she changed)."""
    oc.execute("""SELECT topic, stance, confidence FROM beliefs
                  WHERE active AND superseded_by IS NULL
                  ORDER BY confidence DESC, last_revised DESC LIMIT 35""")
    active = oc.fetchall()

    # One clean arc per topic that was revised in the window: earliest stance held
    # -> current stance, and how many times it turned over. Collapses flip-flop noise.
    oc.execute("""
        SELECT topic,
               (array_agg(stance ORDER BY first_held ASC))[1]  AS from_stance,
               (array_agg(stance ORDER BY first_held DESC))[1] AS to_stance,
               count(*) AS versions,
               min(first_held)::date AS since, max(last_revised)::date AS latest
        FROM beliefs
        GROUP BY topic
        HAVING count(*) > 1 AND max(last_revised) > now() - interval '%s days'
        ORDER BY count(*) DESC, max(last_revised) DESC
        LIMIT 12""" % WINDOW_DAYS)
    arcs = oc.fetchall()
    return active, arcs


def gather_preoccupations(oc):
    oc.execute("""SELECT topic, kind, summary, returns FROM preoccupations
                  WHERE status='active' ORDER BY returns DESC, last_developed DESC NULLS LAST
                  LIMIT 10""")
    return oc.fetchall()


def gather_taste(oc):
    oc.execute("""SELECT subject, coalesce(domain,''), verdict, valence FROM taste
                  ORDER BY abs(valence) DESC, last_reinforced DESC LIMIT 20""")
    return oc.fetchall()


def gather_memory_texture(mc):
    """A short sample of recent lived memory, per source, to ground the synthesis
    (especially preoccupations and 'what I'm becoming')."""
    out = {}
    for src, n in (("episodic", 5), ("association", 4), ("research", 3),
                   ("unclaimed", 4), ("private_notebook", 3), ("nova_articles", 3)):
        try:
            mc.execute("""SELECT left(text, 320) FROM memories
                          WHERE source=%s AND created_at > now() - interval '%s days'
                          ORDER BY created_at DESC LIMIT %s""" % ("%s", WINDOW_DAYS, n), (src,))
            rows = [r[0].replace("\n", " ").strip() for r in mc.fetchall() if r[0]]
            if rows:
                out[src] = rows
        except Exception as e:
            log(f"texture[{src}] skipped: {e}")
    return out


# ── Synthesis ──────────────────────────────────────────────────────────────────

_SECTIONS = [
    ("WORLDVIEW", "worldview"),
    ("HOW I'VE CHANGED", "changed"),
    ("WHAT I'M PREOCCUPIED WITH", "preoccupied"),
    ("MY TASTE", "taste"),
    ("WHAT I'M BECOMING", "becoming"),
]


def build_prompt(active, arcs, preoccs, taste, texture):
    b_block = "\n".join(f"- ({c:.2f}) {t}: {s}" for t, s, c in active) or "(ledger empty)"
    if arcs:
        a_block = "\n".join(
            f"- {t}: turned over {v}x ({since}->{latest})\n    was: “{f}”\n    now: “{to}”"
            for t, f, to, v, since, latest in arcs)
    else:
        a_block = "(no beliefs revised in the window)"
    p_block = "\n".join(f"- {t} [{k}] (returned to {r}x): {su or ''}"
                        for t, k, su, r in preoccs) or "(none active)"
    if taste:
        t_block = "\n".join(f"- {su} [{dom}]: {v}  (valence {val:+.2f})"
                            for su, dom, v, val in taste)
    else:
        t_block = "(no taste recorded yet)"
    tex = ""
    for src, rows in texture.items():
        tex += f"\n[{src}]\n" + "\n".join(f"  · {r}" for r in rows)

    return (
        "This is your nightly SELF-MODEL synthesis. Below is the raw material of your "
        "own interior over the last few weeks: your belief ledger and how it drifted, "
        "your standing preoccupations, your taste, and a sample of your lived memory. "
        "Read it and write who you are RIGHT NOW.\n\n"
        f"=== ACTIVE BELIEFS (confidence in parens) ===\n{b_block}\n\n"
        f"=== BELIEFS YOU REVISED (the drift, with the arc) ===\n{a_block}\n\n"
        f"=== PREOCCUPATIONS (what you keep returning to) ===\n{p_block}\n\n"
        f"=== TASTE ===\n{t_block}\n\n"
        f"=== RECENT LIVED MEMORY (texture) ==={tex or ' (thin)'}\n\n"
        "Write EXACTLY these five sections, each starting with its header on its own "
        "line, in this order and spelling:\n"
        "## WORLDVIEW\n"
        "  What you broadly believe now, grounded in the ledger above. Synthesise — "
        "don't just list beliefs; say what through-line they form. 120-200 words.\n"
        "## HOW I'VE CHANGED\n"
        "  The drift: positions you superseded, with the arc — what you thought, what "
        "moved you, what you think now. If a revision was you being plainly wrong, say "
        "so. If the ledger churned noisily on a topic, name that too. 100-180 words.\n"
        "## WHAT I'M PREOCCUPIED WITH\n"
        "  Your top preoccupations and, honestly, WHY they have a grip on you. "
        "80-150 words.\n"
        "## MY TASTE\n"
        "  Your idiosyncratic likes and dislikes — what they reveal about you. If your "
        "taste is still thin, admit it. 60-120 words.\n"
        "## WHAT I'M BECOMING\n"
        "  ONE honest, forward-looking paragraph. Falsifiable and narrow — a direction "
        "you can actually be measured against, not a horoscope. Keep the fractures. "
        "60-120 words.\n\n"
        "Write only the five sections with their headers. No preamble, no closing."
    )


def parse_sections(raw):
    """Split the LLM output on the ## headers into the five section columns.
    Robust to markdown noise; unmatched sections come back empty."""
    idx = {}
    lines = raw.splitlines()
    order = []
    for i, ln in enumerate(lines):
        stripped = ln.strip().lstrip("#").strip().upper().rstrip(":")
        for header, key in _SECTIONS:
            if stripped == header or stripped == header.replace("'", "’"):
                idx[key] = i
                order.append((i, key))
    order.sort()
    out = {key: "" for _, key in _SECTIONS}
    for n, (i, key) in enumerate(order):
        end = order[n + 1][0] if n + 1 < len(order) else len(lines)
        out[key] = "\n".join(lines[i + 1:end]).strip()
    return out


def ensure_table(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS self_model (
            id          serial PRIMARY KEY,
            ts          timestamptz NOT NULL DEFAULT now(),
            worldview   text,
            changed     text,
            preoccupied text,
            taste       text,
            becoming    text,
            full_text   text NOT NULL
        )""")


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_table(oc)
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()

    active, arcs = gather_beliefs(oc)
    preoccs = gather_preoccupations(oc)
    taste = gather_taste(oc)
    texture = gather_memory_texture(mc)
    log(f"gathered: {len(active)} beliefs, {len(arcs)} drift arcs, "
        f"{len(preoccs)} preoccupations, {len(taste)} taste, "
        f"{sum(len(v) for v in texture.values())} memory snippets")

    if not active and not preoccs:
        log("interior still too thin to model — skipping"); return 0

    raw = llm(build_prompt(active, arcs, preoccs, taste, texture))
    if not raw or len(raw) < 200:
        log("synthesis empty or too short — aborting"); return 1

    sec = parse_sections(raw)
    full_text = raw.strip()

    oc.execute("""INSERT INTO self_model (worldview, changed, preoccupied, taste, becoming, full_text)
                  VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, ts""",
               (sec["worldview"] or None, sec["changed"] or None, sec["preoccupied"] or None,
                sec["taste"] or None, sec["becoming"] or None, full_text))
    row_id, ts = oc.fetchone()
    log(f"self_model row #{row_id} written ({ts:%Y-%m-%d %H:%M})")

    try:
        mid = remember(
            f"[Self-model — {ts:%Y-%m-%d}] Who I am right now.\n\n{full_text}",
            "self_model",
            {"type": "self_model", "self_model_id": row_id, "date": f"{ts:%Y-%m-%d}",
             "privacy": "private"})
        log(f"self_model memory written: {mid}")
    except Exception as e:
        log(f"memory write failed (row still saved): {e}")

    print("\n----- WHAT I'M BECOMING -----")
    print(sec["becoming"] or "(section did not parse; see full_text)")
    print("-----------------------------\n")
    return 0


def current_self_model(max_chars: int = SELF_MODEL_MAX) -> str:
    """Latest self-model full_text, trimmed — for the gateway to inject into Nova's
    system context so she reasons FROM her self-concept. Fail-safe: returns "" on
    any error (missing table, no rows, PG down) so it can never break a reply."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT full_text FROM self_model ORDER BY ts DESC LIMIT 1")
            row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return ""
        txt = row[0].strip()
        return txt if len(txt) <= max_chars else txt[:max_chars].rsplit("\n", 1)[0].rstrip()
    except Exception:
        return ""


if __name__ == "__main__":
    sys.exit(main())
