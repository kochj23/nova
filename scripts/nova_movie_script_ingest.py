#!/usr/bin/env python3
"""
nova_movie_script_ingest.py — Ingest movie scripts/data into Nova's vector memory.

Sources: IMSDb (full scripts), Wikipedia (plot, cast, production, reception)
Target: Nova vector memory (pgvector) at http://127.0.0.1:18790
Notifications: Slack #nova-notifications every 5 minutes

This script handles a single franchise. The orchestrator (nova_movie_ingest_all.sh)
spawns parallel instances.

Usage:
  python3 nova_movie_script_ingest.py --franchise john_wick
  python3 nova_movie_script_ingest.py --franchise batman --dry-run

Written by Jordan Koch.
"""

import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread, Event

sys.path.insert(0, str(Path(__file__).parent))
try:
    import nova_config
    HAS_NOVA_CONFIG = True
except Exception:
    HAS_NOVA_CONFIG = False

# ── Config ────────────────────────────────────────────────────────────────────

VECTOR_URL = "http://127.0.0.1:18790/remember?async=1"
IMSDB_BASE = "https://imsdb.com"
WIKI_API = "https://en.wikipedia.org/w/api.php"
STATUS_INTERVAL = 300
LOG_DIR = Path("/tmp/nova-movie-ingest")
CHUNK_SIZE = 800  # chars per memory chunk

shutdown = Event()
dry_run = False

stats = {
    "franchise": "",
    "source_tag": "",
    "movies_total": 0,
    "movies_processed": 0,
    "scripts_found": 0,
    "wiki_fallbacks": 0,
    "memories_stored": 0,
    "errors": 0,
    "start_time": 0,
    "current_movie": "",
}


def signal_handler(sig, frame):
    shutdown.set()


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[movie_ingest:{stats['franchise']} {ts}] {msg}"
    print(line, flush=True)
    log_file = LOG_DIR / f"{stats['franchise']}.log"
    with open(log_file, "a") as f:
        f.write(line + "\n")


def slack_post(text):
    if dry_run:
        log(f"[DRY RUN] Slack: {text[:80]}...")
        return
    if HAS_NOVA_CONFIG:
        try:
            nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
        except Exception:
            pass


# ── Vector Memory ─────────────────────────────────────────────────────────────

def store_memory(text, source, metadata):
    if dry_run:
        stats["memories_stored"] += 1
        return True
    payload = json.dumps({
        "text": text[:2000],
        "source": source,
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=15)
        stats["memories_stored"] += 1
        return True
    except Exception as e:
        stats["errors"] += 1
        return False


# ── IMSDb Script Fetcher ──────────────────────────────────────────────────────

def fetch_imsdb_script(script_path):
    """Fetch a script from IMSDb. Returns plain text or None."""
    url = f"{IMSDB_BASE}/scripts/{script_path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        pre_match = re.search(r'<pre>(.*?)</pre>', html, re.DOTALL)
        if pre_match:
            text = re.sub(r'<[^>]+>', '', pre_match.group(1))
            text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
            text = text.replace('&#39;', "'").replace('&quot;', '"')
            if len(text) > 500:
                return text
    except Exception:
        pass
    return None


# ── Wikipedia Fetcher ─────────────────────────────────────────────────────────

def _wiki_request(url, max_retries=3):
    """Make a Wikipedia API request with retry and backoff for 429s."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": f"NovaBotMovieIngest/1.0 ({stats.get('franchise', 'unknown')})"
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = (attempt + 1) * 5
                log(f"    Rate limited (429), waiting {wait}s...")
                time.sleep(wait)
            else:
                break
        except Exception:
            break
    return None


def wiki_get_page_text(page_title):
    """Get clean text extract of a Wikipedia page."""
    params = {
        "action": "query",
        "titles": page_title,
        "prop": "extracts",
        "explaintext": "1",
        "format": "json",
        "formatversion": "2",
    }
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    data = _wiki_request(url)
    if data:
        pages = data.get("query", {}).get("pages", [])
        if pages and not pages[0].get("missing"):
            return pages[0].get("extract", "")
    return ""


def wiki_get_wikitext(page_title):
    """Get raw wikitext of a Wikipedia page."""
    params = {
        "action": "parse",
        "page": page_title,
        "prop": "wikitext",
        "format": "json",
        "formatversion": "2",
    }
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    data = _wiki_request(url)
    if data:
        return data.get("parse", {}).get("wikitext", "")
    return ""


# ── Text Chunking ─────────────────────────────────────────────────────────────

def chunk_text(text, chunk_size=CHUNK_SIZE):
    """Split text into chunks at sentence/paragraph boundaries."""
    chunks = []
    paragraphs = text.split('\n\n')
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= chunk_size:
            current = current + "\n\n" + para if current else para
        else:
            if current:
                chunks.append(current.strip())
            if len(para) <= chunk_size:
                current = para
            else:
                # Split long paragraph by sentences
                sentences = re.split(r'(?<=[.!?])\s+', para)
                current = ""
                for sent in sentences:
                    if len(current) + len(sent) + 1 <= chunk_size:
                        current = current + " " + sent if current else sent
                    else:
                        if current:
                            chunks.append(current.strip())
                        current = sent
    if current:
        chunks.append(current.strip())

    return [c for c in chunks if len(c) > 50]


# ── Movie Processor ──────────────────────────────────────────────────────────

def process_movie(movie, source_tag):
    """Process a single movie: fetch script or wiki data, chunk, store."""
    title = movie["title"]
    stats["current_movie"] = title
    log(f"  Processing: {title}")

    stored_count = 0

    # Try IMSDb first
    script_text = None
    if movie.get("imsdb_path"):
        script_text = fetch_imsdb_script(movie["imsdb_path"])
        if script_text:
            stats["scripts_found"] += 1
            log(f"    IMSDb script: {len(script_text):,} chars")
            time.sleep(0.5)

    # Always get Wikipedia data for metadata
    wiki_text = ""
    if movie.get("wiki_page"):
        wiki_text = wiki_get_page_text(movie["wiki_page"])
        if wiki_text:
            log(f"    Wikipedia: {len(wiki_text):,} chars")
        time.sleep(0.5)

    if not script_text and not wiki_text:
        log(f"    No data found — skipping")
        stats["errors"] += 1
        return 0

    if not script_text:
        stats["wiki_fallbacks"] += 1

    # Build metadata
    meta_base = {
        "show": title,
        "franchise": stats["franchise"],
        "category": "film",
    }
    if movie.get("year"):
        meta_base["year"] = movie["year"]
    if movie.get("director"):
        meta_base["director"] = movie["director"]

    # Store movie overview from Wikipedia
    if wiki_text:
        # Extract first few paragraphs as overview
        paragraphs = [p.strip() for p in wiki_text.split('\n') if p.strip() and len(p.strip()) > 30]
        overview = '\n'.join(paragraphs[:5])
        if overview:
            meta = {**meta_base, "type": "movie_overview"}
            store_memory(f"{title} — Overview:\n{overview}", source_tag, meta)
            stored_count += 1

        # Store plot section
        plot_match = re.search(r'(?:^|\n)==\s*Plot\s*==\s*\n(.*?)(?:\n==|\Z)', wiki_text, re.DOTALL)
        if not plot_match:
            plot_match = re.search(r'\n(Plot|Synopsis)\n(.*?)(?:\n[A-Z][a-z]+\n|\Z)', wiki_text, re.DOTALL)
        if plot_match:
            plot_text = plot_match.group(1) if '==' not in plot_match.group(0)[:5] else plot_match.group(1)
            # Try to get just the plot content
            plot_section = wiki_text.split('\n\n')
            in_plot = False
            plot_lines = []
            for p in plot_section:
                if 'plot' in p.lower()[:20] or 'synopsis' in p.lower()[:20]:
                    in_plot = True
                    continue
                if in_plot:
                    if p.startswith('==') or p.startswith('Cast') or p.startswith('Production'):
                        break
                    plot_lines.append(p)
            if plot_lines:
                plot_text = '\n'.join(plot_lines)
                chunks = chunk_text(plot_text)
                for i, chunk in enumerate(chunks[:15]):
                    meta = {**meta_base, "type": "plot", "part": i + 1, "total_parts": len(chunks)}
                    store_memory(f"{title} — Plot (part {i+1}/{len(chunks)}):\n{chunk}", source_tag, meta)
                    stored_count += 1

        # Store cast/production info
        for section_name in ["Cast", "Production", "Reception"]:
            section_lines = []
            in_section = False
            for p in wiki_text.split('\n\n'):
                if section_name.lower() in p.lower()[:30] and len(p) < 50:
                    in_section = True
                    continue
                if in_section:
                    if p.startswith('==') or (len(p) < 30 and any(h in p.lower() for h in ['cast', 'production', 'reception', 'release', 'references', 'see also'])):
                        break
                    section_lines.append(p)
            if section_lines:
                section_text = '\n'.join(section_lines[:5])
                if len(section_text) > 50:
                    meta = {**meta_base, "type": section_name.lower()}
                    store_memory(f"{title} — {section_name}:\n{section_text[:1500]}", source_tag, meta)
                    stored_count += 1

    # Store script content (chunked)
    if script_text:
        chunks = chunk_text(script_text, chunk_size=1000)
        log(f"    Storing {len(chunks)} script chunks")
        for i, chunk in enumerate(chunks):
            if shutdown.is_set():
                break
            meta = {**meta_base, "type": "screenplay", "part": i + 1, "total_parts": len(chunks)}
            store_memory(f"{title} — Screenplay (part {i+1}/{len(chunks)}):\n{chunk}", source_tag, meta)
            stored_count += 1
            time.sleep(0.01)

    log(f"    Stored: {stored_count} memories")
    return stored_count


# ── Status Reporter ──────────────────────────────────────────────────────────

def status_reporter():
    while not shutdown.is_set():
        shutdown.wait(STATUS_INTERVAL)
        if shutdown.is_set():
            break
        post_status()


def post_status():
    elapsed = time.time() - stats["start_time"]
    pct = (stats["movies_processed"] / stats["movies_total"] * 100) if stats["movies_total"] else 0
    remaining = stats["movies_total"] - stats["movies_processed"]
    if stats["movies_processed"] > 0:
        avg = elapsed / stats["movies_processed"]
        eta = str(timedelta(seconds=int(remaining * avg)))
    else:
        eta = "calculating..."

    msg = (
        f":film_projector: *Movie Script Ingest: {stats['franchise']}*\n"
        f"  Progress: {stats['movies_processed']}/{stats['movies_total']} ({pct:.0f}%)\n"
        f"  Scripts from IMSDb: {stats['scripts_found']}\n"
        f"  Wikipedia fallbacks: {stats['wiki_fallbacks']}\n"
        f"  Memories stored: {stats['memories_stored']}\n"
        f"  Current: {stats['current_movie']}\n"
        f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}\n"
        f"  ETA: {eta}"
    )
    if stats["errors"]:
        msg += f"\n  Errors: {stats['errors']}"
    slack_post(msg)


# ── Franchise Definitions ────────────────────────────────────────────────────

FRANCHISES = {
    "death_wish": {
        "source": "movie_script_death_wish",
        "movies": [
            {"title": "Death Wish", "year": 1974, "director": "Michael Winner", "wiki_page": "Death_Wish_(1974_film)", "imsdb_path": None},
            {"title": "Death Wish II", "year": 1982, "director": "Michael Winner", "wiki_page": "Death_Wish_II", "imsdb_path": None},
            {"title": "Death Wish 3", "year": 1985, "director": "Michael Winner", "wiki_page": "Death_Wish_3", "imsdb_path": None},
            {"title": "Death Wish 4: The Crackdown", "year": 1987, "director": "J. Lee Thompson", "wiki_page": "Death_Wish_4:_The_Crackdown", "imsdb_path": None},
            {"title": "Death Wish V: The Face of Death", "year": 1994, "director": "Allan A. Goldstein", "wiki_page": "Death_Wish_V:_The_Face_of_Death", "imsdb_path": None},
            {"title": "Death Wish", "year": 2018, "director": "Eli Roth", "wiki_page": "Death_Wish_(2018_film)", "imsdb_path": None},
        ],
    },
    "cannon_films": {
        "source": "movie_script_cannon",
        "movies": [
            {"title": "Missing in Action", "year": 1984, "director": "Joseph Zito", "wiki_page": "Missing_in_Action_(film)", "imsdb_path": None},
            {"title": "Missing in Action 2: The Beginning", "year": 1985, "director": "Lance Hool", "wiki_page": "Missing_in_Action_2:_The_Beginning", "imsdb_path": None},
            {"title": "Braddock: Missing in Action III", "year": 1988, "director": "Aaron Norris", "wiki_page": "Braddock:_Missing_in_Action_III", "imsdb_path": None},
            {"title": "Breakin'", "year": 1984, "director": "Joel Silberg", "wiki_page": "Breakin%27", "imsdb_path": None},
            {"title": "Breakin' 2: Electric Boogaloo", "year": 1984, "director": "Sam Firstenberg", "wiki_page": "Breakin%27_2:_Electric_Boogaloo", "imsdb_path": None},
            {"title": "The Delta Force", "year": 1986, "director": "Menahem Golan", "wiki_page": "The_Delta_Force", "imsdb_path": None},
            {"title": "Delta Force 2: The Colombian Connection", "year": 1990, "director": "Aaron Norris", "wiki_page": "Delta_Force_2:_The_Colombian_Connection", "imsdb_path": None},
            {"title": "Invasion U.S.A.", "year": 1985, "director": "Joseph Zito", "wiki_page": "Invasion_U.S.A._(1985_film)", "imsdb_path": None},
            {"title": "Over the Top", "year": 1987, "director": "Menahem Golan", "wiki_page": "Over_the_Top_(film)", "imsdb_path": None},
            {"title": "Bloodsport", "year": 1988, "director": "Newt Arnold", "wiki_page": "Bloodsport_(film)", "imsdb_path": None},
            {"title": "Cyborg", "year": 1989, "director": "Albert Pyun", "wiki_page": "Cyborg_(film)", "imsdb_path": None},
            {"title": "Kickboxer", "year": 1989, "director": "Mark DiSalle", "wiki_page": "Kickboxer_(1989_film)", "imsdb_path": None},
            {"title": "American Ninja", "year": 1985, "director": "Sam Firstenberg", "wiki_page": "American_Ninja", "imsdb_path": None},
            {"title": "American Ninja 2: The Confrontation", "year": 1987, "director": "Sam Firstenberg", "wiki_page": "American_Ninja_2:_The_Confrontation", "imsdb_path": None},
            {"title": "Cobra", "year": 1986, "director": "George P. Cosmatos", "wiki_page": "Cobra_(1986_film)", "imsdb_path": None},
            {"title": "Lifeforce", "year": 1985, "director": "Tobe Hooper", "wiki_page": "Lifeforce_(film)", "imsdb_path": None},
            {"title": "Runaway Train", "year": 1985, "director": "Andrei Konchalovsky", "wiki_page": "Runaway_Train_(film)", "imsdb_path": None},
            {"title": "Masters of the Universe", "year": 1987, "director": "Gary Goddard", "wiki_page": "Masters_of_the_Universe_(film)", "imsdb_path": None},
            {"title": "Superman IV: The Quest for Peace", "year": 1987, "director": "Sidney J. Furie", "wiki_page": "Superman_IV:_The_Quest_for_Peace", "imsdb_path": None},
            {"title": "Barfly", "year": 1987, "director": "Barbet Schroeder", "wiki_page": "Barfly_(film)", "imsdb_path": None},
            {"title": "52 Pick-Up", "year": 1986, "director": "John Frankenheimer", "wiki_page": "52_Pick-Up", "imsdb_path": None},
            {"title": "The Texas Chainsaw Massacre 2", "year": 1986, "director": "Tobe Hooper", "wiki_page": "The_Texas_Chainsaw_Massacre_2", "imsdb_path": None},
        ],
    },
    "van_damme": {
        "source": "movie_script_van_damme",
        "movies": [
            {"title": "Bloodsport", "year": 1988, "director": "Newt Arnold", "wiki_page": "Bloodsport_(film)", "imsdb_path": None},
            {"title": "Kickboxer", "year": 1989, "director": "Mark DiSalle", "wiki_page": "Kickboxer_(1989_film)", "imsdb_path": None},
            {"title": "Cyborg", "year": 1989, "director": "Albert Pyun", "wiki_page": "Cyborg_(film)", "imsdb_path": None},
            {"title": "Lionheart", "year": 1990, "director": "Sheldon Lettich", "wiki_page": "Lionheart_(1990_film)", "imsdb_path": None},
            {"title": "Double Impact", "year": 1991, "director": "Sheldon Lettich", "wiki_page": "Double_Impact", "imsdb_path": None},
            {"title": "Universal Soldier", "year": 1992, "director": "Roland Emmerich", "wiki_page": "Universal_Soldier_(1992_film)", "imsdb_path": None},
            {"title": "Nowhere to Run", "year": 1993, "director": "Robert Harmon", "wiki_page": "Nowhere_to_Run_(1993_film)", "imsdb_path": None},
            {"title": "Hard Target", "year": 1993, "director": "John Woo", "wiki_page": "Hard_Target", "imsdb_path": None},
            {"title": "Timecop", "year": 1994, "director": "Peter Hyams", "wiki_page": "Timecop_(film)", "imsdb_path": None},
            {"title": "Street Fighter", "year": 1994, "director": "Steven E. de Souza", "wiki_page": "Street_Fighter_(1994_film)", "imsdb_path": None},
            {"title": "Sudden Death", "year": 1995, "director": "Peter Hyams", "wiki_page": "Sudden_Death_(1995_film)", "imsdb_path": None},
            {"title": "The Quest", "year": 1996, "director": "Jean-Claude Van Damme", "wiki_page": "The_Quest_(1996_film)", "imsdb_path": None},
            {"title": "Maximum Risk", "year": 1996, "director": "Ringo Lam", "wiki_page": "Maximum_Risk", "imsdb_path": None},
            {"title": "JCVD", "year": 2008, "director": "Mabrouk El Mechri", "wiki_page": "JCVD", "imsdb_path": None},
        ],
    },
    "men_in_black": {
        "source": "movie_script_men_in_black",
        "movies": [
            {"title": "Men in Black", "year": 1997, "director": "Barry Sonnenfeld", "wiki_page": "Men_in_Black_(film)", "imsdb_path": "Men-in-Black.html"},
            {"title": "Men in Black II", "year": 2002, "director": "Barry Sonnenfeld", "wiki_page": "Men_in_Black_II", "imsdb_path": None},
            {"title": "Men in Black 3", "year": 2012, "director": "Barry Sonnenfeld", "wiki_page": "Men_in_Black_3", "imsdb_path": "Men-in-Black-3.html"},
            {"title": "Men in Black: International", "year": 2019, "director": "F. Gary Gray", "wiki_page": "Men_in_Black:_International", "imsdb_path": None},
        ],
    },
    "john_wick": {
        "source": "movie_script_john_wick",
        "movies": [
            {"title": "John Wick", "year": 2014, "director": "Chad Stahelski", "wiki_page": "John_Wick_(film)", "imsdb_path": "John-Wick.html"},
            {"title": "John Wick: Chapter 2", "year": 2017, "director": "Chad Stahelski", "wiki_page": "John_Wick:_Chapter_2", "imsdb_path": None},
            {"title": "John Wick: Chapter 3 – Parabellum", "year": 2019, "director": "Chad Stahelski", "wiki_page": "John_Wick:_Chapter_3_%E2%80%93_Parabellum", "imsdb_path": None},
            {"title": "John Wick: Chapter 4", "year": 2023, "director": "Chad Stahelski", "wiki_page": "John_Wick:_Chapter_4", "imsdb_path": "John-Wick:-Chapter-4.html"},
        ],
    },
    "hunger_games": {
        "source": "movie_script_hunger_games",
        "movies": [
            {"title": "The Hunger Games", "year": 2012, "director": "Gary Ross", "wiki_page": "The_Hunger_Games_(film)", "imsdb_path": None},
            {"title": "The Hunger Games: Catching Fire", "year": 2013, "director": "Francis Lawrence", "wiki_page": "The_Hunger_Games:_Catching_Fire", "imsdb_path": None},
            {"title": "The Hunger Games: Mockingjay – Part 1", "year": 2014, "director": "Francis Lawrence", "wiki_page": "The_Hunger_Games:_Mockingjay_%E2%80%93_Part_1", "imsdb_path": None},
            {"title": "The Hunger Games: Mockingjay – Part 2", "year": 2015, "director": "Francis Lawrence", "wiki_page": "The_Hunger_Games:_Mockingjay_%E2%80%93_Part_2", "imsdb_path": None},
            {"title": "The Hunger Games: The Ballad of Songbirds & Snakes", "year": 2023, "director": "Francis Lawrence", "wiki_page": "The_Hunger_Games:_The_Ballad_of_Songbirds_%26_Snakes_(film)", "imsdb_path": None},
        ],
    },
    "oceans": {
        "source": "movie_script_oceans",
        "movies": [
            {"title": "Ocean's Eleven", "year": 2001, "director": "Steven Soderbergh", "wiki_page": "Ocean%27s_Eleven_(2001_film)", "imsdb_path": "Ocean's-Eleven.html"},
            {"title": "Ocean's Twelve", "year": 2004, "director": "Steven Soderbergh", "wiki_page": "Ocean%27s_Twelve", "imsdb_path": "Ocean's-Twelve.html"},
            {"title": "Ocean's Thirteen", "year": 2007, "director": "Steven Soderbergh", "wiki_page": "Ocean%27s_Thirteen", "imsdb_path": None},
            {"title": "Ocean's 8", "year": 2018, "director": "Gary Ross", "wiki_page": "Ocean%27s_8", "imsdb_path": None},
        ],
    },
    "batman": {
        "source": "movie_script_batman",
        "movies": [
            {"title": "Batman (1966)", "year": 1966, "director": "Leslie H. Martinson", "wiki_page": "Batman_(1966_film)", "imsdb_path": None},
            {"title": "Batman", "year": 1989, "director": "Tim Burton", "wiki_page": "Batman_(1989_film)", "imsdb_path": "Batman.html"},
            {"title": "Batman Returns", "year": 1992, "director": "Tim Burton", "wiki_page": "Batman_Returns", "imsdb_path": "Batman-2.html"},
            {"title": "Batman Forever", "year": 1995, "director": "Joel Schumacher", "wiki_page": "Batman_Forever", "imsdb_path": "Batman-Forever.html"},
            {"title": "Batman & Robin", "year": 1997, "director": "Joel Schumacher", "wiki_page": "Batman_%26_Robin_(film)", "imsdb_path": "Batman-and-Robin.html"},
            {"title": "Batman Begins", "year": 2005, "director": "Christopher Nolan", "wiki_page": "Batman_Begins", "imsdb_path": "Batman-Begins.html"},
            {"title": "The Dark Knight", "year": 2008, "director": "Christopher Nolan", "wiki_page": "The_Dark_Knight", "imsdb_path": "Dark-Knight,-The.html"},
            {"title": "The Dark Knight Rises", "year": 2012, "director": "Christopher Nolan", "wiki_page": "The_Dark_Knight_Rises", "imsdb_path": "Dark-Knight-Rises,-The.html"},
            {"title": "The Batman", "year": 2022, "director": "Matt Reeves", "wiki_page": "The_Batman_(film)", "imsdb_path": None},
        ],
    },
    "red_dawn": {
        "source": "movie_script_red_dawn",
        "movies": [
            {"title": "Red Dawn", "year": 1984, "director": "John Milius", "wiki_page": "Red_Dawn", "imsdb_path": None},
        ],
    },
    "taken": {
        "source": "movie_script_taken",
        "movies": [
            {"title": "Taken", "year": 2008, "director": "Pierre Morel", "wiki_page": "Taken_(film)", "imsdb_path": None},
            {"title": "Taken 2", "year": 2012, "director": "Olivier Megaton", "wiki_page": "Taken_2", "imsdb_path": None},
            {"title": "Taken 3", "year": 2014, "director": "Olivier Megaton", "wiki_page": "Taken_3", "imsdb_path": None},
        ],
    },
    "bruce_lee": {
        "source": "movie_script_bruce_lee",
        "movies": [
            {"title": "The Big Boss", "year": 1971, "director": "Lo Wei", "wiki_page": "The_Big_Boss", "imsdb_path": None},
            {"title": "Fist of Fury", "year": 1972, "director": "Lo Wei", "wiki_page": "Fist_of_Fury", "imsdb_path": None},
            {"title": "The Way of the Dragon", "year": 1972, "director": "Bruce Lee", "wiki_page": "The_Way_of_the_Dragon", "imsdb_path": None},
            {"title": "Enter the Dragon", "year": 1973, "director": "Robert Clouse", "wiki_page": "Enter_the_Dragon", "imsdb_path": None},
            {"title": "Game of Death", "year": 1978, "director": "Robert Clouse", "wiki_page": "Game_of_Death", "imsdb_path": None},
        ],
    },
    "evil_dead": {
        "source": "movie_script_evil_dead",
        "movies": [
            {"title": "The Evil Dead", "year": 1981, "director": "Sam Raimi", "wiki_page": "The_Evil_Dead", "imsdb_path": "Evil-Dead.html"},
            {"title": "Evil Dead II", "year": 1987, "director": "Sam Raimi", "wiki_page": "Evil_Dead_II", "imsdb_path": "Evil-Dead-II:-Dead-by-Dawn.html"},
            {"title": "Army of Darkness", "year": 1992, "director": "Sam Raimi", "wiki_page": "Army_of_Darkness", "imsdb_path": None},
            {"title": "Evil Dead", "year": 2013, "director": "Fede Alvarez", "wiki_page": "Evil_Dead_(2013_film)", "imsdb_path": None},
            {"title": "Evil Dead Rise", "year": 2023, "director": "Lee Cronin", "wiki_page": "Evil_Dead_Rise", "imsdb_path": None},
        ],
    },
    "rambo": {
        "source": "movie_script_rambo",
        "movies": [
            {"title": "First Blood", "year": 1982, "director": "Ted Kotcheff", "wiki_page": "First_Blood", "imsdb_path": None},
            {"title": "Rambo: First Blood Part II", "year": 1985, "director": "George P. Cosmatos", "wiki_page": "Rambo:_First_Blood_Part_II", "imsdb_path": "Rambo:-First-Blood-II:-The-Mission.html"},
            {"title": "Rambo III", "year": 1988, "director": "Peter MacDonald", "wiki_page": "Rambo_III", "imsdb_path": None},
            {"title": "Rambo", "year": 2008, "director": "Sylvester Stallone", "wiki_page": "Rambo_(2008_film)", "imsdb_path": None},
            {"title": "Rambo: Last Blood", "year": 2019, "director": "Adrian Grunberg", "wiki_page": "Rambo:_Last_Blood", "imsdb_path": None},
        ],
    },
    "ghostbusters": {
        "source": "movie_script_ghostbusters",
        "movies": [
            {"title": "Ghostbusters", "year": 1984, "director": "Ivan Reitman", "wiki_page": "Ghostbusters_(1984_film)", "imsdb_path": "Ghostbusters.html"},
            {"title": "Ghostbusters II", "year": 1989, "director": "Ivan Reitman", "wiki_page": "Ghostbusters_II", "imsdb_path": "Ghostbusters-2.html"},
            {"title": "Ghostbusters: Afterlife", "year": 2021, "director": "Jason Reitman", "wiki_page": "Ghostbusters:_Afterlife", "imsdb_path": None},
            {"title": "Ghostbusters: Frozen Empire", "year": 2024, "director": "Gil Kenan", "wiki_page": "Ghostbusters:_Frozen_Empire", "imsdb_path": None},
        ],
    },
    "uhf": {
        "source": "movie_script_uhf",
        "movies": [
            {"title": "UHF", "year": 1989, "director": "Jay Levey", "wiki_page": "UHF_(film)", "imsdb_path": None},
        ],
    },
    "valley_girl": {
        "source": "movie_script_valley_girl",
        "movies": [
            {"title": "Valley Girl", "year": 1983, "director": "Martha Coolidge", "wiki_page": "Valley_Girl_(1983_film)", "imsdb_path": None},
        ],
    },
    "decline_western_civ": {
        "source": "movie_script_decline_western_civ",
        "movies": [
            {"title": "The Decline of Western Civilization", "year": 1981, "director": "Penelope Spheeris", "wiki_page": "The_Decline_of_Western_Civilization", "imsdb_path": None},
            {"title": "The Decline of Western Civilization Part II: The Metal Years", "year": 1988, "director": "Penelope Spheeris", "wiki_page": "The_Decline_of_Western_Civilization_Part_II:_The_Metal_Years", "imsdb_path": None},
            {"title": "The Decline of Western Civilization Part III", "year": 1998, "director": "Penelope Spheeris", "wiki_page": "The_Decline_of_Western_Civilization_Part_III", "imsdb_path": None},
        ],
    },
    "walken": {
        "source": "movie_script_walken",
        "movies": [
            {"title": "The Deer Hunter", "year": 1978, "director": "Michael Cimino", "wiki_page": "The_Deer_Hunter", "imsdb_path": "Deer-Hunter,-The.html"},
            {"title": "The Dogs of War", "year": 1980, "director": "John Irvin", "wiki_page": "The_Dogs_of_War_(film)", "imsdb_path": None},
            {"title": "The Dead Zone", "year": 1983, "director": "David Cronenberg", "wiki_page": "The_Dead_Zone_(film)", "imsdb_path": None},
            {"title": "A View to a Kill", "year": 1985, "director": "John Glen", "wiki_page": "A_View_to_a_Kill", "imsdb_path": None},
            {"title": "At Close Range", "year": 1986, "director": "James Foley", "wiki_page": "At_Close_Range", "imsdb_path": None},
            {"title": "King of New York", "year": 1990, "director": "Abel Ferrara", "wiki_page": "King_of_New_York", "imsdb_path": None},
            {"title": "McBain", "year": 1991, "director": "James Glickenhaus", "wiki_page": "McBain_(film)", "imsdb_path": None},
            {"title": "True Romance", "year": 1993, "director": "Tony Scott", "wiki_page": "True_Romance", "imsdb_path": None},
            {"title": "Pulp Fiction", "year": 1994, "director": "Quentin Tarantino", "wiki_page": "Pulp_Fiction", "imsdb_path": "Pulp-Fiction.html"},
            {"title": "The Prophecy", "year": 1995, "director": "Gregory Widen", "wiki_page": "The_Prophecy_(film)", "imsdb_path": None},
            {"title": "Nick of Time", "year": 1995, "director": "John Badham", "wiki_page": "Nick_of_Time_(film)", "imsdb_path": None},
            {"title": "Sleepy Hollow", "year": 1999, "director": "Tim Burton", "wiki_page": "Sleepy_Hollow_(film)", "imsdb_path": None},
            {"title": "The Rundown", "year": 2003, "director": "Peter Berg", "wiki_page": "The_Rundown", "imsdb_path": None},
            {"title": "Man on Fire", "year": 2004, "director": "Tony Scott", "wiki_page": "Man_on_Fire_(2004_film)", "imsdb_path": None},
            {"title": "Shoot 'Em Up", "year": 2007, "director": "Michael Davis", "wiki_page": "Shoot_%27Em_Up", "imsdb_path": None},
        ],
    },
    "godfather": {
        "source": "movie_script_godfather",
        "movies": [
            {"title": "The Godfather", "year": 1972, "director": "Francis Ford Coppola", "wiki_page": "The_Godfather", "imsdb_path": "Godfather.html"},
            {"title": "The Godfather Part II", "year": 1974, "director": "Francis Ford Coppola", "wiki_page": "The_Godfather_Part_II", "imsdb_path": "Godfather-Part-II.html"},
            {"title": "The Godfather Part III", "year": 1990, "director": "Francis Ford Coppola", "wiki_page": "The_Godfather_Part_III", "imsdb_path": "Godfather-Part-III,-The.html"},
        ],
    },
    "i_remember_mama": {
        "source": "movie_script_i_remember_mama",
        "movies": [
            {"title": "I Remember Mama", "year": 1948, "director": "George Stevens", "wiki_page": "I_Remember_Mama_(film)", "imsdb_path": None},
        ],
    },
    "john_hughes": {
        "source": "movie_script_john_hughes",
        "movies": [
            {"title": "National Lampoon's Vacation", "year": 1983, "director": "Harold Ramis", "wiki_page": "National_Lampoon%27s_Vacation", "imsdb_path": None},
            {"title": "Sixteen Candles", "year": 1984, "director": "John Hughes", "wiki_page": "Sixteen_Candles", "imsdb_path": None},
            {"title": "The Breakfast Club", "year": 1985, "director": "John Hughes", "wiki_page": "The_Breakfast_Club", "imsdb_path": "Breakfast-Club,-The.html"},
            {"title": "Weird Science", "year": 1985, "director": "John Hughes", "wiki_page": "Weird_Science_(film)", "imsdb_path": None},
            {"title": "Pretty in Pink", "year": 1986, "director": "Howard Deutch", "wiki_page": "Pretty_in_Pink", "imsdb_path": None},
            {"title": "Ferris Bueller's Day Off", "year": 1986, "director": "John Hughes", "wiki_page": "Ferris_Bueller%27s_Day_Off", "imsdb_path": "Ferris-Bueller's-Day-Off.html"},
            {"title": "Some Kind of Wonderful", "year": 1987, "director": "Howard Deutch", "wiki_page": "Some_Kind_of_Wonderful_(film)", "imsdb_path": None},
            {"title": "Planes, Trains and Automobiles", "year": 1987, "director": "John Hughes", "wiki_page": "Planes,_Trains_and_Automobiles", "imsdb_path": None},
            {"title": "She's Having a Baby", "year": 1988, "director": "John Hughes", "wiki_page": "She%27s_Having_a_Baby", "imsdb_path": None},
            {"title": "Uncle Buck", "year": 1989, "director": "John Hughes", "wiki_page": "Uncle_Buck", "imsdb_path": None},
            {"title": "National Lampoon's Christmas Vacation", "year": 1989, "director": "Jeremiah S. Chechik", "wiki_page": "National_Lampoon%27s_Christmas_Vacation", "imsdb_path": None},
            {"title": "Home Alone", "year": 1990, "director": "Chris Columbus", "wiki_page": "Home_Alone", "imsdb_path": None},
            {"title": "Home Alone 2: Lost in New York", "year": 1992, "director": "Chris Columbus", "wiki_page": "Home_Alone_2:_Lost_in_New_York", "imsdb_path": None},
            {"title": "Curly Sue", "year": 1991, "director": "John Hughes", "wiki_page": "Curly_Sue", "imsdb_path": None},
        ],
    },
    "john_waters": {
        "source": "movie_script_john_waters",
        "movies": [
            {"title": "Pink Flamingos", "year": 1972, "director": "John Waters", "wiki_page": "Pink_Flamingos", "imsdb_path": None},
            {"title": "Female Trouble", "year": 1974, "director": "John Waters", "wiki_page": "Female_Trouble", "imsdb_path": None},
            {"title": "Desperate Living", "year": 1977, "director": "John Waters", "wiki_page": "Desperate_Living", "imsdb_path": None},
            {"title": "Polyester", "year": 1981, "director": "John Waters", "wiki_page": "Polyester_(film)", "imsdb_path": None},
            {"title": "Hairspray", "year": 1988, "director": "John Waters", "wiki_page": "Hairspray_(1988_film)", "imsdb_path": None},
            {"title": "Cry-Baby", "year": 1990, "director": "John Waters", "wiki_page": "Cry-Baby", "imsdb_path": None},
            {"title": "Serial Mom", "year": 1994, "director": "John Waters", "wiki_page": "Serial_Mom", "imsdb_path": None},
            {"title": "Pecker", "year": 1998, "director": "John Waters", "wiki_page": "Pecker_(film)", "imsdb_path": None},
            {"title": "Cecil B. Demented", "year": 2000, "director": "John Waters", "wiki_page": "Cecil_B._Demented", "imsdb_path": None},
            {"title": "A Dirty Shame", "year": 2004, "director": "John Waters", "wiki_page": "A_Dirty_Shame", "imsdb_path": None},
            {"title": "Multiple Maniacs", "year": 1970, "director": "John Waters", "wiki_page": "Multiple_Maniacs", "imsdb_path": None},
            {"title": "Mondo Trasho", "year": 1969, "director": "John Waters", "wiki_page": "Mondo_Trasho", "imsdb_path": None},
        ],
    },
    "rounders": {
        "source": "movie_script_rounders",
        "movies": [
            {"title": "Rounders", "year": 1998, "director": "John Dahl", "wiki_page": "Rounders_(film)", "imsdb_path": None},
        ],
    },
    "deniro": {
        "source": "movie_script_deniro",
        "movies": [
            {"title": "Mean Streets", "year": 1973, "director": "Martin Scorsese", "wiki_page": "Mean_Streets", "imsdb_path": None},
            {"title": "The Godfather Part II", "year": 1974, "director": "Francis Ford Coppola", "wiki_page": "The_Godfather_Part_II", "imsdb_path": "Godfather-Part-II.html"},
            {"title": "Taxi Driver", "year": 1976, "director": "Martin Scorsese", "wiki_page": "Taxi_Driver", "imsdb_path": "Taxi-Driver.html"},
            {"title": "The Deer Hunter", "year": 1978, "director": "Michael Cimino", "wiki_page": "The_Deer_Hunter", "imsdb_path": "Deer-Hunter,-The.html"},
            {"title": "Raging Bull", "year": 1980, "director": "Martin Scorsese", "wiki_page": "Raging_Bull", "imsdb_path": "Raging-Bull.html"},
            {"title": "Once Upon a Time in America", "year": 1984, "director": "Sergio Leone", "wiki_page": "Once_Upon_a_Time_in_America", "imsdb_path": None},
            {"title": "The Untouchables", "year": 1987, "director": "Brian De Palma", "wiki_page": "The_Untouchables_(film)", "imsdb_path": None},
            {"title": "Midnight Run", "year": 1988, "director": "Martin Brest", "wiki_page": "Midnight_Run", "imsdb_path": None},
            {"title": "Goodfellas", "year": 1990, "director": "Martin Scorsese", "wiki_page": "Goodfellas", "imsdb_path": "Goodfellas.html"},
            {"title": "Cape Fear", "year": 1991, "director": "Martin Scorsese", "wiki_page": "Cape_Fear_(1991_film)", "imsdb_path": None},
            {"title": "A Bronx Tale", "year": 1993, "director": "Robert De Niro", "wiki_page": "A_Bronx_Tale", "imsdb_path": None},
            {"title": "Heat", "year": 1995, "director": "Michael Mann", "wiki_page": "Heat_(1995_film)", "imsdb_path": "Heat.html"},
            {"title": "Casino", "year": 1995, "director": "Martin Scorsese", "wiki_page": "Casino_(1995_film)", "imsdb_path": "Casino.html"},
            {"title": "Ronin", "year": 1998, "director": "John Frankenheimer", "wiki_page": "Ronin_(film)", "imsdb_path": None},
            {"title": "The Score", "year": 2001, "director": "Frank Oz", "wiki_page": "The_Score_(film)", "imsdb_path": None},
            {"title": "City by the Sea", "year": 2002, "director": "Michael Caton-Jones", "wiki_page": "City_by_the_Sea", "imsdb_path": None},
            {"title": "The Irishman", "year": 2019, "director": "Martin Scorsese", "wiki_page": "The_Irishman", "imsdb_path": None},
            {"title": "Killers of the Flower Moon", "year": 2023, "director": "Martin Scorsese", "wiki_page": "Killers_of_the_Flower_Moon_(film)", "imsdb_path": None},
        ],
    },
    "hanks": {
        "source": "movie_script_hanks",
        "movies": [
            {"title": "Saving Private Ryan", "year": 1998, "director": "Steven Spielberg", "wiki_page": "Saving_Private_Ryan", "imsdb_path": "Saving-Private-Ryan.html"},
            {"title": "Cast Away", "year": 2000, "director": "Robert Zemeckis", "wiki_page": "Cast_Away", "imsdb_path": "Cast-Away.html"},
            {"title": "Road to Perdition", "year": 2002, "director": "Sam Mendes", "wiki_page": "Road_to_Perdition", "imsdb_path": None},
            {"title": "The Terminal", "year": 2004, "director": "Steven Spielberg", "wiki_page": "The_Terminal", "imsdb_path": None},
            {"title": "Charlie Wilson's War", "year": 2007, "director": "Mike Nichols", "wiki_page": "Charlie_Wilson%27s_War_(film)", "imsdb_path": None},
            {"title": "Captain Phillips", "year": 2013, "director": "Paul Greengrass", "wiki_page": "Captain_Phillips_(film)", "imsdb_path": None},
            {"title": "Bridge of Spies", "year": 2015, "director": "Steven Spielberg", "wiki_page": "Bridge_of_Spies_(film)", "imsdb_path": None},
            {"title": "Sully", "year": 2016, "director": "Clint Eastwood", "wiki_page": "Sully_(film)", "imsdb_path": None},
            {"title": "The Post", "year": 2017, "director": "Steven Spielberg", "wiki_page": "The_Post_(film)", "imsdb_path": None},
            {"title": "Greyhound", "year": 2020, "director": "Aaron Schneider", "wiki_page": "Greyhound_(film)", "imsdb_path": None},
            {"title": "News of the World", "year": 2020, "director": "Paul Greengrass", "wiki_page": "News_of_the_World_(film)", "imsdb_path": None},
        ],
    },
    "american_history_x": {
        "source": "movie_script_american_history_x",
        "movies": [
            {"title": "American History X", "year": 1998, "director": "Tony Kaye", "wiki_page": "American_History_X", "imsdb_path": "American-History-X.html"},
        ],
    },
    "stephen_king": {
        "source": "movie_script_stephen_king",
        "movies": [
            {"title": "Carrie", "year": 1976, "director": "Brian De Palma", "wiki_page": "Carrie_(1976_film)", "imsdb_path": "Carrie.html"},
            {"title": "The Shining", "year": 1980, "director": "Stanley Kubrick", "wiki_page": "The_Shining_(film)", "imsdb_path": "Shining,-The.html"},
            {"title": "Cujo", "year": 1983, "director": "Lewis Teague", "wiki_page": "Cujo_(film)", "imsdb_path": None},
            {"title": "Christine", "year": 1983, "director": "John Carpenter", "wiki_page": "Christine_(1983_film)", "imsdb_path": None},
            {"title": "The Dead Zone", "year": 1983, "director": "David Cronenberg", "wiki_page": "The_Dead_Zone_(film)", "imsdb_path": None},
            {"title": "Children of the Corn", "year": 1984, "director": "Fritz Kiersch", "wiki_page": "Children_of_the_Corn_(1984_film)", "imsdb_path": None},
            {"title": "Firestarter", "year": 1984, "director": "Mark L. Lester", "wiki_page": "Firestarter_(1984_film)", "imsdb_path": None},
            {"title": "Cat's Eye", "year": 1985, "director": "Lewis Teague", "wiki_page": "Cat%27s_Eye_(film)", "imsdb_path": None},
            {"title": "Silver Bullet", "year": 1985, "director": "Daniel Attias", "wiki_page": "Silver_Bullet_(film)", "imsdb_path": None},
            {"title": "Stand by Me", "year": 1986, "director": "Rob Reiner", "wiki_page": "Stand_by_Me_(film)", "imsdb_path": None},
            {"title": "Maximum Overdrive", "year": 1986, "director": "Stephen King", "wiki_page": "Maximum_Overdrive", "imsdb_path": None},
            {"title": "The Running Man", "year": 1987, "director": "Paul Michael Glaser", "wiki_page": "The_Running_Man_(1987_film)", "imsdb_path": None},
            {"title": "Pet Sematary", "year": 1989, "director": "Mary Lambert", "wiki_page": "Pet_Sematary_(1989_film)", "imsdb_path": None},
            {"title": "Misery", "year": 1990, "director": "Rob Reiner", "wiki_page": "Misery_(film)", "imsdb_path": "Misery.html"},
            {"title": "Graveyard Shift", "year": 1990, "director": "Ralph S. Singleton", "wiki_page": "Graveyard_Shift_(1990_film)", "imsdb_path": None},
            {"title": "It", "year": 1990, "director": "Tommy Lee Wallace", "wiki_page": "It_(miniseries)", "imsdb_path": None},
            {"title": "The Lawnmower Man", "year": 1992, "director": "Brett Leonard", "wiki_page": "The_Lawnmower_Man_(film)", "imsdb_path": None},
            {"title": "Needful Things", "year": 1993, "director": "Fraser C. Heston", "wiki_page": "Needful_Things_(film)", "imsdb_path": None},
            {"title": "The Shawshank Redemption", "year": 1994, "director": "Frank Darabont", "wiki_page": "The_Shawshank_Redemption", "imsdb_path": "Shawshank-Redemption,-The.html"},
            {"title": "Dolores Claiborne", "year": 1995, "director": "Taylor Hackford", "wiki_page": "Dolores_Claiborne_(film)", "imsdb_path": None},
            {"title": "Apt Pupil", "year": 1998, "director": "Bryan Singer", "wiki_page": "Apt_Pupil_(film)", "imsdb_path": None},
            {"title": "The Green Mile", "year": 1999, "director": "Frank Darabont", "wiki_page": "The_Green_Mile_(film)", "imsdb_path": None},
            {"title": "Hearts in Atlantis", "year": 2001, "director": "Scott Hicks", "wiki_page": "Hearts_in_Atlantis_(film)", "imsdb_path": None},
            {"title": "Dreamcatcher", "year": 2003, "director": "Lawrence Kasdan", "wiki_page": "Dreamcatcher_(2003_film)", "imsdb_path": None},
            {"title": "Secret Window", "year": 2004, "director": "David Koepp", "wiki_page": "Secret_Window", "imsdb_path": None},
            {"title": "1408", "year": 2007, "director": "Mikael Hafstrom", "wiki_page": "1408_(film)", "imsdb_path": None},
            {"title": "The Mist", "year": 2007, "director": "Frank Darabont", "wiki_page": "The_Mist_(film)", "imsdb_path": None},
            {"title": "It Chapter One", "year": 2017, "director": "Andy Muschietti", "wiki_page": "It_(2017_film)", "imsdb_path": None},
            {"title": "It Chapter Two", "year": 2019, "director": "Andy Muschietti", "wiki_page": "It_Chapter_Two", "imsdb_path": None},
            {"title": "Gerald's Game", "year": 2017, "director": "Mike Flanagan", "wiki_page": "Gerald%27s_Game_(film)", "imsdb_path": None},
            {"title": "Doctor Sleep", "year": 2019, "director": "Mike Flanagan", "wiki_page": "Doctor_Sleep_(film)", "imsdb_path": None},
            {"title": "Pet Sematary", "year": 2019, "director": "Kevin Kolsch", "wiki_page": "Pet_Sematary_(2019_film)", "imsdb_path": None},
            {"title": "In the Tall Grass", "year": 2019, "director": "Vincenzo Natali", "wiki_page": "In_the_Tall_Grass_(film)", "imsdb_path": None},
            {"title": "The Dark Tower", "year": 2017, "director": "Nikolaj Arcel", "wiki_page": "The_Dark_Tower_(film)", "imsdb_path": None},
        ],
    },
    "hellraiser": {
        "source": "movie_script_hellraiser",
        "movies": [
            {"title": "Hellraiser", "year": 1987, "director": "Clive Barker", "wiki_page": "Hellraiser_(film)", "imsdb_path": None},
            {"title": "Hellbound: Hellraiser II", "year": 1988, "director": "Tony Randel", "wiki_page": "Hellbound:_Hellraiser_II", "imsdb_path": None},
            {"title": "Hellraiser III: Hell on Earth", "year": 1992, "director": "Anthony Hickox", "wiki_page": "Hellraiser_III:_Hell_on_Earth", "imsdb_path": None},
            {"title": "Hellraiser: Bloodline", "year": 1996, "director": "Kevin Yagher", "wiki_page": "Hellraiser:_Bloodline", "imsdb_path": None},
            {"title": "Hellraiser: Inferno", "year": 2000, "director": "Scott Derrickson", "wiki_page": "Hellraiser:_Inferno", "imsdb_path": None},
            {"title": "Hellraiser: Hellseeker", "year": 2002, "director": "Rick Bota", "wiki_page": "Hellraiser:_Hellseeker", "imsdb_path": None},
            {"title": "Hellraiser: Deader", "year": 2005, "director": "Rick Bota", "wiki_page": "Hellraiser:_Deader", "imsdb_path": None},
            {"title": "Hellraiser: Hellworld", "year": 2005, "director": "Rick Bota", "wiki_page": "Hellraiser:_Hellworld", "imsdb_path": None},
            {"title": "Hellraiser: Revelations", "year": 2011, "director": "Victor Garcia", "wiki_page": "Hellraiser:_Revelations", "imsdb_path": None},
            {"title": "Hellraiser: Judgment", "year": 2018, "director": "Gary J. Tunnicliffe", "wiki_page": "Hellraiser:_Judgment", "imsdb_path": None},
            {"title": "Hellraiser", "year": 2022, "director": "David Bruckner", "wiki_page": "Hellraiser_(2022_film)", "imsdb_path": None},
        ],
    },
}


# ── Main ─────────────────────────────────────────────────────────────────────

def run_franchise(franchise_key):
    """Process all movies in a franchise."""
    franchise = FRANCHISES[franchise_key]
    source_tag = franchise["source"]
    movies = franchise["movies"]

    stats["franchise"] = franchise_key
    stats["source_tag"] = source_tag
    stats["movies_total"] = len(movies)
    stats["start_time"] = time.time()

    log(f"Starting: {franchise_key} ({len(movies)} movies, source: {source_tag})")

    slack_post(
        f":film_projector: *Movie Script Ingest Starting: {franchise_key}*\n"
        f"  Movies: {len(movies)}\n"
        f"  Source: `{source_tag}`\n"
        f"  Pipeline: IMSDb scripts + Wikipedia → vector memory\n"
        f"  Status updates every 5 minutes"
    )

    reporter = Thread(target=status_reporter, daemon=True)
    reporter.start()

    for movie in movies:
        if shutdown.is_set():
            break
        try:
            process_movie(movie, source_tag)
        except Exception as e:
            log(f"    ERROR: {movie['title']}: {e}")
            stats["errors"] += 1
        stats["movies_processed"] += 1
        time.sleep(3)  # Rate limit — respect Wikipedia when running parallel

    shutdown.set()
    elapsed = time.time() - stats["start_time"]

    final_msg = (
        f":white_check_mark: *Movie Script Ingest Complete: {franchise_key}*\n"
        f"  Movies processed: {stats['movies_processed']}/{stats['movies_total']}\n"
        f"  IMSDb scripts: {stats['scripts_found']}\n"
        f"  Wikipedia fallbacks: {stats['wiki_fallbacks']}\n"
        f"  Memories stored: {stats['memories_stored']}\n"
        f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}"
    )
    if stats["errors"]:
        final_msg += f"\n  Errors: {stats['errors']}"
    slack_post(final_msg)
    log(f"Complete: {stats['memories_stored']} memories in {str(timedelta(seconds=int(elapsed)))}")


def main():
    global dry_run

    import argparse
    parser = argparse.ArgumentParser(description="Nova Movie Script Ingest")
    parser.add_argument("--franchise", required=True, choices=list(FRANCHISES.keys()),
                        help="Franchise to ingest")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to memory")
    args = parser.parse_args()

    dry_run = args.dry_run
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    run_franchise(args.franchise)


if __name__ == "__main__":
    main()
