#!/usr/bin/env python3
"""
nova_memory_quality_filter.py — Intake validation for Nova's vector memory.

Called by the memory server ingest pipeline to reject garbage before it gets
embedded and stored. Returns True if content passes quality checks.

Quality rules:
  1. Minimum content length (50 chars after stripping)
  2. No bracket-only entries ([Name] with just a section header)
  3. No wiki markup fragments (== Cast == with nothing else)
  4. No repetitive loops (same phrase repeated 3+ times)
  5. No entries that are just a list of names/links with no context
  6. Minimum information density (not just whitespace/formatting)

Written by Jordan Koch.
"""

import re


MIN_CONTENT_LENGTH = 50
MAX_BRACKET_RATIO = 0.5  # if >50% of content is bracketed terms, reject


def passes_quality(text: str, source: str = "") -> tuple:
    """Check if text meets minimum quality bar for memory storage.
    
    Returns (passes: bool, reason: str).
    """
    if not text or not text.strip():
        return False, "empty"

    stripped = text.strip()
    
    # Rule 1: Minimum length
    if len(stripped) < MIN_CONTENT_LENGTH:
        return False, f"too_short ({len(stripped)} chars)"
    
    # Rule 2: Bracket-only entries — [Topic Name] followed by just a section header
    if re.match(r'^\[.*?\]\s*\n\s*\w[\w\s]{0,40}$', stripped, re.DOTALL):
        return False, "bracket_header_only"
    
    # Rule 3: Wiki markup fragments with no content
    lines = [l.strip() for l in stripped.split('\n') if l.strip()]
    markup_lines = sum(1 for l in lines if re.match(r'^={2,}.*={2,}$', l))
    if markup_lines > 0 and len(lines) <= markup_lines + 1:
        return False, "wiki_markup_fragment"
    
    # Rule 4: Repetitive loops (same phrase repeated 3+ times)
    if re.search(r'(.{10,}?)\1{2,}', stripped):
        return False, "repetitive_loop"
    
    # Rule 5: Just a list of bracketed terms with no explanatory text
    bracket_count = len(re.findall(r'\[.*?\]', stripped))
    words_outside_brackets = len(re.sub(r'\[.*?\]', '', stripped).split())
    if bracket_count > 3 and words_outside_brackets < bracket_count * 2:
        return False, "bracket_list_no_context"
    
    # Rule 6: Information density — mostly whitespace or formatting
    alpha_chars = sum(1 for c in stripped if c.isalpha())
    if len(stripped) > 0 and alpha_chars / len(stripped) < 0.3:
        return False, f"low_info_density ({alpha_chars/len(stripped):.1%} alpha)"
    
    # Rule 7: Single-word entries or entries that are just a title
    if len(lines) == 1 and len(stripped.split()) < 5:
        return False, "single_phrase"
    
    return True, "ok"


# Wiki/citation section markers — chunks dominated by these are reference material,
# not recallable knowledge. We keep them (non-destructive) but route to tier=reference.
_REFERENCE_MARKERS = (
    "== See also ==", "== References ==", "== External links ==",
    "== Further reading ==", "== Notes ==", "== Bibliography ==",
    "== Citations ==", "== Sources ==", "== Footnotes ==",
)


def classify_quality(text: str, source: str = "") -> tuple:
    """Three-way intake routing for the memory write path.

    Returns (verdict, reason) where verdict is one of:
      "allow"     — store normally (default tier)
      "reference" — store but demote to tier='reference' (kept, excluded from recall)
      "reject"    — do not store (cruft / garbage, fails passes_quality)

    Reference routing fires when a chunk is dominated by citation/section-tail
    boilerplate (e.g. a chunk that is mostly a "Further reading" reference list).
    This is conservative — real prose passes through as "allow".
    """
    if not text or not text.strip():
        return "reject", "empty"

    stripped = text.strip()

    # Reference detection runs BEFORE the hard quality bar: a citation/section
    # chunk (e.g. a "Further reading" list) would otherwise be rejected as a wiki
    # markup fragment, but we want to KEEP it (non-destructive) — just demote it
    # to tier=reference so it's preserved without polluting semantic recall.
    head = stripped[:40].lstrip()
    for marker in _REFERENCE_MARKERS:
        # Whole chunk is a reference list (marker at the very start) OR the marker
        # sits in the first 40% so the recallable prose part is too thin.
        pos = stripped.find(marker)
        if head.startswith(marker) or (pos != -1 and pos < len(stripped) * 0.4):
            # Still reject if it's empty/degenerate below the marker.
            if len(stripped) < MIN_CONTENT_LENGTH:
                return "reject", f"too_short ({len(stripped)} chars)"
            return "reference", f"reference_section ({marker.strip('= ').lower()})"

    # Otherwise apply the hard quality bar — true garbage is rejected outright.
    ok, reason = passes_quality(stripped, source)
    if not ok:
        return "reject", reason

    return "allow", "ok"


def filter_batch(entries: list) -> tuple:
    """Filter a batch of entries. Returns (passed, rejected) lists."""
    passed = []
    rejected = []
    for entry in entries:
        text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
        source = entry.get("source", "") if isinstance(entry, dict) else ""
        ok, reason = passes_quality(text, source)
        if ok:
            passed.append(entry)
        else:
            rejected.append({"entry": entry, "reason": reason})
    return passed, rejected


if __name__ == "__main__":
    # Self-test
    tests = [
        ("[Agar]\nOther uses", False),
        ("[Psilocybin]\nChemistry\n\nPhysical properties", False),
        ("== Cast ==", False),
        ("Mark Anthony. Mark Anthony. Mark Anthony. Mark Anthony. Mark Anthony.", False),
        ("This is a proper memory entry about how mushrooms grow in forest environments with adequate moisture and shade.", True),
        ("", False),
        ("hi", False),
    ]
    
    for text, expected in tests:
        result, reason = passes_quality(text)
        status = "PASS" if result == expected else "FAIL"
        print(f"  {status}: passes={result} (expected={expected}) reason={reason} text={text[:50]}")

    print("\n  classify_quality:")
    ctests = [
        ("This is a proper memory entry about how mushrooms grow in forest environments with adequate moisture and shade.", "allow"),
        ("== Further reading ==\nAllison, Graham; Zelikow, Philip (1999). Essence of Decision. New York: Addison Wesley Longman. ISBN 978-0-321-01349-1.", "reference"),
        ("== See also ==\nGerman idealism\nNeocriticism\nNorth American Kant Society\nList of publications", "reference"),
        ("hi", "reject"),
        ("", "reject"),
    ]
    for text, expected in ctests:
        verdict, reason = classify_quality(text)
        status = "PASS" if verdict == expected else "FAIL"
        print(f"  {status}: verdict={verdict} (expected={expected}) reason={reason} text={text[:50]}")
