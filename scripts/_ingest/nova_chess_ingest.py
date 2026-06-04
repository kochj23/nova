#!/usr/bin/env python3
"""
nova_chess_ingest.py — BFS Wikipedia crawler for chess knowledge.

Starts at https://en.wikipedia.org/wiki/Chess, follows all internal
Wikipedia links recursively, chunks and ingests into Nova's vector
memory under source='chess'. Stops when 5,000 memories are stored.

Posts progress to #nova-notifications every 5 minutes.

Features:
  - BFS with visited-URL dedup
  - Polite delays (1-2s per page) to avoid rate limiting
  - 512-word chunks (consistent with other wiki crawlers)
  - Skips disambiguation, talk, file, help, user, special pages
  - Saves state so it can resume if interrupted
  - nohup-safe

Written by Jordan Koch.
"""

import json
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

TARGET_MEMORIES     = 5000
CHUNK_WORDS         = 512
MIN_CHUNK_WORDS     = 40
MEMORY_URL          = nova_config.VECTOR_URL  # already includes /remember
SLACK_CHANNEL       = nova_config.SLACK_NOTIFY
STATE_FILE          = Path.home() / ".openclaw/workspace/state/chess_ingest_state.json"
LOG_FILE            = Path.home() / ".openclaw/logs/nova_chess_ingest.log"

SEED_URL            = "https://en.wikipedia.org/wiki/Chess"
WIKI_API            = "https://en.wikipedia.org/w/api.php"
WIKI_BASE           = "https://en.wikipedia.org/wiki/"
PAGE_DELAY          = 1.5       # seconds between pages (polite)
NOTIFY_INTERVAL     = 300       # 5 minutes between Slack updates

# Skip these Wikipedia namespaces — not article content
SKIP_PREFIXES = [
    "Talk:", "User:", "User_talk:", "Wikipedia:", "Wikipedia_talk:",
    "File:", "File_talk:", "Help:", "Help_talk:", "Category_talk:",
    "Portal_talk:", "Template:", "Template_talk:", "Special:",
    "MOS:", "Draft:", "TimedText:", "Module:",
]

# ── Graceful shutdown ─────────────────────────────────────────────────────────

_shutdown = False

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received — finishing current page then saving state")

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ── Logging ───────────────────────────────────────────────────────────────────

_start_time = datetime.now()

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[chess_ingest {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {
        "memories_stored": 0,
        "pages_processed": 0,
        "pages_failed": 0,
        "visited": [],
        "queue": [SEED_URL],
        "started": datetime.now().isoformat(),
    }

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Slack ─────────────────────────────────────────────────────────────────────

def post_slack(msg: str):
    try:
        nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)
    except Exception:
        pass

# ── Wikipedia fetch ───────────────────────────────────────────────────────────

class WikiLinkParser(HTMLParser):
    """Extract all /wiki/... links from Wikipedia HTML."""
    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self._in_content = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "div" and "mw-parser-output" in attrs_dict.get("class", ""):
            self._in_content = True
        if tag == "a" and self._in_content:
            href = attrs_dict.get("href", "")
            if href.startswith("/wiki/") and ":" not in href[6:]:
                self.links.append("https://en.wikipedia.org" + href.split("#")[0])

    def handle_endtag(self, tag):
        if tag == "div":
            self._in_content = False


def fetch_page_text(title: str) -> tuple[str, list[str]]:
    """
    Fetch Wikipedia article text and outbound links via the API.
    Returns (plaintext, [wiki_urls]).
    """
    params = urllib.parse.urlencode({
        "action": "query",
        "titles": title,
        "prop": "extracts|links",
        "explaintext": "1",
        "exsectionformat": "plain",
        "pllimit": "500",
        "format": "json",
        "redirects": "1",
    })
    url = f"{WIKI_API}?{params}"
    headers = {"User-Agent": "Nova-Chess-Ingest/1.0 (personal project; contact via GitHub kochj23)"}

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())

    pages = data.get("query", {}).get("pages", {})
    page = next(iter(pages.values()), {})

    text = page.get("extract", "").strip()
    raw_links = page.get("links", [])
    links = [
        f"https://en.wikipedia.org/wiki/{urllib.parse.quote(l['title'].replace(' ', '_'))}"
        for l in raw_links
        if not any(l["title"].startswith(p) for p in SKIP_PREFIXES)
    ]
    return text, links


def url_to_title(url: str) -> str:
    """Convert Wikipedia URL to article title."""
    return urllib.parse.unquote(url.replace(WIKI_BASE, "").replace("_", " "))


# ── Memory storage ────────────────────────────────────────────────────────────

def chunk_text(text: str) -> list[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), CHUNK_WORDS):
        chunk = " ".join(words[i:i + CHUNK_WORDS])
        if len(chunk.split()) >= MIN_CHUNK_WORDS:
            chunks.append(chunk)
    return chunks


def store_memory(text: str, title: str) -> bool:
    payload = json.dumps({
        "text": nova_config.truncate_at_boundary(text),
        "source": "chess",
        "tier": "long_term",
        "privacy": "local-only",
        "metadata": {
            "type": "wikipedia",
            "title": title,
            "ingested_date": datetime.now().strftime("%Y-%m-%d"),
        },
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception:
        return False


# ── Main BFS crawler ──────────────────────────────────────────────────────────

def main():
    log(f"=== Chess ingest starting — target: {TARGET_MEMORIES:,} memories ===")
    log(f"Seed: {SEED_URL}")

    state = load_state()
    visited = set(state["visited"])
    queue = deque(state["queue"])
    memories_stored = state["memories_stored"]
    pages_processed = state["pages_processed"]
    pages_failed    = state["pages_failed"]

    if memories_stored > 0:
        log(f"Resuming: {memories_stored:,} memories already stored, "
            f"{len(visited):,} pages visited, {len(queue):,} in queue")

    post_slack(
        f":chess_pawn: *Chess Knowledge Ingest — Starting*\n"
        f"Target: {TARGET_MEMORIES:,} memories · BFS from Wikipedia\n"
        f"{'Resuming from ' + str(memories_stored) + ' stored' if memories_stored else 'Fresh start'}"
    )

    last_notify = time.time()
    last_save   = time.time()

    while queue and memories_stored < TARGET_MEMORIES and not _shutdown:
        url = queue.popleft()

        if url in visited:
            continue
        visited.add(url)
        title = url_to_title(url)

        # Skip non-article pages
        if any(title.startswith(p.rstrip(":")) for p in SKIP_PREFIXES):
            continue

        try:
            text, links = fetch_page_text(title)
        except (urllib.error.URLError, TimeoutError) as exc:
            log(f"  FETCH FAIL: {title[:60]} — {exc}")
            pages_failed += 1
            time.sleep(PAGE_DELAY * 2)
            continue
        except Exception as exc:
            log(f"  ERROR: {title[:60]} — {exc}")
            pages_failed += 1
            continue

        if not text or len(text.split()) < 50:
            log(f"  SKIP (thin): {title[:60]}")
            continue

        chunks = chunk_text(text)
        stored_here = 0
        for chunk in chunks:
            if memories_stored >= TARGET_MEMORIES:
                break
            if store_memory(f"[Chess: {title}]\n{chunk}", title):
                memories_stored += 1
                stored_here += 1

        pages_processed += 1
        pct = memories_stored * 100 // TARGET_MEMORIES
        log(f"  [{memories_stored:>5,}/{TARGET_MEMORIES:,} {pct:>2}%] "
            f"+{stored_here} — {title[:60]}")

        # Enqueue new links (BFS: add to back of queue)
        new_links = [l for l in links if l not in visited]
        queue.extend(new_links)

        # Save state every 20 pages
        if pages_processed % 20 == 0:
            state.update({
                "memories_stored": memories_stored,
                "pages_processed": pages_processed,
                "pages_failed": pages_failed,
                "visited": list(visited),
                "queue": list(queue)[:2000],  # cap to avoid huge state file
            })
            save_state(state)
            last_save = time.time()

        # Slack progress every 5 minutes
        now = time.time()
        if now - last_notify >= NOTIFY_INTERVAL:
            elapsed_min = int((now - _start_time.timestamp()) / 60)
            rate = memories_stored / max(elapsed_min, 1)
            eta_min = int((TARGET_MEMORIES - memories_stored) / max(rate, 0.1))
            post_slack(
                f":chess_pawn: *Chess Ingest — Progress*\n"
                f":brain: {memories_stored:,}/{TARGET_MEMORIES:,} memories ({pct}%)\n"
                f":page_facing_up: {pages_processed:,} pages processed · {pages_failed} failed\n"
                f":chart_with_upwards_trend: {rate:.0f} memories/min · ETA ~{eta_min}min\n"
                f":mag: Last page: _{title[:60]}_\n"
                f":arrow_forward: Queue: {len(queue):,} pages remaining"
            )
            last_notify = now

        time.sleep(PAGE_DELAY)

    # Final save
    state.update({
        "memories_stored": memories_stored,
        "pages_processed": pages_processed,
        "pages_failed": pages_failed,
        "visited": list(visited),
        "queue": list(queue)[:2000],
        "completed": datetime.now().isoformat(),
    })
    save_state(state)

    status = "Complete" if memories_stored >= TARGET_MEMORIES else \
             "Interrupted" if _shutdown else "Queue exhausted"

    elapsed_min = int((time.time() - _start_time.timestamp()) / 60)
    post_slack(
        f":chess_pawn: *Chess Ingest — {status}*\n"
        f":white_check_mark: {memories_stored:,} memories stored\n"
        f":page_facing_up: {pages_processed:,} pages · {pages_failed} failed\n"
        f":clock3: {elapsed_min}min total"
    )
    log(f"=== {status}: {memories_stored:,} memories in {elapsed_min}min ===")


if __name__ == "__main__":
    main()
