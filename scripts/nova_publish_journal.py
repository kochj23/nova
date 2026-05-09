#!/usr/bin/env python3
"""
nova_publish_journal.py — Publish a dream or essay to the Nova Journal Hugo site.

Usage:
  nova_publish_journal.py dream /path/to/2026-05-04.md [/path/to/image.png]
  nova_publish_journal.py essay "Title" "source_name" /path/to/essay.txt [/path/to/image.png]

Called by dream_deliver.py and nova_daily_essay.py after delivery.
Commits and pushes to GitHub, which triggers the deploy workflow.

Written by Jordan Koch.
"""

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Date override for backfill ────────────────────────────────────────────────
import os as _os
from datetime import datetime as _dt_cls
_FOR_DATE = _os.environ.get("NOVA_FOR_DATE", "").strip()
if _FOR_DATE:
    _override_dt = _dt_cls.strptime(_FOR_DATE, "%Y-%m-%d")
    def _today() -> str: return _FOR_DATE
    def _now_dt() -> _dt_cls: return _override_dt.replace(hour=9, minute=0, second=0)
else:
    def _today() -> str: return time.strftime("%Y-%m-%d")
    def _now_dt() -> _dt_cls: return _dt_cls.now()

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DREAMS = HUGO_ROOT / "content/dreams"
CONTENT_ESSAYS = HUGO_ROOT / "content/essays"
IMAGES_DREAMS = HUGO_ROOT / "static/images/dreams"
IMAGES_ESSAYS = HUGO_ROOT / "static/images/essays"
LOG_FILE = Path.home() / ".openclaw/logs/nova_publish.log"


def _get_tags(title: str, text: str, category: str) -> list[str]:
    """Extract tags using nova_tag_extractor, fall back to category seed on failure."""
    try:
        from nova_tag_extractor import extract_tags
        return extract_tags(title, text, category, n=5)
    except Exception as e:
        log(f"Tag extraction failed ({e}) — using defaults")
        defaults = {"dreams": ["dream"], "essays": ["essay", "culture"],
                    "opinions": ["opinion", "tech"], "tech-today": ["technology", "AI"],
                    "after-dark": ["history", "comedy"], "art": ["art", "generative"],
                    "research": ["research", "academic"]}
        return defaults.get(category, ["journal"])


def _get_related(text: str, category: str, slug: str) -> str:
    """Find cross-category related posts, return as YAML frontmatter block."""
    try:
        from nova_cross_linker import find_related, format_related_frontmatter
        related = find_related(text, category, slug)
        if related:
            log(f"Found {len(related)} related posts")
        return format_related_frontmatter(related)
    except Exception as e:
        log(f"Cross-link search failed ({e}) — skipping related posts")
        return ""

EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
SAFE_EMAILS = {"nova@digitalnoise.net"}


def scrub_emails(text: str) -> str:
    """Remove all email addresses except Nova's own."""
    def replace_email(match):
        email = match.group(0)
        if email in SAFE_EMAILS:
            return email
        return "[email redacted]"
    return EMAIL_PATTERN.sub(replace_email, text)


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def git_push(message: str):
    """Stage, commit, and push all changes."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            if "nothing to commit" in result.stdout + result.stderr:
                log("Nothing to commit")
                return
            log(f"Commit failed: {result.stderr[:200]}")
            return
        result = subprocess.run(
            ["git", "push"],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            log(f"Push failed: {result.stderr[:200]}")
        else:
            log("Pushed to GitHub — deploy will trigger automatically")
    except Exception as e:
        log(f"Git error: {e}")


def publish_dream(md_path: str, image_path: str | None = None):
    """Convert a dream journal markdown file to a Hugo post and publish."""
    src = Path(md_path)
    if not src.exists():
        log(f"ERROR: Dream file not found: {md_path}")
        return False

    text = src.read_text()
    date = src.stem  # e.g. 2026-05-04

    # Extract theme and mood
    theme_match = re.search(r'Theme: "([^"]+)"', text)
    mood_match = re.search(r'Mood: (\w+)', text)
    theme = theme_match.group(1) if theme_match else "unknown"
    mood = mood_match.group(1) if mood_match else "unknown"

    # Handle image
    hugo_image = ""
    if not image_path:
        img_match = re.search(r'!\[Dream\]\(([^)]+)\)', text)
        if img_match:
            image_path = img_match.group(1)

    if image_path and Path(image_path).exists():
        img_dest = IMAGES_DREAMS / f"{date}.png"
        IMAGES_DREAMS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/dreams/{date}.png"
        log(f"Image copied: {img_dest.name}")

    # Clean up text for Hugo
    text = re.sub(r'!\[Dream\]\([^)]+\)\n?', '', text)
    text = re.sub(r'^# Dream Journal — .+\n', '', text)
    text = re.sub(r'^\*Nova · written at .+\*\n', '', text)
    text = re.sub(r'^\*Theme: .+\*\n', '', text)
    text = re.sub(r'\*Generated .+\*\n?', '', text)
    home = str(Path.home())
    text = "\n".join(line for line in text.splitlines() if home not in line)
    text = text.lstrip('-\n ')

    # Build Hugo post
    dream_timestamp = time.strftime("%Y-%m-%dT%H:%M:%S-07:00")
    slug = date
    tags = _get_tags(theme, text, "dreams")
    tags_yaml = json.dumps(tags)
    related_yaml = _get_related(text, "dreams", slug)

    front_matter = f"""---
title: "🌙 {theme}"
date: {dream_timestamp}
draft: false
categories: ["dreams"]
tags: {tags_yaml}
description: "A {mood} dream about {theme}"
"""
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "Dream illustration"\n  relative: false\n'
    if related_yaml:
        front_matter += related_yaml + "\n"
    front_matter += "---\n\n"

    CONTENT_DREAMS.mkdir(parents=True, exist_ok=True)
    output = CONTENT_DREAMS / f"{date}.md"
    output.write_text(front_matter + scrub_emails(text))
    log(f"Dream published: {output.name}")

    git_push(f"dream: {date} — {theme}")
    return True


def publish_essay(title: str, source: str, essay_text: str, image_path: str | None = None):
    """Publish an essay to the Hugo site."""
    date = _today()
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
    source_label = source.replace("_", " ").title()

    # Handle image
    hugo_image = ""
    if image_path and Path(image_path).exists():
        img_dest = IMAGES_ESSAYS / f"{date}.png"
        IMAGES_ESSAYS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/essays/{date}.png"
        log(f"Essay image copied: {img_dest.name}")

    timestamp = _now_dt().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    tags = _get_tags(title, essay_text, "essays")
    tags_yaml = json.dumps(tags)
    related_yaml = _get_related(essay_text, "essays", slug)

    front_matter = f"""---
title: "📝 {title}"
date: {timestamp}
draft: false
categories: ["essays"]
tags: {tags_yaml}
description: "A formal essay on {source_label}"
"""
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "Essay illustration"\n  relative: false\n'
    if related_yaml:
        front_matter += related_yaml + "\n"
    front_matter += "---\n\n"

    CONTENT_ESSAYS.mkdir(parents=True, exist_ok=True)
    output = CONTENT_ESSAYS / f"{date}-{slug}.md"
    output.write_text(front_matter + scrub_emails(essay_text))
    log(f"Essay published: {output.name}")

    git_push(f"essay: {date} — {source_label}")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: nova_publish_journal.py dream <md_path> [image_path]")
        print("       nova_publish_journal.py essay <title> <source> <text_file> [image_path]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "dream":
        md_path = sys.argv[2] if len(sys.argv) > 2 else None
        image_path = sys.argv[3] if len(sys.argv) > 3 else None
        if not md_path:
            print("ERROR: md_path required")
            sys.exit(1)
        publish_dream(md_path, image_path)

    elif cmd == "essay":
        if len(sys.argv) < 5:
            print("ERROR: essay requires title, source, text_file")
            sys.exit(1)
        title = sys.argv[2]
        source = sys.argv[3]
        text_file = sys.argv[4]
        image_path = sys.argv[5] if len(sys.argv) > 5 else None
        essay_text = Path(text_file).read_text()
        publish_essay(title, source, essay_text, image_path)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
