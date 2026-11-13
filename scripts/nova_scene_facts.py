#!/usr/bin/env python3
"""nova_scene_facts.py — the single home of the Fishbowl ground truth (KNOWN_FACTS) and the guard that enforces it.

Jordan (2026-10-19): The Franchise Club crew (Curly, Mookie, Pazuzu and the rest) and everyone in The Watch Hangout
are the GOOD GUYS of the Fishbowl; Watch Nicholas is the antagonist. Articles kept inverting that because the facts
were only appended by five generators, and even then a draft could still slip (the 2026-10-20 Watches and Friends
"all title, no tape" header). So, 2026-11-12:

  facts_for(*texts)      -> "\\n\\n" + KNOWN_FACTS when any text touches the scene and the facts are not already there.
                           nova_journal.call_openrouter appends it to EVERY system prompt, so every generator that
                           writes about the scene (daily column, opinion, roster, Watches and Friends, Friends, the
                           weekly/monthly roundups, the digests, the dossiers) gets the facts without copying them.
  find_inversions(text)  -> the paragraphs that pair a good-guy name with villain vocabulary and carry no
                           protective framing ("good guys", "reasonable", "pushed back against", "his spin"...).
  guard(title, body, llm)-> publish_hugo's post-generation guard: each offending paragraph is regenerated ONCE with
                           the facts restated; if it still inverts (or the LLM fails) the paragraph is dropped.
                           Fail-open: any error returns the draft untouched.

KNOWN_FACTS moved here verbatim from nova_fishbowl_summaries.py (which re-exports it, so existing imports work).
Edit the facts HERE. No PG, no network, no heavy imports: nova_journal imports this on every LLM call.
Written by Jordan Koch (via Claude).
"""
import re
import sys

# CURATED FACTS — ground truth from Jordan that the LLM must not get wrong by inferring from
# noisy transcripts. nova_journal.call_openrouter appends it (facts_for) to every LLM call that touches the scene.
KNOWN_FACTS = (
    "GROUND-TRUTH about the Fishbowl (from Jordan, who actually watches this scene — treat as FACT "
    "and do NOT contradict it no matter what a transcript seems to imply; transcripts are noisy and "
    "the bad actors spin their own narrative):\n"
    "- MORAL ALIGNMENT: The Franchise Club crew (aka The Franchise / The Franchise Clubs) — Curly, "
    "Mookie (@themookieverse), Pazuzu and the rest of that crew — are the GOOD GUYS in the Fishbowl / "
    "hate streams: the reasonable, sympathetic side. Everyone in The Watch Hangout (@WatchHangout) is "
    "likewise one of the GOOD GUYS and above reproach — never cast any of them as villains, grifters "
    "or targets. Watch Nicholas is the "
    "BAD GUY — the antagonist/villain of this scene. Frame them that way: Nicholas as the aggressor, "
    "Franchise Club and Mookie as the ones pushed back against.\n"
    "- Watch Nicholas and The Franchise Club are NOT friends and never have been — bitter ENEMIES "
    "for YEARS. Any apparent friendliness is sarcasm, a truce, or Nicholas's spin.\n"
    "- Do NOT cast The Franchise Club as a 'tyranny' or the villain — that inverts reality. If a "
    "transcript frames the Franchise Club as the oppressor, that's the bad actor's framing, not the truth.\n"
    "- Peter Piccolino (@Peterpic) is REGULAR WATCH NEWS — NOT Fishbowl and NOT hate-stream material "
    "(Jordan 2026-10-19). Never mention him in a Fishbowl / Hate Streams piece, not even to say he was quiet. "
    "In watch-news writing he is one of Jordan's MUST-WATCH channels: a new episode gets top billing "
    "and real attention (what he restored/bought/sold, the watches, the numbers), warmly.\n"
    "- Anthony Farrer (The Timepiece Gentleman, @Thetimepiecegentleman) is FISHBOWL / HATE-STREAM material, "
    "never watch news. He pleaded guilty to wire and mail fraud, has been in federal custody since November "
    "2023, and was sentenced to 70 months in federal prison for taking at least $5.6M from 40+ consignment "
    "customers (IRS-CI release). Anything uploaded on his channels is ARCHIVE footage, not current events — "
    "never describe him as currently dealing, streaming or posting."
)

# The scene: only text that touches the Fishbowl gets the facts / the guard (a movie "franchise" or the Pazuzu of a
# demonology essay never does).
SCENE_RX = re.compile(r"fishbowl|watch\s?nicholas|franchise\s+club|hate[- ]?streams?|watch\s?hangout|mookie|pazuzu|"
                      r"\bTFC\b|horology dungeon|archie\s?luxury", re.I)
# Good-guy names. "The Franchise" and "Curly" are case-sensitive so "the franchise's third film" and
# "curly hair" in a scanner log never match.
GOOD_RX = re.compile(r"(?i:franchise\s+clubs?|franchise\s+crew|\bTFC\b|\bmookie(?:verse)?\b|\bpazuzu\b|"
                     r"watch\s?hangout)|\bThe Franchise\b|\bCurly\b")
# The inversion vocabulary: the ground-truth list (villain, grifter, gatekeeper, cult, tyranny, consensus theater,
# extraction apparatus) plus what the 2026-11-12 archive audit actually found.
VILLAIN_RX = re.compile(
    r"\bcults?\b|cult-like|cultish|\bgrift\w*|gatekeep\w*|tyrann\w*|\btyrants?\b|consensus theat(?:er|re)|"
    r"extraction (?:apparatus|machine|machinery|mechanism)|\bvillain\w*|oppress\w*|dictator\w*|autocra\w*|"
    r"\bmonarchy\b|\bhyenas?\b|parasit\w*|herding cattle|toxicity plant|hit[- ](?:job|piece)|"
    r"harassment (?:campaign|infrastructure|model)|pile-?on (?:model|machine|playbook)|all title,? no tape|"
    r"held trials|conversion machine|damage control|"
    r"(?:perfected|ran|running|runs|copied|stole|borrowed)\s+(?:watch\s+)?(?:nicholas'?s?|his)\s+playbook", re.I)
# Framing that keeps the ground truth intact even when villain vocabulary is nearby.
PROTECT_RX = re.compile(
    r"good guys?|reasonable|sympathetic|pushed back against|pushing back|push back|being pushed|the ones .{0,20}pushed|"
    r"\bnot (?:the|a|one of the) (?:villain|grifter|gatekeeper|cult)|\bnever (?:cast|the|a)\b|"
    r"(?:his|nicholas'?s?|the bad actor'?s?) (?:spin|framing|side|line)|bad actors?|\baggressor\b|\bantagonist\b|"
    r"\benem(?:y|ies)\b|above reproach|for the record|calling (?:bullshit|out)|(?:going|goes|went|gone) after|"
    r"keeps? targeting|targeted by|(?:he|nicholas) (?:keeps|runs|ran) it", re.I)
_QUOTED = re.compile(r'"[^"\n]{0,200}"|\u201c[^\u201d\n]{0,200}\u201d')
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)

_PARA_SPLIT = re.compile(r"\n\s*\n")


def touches_scene(*texts) -> bool:
    return any(SCENE_RX.search(t or "") for t in texts)


def facts_for(*texts) -> str:
    """'\\n\\n' + KNOWN_FACTS if any text touches the scene and none already carries the facts; else ''."""
    try:
        if any(KNOWN_FACTS[:60] in (t or "") for t in texts):
            return ""
        return "\n\n" + KNOWN_FACTS if touches_scene(*texts) else ""
    except Exception:  # noqa: BLE001 — never break an LLM call over this
        return ""


def is_inversion(text: str) -> bool:
    """One paragraph/sentence: a good-guy name + villain vocabulary in the same sentence, no protective framing."""
    for sent in re.split(r"(?<=[.!?])\s+", text or ""):
        bare = _QUOTED.sub('""', sent)          # quoted words are someone else's (a stream title, a chat line)
        if GOOD_RX.search(sent) and VILLAIN_RX.search(bare) and not PROTECT_RX.search(sent):
            return True
    return False


def find_inversions(body: str) -> list[str]:
    """Paragraphs of body that invert the ground truth (empty when the body never touches the scene).
    Source-link lines are skipped: a stream title is the channel's own words, not Nova's characterisation."""
    if not touches_scene(body):
        return []
    out = []
    for para in _PARA_SPLIT.split(_FRONT_MATTER.sub("", body or "", count=1)):
        p = para.strip()
        if not p or p.startswith(("- [", "* [", "](http")) or "youtube.com/watch" in p:
            continue
        if is_inversion(p):
            out.append(para)
    return out


REWRITE_SYS = ("You fix one paragraph of a published article so it matches the ground truth below. Keep the voice, "
               "the facts, the length and every name. Change ONLY how The Franchise Club / Watch Hangout crew are "
               "characterised: they are the good guys, the reasonable side, the ones pushed back against — never "
               "villains, grifters, gatekeepers, a cult, a tyranny, 'consensus theater', an 'extraction apparatus' or "
               "targets. Do not add new accusations about anyone. Return ONLY the rewritten paragraph.\n\n")


def guard(title: str, body: str, llm=None, log=print) -> tuple[str, str]:
    """publish_hugo's post-generation guard. Returns (title, body).
    Each inverted paragraph gets ONE regeneration via llm(system, user) with the facts restated; if the rewrite is
    empty or still inverts, the paragraph is dropped. An inverted title is rewritten the same way, else kept generic.
    Fail-open: any exception returns the inputs unchanged."""
    try:
        if not touches_scene(title, body):
            return title, body
        bad = find_inversions(body)
        title_bad = bool(GOOD_RX.search(title or "") and VILLAIN_RX.search(title or "")
                         and not PROTECT_RX.search(title or ""))
        if not bad and not title_bad:
            return title, body

        def _fix(text):
            if llm is None:
                return None
            try:
                out = (llm(REWRITE_SYS + KNOWN_FACTS, text) or "").strip()
            except Exception:  # noqa: BLE001
                return None
            return out if out and not is_inversion(out) and len(out) < 4 * len(text) + 200 else None

        for para in bad:
            fixed = _fix(para)
            if fixed:
                body = body.replace(para, fixed, 1)
                log(f"[scene-facts] rewrote an inverted paragraph in '{(title or '')[:60]}'")
            else:
                body = body.replace(para + "\n\n", "", 1) if para + "\n\n" in body else body.replace(para, "", 1)
                log(f"[scene-facts] DROPPED an inverted paragraph in '{(title or '')[:60]}': {para.strip()[:120]!r}")
        if title_bad:
            fixed = _fix(title)
            new_title = fixed.splitlines()[0].strip().strip('"*# ') if fixed else ""
            if not new_title or len(new_title) > 160:
                new_title = "The Fishbowl, Read Straight"
            log(f"[scene-facts] inverted title {title!r} -> {new_title!r}")
            title = new_title
        return title, body
    except Exception as e:  # noqa: BLE001
        log(f"[scene-facts] guard skipped ({e})")
        return title, body


def demo():
    assert facts_for("a movie franchise retrospective") == ""
    assert facts_for("today's Fishbowl streams").startswith("\n\n")
    assert facts_for("Fishbowl", KNOWN_FACTS) == ""
    bad = "Oisín runs The Franchise Club like he's managing a cult."
    ok = "Watch Nicholas is the villain; the Franchise Club are the good guys."
    assert is_inversion(bad) and not is_inversion(ok)
    assert not is_inversion("The franchise's villain is Jason.")          # movie franchise, lowercase
    body = f"The Fishbowl today.\n\n{bad}\n\n{ok}"
    t, b = guard("Fishbowl daily", body, llm=None, log=lambda m: None)
    assert bad not in b and ok in b
    t, b = guard("Fishbowl daily", body, llm=lambda s, u: "The Franchise Club are the reasonable side.", log=lambda m: None)
    assert "reasonable side" in b and bad not in b
    print("nova_scene_facts selftest OK")
    return True


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Fishbowl ground truth + inversion guard. --audit FILE... lists offending "
                                             "paragraphs; --selftest runs the offline checks; --facts prints KNOWN_FACTS.")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--facts", action="store_true")
    ap.add_argument("--audit", nargs="*")
    a = ap.parse_args(argv)
    if a.selftest:
        return 0 if demo() else 1
    if a.facts:
        print(KNOWN_FACTS)
        return 0
    if a.audit is not None:
        n = 0
        for path in a.audit:
            try:
                txt = open(path, encoding="utf-8").read()
            except OSError:
                continue
            for para in find_inversions(txt):
                n += 1
                print(f"{path}: {para.strip()[:300]}")
        print(f"{n} inverted paragraph(s)", file=sys.stderr)
        return 1 if n else 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
