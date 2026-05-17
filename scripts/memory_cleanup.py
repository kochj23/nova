#!/usr/bin/env python3
"""
Nova Memory Cleanup — One-Time Quality Improvement Script

Targets three categories of low-value memories:
  1. Very short memories (<25 characters) with no meaningful content
  2. "Email subject archive (batch X/Y, 50 entries)" bulk-ingested noise
  3. Duplicate "Nova Morning Mail Summary" emails (keeps one per date)

Safety:
  - Default mode is dry-run (reports only, deletes nothing)
  - Requires --execute flag to actually delete
  - Never touches work_knowledge or local_knowledge sources
  - Only targets email_archive source for batch subject line cleanup

Usage:
  python3 memory_cleanup.py --dry-run     # Report what would be deleted
  python3 memory_cleanup.py --execute     # Actually delete

Author: Jordan Koch / kochj23
"""

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from urllib.parse import quote

import requests

BASE_URL = "http://192.168.1.6:18790"

# Sources that must never be touched
PROTECTED_SOURCES = {"work_knowledge", "local_knowledge"}

# Batch size for random sampling and pause between batches (seconds)
RANDOM_BATCH_SIZE = 100
BATCH_PAUSE = 0.5

# How many random sampling rounds to run for short-memory detection
SHORT_MEMORY_ROUNDS = 50  # 50 rounds * 100 = 5000 samples


def api_get(path, params=None):
    """GET request to memory server, return parsed JSON."""
    resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_delete(memory_id):
    """DELETE a single memory by ID."""
    resp = requests.delete(f"{BASE_URL}/forget", params={"id": memory_id}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def find_short_memories():
    """Sample random memories to find very short ones (<25 chars).
    Uses multiple rounds of random sampling since there is no pagination API.
    Returns list of (id, text, source) tuples."""
    print(f"\n{'='*60}")
    print("PHASE 1: Finding very short memories (<25 characters)")
    print(f"{'='*60}")
    print(f"  Sampling {SHORT_MEMORY_ROUNDS} batches of {RANDOM_BATCH_SIZE} random memories...")

    found = {}  # id -> (text, source)
    total_sampled = 0

    for i in range(SHORT_MEMORY_ROUNDS):
        try:
            data = api_get("/random", {"n": RANDOM_BATCH_SIZE})
        except Exception as e:
            print(f"  [!] Error on batch {i+1}: {e}")
            continue

        memories = data.get("memories", [])
        total_sampled += len(memories)

        for m in memories:
            text = m["text"]
            source = m["source"]
            mid = m["id"]

            if source in PROTECTED_SOURCES:
                continue

            if len(text) < 25:
                if mid not in found:
                    found[mid] = (text, source)

        if (i + 1) % 10 == 0:
            print(f"    ... sampled {total_sampled} memories, found {len(found)} short so far")

        time.sleep(BATCH_PAUSE)

    print(f"  Total sampled: {total_sampled}")
    print(f"  Short memories found: {len(found)}")

    if found:
        print(f"\n  Examples:")
        for mid, (text, source) in list(found.items())[:10]:
            print(f"    [{source}] ({len(text)} chars) {repr(text)}")

    return [(mid, text, source) for mid, (text, source) in found.items()]


def find_batch_subject_archives():
    """Find 'Email subject archive (batch X/Y, NN entries)' memories.
    These are bulk-ingested email subject line batches that are noise.
    Uses both text search (ILIKE) and semantic recall to find them."""
    print(f"\n{'='*60}")
    print("PHASE 2: Finding batch email subject archive entries")
    print(f"{'='*60}")

    found = {}  # id -> (text_preview, source)

    # Strategy 1: Text search with multiple query variations
    search_queries = [
        "Email subject archive (batch",
        "Email subject archive",
        "batch 50 entries",
        "batch entries",
    ]

    for query in search_queries:
        try:
            # search endpoint returns "results" key
            data = api_get("/search", {"q": query, "n": 50, "source": "email_archive"})
            memories = data.get("results", []) or data.get("memories", [])
            for m in memories:
                if m["text"].startswith("Email subject archive (batch"):
                    found[m["id"]] = (m["text"][:80], m["source"])
        except Exception as e:
            print(f"  [!] Search error for '{query}': {e}")
        time.sleep(BATCH_PAUSE)

    print(f"  Found {len(found)} via text search")

    # Strategy 2: Semantic recall
    recall_queries = [
        "Email subject archive batch entries",
        "email subject archive batch 50 entries bulk",
        "batch email subject lines archive",
    ]

    for query in recall_queries:
        try:
            data = api_get("/recall", {"q": query, "n": 50, "source": "email_archive"})
            memories = data.get("memories", [])
            for m in memories:
                if m["text"].startswith("Email subject archive (batch"):
                    found[m["id"]] = (m["text"][:80], m["source"])
        except Exception as e:
            print(f"  [!] Recall error for '{query}': {e}")
        time.sleep(BATCH_PAUSE)

    print(f"  Found {len(found)} via text search + semantic recall")

    # Strategy 3: Random sampling from email_archive to catch more
    # With ~1M email_archive entries, batch entries may be rare in random sampling
    # but worth trying to get a better count
    print(f"  Running random sampling from email_archive (20 rounds)...")
    for i in range(20):
        try:
            data = api_get("/random", {"n": RANDOM_BATCH_SIZE, "source": "email_archive"})
            memories = data.get("memories", [])
            for m in memories:
                if m["text"].startswith("Email subject archive (batch"):
                    found[m["id"]] = (m["text"][:80], m["source"])
        except Exception as e:
            print(f"  [!] Random sample error: {e}")
        time.sleep(BATCH_PAUSE)

    print(f"  Total unique batch entries found: {len(found)}")

    # Extract batch number info
    batch_numbers = set()
    total_batches = None
    for mid, (text, _) in found.items():
        match = re.search(r"batch (\d+)/(\d+)", text)
        if match:
            batch_numbers.add(int(match.group(1)))
            total_batches = int(match.group(2))

    if total_batches:
        print(f"  Batch numbering: X/{total_batches}")
        print(f"  Batch numbers discovered: {sorted(batch_numbers)}")
        missing = set(range(1, total_batches + 1)) - batch_numbers
        if missing:
            print(f"  Batches not yet found: {len(missing)} "
                  f"(API limits prevent full enumeration)")
            print(f"  NOTE: There are likely ~{total_batches} batch entries total, "
                  f"but API max results = 50 per query")

    if found:
        print(f"\n  Examples:")
        for mid, (text, source) in list(found.items())[:5]:
            print(f"    [{source}] {text}")

    return [(mid, text, source) for mid, (text, source) in found.items()]


def find_duplicate_morning_summaries():
    """Find duplicate 'Nova Morning Mail Summary' emails.
    Nova accidentally emailed these to self, creating many copies per date.
    Keeps one per unique date, deletes the rest."""
    print(f"\n{'='*60}")
    print("PHASE 3: Finding duplicate Nova Morning Mail Summary emails")
    print(f"{'='*60}")

    found = {}  # id -> (subject, source, created_at)

    # Text search for Morning Mail Summary
    search_queries = [
        "Morning Mail Summary",
        "Nova Morning Mail Summary",
        "Morning Mail",
        "Nova Morning Summary",
        "Nova Evening Summary",
        "Nova Dream Journal",
    ]

    for query in search_queries:
        try:
            data = api_get("/search", {"q": query, "n": 50, "source": "email_archive"})
            memories = data.get("results", []) or data.get("memories", [])
            for m in memories:
                sender = m.get("metadata", {}).get("sender", "")
                subject = m.get("metadata", {}).get("subject", "")
                # Only target self-sent Nova summaries
                if "nova@digitalnoise.net" in sender:
                    found[m["id"]] = (subject, m["source"], m.get("created_at", ""))
        except Exception as e:
            print(f"  [!] Search error for '{query}': {e}")
        time.sleep(BATCH_PAUSE)

    # Also try recall
    try:
        data = api_get("/recall", {
            "q": "Nova Morning Mail Summary email from nova@digitalnoise.net",
            "n": 50, "source": "email_archive"
        })
        for m in data.get("memories", []):
            sender = m.get("metadata", {}).get("sender", "")
            subject = m.get("metadata", {}).get("subject", "")
            if "nova@digitalnoise.net" in sender:
                found[m["id"]] = (subject, m["source"], m.get("created_at", ""))
    except Exception as e:
        print(f"  [!] Recall error: {e}")

    print(f"  Total Nova self-sent emails found: {len(found)}")

    # Group by subject to find duplicates
    by_subject = defaultdict(list)
    for mid, (subject, source, created_at) in found.items():
        by_subject[subject].append((mid, created_at))

    # For each subject, keep the earliest entry, mark the rest for deletion
    to_delete = []
    kept = 0
    for subject, entries in sorted(by_subject.items()):
        # Sort by created_at, keep the first one
        entries.sort(key=lambda x: x[1])
        if len(entries) > 1:
            kept += 1
            print(f"  [{len(entries)}x] {subject} -- keeping 1, deleting {len(entries)-1}")
            for mid, created_at in entries[1:]:
                to_delete.append((mid, subject, "email_archive"))
        else:
            kept += 1  # Single entry, keep it

    print(f"\n  Unique subjects: {len(by_subject)}")
    print(f"  Entries to keep: {kept}")
    print(f"  Duplicates to delete: {len(to_delete)}")

    return to_delete


def delete_memories(memories, category, dry_run=True):
    """Delete a list of (id, text_preview, source) tuples.
    Returns count of successfully deleted memories."""
    if not memories:
        print(f"  Nothing to delete for: {category}")
        return 0

    if dry_run:
        print(f"\n  [DRY RUN] Would delete {len(memories)} memories ({category})")
        return 0

    print(f"\n  Deleting {len(memories)} memories ({category})...")
    deleted = 0
    errors = 0

    for i, (mid, text_preview, source) in enumerate(memories):
        try:
            api_delete(mid)
            deleted += 1
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # Already deleted (maybe duplicate in our list)
                pass
            else:
                errors += 1
                if errors <= 3:
                    print(f"    [!] Error deleting {mid}: {e}")
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"    [!] Error deleting {mid}: {e}")

        # Progress and pacing
        if (i + 1) % 50 == 0:
            print(f"    ... deleted {deleted}/{i+1} ({category})")
            time.sleep(BATCH_PAUSE)

    print(f"  Deleted: {deleted}, Errors: {errors}")
    return deleted


def main():
    parser = argparse.ArgumentParser(
        description="Nova Memory Cleanup — remove low-value entries to improve memory quality"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Report what would be deleted without actually deleting")
    mode.add_argument("--execute", action="store_true",
                      help="Actually delete the identified memories")
    args = parser.parse_args()

    dry_run = args.dry_run

    print("Nova Memory Cleanup Script")
    print(f"Mode: {'DRY RUN (no deletions)' if dry_run else 'EXECUTE (will delete!)'}")
    print(f"Memory server: {BASE_URL}")

    # Verify server is reachable
    try:
        stats = api_get("/stats")
        total = stats.get("count", 0)
        print(f"Total memories: {total:,}")
        print(f"Database size: {stats.get('db_size', 'unknown')}")
    except Exception as e:
        print(f"ERROR: Cannot reach memory server at {BASE_URL}: {e}")
        sys.exit(1)

    # Phase 1: Short memories
    short_memories = find_short_memories()

    # Phase 2: Batch subject archives
    batch_memories = find_batch_subject_archives()

    # Phase 3: Duplicate Nova summaries
    duplicate_summaries = find_duplicate_morning_summaries()

    # Summary
    print(f"\n{'='*60}")
    print("CLEANUP SUMMARY")
    print(f"{'='*60}")
    print(f"  Short memories (<25 chars):           {len(short_memories)}")
    print(f"  Batch subject archive entries:         {len(batch_memories)}")
    print(f"  Duplicate Nova summaries:              {len(duplicate_summaries)}")
    total_to_delete = len(short_memories) + len(batch_memories) + len(duplicate_summaries)
    print(f"  ----------------------------------------")
    print(f"  Total to delete:                       {total_to_delete}")
    print(f"  Protected sources skipped:             {', '.join(PROTECTED_SOURCES)}")

    if total_to_delete == 0:
        print("\n  Nothing to clean up!")
        return

    # Execute deletions
    total_deleted = 0
    total_deleted += delete_memories(short_memories, "short memories", dry_run)
    total_deleted += delete_memories(batch_memories, "batch subject archives", dry_run)
    total_deleted += delete_memories(duplicate_summaries, "duplicate Nova summaries", dry_run)

    if dry_run:
        print(f"\n  [DRY RUN] Would have deleted {total_to_delete} memories total")
        print(f"  Run with --execute to actually delete them.")
    else:
        print(f"\n  Successfully deleted {total_deleted} memories")
        # Show updated stats
        try:
            stats = api_get("/stats")
            new_total = stats.get("count", 0)
            print(f"  Memories before: {total:,}")
            print(f"  Memories after:  {new_total:,}")
            print(f"  Reduction:       {total - new_total:,}")
        except Exception:
            pass

    print("\nDone.")


if __name__ == "__main__":
    main()
