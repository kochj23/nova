#!/usr/bin/env python3
"""
nova_meatchurch_ingest.py — Ingest all Meat Church BBQ recipes into Nova vector memory.

Crawls https://www.meatchurch.com/blogs/recipes (paginated, ~25 pages),
extracts each recipe link, fetches full content, and stores as chunked
vector memories under the 'recipes' source.

Usage:
  nova_meatchurch_ingest.py              # full run
  nova_meatchurch_ingest.py --dry-run    # show what would be ingested
  nova_meatchurch_ingest.py --resume     # skip already-ingested URLs

Written by Jordan Koch.
"""

import hashlib
import json
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL   = "https://www.meatchurch.com"
INDEX_URL  = BASE_URL + "/blogs/recipes"
MAX_PAGES  = 30
VECTOR     = "recipes"
VECTOR_URL = "http://192.168.1.6:18790/remember"
STATE_FILE = Path.home() / ".openclaw/workspace/state/meatchurch_state.json"
CHUNK_SIZE = 1500
MIN_WORDS  = 30
RATE_LIMIT = 3.0

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg, level="INFO"):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[meatchurch {now}] [{level}] {msg}", flush=True)

def slack_post(text):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception:
        pass

# ── HTML parsing ──────────────────────────────────────────────────────────────

class HTMLStripper(HTMLParser):
    _SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._data = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "br", "tr"):
            self._data.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._data.append(data)

    def get_text(self):
        raw = "".join(self._data)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def strip_html(html):
    s = HTMLStripper()
    s.feed(html)
    return s.get_text()

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch(url, retries=3):
    headers = {"User-Agent": "Nova/2.0 (local research bot; kochj23@github.com)"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                log(f"Fetch failed: {url[:80]} — {e}", "ERROR")
    return None

# ── Recipe link extraction ────────────────────────────────────────────────────

def find_recipe_links(html):
    pattern = r'href="(/blogs/recipes/[a-z0-9][a-z0-9\-]+)"'
    links = re.findall(pattern, html)
    seen = set()
    unique = []
    for link in links:
        if link == "/blogs/recipes" or "?page=" in link:
            continue
        if link not in seen:
            seen.add(link)
            unique.append(BASE_URL + link)
    return unique

# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text):
    words = text.split()
    if len(words) < MIN_WORDS:
        return []
    chunks = []
    current = []
    current_len = 0
    for word in words:
        current.append(word)
        current_len += len(word) + 1
        if current_len >= CHUNK_SIZE:
            chunks.append(" ".join(current))
            current = []
            current_len = 0
    if current and len(current) >= MIN_WORDS:
        chunks.append(" ".join(current))
    elif current and chunks:
        chunks[-1] += " " + " ".join(current)
    return chunks

# ── Vector memory ─────────────────────────────────────────────────────────────

def remember(text, metadata, done_hashes, dry_run=False):
    h = hashlib.md5(text.encode()).hexdigest()
    if h in done_hashes:
        return False
    done_hashes.add(h)
    if dry_run:
        return True
    payload = json.dumps({
        "text": text,
        "source": VECTOR,
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        VECTOR_URL + "?async=1",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        log(f"remember error: {e}", "WARN")
        return False

# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"ingested_urls": [], "last_run": None, "total_chunks": 0}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Extract recipe title ──────────────────────────────────────────────────────

def extract_title(html, url):
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    if m:
        return m.group(1).strip()
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ingest Meat Church recipes")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    state = load_state()
    ingested_urls = set(state.get("ingested_urls", []))
    done_hashes = set()
    total_chunks = 0
    total_recipes = 0
    errors = 0

    log(f"Starting Meat Church recipe ingest (dry_run={args.dry_run}, resume={args.resume})")
    if not args.dry_run:
        slack_post(":cut_of_meat: *Meat Church Recipe Ingest* starting...")

    # Phase 1: Discover all recipe URLs across paginated index
    all_recipe_urls = []
    for page in range(1, MAX_PAGES + 1):
        url = INDEX_URL if page == 1 else f"{INDEX_URL}?page={page}"
        log(f"Fetching index page {page}...")
        html = fetch(url)
        if not html:
            log(f"Page {page} returned nothing — assuming end of pages")
            break
        links = find_recipe_links(html)
        if not links:
            log(f"No recipe links on page {page} — done with discovery")
            break
        all_recipe_urls.extend(links)
        time.sleep(1.0)

    # Deduplicate
    seen = set()
    unique_urls = []
    for u in all_recipe_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    log(f"Discovered {len(unique_urls)} unique recipes")

    if args.resume:
        unique_urls = [u for u in unique_urls if u not in ingested_urls]
        log(f"After filtering already-ingested: {len(unique_urls)} remaining")

    # Phase 2: Fetch and ingest each recipe
    for i, recipe_url in enumerate(unique_urls):
        slug = recipe_url.rstrip("/").split("/")[-1]
        log(f"[{i+1}/{len(unique_urls)}] {slug}")

        html = fetch(recipe_url)
        if not html:
            errors += 1
            continue

        title = extract_title(html, recipe_url)
        text = strip_html(html)

        # Prepend title context to first chunk
        chunks = chunk_text(text)
        if not chunks:
            log(f"  Skipping {slug} — not enough content")
            ingested_urls.add(recipe_url)
            continue

        chunks[0] = f"Meat Church BBQ Recipe: {title}\n\n{chunks[0]}"

        recipe_chunks = 0
        for ci, chunk in enumerate(chunks):
            meta = {
                "url": recipe_url,
                "title": title,
                "source_site": "meatchurch.com",
                "type": "recipe",
                "chunk": ci + 1,
                "total_chunks": len(chunks),
            }
            if remember(chunk, meta, done_hashes, dry_run=args.dry_run):
                recipe_chunks += 1
                total_chunks += 1

        total_recipes += 1
        ingested_urls.add(recipe_url)
        log(f"  {title}: {recipe_chunks} chunks")

        # Progress notification every 25 recipes
        if not args.dry_run and total_recipes % 25 == 0:
            slack_post(
                f":cut_of_meat: *Meat Church Ingest* progress: "
                f"{total_recipes}/{len(unique_urls)} recipes, {total_chunks} chunks"
            )

        # Save state periodically
        if total_recipes % 10 == 0:
            state["ingested_urls"] = list(ingested_urls)
            state["total_chunks"] = total_chunks
            state["last_run"] = datetime.now().isoformat()
            save_state(state)

        time.sleep(RATE_LIMIT)

    # Final state save
    state["ingested_urls"] = list(ingested_urls)
    state["total_chunks"] = total_chunks
    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    summary = (
        f":cut_of_meat: *Meat Church Recipe Ingest Complete*\n"
        f"  :page_facing_up: {total_recipes} recipes ingested\n"
        f"  :jigsaw: {total_chunks} total chunks\n"
        f"  :x: {errors} errors\n"
        f"  :label: Vector: `{VECTOR}`"
    )
    log(summary.replace(":", "").replace("*", ""))
    if not args.dry_run:
        slack_post(summary)


if __name__ == "__main__":
    main()
