#!/usr/bin/env python3
"""
ingest_bitsavers_ai.py — Ingest historical AI/CS PDFs from bitsavers.org into Nova's memory.

Collections:
  1. Thinking Machines (CM-2, CM-5) → ai_parallel_computing (target: 1500)
  2. MIT AI Lab → ai_foundations (target: 8000)
  3. Symbolics (Lisp Machines) → ai_lisp_machines (target: 3000)
  4. RAND Corp → ai_early_research (target: 2000)
  5. BBN (ARPANET, Lisp) → computing_arpanet (target: 2000)
  6. Stanford AI Lab → ai_stanford (target: 5000)

Posts status to #nova-notifications every 5 minutes.
Written by Jordan Koch.
"""

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

LOG_DIR = Path.home() / ".openclaw/logs"
LOG_FILE = LOG_DIR / "ingest_bitsavers_ai.log"
SLACK_CHANNEL = nova_config.SLACK_NOTIFY
VECTOR_URL = nova_config.VECTOR_URL
BASE_URL = "https://bitsavers.trailing-edge.com/pdf"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bitsavers] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bitsavers")

# Collections to ingest
COLLECTIONS = [
    {
        "name": "Thinking Machines",
        "path": "/thinkingMachines/",
        "vector": "ai_parallel_computing",
        "target": 1500,
        "description": "Connection Machine architecture — massively parallel vector computing",
    },
    {
        "name": "MIT AI Lab",
        "path": "/mit/ai/",
        "vector": "ai_foundations",
        "target": 8000,
        "description": "MIT Artificial Intelligence Laboratory — foundational AI research papers",
    },
    {
        "name": "Symbolics Lisp Machines",
        "path": "/symbolics/",
        "vector": "ai_lisp_machines",
        "target": 3000,
        "description": "Symbolics 3600/I-Machine/G-Machine — AI-native hardware and Lisp OS",
    },
    {
        "name": "RAND Corporation",
        "path": "/rand/",
        "vector": "ai_early_research",
        "target": 2000,
        "description": "RAND — early AI philosophy, JOHNNIAC, IAS computer history",
    },
    {
        "name": "BBN (Bolt Beranek & Newman)",
        "path": "/bbn/",
        "vector": "computing_arpanet",
        "target": 2000,
        "description": "BBN — ARPANET IMPs, BBN-LISP, Tenex OS, early networking",
    },
    {
        "name": "Stanford AI Lab",
        "path": "/stanford/sail/",
        "vector": "ai_stanford",
        "target": 5000,
        "description": "Stanford SAIL — AI research, robotics, NLP, vision",
    },
]

# State
_stats = {
    "total_memories": 0,
    "total_pdfs": 0,
    "current_collection": "",
    "current_pdf": "",
    "last_notify": 0,
    "recent_memories": [],
    "errors": 0,
}


def notify(text):
    try:
        nova_config.post_both(text, slack_channel=SLACK_CHANNEL)
    except Exception as e:
        log.warning(f"Notification failed: {e}")


def notify_status():
    """Post status update every 5 minutes."""
    now = time.time()
    if now - _stats["last_notify"] < 300:
        return
    _stats["last_notify"] = now

    recent = _stats["recent_memories"][-3:] if _stats["recent_memories"] else ["(none yet)"]
    samples = "\n".join(f"  • _{m[:100]}_" for m in recent)

    lines = [
        f":books: *Bitsavers AI Ingest — Status Update*",
        f"",
        f"*Currently ingesting:* {_stats['current_collection']}",
        f"*Current PDF:* `{_stats['current_pdf']}`",
        f"*Progress:* {_stats['total_memories']} memories from {_stats['total_pdfs']} PDFs ({_stats['errors']} errors)",
        f"",
        f"*Recent memories:*",
        samples,
    ]

    # Find next collection
    current_idx = next(
        (i for i, c in enumerate(COLLECTIONS) if c["name"] == _stats["current_collection"]),
        -1,
    )
    if current_idx >= 0 and current_idx < len(COLLECTIONS) - 1:
        nxt = COLLECTIONS[current_idx + 1]
        lines.append(f"\n*Up next:* {nxt['name']} → `{nxt['vector']}` ({nxt['target']} target)")

    notify("\n".join(lines))


def fetch_directory(path: str) -> list[str]:
    """Fetch a bitsavers directory listing and return PDF URLs."""
    url = BASE_URL + path
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Nova/ingest)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # Find all PDF links (relative names only, not full paths)
        pdfs = re.findall(r'href="([^"/][^"]*\.pdf)"', html, re.IGNORECASE)
        # Find subdirectories (relative names only — single component ending in /)
        subdirs = re.findall(r'href="([A-Za-z0-9][^"/]*/)"', html)
        subdirs = [d for d in subdirs if not d.startswith("?")]
        return pdfs, subdirs
    except Exception as e:
        log.warning(f"Failed to fetch directory {url}: {e}")
        return [], []


def crawl_pdfs(base_path: str, max_depth: int = 2, max_pdfs: int = 200) -> list[str]:
    """Recursively crawl a bitsavers directory for PDFs."""
    all_pdfs = []

    def _crawl(path, depth):
        if depth > max_depth or len(all_pdfs) >= max_pdfs:
            return
        pdfs, subdirs = fetch_directory(path)
        for pdf in pdfs:
            if len(all_pdfs) >= max_pdfs:
                break
            full_url = BASE_URL + path + pdf
            all_pdfs.append(full_url)
        for subdir in subdirs:
            if len(all_pdfs) >= max_pdfs:
                break
            _crawl(path + subdir, depth + 1)
            time.sleep(0.5)  # Be polite

    _crawl(base_path, 0)
    return all_pdfs


def download_pdf(url: str) -> bytes | None:
    """Download a PDF, return bytes or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Nova/ingest)"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            if len(data) < 100:
                return None
            # Skip huge files (>50MB)
            if len(data) > 50 * 1024 * 1024:
                log.info(f"Skipping {url} — too large ({len(data) // 1024 // 1024}MB)")
                return None
            return data
    except Exception as e:
        log.warning(f"Download failed {url}: {e}")
        return None


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pypdf."""
    try:
        import pypdf
        import io
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text_parts = []
        for page in reader.pages[:100]:  # Max 100 pages
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
        return "\n".join(text_parts)
    except Exception as e:
        log.warning(f"PDF extraction failed: {e}")
        return ""


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping chunks for embedding."""
    if not text.strip():
        return []
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + chunk_size])
        if len(chunk.strip()) > 50:  # Skip tiny chunks
            chunks.append(chunk.strip())
        i += chunk_size - overlap
    return chunks


def store_memory(text: str, source: str, metadata: dict = None) -> bool:
    """Store a memory in Nova's vector DB."""
    try:
        payload = json.dumps({
            "text": text,
            "source": source,
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception as e:
        log.warning(f"Store failed: {e}")
        return False


def ingest_collection(collection: dict):
    """Ingest a single collection from bitsavers."""
    name = collection["name"]
    path = collection["path"]
    vector = collection["vector"]
    target = collection["target"]

    _stats["current_collection"] = name
    log.info(f"Starting collection: {name} → {vector} (target: {target})")
    notify(f":rocket: *Starting ingest:* {name}\n→ vector `{vector}`, target {target} memories\n_{collection['description']}_")

    # Crawl for PDFs
    max_pdfs = min(target // 5, 200)  # Estimate ~5 chunks per PDF minimum
    pdfs = crawl_pdfs(path, max_depth=3, max_pdfs=max_pdfs)
    log.info(f"Found {len(pdfs)} PDFs in {name}")

    if not pdfs:
        log.warning(f"No PDFs found for {name}")
        return

    memories_this_collection = 0

    for i, pdf_url in enumerate(pdfs):
        if memories_this_collection >= target:
            log.info(f"Reached target ({target}) for {name}")
            break

        pdf_name = pdf_url.rsplit("/", 1)[-1]
        _stats["current_pdf"] = pdf_name
        log.info(f"[{i+1}/{len(pdfs)}] Downloading: {pdf_name}")

        # Download
        pdf_bytes = download_pdf(pdf_url)
        if not pdf_bytes:
            _stats["errors"] += 1
            continue

        # Extract text
        text = extract_text_from_pdf(pdf_bytes)
        if not text or len(text) < 100:
            log.info(f"Skipping {pdf_name} — no extractable text (likely scanned)")
            continue

        # Chunk and store
        chunks = chunk_text(text)
        if not chunks:
            continue

        stored = 0
        for chunk in chunks:
            if memories_this_collection >= target:
                break
            metadata = {
                "source_url": pdf_url,
                "pdf_name": pdf_name,
                "collection": name,
                "ingest_date": datetime.now().isoformat(),
            }
            if store_memory(chunk, vector, metadata):
                stored += 1
                memories_this_collection += 1
                _stats["total_memories"] += 1
                _stats["recent_memories"].append(f"[{pdf_name}] {chunk[:80]}")
                # Keep only last 10
                if len(_stats["recent_memories"]) > 10:
                    _stats["recent_memories"] = _stats["recent_memories"][-10:]

        _stats["total_pdfs"] += 1
        log.info(f"  Stored {stored} chunks from {pdf_name} ({memories_this_collection}/{target})")

        # Status update
        notify_status()

        # Be polite to bitsavers
        time.sleep(1)

    log.info(f"Completed {name}: {memories_this_collection} memories from {_stats['total_pdfs']} PDFs")
    notify(
        f":white_check_mark: *Completed:* {name}\n"
        f"  {memories_this_collection} memories stored in `{vector}`\n"
        f"  Total so far: {_stats['total_memories']} memories from {_stats['total_pdfs']} PDFs"
    )


def main():
    log.info("=" * 60)
    log.info("Bitsavers AI/CS Ingest — Starting")
    log.info(f"Collections: {len(COLLECTIONS)}")
    log.info(f"Total target: {sum(c['target'] for c in COLLECTIONS)} memories")
    log.info("=" * 60)

    notify(
        ":brain: *Bitsavers AI/CS History Ingest — Starting*\n"
        f"Ingesting {len(COLLECTIONS)} collections from bitsavers.org:\n"
        + "\n".join(f"  • {c['name']} → `{c['vector']}` ({c['target']})" for c in COLLECTIONS)
        + f"\n\n*Total target:* {sum(c['target'] for c in COLLECTIONS):,} memories"
    )

    _stats["last_notify"] = time.time()

    for collection in COLLECTIONS:
        try:
            ingest_collection(collection)
        except Exception as e:
            log.error(f"Collection {collection['name']} failed: {e}")
            _stats["errors"] += 1
            notify(f":x: *Error in {collection['name']}:* {e}")

    # Final summary
    log.info("=" * 60)
    log.info(f"COMPLETE: {_stats['total_memories']} memories, {_stats['total_pdfs']} PDFs, {_stats['errors']} errors")
    log.info("=" * 60)

    notify(
        f":tada: *Bitsavers AI/CS Ingest — Complete!*\n"
        f"  *Total memories:* {_stats['total_memories']:,}\n"
        f"  *PDFs processed:* {_stats['total_pdfs']}\n"
        f"  *Errors:* {_stats['errors']}\n"
        f"  *Vectors:* " + ", ".join(f"`{c['vector']}`" for c in COLLECTIONS)
    )


if __name__ == "__main__":
    main()
