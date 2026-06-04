#!/usr/bin/env python3
"""
nova_scifi_scripts_ingest.py — Ingest top sci-fi movie scripts into Nova's memory.

Fetches publicly available screenplays for the top 100 sci-fi movies of all time,
classifies each into the appropriate vector (sci_fi, horror, crime_drama, etc.),
chunks and ingests into Nova's PostgreSQL vector DB.

Sources: IMSDB (Internet Movie Script Database), Simply Scripts, Script Slug.
Each completed ingest posts to #nova-notifications with a random memory snippet.
Notifications go to #nova-notifications (C0ATAF7NZG9), NOT #nova-chat.

PRIVACY: All screenplay content is public domain / publicly available.

Written by Jordan Koch.
"""

import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from html.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

MEMORY_URL      = nova_config.VECTOR_URL   # already includes /remember
SLACK_CHANNEL   = nova_config.SLACK_NOTIFY  # C0ATAF7NZG9 = #nova-notifications
STATE_FILE      = Path.home() / ".openclaw/workspace/state/scifi_scripts_state.json"
LOG_FILE        = Path.home() / ".openclaw/logs/nova_scifi_scripts_ingest.log"
CHUNK_WORDS     = 400
MIN_CHUNK_WORDS = 40
PAGE_DELAY      = 2.0   # seconds between requests — polite

# ── Top 100 Sci-Fi Movies with genre classification ──────────────────────────

MOVIES = [
    # (title, year, vector, imsdb_slug)
    ("2001: A Space Odyssey",           1968, "sci_fi",      "2001-A-Space-Odyssey"),
    ("Blade Runner",                    1982, "sci_fi",      "Blade-Runner"),
    ("Metropolis",                      1927, "sci_fi",      None),
    ("The Matrix",                      1999, "sci_fi",      "Matrix,-The"),
    ("Aliens",                          1986, "sci_fi",      "Aliens"),
    ("Alien",                           1979, "horror",      "Alien"),
    ("E.T. the Extra-Terrestrial",      1982, "sci_fi",      "E.T."),
    ("Star Wars",                       1977, "sci_fi",      "Star-Wars"),
    ("The Empire Strikes Back",         1980, "sci_fi",      "Empire-Strikes-Back,-The"),
    ("Terminator 2: Judgment Day",      1991, "sci_fi",      "Terminator-2-Judgment-Day"),
    ("The Terminator",                  1984, "sci_fi",      "Terminator,-The"),
    ("Jurassic Park",                   1993, "sci_fi",      "Jurassic-Park"),
    ("Interstellar",                    2014, "sci_fi",      "Interstellar"),
    ("Arrival",                         2016, "sci_fi",      "Arrival"),
    ("Inception",                       2010, "sci_fi",      "Inception"),
    ("The Thing",                       1982, "horror",      "Thing,-The"),
    ("Back to the Future",              1985, "sci_fi",      "Back-to-the-Future"),
    ("Close Encounters of the Third Kind", 1977, "sci_fi",   "Close-Encounters-of-the-Third-Kind"),
    ("Solaris",                         1972, "sci_fi",      None),
    ("Brazil",                          1985, "sci_fi",      "Brazil"),
    ("Total Recall",                    1990, "sci_fi",      "Total-Recall"),
    ("RoboCop",                         1987, "sci_fi",      "RoboCop"),
    ("The Fly",                         1986, "horror",      "Fly,-The"),
    ("Videodrome",                      1983, "horror",      "Videodrome"),
    ("eXistenZ",                        1999, "sci_fi",      "eXistenZ"),
    ("Annihilation",                    2018, "horror",      "Annihilation"),
    ("Ex Machina",                      2014, "sci_fi",      "Ex-Machina"),
    ("Moon",                            2009, "sci_fi",      "Moon"),
    ("Gravity",                         2013, "sci_fi",      "Gravity"),
    ("The Martian",                     2015, "sci_fi",      "Martian,-The"),
    ("District 9",                      2009, "sci_fi",      "District-9"),
    ("Children of Men",                 2006, "sci_fi",      "Children-of-Men"),
    ("Eternal Sunshine of the Spotless Mind", 2004, "drama", "Eternal-Sunshine-of-the-Spotless-Mind"),
    ("Her",                             2013, "drama",       "Her"),
    ("Under the Skin",                  2013, "sci_fi",      None),
    ("A.I. Artificial Intelligence",    2001, "sci_fi",      "A.I.-Artificial-Intelligence"),
    ("Minority Report",                 2002, "sci_fi",      "Minority-Report"),
    ("Looper",                          2012, "sci_fi",      "Looper"),
    ("Predator",                        1987, "sci_fi",      "Predator"),
    ("The Fifth Element",               1997, "sci_fi",      "Fifth-Element,-The"),
    ("Galaxy Quest",                    1999, "comedy",      "Galaxy-Quest"),
    ("WALL-E",                          2008, "sci_fi",      None),
    ("Contact",                         1997, "sci_fi",      "Contact"),
    ("Gattaca",                         1997, "sci_fi",      "Gattaca"),
    ("Never Let Me Go",                 2010, "drama",       "Never-Let-Me-Go"),
    ("Planet of the Apes",              1968, "sci_fi",      "Planet-of-the-Apes"),
    ("Logan's Run",                     1976, "sci_fi",      None),
    ("Soylent Green",                   1973, "sci_fi",      None),
    ("THX 1138",                        1971, "sci_fi",      None),
    ("Akira",                           1988, "sci_fi",      None),
    ("Ghost in the Shell",              1995, "sci_fi",      None),
    ("The Day the Earth Stood Still",   1951, "sci_fi",      None),
    ("Forbidden Planet",                1956, "sci_fi",      None),
    ("Them!",                           1954, "horror",      None),
    ("Invasion of the Body Snatchers",  1956, "horror",      "Invasion-of-the-Body-Snatchers"),
    ("War of the Worlds",               1953, "sci_fi",      None),
    ("Twelve Monkeys",                  1995, "sci_fi",      "Twelve-Monkeys"),
    ("Dark City",                       1998, "sci_fi",      "Dark-City"),
    ("Event Horizon",                   1997, "horror",      "Event-Horizon"),
    ("Sphere",                          1998, "sci_fi",      "Sphere"),
    ("Pi",                              1998, "sci_fi",      "Pi"),
    ("Strange Days",                    1995, "sci_fi",      "Strange-Days"),
    ("The Road",                        2009, "drama",       "Road,-The"),
    ("Oblivion",                        2013, "sci_fi",      None),
    ("Edge of Tomorrow",                2014, "sci_fi",      None),
    ("Dredd",                           2012, "sci_fi",      None),
    ("Source Code",                     2011, "sci_fi",      "Source-Code"),
    ("In Time",                         2011, "sci_fi",      None),
    ("Elysium",                         2013, "sci_fi",      None),
    ("I, Robot",                        2004, "sci_fi",      "I,-Robot"),
    ("Equilibrium",                     2002, "sci_fi",      "Equilibrium"),
    ("V for Vendetta",                  2005, "sci_fi",      "V-for-Vendetta"),
    ("X-Men",                           2000, "sci_fi",      "X-Men"),
    ("Iron Man",                        2008, "sci_fi",      None),
    ("Avengers: Endgame",               2019, "sci_fi",      None),
    ("Spider-Man: Into the Spider-Verse", 2018, "sci_fi",    None),
    ("Guardians of the Galaxy",         2014, "sci_fi",      None),
    ("Pacific Rim",                     2013, "sci_fi",      None),
    ("Transformers",                    2007, "sci_fi",      None),
    ("Cloverfield",                     2008, "horror",      None),
    ("Prometheus",                      2012, "sci_fi",      "Prometheus"),
    ("Avatar",                          2009, "sci_fi",      "Avatar"),
    ("Sunshine",                        2007, "sci_fi",      "Sunshine"),
    ("Primer",                          2004, "sci_fi",      "Primer"),
    ("Another Earth",                   2011, "drama",       None),
    ("Safety Not Guaranteed",           2012, "comedy",      None),
    ("The One I Love",                  2014, "sci_fi",      None),
    ("Coherence",                       2013, "sci_fi",      None),
    ("Upstream Color",                  2013, "sci_fi",      None),
    ("These Final Hours",               2013, "drama",       None),
    ("Predestination",                  2014, "sci_fi",      None),
    ("Timecrimes",                      2007, "sci_fi",      None),
    ("Attack the Block",                2011, "sci_fi",      None),
    ("The Host",                        2006, "horror",      None),
    ("Snowpiercer",                     2013, "sci_fi",      None),
    ("Mad Max: Fury Road",              2015, "sci_fi",      None),
    ("1984",                            1984, "sci_fi",      None),
    ("A Clockwork Orange",              1971, "crime_drama",  "A-Clockwork-Orange"),
    ("Fahrenheit 451",                  1966, "sci_fi",      None),
    ("Repo Man",                        1984, "sci_fi",      None),
    ("They Live",                       1988, "sci_fi",      "They-Live"),
    ("Dune",                            2021, "sci_fi",      None),
]


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[scifi_ingest {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"done": {}}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Script fetching ───────────────────────────────────────────────────────────

class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "header", "footer"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "header", "footer"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)

    @property
    def text(self) -> str:
        return " ".join(self._parts)


def fetch_url(url: str, retries: int = 3) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
    }
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                # Try UTF-8 first, fall back to latin-1
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("latin-1", errors="ignore")
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep(PAGE_DELAY * (attempt + 1))
    return None


def fetch_imsdb_script(slug: str) -> str | None:
    """Fetch screenplay text from IMSDB."""
    url = f"https://imsdb.com/scripts/{slug}.html"
    html = fetch_url(url)
    if not html:
        return None
    # IMSDB wraps the script in <td class="scrtext"> or <pre>
    match = re.search(r'<pre>(.*?)</pre>', html, re.DOTALL | re.IGNORECASE)
    if not match:
        match = re.search(r'class="scrtext".*?<pre>(.*?)</pre>', html, re.DOTALL | re.IGNORECASE)
    if match:
        text = re.sub(r'<[^>]+>', ' ', match.group(1))
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text if len(text) > 500 else None
    return None


def fetch_script(title: str, imsdb_slug: str | None) -> str | None:
    """Try multiple sources to get the screenplay text."""
    # 1. IMSDB (best source)
    if imsdb_slug:
        text = fetch_imsdb_script(imsdb_slug)
        if text:
            log(f"  Got script from IMSDB: {len(text.split())} words")
            return text
        time.sleep(PAGE_DELAY)

    # 2. Try IMSDB with auto-slugified title
    auto_slug = title.replace(" ", "-").replace(":", "").replace("'", "").replace(",", "")
    if auto_slug != imsdb_slug:
        text = fetch_imsdb_script(auto_slug)
        if text:
            log(f"  Got script from IMSDB (auto): {len(text.split())} words")
            return text
        time.sleep(PAGE_DELAY)

    # 3. Try Script Slug
    slug = title.lower().replace(" ", "-").replace(":", "").replace("'", "").replace(",", "")
    url = f"https://scriptslug.com/asset/uploads/scripts/{slug}.pdf"
    # PDF is binary - skip, go to next

    # 4. Simply Scripts
    url = f"https://www.simplyscripts.com/scripts/{slug.replace('-',' ')}.html"
    html = fetch_url(url)
    if html and len(html) > 5000:
        parser = HTMLTextExtractor()
        parser.feed(html)
        text = parser.text
        if len(text.split()) > 500:
            log(f"  Got script from Simply Scripts: {len(text.split())} words")
            return text
        time.sleep(PAGE_DELAY)

    return None


# ── Memory ingestion ──────────────────────────────────────────────────────────

def chunk_text(text: str) -> list[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), CHUNK_WORDS):
        chunk = " ".join(words[i:i + CHUNK_WORDS])
        if len(chunk.split()) >= MIN_CHUNK_WORDS:
            chunks.append(chunk)
    return chunks


def remember(text: str, source: str, metadata: dict) -> bool:
    payload = json.dumps({
        "text": nova_config.truncate_at_boundary(text),
        "source": source,
        "tier": "long_term",
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception:
        return False


def random_memory_for_movie(title: str) -> str | None:
    """Pull a random existing memory about this movie."""
    payload = json.dumps({
        "query": f"{title} movie screenplay film",
        "limit": 10,
    }).encode()
    req = urllib.request.Request(
        nova_config.VECTOR_URL.replace("/remember", "/recall"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            memories = data.get("memories", data.get("results", []))
            if memories:
                m = random.choice(memories)
                text = re.sub(r"^\[.*?\]\s*", "", m.get("text", ""))
                return text[:200].strip()
    except Exception:
        pass
    return None


# ── Slack notification ────────────────────────────────────────────────────────

def post_slack(msg: str):
    try:
        nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)
    except Exception as exc:
        log(f"Slack error: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log(f"=== Sci-Fi Scripts Ingest started — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    state = load_state()
    total_ingested = 0
    total_chunks = 0
    skipped = 0
    not_found = 0

    post_slack(
        f":clapper: *Sci-Fi Movie Scripts Ingest — Starting*\n"
        f":film_frames: {len(MOVIES)} movies queued · sources: IMSDB, Simply Scripts\n"
        f":brain: Results → #nova-notifications per film"
    )

    for i, (title, year, vector, imsdb_slug) in enumerate(MOVIES):
        key = f"{title}_{year}"

        if key in state["done"]:
            skipped += 1
            continue

        log(f"[{i+1}/{len(MOVIES)}] {title} ({year}) → {vector}")

        script_text = fetch_script(title, imsdb_slug)

        if not script_text:
            log(f"  No script found — skipping")
            state["done"][key] = {
                "title": title, "year": year, "status": "not_found"
            }
            save_state(state)
            not_found += 1
            time.sleep(PAGE_DELAY)
            continue

        chunks = chunk_text(script_text)
        ingested = 0
        for j, chunk in enumerate(chunks):
            if remember(
                f"[{title} ({year}) screenplay] {chunk}",
                vector,
                {
                    "type": "movie_screenplay",
                    "title": title,
                    "year": year,
                    "chunk": j + 1,
                    "total_chunks": len(chunks),
                    "ingested_date": datetime.now().strftime("%Y-%m-%d"),
                },
            ):
                ingested += 1

        state["done"][key] = {
            "title": title, "year": year, "vector": vector,
            "status": "ingested", "chunks": ingested,
        }
        save_state(state)
        total_ingested += 1
        total_chunks += ingested
        log(f"  ✓ {ingested} chunks → {vector}")

        # Per-film notification to #nova-notifications
        memory_snippet = random_memory_for_movie(title)
        if not memory_snippet and chunks:
            memory_snippet = re.sub(r"^\[.*?\]\s*", "", random.choice(chunks))[:200]

        notif_lines = [
            f":film_strip: *{title}* ({year}) → `{vector}`",
            f":brain: {ingested} memories stored · {len(script_text.split()):,} words",
        ]
        if memory_snippet:
            notif_lines.append(f":thought_balloon: _\"{memory_snippet[:180]}…\"_")
        post_slack("\n".join(notif_lines))

        time.sleep(PAGE_DELAY)

    # Final summary
    post_slack(
        f":clapper: *Sci-Fi Scripts Ingest — Complete*\n"
        f":white_check_mark: {total_ingested} scripts ingested · {total_chunks:,} memory chunks\n"
        f":x: {not_found} scripts not found · :fast_forward: {skipped} already done"
    )
    log(f"=== Complete: {total_ingested} ingested, {not_found} not found, {skipped} skipped ===")


if __name__ == "__main__":
    main()
