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

Usage: nova_friends.py --subject cars|tech|food|music|military|screen|science|mystery|crime|workshop|travel
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
from nova_watches_and_friends import (MEM_DSN, MODEL, OPS_DSN, _connect, digest, featured_block,  # noqa: E402
                                      group_videos, place_block, sources, split_title, words)

VID_PER, VID_MAX = 1600, 30         # ~48k chars of video transcript
ART_PER, ART_MAX = 700, 24          # + ~17k of articles: under the size that hangs claude -p

CARS_CHANNELS = (
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
    # Jordan 2026-10-19: RC cars -> Cars (Parkflyers RC, planes, stays out)
    "AMain Hobbies", "RCDriver_Online", "RC Girl", "Redcat Racing", "TeamAssociatedRC", "1:10 Rod Shop",
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
    "Rick Beato", "Rick Beato 2", "Adam Neely", "12tone", "Trash Theory",
    "Amplified - Classic Rock & Music History", "Rock N' Rewind", "Discogs", "KEXP", "UKF Drum & Bass",
    "DnB Allstars", "Drum&BassArena", "RELEVANT DNB", "DnB Portal", "Nia Archives", "Boiler Room", "HÖR BERLIN",
    "DJ Mag", "Skratch Bastid", "Rave United", "RJD2", "Juno Reactor", "Trickyofficial", "Laufey",
    "Marc Rebillet - Topic", "Justin Johnson",
    # clearly-music subscriptions added alongside Jordan's list
    "DJ Studio | Bass Portal & DnB Portal", "Didi Early Rave", "TRACKLIB", "Mixtape Magic", "Pearl Acoustics",
    "FeelTheSound", "Synthet", "DJRapOfficial", "BASSKILLAH", "KROQ", "Current Joys", "EYEDRESS", "Jonathan Bree",
    "Tubeway Army - Topic", "RŮDE - Topic", "Sleepy Fish - Topic", "Nvrsoft - Topic",
)
MILITARY_CHANNELS = (
    "Forgotten Weapons", "LazerPig", "Task & Purpose", "Ward Carroll", "Military Aviation History",
    "The Military Show", "Cold War Impact", "The Great War", "WW2 Stories", "VASAviation -",
    "American Veterans Center", "U.S. Navy", "Combat Veteran News", "Airforceproud95",
)
SCREEN_CHANNELS = (
    "RedLetterMedia", "CinemaSins", "Screen Junkies", "ScreenCrush", "How It Should Have Ended",
    "How Did This Get Made?", "JoBlo Horror Originals", "The A.V. Club", "ExtendedCutHD", "Defunctland",
    "Bright Sun Films", "WatchMojo.com", "Multiverse Showdown",
    # late night and comedy
    "Jimmy Kimmel Live", "Late Night with Seth Meyers", "Late Night With Conan O'Brien", "LastWeekTonight",
    "The Daily Show", "The Problem With Jon Stewart", "The Weekly Show with Jon Stewart", "Trevor Noah",
    "Patton Oswalt", "Kevin Smith",
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
    """MILITARY_STANCE for Military always; for Screen/Travel only if the material touches the war."""
    if subj == "military" or (subj in ("screen", "travel") and STANCE_TOPICS.search(text or "")):
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


def fetch(cur, cfg, days):
    """-> (videos, articles) grouped newest-first, banned-term rows dropped."""
    keys = sorted({norm(c) for c in cfg["channels"]})
    cur.execute(f"""SELECT text, created_at, metadata FROM memories
                    WHERE created_at > now() - make_interval(days => %s)
                      AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
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
    videos = group_videos(vrows, VID_PER, VID_MAX)
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


def fetch_history(cur, today=None, n=HISTORY_MAX):
    """This-day-in-military-history notes: Nova's military/world history memories (with a source URL)
    that state a military event on today's month-day in a past year ("October 19, 1944" / "19 October
    1307"). -> [{channel,title,url,date,text}] with the text cut around the date. Date-seeded order."""
    md_ = month_day(today)
    mon, day = md_.split()
    rx = rf"(\m{mon} {day},? [0-9]{{3,4}}\M|\m{day} {mon},? [0-9]{{3,4}}\M)"
    cur.execute("""/* on_this_day */ SELECT text, created_at, metadata FROM memories
                   WHERE (source = ANY(%s) OR source LIKE 'military_doctrine%%')
                     AND metadata->>'url' LIKE 'http%%'
                     AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                     AND text ~ %s""", (list(HISTORY_SOURCES), rx))
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
        if not MILITARY_WORDS.search(sentence) or CITATION_RE.search(sentence):
            continue
        picked[url] = {"channel": "Nova's history library", "title": md.get("title") or "history note",
                       "url": url, "date": md_, "text": snip.strip(),
                       "_rank": ("wikipedia.org/" in url, zlib.crc32(f"{seed}{url}".encode()))}   # encyclopedia before manuals
    out = sorted(picked.values(), key=lambda v: v["_rank"], reverse=True)[:n]
    for v in out:
        v.pop("_rank")
    return out


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


def top_block(subj, videos, arts, recipes=(), food_rules=None, extra=()):
    """The featured block for this subject ("" = omitted, fail closed). Food needs its house rules
    (food_rules) and a block free of every private term, or there is no block. `extra` = the
    military history notes / mystery library excerpts (linkable grounding, labelled)."""
    heading, task = FEATURES[subj]
    if subj == "military":
        task = task.replace("{today}", month_day())
    if subj == "food":
        if not food_rules:
            log("Recipe Recommendation: no house rules — block omitted (fail closed)")
            return ""
        task += "\n\nHOUSE RULES:\n" + food_rules["rules"]
    material = digest([*videos, *arts])
    if recipes:
        material += "\n\n--- RECIPE PAGES (Nova's recipe memories) ---\n\n" + digest(list(recipes))
    if extra:
        label = "THIS DAY IN HISTORY" if subj == "military" else "ESOTERIC LIBRARY (keep each label)"
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
    if block and subj == "crime" and ADDRESS_RE.search(block):
        log("Case of the Day: street address in the block — omitted (fail closed)")
        return ""
    if block and subj == "food":
        hit = private_hit(block, food_rules.get("private_terms"))
        if hit:
            log("Recipe Recommendation: private-term check tripped — block omitted (fail closed)")
            return ""
    return block


def source_list(items):
    """Linked items via the Watches helper, plus unlinked (downloaded-episode) ones listed plainly."""
    linked = [v for v in items if v["url"]]
    out = sources(linked) if linked else ""
    plain = [f"- {v['channel']} — {v['title'] or 'video'} ({v['date']})" for v in items if not v["url"]]
    return "\n".join(x for x in [out, *plain] if x) or "- (none)"


def main():
    ap = argparse.ArgumentParser(description="The *and Friends* roundups (cars, tech, food, music, military, "
                                             "screen, science, mystery, crime, workshop, travel)")
    ap.add_argument("--subject", choices=sorted(SUBJECTS), required=True)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = SUBJECTS[a.subject]
    name, section, noun = cfg["name"], cfg["section"], cfg["noun"]

    mc = _connect(MEM_DSN).cursor()
    videos, arts = fetch(mc, cfg, a.days)
    if cfg.get("widen") and a.days == 1 and len(videos) + len(arts) < THIN_MIN:
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
    news_ctx = (f"You are writing '{name}', Nova's {period} {noun} roundup {span}. Voice: Nova — sharp, funny, "
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
        elif a.subject == "mystery":
            extra = fetch_lore(mc, videos)
    except Exception as e:  # noqa: BLE001
        log(f"top-block library unavailable ({e})")
    block = top_block(a.subject, videos, arts, recipes, food_rules, extra)
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
    linked = [r for r in extra if r["url"] in block]
    if linked:
        body += "\n### " + ("This Day in History" if a.subject == "military" else "Library") + "\n\n" + \
            source_list(linked) + "\n"
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
    if not nj.publish_hugo(title, body, section, [*cfg["tags"], period],
                           cfg["desc"].format(period=period, span=unit),
                           image_path=img, emoji=cfg["emoji"], profile=None):
        log("NOT PUBLISHED — quality guard rejected")
        return 1
    push = nj.git_push(section, title)
    nj.notify_slack(section, f"{cfg['emoji']} {title}", f"{name} is out ({words(body)} words, {len(videos)} videos, "
                    f"{len(arts)} articles). Push: {push}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
