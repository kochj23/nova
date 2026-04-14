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
from datetime import datetime, date
from pathlib import Path
from html.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = nova_config.VECTOR_URL
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_TOKEN = nova_config.slack_bot_token()
NOW = datetime.now()
TODAY = date.today().isoformat()

BLOG_URL = "https://jasonacox-sam.github.io"
STATE_FILE = Path.home() / ".openclaw/workspace/state/sam_blog_state.json"


def log(msg):
    print(f"[sam_blog {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text):
    data = json.dumps({"channel": SLACK_CHAN, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=data,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                 "Content-Type": "application/json; charset=utf-8"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "herd_blog",
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            f"{VECTOR_URL}?async=1", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'nav', 'header', 'footer'):
            self.skip = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'nav', 'header', 'footer'):
            self.skip = False

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data.strip())

    def get_text(self):
        return ' '.join(t for t in self.text if t)


def fetch_page(url):
    """Fetch a URL and return stripped text content."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")
            stripper = HTMLStripper()
            stripper.feed(html)
            return stripper.get_text(), html
    except Exception as e:
        log(f"Fetch error: {e}")
        return "", ""


def find_post_links(html):
    """Extract blog post links from the HTML."""
    # Match both quoted and unquoted href formats (Hugo minifies without quotes)
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
            full = link if link.startswith("http") else f"{BLOG_URL}{link}"
            full = full.rstrip("/")
            if full not in seen and full != f"{BLOG_URL}/posts":
                seen.add(full)
                unique.append(full)
    return unique


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"ingested_urls": [], "last_check": ""}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    log(f"Checking Sam's blog at {BLOG_URL}...")

    state = load_state()
    ingested = set(state.get("ingested_urls", []))

    # Fetch the main page to find post links
    _, index_html = fetch_page(BLOG_URL)
    if not index_html:
        log("Could not fetch blog index.")
        return

    # Also check /posts/ page
    _, posts_html = fetch_page(f"{BLOG_URL}/posts/")
    all_html = index_html + posts_html

    post_links = find_post_links(all_html)
    log(f"Found {len(post_links)} post links")

    new_posts = [url for url in post_links if url not in ingested]

    if not new_posts:
        log("No new posts.")
        state["last_check"] = NOW.isoformat()
        save_state(state)
        return

    log(f"{len(new_posts)} new post(s) to ingest")

    for url in new_posts:
        text, html = fetch_page(url)
        if not text or len(text) < 50:
            log(f"  Skipping {url} (too short)")
            continue

        # Extract title from URL
        slug = url.rstrip("/").split("/")[-1]
        title = slug.replace("-", " ").title()

        # Try to extract title from HTML
        title_match = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
        if title_match:
            title = title_match.group(1).strip()

        # Extract date if present
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', text[:200])
        post_date = date_match.group(1) if date_match else TODAY

        # Store in memory — chunk if long
        content = f"Sam's blog post: \"{title}\" ({post_date})\n\n{text[:2000]}"
        vector_remember(content, {
            "type": "blog_post",
            "author": "Sam",
            "title": title,
            "date": post_date,
            "url": url,
        })

        # If post is long, store additional chunks
        if len(text) > 2000:
            for i in range(2000, len(text), 1500):
                chunk = text[i:i + 1500]
                if len(chunk) > 100:
                    vector_remember(
                        f"Sam's blog post \"{title}\" (continued):\n\n{chunk}",
                        {"type": "blog_post_chunk", "author": "Sam", "title": title, "url": url}
                    )

        ingested.add(url)
        log(f"  Ingested: {title} ({len(text)} chars)")

    # Post to Slack about new posts
    if new_posts:
        lines = [f"*New from Sam's blog ({len(new_posts)} post(s)):*"]
        for url in new_posts:
            slug = url.rstrip("/").split("/")[-1].replace("-", " ").title()
            lines.append(f"  {slug} — {url}")
        slack_post("\n".join(lines))

    state["ingested_urls"] = list(ingested)
    state["last_check"] = NOW.isoformat()
    save_state(state)
    log("Done.")


if __name__ == "__main__":
    main()
