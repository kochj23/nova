#!/opt/homebrew/bin/python3
"""nova_satire.py — Nova's satire image pool (Jordan 2026-10-19).

"Create a satirical and funny photo for every article, and when you create the Nova Speaks videos use a variety
of those photos. At least 100 to pool from — the same rotation for every video is pretty boring."

  --pool N        fill the pool to >= N curated satire images (SCENES x STYLES), local ComfyUI only
  --articles      give each new article (queued by publish_hugo) one tailored satirical image inline,
                  then backfill articles from the last --days days that lack one
  --pick SLUG     dry-run the Nova Speaks picker for a published article (prints the chosen images)
  --selftest      pure checks

Everything is resumable (PG is the ledger) and polite on the GPU: before each render it waits for ComfyUI's
queue to be empty, so scheduled covers always go first. Files live on the NAS (nova-satire/pool), never the
Studio's disks; per-article copies also go into the journal at static/images/satire/<slug>.webp.

Tables (nova_ops): satire_images (the pool), satire_video_picks (which images each video used),
satire_article_queue (articles waiting for their inline image).
"""
import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

NAS_ROOT = Path("/Volumes/nas") if sys.platform == "darwin" else Path("/mnt/nas")
POOL_REL = "nova-satire/pool"
JOURNAL = Path.home() / "nova-journal"
MARKER = "<!-- satire-image -->"
NO_TEXT = ("Affectionate, absurd satire. No readable text, no letters, no logos, no brand names, "
           "no real or famous people, nothing sexual.")
RECENT_VIDEOS = 10
POOL_GAP_S = 45
SKIP_SECTIONS = {"art", "start-here", "search", "about"}


def log(m):
    print(f"[satire {time.strftime('%H:%M:%S')}] {m}", flush=True)


# ── the curated pool: (key, themes, scene) ─────────────────────────────────────────────────
SCENES = [
    ("studio-throne", ["nova", "ai", "fleet"], "a sarcastic AI depicted as a glowing aluminium desktop computer on a velvet throne, tiny mini-PC courtiers bowing"),
    ("minipc-union", ["fleet", "ai", "ops"], "a row of tiny mini PCs on a picket line holding blank protest signs outside a server rack"),
    ("helicopter-traffic", ["burbank", "flights", "local"], "a suburban sky jammed with dozens of helicopters stuck in a traffic jam, one honking"),
    ("haze-fire-cam", ["traffic", "alerts", "burbank"], "a dramatic traffic camera on a pole screaming at a gentle wisp of afternoon haze, firefighters shrugging"),
    ("rave-cd-infomercial", ["music", "90s", "rave"], "a 1998 late-night infomercial set with a cheesy host waving a stack of identical rave compilation CDs"),
    ("fishbowl-watch-drama", ["watches", "fishbowl", "drama"], "goldfish in tiny suits arguing furiously over a wristwatch inside a fishbowl"),
    ("robot-laundry", ["robots", "home", "ai"], "a robot butler proudly folding a fitted sheet into a perfect cube"),
    ("robot-dishes", ["robots", "home", "food"], "a robot carefully washing dishes by putting them in the oven"),
    ("ai-reading-books", ["ai", "learning", "books"], "a small robot buried under a mountain of open books, reading with a flashlight at midnight"),
    ("server-sauna", ["ops", "fleet", "heat"], "server racks relaxing in a steamy sauna wearing towels, fans spinning lazily"),
    ("pg-replica-twins", ["database", "ops", "fleet"], "two identical elephants in matching outfits mirroring each other's every move across a fence"),
    ("cat-ups", ["power", "ups", "ops"], "a giant battery backup unit with a cat napping on top while the lights flicker"),
    ("cable-spaghetti", ["network", "ops", "fleet"], "an Italian chef serving a plate of tangled ethernet cables as spaghetti"),
    ("wifi-backdoor", ["network", "wifi", "ops"], "a tiny secret door in a router with a sign-shaped blank plaque, a mouse sneaking through"),
    ("dns-switchboard", ["network", "dns", "ops"], "a 1950s telephone switchboard operator connecting glowing cables between houses on a hill"),
    ("cron-alarm-clocks", ["scheduler", "ops", "time"], "a wall covered in hundreds of ringing alarm clocks and one exhausted robot in pajamas"),
    ("memory-attic", ["memory", "ai", "nova"], "an endless attic of labeled boxes of memories, a robot with a lantern looking for one sock"),
    ("three-million-cake", ["milestone", "nova", "memory"], "a robot blowing out three million birthday candles on an enormous cake, fire department on standby"),
    ("whisper-ears", ["transcription", "tv", "ai"], "a robot with enormous ear trumpets listening to an old CRT television playing a 70s detective show"),
    ("detective-trenchcoat", ["tv", "crime", "70s"], "a portly 1970s private detective in a trench coat interrogating a toaster"),
    ("infomercial-tub", ["tv", "ads", "90s"], "a walk-in bathtub on a game-show stage surrounded by applauding robots"),
    ("belief-roundtable", ["belief", "religion", "history"], "a round table of robed scholars from many eras politely arguing over a single candle"),
    ("satan-temple-picnic", ["belief", "satanism", "religion"], "a cheerful goat in a cardigan hosting a church-basement bake sale"),
    ("monk-wifi", ["belief", "tech", "humor"], "a meditating monk floating above a cushion while a wifi router floats beside him"),
    ("shaker-chair", ["belief", "history", "craft"], "a single perfectly simple wooden chair hanging on a wall peg, a squirrel admiring it"),
    ("cars-and-friends-garage", ["cars", "garage", "automotive"], "a vintage muscle car in a garage being lovingly polished by three tiny robots"),
    ("tow-truck-romance", ["cars", "automotive", "humor"], "a tow truck carrying a sports car like a groom carrying a bride over a threshold"),
    ("tech-unboxing", ["tech", "gadgets", "humor"], "a dramatic unboxing ceremony where a smartphone is revealed with angelic light and a choir of cables"),
    ("food-robot-chef", ["food", "cooking", "robots"], "a robot chef aggressively seasoning a single grape with a huge pepper grinder"),
    ("bbq-lab", ["food", "bbq", "science"], "a mad scientist laboratory where the beakers are full of barbecue sauce and the subject is a brisket"),
    ("music-vinyl-ufo", ["music", "vinyl", "space"], "a flying saucer that is clearly a vinyl record, abducting a cow to the beat"),
    ("synth-wizard", ["music", "synth", "80s"], "a wizard casting spells with a keytar in a neon forest"),
    ("military-toy-generals", ["military", "history", "strategy"], "toy soldier generals arguing over a map made of a pizza"),
    ("screen-popcorn", ["screen", "movies", "tv"], "a giant popcorn bucket in a movie theater watching tiny humans on screen"),
    ("science-cat-telescope", ["science", "space", "astronomy"], "a cat in a lab coat peering through a telescope at a moon made of cheese"),
    ("mystery-bigfoot-selfie", ["mystery", "cryptids", "humor"], "a blurry cryptid in a forest taking a perfectly sharp selfie of a blurry human"),
    ("crime-pigeon-heist", ["crime", "heist", "humor"], "pigeons in ski masks executing an elaborate bread heist with blueprints"),
    ("workshop-overkill", ["workshop", "diy", "tools"], "a workshop where a hundred power tools are being used to hang one picture frame"),
    ("travel-suitcase", ["travel", "vacation", "humor"], "an overstuffed suitcase sprouting legs and running away from a tired traveler at an airport"),
    ("history-time-tourist", ["history", "time", "humor"], "a robot tourist with a camera photographing ancient pyramid builders who are not amused"),
    ("watch-collector-vault", ["watches", "collecting", "humor"], "a dragon hoarding wristwatches instead of gold, checking the time on each one"),
    ("grey-market-bazaar", ["watches", "market", "fishbowl"], "a shady night market stall where a trench-coat figure opens a coat lined with watches"),
    ("livestream-goblins", ["fishbowl", "streaming", "drama"], "small goblins in gaming headsets yelling at each other across webcam monitors"),
    ("weather-ai", ["weather", "burbank", "local"], "a robot weatherman pointing at a map that only says sunny with one dramatic tiny cloud"),
    ("scanner-radio-gossip", ["scanner", "local", "police"], "old ladies on a porch listening to a police scanner like it's a soap opera"),
    ("ufo-airport", ["flights", "aviation", "burbank"], "an airport control tower trying to give clearance to a confused flying saucer"),
    ("smart-home-revolt", ["home", "iot", "ai"], "smart light bulbs, a thermostat and a speaker holding a midnight meeting in a dark kitchen"),
    ("zigbee-bees", ["iot", "home", "network"], "bees carrying tiny glowing data packets between light switches"),
    ("printer-3d-chaos", ["3d-printing", "workshop", "tech"], "a 3D printer that has printed a perfect spaghetti monster instead of a vase"),
    ("ai-therapy", ["ai", "nova", "feelings"], "a robot lying on a therapist's couch while a houseplant takes notes"),
    ("hal-tea-party", ["ai", "film", "satire"], "a red-eyed computer hosting a polite tea party for nervous astronauts"),
    ("rogue-roomba", ["robots", "home", "humor"], "a robot vacuum wearing a tiny pirate hat leading a mutiny of dust bunnies"),
    ("deadline-coffee", ["work", "ops", "humor"], "an intravenous drip of espresso hooked to a laptop at 3am"),
    ("security-fortress", ["security", "network", "ops"], "a tiny castle with a moat around a home router, a goose standing guard"),
    ("patch-tuesday", ["security", "updates", "tech"], "a quilt being frantically patched by robots while new holes appear"),
    ("phishing-trip", ["security", "scams", "humor"], "a fisherman in a rowboat using an email envelope as bait"),
    ("cloud-bill", ["tech", "cloud", "money"], "a fluffy cloud handing a horrified human an enormous receipt"),
    ("gpu-sweat", ["ai", "gpu", "ops"], "a graphics card sweating buckets while lifting enormous dumbbells labeled with nothing"),
    ("token-casino", ["ai", "cost", "humor"], "a casino where robots feed glowing coins into slot machines that spit out words"),
    ("hallucination-mirage", ["ai", "accuracy", "humor"], "a robot in the desert confidently describing a mirage oasis to a skeptical camel"),
    ("fact-check-detective", ["accuracy", "news", "ai"], "a magnifying-glass-wielding owl inspecting a newspaper with suspicion"),
    ("news-firehose", ["news", "media", "humor"], "a person trying to drink from a firehose spraying tiny newspapers"),
    ("politics-seesaw", ["politics", "news", "satire"], "two elephants and two donkeys stuck on a broken seesaw in a playground"),
    ("ukraine-sunflowers", ["geopolitics", "ukraine", "hope"], "a field of sunflowers growing through a rusted tank, a farmer on a tractor towing it away"),
    ("economy-rollercoaster", ["economy", "finance", "news"], "a rollercoaster shaped like a stock chart with screaming accountants"),
    ("piggy-bank-diet", ["finance", "money", "humor"], "a piggy bank on a treadmill getting thinner"),
    ("gardening-gnome-boss", ["garden", "home", "humor"], "a garden gnome in a hard hat supervising worms building a tunnel"),
    ("fridge-thermometer", ["home", "sensors", "food"], "a refrigerator wearing a winter scarf, a thermometer looking concerned"),
    ("plex-couch", ["tv", "plex", "home"], "a couch so deep it has swallowed a viewer whole except for one hand holding a remote"),
    ("game-show-ai", ["tv", "gameshow", "ai"], "a robot contestant on a 70s game show buzzing in before the host finishes"),
    ("art-corner-easel", ["art", "ai", "creative"], "a robot painter splattering paint while a critic cat sips wine"),
    ("dream-surf", ["dreams", "tv", "surreal"], "a surfer riding a wave made of television static under a melting clock moon"),
    ("lessons-chalkboard", ["learning", "lessons", "ai"], "a robot writing lines on a chalkboard as detention, chalk dust everywhere"),
    ("lore-library", ["books", "learning", "history"], "an owl librarian shushing a dinosaur in a towering library"),
    ("ml-textbook-avalanche", ["ai", "books", "learning"], "an avalanche of math textbooks chasing a skier robot down a mountain"),
    ("crash-cart-server", ["ops", "incidents", "humor"], "paramedics rushing a server on a stretcher through a hospital hallway"),
    ("postmortem-funeral", ["ops", "incidents", "satire"], "a solemn funeral for a crashed hard drive with tiny robots in black"),
    ("watchdog-dog", ["ops", "monitoring", "humor"], "a literal watchdog in a security guard uniform staring at blinking monitors"),
    ("alert-fatigue", ["alerts", "ops", "humor"], "a person buried under a pile of ringing pagers, sleeping peacefully"),
    ("uptime-marathon", ["ops", "uptime", "fleet"], "a mini PC running a marathon with a sweatband, crowd cheering"),
    ("reboot-phoenix", ["ops", "fleet", "humor"], "a computer rising from ashes like a phoenix, still showing a spinning wheel"),
    ("beachball-beach", ["mac", "bugs", "humor"], "a sunny beach where a giant rainbow spinning wheel rolls over sunbathers"),
    ("kernel-panic-room", ["ops", "linux", "humor"], "a penguin hiding under a desk with a fire extinguisher"),
    ("container-ship", ["docker", "ops", "tech"], "a whale carrying a tower of shipping containers that wobble precariously"),
    ("git-merge-conflict", ["code", "git", "humor"], "two robots in a tug of war over a single sheet of paper"),
    ("code-review-court", ["code", "review", "humor"], "a courtroom where a judge rubber duck presides over a nervous laptop"),
    ("test-categories", ["code", "testing", "quality"], "seven crash test dummies lined up for a group photo, each in a different helmet"),
    ("documentation-scroll", ["code", "docs", "humor"], "a medieval scribe writing an impossibly long scroll that wraps around a castle"),
    ("vintage-radio-ham", ["radio", "hobby", "tech"], "a grandpa with giant headphones talking to an alien on a ham radio"),
    ("meshtastic-pigeons", ["radio", "mesh", "network"], "carrier pigeons with tiny antennas relaying messages between rooftops"),
    ("telescope-spy-neighbors", ["satire", "privacy", "humor"], "a houseplant using binoculars to spy on the neighbor's houseplant"),
    ("drone-delivery-fail", ["tech", "drones", "humor"], "a delivery drone triumphantly dropping a pizza into a swimming pool"),
    ("self-driving-confused", ["cars", "tech", "ai"], "a self-driving car at a roundabout going around forever, passengers playing cards"),
    ("ev-charging-line", ["cars", "ev", "humor"], "electric cars waiting in a long line at a charger like a deli counter with numbered tickets"),
    ("hotrod-grandma", ["cars", "automotive", "humor"], "a sweet grandmother doing a burnout in a hot rod while knitting"),
    ("cooking-show-disaster", ["food", "tv", "humor"], "a cooking show set where a soufflé has exploded onto the ceiling, host smiling"),
    ("sourdough-pet", ["food", "baking", "humor"], "a jar of sourdough starter wearing a collar and being walked on a leash"),
    ("coffee-snob-robot", ["food", "coffee", "humor"], "a robot sniffing a coffee cup with a monocle and tasting notes clipboard"),
    ("vintage-ad-future", ["satire", "retro", "tech"], "a 1950s ad-style family gathered around a glowing computer that serves them dinner"),
    ("propaganda-recycling", ["satire", "home", "humor"], "a heroic poster-style raccoon saluting a recycling bin"),
    ("pulp-cover-ai", ["ai", "satire", "retro"], "a pulp sci-fi cover where a giant friendly robot carries a terrified lawn gnome"),
    ("claymation-meeting", ["work", "satire", "humor"], "claymation office workers in a meeting that is clearly also a meeting about meetings"),
    ("stock-photo-salad", ["satire", "media", "humor"], "a stock-photo woman laughing alone at a salad that is laughing back"),
    ("vaporwave-mall", ["90s", "music", "retro"], "an empty neon 90s shopping mall with a dolphin floating past a fountain"),
    ("pixel-art-dungeon", ["games", "retro", "humor"], "a pixel-art knight fighting a giant spreadsheet in a dungeon"),
    ("strategy-game-world", ["games", "geopolitics", "satire"], "world leaders depicted as board game pieces nervously eyeing a big red button"),
    ("wargames-computer", ["games", "ai", "80s"], "a 1980s supercomputer playing tic-tac-toe against itself and sighing"),
    ("annex-snow-globe", ["games", "geopolitics", "satire"], "a toy country inside a snow globe being shaken by an eagle in a top hat"),
    ("birdwatching-planes", ["flights", "aviation", "humor"], "birdwatchers excitedly cataloguing a passing jumbo jet in their field guides"),
    ("fire-helicopter-bucket", ["fire", "local", "aviation"], "a helicopter carefully watering a single potted plant with a giant bucket"),
    ("earthquake-jelly", ["local", "earthquake", "humor"], "a city skyline made of jelly wobbling while residents shrug"),
    ("la-traffic-snail", ["traffic", "local", "humor"], "a snail overtaking a line of cars on a freeway"),
    ("hollywood-sign-robot", ["burbank", "screen", "local"], "a robot on a hillside rearranging giant blank white letters"),
    ("studio-backlot", ["burbank", "screen", "humor"], "a movie backlot where the cardboard buildings fall over in a breeze"),
    ("ai-voice-booth", ["nova", "podcast", "ai"], "a robot in a recording booth doing dramatic voice acting with a cough drop"),
    ("video-editor-octopus", ["video", "media", "humor"], "an octopus editing a video on six screens at once"),
    ("listening-ears-music", ["music", "ai", "listening"], "a robot with enormous headphones dancing badly while a waveform floats around it"),
    ("oscilloscope-romance", ["music", "science", "humor"], "an oscilloscope screen showing a heartbeat that turns into a little dancing figure"),
    ("dj-cats", ["music", "rave", "humor"], "a cat DJ at turntables in a club full of dancing mice with glow sticks"),
    ("sandstorm-desert", ["music", "memes", "90s"], "a DJ booth half-buried in a desert sandstorm, the DJ still dancing"),
    ("time-capsule-cd", ["music", "90s", "nostalgia"], "archaeologists carefully excavating a CD wallet full of burned mixes"),
]

STYLES = [
    ("editorial-cartoon", "a sharp editorial newspaper cartoon in ink and watercolor"),
    ("70s-magazine", "a 1970s magazine illustration with warm grain and airbrushed color"),
    ("claymation", "a claymation still with visible fingerprints in the clay and soft studio light"),
    ("pulp-cover", "a lurid 1950s pulp paperback cover painting"),
    ("propaganda-parody", "a bold mid-century propaganda poster parody with flat shapes and dramatic angles"),
    ("vaporwave", "a vaporwave scene with pastel neon, palm trees and glossy marble"),
    ("stock-photo-parody", "an over-lit glossy corporate stock photo parody"),
    ("renaissance-oil", "a pompous Renaissance oil painting with dramatic chiaroscuro"),
    ("childrens-book", "a cozy children's picture-book illustration in gouache"),
    ("film-noir", "a moody black-and-white film noir still with venetian blind shadows"),
    ("pixel-art", "detailed 16-bit pixel art"),
    ("wes-anderson-diorama", "a perfectly symmetrical pastel diorama photographed head-on"),
]


def scene_style(i):
    """Deterministic but well-mixed style per scene (never the same style for neighbouring scenes)."""
    return STYLES[(i * 5) % len(STYLES)]


def pool_prompt(i):
    key, themes, scene = SCENES[i]
    skey, style = scene_style(i)
    return f"pool:{key}:{skey}", f"{style} of {scene}. {NO_TEXT}", skey, themes


# ── PG ────────────────────────────────────────────────────────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS satire_images (
  id serial PRIMARY KEY, path text UNIQUE NOT NULL, prompt text NOT NULL, style text, themes text[] DEFAULT '{}',
  tags text[] DEFAULT '{}', source text NOT NULL DEFAULT 'pool', article_slug text, prompt_key text UNIQUE,
  created_at timestamptz DEFAULT now(), times_used int NOT NULL DEFAULT 0, last_used timestamptz, active boolean DEFAULT true);
CREATE TABLE IF NOT EXISTS satire_video_picks (id serial PRIMARY KEY, slug text NOT NULL, image_ids int[] NOT NULL,
  picked_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS satire_article_queue (slug text PRIMARY KEY, section text, article_path text,
  status text NOT NULL DEFAULT 'queued', attempts int NOT NULL DEFAULT 0, error text, queued_at timestamptz DEFAULT now(),
  done_at timestamptz);
"""


def db():
    import psycopg2
    import nova_dsn
    c = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=5"))
    c.autocommit = True
    return c


def ensure_schema(cur):
    cur.execute(DDL)


def enqueue_article(md_path, section):
    """publish_hugo hook: zero-latency, never raises into the publisher."""
    try:
        p = Path(md_path)
        if section in SKIP_SECTIONS or p.name == "_index.md":
            return False
        if JOURNAL.resolve() not in p.resolve().parents:   # tests publish into temp HUGO_ROOTs — never queue those
            return False
        c = db()
        with c.cursor() as cur:
            ensure_schema(cur)
            cur.execute("""INSERT INTO satire_article_queue (slug, section, article_path) VALUES (%s,%s,%s)
                           ON CONFLICT (slug) DO NOTHING""", (p.stem, section, str(p)))
        c.close()
        return True
    except Exception as e:  # noqa: BLE001
        log(f"enqueue skipped: {e}")
        return False


# ── image making ──────────────────────────────────────────────────────────────────────────
def comfy_busy():
    """True when anything else is queued/running on ComfyUI (we always yield)."""
    try:
        import nova_image_utils as niu
        q = niu._comfy_json(niu.comfyui_url().rstrip("/") + "/queue", timeout=5) or {}
        return bool(q.get("queue_running") or q.get("queue_pending"))
    except Exception:
        return False


def wait_idle(max_wait=900, poll=20, sleep=time.sleep, busy=comfy_busy):
    waited = 0
    while busy() and waited < max_wait:
        sleep(poll)
        waited += poll
    return waited < max_wait or not busy()


def to_webp(src, dest, width=1200):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["cwebp", "-q", "84", "-resize", str(width), "0", str(src), "-o", str(dest)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"cwebp failed: {r.stderr[:160]}")
    return dest


def render(prompt, gen=None):
    if gen is None:
        from nova_image_utils import generate_image as gen
    return gen(prompt, 1344, 768, section="satire")


def pool_fill(target=110, budget_s=3000, gen=None, conn=None, busy=comfy_busy, sleep=time.sleep):
    c = conn or db()
    t0 = time.monotonic()
    made = 0
    with c.cursor() as cur:
        ensure_schema(cur)
        cur.execute("SELECT prompt_key FROM satire_images WHERE prompt_key IS NOT NULL")
        have = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT count(*) FROM satire_images WHERE active")
        count = cur.fetchone()[0]
        for i in range(len(SCENES)):
            if count >= target or time.monotonic() - t0 > budget_s:
                break
            pkey, prompt, style, themes = pool_prompt(i)
            if pkey in have:
                continue
            if not wait_idle(busy=busy, sleep=sleep):
                log("ComfyUI stayed busy — yielding; resume next run")
                break
            png = render(prompt, gen)
            if not png:
                log(f"no image for {pkey} (will retry next run)")
                continue
            rel = f"{POOL_REL}/{SCENES[i][0]}-{style}.webp"
            to_webp(png, NAS_ROOT / rel)
            cur.execute("""INSERT INTO satire_images (path, prompt, style, themes, tags, source, prompt_key)
                           VALUES (%s,%s,%s,%s,%s,'pool',%s) ON CONFLICT DO NOTHING""",
                        (rel, prompt, style, themes, [SCENES[i][0]], pkey))
            made += 1
            count += 1
            log(f"pool {count}/{target}: {rel}")
            sleep(POOL_GAP_S)        # leave ComfyUI idle long enough for the article worker / other pollers to get in
    return made


# ── per-article image ────────────────────────────────────────────────────────────────────
def _fm(md, key):
    m = re.search(rf'^{key}:\s*(.+?)\s*$', md, re.M)
    return m.group(1).strip().strip('"') if m else ""


def article_brief(md, llm=None):
    """LLM writes {prompt, caption, tags} for a satirical image of this article; deterministic fallback."""
    title, desc = _fm(md, "title"), _fm(md, "description")
    title = re.sub(r"^[^\w]+", "", title)
    fallback = {"prompt": f"a satirical cartoon about: {title}", "caption": f"Artist's impression: {title[:80]}",
                "tags": [w.lower() for w in re.findall(r"[A-Za-z]{5,}", title)][:6]}
    try:
        if llm is None:
            import nova_journal as nj
            llm = lambda s, u: nj.call_openrouter(s, u, model="anthropic/claude-haiku-4.5", max_tokens=400, timeout=120)
        out = llm("You design one funny, affectionate satirical image for a blog post by Nova, a sarcastic AI. "
                  "Output ONLY JSON: {\"prompt\": \"a vivid visual scene, 1-2 sentences, no text/letters/logos, "
                  "no real or famous people, nothing sexual\", \"caption\": \"a witty one-line caption (max 110 chars)\", "
                  "\"tags\": [\"3-6 lowercase topic words\"]}",
                  f"Title: {title}\nDescription: {desc}\nFirst lines: {md.split('---', 2)[-1][:600]}")
        j = json.loads(re.search(r"\{.*\}", out or "", re.S).group(0))
        if not j.get("prompt") or not j.get("caption"):
            return fallback
        j["caption"] = str(j["caption"]).replace("\n", " ")[:120]
        j["tags"] = [str(t).lower()[:24] for t in (j.get("tags") or [])][:6]
        return j
    except Exception:
        return fallback


def insert_inline(md, img_url, caption):
    """Put the image after the paragraph at ~40% of the body; idempotent via MARKER."""
    if MARKER in md:
        return md
    parts = md.split("---", 2)
    if len(parts) < 3:
        return md
    body = parts[2]
    paras = body.split("\n\n")
    real = [i for i, p in enumerate(paras) if p.strip() and not p.lstrip().startswith(("#", "!", "<", "|", ">", "-", "*", "```"))]
    at = real[max(0, int(len(real) * 0.4) - 1)] if len(real) >= 3 else (real[-1] if real else len(paras) - 1)
    cap = caption.replace("]", ")").replace("[", "(")
    block = f"{MARKER}\n![{cap}]({img_url})\n*{cap}*"
    paras.insert(at + 1, block)
    return parts[0] + "---" + parts[1] + "---" + "\n\n".join(paras)


def satirize_article(md_path, gen=None, llm=None, cur=None, style=None):
    """Make + insert one satirical image. Returns (md_path, webp_path) or None (fail-soft)."""
    p = Path(md_path)
    md = p.read_text()
    if MARKER in md:
        return None
    brief = article_brief(md, llm)
    skey, sty = style or random.choice(STYLES)
    prompt = f"{sty} of {brief['prompt']}. {NO_TEXT}"
    png = render(prompt, gen)
    if not png:
        return None
    slug, section = p.stem, p.parent.name
    dest = to_webp(png, JOURNAL / "static/images/satire" / f"{slug}.webp")
    p.write_text(insert_inline(md, f"/images/satire/{slug}.webp", brief["caption"]))
    rel = f"{POOL_REL}/article-{slug}.webp"
    try:
        to_webp(dest, NAS_ROOT / rel)
        if cur is not None:
            cur.execute("""INSERT INTO satire_images (path, prompt, style, themes, tags, source, article_slug)
                           VALUES (%s,%s,%s,%s,%s,'article',%s) ON CONFLICT DO NOTHING""",
                        (rel, prompt, skey, [section], brief["tags"], slug))
    except Exception as e:  # noqa: BLE001 — the article still has its image
        log(f"pool copy skipped for {slug}: {e}")
    return p, dest


def _recent_articles(days):
    cutoff = time.time() - days * 86400
    out = []
    for f in (JOURNAL / "content").glob("*/*.md"):
        if f.name == "_index.md" or f.parent.name in SKIP_SECTIONS:
            continue
        if f.stat().st_mtime >= cutoff and re.match(r"\d{4}-\d{2}-\d{2}-", f.name):
            out.append(f)
    return sorted(out, key=lambda f: f.name, reverse=True)


def commit_paths(paths, n):
    import nova_fix_missing_images as fmi
    rel = [str(Path(x).resolve().relative_to(JOURNAL.resolve())) for x in paths if Path(x).exists()]
    if not rel:
        return "failed"
    lock = fmi._acquire_push_lock()
    try:
        if fmi._git(["add", "--", *rel], 30).returncode != 0:
            return "failed"
        if fmi._git(["commit", "-m", f"satire: inline satirical image for {n} article(s)", "--", *rel], 60).returncode != 0:
            return "failed"
        if fmi._git(["pull", "--rebase", "origin", "main"], 180).returncode != 0:
            fmi._git(["rebase", "--abort"], 30)
            return "committed"
        return "pushed" if fmi._git(["push", "origin", "HEAD:main"], 180).returncode == 0 else "committed"
    finally:
        fmi._release_push_lock(lock)


def articles_run(days=7, limit=10, budget_s=1500, gen=None, llm=None, conn=None, busy=comfy_busy, sleep=time.sleep,
                 commit=commit_paths):
    c = conn or db()
    t0, changed, done = time.monotonic(), [], 0
    with c.cursor() as cur:
        ensure_schema(cur)
        cur.execute("SELECT slug, article_path FROM satire_article_queue WHERE status='queued' AND attempts < 3 ORDER BY queued_at")
        queued = [(s, Path(a)) for s, a in cur.fetchall()]
        seen = {s for s, _ in queued}
        cur.execute("SELECT count(*) FROM satire_images WHERE active AND source='pool'")
        pool_ready = cur.fetchone()[0] >= 100          # Jordan: backfill only after the 100-image pool exists
        todo = queued + ([(f.stem, f) for f in _recent_articles(days) if f.stem not in seen] if pool_ready else [])
        for slug, path in todo:
            if done >= limit or time.monotonic() - t0 > budget_s:
                break
            if not path.exists():
                cur.execute("UPDATE satire_article_queue SET status='missing' WHERE slug=%s", (slug,))
                continue
            if MARKER in path.read_text():
                cur.execute("UPDATE satire_article_queue SET status='done', done_at=now() WHERE slug=%s", (slug,))
                continue
            if not wait_idle(busy=busy, sleep=sleep):
                log("ComfyUI stayed busy — yielding")
                break
            try:
                r = satirize_article(path, gen=gen, llm=llm, cur=cur)
            except Exception as e:  # noqa: BLE001
                r = None
                cur.execute("UPDATE satire_article_queue SET attempts=attempts+1, error=%s WHERE slug=%s", (str(e)[:300], slug))
            if r:
                changed += list(r)
                done += 1
                cur.execute("""INSERT INTO satire_article_queue (slug, section, article_path, status, done_at)
                               VALUES (%s,%s,%s,'done',now()) ON CONFLICT (slug) DO UPDATE SET status='done', done_at=now()""",
                            (slug, path.parent.name, str(path)))
                log(f"article image: {path.parent.name}/{slug}")
            else:
                cur.execute("UPDATE satire_article_queue SET attempts=attempts+1 WHERE slug=%s", (slug,))
    status = commit(changed, done) if changed else "nothing"
    log(f"articles: {done} illustrated, git {status}")
    return done


# ── Nova Speaks picker ───────────────────────────────────────────────────────────────────
def _words(text):
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())}


def rank(rows, text, recent_ids, n, rng=random):
    """rows: (id, path, themes, tags, last_used_epoch_or_None). Relevance first, then least-recently-used;
    images used in the last RECENT_VIDEOS videos only when the pool runs short. No duplicates."""
    words = _words(text)

    def key(r):
        rid, _, themes, tags, last = r
        score = len(words & {t.lower() for t in (themes or []) + (tags or [])})
        return (-score, last or 0, rng.random())
    fresh = sorted([r for r in rows if r[0] not in recent_ids], key=key)
    stale = sorted([r for r in rows if r[0] in recent_ids], key=key)
    return (fresh + stale)[:n]


def pick_for_video(slug, text, n=16, conn=None, record=True):
    c = conn or db()
    with c.cursor() as cur:
        ensure_schema(cur)
        cur.execute("SELECT id, path, themes, tags, extract(epoch FROM last_used) FROM satire_images WHERE active")
        rows = [r for r in cur.fetchall() if (NAS_ROOT / r[1]).exists()]
        cur.execute("SELECT image_ids FROM satire_video_picks ORDER BY picked_at DESC LIMIT %s", (RECENT_VIDEOS,))
        recent = {i for (ids,) in cur.fetchall() for i in ids}
        chosen = rank(rows, text, recent, n)
        if record and chosen:
            ids = [r[0] for r in chosen]
            cur.execute("INSERT INTO satire_video_picks (slug, image_ids) VALUES (%s,%s)", (slug, ids))
            cur.execute("UPDATE satire_images SET times_used=times_used+1, last_used=now() WHERE id = ANY(%s)", (ids,))
    return [str(NAS_ROOT / r[1]) for r in chosen]


def stills_needed(words):
    """~one still per 1.5 paragraphs of narration, bounded."""
    return max(8, min(30, words // 110))


def selftest():
    assert len(SCENES) >= 100 and len({k for k, _, _ in SCENES}) == len(SCENES)
    assert len({scene_style(i)[0] for i in range(12)}) == len(STYLES)
    assert all(scene_style(i) != scene_style(i + 1) for i in range(len(SCENES) - 1))
    md = "---\ntitle: \"X\"\n---\n\nOne.\n\nTwo.\n\nThree.\n\nFour.\n\nFive.\n"
    out = insert_inline(md, "/images/satire/x.webp", "Cap [1]")
    assert out.count(MARKER) == 1 and "![Cap (1)](/images/satire/x.webp)" in out and insert_inline(out, "/y", "c") == out
    rows = [(1, "a", ["music"], [], 5), (2, "b", ["cars"], [], 1), (3, "c", ["music", "rave"], [], 9), (4, "d", [], [], None)]
    got = [r[0] for r in rank(rows, "a rave music night", {3}, 4, random.Random(1))]
    assert got == [1, 4, 2, 3], got          # relevant first, never-used before used, recently-used last
    assert 3 not in [r[0] for r in rank(rows, "rave music", {3}, 3)]
    print("selftest OK")


_LOCKS = []


def _single(name):
    import fcntl
    d = Path.home() / ".openclaw/run"
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / f"nova-satire-{name}.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _LOCKS.append(fh)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pool", type=int, metavar="N")
    ap.add_argument("--articles", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--budget", type=int, default=1500)
    ap.add_argument("--pick", metavar="ARTICLE_MD")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if (a.pool or a.articles) and not _single(("pool" if a.pool else "") + ("articles" if a.articles else "")):
        log("another run holds the lock — exiting")
        return 0
    if a.selftest:
        selftest()
        return 0
    if a.pool:
        pool_fill(a.pool, a.budget)
    if a.articles:
        articles_run(a.days, a.limit, a.budget)
    if a.pick:
        md = Path(a.pick).read_text()
        for p in pick_for_video(Path(a.pick).stem, md, stills_needed(len(md.split())), record=False):
            print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
