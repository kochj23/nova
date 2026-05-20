#!/usr/bin/env python3
from __future__ import annotations
"""
nova_cartoons_ingest.py — BFS Wikipedia crawl for 5 classic cartoons.

Collectively ingests 10,000 memory chunks across ThunderCats, He-Man,
She-Ra, Voltron, and Fist of the North Star into Nova's PostgreSQL vector
memory. Each show gets its own source label. BFS crawls round-robin across
all 5 queues so coverage stays balanced.

Defaults match nova_robotech_ingest.py (archived → use nova_ingest wikipedia "Robotech"):
  - 3–5s polite delay between Wikipedia API calls
  - 429 exponential backoff
  - Per-page status to #nova-notifications with a random memory snippet
  - Resume-safe: visited state saved to /tmp so restarts pick up mid-crawl
  - Async ingest (POST /remember?async=1)

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

MEMORY_URL    = "http://192.168.1.6:18790/remember?async=1"
TARGET_CHUNKS = 10_000          # collective across all shows
CHUNK_SIZE    = 1_500
DELAY_MIN     = 3.0
DELAY_MAX     = 5.0
STATUS_EVERY  = 5               # notify every N pages
USER_AGENT    = "Nova/1.0 (classic cartoons research bot; kochj23@github)"
VISITED_FILE  = Path("/tmp/nova_cartoons_visited.json")
LOG_FILE      = Path.home() / ".openclaw/logs/cartoons_ingest.log"

# Show definitions: (display name, source label, start URL)
SHOWS = [
    ("ThunderCats",            "thundercats",          "https://en.wikipedia.org/wiki/ThunderCats"),
    ("He-Man",                 "he_man",               "https://en.wikipedia.org/wiki/He-Man"),
    ("She-Ra",                 "she_ra",               "https://en.wikipedia.org/wiki/She-Ra"),
    ("Voltron",                "voltron",              "https://en.wikipedia.org/wiki/Voltron"),
    ("Fist of the North Star", "fist_of_north_star",   "https://en.wikipedia.org/wiki/Fist_of_the_North_Star"),
]

# ── State ─────────────────────────────────────────────────────────────────────

shutdown = False
stats = {
    "total_ingested":  0,
    "pages_processed": 0,
    "errors":          0,
    "by_show":         {s[1]: 0 for s in SHOWS},
    "current_page":    "",
    "current_show":    "",
    "queue_sizes":     {s[1]: 0 for s in SHOWS},
    "start_time":      datetime.now().isoformat(),
}
_start_ts = time.time()


def signal_handler(sig, frame):
    global shutdown
    shutdown = True
    log("Shutdown signal — stopping after current page.")


signal.signal(signal.SIGINT,  signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[cartoons-ingest {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── Notifications ─────────────────────────────────────────────────────────────

def notify(text: str):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception as e:
        log(f"Slack notify failed: {e}")


# ── Wikipedia API ─────────────────────────────────────────────────────────────

def fetch_wiki_page(url: str):
    """Fetch page extract + outgoing links via MediaWiki API.
    Returns: (title, text), [link_urls], error_or_None
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

def chunk_text(text: str) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) >= 40]
    chunks, current = [], ""
    for para in paragraphs:
        candidate = (current + "\n\n" + para).strip() if current else para
        if len(candidate) > CHUNK_SIZE and current:
            chunks.append(current.strip())
            current = para
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


# ── Ingest ────────────────────────────────────────────────────────────────────

def ingest_chunk(text: str, title: str, url: str, source: str) -> bool:
    payload = json.dumps({
        "text":     text,
        "source":   source,
        "metadata": {
            "source":      source,
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

def post_page_status(show_name: str, source: str, title: str, url: str,
                     chunks: list[str], ingested: int):
    pct       = min(stats["total_ingested"] / TARGET_CHUNKS * 100, 100)
    remaining = max(TARGET_CHUNKS - stats["total_ingested"], 0)
    snippet   = random.choice(chunks)[:300].replace("\n", " ").strip() if chunks else "(no content)"
    elapsed   = int((time.time() - _start_ts) / 60)

    by_show_lines = "\n".join(
        f"  • {s[0]}: {stats['by_show'][s[1]]} chunks"
        for s in SHOWS
    )

    msg = (
        f":tv: *Classic Cartoons Ingest* — {pct:.1f}% complete\n"
        f":clapper: *Show:* {show_name}  |  *Page:* {title}\n"
        f":link: {url}\n"
        f":jigsaw: This page: {ingested} chunks | Total: {stats['total_ingested']:,}/{TARGET_CHUNKS:,} | Remaining: {remaining:,}\n"
        f":card_index: Pages done: {stats['pages_processed']} | Errors: {stats['errors']}\n"
        f":stopwatch: Elapsed: {elapsed}m\n\n"
        f"*Chunks by show:*\n{by_show_lines}\n\n"
        f":brain: *Random memory from this page:*\n> {snippet}"
    )
    notify(msg)


# ── Visited state persistence ─────────────────────────────────────────────────

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

def main():
    log(f"Starting classic cartoons ingest — collective target: {TARGET_CHUNKS:,} chunks")
    log(f"Shows: {', '.join(s[0] for s in SHOWS)}")

    visited = load_visited()
    log(f"Resuming with {len(visited)} already-visited URLs")

    # One BFS queue per show, seeded with the show's start URL
    queues = {}
    for show_name, source, start_url in SHOWS:
        q = deque()
        if start_url not in visited:
            q.append(start_url)
        queues[source] = q
        log(f"  {show_name}: queue seeded with {start_url}")

    notify(
        f":tv: *Classic Cartoons Wikipedia Ingest Starting*\n"
        + "\n".join(f"  • {s[0]} → `{s[1]}`" for s in SHOWS)
        + f"\n• *Collective target:* {TARGET_CHUNKS:,} chunks\n"
        f"• Rate limit: {DELAY_MIN}–{DELAY_MAX}s between calls\n"
        f"• Status: every {STATUS_EVERY} pages per show to #nova-notifications\n"
        f"• Resume state: `{VISITED_FILE}`\n"
        f"• Already visited: {len(visited)} URLs"
    )

    show_page_counts = {s[1]: 0 for s in SHOWS}  # pages processed per show
    show_cycle = [s[1] for s in SHOWS]             # round-robin order

    while stats["total_ingested"] < TARGET_CHUNKS and not shutdown:
        # Check if all queues are empty
        if all(len(queues[src]) == 0 for src in show_cycle):
            log("All queues exhausted before reaching target.")
            break

        # Round-robin: one page from each non-empty show queue per cycle
        made_progress = False
        for source in show_cycle:
            if stats["total_ingested"] >= TARGET_CHUNKS or shutdown:
                break

            queue = queues[source]
            if not queue:
                continue

            # Pop next unvisited URL from this show's queue
            url = None
            while queue:
                candidate = queue.popleft()
                if candidate not in visited:
                    url = candidate
                    break

            if url is None:
                continue

            visited.add(url)
            made_progress = True

            show_name = next(s[0] for s in SHOWS if s[1] == source)
            title_display = urllib.parse.unquote(url.split("/wiki/")[-1].replace("_", " "))
            stats["current_page"] = title_display
            stats["current_show"] = show_name
            stats["queue_sizes"][source] = len(queue)

            log(f"[{stats['pages_processed']+1}] [{show_name}] {title_display}")

            result, links, error = fetch_wiki_page(url)
            if error or not result:
                stats["errors"] += 1
                log(f"  Error: {error}")
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                continue

            page_title, text = result
            if not text or len(text) < 100:
                log(f"  Skipping — too short ({len(text)} chars)")
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                continue

            chunks = chunk_text(text)
            ingested_this_page = 0
            for chunk in chunks:
                if stats["total_ingested"] >= TARGET_CHUNKS:
                    break
                if ingest_chunk(chunk, page_title, url, source):
                    stats["total_ingested"] += 1
                    stats["by_show"][source] += 1
                    ingested_this_page += 1

            stats["pages_processed"] += 1
            show_page_counts[source] += 1

            # Enqueue new links into this show's queue
            new_links = [l for l in links if l not in visited]
            queue.extend(new_links)
            stats["queue_sizes"][source] = len(queue)

            log(f"  +{ingested_this_page} chunks ({source}: {stats['by_show'][source]} | total: {stats['total_ingested']}/{TARGET_CHUNKS})")

            # Notify every STATUS_EVERY pages for this show
            if show_page_counts[source] % STATUS_EVERY == 0 or show_page_counts[source] == 1:
                post_page_status(show_name, source, page_title, url, chunks, ingested_this_page)

            # Save visited every 25 pages total
            if stats["pages_processed"] % 25 == 0:
                save_visited(visited)

            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        if not made_progress:
            break

    # ── Done ──────────────────────────────────────────────────────────────────

    save_visited(visited)
    elapsed = int((time.time() - _start_ts) / 60)
    reason  = "shutdown signal"  if shutdown else (
              "target reached"   if stats["total_ingested"] >= TARGET_CHUNKS else
              "all queues exhausted"
             )

    by_show_lines = "\n".join(
        f"  • {s[0]} (`{s[1]}`): {stats['by_show'][s[1]]:,} chunks"
        for s in SHOWS
    )

    final_msg = (
        f":white_check_mark: *Classic Cartoons Ingest Complete* — {reason}\n"
        f":jigsaw: Total chunks: {stats['total_ingested']:,}/{TARGET_CHUNKS:,}\n"
        f":page_facing_up: Pages processed: {stats['pages_processed']:,}\n"
        f":x: Errors: {stats['errors']}\n"
        f":stopwatch: Total time: {elapsed} minutes\n\n"
        f"*By show:*\n{by_show_lines}"
    )
    log(final_msg)
    notify(final_msg)


if __name__ == "__main__":
    main()
