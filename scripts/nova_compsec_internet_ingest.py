#!/usr/bin/env python3
"""
nova_compsec_internet_ingest.py — Recursive Wikipedia ingest starting from Computer Security, IoT, Internet, ISPs, and Computing Hardware.

Crawls from Computer Security, IoT, Internet, ISP, and Computing Hardware pages, follows links, classifies content into
specific music sub-vectors, and ingests into Nova's PG vector memory.

Target: 10,000 chunks. BFS crawl, 3+ levels deep.

Written by Jordan Koch.
"""

import json
import re
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

MEMORY_URL = "http://127.0.0.1:18790/remember?async=1"
TARGET_CHUNKS = 10000
STATUS_INTERVAL = 300
CHUNK_SIZE = 1500
DELAY_BETWEEN_PAGES = 5.0

START_URLS = ["https://en.wikipedia.org/wiki/Computer_security", "https://en.wikipedia.org/wiki/Internet_of_things", "https://en.wikipedia.org/wiki/Internet", "https://en.wikipedia.org/wiki/Internet_service_provider", "https://en.wikipedia.org/wiki/History_of_computing_hardware"]

# Music sub-vector classification
VECTOR_CATEGORIES = {
    "compsec_core": [
        "computer security", "cybersecurity", "information security",
        "vulnerability", "exploit", "malware", "ransomware",
        "phishing", "zero-day", "penetration testing", "firewall",
    ],
    "compsec_crypto": [
        "cryptography", "encryption", "aes", "rsa", "hash",
        "tls", "ssl", "certificate", "public key", "cipher",
        "digital signature", "blockchain",
    ],
    "compsec_network": [
        "network security", "intrusion detection", "vpn", "proxy",
        "packet", "tcp/ip", "dns", "ddos", "botnet",
        "man-in-the-middle", "sniffing",
    ],
    "internet_core": [
        "internet", "world wide web", "http", "html", "url",
        "browser", "website", "domain name", "ip address",
        "arpanet", "tcp/ip", "packet switching",
    ],
    "internet_isp": [
        "internet service provider", "isp", "broadband", "fiber",
        "cable", "dsl", "dial-up", "bandwidth", "latency",
        "telecommunications", "carrier", "peering",
    ],
    "iot_core": [
        "internet of things", "iot", "smart home", "sensor",
        "embedded", "arduino", "raspberry pi", "mqtt",
        "zigbee", "z-wave", "bluetooth", "wearable",
    ],
    "computing_hardware": [
        "computer hardware", "processor", "cpu", "memory", "ram",
        "motherboard", "gpu", "transistor", "integrated circuit",
        "semiconductor", "moore's law", "vacuum tube",
    ],
    "computing_history": [
        "history", "eniac", "univac", "ibm", "mainframe",
        "personal computer", "microprocessor", "apple", "intel",
        "turing", "von neumann", "babbage", "punch card",
    ],
    "computing_os": [
        "operating system", "linux", "unix", "windows", "macos",
        "kernel", "file system", "process", "thread",
        "virtualization", "container",
    ],
    "computing_networking": [
        "ethernet", "wifi", "router", "switch", "protocol",
        "osi model", "lan", "wan", "subnet", "nat",
        "ipv4", "ipv6", "bgp", "routing",
    ],
    "technology_general": [],  # fallback
}

# ── State ─────────────────────────────────────────────────────────────────────

shutdown = False
stats = {
    "pages_processed": 0,
    "chunks_ingested": 0,
    "queue_size": 0,
    "current_page": "",
    "current_vector": "",
    "errors": 0,
    "by_vector": {},
    "last_pages": [],
}
last_status_time = 0


def signal_handler(sig, frame):
    global shutdown
    shutdown = True
    log("Shutdown requested...")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[compsec-ingest {ts}] {msg}", flush=True)


def notify(text):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception as e:
        log(f"Slack notify failed: {e}")


# ── Classification ────────────────────────────────────────────────────────────

def classify_content(title, text):
    combined = (title + " " + text[:2000]).lower()
    scores = {}
    for vector, keywords in VECTOR_CATEGORIES.items():
        if not keywords:
            continue
        score = sum(1 for kw in keywords if kw in combined)
        if score > 0:
            scores[vector] = score

    if not scores:
        return "technology_general"

    return max(scores, key=scores.get)


# ── Wikipedia Fetching ────────────────────────────────────────────────────────

def fetch_wiki_page(url):
    title = url.split("/wiki/")[-1]
    api_url = (
        f"https://en.wikipedia.org/w/api.php?action=query&titles={title}"
        f"&prop=extracts|links&explaintext=1&pllimit=max&format=json"
    )
    req = urllib.request.Request(api_url, headers={"User-Agent": "Nova/1.0 (local research bot; kochj23@github)"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 10 * (attempt + 1)
                log(f"  Rate limited (429), waiting {wait}s...")
                time.sleep(wait)
                continue
            return None, [], str(e)
        except Exception as e:
            return None, [], str(e)
    else:
        return None, [], "rate limited after 5 retries"

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        return None, [], "no pages"

    page = list(pages.values())[0]
    if "missing" in page:
        return None, [], "page missing"

    text = page.get("extract", "")
    page_title = page.get("title", title)

    # Get links
    links = []
    for link in page.get("links", []):
        link_title = link.get("title", "")
        if link.get("ns", 0) == 0 and ":" not in link_title:
            link_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(link_title.replace(' ', '_'))}"
            links.append(link_url)

    return (page_title, text), links, None


def chunk_text(text, title, chunk_size=CHUNK_SIZE):
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para or len(para) < 30:
            continue
        if len(current) + len(para) > chunk_size:
            if current:
                chunks.append(current.strip())
            current = para
        else:
            current += "\n\n" + para if current else para

    if current.strip():
        chunks.append(current.strip())

    return chunks


def ingest_chunk(text, title, vector, url):
    payload = json.dumps({
        "text": text,
        "metadata": {
            "source": vector,
            "title": title,
            "url": url,
            "type": "wikipedia",
            "ingested_at": datetime.now().isoformat(),
            "privacy": "public",
        },
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True
    except Exception:
        return False


# ── Status Reporting ──────────────────────────────────────────────────────────

def post_status():
    top_vectors = sorted(stats["by_vector"].items(), key=lambda x: x[1], reverse=True)[:10]
    vector_lines = "\n".join(f"  • {v}: {c} chunks" for v, c in top_vectors)
    recent = "\n".join(f"  • {p}" for p in stats["last_pages"][-5:])
    pct = (stats["chunks_ingested"] / TARGET_CHUNKS * 100)
    remaining = TARGET_CHUNKS - stats["chunks_ingested"]

    msg = (
        f":brain: *CompSec/Internet Wikipedia Ingest* — {pct:.1f}%\n"
        f":page_facing_up: Pages processed: {stats['pages_processed']}\n"
        f":jigsaw: Chunks ingested: {stats['chunks_ingested']}/{TARGET_CHUNKS}\n"
        f":hourglass: Remaining: {remaining}\n"
        f":link: Queue: {stats['queue_size']} pages\n"
        f":x: Errors: {stats['errors']}\n"
        f":mag: Current: {stats['current_page']}\n"
        f":label: Vector: {stats['current_vector']}\n\n"
        f"*By category:*\n{vector_lines}\n\n"
        f"*Recent pages:*\n{recent}"
    )
    notify(msg)


# ── Main Crawl ────────────────────────────────────────────────────────────────

def main():
    global last_status_time

    log(f"Starting CompSec/Internet Wikipedia ingest — target: {TARGET_CHUNKS} chunks")
    log(f"Start URLs: {START_URLS}")

    notify(
        f":guitar: *CompSec/Internet Wikipedia Ingest Starting*\n"
        f"• Sources: CompSec, IoT, Internet, ISP, Computing Hardware\n"
        f"• Target: {TARGET_CHUNKS} chunks\n"
        f"• Strategy: BFS crawl, classify into music sub-vectors\n"
        f"• Vectors: {', '.join(VECTOR_CATEGORIES.keys())}\n"
        f"• Updates every 5 min"
    )

    queue = deque(START_URLS)
    visited = set()
    last_status_time = time.time()

    while queue and stats["chunks_ingested"] < TARGET_CHUNKS and not shutdown:
        url = queue.popleft()

        if url in visited:
            continue
        visited.add(url)

        stats["queue_size"] = len(queue)

        # Fetch page
        result, links, error = fetch_wiki_page(url)
        if error or not result:
            stats["errors"] += 1
            continue

        title, text = result
        if len(text) < 100:
            continue

        # Classify
        vector = classify_content(title, text)
        stats["current_page"] = title
        stats["current_vector"] = vector

        # Chunk and ingest
        chunks = chunk_text(text, title)
        for chunk in chunks:
            if stats["chunks_ingested"] >= TARGET_CHUNKS:
                break
            if ingest_chunk(chunk, title, vector, url):
                stats["chunks_ingested"] += 1
                stats["by_vector"][vector] = stats["by_vector"].get(vector, 0) + 1

        stats["pages_processed"] += 1
        stats["last_pages"].append(f"{title} [{vector}] ({len(chunks)} chunks)")
        if len(stats["last_pages"]) > 10:
            stats["last_pages"] = stats["last_pages"][-10:]

        log(f"[{stats['chunks_ingested']}/{TARGET_CHUNKS}] {title} → {vector} ({len(chunks)} chunks)")

        # Add links to queue
        for link in links:
            if link not in visited:
                queue.append(link)

        # Status update
        if time.time() - last_status_time >= STATUS_INTERVAL:
            post_status()
            last_status_time = time.time()

        time.sleep(DELAY_BETWEEN_PAGES)

    # Final report
    post_status()
    top_vectors = sorted(stats["by_vector"].items(), key=lambda x: x[1], reverse=True)
    vector_summary = "\n".join(f"  • {v}: {c}" for v, c in top_vectors)

    notify(
        f":checkered_flag: *CompSec/Internet Ingest Complete!*\n"
        f"• Pages: {stats['pages_processed']}\n"
        f"• Chunks: {stats['chunks_ingested']}\n"
        f"• Errors: {stats['errors']}\n\n"
        f"*Final breakdown:*\n{vector_summary}"
    )
    log(f"Done. {stats['chunks_ingested']} chunks from {stats['pages_processed']} pages.")


if __name__ == "__main__":
    main()
