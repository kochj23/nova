#!/usr/bin/env python3
"""
nova_backfill_tags.py — Backfill tags on all existing Hugo posts that have generic tags.

One-time script. Replaces single-word mood tags (e.g., tags: ["surreal"]) on dreams
and single-tag entries across all categories with meaningful multi-word tags.

Run once: python3 nova_backfill_tags.py

Written by Jordan Koch.
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_tag_extractor import extract_tags

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
LOG_FILE = Path.home() / ".openclaw/logs/nova_backfill_tags.log"

MOOD_WORDS = {"surreal", "anxious", "euphoric", "melancholic", "intense", "peaceful",
               "vivid", "dark", "luminous", "fractured", "noir", "warm", "cold",
               "weekly", "news"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def needs_backfill(tags: list[str]) -> bool:
    """Return True if tags are just generic/mood words or very few."""
    if not tags:
        return True
    if len(tags) <= 1:
        return True
    if all(t.lower() in MOOD_WORDS for t in tags):
        return True
    return False


def process_file(md_path: Path, category: str) -> bool:
    """Update tags in a single markdown file. Returns True if modified."""
    content = md_path.read_text(errors="replace")

    # Extract existing tags
    tags_match = re.search(r'^tags:\s*\[(.+?)\]', content, re.MULTILINE)
    if tags_match:
        existing = [t.strip().strip('"\'') for t in tags_match.group(1).split(',')]
    else:
        existing = []

    if not needs_backfill(existing):
        return False

    # Extract title and body
    title_match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else md_path.stem
    fm_end = content.find("---\n", 3)
    body = content[fm_end + 4:].strip()[:1500] if fm_end > 0 else content[:1500]

    new_tags = extract_tags(title, body, category, n=5)
    if not new_tags:
        return False

    tags_yaml = json.dumps(new_tags)

    if tags_match:
        # Replace existing tags line
        new_content = re.sub(
            r'^tags:\s*\[.+?\]',
            f'tags: {tags_yaml}',
            content,
            flags=re.MULTILINE
        )
    else:
        # Insert tags after categories line
        new_content = re.sub(
            r'^(categories:\s*\[.+?\])',
            f'\\1\ntags: {tags_yaml}',
            content,
            flags=re.MULTILINE
        )

    if new_content != content:
        md_path.write_text(new_content)
        return True
    return False


def main():
    log("Starting tag backfill...")
    categories = ["dreams", "essays", "opinions", "tech-today", "after-dark",
                  "art", "research", "digests"]
    updated = 0
    skipped = 0
    errors = 0

    for cat in categories:
        cat_dir = HUGO_ROOT / "content" / cat
        if not cat_dir.exists():
            continue
        posts = [f for f in cat_dir.glob("*.md") if not f.name.startswith("_")]
        log(f"Processing {len(posts)} posts in {cat}...")
        for md_file in sorted(posts):
            try:
                if process_file(md_file, cat):
                    log(f"  Updated: {md_file.name}")
                    updated += 1
                    time.sleep(0.5)  # Don't hammer Ollama
                else:
                    skipped += 1
            except Exception as e:
                log(f"  ERROR {md_file.name}: {e}")
                errors += 1

    log(f"Backfill complete: {updated} updated, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
