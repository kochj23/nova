#!/usr/bin/env python3
"""
nova_journal_guard.py — the publish-quality gate for Nova's auto-journal.

Nova's LLM pipelines sometimes emit something that is NOT an article: a refusal ("I can't
write this essay — you handed me a grocery list of Wikipedia excerpts"), a clarifying
question to the operator ("I need the direct URL to fetch the article"), a raw template
placeholder ("# TITLE:"), or a generic "Introduction" stub from a title-extraction bug.
With no gate, these went straight to the PUBLIC site as "articles" — including a fake
"BREAKING" security alert that was really the model asking for a URL (2026-07-25).

is_publishable(title, body) is the single choke point every publisher calls before writing.
It errs toward NOT nuking real content: it fires only on markers that are specifically
LLM-talking-to-the-operator or LLM-refusing-bad-input, not on any dramatic opening (a real
essay may legitimately start "Let me walk you through this nightmare").
"""
import re

# Strong body markers: the model addressing the OPERATOR or refusing the INPUT. These do
# not appear in a genuine finished article. Matched case-insensitively anywhere in the body.
_REFUSAL_BODY = [
    r"i need the (direct )?url",
    r"provide the full url",
    r"can you provide the",
    r"i need websearch permission",
    r"i'?m ready to fetch",
    r"i'?ll fetch the (full )?(details|article)",
    r"that'?s not a topic",
    r"(grocery list|pile|salad|collection) of .{0,30}wikipedia",
    r"you (just )?(handed|fed) me",
    r"isn'?t a real (thing|topic)",
    r"i'?m going to stop you right there",
    r"i need to stop you (right )?here",
    r"hold the fuck up",
    r"hold up,? little mister",
    r"did you mean to write",
    r"want me to write it as",
    r"what would you like me to",
    r"i'?m not going to (guess|invent|fabricate|write)",
    r"i can'?t write (this|a formal)",
    r"i'?ll infer the .{0,20}pattern",
]
# Template/system leakage that should never be in published output.
_META = [r"\[system instructions\]", r"^#?\s*title:\s*$", r"\bas an ai language model\b",
         r"i don'?t have (real-?time )?access"]

# Titles that are themselves the tell (meta / placeholder / refusal openings).
_BAD_TITLE_EXACT = {"introduction", "untitled", "", "title"}
_BAD_TITLE_PREFIX = ("i'm ready", "i need", "i can't", "i cannot", "want me to", "you've got",
                     "did you mean", "a typo for", "can you provide", "hold up", "hold the fuck",
                     "i'm going to stop you", "i need to stop you", "let me know", "i'll infer",
                     "understood", "sure,", "okay,", "here's what i need")

_STRIP_EMOJI = re.compile(r"^[^\w]+")   # leading emoji/space (titles are "🛡️ I'm ready…")


def is_publishable(title: str, body: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means DO NOT publish — it's a refusal/meta/stub."""
    t = _STRIP_EMOJI.sub("", (title or "")).strip()
    tl = t.lower()
    b = (body or "").strip()
    bl = b.lower()

    if tl in _BAD_TITLE_EXACT:
        return (False, f"placeholder/meta title: {title!r}")
    if any(tl.startswith(p) for p in _BAD_TITLE_PREFIX):
        return (False, f"refusal/meta title: {title!r}")

    # A real article is substantial; refusals/stubs are short. Word count on the body only.
    words = len(re.findall(r"\w+", b))
    if words < 60:
        return (False, f"too short to be an article ({words} words)")

    for rx in _REFUSAL_BODY:
        if re.search(rx, bl):
            return (False, f"body reads as a refusal/clarifying-question (matched /{rx}/)")
    for rx in _META:
        if re.search(rx, bl, re.MULTILINE):
            return (False, f"body contains template/system leakage (matched /{rx}/)")

    return (True, "ok")


if __name__ == "__main__":
    # self-check: the real garbage must be blocked, real articles must pass.
    BAD = [
        ("🛡️ I'm ready to fetch the article, but I need the direct URL to", "The headline you provided is X. Can you provide the full URL? " * 6),
        ("Introduction", "word " * 400),
        ("I can't write this essay", "You handed me a grocery list of Wikipedia excerpts with no coherent thesis. " * 4),
        ("Hold the fuck up, Little Mister", "You just handed me a salad of unrelated Wikipedia fragments. " * 4),
    ]
    GOOD = [
        ("Let me walk you through this nightmare", "The scheduler vanished today and here is the full postmortem. " * 30),
        ("Burbank Hits 96 Degrees", "It was another scorcher in the Media District today. " * 40),
    ]
    ok = True
    for ti, bo in BAD:
        p, r = is_publishable(ti, bo)
        print(f"BLOCK {'✓' if not p else '✗ LEAKED'}: {ti[:40]!r} -> {r}"); ok &= (not p)
    for ti, bo in GOOD:
        p, r = is_publishable(ti, bo)
        print(f"ALLOW {'✓' if p else '✗ FALSE-BLOCK'}: {ti[:40]!r} -> {r}"); ok &= p
    print("OK" if ok else "FAIL")
