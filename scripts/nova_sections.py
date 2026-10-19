#!/usr/bin/env python3
"""nova_sections.py — the journal's section list, in one importable place.

Jordan, 2026-10-19: Operations + Local merged into Burbank and Ops; Essays + Synthesis
merged into Research; Tech Today became Tech; Dreams retired outright.

Lightweight on purpose (no imports beyond stdlib) so nova_lexicon, nova_image_utils,
nova_cross_linker, nova_big_brother and friends can use it without dragging in
nova_journal. nova_journal.SECTION_REDIRECTS / SECTION_REMAP ARE this dict.
"""

# Sections a generator may publish into today.
LIVE_SECTIONS = ("burbank-ops", "opinions", "watches", "cars", "tech", "food", "research",
                 "digests", "security", "meta", "after-dark", "pilot", "start-here")

# Kept on the site, no new posts.
ARCHIVE_SECTIONS = ("art",)

# Retired names and where their writes go now (the single remap).
SECTION_REDIRECTS = {"rando": "burbank-ops", "tech-today": "tech",
                     "operations": "burbank-ops", "local": "burbank-ops",   # Burbank and Ops 2026-10-19
                     "essays": "research", "synthesis": "research"}           # Research 2026-10-19

# Retired with nowhere to go: nothing may publish here.
RETIRED_SECTIONS = ("dreams",)


def canon(section: str) -> str:
    """Retired-but-redirected names map to their new home; everything else passes through."""
    s = (section or "").strip().lower()
    return SECTION_REDIRECTS.get(s, s)


def is_retired(section: str) -> bool:
    return canon(section) in RETIRED_SECTIONS
