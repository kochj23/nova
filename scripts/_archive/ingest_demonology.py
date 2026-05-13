#!/usr/bin/env python3
"""
ingest_demonology.py — Ingest demonology facts into Nova's vector memory.

Reads JSONL from data/demonology_facts.jsonl and POSTs each fact to
the vector memory server at http://127.0.0.1:18790/remember.

Usage:  python3 ingest_demonology.py [--dry-run]

Written by Jordan Koch.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

VECTOR_URL = "http://127.0.0.1:18790/remember"
DATA_FILE = Path(__file__).parent / "data" / "demonology_facts.jsonl"
BATCH_DELAY = 0.05  # 50ms between requests to avoid hammering


def ingest(dry_run=False):
    if not DATA_FILE.exists():
        print(f"ERROR: {DATA_FILE} not found")
        sys.exit(1)

    lines = DATA_FILE.read_text().strip().split("\n")
    total = len(lines)
    success = 0
    failed = 0
    skipped = 0

    print(f"Ingesting {total} demonology facts into Nova's memory...")
    print(f"Target: {VECTOR_URL}")
    if dry_run:
        print("DRY RUN — no data will be sent\n")
    print()

    for i, line in enumerate(lines, 1):
        try:
            fact = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"  [{i}/{total}] SKIP — invalid JSON: {e}")
            skipped += 1
            continue

        text = fact.get("text", "")
        source = fact.get("source", "demonology")
        metadata = fact.get("metadata", {})

        if not text:
            skipped += 1
            continue

        payload = json.dumps({
            "text": text,
            "source": source,
            "metadata": metadata
        }).encode()

        if dry_run:
            success += 1
            if i <= 3 or i == total:
                print(f"  [{i}/{total}] OK (dry) — {text[:80]}...")
            elif i == 4:
                print(f"  ... ({total - 4} more) ...")
            continue

        try:
            req = urllib.request.Request(
                VECTOR_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            resp = urllib.request.urlopen(req, timeout=10)
            if resp.status == 200:
                success += 1
                if i % 50 == 0 or i == total:
                    print(f"  [{i}/{total}] ingested...")
            else:
                failed += 1
                print(f"  [{i}/{total}] FAIL — HTTP {resp.status}")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{total}] ERROR — {e}")

        time.sleep(BATCH_DELAY)

    print(f"\nDone. {success} ingested, {failed} failed, {skipped} skipped out of {total} total.")

    # Verify count
    if not dry_run:
        try:
            stats = json.loads(urllib.request.urlopen("http://127.0.0.1:18790/stats", timeout=5).read())
            demon_count = stats.get("by_source", {}).get("demonology", 0)
            total_count = stats.get("count", "?")
            print(f"Vector DB: {demon_count} demonology entries, {total_count} total memories.")
        except Exception:
            pass


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    ingest(dry_run=dry_run)
