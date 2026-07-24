#!/usr/bin/env python3
"""
nova_mostlycopyandpaste_ingest.py — Ingest mostlycopyandpaste.com (Kevin Duane's
blog: cloud / AI / automation / Linux / devops, Hugo, ~306 articles).

Per Jordan 2026-06-24: "make sure Nova ingests all of these articles going forward."
Runs daily; state-tracked so the first run backfills everything and later runs only
pick up NEW posts. Sources from the RSS feed (/index.xml) AND the /archive/ index so
nothing is missed. Chunks article text into vector memory (source=tech_blog).

stdlib-only, modeled on nova_sam_blog_ingest.py.
"""
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

BLOG_URL   = "https://mostlycopyandpaste.com"
FEED_URL   = BLOG_URL + "/index.xml"
ARCHIVE_URL= BLOG_URL + "/archive/"
STATE_FILE = Path.home() / ".openclaw/workspace/state/mostlycopyandpaste_state.json"
VECTOR_URL = "http://memory-server.digitalnoise.net:18790/remember"
SOURCE     = "tech_blog"
AUTHOR     = "Kevin Duane"
TODAY      = date.today()

# Non-article paths to ignore when scraping links (taxonomy/section/util pages).
SKIP_RE = re.compile(r"/(categories|tags|archive|search|about|page|index\.xml)(/|$)", re.I)


def log(msg):
    print(f"[mcap {datetime.now():%H:%M:%S}] {msg}", flush=True)


def vector_remember(text, source=SOURCE, metadata=None):
    payload = json.dumps({"text": text, "source": source, "metadata": metadata or {}}).encode()
    req = urllib.request.Request(VECTOR_URL + "?async=1", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        log(f"vector_remember error: {e}")


class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__(); self.skip = False; self._data = []; self.title = None; self._in_title = False
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "header", "footer", "nav"): self.skip = True
        if tag == "title": self._in_title = True
    def handle_endtag(self, tag):
        if tag in ("script", "style", "header", "footer", "nav"): self.skip = False
        if tag == "title": self._in_title = False
    def handle_data(self, data):
        if self._in_title and self.title is None: self.title = data.strip()
        if not self.skip: self._data.append(data)
    def get_text(self):
        return " ".join(c.strip() for c in self._data if c.strip())


def fetch(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0 (+nova.digitalnoise.net)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log(f"fetch error {url}: {e}")
        return None


def article_urls():
    """All article URLs: union of the RSS feed and the /archive/ link list."""
    urls = {}   # url -> {title, date}
    # 1) RSS feed — authoritative title + pubDate
    xml = fetch(FEED_URL)
    if xml:
        try:
            for item in ET.fromstring(xml).iter("item"):
                link = (item.findtext("link") or "").strip()
                if link:
                    urls[link.rstrip("/")] = {
                        "title": (item.findtext("title") or "").strip(),
                        "date": (item.findtext("pubDate") or "").strip(),
                    }
        except Exception as e:
            log(f"feed parse error: {e}")
    log(f"feed: {len(urls)} articles")
    # 2) /archive/ — catches everything the (possibly truncated) feed omits
    html = fetch(ARCHIVE_URL) or ""
    for link in re.findall(r'href="([^"]+)"', html):
        if not link.startswith("http"):
            link = BLOG_URL.rstrip("/") + ("/" + link.lstrip("/"))
        if link.startswith(BLOG_URL) and not SKIP_RE.search(link) and link.rstrip("/") != BLOG_URL.rstrip("/"):
            urls.setdefault(link.rstrip("/"), {"title": None, "date": None})
    log(f"feed+archive: {len(urls)} unique article URLs")
    return urls


def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except Exception: pass
    return {"ingested_urls": [], "last_check": None}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    state = load_state()
    ingested = set(state.get("ingested_urls", []))
    found = article_urls()
    new = {u: m for u, m in found.items() if u not in ingested}
    if not new:
        log("No new articles.")
        state["last_check"] = TODAY.isoformat(); save_state(state); return
    log(f"{len(new)} new article(s) to ingest")

    done = []
    for url, meta in new.items():
        html = fetch(url)
        if not html:
            continue
        s = HTMLStripper(); s.feed(html); content = s.get_text()
        if not content or len(content) < 200:
            log(f"  skip {url} (too short)"); ingested.add(url); continue
        title = meta.get("title") or s.title or url.rstrip("/").split("/")[-1].replace("-", " ").title()
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", meta.get("date") or "") or re.search(r"(\d{4}-\d{2}-\d{2})", content)
        post_date = dm.group(1) if dm else (meta.get("date") or TODAY.isoformat())

        words = content.split(); chunks = []; cur = []
        for w in words:
            cur.append(w)
            if len(" ".join(cur)) >= 1500:
                chunks.append(" ".join(cur)); cur = []
        if cur: chunks.append(" ".join(cur))

        for i, ch in enumerate(chunks):
            head = f'mostlycopyandpaste.com article: "{title}" ({post_date}):' if i == 0 \
                   else f'mostlycopyandpaste.com article "{title}" (continued):'
            vector_remember(f"{head}\n{ch}", source=SOURCE if i == 0 else "blog_post_chunk",
                            metadata={"type": "blog_post", "author": AUTHOR, "site": "mostlycopyandpaste.com",
                                      "url": url, "title": title, "date": post_date})
        ingested.add(url); done.append(f'"{title}"')
        log(f"  ingested {title} ({len(chunks)} chunks)")

    if done:
        notify(f"mostlycopyandpaste.com: ingested {len(done)} new article(s)",
               body="\n".join(f"• {d}" for d in done[:20]), level="info",
               category="ingest", dedup_key="mostlycopyandpaste-ingest")
    state["ingested_urls"] = sorted(ingested)
    state["last_check"] = TODAY.isoformat(); save_state(state)
    log(f"Done. {len(done)} ingested, {len(ingested)} total tracked.")


if __name__ == "__main__":
    main()
