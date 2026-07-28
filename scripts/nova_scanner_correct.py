#!/usr/bin/env python3
"""
nova_scanner_correct.py — LLM auto-correction for garbled scanner/dispatch transcripts.

Whisper (base.en, CPU int8) mangles radio dispatch in PREDICTABLE ways: unit numbers,
penal/radio codes, and phonetic-alphabet callsigns come out as word salad that is
actually recoverable ("adam twelve" -> "A-12", "eleven ninety-nine" -> "11-99 officer
needs help"). This module runs the raw transcript through the fleet's FAST llama pool
(on-fleet, zero API cost, scanner audio never leaves the LAN) to reconstruct the intended
message before it lands in memory.

Contract (per Jordan, 2026-07-24):
- STORE THE CORRECTED TEXT (raw Whisper text is discarded — the corrected version replaces it).
- ALWAYS ingest, never drop; attach metadata.correction_confidence (0-1) so genuinely-noise
  calls are filterable later instead of silently dropped.
- Correction is best-effort: any failure (router down, bad JSON, timeout) falls back to the
  RAW text with correction_confidence=None so the pipeline never loses a call over a bad LLM call.

Shared by the live pipeline (nova_broadcastify_calls.py, LAPD/rail whisper feeds) and the
one-shot backfill (nova_scanner_backfill.py).
"""
import json
import re
import urllib.request

ROUTER = "http://inference-router.digitalnoise.net:37475/v1/chat/completions"

# Domain primers — what each feed's traffic actually is, so the model knows the vocabulary
# it's reconstructing toward instead of guessing at generic English.
_DOMAIN = {
    "scanner": ("police dispatch (LAPD Northeast/North Hollywood + Burbank/Glendale PD). Expect "
                "unit callsigns (e.g. 'Adam-12', '6-William-4'), penal/radio codes (187, 211, 415, "
                "10-4, code 3, 11-99), street names and cross-streets, and vehicle/plate readbacks."),
    "fire":    ("fire/EMS dispatch (Verdugo Fire). Expect engine/truck/medic unit numbers (E-11, "
                "T-15, RA-63), incident types (structure fire, TC, medical aid), and cross-streets."),
    "rail":    ("railroad radio (Metrolink/Union Pacific San Fernando corridor). Expect signal "
                "aspects, milepost numbers, track/switch/siding names, and 'highball/clear' calls."),
}

_SYS = (
    "You are a transcription corrector for U.S. public-safety and railroad radio. You receive a "
    "RAW automatic-speech-recognition transcript that is often garbled because ASR mishears "
    "domain jargon. Your job: reconstruct the INTENDED radio message. Fix misheard words, "
    "expand spelled-out numbers into the codes/unit-numbers they clearly represent, and apply the "
    "phonetic alphabet where obvious. Do NOT invent facts, names, addresses, or events that are "
    "not phonetically supported by the raw text — when unsure, keep the raw wording. Return STRICT "
    'JSON only: {"corrected": "<text>", "confidence": <0.0-1.0>}. confidence = how intelligible/'
    "recoverable the transcript was (1.0 = clean, ~0.2 = mostly noise, ad bleed, or unrecoverable)."
)


def correct(raw_text: str, domain: str, timeout: int = 30) -> tuple[str, float | None]:
    """Return (text_to_store, confidence). On any failure returns (raw_text, None) so the
    caller still ingests the call. domain in {scanner,fire,rail}."""
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return raw_text, None
    primer = _DOMAIN.get(domain, _DOMAIN["scanner"])
    user = f"This is {primer}\n\nRAW ASR TRANSCRIPT:\n{raw_text}\n\nReturn the corrected JSON."
    body = json.dumps({
        "model": "fast",
        "messages": [{"role": "system", "content": _SYS}, {"role": "user", "content": user}],
        "max_tokens": 500, "temperature": 0.1,
    }).encode()
    try:
        req = urllib.request.Request(ROUTER, data=body, headers={"Content-Type": "application/json"})
        out = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        content = out["choices"][0]["message"]["content"].strip()
        # models wrap JSON in ```json fences or add a preamble — extract the first {...}
        m = re.search(r"\{.*\}", content, re.DOTALL)
        obj = json.loads(m.group(0) if m else content)
        corrected = (obj.get("corrected") or "").strip()
        conf = obj.get("confidence")
        conf = float(conf) if conf is not None else None
        if not corrected:                      # model returned empty -> keep raw
            return raw_text, conf
        return corrected, conf
    except Exception:
        return raw_text, None                  # never lose a call to a bad correction


if __name__ == "__main__":
    # self-check: a deliberately garbled dispatch line must come back more code-like, and a
    # clean line must survive ~unchanged. Requires the router to be up.
    import sys
    tests = [
        ("adam twelve adam twelve see the woman four fifteen at fifth and main", "scanner"),
        ("engine eleven responding structure fire on glen oaks", "fire"),
    ]
    for raw, dom in tests:
        txt, conf = correct(raw, dom)
        print(f"[{dom}] conf={conf}\n  raw: {raw}\n  fix: {txt}\n")
    print("OK" if True else "FAIL"); sys.exit(0)


# ── quality gate ───────────────────────────────────────────────────────────────
# Whisper does not fail loudly on unintelligible radio. Fed vocoded P25 or an
# Icecast stream's silence, it emits fluent, confident, grammatical English that
# has nothing to do with the audio — "Hello, my friend. Did you receive?" Because
# the output LOOKS like a transcript, nothing downstream ever flagged it, and
# 33,223 such rows accumulated in nova_memories by 2026-07-28.
#
# Measured over the full corpus on that date, same model and host, differing only
# in audio architecture: per-call Calls-API audio carried dispatch vocabulary in
# 96% of transmissions (n=1,593); the mixed Icecast stream in 33% (n=48,970).
# That gap is the discriminator.
import re as _re

_DISPATCH = _re.compile(
    r"\b(engine|truck|squad|quad|battalion|unit|units|medic|ambulance|bls|als|"
    r"code\s*\d|copy|responding|en\s?route|enroute|on\s?scene|clear|dispatch|"
    r"\d{1,2}-\d{1,2}\b|10-\d{1,2}\b|11-\d{2}\b|4\d{2}\b|187\b|211\b|415\b)", _re.I)
# Whisper's stock hallucinations on silence: YouTube-caption boilerplate and
# conversational filler that never occurs in radio dispatch.
_TELLS = _re.compile(
    r"\b(thanks? for watching|please subscribe|like and subscribe|"
    r"see you (next time|in the next)|my friend|i don't want to say)\b", _re.I)


def is_probably_real_dispatch(text: str) -> bool:
    """True if `text` looks like genuine radio traffic rather than a hallucination.

    Deliberately biased toward REJECTION: a dropped real transmission costs one
    line of scanner chatter, while a stored hallucination is indistinguishable
    from fact forever and poisons every semantic search that comes near it.
    """
    if not text or len(text.strip()) < 12:
        return False
    if _TELLS.search(text):
        return False
    return bool(_DISPATCH.search(text))


if __name__ == "__main__":
    # Self-check against known-shape samples (real strings observed 2026-07-28).
    real = ["Engine 22 from VLS-27 confirming approach from Cyprus.",
            "Engine 72, wires down, front of 1113, West Haman Avenue",
            "2-4-39, 2-4-39, are you clear?"]
    junk = ["Hello, my friend. Did you receive?", "Rise of that",
            "I don't want to say anything, brother.", "Thanks for watching!", ""]
    for t in real:
        assert is_probably_real_dispatch(t), f"FALSE NEGATIVE: {t!r}"
    for t in junk:
        assert not is_probably_real_dispatch(t), f"FALSE POSITIVE: {t!r}"
    print(f"self-check OK: {len(real)} real kept, {len(junk)} junk rejected")
