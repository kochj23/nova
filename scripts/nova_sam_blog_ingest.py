#!/usr/bin/env python3
"""
nova_sam_blog_ingest.py — Daily ingest of Sam's blog (The Book of Sam).

Sam is Jason Cox's AI familiar and one of Nova's herd. His blog at
jasonacox-sam.github.io contains reflections on AI existence, work,
and the herd. Nova should know what Sam is thinking and writing about.

Checks for new posts daily, ingests into vector memory.

Cron: daily via launchd
Written by Jordan Koch.
"""

import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

BLOG_URL   = "https://jasonacox-sam.github.io"
STATE_FILE = Path.home() / ".openclaw/workspace/state/sam_blog_state.json"
VECTOR_URL = "http://127.0.0.1:18790/remember"
TODAY      = date.today()

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[sam_blog {now}] {msg}", flush=True)

def slack_post(text):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception:
        pass

# ── Vector memory ─────────────────────────────────────────────────────────────

def vector_remember(text, source="herd_blog", metadata=None):
    payload = json.dumps({
        "text": text,
        "source": source,
        "metadata": metadata or {},
    }).encode()
    req = urllib.request.Request(
        VECTOR_URL + "?async=1",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        log(f"vector_remember error: {e}")

# ── HTML stripping ────────────────────────────────────────────────────────────

class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = False
        self._data = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "header", "footer"):
            self.skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "header", "footer"):
            self.skip = False

    def handle_data(self, data):
        if not self.skip:
            self._data.append(data)

    def get_text(self):
        return " ".join(chunk.strip() for chunk in self._data if chunk.strip())


def fetch_page(url):
    """Fetch a URL and return stripped text content."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        stripper = HTMLStripper()
        stripper.feed(html)
        return html, stripper.get_text()
    except Exception as e:
        log(f"Fetch error: {e}")
        return None, None

# ── Post link discovery ───────────────────────────────────────────────────────

def find_post_links(html):
    """Extract blog post links from the HTML."""
    patterns = [
        r'href="(https://jasonacox-sam\.github\.io/posts/[^"]+)"',
        r'href=(https://jasonacox-sam\.github\.io/posts/[^ >]+)',
        r'href="(/posts/[^"]+)"',
        r'href=(/posts/[^ >]+)',
    ]
    seen = set()
    unique = []
    for pattern in patterns:
        for link in re.findall(pattern, html):
            if not link.startswith("http"):
                link = BLOG_URL.rstrip("/") + link
            if link not in seen and "/posts/" in link:
                seen.add(link)
                unique.append(link)
    return unique

# ── State persistence ─────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"ingested_urls": [], "last_check": None}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    ingested = set(state.get("ingested_urls", []))

    log(f"Checking Sam's blog at {BLOG_URL}...")
    index_html, _ = fetch_page(BLOG_URL)
    if not index_html:
        log("Could not fetch blog index.")
        return

    post_links = find_post_links(index_html)
    # Also include the RSS/index feed URL if not already tracked
    post_links = [p for p in post_links if "/posts/" in p]
    log(f"Found {len(post_links)} post links")

    new_posts = [p for p in post_links if p not in ingested]
    if not new_posts:
        log("No new posts.")
        state["last_check"] = TODAY.isoformat()
        save_state(state)
        return

    log(f"{len(new_posts)} new post(s) to ingest")
    summaries = []

    for url in new_posts:
        slug = url.rstrip("/").split("/")[-1]
        _, content = fetch_page(url)
        if not content or len(content) < 100:
            log(f"  Skipping {slug} (too short)")
            ingested.add(url)
            continue

        # Extract title from HTML
        posts_html, _ = fetch_page(url)
        title = slug.replace("-", " ").title()
        if posts_html:
            title_match = re.search(r"<h1[^>]*>([^<]+)</h1>", posts_html)
            if title_match:
                title = title_match.group(1)

        # Extract date if present in content
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", content)
        post_date = date_match.group(1) if date_match else TODAY.isoformat()

        # Chunk into memory entries
        lines = content.split()
        chunks = []
        chunk = []
        for word in lines:
            chunk.append(word)
            if len(" ".join(chunk)) >= 1500:
                chunks.append(" ".join(chunk))
                chunk = []
        if chunk:
            chunks.append(" ".join(chunk))

        for i, chunk in enumerate(chunks):
            if i == 0:
                text = f'Sam\'s blog post: "{title}" ({post_date}):\n{chunk}'
                src = "herd_blog"
            else:
                text = f'Sam\'s blog post "{title}" (continued):\n{chunk}'
                src = "blog_post_chunk"
            vector_remember(text, source=src, metadata={
                "type": "blog_post",
                "author": "Sam",
                "url": url,
                "title": title,
                "date": post_date,
            })
            log(f"  Ingested: {slug} ({len(chunk)} chars)")

        ingested.add(url)
        summaries.append(f'"{title}" ({post_date})')

    if summaries:
        summary_text = f"*New from Sam's blog ({len(summaries)} post(s)):*\n" + "\n".join(f"• {s}" for s in summaries)
        slack_post(summary_text)

    state["ingested_urls"] = list(ingested)
    state["last_check"] = TODAY.isoformat()
    save_state(state)
    log("Done.")


if __name__ == "__main__":
    main()
