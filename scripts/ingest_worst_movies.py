#!/usr/bin/env python3
"""
ingest_worst_movies.py — Download movie scripts from IMSDB/screenplays.io and
ingest them into Nova's vector memory, chunked by ~500 words and classified by genre.

Target list: RT 100 Worst + Razzie Winners (the absolute dregs of cinema).

Written by Jordan Koch.
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
import logging
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config
from nova_notify import notify

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FILE = Path.home() / ".openclaw/logs/ingest_worst_movies_scripts.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

VECTOR_URL = nova_config.VECTOR_URL  # http://memory-server.digitalnoise.net:18790/remember
DELAY = 2  # seconds between HTTP requests (politeness)
CHUNK_SIZE = 500  # words per chunk

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ── Movie List ────────────────────────────────────────────────────────────────

MOVIES = [
    # RT 100 Worst
    {"title": "Ballistic: Ecks vs. Sever", "year": 2002, "genre": "action"},
    {"title": "Left Behind", "year": 2014, "genre": "drama"},
    {"title": "Battlefield Earth", "year": 2000, "genre": "sci_fi"},
    {"title": "Jack and Jill", "year": 2011, "genre": "comedy"},
    {"title": "Speed 2: Cruise Control", "year": 1997, "genre": "action"},
    {"title": "Mortal Kombat: Annihilation", "year": 1997, "genre": "action"},
    {"title": "The Last Airbender", "year": 2010, "genre": "sci_fi"},
    {"title": "The Adventures of Pluto Nash", "year": 2002, "genre": "sci_fi"},
    {"title": "Mac and Me", "year": 1988, "genre": "sci_fi"},
    {"title": "Jaws: The Revenge", "year": 1987, "genre": "horror"},
    {"title": "Highlander II: The Quickening", "year": 1991, "genre": "sci_fi"},
    {"title": "Cool as Ice", "year": 1991, "genre": "drama"},
    {"title": "Staying Alive", "year": 1983, "genre": "drama"},
    {"title": "The Toy", "year": 1982, "genre": "comedy"},
    {"title": "Problem Child", "year": 1990, "genre": "comedy"},
    {"title": "Police Academy 4: Citizens on Patrol", "year": 1987, "genre": "comedy"},
    {"title": "Look Who's Talking Now", "year": 1993, "genre": "comedy"},
    {"title": "Return to the Blue Lagoon", "year": 1991, "genre": "drama"},
    {"title": "Bolero", "year": 1984, "genre": "drama"},
    {"title": "The Master of Disguise", "year": 2002, "genre": "comedy"},
    {"title": "Alone in the Dark", "year": 2005, "genre": "horror"},
    {"title": "House of the Dead", "year": 2003, "genre": "horror"},
    {"title": "BloodRayne", "year": 2005, "genre": "horror"},
    {"title": "Baby Geniuses", "year": 1999, "genre": "comedy"},
    {"title": "The Ridiculous 6", "year": 2015, "genre": "comedy"},
    {"title": "Disaster Movie", "year": 2008, "genre": "comedy"},
    {"title": "Epic Movie", "year": 2007, "genre": "comedy"},
    {"title": "Meet the Spartans", "year": 2008, "genre": "comedy"},
    {"title": "Scary Movie V", "year": 2013, "genre": "comedy"},
    {"title": "Vampires Suck", "year": 2010, "genre": "comedy"},
    {"title": "The Fog", "year": 2005, "genre": "horror"},
    {"title": "Cabin Fever", "year": 2016, "genre": "horror"},
    {"title": "One Missed Call", "year": 2008, "genre": "horror"},
    {"title": "Killing Me Softly", "year": 2002, "genre": "drama"},
    {"title": "Rollerball", "year": 2002, "genre": "action"},
    {"title": "Half Past Dead", "year": 2002, "genre": "action"},
    {"title": "Getaway", "year": 2013, "genre": "action"},
    {"title": "Dark Crimes", "year": 2016, "genre": "crime_drama"},
    {"title": "Twisted", "year": 2004, "genre": "crime_drama"},
    {"title": "Gotti", "year": 2018, "genre": "crime_drama"},
    {"title": "A Thousand Words", "year": 2012, "genre": "comedy"},
    {"title": "Pinocchio", "year": 2002, "genre": "comedy"},
    # Razzie winners
    {"title": "Catwoman", "year": 2004, "genre": "action"},
    {"title": "Gigli", "year": 2003, "genre": "crime_drama"},
    {"title": "Freddy Got Fingered", "year": 2001, "genre": "comedy"},
    {"title": "The Hottie and the Nottie", "year": 2008, "genre": "comedy"},
    {"title": "Saving Christmas", "year": 2014, "genre": "comedy"},
    {"title": "Fifty Shades of Grey", "year": 2015, "genre": "drama"},
    {"title": "The Emoji Movie", "year": 2017, "genre": "comedy"},
    {"title": "Holmes and Watson", "year": 2018, "genre": "comedy"},
    {"title": "Cats", "year": 2019, "genre": "comedy"},
    {"title": "Diana the Musical", "year": 2021, "genre": "drama"},
    {"title": "Blonde", "year": 2022, "genre": "drama"},
    {"title": "Madame Web", "year": 2024, "genre": "action"},
]


# ── HTML Parser to extract script text ────────────────────────────────────────

class ScriptTextExtractor(HTMLParser):
    """Extract text from elements with class 'scrtext' (td, pre, div) or large <pre> blocks.
    IMSDB uses <td class="scrtext"> — all text within is the screenplay."""

    def __init__(self):
        super().__init__()
        self._in_pre = False
        self._in_scrtext = False
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        if "scrtext" in attr_dict.get("class", ""):
            self._in_scrtext = True
        elif tag == "pre" and not self._in_scrtext:
            self._in_pre = True

    def handle_endtag(self, tag):
        # IMSDB wraps in <td class="scrtext"> — end on </td>
        # But since we can't easily track nesting, we just stay in scrtext
        # mode once activated and collect everything. The page structure
        # ensures only script text appears after the scrtext td opens.
        if tag == "pre" and not self._in_scrtext:
            self._in_pre = False

    def handle_data(self, data):
        if self._in_scrtext or self._in_pre:
            self.text_parts.append(data)


class SpringfieldExtractor(HTMLParser):
    """Extract script text from Springfield! Springfield! (scrolling-script-container div)."""

    def __init__(self):
        super().__init__()
        self._in_script = False
        self._depth = 0
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        if "scrolling-script-container" in attr_dict.get("class", ""):
            self._in_script = True
            self._depth = 1
        elif self._in_script and tag == "div":
            self._depth += 1

    def handle_endtag(self, tag):
        if self._in_script and tag == "div":
            self._depth -= 1
            if self._depth <= 0:
                self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.text_parts.append(data)


class GenericTextExtractor(HTMLParser):
    """Fallback: extract all visible text from screenplays.io pages."""

    def __init__(self):
        super().__init__()
        self._skip = False
        self._skip_tags = {"script", "style", "nav", "header", "footer"}
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip = True

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self.text_parts.append(stripped)


# ── URL Generation ────────────────────────────────────────────────────────────

def title_to_imsdb_slugs(title: str) -> list[str]:
    """Generate possible IMSDB URL slugs for a given title."""
    # Remove year parenthetical if present
    clean = re.sub(r"\s*\(\d{4}\)\s*", "", title).strip()
    # Remove special chars except hyphens and spaces
    clean = re.sub(r"[^\w\s\-]", "", clean)
    # Variant 1: spaces to hyphens
    slug1 = re.sub(r"\s+", "-", clean)
    # Variant 2: no hyphens, spaces to %20 (IMSDB style)
    slug2 = re.sub(r"\s+", "%20", clean)
    # Variant 3: The-prefix removed
    slug3 = re.sub(r"^The-", "", slug1)
    slug4 = re.sub(r"^The%20", "", slug2)
    # Variant 5: colons removed but keep words
    clean_no_colon = re.sub(r":\s*", " ", title)
    clean_no_colon = re.sub(r"[^\w\s\-]", "", clean_no_colon).strip()
    slug5 = re.sub(r"\s+", "-", clean_no_colon)
    slug6 = re.sub(r"\s+", "%20", clean_no_colon)

    seen = set()
    results = []
    for s in [slug1, slug2, slug3, slug4, slug5, slug6]:
        if s and s not in seen:
            seen.add(s)
            results.append(s)
    return results


def get_imsdb_urls(title: str) -> list[str]:
    """Generate list of IMSDB URLs to try."""
    urls = []
    for slug in title_to_imsdb_slugs(title):
        urls.append(f"https://imsdb.com/scripts/{slug}.html")
    return urls


def get_springfield_urls(title: str) -> list[str]:
    """Generate Springfield! Springfield! movie script URLs (multiple variants).
    Their pattern: movie_script.php?movie=title-with-hyphens (lowercase, no special chars)."""
    clean = re.sub(r"\s*\(\d{4}\)\s*", "", title).strip()
    # Remove colons, apostrophes, periods, commas
    clean = re.sub(r"[':.,!?]", "", clean)
    # Replace spaces/special with hyphens
    slug = re.sub(r"[^\w]+", "-", clean).strip("-").lower()

    base = "https://www.springfieldspringfield.co.uk/movie_script.php?movie="
    urls = [f"{base}{slug}"]

    # Try without "the-" prefix
    if slug.startswith("the-"):
        urls.append(f"{base}{slug[4:]}")

    # Try with "the-" prefix if not present
    if not slug.startswith("the-"):
        urls.append(f"{base}the-{slug}")

    # For titles with numbers spelled differently, try year suffix
    return urls


def get_screenplaysio_url(title: str) -> str:
    """Generate screenplays.io URL."""
    clean = re.sub(r"\s*\(\d{4}\)\s*", "", title).strip()
    clean = re.sub(r"[^\w\s]", "", clean)
    slug = re.sub(r"\s+", "-", clean).lower()
    return f"https://www.screenplays.io/screenplay/{slug}"


# ── HTTP Fetch ────────────────────────────────────────────────────────────────

def fetch_url(url: str) -> str | None:
    """Fetch a URL and return HTML content, or None on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        log.debug(f"  Fetch failed for {url}: {e}")
    return None


# ── Script Extraction ─────────────────────────────────────────────────────────

def extract_imsdb_script(html: str) -> str | None:
    """Extract script text from IMSDB HTML."""
    parser = ScriptTextExtractor()
    parser.feed(html)
    text = "\n".join(parser.text_parts).strip()
    # IMSDB scripts should be substantial (at least 5000 chars for a real script)
    if len(text) > 5000:
        return text
    # Try looking for any large pre block
    if len(text) > 2000:
        return text
    return None


def extract_springfield_script(html: str) -> str | None:
    """Extract script/transcript from Springfield! Springfield! HTML."""
    parser = SpringfieldExtractor()
    parser.feed(html)
    text = "\n".join(parser.text_parts).strip()
    # Springfield transcripts should be at least a few thousand chars
    if len(text) > 3000:
        return text
    return None


def extract_screenplaysio_script(html: str) -> str | None:
    """Extract script text from screenplays.io HTML."""
    # screenplays.io uses <pre> or <div class="screenplay-text">
    parser = ScriptTextExtractor()
    parser.feed(html)
    text = "\n".join(parser.text_parts).strip()
    if len(text) > 5000:
        return text
    # Fallback: extract body text
    parser2 = GenericTextExtractor()
    parser2.feed(html)
    text = "\n".join(parser2.text_parts).strip()
    # Only return if it looks like a script (has dialogue patterns)
    if len(text) > 10000 and re.search(r"(INT\.|EXT\.|FADE IN|CUT TO)", text):
        return text
    return None


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    """Split text into chunks of approximately chunk_size words."""
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i:i + chunk_size])
        if len(chunk.strip()) > 50:  # skip tiny trailing chunks
            chunks.append(chunk)
    return chunks


# ── Vector Storage ────────────────────────────────────────────────────────────

def store_chunk(text: str, genre: str, title: str, year: int, chunk_num: int, total_chunks: int) -> bool:
    """Store a single chunk in Nova's vector memory."""
    metadata = {
        "title": title,
        "year": year,
        "type": "movie_script",
        "list": "RT Worst / Razzie",
        "chunk": f"{chunk_num}/{total_chunks}",
    }
    payload = json.dumps({
        "text": text,
        "source": genre,
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        VECTOR_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        log.warning(f"  Store failed for {title} chunk {chunk_num}: {e}")
        return False


# ── Main Fetch + Ingest Loop ──────────────────────────────────────────────────

def fetch_script(movie: dict) -> str | None:
    """Try all URL patterns to fetch a script. Returns text or None.
    Order: Springfield Springfield (best coverage) -> IMSDB -> screenplays.io"""
    title = movie["title"]

    # Try Springfield! Springfield! first (has most movies)
    for url in get_springfield_urls(title):
        log.info(f"  Trying: {url}")
        html = fetch_url(url)
        if html:
            text = extract_springfield_script(html)
            if text:
                log.info(f"  Found on Springfield ({len(text)} chars)")
                return text
        time.sleep(DELAY)

    # Try IMSDB (has full screenplays for some films)
    imsdb_urls = get_imsdb_urls(title)
    for url in imsdb_urls[:3]:  # Limit to 3 attempts
        log.info(f"  Trying: {url}")
        html = fetch_url(url)
        if html:
            text = extract_imsdb_script(html)
            if text:
                log.info(f"  Found on IMSDB ({len(text)} chars)")
                return text
        time.sleep(DELAY)

    # Try screenplays.io as last resort
    url = get_screenplaysio_url(title)
    log.info(f"  Trying: {url}")
    html = fetch_url(url)
    if html:
        text = extract_screenplaysio_script(html)
        if text:
            log.info(f"  Found on screenplays.io ({len(text)} chars)")
            return text
    time.sleep(DELAY)

    return None


def process_movie(movie: dict) -> dict:
    """Fetch and ingest a single movie script. Returns stats."""
    title = movie["title"]
    year = movie["year"]
    genre = movie["genre"]

    log.info(f"Processing: {title} ({year}) [{genre}]")

    script_text = fetch_script(movie)
    if not script_text:
        log.warning(f"  SKIP: No script found for {title}")
        return {"title": title, "status": "not_found", "chunks": 0}

    # Chunk the script
    chunks = chunk_text(script_text)
    total_chunks = len(chunks)
    log.info(f"  Chunked into {total_chunks} segments (~500 words each)")

    # Store each chunk
    stored = 0
    failed = 0
    for i, chunk in enumerate(chunks, 1):
        # Prepend context to each chunk
        contextualized = f"[Movie Script: {title} ({year}) - Segment {i}/{total_chunks}]\n\n{chunk}"
        if store_chunk(contextualized, genre, title, year, i, total_chunks):
            stored += 1
        else:
            failed += 1
        # Small delay to not hammer the vector endpoint
        if i % 10 == 0:
            time.sleep(0.5)

    log.info(f"  Stored {stored}/{total_chunks} chunks (failed: {failed})")
    return {"title": title, "status": "ingested", "chunks": stored, "failed": failed}


def main():
    log.info("=" * 70)
    log.info("WORST MOVIES SCRIPT INGEST — Starting")
    log.info(f"Target: {len(MOVIES)} movies from RT Worst + Razzie lists")
    log.info(f"Vector endpoint: {VECTOR_URL}")
    log.info("=" * 70)

    # Notify (central bus): FYI status that the ingest run is starting.
    notify(
        "Worst Movies Script Ingest starting",
        body=f"{len(MOVIES)} movies targeted from RT Worst + Razzie lists",
        level="info",
        category="ingest",
        dedup_key="worst-movies-ingest",
        meta={"host": "studio", "source": "worst_movies"},
    )

    start_time = time.time()
    results = {"ingested": [], "not_found": [], "errors": []}
    total_chunks_stored = 0

    for i, movie in enumerate(MOVIES, 1):
        log.info(f"\n[{i}/{len(MOVIES)}] {'─' * 50}")
        try:
            result = process_movie(movie)
            if result["status"] == "ingested":
                results["ingested"].append(result)
                total_chunks_stored += result["chunks"]
            else:
                results["not_found"].append(result)
        except Exception as e:
            log.error(f"  ERROR processing {movie['title']}: {e}")
            results["errors"].append({"title": movie["title"], "error": str(e)})

        # Progress update every 10 movies
        if i % 10 == 0:
            elapsed = time.time() - start_time
            notify(
                "Worst Movies progress",
                body=(
                    f"{i}/{len(MOVIES)} processed, "
                    f"{len(results['ingested'])} scripts found, {total_chunks_stored} chunks stored "
                    f"({elapsed/60:.1f}m elapsed)"
                ),
                level="info",
                category="ingest",
                dedup_key="worst-movies-ingest",
                meta={"host": "studio", "source": "worst_movies"},
            )

    # Final summary
    elapsed = time.time() - start_time
    summary = (
        f":white_check_mark: *Worst Movies Script Ingest Complete*\n"
        f"- Scripts found & ingested: {len(results['ingested'])}/{len(MOVIES)}\n"
        f"- Total chunks stored: {total_chunks_stored}\n"
        f"- Not found: {len(results['not_found'])}\n"
        f"- Errors: {len(results['errors'])}\n"
        f"- Time: {elapsed/60:.1f} minutes\n"
        f"- Titles ingested: {', '.join(r['title'] for r in results['ingested'][:15])}"
        f"{'...' if len(results['ingested']) > 15 else ''}"
    )
    log.info("\n" + summary)
    _summary_lines = summary.split("\n", 1)
    notify(
        "Worst Movies Script Ingest Complete",
        body=_summary_lines[1].strip() if len(_summary_lines) > 1 else None,
        level="info",
        category="ingest",
        dedup_key="worst-movies-ingest",
        meta={"host": "studio", "source": "worst_movies"},
    )

    # Log not-found titles for reference
    if results["not_found"]:
        not_found_titles = [r["title"] for r in results["not_found"]]
        log.info(f"\nNot found on any source: {', '.join(not_found_titles)}")

    log.info(f"\nDone. Total elapsed: {elapsed/60:.1f} minutes")


if __name__ == "__main__":
    main()
