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
Usage: nova_friends.py --subject cars|tech|food [--days 1] [--dry-run]
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
                                      group_videos, sources, split_title, words)

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
FOOD_CHANNELS = (   # Jordan 2026-10-19. Undecided, so NOT here: Peter Santenello, Miha's Life in Japan,
    #                   Bacon 28, fonki cheff, The Stock Pot.
    "Meat Church BBQ", "HowToBBQRight", "Chuds BBQ", "Mad Scientist BBQ", "allthingsbbq",
    "Cooper Abercrombie - Bar-A-BBQ", "Sous Vide Everything", "Binging with Babish", "SAM THE COOKING GUY",
    "SONS OF SAM THE COOKING GUY", "Gordon Ramsay", "Alton Brown", "MATTY MATHESON", "NOT ANOTHER COOKING SHOW",
    "America's Test Kitchen", "French Cooking Academy", "Christian Puglisi", "Iron Chef Dad", "Buzz Cooks",
    "Abuela's Table", "A Cooks Tour with Anthony Bourdain", "Andrew Zimmern", "Eater", "First We Feast",
    "The Immigrant Table", "Jimmy Tries World", "Tasting History with Max Miller", "Whiskey Tribe", "Still It",
    "James May's Planet Gin", "City Steading Brews", "Beers & Bars",
)
FOOD_EXCLUDED = ("Peter Santenello", "Miha's Life in Japan", "Bacon 28", "fonki cheff", "The Stock Pot")
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


def top_block(subj, videos, arts, recipes=(), food_rules=None):
    """The featured block for this subject ("" = omitted, fail closed). Food needs its house rules
    (food_rules) and a block free of every private term, or there is no block."""
    heading, task = FEATURES[subj]
    if subj == "food":
        if not food_rules:
            log("Recipe Recommendation: no house rules — block omitted (fail closed)")
            return ""
        task += "\n\nHOUSE RULES:\n" + food_rules["rules"]
    material = digest([*videos, *arts])
    if recipes:
        material += "\n\n--- RECIPE PAGES (Nova's recipe memories) ---\n\n" + digest(list(recipes))
    rules = ("This is a PUBLIC page: never mention Jordan's employer or his work, never mention 'OpenClaw', no "
             "IP addresses, no hostnames, no file paths, no names of people in Jordan's household, nothing about "
             "when anyone is home or asleep, no sexual content. Never invent prices, specs, model numbers, "
             "ingredients, amounts or quotes that are not in the material.")
    block = featured_block(heading, task, material, [*videos, *arts, *recipes], rules, llm=nj.call_openrouter,
                           max_words=650 if subj == "food" else 450)   # a recipe needs room for its method
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
    ap = argparse.ArgumentParser(description="Cars and Friends / Tech and Friends / Food and Friends roundup")
    ap.add_argument("--subject", choices=sorted(SUBJECTS), required=True)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = SUBJECTS[a.subject]
    name, section, noun = cfg["name"], cfg["section"], cfg["noun"]

    mc = _connect(MEM_DSN).cursor()
    videos, arts = fetch(mc, cfg, a.days)
    log(f"{name}: {len(videos)} videos, {len(arts)} articles in the last {a.days} days")
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
    src = ""
    if videos:
        src += "--- VIDEO TRANSCRIPTS ---\n\n" + digest(videos)
    if arts:
        src += "\n\n--- NEWS AND BLOG ARTICLES ---\n\n" + digest(arts)
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
    block = top_block(a.subject, videos, arts, recipes, food_rules)
    if block:
        body = block + "\n" + body
        log(f"{FEATURES[a.subject][0]}: {words(block)} words at the top")

    body += "\n\n## Sources\n\n"
    if videos:
        body += "### Videos\n\n" + source_list(videos) + "\n\n"
    if arts:
        body += "### Articles\n\n" + source_list(arts) + "\n"
    linked = [r for r in recipes if r["url"] in block]
    if linked:
        body += "\n### Recipe\n\n" + source_list(linked) + "\n"
    body = body.rstrip() + "\n"
    if not title or len(title) < 8:
        title = f"{name} — {week}" if daily else f"{name} — Week Ending {week}"
    title = title if title.lower().startswith(name.lower()) else f"{name}: {title}"
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
