#!/usr/bin/env python3
"""
nova_horror_scripts_imsdb.py — Fetch actual screenplays from IMSDB and ingest into Nova's memory.

Written by Jordan Koch.
"""

import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = "http://192.168.1.6:18790/remember"
LOG_FILE = Path("/tmp/nova-horror-scripts-imsdb.log")

SCRIPTS = {
    "Nightmare-on-Elm-Street,-A": ("A Nightmare on Elm Street", 1984),
    "Halloween": ("Halloween", 1978),
    "Thing,-The": ("The Thing", 1982),
    "Scream": ("Scream", 1996),
    "Friday-the-13th": ("Friday the 13th", 1980),
    "Videodrome": ("Videodrome", 1983),
    "Fly,-The": ("The Fly", 1986),
    "Poltergeist": ("Poltergeist", 1982),
    "Lost-Highway": ("Lost Highway", 1997),
    "Wicker-Man,-The": ("The Wicker Man", 1973),
    "Let-the-Right-One-In": ("Let The Right One In", 2008),
    "Fright-Night": ("Fright Night", 1985),
    "Zombieland": ("Zombieland", 2009),
    "Bram-Stoker's-Dracula": ("Bram Stoker's Dracula", 1992),
    "Nosferatu": ("Nosferatu", 1922),
    "Creature-from-the-Black-Lagoon": ("Creature From the Black Lagoon", 1954),
    "It-Follows": ("It Follows", 2015),
    "Trick-'r-Treat": ("Trick 'r Treat", 2009),
}

stats = {"fetched": 0, "stored": 0, "errors": 0, "skipped": 0}


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[horror_imsdb {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def fetch_script(slug):
    url = f"https://imsdb.com/scripts/{slug}.html"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", errors="replace")
        match = re.search(r"<pre>(.*?)</pre>", html, re.DOTALL)
        if match:
            text = re.sub(r"<[^>]+>", "", match.group(1))
            text = text.strip()
            return text
        # Try alternate: some scripts use <td class="scrtext">
        match = re.search(r'class="scrtext">(.*?)</td>', html, re.DOTALL)
        if match:
            text = re.sub(r"<[^>]+>", "", match.group(1))
            return text.strip()
    except Exception as e:
        log(f"  Fetch failed for {slug}: {e}")
    return None


def chunk_text(text, max_chars=1600):
    if len(text) <= max_chars:
        return [text]
    chunks = []
    lines = text.split("\n")
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > max_chars:
            if current:
                chunks.append(current.strip())
            current = line
        else:
            current = current + "\n" + line if current else line
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if len(c.strip()) > 50]


def ingest(text, title, year):
    chunks = chunk_text(text)
    stored = 0
    for i, chunk in enumerate(chunks):
        if len(chunk.strip()) < 50:
            continue
        chunk_title = f"{title} ({year}) screenplay (part {i+1}/{len(chunks)})" if len(chunks) > 1 else f"{title} ({year}) screenplay"
        payload = json.dumps({
            "text": f"Movie screenplay: {chunk_title}\n\n{chunk[:1900]}",
            "source": "movie_script_horror",
            "metadata": {
                "privacy": "local-only",
                "origin": "imsdb-screenplay",
                "title": chunk_title,
                "film": title,
                "year": str(year),
                "type": "screenplay",
            },
        }).encode()
        try:
            req = urllib.request.Request(VECTOR_URL, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            stored += 1
        except Exception:
            stats["errors"] += 1
    return stored


def main():
    log(f"Starting IMSDB horror script ingest — {len(SCRIPTS)} films")
    nova_config.post_both(
        f":movie_camera: *Horror Script Ingest (IMSDB) — Starting*\nFetching {len(SCRIPTS)} actual screenplays from IMSDB and ingesting full text.",
        slack_channel=nova_config.SLACK_NOTIFY
    )

    for slug, (title, year) in SCRIPTS.items():
        log(f"  Fetching: {title} ({year})...")
        text = fetch_script(slug)
        if not text or len(text) < 500:
            log(f"    SKIP — no script text found or too short")
            stats["skipped"] += 1
            continue

        stats["fetched"] += 1
        word_count = len(text.split())
        stored = ingest(text, title, year)
        stats["stored"] += stored
        log(f"    OK — {word_count:,} words, {stored} chunks ingested")
        time.sleep(2)  # Be polite to IMSDB

    nova_config.post_both(
        f":white_check_mark: *Horror Script Ingest (IMSDB) — Complete*\n"
        f"• Scripts fetched: {stats['fetched']}/{len(SCRIPTS)}\n"
        f"• Chunks stored: {stats['stored']}\n"
        f"• Skipped: {stats['skipped']}\n"
        f"• Errors: {stats['errors']}",
        slack_channel=nova_config.SLACK_NOTIFY
    )
    log(f"Done! {stats['fetched']} scripts, {stats['stored']} chunks, {stats['errors']} errors.")


if __name__ == "__main__":
    main()
