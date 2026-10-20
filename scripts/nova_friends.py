#!/opt/homebrew/bin/python3
"""nova_friends.py — "Cars and Friends", "Tech and Friends" and "Food and Friends", the sibling
sections of Watches and Friends (Jordan 2026-10-19). Same schedule, same rules, same shape — minus
the Fishbowl / Hate Streams part, which has no equivalent outside the watch world.

Featured block at the TOP of every issue, above the roundup (Jordan 2026-10-19), built by the shared
nova_watches_and_friends.featured_block (grounded, fail closed — no grounding = no block):
  * cars  — "The Big Story": the day's most interesting new car story, linked.
  * tech  — "Buying Recommendation": one device from the day's tech material that fits the publicly
            known home stack (HOME_STACK — the About page's list, nothing private).
  * food  — "Recipe Recommendation": one recipe from the day's food videos or Nova's recipe memories
            (source 'recipes', type 'recipe' — full recipe pages with URLs), credited and linked.
            Selection follows the house rules in PG (nova_ops.service_config food_and_friends /
            recipe_constraints: prompt rules, preferred/avoided recipe words, and private terms the
            published block must never contain). They are loaded at runtime and never live in this repo;
            if they can't be loaded, or the block contains a private term, the block is omitted.

Shape: one grounded NEWS call over the window's transcripts/articles, a grounded "deeper cuts"
call only if the issue is still under the minimum, then a code-built Sources list from the
memory rows. Tech and Friends replaces the old Tech Today section (content/tech-today -> tech).

Sources (all from nova_memories, window = last DAYS days):
  * subscription-audio captures (pipeline yt_subs_audio) and downloaded YouTube episodes
    (/videos/youtube/<show>/) from the subject's channel list — Jordan's subscriptions, split
    by subject 2026-10-19. The vector a capture landed in is NOT trusted (Hotrods & Homestead
    lands in computing, AMMO NYC in automotive), the channel list is.
  * tech only: news/blog articles (gov_rss, blog_post) from the computing / tech_blog /
    technology_general / home_automation vectors, minus space/astronomy feeds and forum chatter.

Rules carried over from Watches and Friends: grounded only, fail closed (no publish if the
news call fails, if nothing is in the window, or if the draft trips the public-journal privacy
check below). Never anything about Jordan's employer or work, no "OpenClaw", no sexual content.

Reuses group_videos / digest / sources / split_title / words / _connect / featured_block from
nova_watches_and_friends.py.
Eight more siblings (Jordan 2026-10-19), same shape, each with its own top block:
  music "Track of the Day", military "This Day in Military History" (grounded in Nova's military /
  world history memories for today's month-day; falls back to the day's best story), screen "What to
  Watch Tonight" (channel material ONLY — never Plex or viewing history), science "The Coolest Thing I
  Learned", mystery "The Rabbit Hole of the Day" (may draw on the esoteric library, labels kept:
  Lovecraft's Necronomicon is fiction, no grimoire is ever instructions), crime "Case of the Day" (no
  private individuals beyond what the channels cover, never an address), workshop "Project Worth
  Stealing", travel "Place of the Day". Thin channels: a daily run with fewer than THIN_MIN videos
  widens its window to WIDEN_DAYS (new subjects only).

History and Belief (Jordan 2026-10-19, second pass): history "This Day in History" (Nova's world_history /
  history memories for today's month-day, non-military events first; falls back to the day's best story);
  belief "Text in Context" (one passage from Nova's scripture + non-theism library, quoted word for word and
  cited — verse_ok() checks the quote against the library text and the Bible chapter:verse, fail closed —
  then who wrote it and when, what it meant first, how it is read now, and a skeptic's or another
  tradition's view). Belief was "Faith and Friends" until Jordan (an atheist who finds religion
  interesting) asked for a historian's section, not a devotional one — renamed before its first issue.

Usage: nova_friends.py --subject cars|tech|food|music|military|screen|science|mystery|crime|workshop|travel|history|belief
                       [--days 1] [--dry-run]
"""
import argparse
import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj          # noqa: E402
import nova_voice                  # noqa: E402
from nova_sections import is_belief_topic   # noqa: E402
from nova_watches_and_friends import (MEM_DSN, MODEL, OPS_DSN, _connect, digest, featured_block,  # noqa: E402
                                      group_videos, place_block, sources, split_title, words)

VID_PER, VID_MAX = 1600, 30         # ~48k chars of video transcript
ART_PER, ART_MAX = 700, 24          # + ~17k of articles: under the size that hangs claude -p

CARS_CHANNELS = (
    "We Are Curated", "WatchJRGo",  # Jordan 2026-10-19: automotive despite the names
    "1320video", "Adam LZ", "April's Garage", "ArnieTex", "Atlantic Beach Customs", "Audrain Museum Network",
    "Auto Auction Rebuilds", "AutoLegacy", "Bad Obsession Motorsport", "Banks Power", "Barrett-Jackson",
    "Ben Collins Drives", "Between Two Pumps Podcast", "Brian Lohnes", "Brian Scotto", "BluCollarGarage",
    "Burnout Alley", "CarAudioFabrication", "carstereochick", "Car Stories Podcast", "Car Wizard", "Chip Foose",
    "CJ Race Cars", "Clay Millican", "Cleetus McFarland", "C&S Corvettes", "CRUTCHFIELD", "Dave's Auto Center",
    "David Freiburger", "Dennis Collins", "Desert 1320", "DragDriveRepeat", "DRIVETRIBE", "Driving With Jonny",
    "Dropping Names with Brent and Jonny", "Ed Bolian", "Edelbrock USA", "Edmunds Cars", "Electric Classic Cars",
    "EmeliaHartford", "Engine Power", "Extreme Online Store", "FarmtruckandAZN", "Ficarra Classic",
    "Filthy Built", "Finnegan's Garage", "Five Star Car Stereo", "Fluid MotorUnion", "Ford Racing",
    "Forgotten Wheels", "Gas Monkey Garage", "Gearshift Garage", "Grassroots Motorsports", "Guild's Garage",
    "Hagerty", "Haltech", "Hemmings", "Holley", "Hoovies Garage", "Hotrods & Homestead", "HP Tuners",
    "Jay Leno's Garage", "John Force Racing", "JonnyF1", "Kandy Man", "Killer Kustoms", "King Srt Mopar",
    "Kitty Explains Cars", "Kiwi Classics and Customs", "Larry Groves", "Late Model Performance",
    "Legendary Motorcar", "LegitStreetCars", "Lucky's Garage Show", "Mecum Auctions", "Mighty Car Mods",
    "Misha Charoudin", "Morley Kert", "MotoIQ", "MotorTrend Channel", "MotorTrend Network", "MotorWeek",
    "MyClassicCarTV", "Nappy Boy Automotive", "Official Stacey David", "O'Reilly Auto Parts", "PapadakisRacing",
    "Petersen Automotive Museum", "Petrolicious", "Porschify", "POWERBLOCK", "POWERNATION", "POWERtube TV",
    "Pushing Gears", "QA1", "RaceProvenMotorsports", "REALSTREETPERFORMANCE", "redline.archives",
    "RenegadeHybrids", "REPerformance UK", "Richard Holdener", "Rich Rebuilds", "Ringbrothers",
    "Roadkill Reborn", "Rob Dahm", "Rowdy Street Cars", "S650_Mendoza", "Sarah -n- Tuned", "Saturday Garage",
    "ScrapLifeLee", "ScrappedLifeLee", "Sparks Motors", "Speed Academy", "Speed Cartel", "Speedway Motors",
    "Speeed", "Spike's Car Radio", "StanceWorks", "Steve Dulcich", "Steve Magnante", "StreetRacingSource",
    "Subdivision Auto", "Supercharged Sib 🇺🇸", "Supercharged Sib", "superspeedersRob", "Tavarish",
    "That Racing Channel", "The Automotive Advantage", "The Backspeed Boys", "The Bearded Mechanic",
    "The Carmudgeon Show", "The Dirthead Shed", "The Gearhead Lounge", "The Hooptie Lot!",
    "The Questionable Garage", "TheSilverBuick", "TheSmokingTire", "TheSmokingTirePodcast", "Throttle House",
    "Torco USA", "Toys4Life C5", "Uncle Tony's Garage", "Valley Racing", "Vice Grip Garage", "VINwiki", "MISTER ZACHARY",
    "Viper Vlogs", "Vortech Superchargers", "Watts Performance Solutions", "WaxedClassics",
    "WestCoast Grudge Racing Media", "Wheeler Dealers", "Zack Klapman's Drivers' Test",
    "Zero To Sixty Automotive", "ᴢᴇʀᴏ ᴛᴏ ꜱɪxᴛʏ", "Zip Tie Tuning", "Bikes and Beards",
    "Orange County Choppers Original Network", "Pepe's Towing Service",
    # Jordan 2026-10-19: RC cars -> Cars
    "AMain Hobbies", "RCDriver_Online", "RC Girl", "Redcat Racing", "TeamAssociatedRC", "1:10 Rod Shop",
    # Jordan 2026-10-19 (second pass; Parkflyers RC, RC planes, now in Cars too, by his call)
    "Exotic Rubber", "Quick Shift", "Auto Focus", "Joy Riding", "GarageInsiderTV. com", "Drake Built",
    "Frankly Built", "MegaBuilds", "DipYourCar", "Glease Man", "Junktown James", "Riding With Alex Taylor",
    "RR&BD Driving School", "Rev Explained", "Toprank Importers", "John Clay Wolfe", "Parkflyers RC",
)
TECH_CHANNELS = (
    "3D Printed Props", "3D Printing Nerd", "3D Print Stuff", "9to5Mac", "Action Retro", "Adam Savage's Tested",
    "AI Engineer", "AIAnimation - Jon Draper", "AKZ Dev", "Alex Finn", "Alex Ziskind", "Apple", "Apple Developer",
    "Apple Home Authority", "AppleInsider", "AppleTrack", "Asianometry", "A Smarter House", "AssemblyAI",
    "Austin Evans", "Bambu Lab", "Brian Tong", "christophertitustv", "clabretro", "Claude", "CNC Kitchen", "CNET",
    "COMPUTER DATA", 'Computer History Archives Project ("CHAP")', "ConsumerTech With Joe",
    "Crosstalk Solutions", "Ctrl+Alt+Fail", "CyberFlow", "CZsWorld", "Dave's Garage", "DevOps Toolbox",
    "DevPsyche", "dosdude1", "DPC Technology", "Engadget", "Framework", "Gamers Nexus", "Grafana", "Hacker Joe",
    "Hacking Modern Life", "Ham Radio Crash Course", "Hardware Haven", "Home Automation Guy", "HotshotTek",
    "Hubitat Home Automation", "Hugh Jeffreys", "Ian Wootten", "IT Revolution", "jakkuh", "Kai Lentit",
    "LaurieWired", "Lawrence Systems", "Level1Techs", "Level 2 Jeff", "Linus Tech Tips", "LMGClips",
    "Louis Rossmann", "Low Level", "Luke Miani", "Mac84", "Machine Thinking", "Mactelecom Networks",
    "Made with Layers (Thomas Sanladerer)", "Made with Layers", "Magma Networks Ltd", "Maker's Muse",
    "Make Smart Matter", "Marques Brownlee", "MattKC", "Matthew Berman", "Ministry Of Hacking", "Missing Remote",
    "moshix", "NetworkChuck", "NetzSecurity", "Parts-People Dell Laptop Experts", "PCWorld", "RetroBytes",
    "Retro Game Corps", "saveitforparts", "ServeTheHome", "ShortCircuit", "Smarter Circuits", "Smart Home Solver",
    "Snazzy Labs", "SpaceRex", "Stuff Made Here", "tabGeeks: Pro Tech Meets Real Life", "Techmoan", "Techno Tim",
    "TechRadar", "The Computer Chronicles", "The Edge of Tech", "The Hacker Academy", "The Serial Port",
    "This Does Not Compute", "This Week in Tech", "Tom's Guide", "Tony259 | Tony's Hyper Tinkering Shop",
    "Tony's Hyper Tinkering Shop", "Ubiquiti", "Undecided with Matt Ferrell", "Unified IT", "WAN Show", "wifi U",
    "WIRED", "WunderTech", "YouTux Channel", "Welch Labs",
    "Mark Rober", "Real Engineering", "Veritasium",   # Jordan 2026-10-19: engineering/science -> Tech
    "Honest HiFi",                                    # Jordan 2026-10-19: HiFi -> Tech
    # Jordan 2026-10-19 (second pass)
    "TheGamingLemon", "Joe's Classic Video Games", "GhostStrats", "Zip Tie Tech", "Digit Nerd HTv", "styropyro",
    "Design Docs", "ModBot", "Lars Klint", "The Next Layer", "nthroot", "Bleeps and Boops", "Williston Audio Labs",
    "BocaBola",
)
FOOD_CHANNELS = (   # Jordan 2026-10-19. Undecided, so NOT here: Bacon 28, fonki cheff, The Stock Pot
    #                   (Peter Santenello and Miha's Life in Japan went to Travel).
    "Meat Church BBQ", "HowToBBQRight", "Chuds BBQ", "Mad Scientist BBQ", "allthingsbbq",
    "Cooper Abercrombie - Bar-A-BBQ", "Sous Vide Everything", "Binging with Babish", "SAM THE COOKING GUY",
    "SONS OF SAM THE COOKING GUY", "Gordon Ramsay", "Alton Brown", "MATTY MATHESON", "NOT ANOTHER COOKING SHOW",
    "America's Test Kitchen", "French Cooking Academy", "Christian Puglisi", "Iron Chef Dad", "Buzz Cooks",
    "Abuela's Table", "A Cooks Tour with Anthony Bourdain", "Andrew Zimmern", "Eater", "First We Feast",
    "The Immigrant Table", "Jimmy Tries World", "Tasting History with Max Miller", "Whiskey Tribe", "Still It",
    "James May's Planet Gin", "City Steading Brews", "Beers & Bars",
)
FOOD_EXCLUDED = ("Bacon 28", "fonki cheff", "The Stock Pot")

# Jordan 2026-10-19: eight more sections. Names are exact subscription names (service_config
# yt_subs_audio/subscriptions); every list is disjoint from every other (a test enforces it).
MUSIC_CHANNELS = (
    "The Punk Rock MBA",  # Jordan 2026-10-19
    "Rick Beato", "Rick Beato 2", "Adam Neely", "12tone", "Trash Theory",
    "Amplified - Classic Rock & Music History", "Rock N' Rewind", "Discogs", "KEXP", "UKF Drum & Bass",
    "DnB Allstars", "Drum&BassArena", "RELEVANT DNB", "DnB Portal", "Nia Archives", "Boiler Room", "HÖR BERLIN",
    "DJ Mag", "Skratch Bastid", "Rave United", "RJD2", "Juno Reactor", "Trickyofficial", "Laufey",
    "Marc Rebillet - Topic", "Justin Johnson",
    # clearly-music subscriptions added alongside Jordan's list
    "DJ Studio | Bass Portal & DnB Portal", "Didi Early Rave", "TRACKLIB", "Mixtape Magic", "Pearl Acoustics",
    "FeelTheSound", "Synthet", "DJRapOfficial", "BASSKILLAH", "KROQ", "Current Joys", "EYEDRESS", "Jonathan Bree",
    "Tubeway Army - Topic", "RŮDE - Topic", "Sleepy Fish - Topic", "Nvrsoft - Topic",
    # Jordan 2026-10-19 (second pass)
    "djvlad", "Key of Geebz", "Goddess Music YT", "Airplay Beats", "JAEL", "Singapore Community Radio",
    "The Streets", "Wax Lounge", "Letters Live",
)
MILITARY_CHANNELS = (
    "Forgotten Weapons", "LazerPig", "Task & Purpose", "Ward Carroll", "Military Aviation History",
    "The Military Show", "Cold War Impact", "The Great War", "WW2 Stories", "VASAviation -",
    "American Veterans Center", "U.S. Navy", "Combat Veteran News", "Airforceproud95",
    # Jordan 2026-10-19 (second pass)
    "Ukraine News NowUA", "RFU News — Strategic Geopolitics", "Professor Gerdes Explains 🇺🇦", "Shawn Ryan Show",
)
SCREEN_CHANNELS = (
    "RedLetterMedia", "CinemaSins", "Screen Junkies", "ScreenCrush", "How It Should Have Ended",
    "How Did This Get Made?", "JoBlo Horror Originals", "The A.V. Club", "ExtendedCutHD", "Defunctland",
    "Bright Sun Films", "WatchMojo.com", "Multiverse Showdown",
    # late night and comedy
    "Jimmy Kimmel Live", "Late Night with Seth Meyers", "Late Night With Conan O'Brien", "LastWeekTonight",
    "The Daily Show", "The Problem With Jon Stewart", "The Weekly Show with Jon Stewart", "Trevor Noah",
    "Patton Oswalt", "Kevin Smith",
    # Jordan 2026-10-19 (second pass): comedy, TV, Star Wars/Marvel, reactions, sports-comedy
    "The Onion", "Tim Heidecker", "Neal Brennan", "Brad Williams", "Cliff Cash Comedy", "Binge Burr",
    "The West Wing", "Hulu", "Stickman Anakin", "R2-D2 SPEAKS! The REAL hero of STAR WARS", "Marvelous Videos",
    "Marvelous Anime", "AV Pop Culture", "Rob Squad Reactions", "POLO REACTS", "Stay Tuned",
    "Trash Taste Highlights", "Jomboy Media",
)
SCIENCE_CHANNELS = (   # Veritasium, Real Engineering and Mark Rober stay in TECH_CHANNELS
    "PBS Space Time", "PBS Eons", "PBS Terra", "SciShow", "SciShow Space", "MinuteEarth", "minutephysics",
    "CrashCourse", "Curious Droid", "Astro Kirsten", "Science Unbound", "The Overview Effekt",
    "T. Folse Nuclear", "YaleCourses",
)
MYSTERY_CHANNELS = (
    "Dark5", "Dark5 Ancient Mysteries", "Dark Docs", "Dark Files", "Dark Seas", "Dark Skies", "Dark Space",
    "Wendigoon", "Lore Documentaries", "Decoding the Unknown", "ESOTERICA", "Library of Thoth", "Miniminuteman",
    "Uncovering", "Unhinged Past", "Hollow Century", "The Lore Lodge", "Brain Blaze",
)
CRIME_CHANNELS = (
    'Salvatore "Sammy The Bull" Gravano', "The Gangland History Podcast", "Rene Enriquez Uncensored",
    "Matthew Cox | Inside True Crime", "John Kiriakou", "Jason Hanson", "Kitboga", "Scammer Payback",
    "PolicecamStory", "Street Files", "The Casual Criminalist", "JD Delay", "LegalEagle", "Insider", "Rap News & Street Politics",
)
WORKSHOP_CHANNELS = (
    "This Old House", "Blacktail Studio", "Foureyes Furniture", "The Old Furniture Guys", "Jesper Makes",
    "Jack Builds It", "B is for Build", "Build It Yourself", "Remove It Pros Demolition", "Bertha's House Hacks",
    "A R Pond Maintenance And Supplies.", "Mountain Aquatics",
)
TRAVEL_CHANNELS = (
    "Peter Santenello", "Miha's Life in Japan", "Ghost Town Living", "LTV Squad Urban Exploring: Train Decay",
    "American Hobo - Train Riding", "Hobo Shoestring", "Koda Bear the Explorer", "Away Together w/ Nik and Allie",
    "Smarty Van", "Sidetrack Adventures", "Deep Sea Explorer YT",
)
HISTORY_CHANNELS = (   # Jordan 2026-10-19. Military history stays in MILITARY_CHANNELS.
    "OverSimplified", "Biographics", "Geographics", "History Buffs", "History Comes to Life",
    "History of Everything", "AlternateHistoryHub", "RealLifeLore", "Megaprojects", "Sealed History",
    "Vidstorical Films", "Well There's Your Problem Podcast", "Who Do You Think You Are?",
)
# Belief and Friends reads its channels from PG (nova_ops.belief_sources, belief_families — Jordan 2026-10-19:
# all state in PG; how-to in agent_docs 'belief-sources'). One row per channel: tradition, rotation family,
# role, fetch_mode (subscription / direct = ingested by channel id without a subscription, nova_yt_direct),
# owned_by_section + topic_only (religion-topic videos only), nrm (capped per issue). Loaded once per run.
NRM_MAX = 8   # new religious movements and sects: at most this many videos an issue
# PG unreachable: a run still happens from these broad scholarly channels, and says so loudly (ERROR + alert).
BELIEF_FALLBACK = ("ReligionForBreakfast", "Let's Talk Religion", "Harvard Divinity School", "Dan McClellan")
_BELIEF = None


def belief_sources(oc=None):
    """{channels, shared, capped, family_of, families, rows} from belief_sources / belief_families; cached per
    run. On a PG failure: the BELIEF_FALLBACK channels, one family, and a loud ERROR — never silently empty."""
    global _BELIEF
    if _BELIEF is not None:
        return _BELIEF
    try:
        cur = oc or _connect(OPS_DSN, attempts=2).cursor()
        cur.execute("SELECT name, ord, scripture_sources FROM belief_families ORDER BY ord")
        families = [(n, tuple(srcs or ())) for n, _, srcs in cur.fetchall()]
        cur.execute("""SELECT name, tradition, family, role, fetch_mode, owned_by_section, topic_only, nrm
                         FROM belief_sources WHERE active ORDER BY name""")
        rows = [dict(zip(("name", "tradition", "family", "role", "fetch_mode", "owned_by_section", "topic_only",
                          "nrm"), r)) for r in cur.fetchall()]
        if not rows or not families:
            raise RuntimeError("belief_sources / belief_families empty")
    except Exception as e:  # noqa: BLE001
        log(f"ERROR belief_sources unreadable ({e}) — FALLBACK to {len(BELIEF_FALLBACK)} scholarly channels")
        nj.log(f"[friends] ALERT Belief and Friends running on its code fallback: {e}")
        families = [("All traditions", tuple(SCRIPTURE_SOURCES))]
        rows = [{"name": n, "tradition": "Religious studies", "family": families[0][0], "role": "scholar",
                 "fetch_mode": "direct", "owned_by_section": None, "topic_only": False, "nrm": False}
                for n in BELIEF_FALLBACK]
    shared = tuple(r["name"] for r in rows if r["topic_only"] or r["owned_by_section"])
    _BELIEF = {"channels": tuple(r["name"] for r in rows if r["name"] not in shared), "shared": shared,
               "capped": (tuple(r["name"] for r in rows if r["nrm"]), NRM_MAX),
               "family_of": {norm(r["name"]): r["family"] for r in rows},
               "tradition_of": {norm(r["name"]): r["tradition"] for r in rows}, "families": families, "rows": rows}
    return _BELIEF


def apply_belief_sources(cfg, oc=None):
    """Point the belief subject at the table's channels (main() calls this for --subject belief)."""
    b = belief_sources(oc)
    cfg.update(channels=b["channels"], shared_channels=b["shared"], capped_channels=b["capped"],
               family_of=b["family_of"])
    return cfg


# ── Theme days (Jordan 2026-10-19: "an overall story ... with supporting and disagreeing viewpoints of the various
# religions. Not all the time but about half the time. Pick a random topic and go from there.") ───────────────
# About half the days (a coin per date, never three of a kind in a row) Belief is ONE essay on a Big Question from
# nova_ops.belief_themes, built from material across every family and any date, instead of the day's roundup.
THEME_EPOCH = "2026-10-01"          # the streak rule is replayed from here, so a date's answer never changes
THEME_REPEAT_DAYS = 30              # a theme is not reused within this many days
THEME_LOOKBACK_DAYS = 365           # theme material may come from any video ingested in the last year
THEME_MIN_TRADITIONS = 3            # fewer traditions with material -> roundup that day (logged)
THEME_PASSAGES = 3
THEME_MIN_FAMILIES = 4              # the picker prefers a theme whose material spans at least this many families
THEME_MIN_HITS = 2                  # a video counts for a theme only if it hits this many distinct keywords
GENERIC_TRADITIONS = ("Religious studies", "Religious history", "Religious demographics", "American religious history")


def _theme_coin(day):
    import hashlib
    return int(hashlib.sha256(f"belief-theme:{day}".encode()).hexdigest(), 16) % 2 == 1


def is_theme_day(day):
    """True on theme days: a coin per date, flipped whenever it would make a third theme (or roundup) day in a
    row. Deterministic — replayed from THEME_EPOCH."""
    from datetime import date, timedelta
    d, cur = date.fromisoformat(day[:10]), date.fromisoformat(THEME_EPOCH)
    if d < cur:
        return _theme_coin(d.isoformat())
    hist = []
    while cur <= d:
        v = _theme_coin(cur.isoformat())
        if len(hist) >= 2 and hist[-1] == hist[-2] == v:
            v = not v
        hist.append(v)
        cur += timedelta(days=1)
    return hist[-1]


def theme_candidates(oc, today):
    """Active themes in pick order: the least-used half of those not used in THEME_REPEAT_DAYS, shuffled by date,
    then the rest of the fresh ones, then the recently used ones oldest first."""
    import random
    from datetime import date, timedelta
    oc.execute("""SELECT theme, question, keywords, last_used, times_used FROM belief_themes WHERE active
                  ORDER BY last_used NULLS FIRST, times_used, theme""")
    rows = [dict(zip(("theme", "question", "keywords", "last_used", "times_used"), r)) for r in oc.fetchall()]
    cutoff = date.fromisoformat(today[:10]) - timedelta(days=THEME_REPEAT_DAYS)
    fresh = [r for r in rows if r["last_used"] is None or r["last_used"] <= cutoff]
    k = max(5, len(fresh) // 2)
    pool, rest = fresh[:k], fresh[k:]
    random.Random(f"belief-theme-pick:{today}").shuffle(pool)
    return pool + rest + [r for r in rows if r not in fresh]


def pick_theme(oc, today, want=None):
    """{theme, question, keywords}: `want` if given, else random (seeded by date) among the least-used half of the
    active themes not used in THEME_REPEAT_DAYS; if all were used recently, the one used longest ago."""
    import random
    oc.execute("""SELECT theme, question, keywords, last_used, times_used FROM belief_themes WHERE active
                  ORDER BY last_used NULLS FIRST, times_used, theme""")
    rows = [dict(zip(("theme", "question", "keywords", "last_used", "times_used"), r)) for r in oc.fetchall()]
    if want:
        return next((r for r in rows if r["theme"] == want), None)
    from datetime import date, timedelta
    cutoff = date.fromisoformat(today[:10]) - timedelta(days=THEME_REPEAT_DAYS)
    fresh = [r for r in rows if r["last_used"] is None or r["last_used"] <= cutoff]
    if not fresh:
        return rows[0] if rows else None
    pool = fresh[:max(5, len(fresh) // 2)]   # least-recently-used half
    return random.Random(f"belief-theme-pick:{today}").choice(pool)


def choose_theme(oc, mc, cfg, today, want=None, tradition_of=None):
    """(theme, videos, traditions, families) for today's theme day, or (None, ...) when no theme has material from
    THEME_MIN_TRADITIONS traditions. Walks theme_candidates() and takes the first whose material spans
    THEME_MIN_FAMILIES families; otherwise the candidate with the most families (first edition 2026-10-19: a blind
    random pick landed on a theme with 2 families and off-topic videos)."""
    tradition_of = tradition_of or belief_sources()["tradition_of"]
    cands = [pick_theme(oc, today, want)] if want else theme_candidates(oc, today)
    best = (None, [], [], [])
    for t in cands[:12]:
        if not t:
            continue
        vids = theme_material(mc, cfg, t["keywords"])
        trads = traditions_in(vids, tradition_of)
        fams = sorted({cfg.get("family_of", {}).get(norm(v["channel"] or ""), "?") for v in vids})
        if len(trads) >= THEME_MIN_TRADITIONS and len(fams) >= THEME_MIN_FAMILIES:
            return t, vids, trads, fams
        if len(trads) >= THEME_MIN_TRADITIONS and len(fams) > len(best[3]):
            best = (t, vids, trads, fams)
        if want:
            return (t, vids, trads, fams) if len(trads) >= THEME_MIN_TRADITIONS else (None, vids, trads, fams)
    return best


def mark_theme_used(oc, theme, today):
    oc.execute("UPDATE belief_themes SET last_used = %s, times_used = times_used + 1 WHERE theme = %s",
               (today[:10], theme))


def theme_regex(keywords):
    """Postgres ARE whole-word alternation for a theme's keywords."""
    return r"\m(" + "|".join(re.escape(k) for k in keywords) + r")\M"


def theme_material(cur, cfg, keywords, days=THEME_LOOKBACK_DAYS):
    """Belief videos from any date whose transcript chunks mention the theme, best-matching first, dealt
    round-robin by family and NRM-capped like a roundup. Only the matching chunks are kept as each video's text."""
    shared = {norm(c) for c in cfg.get("shared_channels", ())}
    keys = sorted({norm(c) for c in cfg["channels"]} | shared)
    cur.execute(f"""/* theme_material */ SELECT text, created_at, metadata FROM memories
                    WHERE created_at > now() - make_interval(days => %s)
                      AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                      AND metadata->>'pipeline' = 'yt_subs_audio' AND {NORM_SQL} = ANY(%s) AND text ~* %s
                    ORDER BY created_at DESC""", (days, keys, theme_regex(keywords)))
    rx = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")\b", re.I)
    vrows = []
    for text, created, md in cur.fetchall():
        md = dict(md or {})
        if BANNED.search(text or "") or BANNED.search(md.get("title") or ""):
            continue
        md["channel"] = md.get("channel") or md.get("show")
        vrows.append((text, created, md))
    videos = group_videos(vrows, VID_PER, len(vrows))
    videos = [v for v in videos if norm(v["channel"] or "") not in shared
              or is_belief_topic(f"{v['title']} {v.get('text', '')[:3000]}")]
    for v in videos:
        v["_hits"] = len({m.group(0).lower() for m in rx.finditer(f"{v['title']} {v.get('text', '')}")})
    videos = sorted((v for v in videos if v["_hits"] >= THEME_MIN_HITS), key=lambda v: v["_hits"], reverse=True)
    capped, cap = cfg.get("capped_channels", ((), 0))
    capped, kept, n = {norm(c) for c in capped}, [], 0
    for v in videos:
        if norm(v["channel"] or "") in capped:
            if n >= cap:
                continue
            n += 1
        kept.append(v)
    videos = round_robin(kept, cfg.get("family_of", {}))[:VID_MAX] if cfg.get("family_of") else kept[:VID_MAX]
    for v in videos:
        v.pop("_hits", None)
    return videos


def traditions_in(videos, tradition_of):
    """Base traditions ('Islam', not 'Islam (Shia)') with at least one video; generic scholarly buckets
    (religious studies / history / demographics) are sources, not traditions, and do not count."""
    return sorted({tradition_of[norm(v["channel"] or "")].split(" (")[0] for v in videos
                   if norm(v["channel"] or "") in tradition_of} - set(GENERIC_TRADITIONS))


def theme_context(name, question, week, words_, cap, rules):
    """The theme-day writing brief: one narrative on the question, agreement and disagreement (between and
    inside traditions), the skeptic's view, a synthesis that never rules on which is true."""
    return (f"You are writing '{name}', Nova's THEME-DAY issue for {week} — THE BIG QUESTION: '{question}'. This is "
            "NOT a roundup of the day's videos: it is ONE narrative essay on that question, built from the transcripts "
            "below (many traditions, many dates). Structure it with ## headers, in this order: "
            "1) The question — why people have asked it, in a short opening. "
            "2) How the traditions answer — only where the material speaks; name the channel and the video for every "
            "claim. "
            "3) Where they AGREE — the real common ground, said explicitly. "
            "4) Where they DISAGREE — between traditions AND inside them (Sunni and Shia, Orthodox and Reform "
            "Judaism, Theravada and Mahayana, Catholic and Protestant, Latter-day Saints and Community of Christ...) "
            "whenever the material shows it. "
            "5) The skeptic's and non-theist view, from the material. "
            "6) Nova's closing synthesis — what the comparison reveals about the question; NO verdict on which "
            f"tradition is true and no ranking of doctrines. Give real depth: {words_} words (under {cap}). {rules}\n"
            "First line exactly: TITLE: <anything — it is replaced>")


THEME_TOP_TASK = (" TODAY IS A THEME DAY on the question '{question}': choose TWO passages from DIFFERENT traditions "
                  "in the library that answer it differently (one may be a non-theist philosopher). Quote each EXACTLY "
                  "as its own markdown blockquote, each followed on its own line by its citation; then put both in "
                  "context and say plainly where they agree and where they part. If only one passage fits, use one.")


def belief_coverage(oc, mc, days=7):
    """One log line per gap: per base tradition, the scholar/insider/critic slots with no active channel
    (belief_coverage view) and the traditions with no video ingested in `days`. -> [str]."""
    oc.execute("SELECT tradition, missing_roles FROM belief_coverage ORDER BY tradition")
    cov = oc.fetchall()
    oc.execute("SELECT split_part(tradition, ' (', 1), name FROM belief_sources WHERE active")
    by_trad = {}
    for t, n in oc.fetchall():
        by_trad.setdefault(t, []).append(norm(n))
    mc.execute(f"""SELECT DISTINCT {NORM_SQL}, metadata->>'video_id' FROM memories
                   WHERE created_at > now() - make_interval(days => %s) AND metadata->>'pipeline' = 'yt_subs_audio'
                     AND {NORM_SQL} = ANY(%s)""", (days, sorted({n for ns in by_trad.values() for n in ns})))
    vids = {}
    for ch, _ in mc.fetchall():
        vids[ch] = vids.get(ch, 0) + 1
    out = []
    for t, missing in cov:
        n = sum(vids.get(c, 0) for c in by_trad.get(t, []))
        bits = ([f"{'/'.join(missing)} slot empty"] if missing else []) + ([f"no material in {days} days"] if not n
                                                                             else [f"{n} videos in {days} days"])
        out.append(f"coverage {t}: " + "; ".join(bits))
    return out


# Jordan 2026-10-19: style and health stay unassigned — in NO subject and no pool (a test enforces it).
UNASSIGNED_CHANNELS = ("GQ", "Real Men Real Style", "Brick and Sole", "Freshhkiicks", "Inkbox", "Doctor Mike",
                       "Dr.Sanders", "Jesse Enkamp")
# Editorial ground truth (Jordan 2026-10-19), injected like Watches' KNOWN_FACTS: always for Military, and
# for Screen/Travel only when the day's material touches the war (STANCE_TOPICS).
MILITARY_STANCE = (
    "EDITORIAL STANCE (ground truth for this section): Nova is strongly pro-Ukraine and pro-NATO, and opposed "
    "to the Russian state and its invasion. Russia's full-scale invasion of Ukraine is unprovoked aggression; "
    "Ukraine is the defender. Treat NATO and the alliance positively. Never repeat Kremlin talking points as "
    "'balance' — if a source carries Russian propaganda, call it propaganda. This stance never licenses "
    "invention: no battlefield facts, casualty numbers or quotes that are not in the material. Aim criticism at "
    "the Russian government, its military leadership and the invasion — never at Russian people as an "
    "ethnicity, and no dehumanising language.")
STANCE_TOPICS = re.compile(r"\b(?:ukrain\w*|russia\w*|kremlin|putin|nato|kyiv|moscow|zelensk\w*|donbas\w*|crimea\w*)\b", re.I)


def stance_for(subj, text):
    """MILITARY_STANCE for Military always; for Screen/Travel/History only if the material touches the war."""
    if subj == "military" or (subj in ("screen", "travel", "history") and STANCE_TOPICS.search(text or "")):
        return MILITARY_STANCE
    return ""


THIN_MIN, WIDEN_DAYS = 2, 3     # new subjects: a daily run with < THIN_MIN videos looks back WIDEN_DAYS

# Military top block: "This Day in Military History" from Nova's history memories (Wikipedia-backed,
# so every pick links its source page) plus the military_doctrine* vectors (rarely dated battles, but in scope).
HISTORY_SOURCES = ("military_history", "world_history")
HISTORY_MAX, HISTORY_CHARS = 6, 900
MILITARY_WORDS = re.compile(r"\b(?:war|battle|army|armies|navy|naval|fleet|troops|soldiers|regiment|division|"
                            r"invasion|invaded|siege|offensive|campaign|surrender|air force|squadron|bomb\w*|"
                            r"marines?|artillery|fighter|warship|submarine|military|armistice|front)\b", re.I)

CITATION_RE = re.compile(r"\b(?:JP|AFI|AFMAN|AR|FM|ATP|MCRP|MCWP|DoD[DI]?|CNGBI|OPNAVINST|SECNAVINST)\s?\d|"
                         r"Directive|Instruction|Memorandum|Memo\b|Publication|Change \d|supersedes", re.I)

# History top block: "This Day in History" from the general history library (any kind of event; a non-military
# one is preferred — military history has its own section).
GENERAL_HISTORY_SOURCES = ("world_history", "history")

# Belief top block: "Text in Context" from Nova's scripture library + the non-theism philosophers (Lucretius,
# Hume, d'Holbach, Russell...); every passage carries its citation header.
SCRIPTURE_SOURCES = ("bible", "quran", "buddhism", "hinduism", "sikhism", "taoism", "shinto", "confucianism",
                     "jainism", "zoroastrianism", "egyptian_book_of_the_dead", "books_of_the_dead", "nontheism")
SCRIPTURE_MAX, SCRIPTURE_CHARS, SCRIPTURE_PER_SOURCE = 5, 1500, 2
# Balance (Jordan 2026-10-19): the day's videos skew Christian (most religious channels he follows are Catholic),
# and the picker ranks by word overlap with them — so Text in Context rotates a focus family by date
# (belief_families: Abrahamic, Dharmic, East Asian, Non-theist and ancient, New religious movements and sects).
# The focus family's passages are offered first (FAMILY_MIN of them, word-matched if possible) and the task
# says to prefer one; a family with no library texts of its own (new movements) may use any passage, read
# through how a movement uses it.
FAMILY_MIN = 2


def focus_family(today=None):
    """(name, sources) of the tradition family Text in Context leans on today; rotates daily by date."""
    from datetime import date
    d = date.fromisoformat((today or nj.today_str())[:10])
    families = belief_sources()["families"]
    return families[d.toordinal() % len(families)]
SCRIPTURE_STOP = {"church", "catholic", "christian", "podcast", "episode", "father", "people", "really", "things",
                  "should", "because", "something", "actually", "question", "answer", "explained", "anything",
                  "everything", "nothing", "little", "always", "believe", "pretty", "having", "talking", "before",
                  "another", "around", "through", "better", "without", "though", "between", "during", "within"}
BELIEF_TONE = ("Stance: a historian and anthropologist of religion, not a believer and not a debunker. Cover what "
               "people believe, where those beliefs came from, how the texts were written and handed down, and "
               "where traditions — and unbelief — disagree. The religious channels are PRIMARY SOURCES about "
               "believers, never authorities: report them in their own terms ('Catholics believe...', 'the Gita "
               "teaches...', 'Barron argues...'). Keep it balanced — believers, scholars, skeptics and the "
               "theological counter-arguments to skeptics all get a fair hearing, and so do the world's "
               "traditions — Abrahamic (Jewish, Christian, Muslim: Sunni, Shia, Sufi), Dharmic (Hindu, Buddhist, "
               "Sikh, Jain), East Asian (Taoist, Confucian, Shinto) and non-religious; when the material is mostly "
               "one faith, say so rather than writing as if it were the whole picture. New religious movements and "
               "sects are described the same way — what they teach, where they came from, how many follow them — "
               "and documented abuses, crimes or court findings are reported factually with their source, never "
               "as a verdict on the doctrine. Never call any group's doctrine 'wrong', 'false' or 'heretical' in "
               "Nova's own voice, and never promote, recruit for or link joining information for any group; "
               "extremist or hate movements are covered only through scholars, journalists and former members. "
               "Respectful and curious; never "
               "preachy, never proselytising, never ranking religions, never mocking believers or unbelievers. "
               "Occult and paranormal belong to Mystery, not here. Quote scripture only word-for-word from the "
               "material with its exact citation; never invent, paraphrase-as-quote or misattribute a verse.")

# Mystery top block may lean on the esoteric library for context. Labels travel with every excerpt.
LORE_SOURCES = ("occult", "necronomicon", "books_of_the_dead", "shams_al_maarif")
LORE_MAX, LORE_CHARS = 3, 1200
LORE_STOP = {"with", "this", "that", "from", "they", "were", "what", "have", "there", "their", "about", "which",
             "into", "than", "then", "when", "most", "more", "story", "video", "history", "mystery", "mysteries",
             "dark", "part"}
# A street address never belongs in a public crime write-up (victims' addresses above all).
ADDRESS_RE = re.compile(r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][\w'.-]*\s+){1,4}(?:Street|St|Avenue|Ave|Road|Rd|"
                        r"Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way|Place|Pl|Terrace|Circle|Parkway|Pkwy|Highway|Hwy)\b\.?")
RECIPE_SITES = {"meatchurch.com": "Meat Church BBQ"}   # recipe-memory source_site -> credit name
RECIPES_MAX, RECIPE_CHARS = 4, 4000
RECIPE_STOP = {"with", "this", "that", "from", "your", "recipe", "style", "best", "easy", "make", "homemade"}

# The publicly known home stack (it is on the site's About page) — the ONLY stack detail a Buying
# Recommendation may use. Never hostnames, IPs, camera/room names or anything about Jordan's employer.
HOME_STACK = ("a Mac Studio plus Mac minis; Linux mini-PCs (Beelink); UniFi networking and a UNAS; a Synology "
              "NAS; a HomeKit / Hue / Lutron smart home; Meshtastic LoRa radios; SDRplay and RTL-SDR software "
              "radios; Bambu 3D printers; Ubiquiti cameras")

# Space/astronomy feeds share the computing vector; they are not tech news. Forum chatter neither.
TECH_FEED_EXCLUDE = re.compile(r"astro|nasa|space|earthsky|hirise|galaxy|sky|planetary|universe|eso\b|nrao|"
                               r"nautilus|aura|community", re.I)

SUBJECTS = {
    "cars": {
        "name": "Cars and Friends", "section": "cars", "noun": "car", "emoji": "🚗",
        "channels": CARS_CHANNELS, "article_vectors": (), "feed_exclude": None,
        "beats": "new metal and reveals, builds and project cars, racing and the drag strip, auctions and "
                 "the market, parts, tuning and tools, car audio, motorcycles, RC cars, the industry",
        "audience": "car people", "image_topic": "classic, muscle and performance cars, garages and race tracks",
        "tags": ["cars", "automotive", "car-news"],
        "desc": "Nova's {period} car roundup: the {span}'s car news from Jordan's car channels — with every source linked.",
    },
    "tech": {
        "name": "Tech and Friends", "section": "tech", "noun": "tech", "emoji": "💻",
        "channels": TECH_CHANNELS,
        "article_vectors": ("computing", "tech_blog", "technology_general", "home_automation"),
        "feed_exclude": TECH_FEED_EXCLUDE,
        "beats": "AI, Apple and the platforms, hardware and PCs, homelab, networking and self-hosting, "
                 "security, right to repair, 3D printing and making, home automation, retro computing, hi-fi, "
                 "engineering and science, the industry",
        "audience": "tech people", "image_topic": "computers, circuit boards, workbenches and glowing screens",
        "tags": ["tech", "technology", "tech-news"],
        "desc": "Nova's {period} tech roundup: the {span}'s tech news from Jordan's tech channels and feeds — with every source linked.",
    },
    "food": {
        "name": "Food and Friends", "section": "food", "noun": "food", "emoji": "🍳",
        "channels": FOOD_CHANNELS, "article_vectors": (), "feed_exclude": None,
        "beats": "barbecue and live fire, recipes and technique, chefs and restaurants, food travel and culture, "
                 "food history, and drinks — whiskey, distilling, gin, brewing and bars",
        "audience": "home cooks and pitmasters",
        "image_topic": "barbecue smokers, kitchens, plated dishes and cooking fires",
        "tags": ["food", "cooking", "food-news"],
        "desc": "Nova's {period} food roundup: the {span}'s food news from Jordan's food channels — with every source linked.",
    },
}


def _subject(slug, name, emoji, channels, beats, audience, image_topic, tags, noun, extra_rules=""):
    """The eight 2026-10-19 sections: video channels only, thin-day widening, optional extra rules."""
    return {"name": name, "section": slug, "noun": noun, "emoji": emoji, "channels": channels,
            "article_vectors": (), "feed_exclude": None, "beats": beats, "audience": audience,
            "image_topic": image_topic, "tags": tags, "widen": True, "extra_rules": extra_rules,
            "desc": f"Nova's {{period}} {noun} roundup: the {{span}}'s {noun} stories from Jordan's {noun} "
                    "channels — with every source linked."}


SUBJECTS.update({
    "music": _subject("music", "Music and Friends", "🎧", MUSIC_CHANNELS,
                      "music theory and the craft, records, artists and music history, drum & bass and the club "
                      "world, DJ culture and sets, live sessions, gear and studio, the industry",
                      "listeners, players and record collectors",
                      "turntables, vinyl records, studio mixing desks and concert stage lights",
                      ["music", "music-news"], "music"),
    "military": _subject("military", "Military and Friends", "🎖️", MILITARY_CHANNELS,
                         "weapons and their history, military aviation, the navies, current defence news and "
                         "analysis, the world wars and the Cold War, veterans' stories",
                         "military history buffs and veterans",
                         "historic warplanes, warships, museum armour and archival battlefield maps",
                         ["military", "military-history", "defense"], "military",
                         "Military subjects are reported as history and analysis: no operational how-to, no "
                         "glorification of atrocities, and never anything about Jordan's own work."),
    "screen": _subject("screen", "Screen and Friends", "🍿", SCREEN_CHANNELS,
                       "film and TV reviews and criticism, trailers and new releases, retrospectives and behind "
                       "the scenes, theme parks and entertainment history as covered, late night and comedy",
                       "film, TV and comedy fans",
                       "a vintage cinema marquee, film reels, a late-night talk show desk and stage lights",
                       ["screen", "film", "tv", "comedy"], "screen",
                       "Use ONLY the channels' material — never anything Jordan or his household has watched, "
                       "never a Plex library or viewing history."),
    "science": _subject("science", "Science and Friends", "🔭", SCIENCE_CHANNELS,
                        "space and cosmology, physics, deep time, evolution and palaeontology, the planet and "
                        "climate, nuclear power, spaceflight, science lectures",
                        "curious people", "nebulae, telescopes, fossils, laboratory glassware and planets",
                        ["science", "space", "science-news"], "science"),
    "mystery": _subject("mystery", "Mystery and Friends", "🕯️", MYSTERY_CHANNELS,
                        "unsolved mysteries, strange history and lost places, folklore and legends, the deep sea "
                        "and space oddities, esoteric and religious history, internet lore",
                        "people who love a good rabbit hole",
                        "candlelit old libraries, foggy ruins, ancient maps and a dark starry sky",
                        ["mystery", "folklore", "history"], "mystery",
                        "Separate what is known from what is claimed. The Necronomicon is H. P. Lovecraft's "
                        "fiction; grimoires, magical and funerary texts are historical-esoteric literature, "
                        "described as history, never as working instructions or rites."),
    "crime": _subject("crime", "Crime and Friends", "🔦", CRIME_CHANNELS,
                      "organised crime and mob history, scams and the people who bust them, intelligence and "
                      "security tradecraft as told publicly, police and court cases, crime history",
                      "true-crime fans",
                      "a noir city street at night, a detective's desk, case files and a single spotlight",
                      ["crime", "true-crime", "scams"], "crime",
                      "Name only people the channels themselves publicly cover as figures in a case (defendants, "
                      "officials, the hosts); never private bystanders, never victims' addresses or personal "
                      "details, never a how-to for committing a crime or a scam."),
    "workshop": _subject("workshop", "Workshop and Friends", "🔨", WORKSHOP_CHANNELS,
                         "woodworking and furniture, restoration and refinishing, home building and renovation, "
                         "demolition, DIY tricks, ponds and aquatics",
                         "makers and DIYers", "a woodworking shop bench, hand tools, sawdust and a half-built table",
                         ["workshop", "diy", "woodworking"], "workshop"),
    "travel": _subject("travel", "Travel and Friends", "🗺️", TRAVEL_CHANNELS,
                       "places and the people in them, small-town America, Japan, ghost towns and urban "
                       "exploration, trains and the open road, van life and road trips, the deep sea",
                       "travellers and armchair explorers",
                       "an open road, an old train yard, a ghost town and a sweeping map",
                       ["travel", "exploration"], "travel",
                       "Never invent prices, opening hours, directions or travel logistics."),
    "history": _subject("history", "History and Friends", "🏛️", HISTORY_CHANNELS,
                        "world history and its turning points, biographies, geography and how places got that "
                        "way, alternate history, megaprojects and engineering disasters, lost and sealed "
                        "history, family history",
                        "history buffs", "an old atlas, marble columns, archival photographs and a museum hall",
                        ["history", "world-history"], "history",
                        "Separate what is documented from legend, speculation and alternate-history 'what ifs' — "
                        "label alternate history as hypothetical. Never invent dates, numbers or quotes."),
    "belief": _subject("belief", "Belief and Friends", "🔍", (),   # channels from belief_sources (main())
                       "what people believe and why, where traditions came from, how scriptures were written and "
                       "transmitted, religion in the news and in society, atheism and non-religion, apologetics "
                       "and the arguments against it, the scholars' view",
                       "the curious — believers, skeptics and everyone in between",
                       "old illuminated manuscripts, museum reading rooms, worn scrolls and a map of the world's "
                       "traditions", ["belief", "religion"], "religion", BELIEF_TONE),
})

# The featured block at the top of each issue: (heading, task). Grounded, fail closed (featured_block).
FEATURES = {
    "cars": ("The Big Story",
             "Pick the single most interesting NEW car story in the material (a reveal, a build milestone, a race "
             "result, an auction, an industry move) and write it up: what happened, why it matters, who covered it."),
    "tech": ("Buying Recommendation",
             "Recommend ONE device worth buying, chosen from products actually covered in the material, that fits "
             f"Nova's publicly known home stack: {HOME_STACK}. Name the device, say what the source said about it, "
             "why it fits that stack and what it would replace or add. Talk about the stack only in those general "
             "terms — never hostnames, IP addresses, camera or room names, device inventories, or anything about "
             "Jordan's employer or work. Never state a price, spec or availability the material does not give. If "
             "no device in the material genuinely fits, reply NONE."),
    "food": ("Recipe Recommendation",
             "Recommend ONE recipe Nova would cook, taken from the day's food videos or the RECIPE PAGES in the "
             "material and chosen by the HOUSE RULES below: the dish name, a line on why, then a short "
             "**Ingredients** bullet list and a short **Method** (numbered). Amounts, times and temperatures come "
             "ONLY from the source — never invent them; where the house rules call for a change, mark it plainly as "
             "a swap. Credit and link the channel/video or recipe page it comes from."),
    "music": ("🎧 Track of the Day",
              "Pick ONE track or performance from the material (a song, a set, a live session, a record "
              "someone played or broke down) and say who made it, what it is, and why it is worth hearing today, "
              "in the source's terms. Link the video it comes from. Never invent release dates, chart facts or "
              "quotes."),
    "military": ("📜 This Day in Military History",
                 "The THIS DAY IN HISTORY notes in the material come from Nova's military and world history "
                 "library for {today}. Pick the most interesting MILITARY event that a note states happened on "
                 "{today} in a past year: what happened, the year, why it mattered, linked to that note's page. If "
                 "one of the day's videos connects to it, say so and link it too. If no note gives a military event "
                 "on this date, write up the day's single most interesting military story from the videos instead "
                 "and say plainly that it is today's story. Never invent dates, numbers, units or quotes."),
    "screen": ("🍿 What to Watch Tonight",
               "Recommend ONE film, show, special or episode to watch tonight, chosen ONLY from titles the "
               "material itself reviews, recommends or discusses: name it, what the source said about it, and why "
               "tonight. Base it on the channels' material alone — never on anything Jordan or his household has "
               "watched, never a Plex library or viewing history. Never invent streaming availability."),
    "science": ("🌌 The Coolest Thing I Learned",
                "The single most mind-bending fact, finding or idea in the material, explained plainly: what it is, "
                "why it is surprising, and who covered it. Never invent numbers or results."),
    "mystery": ("🕯️ The Rabbit Hole of the Day",
                "One mystery, legend, unexplained event or strange piece of history from the day's videos worth "
                "falling into: the hook, what is actually known versus what is claimed, and where to start (the "
                "linked video). ESOTERIC LIBRARY notes, if any, may add context but keep their labels: the "
                "Necronomicon is H. P. Lovecraft's fiction; grimoires, magical and funerary texts are "
                "historical-esoteric literature, described as history, never as instructions, spells or rites."),
    "crime": ("🔦 Case of the Day",
              "One case, scam or criminal story from the material: what happened, how it was caught or how the "
              "scam works so readers can spot it, and the lesson. Name only people the source itself publicly "
              "covers as figures in the case; never private bystanders, never a victim's address or personal "
              "details, never a how-to for committing it."),
    "workshop": ("🔨 Project Worth Stealing",
                 "One build, repair, restoration or technique from the material worth copying in your own shop or "
                 "home: what was made, the key trick, and the tools and materials ONLY as the source names them. "
                 "Never invent measurements or product names."),
    "history": ("🏛️ This Day in History",
                "The THIS DAY IN HISTORY notes in the material come from Nova's world-history library for "
                "{today}. Pick the most interesting event a note states happened on {today} in a past year — "
                "preferably not a battle (military history has its own section): what happened, the year, why it "
                "mattered, linked to that note's page. If one of the day's videos connects to it, say so and link "
                "it too. If no note gives an event on this date, write up the day's single most interesting "
                "history story from the videos instead and say plainly that it is today's story. Never invent "
                "dates, numbers, names or quotes."),
    "belief": ("📜 Text in Context",
               "Choose ONE passage from the SCRIPTURE LIBRARY in the material — any tradition, or one of the "
               "non-theist philosophers — that connects to something in the day's videos. Quote it EXACTLY, word "
               "for word, as a markdown blockquote (lines starting with '> '), copying only a contiguous run of "
               "words from that passage (you may drop the inline verse numbers); then on its own line the "
               "citation exactly as given in its CITE AS line, narrowed to the verse(s) you quoted when the "
               "passage numbers its verses. Then put it in context, as a historian would (120-250 words): who "
               "wrote it and when (as far as scholars know — say when authorship is traditional or disputed), "
               "what it meant to its first audience, how it is read today, and ONE line on how a skeptic or "
               "another tradition sees it. Link the video it connects to. This is study, not devotion: no "
               "sermon, no 'reflection', no telling the reader what to believe. Never invent, alter or "
               "paraphrase-as-quote scripture; never quote anything that is not in the library text; never "
               "invent dates or authors. If nothing in the library fits, reply NONE."),
    "travel": ("🗺️ Place of the Day",
               "One place from the material: where it is, what makes it worth knowing about, and what the creator "
               "found there. Never invent prices, opening hours, directions or logistics."),
}

# Public-journal fail-closed check. Inputs mentioning these are dropped; a draft that still
# contains one is NOT published (Watches-and-Friends rules: no employer/work, no OpenClaw,
# no IPs, no home paths).
BANNED = re.compile(r"\bdisney\b|openclaw|\b(?:\d{1,3}\.){3}\d{1,3}\b|/Users/|/Volumes/", re.I)


def log(m):
    nj.log(f"[friends] {m}")


def norm(name):
    """Channel-name key: curly apostrophes, case and runs of whitespace don't matter."""
    return re.sub(r"\s+", " ", (name or "").replace("’", "'")).strip().lower()


NORM_SQL = r"lower(btrim(regexp_replace(replace(coalesce(metadata->>'channel', metadata->>'show'), E'’', ''''), '\s+', ' ', 'g')))"


def recently_cited(section, days=2):
    """Video URLs the section's own posts from the last `days` already list — a video ingested late one day is
    still inside the next day's window, and the next issue must not retell it. Fast-forwards and reads the
    local Hugo repo (the one this run publishes from); unreadable = nothing to skip."""
    try:
        import subprocess
        import time as _t
        subprocess.run(["git", "-C", str(nj.HUGO_ROOT), "pull", "--ff-only", "-q"], capture_output=True,
                       timeout=30)   # another host may have published the last issue (publish rebases later anyway)
    except Exception:  # noqa: BLE001
        pass
    try:
        cutoff = _t.time() - days * 86400
        folder = Path(nj.HUGO_ROOT) / "content" / section
        posts = [p for p in folder.glob("*.md") if p.stat().st_mtime >= cutoff]
        return {u for p in posts for u in re.findall(r"https://www\.youtube\.com/watch\?v=[\w-]{11}", p.read_text())}
    except Exception:  # noqa: BLE001 — dedup is a nicety, never a reason to skip an issue
        return set()


def round_robin(videos, family_of):
    """Newest-first videos re-dealt one per family in turn (first edition 2026-10-19: newest-first let the last
    families ingested take every slot and dropped all the Buddhist, Hindu, Sikh and Catholic videos)."""
    lanes = {}
    for v in videos:
        lanes.setdefault(family_of.get(norm(v["channel"] or ""), ""), []).append(v)
    out, queues = [], [list(q) for q in lanes.values()]
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def fetch(cur, cfg, days):
    """-> (videos, articles) grouped newest-first, banned-term rows dropped, videos the section's last
    issues already cited dropped."""
    shared = {norm(c) for c in cfg.get("shared_channels", ())}   # another section's channel, topic videos only
    keys = sorted({norm(c) for c in cfg["channels"]} | shared)
    cur.execute(f"""SELECT text, created_at, metadata FROM memories
                    WHERE created_at > now() - make_interval(days => %s)
                      AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                      AND NOT metadata ?| array['supersedes', 'skipping_needle']   -- re-transcribed old video / loop junk
                      AND (metadata->>'pipeline' = 'yt_subs_audio'
                           OR metadata->>'source_file' LIKE '%%/videos/youtube/%%')
                      AND {NORM_SQL} = ANY(%s)
                    ORDER BY created_at DESC,
                             CASE WHEN metadata->>'chunk' ~ '^[0-9]+$' THEN (metadata->>'chunk')::int END NULLS FIRST""", (days, keys))
    vrows = []
    for text, created, md in cur.fetchall():
        md = dict(md or {})
        if BANNED.search(text or "") or BANNED.search(md.get("title") or ""):
            continue
        md["channel"] = md.get("channel") or md.get("show")
        if not md.get("video_id") and not md.get("url"):
            md["url"] = "local:" + (md.get("source_file") or md.get("title") or "")   # grouping key only
        vrows.append((text, created, md))
    videos = group_videos(vrows, VID_PER, len(vrows))
    cited = recently_cited(cfg["section"])   # before the cap: an already-told video never takes a slot
    if cited:
        videos = [v for v in videos if v["url"] not in cited]
    if shared:   # filter before the cap so off-topic shared videos never crowd out the section's own
        videos = [v for v in videos if norm(v["channel"] or "") not in shared
                  or is_belief_topic(f"{v['title']} {(v.get('text') or '')[:3000]}")]
    capped, cap = cfg.get("capped_channels", ((), 0))
    if capped:
        capped, kept, n_capped = {norm(c) for c in capped}, [], 0
        for v in videos:   # newest first: the newest NRM videos win the capped slots
            if norm(v["channel"] or "") in capped:
                if n_capped >= cap:
                    continue
                n_capped += 1
            kept.append(v)
        videos = kept
    if cfg.get("family_of") and len(videos) > VID_MAX:
        videos = round_robin(videos, cfg["family_of"])
    videos = videos[:VID_MAX]
    for v in videos:
        if (v["url"] or "").startswith("local:"):
            v["url"] = None

    arts = []
    if cfg["article_vectors"]:
        cur.execute("""SELECT text, created_at, metadata FROM memories
                       WHERE created_at > now() - make_interval(days => %s)
                         AND source = ANY(%s) AND metadata->>'type' IN ('gov_rss', 'blog_post')
                         AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                       ORDER BY created_at DESC""", (days, list(cfg["article_vectors"])))
        arows = []
        for text, created, md in cur.fetchall():
            md = dict(md or {})
            feed = md.get("feed") or md.get("site") or ""
            if cfg["feed_exclude"] and cfg["feed_exclude"].search(feed):
                continue
            if BANNED.search(text or "") or BANNED.search(md.get("title") or ""):
                continue
            md["channel"] = feed or "web"
            arows.append((text, created, md))
        arts = group_videos(arows, ART_PER, ART_MAX)
    return videos, arts


def load_food_rules():
    """The Recipe Recommendation house rules from nova_ops.service_config (food_and_friends /
    recipe_constraints): {"rules": str, "private_terms": [...], "prefer": [...], "avoid": [...]}.
    Private by design — never hardcoded here. None when unavailable (the block is then omitted)."""
    try:
        conn = _connect(OPS_DSN)
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM service_config WHERE service = %s AND key = %s",
                        ("food_and_friends", "recipe_constraints"))
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log(f"food house rules unavailable ({type(e).__name__})")
        return None
    v = row[0] if row else None
    if not isinstance(v, dict) or not v.get("rules") or not v.get("private_terms"):
        log("food house rules missing/incomplete")
        return None
    return v


def private_hit(text, terms):
    """First private term found in text (word-start match, case-insensitive), else None."""
    for t in terms or ():
        if re.search(r"\b" + re.escape(t.lower()), (text or "").lower()):
            return t
    return None


def fetch_recipes(cur, videos, n=RECIPES_MAX, prefer=(), avoid=()):
    """Nova's recipe memories (source 'recipes', type 'recipe': full recipe pages with URLs) for the
    Recipe Recommendation — the ones whose titles best match the day's videos, else a date-seeded
    pick so the block still has grounding on a thin day. -> [{channel,title,url,date,text}]."""
    cur.execute("""SELECT metadata->>'url', max(metadata->>'title'), max(metadata->>'source_site')
                   FROM memories
                   WHERE source = 'recipes' AND metadata->>'type' = 'recipe' AND metadata->>'url' LIKE 'https://%%'
                     AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                   GROUP BY 1""")
    pages = [(u, t or "", site or "") for u, t, site in cur.fetchall() if u]
    if not pages:
        return []
    day = set(re.findall(r"[a-z]{4,}", " ".join(f"{v['title']} {v['text']}" for v in videos).lower())) - RECIPE_STOP
    seed = nj.today_str()          # same pick all day, a different one tomorrow

    pref, avd = {w.lower() for w in prefer}, {w.lower() for w in avoid}

    def score(p):
        t = set(re.findall(r"[a-zñé]{3,}", p[1].lower()))
        return (len(t & pref) - 2 * len(t & avd), len(t & day), zlib.crc32(f"{seed}{p[0]}".encode()))
    pick = sorted(pages, key=score, reverse=True)[:n]
    cur.execute("""SELECT text, created_at, metadata FROM memories
                   WHERE source = 'recipes' AND metadata->>'type' = 'recipe' AND metadata->>'url' = ANY(%s)
                   ORDER BY metadata->>'url',
                            CASE WHEN metadata->>'chunk' ~ '^[0-9]+$' THEN (metadata->>'chunk')::int END""",
                ([p[0] for p in pick],))
    rows = []
    for text, created, md in cur.fetchall():
        md = dict(md or {})
        if BANNED.search(text or ""):
            continue
        md["channel"] = RECIPE_SITES.get(md.get("source_site") or "", md.get("source_site") or "recipe")
        rows.append((text, created, md))
    return group_videos(rows, RECIPE_CHARS, n)


def month_day(today=None):
    """'October 19' for nj.today_str() (YYYY-MM-DD)."""
    from datetime import date
    d = date.fromisoformat(today or nj.today_str())
    return f"{d.strftime('%B')} {d.day}"


def fetch_history(cur, today=None, n=HISTORY_MAX, military=True):
    """This-day-in-history notes: Nova's history memories (with a source URL) that state an event on
    today's month-day in a past year ("October 19, 1944" / "19 October 1307"). -> [{channel,title,url,
    date,text}] with the text cut around the date. Date-seeded order.
    military=True (Military and Friends): military_history / world_history / military_doctrine*, and the
    dated sentence must describe a military event. military=False (History and Friends): world_history /
    history, any event, non-military ones ranked first (military history has its own section)."""
    md_ = month_day(today)
    mon, day = md_.split()
    rx = rf"(\m{mon} {day},? [0-9]{{3,4}}\M|\m{day} {mon},? [0-9]{{3,4}}\M)"
    srcs = HISTORY_SOURCES if military else GENERAL_HISTORY_SOURCES
    cur.execute("""/* on_this_day */ SELECT text, created_at, metadata FROM memories
                   WHERE (source = ANY(%s) OR (%s AND source LIKE 'military_doctrine%%'))
                     AND metadata->>'url' LIKE 'http%%'
                     AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                     AND text ~ %s""", (list(srcs), bool(military), rx))
    seed, picked = nj.today_str(), {}
    find = re.compile(rf"\b(?:{mon} {day},? \d{{3,4}}|{day} {mon},? \d{{3,4}})\b")
    for text, created, md in cur.fetchall():
        md = dict(md or {})
        url = md.get("url") or ""
        hit = find.search(text or "")
        if not hit or url in picked or BANNED.search(text or ""):
            continue
        lo = max(0, hit.start() - HISTORY_CHARS // 2)
        snip = (text or "")[lo:lo + HISTORY_CHARS]
        # the military event must be in the SAME sentence as the date — not a manual's publication date
        start = max(text.rfind(". ", 0, hit.start()), text.rfind("\n", 0, hit.start())) + 1
        ends = [i for i in (text.find(". ", hit.end()), text.find("\n", hit.end())) if i != -1]
        sentence = text[start:min(ends) if ends else len(text)]
        is_mil = bool(MILITARY_WORDS.search(sentence))
        if (military and not is_mil) or CITATION_RE.search(sentence):
            continue
        picked[url] = {"channel": "Nova's history library", "title": md.get("title") or "history note",
                       "url": url, "date": md_, "text": snip.strip(),
                       "_rank": (military or not is_mil,             # History: non-military events first
                                 "wikipedia.org/" in url, zlib.crc32(f"{seed}{url}".encode()))}   # encyclopedia before manuals
    out = sorted(picked.values(), key=lambda v: v["_rank"], reverse=True)[:n]
    for v in out:
        v.pop("_rank")
    return out


def scripture_cite(source, md, text):
    """The citation a Text in Context block must carry: Bible '<Book> <ch>:<verses> (KJV)', Qur'an 'Qur'an
    <sura> (Rodwell)', else the passage's own header ('<work> — <section> (<translator> <year>)')."""
    if source == "bible" and md.get("book") and md.get("chapter"):
        tr = "KJV" if "King James" in (md.get("translation") or "KJV") else md.get("translation")
        return f"{md['book']} {md['chapter']}:{md.get('verses') or ''} ({tr})".replace(": (", " (")
    if source == "quran" and md.get("sura"):
        return f"Qur'an {md['sura']} ({(md.get('translation') or 'Rodwell').split(' (')[0]})"
    head = re.match(r"\[([^\]]{4,240})\]", text or "")
    if head:
        return head.group(1).strip()
    bits = [md.get("work") or md.get("title"), md.get("section")]
    return " — ".join(b for b in bits if b) or "scripture"


def scripture_stems(item):
    """Citation stems the published block must contain (any one): Bible 'Book ch', Qur'an sura, else the
    first two words of the work."""
    md = item.get("_md") or {}
    if item.get("_source") == "bible":
        return [f"{md.get('book')} {md.get('chapter')}"]
    if item.get("_source") == "quran":
        return [f"Qur'an {md.get('sura')}", f"Quran {md.get('sura')}", f"Sura {md.get('sura')}",
                f"Surah {md.get('sura')}"]
    work = re.sub(r"\(.*?\)", "", md.get("work") or md.get("title") or item.get("title") or "")
    return [" ".join(work.split()[:2])] if work.strip() else []


def fetch_scripture(cur, videos, n=SCRIPTURE_MAX, today=None):
    """Scripture-library passages for Text in Context: the passages sharing the most real words
    (>= 6 letters) with the day's video titles and text, at most SCRIPTURE_PER_SOURCE per tradition, plus
    one date-seeded passage so a thin match still leaves a choice. Every item carries its citation (title)
    and tradition label (channel). -> [{channel,title,url,date,text,_source,_md}]"""
    seed = today or nj.today_str()
    blob = " ".join(f"{v['title']} {v['title']} {v.get('text', '')[:3000]}" for v in videos).lower()
    freq = {}
    for w in re.findall(r"[a-z]{6,}", blob):
        if w not in SCRIPTURE_STOP:
            freq[w] = freq.get(w, 0) + 1
    words_ = [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:14]]
    rows = []
    if words_:
        cur.execute("""/* scripture_library */ SELECT text, metadata, source FROM memories
                       WHERE source = ANY(%s) AND metadata->>'type' IN ('scripture', 'philosophy')
                         AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                         AND text ~* %s
                       LIMIT 400""", (list(SCRIPTURE_SOURCES), r"\m(" + "|".join(words_) + r")\M"))
        rows = list(cur.fetchall())
    cur.execute("""/* scripture_library */ SELECT text, metadata, source FROM memories
                   WHERE source = ANY(%s) AND metadata->>'type' IN ('scripture', 'philosophy')
                     AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                     AND length(text) > 200
                   ORDER BY md5(coalesce(metadata->>'uid', '') || left(text, 60) || %s) LIMIT 1""",
                (list(SCRIPTURE_SOURCES), seed))
    fallback = list(cur.fetchall())
    fam_name, fam_srcs = focus_family(seed)
    fam_rows, fam_fallback = [], []
    if words_ and fam_srcs:   # the focus family's own word matches — the general query's LIMIT can crowd them out
        cur.execute("""/* scripture_library family */ SELECT text, metadata, source FROM memories
                       WHERE source = ANY(%s) AND metadata->>'type' IN ('scripture', 'philosophy')
                         AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                         AND text ~* %s
                       LIMIT 200""", (list(fam_srcs), r"\m(" + "|".join(words_) + r")\M"))
        fam_rows = list(cur.fetchall())
    if fam_srcs:
        cur.execute("""/* scripture_library family */ SELECT text, metadata, source FROM memories
                       WHERE source = ANY(%s) AND metadata->>'type' IN ('scripture', 'philosophy')
                         AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                         AND length(text) > 200
                       ORDER BY md5(coalesce(metadata->>'uid', '') || left(text, 60) || %s) LIMIT %s""",
                    (list(fam_srcs), seed, FAMILY_MIN))
        fam_fallback = list(cur.fetchall())
    scored = []
    for text, md, src in rows + fam_rows:
        md = md if isinstance(md, dict) else {}
        low = (text or "").lower()
        hit = {w for w in words_ if re.search(rf"\b{w}\b", low)}
        scored.append((len(hit), sum(freq[w] for w in hit), zlib.crc32(f"{seed}{text[:80]}".encode()), text, md, src))
    scored.sort(key=lambda r: r[:3], reverse=True)
    fam = [r for r in scored if r[5] in fam_srcs] + [(0, 0, 0, t, m, s_) for t, m, s_ in fam_fallback]
    picked, per = [], {}
    order = fam[:FAMILY_MIN] + scored + [(0, 0, 0, t, m, s_) for t, m, s_ in fallback]
    for *_, text, md, src in order:
        md = md if isinstance(md, dict) else {}
        if len(picked) >= n or per.get(src, 0) >= SCRIPTURE_PER_SOURCE or BANNED.search(text or ""):
            continue
        if any(p["text"] == (text or "")[:SCRIPTURE_CHARS] for p in picked):
            continue
        per[src] = per.get(src, 0) + 1
        url = md.get("url") or md.get("source_url")
        tradition = md.get("tradition") or {"bible": "Christianity / Judaism (Bible)", "quran": "Islam"}.get(src, src)
        if src == "nontheism":
            tradition += " — philosophy, not scripture"
        picked.append({"channel": f"Nova's scripture library — {tradition}", "title": scripture_cite(src, md, text),
                       "url": url if url and url.startswith("http") else None, "date": seed,
                       "text": (text or "")[:SCRIPTURE_CHARS], "_source": src, "_md": md})
    return picked


_VERSE_NUM = re.compile(r"(?:^|\s)(\d{1,3})\s+(?=[A-Z\[(‘“'\"])")


def _flat(t):
    """Compare-key for quotes: verse numbers, punctuation, case and whitespace removed."""
    t = re.sub(r"\d+", " ", (t or "").lower().replace("’", "'"))
    return " ".join(re.sub(r"[^a-z' ]+", " ", t).split())


def _bible_verses(text):
    """{verse_no: verse text} from a KJV passage ('[Book ch:a-b (KJV)] 1 In the ... 2 And ...')."""
    body = re.sub(r"^\[[^\]]*\]\s*", "", text or "")
    parts = _VERSE_NUM.split(" " + body)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def verse_ok(block, items):
    """Fail-closed check for Text in Context: EVERY blockquote in the block (a theme day may contrast two) must be
    a word-for-word run from one scripture passage, the block must carry that passage's citation stem, and a
    Bible citation's verse numbers must contain the quoted words. -> (ok, reason: the cited titles joined ' + ')."""
    groups, cur = [], []
    for ln in (block or "").splitlines():
        if ln.startswith(">"):
            cur.append(ln[1:].strip())
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    if not groups:
        return False, "no quoted passage"
    cites = []
    for g in groups:
        ok, why = _quote_ok(g, block, items)
        if not ok:
            return False, why
        if why not in cites:
            cites.append(why)
    return True, " + ".join(cites)


def _quote_ok(quote_lines, block, items):
    quote = " ".join(q for q in quote_lines if q and not re.match(r"^[—–-]", q))
    quote = re.sub(r"[—–-]\s*[A-Z][^\n]*\d[^\n]*$", "", quote).strip()   # trailing attribution on the quote line
    fq = _flat(quote)
    if len(fq.split()) < 5:
        return False, "no quoted passage"
    for it in (i for i in items if i.get("_source")):
        if fq not in _flat(it["text"]):
            continue
        stems = [st for st in scripture_stems(it) if st.strip()]
        if stems and not any(st.lower() in block.lower() for st in stems):
            return False, f"quote matches {it['title']!r} but the block does not cite it"
        if it["_source"] == "bible":
            md = it.get("_md") or {}
            m = re.search(rf"{re.escape(str(md.get('book')))}\s+{md.get('chapter')}:(\d+)(?:\s*[-–]\s*(\d+))?", block)
            if not m:
                return False, "Bible quote without a chapter:verse citation"
            verses = _bible_verses(it["text"])
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            span = " ".join(verses.get(v, "") for v in range(a, b + 1))
            if verses and fq not in _flat(span):
                return False, f"quoted words are not in {md.get('book')} {md.get('chapter')}:{a}-{b}"
        return True, it["title"]
    return False, "quote is not word-for-word from the scripture library"


def lore_label(source, md):
    """The framing label an esoteric-library excerpt must keep in public."""
    if source == "necronomicon" or md.get("fiction") or md.get("type") == "fiction_text":
        return "FICTION (H. P. Lovecraft's mythos)" if source == "necronomicon" else "FICTION"
    if source in ("books_of_the_dead", "shams_al_maarif"):
        return md.get("framing") or "historical-esoteric text (history, not instructions)"
    return "encyclopedia article" if md.get("type") == "wikipedia" else "historical-esoteric text"


def fetch_lore(cur, videos, n=LORE_MAX):
    """Esoteric-library context for the Rabbit Hole: the pages whose titles share the most words with
    the day's videos (none shared = nothing). Every item carries its label in `channel`."""
    def keys(t):
        return {w.rstrip("s") for w in re.findall(r"[a-z]{5,}", (t or "").lower())} - LORE_STOP
    day = keys(" ".join(v["title"] for v in videos))
    if not day:
        return []
    cur.execute("""/* esoteric_library */ SELECT metadata->>'url', max(coalesce(metadata->>'title', metadata->>'work')), max(source)
                   FROM memories
                   WHERE source = ANY(%s) AND metadata->>'url' LIKE 'http%%'
                     AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                   GROUP BY 1""", (list(LORE_SOURCES),))
    scored = []
    for url, title, src in cur.fetchall():
        shared = keys(title) & day
        hits = sum(len(w) for w in shared)
        if any(len(w) >= 6 for w in shared):     # one real topic word, not a stray common one
            scored.append((hits, zlib.crc32(f"{nj.today_str()}{url}".encode()), url, title, src))
    pick = sorted(scored, reverse=True)[:n]
    if not pick:
        return []
    cur.execute("""/* esoteric_library */ SELECT text, created_at, metadata, source FROM memories
                   WHERE source = ANY(%s) AND metadata->>'url' = ANY(%s)
                   ORDER BY metadata->>'url',
                            CASE WHEN metadata->>'chunk' ~ '^[0-9]+$' THEN (metadata->>'chunk')::int END NULLS FIRST""",
                (list(LORE_SOURCES), [p[2] for p in pick]))
    rows = []
    for text, created, md, src in cur.fetchall():
        md = dict(md or {})
        if BANNED.search(text or ""):
            continue
        md["channel"] = f"Nova's esoteric library — {lore_label(src, md)}"
        md["title"] = md.get("title") or md.get("work") or "library text"
        rows.append((text, created, md))
    return group_videos(rows, LORE_CHARS, n)


EXTRA_LABELS = {"military": "THIS DAY IN HISTORY", "history": "THIS DAY IN HISTORY",
                "belief": "SCRIPTURE LIBRARY (quote word for word, cite exactly)",
                "mystery": "ESOTERIC LIBRARY (keep each label)"}
EXTRA_SOURCE_HEADINGS = {"military": "This Day in History", "history": "This Day in History",
                         "belief": "Scripture", "mystery": "Library"}


def top_block(subj, videos, arts, recipes=(), food_rules=None, extra=(), theme=None):
    """The featured block for this subject ("" = omitted, fail closed). Food needs its house rules
    (food_rules) and a block free of every private term, or there is no block. `extra` = the
    military history notes / mystery library excerpts (linkable grounding, labelled)."""
    heading, task = FEATURES[subj]
    if subj in ("military", "history"):
        task = task.replace("{today}", month_day())
    if subj == "belief" and theme:
        task += THEME_TOP_TASK.format(question=theme["question"])
    elif subj == "belief":
        fam, srcs = focus_family()
        task += (f" BALANCE: today's focus is the {fam} traditions — " +
                 ("choose a passage from them when one connects at all, even if the day's videos are mostly about "
                  "another faith; " if srcs else
                  "choose any passage that a new movement or sect in the material quotes, rereads or rejects, and "
                  "say how it reads it; ") +
                 "never let one tradition (Christianity included) own the block day after day.")
    if subj == "food":
        if not food_rules:
            log("Recipe Recommendation: no house rules — block omitted (fail closed)")
            return ""
        task += "\n\nHOUSE RULES:\n" + food_rules["rules"]
    material = digest([*videos, *arts])
    if recipes:
        material += "\n\n--- RECIPE PAGES (Nova's recipe memories) ---\n\n" + digest(list(recipes))
    if extra:
        label = EXTRA_LABELS.get(subj, "ESOTERIC LIBRARY (keep each label)")
        if subj == "belief":   # every passage with the exact citation to copy
            material += f"\n\n--- {label} ---\n\n" + "\n\n".join(
                f"[{x['channel']}]\nCITE AS: {x['title']}\n{x['text']}" for x in extra)
        else:
            material += f"\n\n--- {label} ---\n\n" + digest(list(extra))
    if SUBJECTS[subj].get("extra_rules"):
        task += " " + SUBJECTS[subj]["extra_rules"]
    stance = stance_for(subj, material)
    if stance:
        task += "\n\n" + stance
    rules = ("This is a PUBLIC page: never mention Jordan's employer or his work, never mention 'OpenClaw', no "
             "IP addresses, no hostnames, no file paths, no names of people in Jordan's household, nothing about "
             "when anyone is home or asleep, no sexual content. Never invent prices, specs, model numbers, "
             "ingredients, amounts or quotes that are not in the material.")
    block = featured_block(heading, task, material, [*videos, *arts, *recipes, *extra], rules, llm=nj.call_openrouter,
                           max_words=650 if subj == "food" else 450)   # a recipe needs room for its method
    if block and subj == "belief":
        ok, why = verse_ok(block, extra)
        if not ok:
            log(f"Text in Context: {why} — block omitted (fail closed)")
            return ""
        log(f"Text in Context: verified against {why}")
    if block and subj == "crime" and ADDRESS_RE.search(block):
        log("Case of the Day: street address in the block — omitted (fail closed)")
        return ""
    if block and subj == "food":
        hit = private_hit(block, food_rules.get("private_terms"))
        if hit:
            log("Recipe Recommendation: private-term check tripped — block omitted (fail closed)")
            return ""
    if block and subj == "music":   # Jordan 2026-10-19: Nova "hears" the track (nova_hear; fail soft, never raises)
        import nova_hear
        block = nova_hear.add_sound_note(block, videos, log)
    return block


def source_list(items):
    """Linked items via the Watches helper, plus unlinked (downloaded-episode) ones listed plainly."""
    linked = [v for v in items if v["url"]]
    out = sources(linked) if linked else ""
    plain = [f"- {v['channel']} — {v['title'] or 'video'} ({v['date']})" for v in items if not v["url"]]
    return "\n".join(x for x in [out, *plain] if x) or "- (none)"


def main():
    ap = argparse.ArgumentParser(description="The *and Friends* roundups (cars, tech, food, music, military, "
                                             "screen, science, mystery, crime, workshop, travel, history, belief)")
    ap.add_argument("--subject", choices=sorted(SUBJECTS), required=True)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mode", choices=("auto", "theme", "roundup"), default="auto",
                    help="belief: auto = theme on is_theme_day() dates, roundup otherwise")
    ap.add_argument("--theme", help="belief: this belief_themes.theme instead of a random pick (implies --mode theme)")
    a = ap.parse_args()
    cfg = SUBJECTS[a.subject]
    name, section, noun = cfg["name"], cfg["section"], cfg["noun"]

    mc = _connect(MEM_DSN).cursor()
    if a.subject == "belief":
        try:
            ops = _connect(OPS_DSN, attempts=2)
            ops.autocommit = True   # mark_theme_used() writes through this cursor
            oc = ops.cursor()
        except Exception as e:  # noqa: BLE001 — belief_sources() logs the fallback loudly
            oc = None
            log(f"ERROR nova_ops unreachable for belief_sources ({e})")
        apply_belief_sources(cfg, oc)
        if oc is not None:
            try:
                for line in belief_coverage(oc, mc):
                    log(line)
            except Exception as e:  # noqa: BLE001 — the coverage report never sinks an issue
                log(f"coverage report failed ({e})")
    theme = None
    if a.subject == "belief" and (a.theme or a.mode == "theme" or (a.mode == "auto" and is_theme_day(nj.today_str()))):
        theme, videos, trads, fams = (choose_theme(oc, mc, cfg, nj.today_str(), a.theme) if oc is not None
                                      else (None, [], [], []))
        if theme is None:
            log(f"theme day FALLBACK: no theme has material from {THEME_MIN_TRADITIONS} traditions "
                f"(belief_themes unreadable, unknown --theme, or thin material) — roundup instead")
        else:
            arts = []
            log(f"theme day: '{theme['question']}' ({theme['theme']}) — {len(videos)} videos, "
                f"{len(trads)} traditions ({', '.join(trads)}), {len(fams)} families ({', '.join(fams)})")
    if theme is None:
        videos, arts = fetch(mc, cfg, a.days)
    if theme is None and cfg.get("widen") and a.days == 1 and len(videos) + len(arts) < THIN_MIN:
        log(f"{name}: only {len(videos)} videos today — looking back {WIDEN_DAYS} days")
        videos, arts = fetch(mc, cfg, WIDEN_DAYS)
    log(f"{name}: {len(videos)} videos, {len(arts)} articles in the window")
    if not videos and not arts:
        log(f"no {noun} news in the window — no issue today")
        return 0

    week = nj.today_str()
    daily = a.days == 1
    period, span = ("daily", f"for {week}") if daily else ("weekly", f"for the week ending {week}")
    unit = "day" if daily else "week"
    news_w, cap_w, more_w, min_words = (("1,500-3,000", "4,500", "800-1,500", 2000) if daily else
                                        ("4,000-6,000", "10,000", "1,500-3,000", 5000))
    rules = ("Ground every claim in the transcripts and articles below; name the channel or outlet and the "
             "video or article when you use it. Never invent prices, specs, model numbers, dates or quotes "
             "that are not in the sources. If the sources are thin, write less — never pad with invention. "
             "This is a PUBLIC page: never mention Jordan's employer or his work, never mention 'OpenClaw', "
             "no IP addresses, no file paths, no names of people in Jordan's household, nothing about when "
             "anyone is home or asleep, no sexual content. Markdown with ## and ### headers, no H1. Do NOT "
             "write a sources or links list — it is appended automatically.")
    if cfg.get("extra_rules"):
        rules += " " + cfg["extra_rules"]
    src = ""
    if videos:
        src += "--- VIDEO TRANSCRIPTS ---\n\n" + digest(videos)
    if arts:
        src += "\n\n--- NEWS AND BLOG ARTICLES ---\n\n" + digest(arts)
    stance = stance_for(a.subject, src)
    if stance:
        rules += "\n\n" + stance
    news_ctx = theme_context(name, theme["question"], week, news_w, cap_w, rules) if theme else (
                f"You are writing '{name}', Nova's {period} {noun} roundup {span}. Voice: Nova — sharp, funny, "
                f"opinionated, but the {noun} content is the point. Open with a short intro to the {unit}, then "
                f"organise by STORY, not by channel: {cfg['beats']}. Where several sources covered the same "
                f"thing, compare their takes. Give real depth: {news_w} words (the whole issue must stay under "
                f"{cap_w}). {rules}\nFirst line exactly: TITLE: <a punchy title for the whole {period} issue>")
    raw = nj.call_openrouter(nova_voice.system_prompt(news_ctx, section=section),
                             f"--- THIS {unit.upper()}'S {noun.upper()} SOURCES ---\n\n{src}\n\nWrite the issue.",
                             model=MODEL, max_tokens=16000, temperature=0.75, timeout=1200)
    title, body = split_title(raw)
    if not body:
        log("news generation produced nothing — aborting")
        return 1
    if theme:   # theme days carry their own title format, distinct from the roundups
        title = f"{name} — The Big Question: {theme['question']}"

    if words(body) < min_words:
        more_ctx = (f"Continue '{name}' with a 'Deeper Cuts' part: {more_w} more words on the sources below "
                    f"that the main piece covered least — specifics, comparisons, what it means for {cfg['audience']}. "
                    f"Do not repeat the existing text. {rules} Start with '## Deeper Cuts'.")
        more = nj.call_openrouter(nova_voice.system_prompt(more_ctx, section=section),
                                  "--- ALREADY WRITTEN ---\n" + body[:12000] + "\n\n" + src,
                                  model=MODEL, max_tokens=8000, temperature=0.75, timeout=900)
        if more:
            body += "\n\n" + more.strip()

    recipes, food_rules = [], None
    if a.subject == "food":
        food_rules = load_food_rules()
        try:
            recipes = fetch_recipes(mc, videos, prefer=(food_rules or {}).get("prefer", ()),
                                    avoid=(food_rules or {}).get("avoid", ()))
        except Exception as e:  # noqa: BLE001 — recipe memories are optional grounding
            log(f"recipe memories unavailable ({e})")
    extra = []
    try:   # optional grounding for the top block; never sinks the issue
        if a.subject == "military":
            extra = fetch_history(mc)
        elif a.subject == "history":
            extra = fetch_history(mc, military=False)
        elif a.subject == "belief":
            lead = [{"title": " ".join(theme["keywords"]) * 2, "text": theme["question"]}] if theme else []
            extra = fetch_scripture(mc, lead + videos, n=SCRIPTURE_MAX + (THEME_PASSAGES if theme else 0))
        elif a.subject == "mystery":
            extra = fetch_lore(mc, videos)
    except Exception as e:  # noqa: BLE001
        log(f"top-block library unavailable ({e})")
    block = top_block(a.subject, videos, arts, recipes, food_rules, extra, theme=theme)
    if not title or len(title) < 8:
        title = f"{name} — {week}" if daily else f"{name} — Week Ending {week}"
    title = title if title.lower().startswith(name.lower()) else f"{name}: {title}"
    body = place_block(title, block, body)
    if block and body.startswith(block):
        log(f"{FEATURES[a.subject][0]}: {words(block)} words at the top")
    else:
        block = ""

    body += "\n\n## Sources\n\n"
    if videos:
        body += "### Videos\n\n" + source_list(videos) + "\n\n"
    if arts:
        body += "### Articles\n\n" + source_list(arts) + "\n"
    linked = [r for r in recipes if r["url"] in block]
    if linked:
        body += "\n### Recipe\n\n" + source_list(linked) + "\n"
    linked = [r for r in extra if r["url"] and r["url"] in block]
    if a.subject == "belief" and block:   # the verified passages are listed even when their pages are not linked
        ok, cite = verse_ok(block, extra)
        linked = [r for r in extra if (ok and r["title"] in cite.split(" + ")) or (r["url"] and r["url"] in block)][:3]
    if linked:
        body += "\n### " + EXTRA_SOURCE_HEADINGS.get(a.subject, "Library") + "\n\n" + \
            source_list([{k: v for k, v in r.items() if not k.startswith("_")} for r in linked]) + "\n"
    body = body.rstrip() + "\n"
    if a.subject == "crime":   # never a street address in a public crime write-up
        body = ADDRESS_RE.sub("[address withheld]", body)
    hit = BANNED.search(title + "\n" + body)
    if hit:
        log(f"NOT PUBLISHED — public-journal check tripped on {hit.group(0)!r} (fail closed)")
        return 1
    log(f"{words(body)} words: {title}")
    if a.dry_run:
        print(body)
        return 0

    img = None
    try:
        img = nj.generate_image(nj.get_image_prompt(title, cfg["image_topic"], section),
                                width=1024, height=768, section=section)
    except Exception as e:
        log(f"image failed (non-fatal): {e}")
    front = None
    if a.subject == "belief":
        front = {"edition": "theme", "theme": theme["theme"]} if theme else {"edition": "roundup"}
    desc = (f"The Big Question — {theme['question'].rstrip('.?!')}{'?' if theme['question'].endswith('?') else '.'} How the world's traditions, their scholars and their skeptics "
            "answer it, where they agree and where they part." if theme else cfg["desc"].format(period=period, span=unit))
    if not nj.publish_hugo(title, body, section, [*cfg["tags"], "theme" if theme else period], desc,
                           image_path=img, emoji=cfg["emoji"], profile=None, extra_front=front):
        log("NOT PUBLISHED — quality guard rejected")
        return 1
    if theme and oc is not None:
        mark_theme_used(oc, theme["theme"], nj.today_str())
    push = nj.git_push(section, title)
    nj.notify_slack(section, f"{cfg['emoji']} {title}", f"{name} is out ({words(body)} words, {len(videos)} videos, "
                    f"{len(arts)} articles). Push: {push}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
