#!/usr/bin/env python3
"""
nova_robotech_ingest.py — BFS Wikipedia crawl starting from Robotech.

Ingests up to 10,000 memory chunks about Robotech and related topics into
Nova's PostgreSQL vector memory. BFS crawl follows all links from each page,
breadth-first, indefinitely until target is reached.

Features:
  - Polite rate limiting (3-5s between Wikipedia API calls)
  - 429 backoff with exponential wait
  - Per-page status to #nova-notifications with a random memory from that page
  - nohup-safe: writes state to VISITED_FILE so it can resume after restart
  - Source label: "robotech" (fine-grained, won't pollute other domains)
  - Async ingest (POST /remember?async=1) to not block the crawl

Written by Jordan Koch / kochj23
"""

import json
import os
import random
import signal
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

MEMORY_URL      = "http://192.168.1.6:18790/remember?async=1"
TARGET_CHUNKS   = 10_000
CHUNK_SIZE      = 1_500          # chars per memory chunk
SOURCE          = "robotech"
START_URL       = "https://en.wikipedia.org/wiki/Robotech"
DELAY_MIN       = 3.0            # seconds between Wikipedia API calls
DELAY_MAX       = 5.0            # randomised to be polite
STATUS_EVERY    = 5              # post to nova-notifications every N pages
VISITED_FILE    = Path("/tmp/nova_robotech_visited.json")
LOG_FILE        = Path.home() / ".openclaw/logs/robotech_ingest.log"
USER_AGENT      = "Nova/1.0 (Robotech research bot; kochj23@github)"

# ── State ─────────────────────────────────────────────────────────────────────

shutdown  = False
stats = {
    "pages_processed": 0,
    "chunks_ingested": 0,
    "errors":          0,
    "current_page":    "",
    "queue_size":      0,
    "start_time":      datetime.now().isoformat(),
}


def signal_handler(sig, frame):
    global shutdown
    shutdown = True
    log("Shutdown signal received — will stop after current page.")


signal.signal(signal.SIGINT,  signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[robotech-ingest {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── Slack notify ──────────────────────────────────────────────────────────────

def notify(text: str):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception as e:
        log(f"Slack notify failed: {e}")


# ── Wikipedia API ─────────────────────────────────────────────────────────────

def fetch_wiki_page(url: str):
    """Fetch page text + outgoing wiki links via the MediaWiki API.

    Returns: (title, text), [link_urls], error_str_or_None
    Rate-limit 429 handled with exponential backoff.
    """
    encoded_title = url.split("/wiki/")[-1]
    api_url = (
        "https://en.wikipedia.org/w/api.php"
        f"?action=query&titles={encoded_title}"
        "&prop=extracts|links&explaintext=1&pllimit=max&format=json"
    )
    req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})

    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 15 * (attempt + 1)
                log(f"  429 rate-limited — waiting {wait}s (attempt {attempt+1}/6)")
                time.sleep(wait)
                continue
            return None, [], f"HTTP {e.code}"
        except Exception as e:
            if attempt < 5:
                time.sleep(5)
                continue
            return None, [], str(e)
    else:
        return None, [], "rate-limited after 6 retries"

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        return None, [], "no pages in response"

    page = list(pages.values())[0]
    if "missing" in page:
        return None, [], "page missing"

    text       = page.get("extract", "").strip()
    page_title = page.get("title", encoded_title.replace("_", " "))

    # Collect outgoing main-namespace links (no colon = no meta/file/talk pages)
    links = []
    for link in page.get("links", []):
        lt = link.get("title", "")
        if link.get("ns", 0) == 0 and ":" not in lt and lt:
            links.append(
                "https://en.wikipedia.org/wiki/"
                + urllib.parse.quote(lt.replace(" ", "_"))
            )

    return (page_title, text), links, None


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    """Split on paragraph boundaries, keeping chunks <= chunk_size chars."""
    paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) >= 40]
    chunks, current = [], ""
    for para in paragraphs:
        candidate = (current + "\n\n" + para).strip() if current else para
        if len(candidate) > chunk_size and current:
            chunks.append(current.strip())
            current = para
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


# ── Ingest ────────────────────────────────────────────────────────────────────

def ingest_chunk(text: str, title: str, url: str) -> bool:
    payload = json.dumps({
        "text":     text,
        "source":   SOURCE,
        "metadata": {
            "source":      SOURCE,
            "title":       title,
            "url":         url,
            "type":        "wikipedia",
            "privacy":     "public",
            "ingested_at": datetime.now().isoformat(),
        },
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:
        log(f"  Ingest error: {e}")
        return False


# ── Per-page status ───────────────────────────────────────────────────────────

def post_page_status(title: str, url: str, chunks: list[str], ingested: int):
    """Post a detailed status to #nova-notifications with a random memory snippet."""
    pct       = min(stats["chunks_ingested"] / TARGET_CHUNKS * 100, 100)
    remaining = max(TARGET_CHUNKS - stats["chunks_ingested"], 0)
    snippet   = random.choice(chunks)[:300].replace("\n", " ").strip() if chunks else "(no content)"
    elapsed   = int((time.time() - _start_ts) / 60)

    msg = (
        f":robot_face: *Robotech Ingest* — {pct:.1f}% complete\n"
        f":page_facing_up: *Page:* {title}\n"
        f":link: {url}\n"
        f":jigsaw: Chunks this page: {ingested} | Total: {stats['chunks_ingested']}/{TARGET_CHUNKS} | Remaining: {remaining}\n"
        f":card_index: Pages done: {stats['pages_processed']} | Queue: {stats['queue_size']} | Errors: {stats['errors']}\n"
        f":stopwatch: Elapsed: {elapsed}m\n\n"
        f":brain: *Random memory from this page:*\n> {snippet}"
    )
    notify(msg)


# ── Visited state persistence (resume-safe) ───────────────────────────────────

def load_visited() -> set[str]:
    if VISITED_FILE.exists():
        try:
            return set(json.loads(VISITED_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_visited(visited: set[str]):
    try:
        VISITED_FILE.write_text(json.dumps(list(visited)))
    except Exception as e:
        log(f"Could not save visited state: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

_start_ts = time.time()


def main():
    log(f"Starting Robotech Wikipedia ingest — target: {TARGET_CHUNKS} chunks")
    log(f"Start: {START_URL}")
    log(f"Log: {LOG_FILE}")
    log(f"Visited state: {VISITED_FILE}")

    visited = load_visited()
    log(f"Resuming with {len(visited)} already-visited URLs")

    notify(
        f":robot_face: *Robotech Wikipedia Ingest Starting*\n"
        f"• Start: {START_URL}\n"
        f"• Target: {TARGET_CHUNKS:,} memory chunks\n"
        f"• Source label: `{SOURCE}`\n"
        f"• Strategy: BFS, unlimited depth until target reached\n"
        f"• Rate limit: {DELAY_MIN}–{DELAY_MAX}s between Wikipedia calls\n"
        f"• Status: every {STATUS_EVERY} pages to #nova-notifications\n"
        f"• Already visited: {len(visited)} URLs (resume mode)"
    )

    queue = deque()
    if START_URL not in visited:
        queue.append(START_URL)
    # If resuming after early stop, the queue is lost — restart from seed
    if not queue:
        queue.append(START_URL)

    while queue and stats["chunks_ingested"] < TARGET_CHUNKS and not shutdown:
        url = queue.popleft()

        if url in visited:
            continue
        visited.add(url)
        stats["queue_size"] = len(queue)

        title_display = urllib.parse.unquote(url.split("/wiki/")[-1].replace("_", " "))
        stats["current_page"] = title_display
        log(f"[{stats['pages_processed']+1}] Fetching: {title_display}")

        result, links, error = fetch_wiki_page(url)
        if error or not result:
            stats["errors"] += 1
            log(f"  Error: {error}")
            continue

        page_title, text = result
        if not text or len(text) < 100:
            log(f"  Skipping — too short ({len(text)} chars)")
            continue

        chunks = chunk_text(text)
        ingested_this_page = 0
        for chunk in chunks:
            if stats["chunks_ingested"] >= TARGET_CHUNKS:
                break
            if ingest_chunk(chunk, page_title, url):
                stats["chunks_ingested"] += 1
                ingested_this_page += 1

        stats["pages_processed"] += 1
        log(f"  Ingested {ingested_this_page} chunks (total: {stats['chunks_ingested']}/{TARGET_CHUNKS})")

        # Enqueue all new links (BFS — maintain discovery order)
        new_links = [l for l in links if l not in visited]
        queue.extend(new_links)
        stats["queue_size"] = len(queue)

        # Per-page status to nova-notifications
        if stats["pages_processed"] % STATUS_EVERY == 0 or stats["pages_processed"] == 1:
            post_page_status(page_title, url, chunks, ingested_this_page)

        # Save visited state periodically so we can resume
        if stats["pages_processed"] % 25 == 0:
            save_visited(visited)

        # Polite rate limiting
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # ── Done ──────────────────────────────────────────────────────────────────

    save_visited(visited)
    elapsed  = int((time.time() - _start_ts) / 60)
    reason   = "shutdown signal" if shutdown else (
                "target reached" if stats["chunks_ingested"] >= TARGET_CHUNKS else "queue exhausted"
               )

    final_msg = (
        f":white_check_mark: *Robotech Ingest Complete* — {reason}\n"
        f":jigsaw: Chunks ingested: {stats['chunks_ingested']:,}/{TARGET_CHUNKS:,}\n"
        f":page_facing_up: Pages processed: {stats['pages_processed']:,}\n"
        f":x: Errors: {stats['errors']}\n"
        f":stopwatch: Total time: {elapsed} minutes\n"
        f":brain: Source label: `{SOURCE}` — query with `source=robotech`"
    )
    log(final_msg)
    notify(final_msg)


if __name__ == "__main__":
    main()
