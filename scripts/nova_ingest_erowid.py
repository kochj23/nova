#!/usr/bin/env python3
"""
nova_ingest_erowid.py — Spider and ingest erowid.org into Nova's memory.

Crawls all accessible pages on erowid.org, extracts text content,
and stores as vectors in the 'pharmacology' source. BFS crawl with
polite delays. Status updates every 5 minutes to Slack.

Written by Jordan Koch.
"""

import hashlib
import json
import re
import signal
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

VECTOR = "pharmacology"
BASE_URL = "https://www.erowid.org"
STATE_FILE = Path.home() / ".openclaw/cache/erowid_ingest_state.json"
LOG_FILE = Path.home() / ".openclaw/logs/erowid_ingest.log"
MEMORY_SERVER = "http://192.168.1.6:18790"
SLACK_CHANNEL = "#nova-notifications"

DELAY_BETWEEN_PAGES = 2.0  # polite crawl delay (seconds)
STATUS_INTERVAL = 300       # 5 minutes between status updates
CHUNK_SIZE = 800            # chars per memory chunk
CHUNK_OVERLAP = 100         # overlap between chunks

# Paths to prioritize (crawl these first)
PRIORITY_PATHS = [
    "/chemicals/",
    "/experiences/",
    "/library/",
    "/plants/",
    "/animals/",
    "/smartdrinks/",
    "/psychoactives/",
    "/ask/",
]

# Paths to skip
SKIP_PATTERNS = [
    "/references/",
    "/general/big_chart",
    "/freedom/",
    "/donations/",
    "/about/",
    "/search",
    "/sitemap",
    "/includes/",
    "/images/",
    "/columns/",
    ".pdf",
    ".jpg", ".png", ".gif",
    "mailto:",
    "javascript:",
]

_shutdown = False

def _sig_handler(sig, frame):
    global _shutdown
    _shutdown = True

signal.signal(signal.SIGTERM, _sig_handler)
signal.signal(signal.SIGINT, _sig_handler)

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[erowid {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass

def notify(msg: str):
    try:
        nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)
    except Exception as e:
        log(f"Slack notify failed: {e}")

# ── State Management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"visited": [], "queue": [], "chunks_ingested": 0, "pages_scraped": 0, "errors": 0}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Memory Storage ────────────────────────────────────────────────────────────

def remember(text: str, metadata: dict, done_hashes: set) -> bool:
    text_hash = hashlib.md5(text.encode()).hexdigest()
    if text_hash in done_hashes:
        return False
    try:
        r = requests.post(f"{MEMORY_SERVER}/remember", json={
            "text": text,
            "source": VECTOR,
            "tier": "long_term",
            "metadata": metadata,
        }, timeout=10)
        if r.status_code == 200:
            done_hashes.add(text_hash)
            return True
        elif r.status_code == 409:
            done_hashes.add(text_hash)
            return False
    except Exception as e:
        log(f"Memory server error: {e}")
    return False

def random_mem() -> str:
    try:
        r = requests.post(f"{MEMORY_SERVER}/recall", json={
            "query": "psychoactive substance pharmacology effects",
            "limit": 5, "source": VECTOR
        }, timeout=5)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                import random
                pick = random.choice(results)
                return pick.get("text", "")[:200]
    except Exception:
        pass
    return ""

# ── Web Crawling ──────────────────────────────────────────────────────────────

def fetch_page(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Nova/1.0 (personal AI memory; polite crawler; kochj23@github)",
        })
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except Exception as e:
        log(f"Fetch error: {url[:60]} — {e}")
    return None

def extract_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html5lib")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc and "erowid.org" not in parsed.netloc:
            continue
        if any(skip in full.lower() for skip in SKIP_PATTERNS):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if clean.startswith(BASE_URL):
            links.append(clean)
    return links

def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html5lib")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()

def extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "html5lib")
    title_tag = soup.find("title")
    return title_tag.get_text(strip=True) if title_tag else "Erowid Page"

def chunk_text(text: str) -> list[str]:
    if len(text) < CHUNK_SIZE:
        return [text] if len(text) > 50 else []
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        if end < len(text):
            # Try to break at a sentence boundary
            for sep in ['. ', '.\n', '\n\n', '\n', ' ']:
                break_at = text.rfind(sep, start + CHUNK_SIZE // 2, end + 100)
                if break_at > start:
                    end = break_at + len(sep)
                    break
        chunk = text[start:end].strip()
        if len(chunk) > 50:
            chunks.append(chunk)
        start = end - CHUNK_OVERLAP
    return chunks

def is_garbage(text: str) -> bool:
    if len(text) < 60:
        return True
    if text.count("©") > 2 or "cookie" in text.lower()[:100]:
        return True
    words = text.split()
    if len(words) < 10:
        return True
    return False

# ── Main Crawl Loop ───────────────────────────────────────────────────────────

def build_seed_queue() -> list[str]:
    seeds = [BASE_URL + "/"]
    for path in PRIORITY_PATHS:
        seeds.append(BASE_URL + path)
    return seeds

def main():
    state = load_state()
    visited = set(state.get("visited", []))
    queue = deque(state.get("queue", []))
    chunks_ingested = state.get("chunks_ingested", 0)
    pages_scraped = state.get("pages_scraped", 0)
    errors = state.get("errors", 0)
    done_hashes = set()
    recent_pages = []

    if not queue:
        for seed in build_seed_queue():
            if seed not in visited:
                queue.append(seed)

    log(f"Starting Erowid ingest: {len(queue)} in queue, {len(visited)} visited, {chunks_ingested} chunks so far")
    notify(
        f":pill: *Erowid Ingest Starting*\n"
        f"  Vector: `{VECTOR}`\n"
        f"  Queue: {len(queue)} URLs\n"
        f"  Already visited: {len(visited)}\n"
        f"  Chunks so far: {chunks_ingested:,}"
    )

    last_status = time.time()
    session_start = time.time()
    session_chunks = 0
    session_pages = 0

    while queue and not _shutdown:
        url = queue.popleft()

        if url in visited:
            continue
        visited.add(url)

        html = fetch_page(url)
        if not html:
            errors += 1
            time.sleep(DELAY_BETWEEN_PAGES)
            continue

        # Extract and store content
        text = extract_text(html)
        title = extract_title(html)
        page_chunks = 0

        if text and len(text) > 100:
            chunks = chunk_text(text)
            for chunk in chunks:
                if is_garbage(chunk):
                    continue
                meta = {"url": url, "title": title, "type": "erowid"}
                if remember(chunk, meta, done_hashes):
                    chunks_ingested += 1
                    session_chunks += 1
                    page_chunks += 1

        pages_scraped += 1
        session_pages += 1
        recent_pages.append({"title": title[:60], "url": url, "chunks": page_chunks})
        if len(recent_pages) > 20:
            recent_pages = recent_pages[-20:]

        # Discover new links
        new_links = extract_links(html, url)
        added = 0
        for link in new_links:
            if link not in visited and link not in queue:
                queue.append(link)
                added += 1

        if page_chunks > 0:
            log(f"  [{pages_scraped}] {title[:50]} — {page_chunks} chunks, +{added} links")

        # Status update every 5 minutes
        now = time.time()
        if now - last_status >= STATUS_INTERVAL:
            last_status = now
            elapsed = (now - session_start) / 60
            rate = session_chunks / max(elapsed, 0.1)

            # 3 random memory samples
            samples = []
            for _ in range(3):
                m = random_mem()
                if m:
                    samples.append(f"  :thought_balloon: _{m[:150].replace(chr(10), ' ')}_")

            # Recent pages summary
            recent_5 = recent_pages[-5:]
            recent_text = "\n".join(f"  • {p['title']} ({p['chunks']} chunks)" for p in recent_5)

            # Next pages preview
            next_5 = list(queue)[:5]
            next_text = "\n".join(f"  • {urlparse(u).path[:60]}" for u in next_5)

            msg = (
                f":pill: *Erowid Ingest Status* ({elapsed:.0f} min)\n"
                f"  :jigsaw: {chunks_ingested:,} total chunks | +{session_chunks:,} this session\n"
                f"  :page_facing_up: {pages_scraped:,} pages | Queue: {len(queue):,}\n"
                f"  :zap: {rate:.0f} chunks/min\n\n"
                f"*Last 5 pages:*\n{recent_text}\n\n"
            )
            if samples:
                msg += "*3 random memories:*\n" + "\n".join(samples) + "\n\n"
            if next_text:
                msg += f"*Next up:*\n{next_text}"

            notify(msg)

            # Save state periodically
            save_state({
                "visited": list(visited),
                "queue": list(queue),
                "chunks_ingested": chunks_ingested,
                "pages_scraped": pages_scraped,
                "errors": errors,
            })

        time.sleep(DELAY_BETWEEN_PAGES)

    # Final save and notification
    save_state({
        "visited": list(visited),
        "queue": list(queue),
        "chunks_ingested": chunks_ingested,
        "pages_scraped": pages_scraped,
        "errors": errors,
    })

    reason = "shutdown signal" if _shutdown else "queue empty"
    log(f"Erowid ingest stopped ({reason}): {chunks_ingested:,} chunks, {pages_scraped} pages, {errors} errors")
    notify(
        f":checkered_flag: *Erowid Ingest {'Paused' if _shutdown else 'Complete'}*\n"
        f"  Reason: {reason}\n"
        f"  :jigsaw: {chunks_ingested:,} total chunks\n"
        f"  :page_facing_up: {pages_scraped:,} pages scraped\n"
        f"  :link: {len(queue):,} URLs remaining in queue\n"
        f"  :x: {errors} errors\n"
        f"  Vector: `{VECTOR}`"
    )


if __name__ == "__main__":
    main()
