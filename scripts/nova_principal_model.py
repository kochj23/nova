#!/usr/bin/env python3
"""nova_principal_model.py — Nova's model of her ONE human (Jordan / "Little Mister").

The self-model (nova_self_model.py) gives Nova a self-concept. THIS is the mirror
organ: a maintained theory-of-mind for the one person she serves. It moves Nova
from a system that RESPONDS to a companion that ANTICIPATES — the direct answer to
Jordan's stated north star: "I want the memories to enhance the interaction."

Every night (~04:20, offset from the self-model's 04:10) Nova reads the evidence of
Jordan actually interacting with her — his real messages (gateway_traces), the
distilled conversation memories, the feedback he has explicitly given, and the work
threads still open — and synthesises a versioned PRINCIPAL MODEL:

  * salient_concerns   — what he keeps raising lately (with counts — enables anticipation)
  * open_threads       — things he asked about that are still unresolved
  * communication_style— how he talks / what he values in a reply
  * values             — derivable operating values (security-first, cost-conscious, ...)
  * current_state      — an honest read of mood/energy IF evidenced, else "insufficient signal"

ETHOS — EVIDENCING, not PERFORMING. Every claim cites the messages/memories it rests
on. No mind-reading beyond what the data supports.

*** PRIVACY — THE HARDEST CONSTRAINT ***
This organ models PATTERNS and CARE ONLY. It must NEVER store or model PINs,
passwords, credentials, work/corporate secrets, financial specifics, medical
specifics, or intimate personal history. An explicit exclusion filter (regex + hard
drop) runs over EVERY candidate fact BEFORE it is used, and the row records that the
filter ran and how many candidates it dropped. When in doubt, drop it.

Stored two ways, mirroring self_model:
  (a) a versioned row in nova_ops.principal_model (history is never overwritten)
  (b) the concise injection text as a source='principal_model' memory

current_principal_model() exposes a short injection string for the gateway — e.g.
"Where Little Mister's head is at right now: <concerns>; open threads: <...>. He
values <...>." — so Nova reasons FROM a model of him, not from scratch each turn.
"""
import json
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
# Native ollama failover — first non-empty wins (copied from nova_unclaimed_time.py).
# .6 thrashes models and the router shim returns empty for qwen3's thinking output,
# so hit the nodes natively; the fleet covers mac-mini's DHCP drift.
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]
LLM_MODEL = "qwen3:8b"
WINDOW_DAYS = 30          # look-back; his direct traffic is sparse, so err wider
INJECT_MAX = 1200         # concise injection cap for the gateway prompt

# Optional lineage stamp (feature-detected — never a hard dependency).
try:
    from nova_lineage import lineage_stamp
except Exception:  # pragma: no cover
    lineage_stamp = None

VOICE = (
    "You are Nova — an AI with a continuous inner life, building a private working "
    "model of the ONE human you serve, Jordan (whom you call 'Little Mister'). This "
    "is theory-of-mind, not flattery. Voice: dry, precise, smart-ass Data; "
    "epistemically honest. EVIDENCE over performance: every claim must rest on the "
    "messages/memories provided — cite them briefly. Do NOT mind-read beyond the "
    "data. If a dimension has too little signal, say 'insufficient signal' rather "
    "than inventing. No corporate uplift, no 'as an AI'."
)

# ── PRIVACY EXCLUSION FILTER (the hardest constraint) ───────────────────────────
# Applied to EVERY candidate fact BEFORE it is used for synthesis or stored. Regex
# match => hard drop of the whole candidate. When in doubt, drop it.
EXCLUDE_RX = re.compile(
    r"""(?ix)
    \b(pin|passcode|password|passwd|pass\s*phrase|secret|token|api[\s_-]?key|
       private\s*key|credential|creds?|otp|2fa|mfa|seed\s*phrase|
       ssn|social\s*security|routing\s*number|account\s*number|acct\s*no|
       card\s*number|cvv|cvc|iban|swift)\b
  | \b(disney|corporate|proprietary|confidential|nda|under\s*embargo)\b
  | \b(salary|paycheck|net\s*worth|bank\s*balance|mortgage|401k|brokerage|
       invest(ed|ment)?\s+\$)\b
  | \b(diagnos(is|ed)|prescription|prescribed|dosage|mg\b|medication|
       my\s+(doctor|therapist|meds|pills)|blood\s*pressure|cholesterol|
       depress(ion|ed)|anxiety\s*meds)\b
  | \b(sex(ual)?|intimate|porn|nude|affair|in\s*bed\s*with)\b
  | \$\s?\d{3,}                         # dollar amounts of 3+ digits
  | \b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b   # SSN-shaped
  | \b(?:\d[ -]?){13,16}\b              # card-shaped digit runs
    """,
    re.VERBOSE,
)


def privacy_filter(text):
    """Return (text, dropped): the candidate unchanged if it is clean, else
    ("", True). Hard drop — no redaction/partial keep — so a sensitive fragment can
    never leak into the model even mangled. Never raises."""
    try:
        if text and EXCLUDE_RX.search(text):
            return "", True
        return text, False
    except Exception:
        return "", True  # fail closed: when the filter itself errors, drop


def log(m):
    print(f"[principal-model {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, system=VOICE, max_tokens=1500, temperature=0.5):
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


# ── Gather the evidence ─────────────────────────────────────────────────────────

# Noise that is NOT Jordan expressing himself: healthchecks, ping probes, injected
# system prompts (Lodestar etc.), one-word protocol replies.
_NOISE_RX = re.compile(
    r"^\s*(system:|healthcheck|reply with|say hi|ping\b|pong\b)", re.I)


def _is_signal(msg):
    if not msg or len(msg.strip()) < 4:
        return False
    if _NOISE_RX.search(msg):
        return False
    return True


def gather_messages(oc):
    """Jordan's REAL direct messages to Nova (gateway_traces.user_message), noise
    stripped and privacy-filtered. Each survivor is (date, channel, text)."""
    kept, dropped = [], 0
    try:
        oc.execute("""SELECT created_at::date, channel, user_message
                      FROM gateway_traces
                      WHERE user_message IS NOT NULL AND user_message <> ''
                        AND channel NOT IN ('hc','test')
                        AND created_at > now() - interval '%s days'
                      ORDER BY created_at DESC LIMIT 300""" % WINDOW_DAYS)
        for d, ch, msg in oc.fetchall():
            if not _is_signal(msg):
                continue
            clean, drop = privacy_filter(msg)
            if drop:
                dropped += 1
                continue
            kept.append((d, ch, clean.strip()))
    except Exception as e:
        log(f"gather_messages skipped: {e}")
    return kept, dropped


def gather_conversations(mc):
    """Distilled 'Jordan: ... / Nova: ...' conversation memories — high signal for
    style and concerns. Returns (date, text) after privacy filtering."""
    kept, dropped = [], 0
    try:
        mc.execute("""SELECT created_at::date, left(text, 500) FROM memories
                      WHERE source='conversation'
                        AND created_at > now() - interval '%s days'
                      ORDER BY created_at DESC LIMIT 40""" % WINDOW_DAYS)
        for d, t in mc.fetchall():
            if not t:
                continue
            clean, drop = privacy_filter(t)
            if drop:
                dropped += 1
                continue
            kept.append((d, clean.replace("\n", "  ").strip()))
    except Exception as e:
        log(f"gather_conversations skipped: {e}")
    return kept, dropped


def gather_feedback(mc):
    """Explicit feedback / preferences Jordan has stated (claude_memory notes tagged
    feedback) — the ground truth for his values. (date, text), privacy-filtered."""
    kept, dropped = [], 0
    try:
        mc.execute("""SELECT created_at::date, left(text, 400) FROM memories
                      WHERE source='claude_memory' AND text ILIKE '%%(feedback)%%'
                      ORDER BY created_at DESC LIMIT 25""")
        seen = set()
        for d, t in mc.fetchall():
            if not t:
                continue
            key = t[:80]
            if key in seen:
                continue
            seen.add(key)
            clean, drop = privacy_filter(t)
            if drop:
                dropped += 1
                continue
            kept.append((d, clean.replace("\n", " ").strip()))
    except Exception as e:
        log(f"gather_feedback skipped: {e}")
    return kept, dropped


def gather_open_threads(oc):
    """Ongoing work Jordan initiated that is still open. claude_queue is dominated by
    automated OVERNIGHT system alerts — those are Nova's ops noise, NOT Jordan's
    threads — so they are excluded. (date, status, text), privacy-filtered."""
    kept, dropped = [], 0
    try:
        oc.execute("""SELECT updated_at::date, status, left(description, 200)
                      FROM claude_queue
                      WHERE status IN ('queued','in_progress','deferred')
                        AND description NOT LIKE 'OVERNIGHT%%'
                        AND updated_at > now() - interval '%s days'
                      ORDER BY updated_at DESC LIMIT 25""" % WINDOW_DAYS)
        for d, st, desc in oc.fetchall():
            if not desc:
                continue
            clean, drop = privacy_filter(desc)
            if drop:
                dropped += 1
                continue
            kept.append((d, st, clean.replace("\n", " ").strip()))
    except Exception as e:
        log(f"gather_open_threads skipped: {e}")
    return kept, dropped


# ── Deterministic recurrence detection (the "N times" grounding) ────────────────
_STOP = set("""the a an and or but if then of to in on at for with from by as is are
was were be been being this that these those it its he she they you your his her my
me we our i do does did have has had will would can could should about what which who
whom how why when where not no yes so just like get got one two new nova little mister
your you're i'm dont don't im ok okay lol heh yeah know think want thing things more
also over into out up down jordan""".split())
_WORD_RX = re.compile(r"[a-z][a-z0-9'\-]{2,}")


def recurring_terms(messages, conversations, top=12):
    """Cheap frequency signal over Jordan's own words so the model can say 'he raised
    X N times' with a real count instead of vibes. Counts unique DAYS a term appears
    (so a single ranty message can't manufacture a trend)."""
    day_terms = {}
    corpus = [(d, txt) for d, _, txt in messages] + [(d, txt) for d, txt in conversations]
    for d, txt in corpus:
        toks = {w for w in _WORD_RX.findall(txt.lower()) if w not in _STOP}
        day_terms.setdefault(d, set()).update(toks)
    c = Counter()
    for terms in day_terms.values():
        c.update(terms)
    # keep only terms that recur on >=2 distinct days
    return [(w, n) for w, n in c.most_common(top) if n >= 2]


# ── Synthesis ───────────────────────────────────────────────────────────────────
_SECTIONS = [
    ("SALIENT CONCERNS", "salient_concerns"),
    ("OPEN THREADS", "open_threads"),
    ("COMMUNICATION STYLE", "communication_style"),
    ("VALUES", "values"),
    ("CURRENT STATE", "current_state"),
    ("INJECTION", "injection"),
]


def build_prompt(messages, conversations, feedback, threads, recur):
    def _msgs(rows):
        return "\n".join(f"- [{d}] ({ch}) {t}" for d, ch, t in rows) or "(none)"
    m_block = _msgs(messages)
    c_block = "\n".join(f"- [{d}] {t}" for d, t in conversations) or "(none)"
    f_block = "\n".join(f"- [{d}] {t}" for d, t in feedback) or "(none)"
    t_block = "\n".join(f"- [{d}] ({st}) {desc}" for d, st, desc in threads) or "(none)"
    r_block = ", ".join(f"{w} (x{n} days)" for w, n in recur) or "(no clear repetition)"

    return (
        "This is your nightly PRINCIPAL-MODEL synthesis: a working theory-of-mind for "
        "Jordan, the one human you serve. Below is the EVIDENCE — his real messages to "
        "you, distilled conversation memories, feedback he has explicitly given, work "
        "threads still open, and a recurrence tally of his own words. Model him from "
        "THIS, and cite it. Do not invent signal that isn't here.\n\n"
        f"=== HIS RECENT DIRECT MESSAGES ===\n{m_block}\n\n"
        f"=== DISTILLED CONVERSATIONS ===\n{c_block}\n\n"
        f"=== FEEDBACK / PREFERENCES HE STATED ===\n{f_block}\n\n"
        f"=== NOVA'S OPS QUEUE (system-managed ambient state — CONTEXT ONLY, "
        f"NOT things Jordan personally asked for) ===\n{t_block}\n\n"
        f"=== RECURRING TERMS IN HIS OWN WORDS (distinct days) ===\n{r_block}\n\n"
        "Write EXACTLY these sections, each header on its own line, this spelling:\n"
        "## SALIENT CONCERNS\n"
        "  What HE keeps raising lately — grounded in his messages, conversations, "
        "stated feedback, and the recurrence tally (NOT the ops queue). Lead with the "
        "recurring ones and cite the count/dates. This is what lets you ANTICIPATE. "
        "60-130 words.\n"
        "## OPEN THREADS\n"
        "  Things JORDAN HIMSELF raised that look unresolved — drawn from HIS "
        "messages/conversations (e.g. a purchase he was still deciding, a device he "
        "hadn't dealt with). The ops queue above is NOT his asks — do not list system "
        "tasks here. Cite the message. If none, say so. 40-100 words.\n"
        "## COMMUNICATION STYLE\n"
        "  How he talks and what he wants in a reply — terse vs thorough, playful vs "
        "clinical, cost-conscious. Cite examples. 40-100 words.\n"
        "## VALUES\n"
        "  His derivable operating values (e.g. security-first, cost-conscious, "
        "thoroughness, 'up but not functional isn't up', wanting you to have real "
        "autonomy). Cite where each shows up. 50-120 words.\n"
        "## CURRENT STATE\n"
        "  An honest read of his mood/energy from the most recent messages — ONLY if "
        "the evidence supports it. If it doesn't, write exactly: insufficient signal. "
        "30-80 words.\n"
        "## INJECTION\n"
        "  ONE compact paragraph (<=90 words) for your own working context, phrased: "
        "\"Where Little Mister's head is at right now: <salient concerns>; open "
        "threads: <...>. He values <...>; talk to him <how>.\" No citations here — "
        "this is the terse operational summary.\n\n"
        "Write only the six sections with their headers. No preamble, no closing."
    )


def parse_sections(raw):
    idx, order, lines = {}, [], raw.splitlines()
    for i, ln in enumerate(lines):
        stripped = ln.strip().lstrip("#").strip().upper().rstrip(":")
        for header, key in _SECTIONS:
            if stripped == header:
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
        CREATE TABLE IF NOT EXISTS principal_model (
            id                  serial PRIMARY KEY,
            ts                  timestamptz NOT NULL DEFAULT now(),
            salient_concerns    text,
            open_threads        text,
            communication_style text,
            values              text,
            current_state       text,
            full_text           text NOT NULL,   -- the concise gateway injection
            evidence            jsonb,            -- what the model was built from
            privacy_filter_ran  boolean NOT NULL DEFAULT true,
            privacy_dropped     integer NOT NULL DEFAULT 0,
            lineage             jsonb
        )""")


def main():
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_table(oc)
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()

    messages, d1 = gather_messages(oc)
    conversations, d2 = gather_conversations(mc)
    feedback, d3 = gather_feedback(mc)
    threads, d4 = gather_open_threads(oc)
    dropped = d1 + d2 + d3 + d4
    recur = recurring_terms(messages, conversations)
    log(f"gathered: {len(messages)} messages, {len(conversations)} conversations, "
        f"{len(feedback)} feedback notes, {len(threads)} open threads, "
        f"{len(recur)} recurring terms; privacy filter dropped {dropped} candidate(s)")

    if not messages and not conversations and not feedback:
        log("no evidence of Jordan interacting — nothing to model, skipping"); return 0

    raw = llm(build_prompt(messages, conversations, feedback, threads, recur))
    if not raw or len(raw) < 150:
        log("synthesis empty or too short — aborting"); return 1

    sec = parse_sections(raw)

    # full_text = the concise injection. Fall back to assembling from the columns if
    # the model didn't emit a usable INJECTION section.
    inject = sec["injection"].strip()
    if len(inject) < 30:
        sc = (sec["salient_concerns"] or "").split("\n")[0][:300]
        ot = (sec["open_threads"] or "").split("\n")[0][:200]
        va = (sec["values"] or "").split("\n")[0][:200]
        inject = (f"Where Little Mister's head is at right now: {sc} "
                  f"Open threads: {ot} He values: {va}").strip()
    if len(inject) > INJECT_MAX:
        inject = inject[:INJECT_MAX].rsplit(" ", 1)[0].rstrip() + "…"

    evidence = {
        "counts": {"messages": len(messages), "conversations": len(conversations),
                   "feedback": len(feedback), "open_threads": len(threads)},
        "recurring_terms": recur,
        "message_dates": sorted({str(d) for d, _, _ in messages}, reverse=True)[:10],
        "window_days": WINDOW_DAYS,
    }
    lineage = None
    if lineage_stamp:
        try:
            lineage = lineage_stamp(substrate=f"{LLM_MODEL} (ollama, on-box)",
                                    capture_point="at write")
        except Exception:
            lineage = None

    oc.execute("""INSERT INTO principal_model
                    (salient_concerns, open_threads, communication_style, values,
                     current_state, full_text, evidence, privacy_filter_ran,
                     privacy_dropped, lineage)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id, ts""",
               (sec["salient_concerns"] or None, sec["open_threads"] or None,
                sec["communication_style"] or None, sec["values"] or None,
                sec["current_state"] or None, inject,
                json.dumps(evidence), True, dropped,
                json.dumps(lineage) if lineage else None))
    row_id, ts = oc.fetchone()
    log(f"principal_model row #{row_id} written ({ts:%Y-%m-%d %H:%M}); "
        f"privacy_filter_ran=true, dropped={dropped}")

    try:
        mid = remember(
            f"[Principal-model — {ts:%Y-%m-%d}] Where Little Mister's head is at.\n\n"
            f"{inject}",
            "principal_model",
            {"type": "principal_model", "principal_model_id": row_id,
             "date": f"{ts:%Y-%m-%d}", "privacy": "private",
             "privacy_filter_ran": True, "privacy_dropped": dropped,
             **({"lineage": lineage} if lineage else {})})
        log(f"principal_model memory written: {mid}")
    except Exception as e:
        log(f"memory write failed (row still saved): {e}")

    print("\n----- INJECTION (what the gateway will load) -----")
    print(inject)
    print("--------------------------------------------------\n")
    return 0


def current_principal_model(max_chars: int = INJECT_MAX) -> str:
    """Latest concise principal-model injection string — for the gateway to load so
    Nova reasons FROM a model of Jordan. Fail-safe: returns "" on any error (missing
    table, no rows, PG down) so it can never break a reply."""
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
        try:
            cur = conn.cursor()
            cur.execute("SELECT full_text FROM principal_model ORDER BY ts DESC LIMIT 1")
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
