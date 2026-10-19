#!/usr/bin/env python3
"""nova_sections.py — the journal's section list, in one importable place.

Jordan, 2026-10-19: Operations + Local merged into Burbank and Ops; Essays + Synthesis
merged into Research; Tech Today became Tech; Dreams retired outright. Later the same day
Research + Opinions merged into Nova and Friends (/nova/), which absorbs essays/synthesis too.

Lightweight on purpose (no imports beyond stdlib) so nova_lexicon, nova_image_utils,
nova_cross_linker, nova_big_brother and friends can use it without dragging in
nova_journal. nova_journal.SECTION_REDIRECTS / SECTION_REMAP ARE this dict.
"""

# Sections a generator may publish into today.
LIVE_SECTIONS = ("burbank-ops", "nova", "watches", "cars", "tech", "food",
                 "digests", "security", "meta", "after-dark", "pilot", "start-here")

# The eight *and Friends* sections added 2026-10-19 (nova_friends.py --subject <slug>).
LIVE_SECTIONS += ("music", "military", "screen", "science", "mystery", "crime", "workshop", "travel")

# Kept on the site, no new posts.
ARCHIVE_SECTIONS = ("art",)

# Retired names and where their writes go now (the single remap).
SECTION_REDIRECTS = {"rando": "burbank-ops", "tech-today": "tech",
                     "operations": "burbank-ops", "local": "burbank-ops",   # Burbank and Ops 2026-10-19
                     "research": "nova", "opinions": "nova",               # Nova and Friends 2026-10-19
                     "essays": "nova", "synthesis": "nova"}

# Retired with nowhere to go: nothing may publish here.
RETIRED_SECTIONS = ("dreams",)


def canon(section: str) -> str:
    """Retired-but-redirected names map to their new home; everything else passes through."""
    s = (section or "").strip().lower()
    return SECTION_REDIRECTS.get(s, s)


def is_retired(section: str) -> bool:
    return canon(section) in RETIRED_SECTIONS
