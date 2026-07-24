#!/usr/bin/env python3
"""nova_code_reference.py — verified code/term lookup for LAPD/LAFD/aviation (KBUR) articles.

When Nova writes about the scanners or KBUR traffic she must translate radio/penal/aviation codes
from AUTHORITATIVE reference vectors, not guess. This queries those vectors (police_codes, fire_ops,
aviation_ref) semantically with the article's raw data and returns a prompt-injectable block of
verified definitions, plus a hard instruction never to invent a code. Safe to call always: returns
"" if the vectors are empty (e.g. still ingesting), so it degrades gracefully.
"""
import json
import urllib.parse
import urllib.request

RECALL = "http://memory-server.digitalnoise.net:18790/recall"
DOMAIN_VECTOR = {"police": "police_codes", "fire": "fire_ops", "aviation": "aviation_ref"}


def _recall(query, vector, n):
    try:
        url = f"{RECALL}?q={urllib.parse.quote(query[:400])}&n={n}&source={vector}"
        data = json.loads(urllib.request.urlopen(url, timeout=15).read())
        return [(m.get("text") or "").strip() for m in data.get("memories", []) if m.get("text")]
    except Exception:
        return []


def code_reference_block(query_text, domains, n=10):
    """Prompt-injectable block of VERIFIED code/term definitions relevant to query_text, pulled
    from the reference vectors for the given domains (any of 'police','fire','aviation').
    Returns "" if nothing found — so callers can always append it unconditionally."""
    seen, defs = set(), []
    for d in domains:
        vec = DOMAIN_VECTOR.get(d)
        if not vec:
            continue
        for t in _recall(query_text, vec, n):
            key = t[:80].lower()
            if key in seen:
                continue
            seen.add(key)
            defs.append(t[:300])
    if not defs:
        return ""
    body = "\n".join(f"- {d}" for d in defs[:24])
    return (
        "\n\n[VERIFIED CODE/TERM REFERENCE — authoritative definitions retrieved from Nova's "
        "reference vectors. When the article mentions any police/fire/aviation code, penal-code "
        "section, 10-code, alarm level, or ATC/pattern term, use THESE meanings. NEVER invent or "
        "guess a code's meaning; if a code isn't covered here, describe the event plainly without "
        "asserting a specific code definition.]\n" + body + "\n"
    )


if __name__ == "__main__":  # smoke test
    print(code_reference_block("211 robbery suspect code 3 foot pursuit 415 disturbance",
                               ["police", "fire"]) or "(no reference data in the vectors yet)")
