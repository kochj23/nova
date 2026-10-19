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


# ── Topic routing (Jordan 2026-10-19) ────────────────────────────────────────────────────────────
# Nova's own long-form pieces (essays, research, opinions, topic studies, research papers) that are
# clearly ABOUT music or the military publish to Music and Friends / Military and Friends instead of
# Nova and Friends. Conservative on purpose: one stray keyword never moves a piece.
import re as _re  # noqa: E402

MUSIC_SOURCES = frozenset({"music", "metal", "jazz", "hardcore_punk", "wiki_punk_hardcore", "nowave", "newwave",
                           "edm", "idm"})
MILITARY_SOURCES = frozenset({"military_history", "vietnam_war", "korean_war", "american_civil_war",
                              "american_indian_wars"})          # + every military_doctrine* vector
# Count toward military only when the topic itself is about war/defence.
MILITARY_IF_TOPIC = frozenset({"world_history", "intelligence", "signals_intelligence"})

_MUSIC_RX = _re.compile(
    r"\b(?:music\w*|musician\w*|songs?|songwrit\w*|albums?|bands?|punk|no[ -]?wave|new[ -]?wave|post-punk|jazz|"
    r"hip[ -]?hop|techno|drum (?:and|&|n) bass|dnb|edm|idm|rock (?:and|&|n'?) roll|heavy metal|black metal|"
    r"thrash|hardcore punk|guitar\w*|bassist|drummer|record labels?|vinyl|discograph\w*|symphon\w*|opera|"
    r"orchestra\w*|composers?|concerts?|melod\w*|lyric\w*|singers?|chords?|riffs?|rave|dj|djs|disco|funk|"
    r"blues|reggae|hymns?|anthems?)\b", _re.I)
_MILITARY_RX = _re.compile(
    r"\b(?:military|militar\w*|wars?|warfare|wartime|battles?|battlefield\w*|army|armies|navy|naval|air force|"
    r"marines?|soldiers?|troops|weapons?|weaponry|rifles?|firearms?|tanks?|artillery|missiles?|drone warfare|"
    r"doctrine|defen[cs]e polic\w*|pentagon|nato|invasion|invaded|wwi|wwii|world war|cold war|ukrain\w*|"
    r"kremlin|insurgen\w*|counterinsurgency|sieges?|generals|admirals?|regiments?|divisions|campaigns?|"
    r"armou?r|warships?|submarines?|fighter jets?|bombers?|veterans?)\b", _re.I)
# Figurative wars and the like never count.
_NOT_MILITARY = _re.compile(r"\b(?:culture|price|console|format|browser|flame|star|chip|streaming|bidding|talent|"
                            r"patent|turf|trade|tariff|bidding|search|cola|cloud|ai|editor|platform)\s+wars?\b|"
                            r"\bwar on (?:drugs|poverty|terror)\b|\bwar room\b|\bcampaigns? (?:finance|trail)\b", _re.I)


def _kw(rx, text):
    return {m.group(0).lower() for m in rx.finditer(text or "")}


def topic_scores(text: str, sources=()) -> dict:
    """{"music": n, "military": n}: 2 per distinct topic keyword, plus 2 when >= half of the memory
    sources are that subject's vectors (3 when >= 80%; needs >= 3 sources, or a single source vector)."""
    clean = _NOT_MILITARY.sub(" ", text or "")
    kw = {"music": _kw(_MUSIC_RX, clean), "military": _kw(_MILITARY_RX, clean)}
    srcs = [str(s or "").lower() for s in sources if s]
    scores = {}
    for subj in ("music", "military"):
        def hit(s):
            if subj == "music":
                return s in MUSIC_SOURCES
            return s in MILITARY_SOURCES or s.startswith("military_doctrine") or \
                (s in MILITARY_IF_TOPIC and bool(kw["military"]))
        share = (sum(map(hit, srcs)) / len(srcs)) if srcs and (len(srcs) >= 3 or len(set(srcs)) == 1) else 0
        scores[subj] = 2 * len(kw[subj]) + (3 if share >= 0.8 else 2 if share >= 0.5 else 0)
    return scores


def topic_section(text: str, sources=(), default=None):
    """'music' / 'military' when the piece is clearly about that subject (score >= 3: two distinct
    keywords, a keyword plus a source majority, or an 80% source majority), else default. If both
    qualify, the higher score wins; a tie stays put (default)."""
    s = topic_scores(text, sources)
    ok = {k: v for k, v in s.items() if v >= 3}
    if not ok:
        return default
    if len(ok) == 2 and ok["music"] == ok["military"]:
        return default
    return max(ok, key=ok.get)


def route_topic(section: str, text: str, sources=()) -> str:
    """Re-route a Nova-and-Friends piece (nova / research / opinions / essays / synthesis) that is
    clearly about music or the military; every other section is returned unchanged."""
    if canon(section) != "nova":
        return section
    return topic_section(text, sources, default=section)
