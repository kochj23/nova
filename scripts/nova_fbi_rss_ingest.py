#!/usr/bin/env python3
"""
nova_fbi_rss_ingest.py — Ingest FBI RSS feed into Nova's vector memory.

Fetches the FBI RSS feed, extracts articles, chunks content, and stores
in the military_history vector (law enforcement / federal operations).

Runs every 6 hours via scheduler. Tracks seen URLs to avoid duplicates.

Written by Jordan Koch (via Claude).
"""

import hashlib
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

FBI_FEEDS = [
    "https://www.fbi.gov/feeds/fbi-top-stories/rss.xml",
    "https://www.fbi.gov/feeds/news-blog/rss.xml",
    "https://www.fbi.gov/feeds/toptenwanted/rss.xml",
    "https://www.fbi.gov/feeds/congressional-testimony/rss.xml",
]
MEMORY_URL = "http://192.168.1.6:18790/remember?async=1"
VECTOR = "law"
STATE_FILE = Path.home() / ".openclaw/workspace/state/fbi_rss_seen.json"
CHUNK_SIZE = 1500


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[fbi-rss {ts}] {msg}", flush=True)


def load_seen() -> set:
    try:
        if STATE_FILE.exists():
            return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        pass
    return set()


def save_seen(seen: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(list(seen)[-500:]))




def fetch_article(url: str) -> str:
    """Fetch full article text from URL."""
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Nova-RSS/1.0")
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Basic HTML to text
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        # Grab the main content area (heuristic)
        if len(text) > 500:
            return text[:8000]
        return text
    except Exception:
        return ""


def chunk_text(text: str, title: str) -> list:
    """Split text into chunks with title prefix."""
    chunks = []
    words = text.split()
    current = f"[FBI] {title}: "
    for word in words:
        if len(current) + len(word) + 1 > CHUNK_SIZE:
            chunks.append(current.strip())
            current = f"[FBI] {title} (cont): "
        current += word + " "
    if current.strip() and len(current.strip()) > 50:
        chunks.append(current.strip())
    return chunks


def ingest_chunk(text: str, metadata: dict):
    """Send a chunk to the memory server."""
    payload = json.dumps({
        "text": text,
        "source": VECTOR,
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def fetch_all_feeds() -> list:
    """Fetch items from all configured FBI feeds."""
    all_items = []
    for feed_url in FBI_FEEDS:
        items = fetch_feed_url(feed_url)
        all_items.extend(items)
        log(f"  {feed_url.split('/')[-2]}: {len(items)} items")
    return all_items


def fetch_feed_url(url: str) -> list:
    """Fetch and parse a single RSS feed."""
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Nova-RSS/1.0")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"  Fetch failed ({url}): {e}")
        return []

    items = []
    for match in re.finditer(r'<item>(.*?)</item>', body, re.DOTALL):
        item_xml = match.group(1)
        title = re.search(r'<title>(.*?)</title>', item_xml, re.DOTALL)
        link = re.search(r'<link>(.*?)</link>', item_xml, re.DOTALL)
        guid = re.search(r'<guid>(.*?)</guid>', item_xml, re.DOTALL)
        desc = re.search(r'<description>(.*?)</description>', item_xml, re.DOTALL)
        pub = re.search(r'<pubDate>(.*?)</pubDate>', item_xml, re.DOTALL)

        item_link = (link.group(1).strip() if link else "") or (guid.group(1).strip() if guid else "")
        items.append({
            "title": (title.group(1).strip() if title else ""),
            "link": item_link,
            "description": (desc.group(1).strip() if desc else ""),
            "pubDate": (pub.group(1).strip() if pub else ""),
        })
    return items


def run():
    log("Fetching FBI RSS feeds...")
    items = fetch_all_feeds()
    if not items:
        log("No items in feeds")
        return

    log(f"Total: {len(items)} items across {len(FBI_FEEDS)} feeds")
    seen = load_seen()
    new_count = 0
    ingested = 0

    for item in items:
        url_hash = hashlib.md5(item["link"].encode()).hexdigest()[:12]
        if url_hash in seen:
            continue

        new_count += 1
        seen.add(url_hash)

        # Try to fetch full article
        full_text = fetch_article(item["link"]) if item["link"] else ""
        content = full_text if len(full_text) > len(item["description"]) else item["description"]

        if not content or len(content) < 50:
            content = f"{item['title']}. {item['description']}"

        # Clean HTML entities
        content = content.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        content = content.replace("&#39;", "'").replace("&quot;", '"')

        chunks = chunk_text(content, item["title"])
        metadata = {
            "type": "rss_ingest",
            "feed": "fbi.gov",
            "title": item["title"][:200],
            "url": item["link"],
            "published": item["pubDate"],
            "ingested_at": datetime.now().isoformat(),
        }

        for chunk in chunks:
            if ingest_chunk(chunk, metadata):
                ingested += 1

        time.sleep(1)  # Rate limit

    save_seen(seen)
    log(f"Done: {new_count} new articles, {ingested} chunks ingested into '{VECTOR}'")

    if new_count > 0:
        nova_config.post_both(
            f"📋 *FBI RSS Ingest* — {new_count} new articles, {ingested} chunks → `{VECTOR}` vector",
            slack_channel=nova_config.SLACK_NOTIFY
        )


if __name__ == "__main__":
    run()
