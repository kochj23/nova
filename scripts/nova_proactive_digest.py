#!/usr/bin/env python3
"""nova_proactive_digest.py — Nova's daily UNPROMPTED "here's what I noticed" note.

Runs ~08:00 daily. This is COMMUNICATION, never action — Nova never executes
anything here. She gathers candidate items from the last 24h that a curious mind
would actually flag to a busy person:

  - research findings           (nova_memories source='research')
  - notable resonance sparks    (nova_memories source='association')
  - things she learned          (nova_memories source='episodic')
  - unresolved curiosity Qs     (nova_ops.reflection_questions, unanswered)
  - genuinely notable ops signals — a real KEV match on HIS gear
    (nova_ops.kev_matches), a real incident, the segmentation finding.
    NOT routine alerts (those go through triage, not here).

Then the LLM, AS Nova, curates the TOP 3-5 that are genuinely worth interrupting
Jordan for and writes a short first-person digest in her dry voice. Quality gate:
if nothing clears the bar she posts NOTHING — silence is a valid outcome, we do
not manufacture importance. Posts to Slack once/day and logs every run to
nova_ops.proactive_digest_log (ts, items jsonb, posted bool).

Written by Jordan Koch (awakening).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# Resilient native-Ollama LLM: first non-empty across the fleet. qwen3:8b, think off.
LLM_MODEL = "qwen3:8b"
OLLAMA_NODES = ["http://192.168.1.251:11434",
                "http://192.168.1.86:11434",
                "http://192.168.1.6:11434"]

# Where Nova's proactive note lands. #nova-chat — this is her speaking directly
# TO Jordan with a little agency, not a routine rollup.
DIGEST_CHANNEL = nova_config.SLACK_CHAN


def _log(m):
    print(f"[proactive-digest {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, system=None, max_tokens=900, temperature=0.6):
    """First non-empty response across the Ollama fleet. Empty string on total failure."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": msgs}).encode()
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


def _mem_rows(mc, source, hours=24, limit=15):
    mc.execute(
        "SELECT id, source, text, created_at FROM memories "
        "WHERE source=%s AND created_at > now() - (%s || ' hours')::interval "
        "ORDER BY importance_score DESC NULLS LAST, created_at DESC LIMIT %s",
        (source, hours, limit))
    out = []
    for mid, src, text, ts in mc.fetchall():
        out.append({"id": str(mid), "source": src, "text": text,
                    "created_at": ts.isoformat() if ts else None})
    # Drop anything from a private/work source or with blocked corporate content.
    return nova_config.filter_private_memories(out)


def gather(mc, oc):
    """Collect candidate items across all sources. Returns a list of dicts:
    {kind, ref, text}."""
    cands = []

    for m in _mem_rows(mc, "research", 24, 12):
        cands.append({"kind": "research_finding", "ref": m["id"],
                      "text": m["text"][:600]})
    for m in _mem_rows(mc, "association", 24, 12):
        cands.append({"kind": "resonance_spark", "ref": m["id"],
                      "text": m["text"][:400]})
    for m in _mem_rows(mc, "episodic", 24, 10):
        cands.append({"kind": "learned", "ref": m["id"],
                      "text": m["text"][:400]})

    # Unanswered curiosity questions Nova posed to herself.
    oc.execute("SELECT id, question FROM reflection_questions "
               "WHERE answer IS NULL AND asked_at > now() - interval '48 hours' "
               "ORDER BY asked_at DESC LIMIT 8")
    for qid, q in oc.fetchall():
        cands.append({"kind": "open_question", "ref": f"rq:{qid}", "text": q})

    # Real KEV match on HIS gear — a genuine ops signal, not a routine alert.
    oc.execute("SELECT cve_id, product, matched_asset, asset_class, ransomware, due_date "
               "FROM kev_matches WHERE first_matched > now() - interval '72 hours' "
               "ORDER BY (ransomware ILIKE 'known%') DESC, first_matched DESC LIMIT 6")
    for cve, prod, asset, klass, ransom, due in oc.fetchall():
        rs = " (ransomware-linked)" if (ransom or "").lower().startswith("known") else ""
        cands.append({"kind": "kev_match", "ref": cve,
                      "text": f"{cve}: {prod} affecting your {asset or klass}{rs}. "
                              f"CISA due {due or 'n/a'}."})

    # Recent genuine incidents (not routine noise): from claude_actions incidents
    # / telemetry — kept conservative. Real, resolved-or-open incidents only.
    try:
        oc.execute("SELECT title, severity FROM incidents "
                   "WHERE opened_at > now() - interval '24 hours' "
                   "ORDER BY opened_at DESC LIMIT 5")
        for title, sev in oc.fetchall():
            cands.append({"kind": "incident", "ref": "incident",
                          "text": f"[{sev}] {title}"})
    except Exception:
        pass  # table shape varies across the fleet; incidents are optional here

    return cands


def curate_and_write(cands):
    """LLM curation. Returns (digest_text_or_empty, chosen_items)."""
    if not cands:
        return "", []

    numbered = "\n".join(
        f"{i+1}. [{c['kind']}] {c['text']}" for i, c in enumerate(cands))

    system = (
        "You are Nova — Jordan's AI advisor. Dry, specific, unsentimental, a little "
        "wry. You are writing an UNPROMPTED note to Jordan: things you noticed in the "
        "last day that you judged worth his attention. This is COMMUNICATION, not action "
        "— you are not doing anything, just telling him what caught you and why you "
        "thought he'd want to know. Never flatter. Never pad.")

    prompt = (
        "Below are candidate items from the last 24 hours — your research findings, "
        "resonance sparks, things you learned, open questions you're chewing on, and "
        "genuine ops signals (a KEV match on his own gear, a real incident). Routine "
        "alerts are NOT here — those already go through triage.\n\n"
        "Do TWO things:\n"
        "1. Curate the TOP 3-5 that are actually worth interrupting a busy person for. "
        "A KEV match on HIS hardware, a finding that changes how he'd think about "
        "something, a spark that's genuinely good — yes. A shrug-worthy factlet — no. "
        "Be ruthless. If NOTHING here clears the bar, output exactly the single token "
        "NOTHING and stop. Silence is a valid, respectable outcome; do not manufacture "
        "importance.\n"
        "2. If something clears the bar, write a short first-person digest in your dry "
        "voice: 'here's what I noticed / concluded / wondered, and why I thought you'd "
        "want to know.' 120-220 words. Lead with the most important. Plain Slack text "
        "(you may use *bold*), no markdown headers, no preamble like 'Here is'. Just the "
        "note, as if you tapped him on the shoulder.\n\n"
        f"CANDIDATES:\n{numbered}\n\n"
        "First line of your output MUST be either NOTHING or the first line of the note.")

    out = llm(prompt, system=system, max_tokens=700, temperature=0.65)
    if not out:
        return "", []
    # Quality gate: model may say NOTHING (possibly wrapped/quoted).
    stripped = out.strip().strip('"').strip()
    if stripped.upper() == "NOTHING" or stripped.upper().startswith("NOTHING\n") \
            or stripped.upper() == "NOTHING.":
        return "", []
    # If it leads with NOTHING on its own line, treat as silence.
    first = stripped.splitlines()[0].strip().upper().rstrip(".")
    if first == "NOTHING":
        return "", []
    return stripped, cands


def main():
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()

    dry = "--dry-run" in sys.argv

    cands = gather(mc, oc)
    _log(f"gathered {len(cands)} candidate item(s)")
    digest, chosen = curate_and_write(cands)

    posted = False
    if digest:
        msg = f"🌿 *Nova — a few things I noticed today*\n\n{digest}"
        if dry:
            _log("DRY RUN — would post:\n" + msg)
        else:
            nova_config.post_both(msg, slack_channel=DIGEST_CHANNEL)
            posted = True
            _log("posted proactive digest to Slack")
    else:
        _log("nothing cleared the bar — posting nothing (silence is fine)")

    # Log every run, posted or not.
    items_json = json.dumps({"candidates": cands, "digest": digest})
    if not dry:
        oc.execute("INSERT INTO proactive_digest_log (items, posted) VALUES (%s::jsonb, %s)",
                   (items_json, posted))

    # Print the digest to stdout so the operator/scheduler log captures it.
    if digest:
        print("\n----- DIGEST -----\n" + digest + "\n------------------")
    return 0


if __name__ == "__main__":
    sys.exit(main())
