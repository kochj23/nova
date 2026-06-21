#!/usr/bin/env python3
"""
nova_fix_missing_images.py — Hourly scan of nova-journal for posts missing cover images.

Checks all content sections, generates images for any posts missing them,
updates frontmatter, commits and pushes.

Written by Jordan Koch.
"""

import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_notify import notify as bus_notify
from nova_image_utils import generate_image

# ── Config ────────────────────────────────────────────────────────────────────

JOURNAL_DIR = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = JOURNAL_DIR / "content"
STATIC_DIR = JOURNAL_DIR / "static/images"

SECTIONS = {
    "dreams": "dreamlike surreal digital painting, abstract, moody, ethereal",
    "essays": "scholarly illustration, clean composition, academic",
    "opinions": "editorial cartoon style, satirical, bold colors",
    "digests": "collage data visualization, editorial layout, modern",
    "tech-today": "futuristic technology, circuits, neon, cyberpunk",
    "research": "academic research illustration, technical, detailed",
    "after-dark": "late night talk show set, purple blue neon, moody spotlight",
    "operations": "cyberpunk operations center, server racks, holographic displays, dark moody",
    "local": "Los Angeles cityscape, Burbank suburban, editorial photography",
    "synthesis": "abstract neural network, data flow, interconnected nodes, glowing",
}

LOG_FILE = "/tmp/nova-fix-images.log"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[fix-images {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def notify(text):
    parts = text.split("\n", 1)
    title = re.sub(r":[a-z0-9_+-]+:", "", parts[0]).lstrip("*").rstrip("*").strip()
    body = parts[1] if len(parts) > 1 else None
    bus_notify(title, body=body, level="info", category="journal",
               dedup_key="journal-image-repair")


def get_posts_missing_images() -> list[dict]:
    """Scan all sections for posts without cover images."""
    missing = []
    for section, style in SECTIONS.items():
        section_dir = CONTENT_DIR / section
        if not section_dir.exists():
            continue
        for md_file in section_dir.glob("*.md"):
            if md_file.name == "_index.md":
                continue
            content = md_file.read_text()
            # Check if frontmatter has cover: image:
            if "cover:" in content and "image:" in content:
                # Verify the image file actually exists
                match = re.search(r'image:\s*"([^"]+)"', content)
                if match:
                    img_path = STATIC_DIR.parent / match.group(1).lstrip("/")
                    if img_path.exists():
                        continue
            # Missing image
            title = ""
            title_match = re.search(r'title:\s*"([^"]+)"', content)
            if title_match:
                title = title_match.group(1)
            missing.append({
                "file": md_file,
                "section": section,
                "style": style,
                "title": title,
            })
    return missing


def generate_image_for_post(post: dict) -> str | None:
    """Generate a cover image based on the post's section and title."""
    title = post["title"]
    style = post["style"]
    section = post["section"]

    # Reuse an already-generated image for this post if one exists on disk under
    # ANY common extension. The loop bug was posts whose cover ref said .webp while
    # only a .png existed — those don't need a fresh SwarmUI render, just a convert.
    img_dir = STATIC_DIR / section
    slug = post["file"].stem
    for ext in (".webp", ".png", ".jpg", ".jpeg"):
        cand = img_dir / f"{slug}{ext}"
        if cand.exists():
            return str(cand)

    # Clean title for prompt
    clean_title = re.sub(r'[📝🌃💻📄]', '', title).strip()[:60]
    prompt = f"{style}, inspired by: {clean_title}, no text, no words, no letters"

    image_path = generate_image(prompt, 1024, 768)
    return image_path


def add_image_to_post(post: dict, image_path: str) -> bool:
    """Copy image and update post frontmatter."""
    section = post["section"]
    md_file = post["file"]
    title = post["title"]

    # Output WebP — the journal references .webp everywhere (publish_hugo writes
    # .webp refs, deploy expects WebP). The repair previously wrote .png, so the
    # cover ref (.webp) never matched the file (.png) and the post looped as
    # "missing" forever. Convert to .webp here so the ref resolves and it converges.
    slug = md_file.stem
    img_filename = f"{slug}.webp"
    img_dir = STATIC_DIR / section
    img_dir.mkdir(parents=True, exist_ok=True)
    dest = img_dir / img_filename

    try:
        if image_path.lower().endswith(".webp"):
            shutil.copy2(image_path, dest)
        else:
            r = subprocess.run(["cwebp", "-q", "82", image_path, "-o", str(dest)],
                               capture_output=True, timeout=30)
            if r.returncode != 0 or not dest.exists():
                shutil.copy2(image_path, dest)  # fallback: at least the file exists
    except Exception as e:
        log(f"  Failed to write image: {e}")
        return False

    # Update frontmatter
    content = md_file.read_text()
    cover_ref = f"/images/{section}/{img_filename}"

    # Split on frontmatter delimiters — content is between first and second ---
    fm_parts = content.split("---", 2)
    if len(fm_parts) < 3:
        log(f"  Malformed frontmatter in {md_file.name} — skipping")
        return False

    frontmatter, body = fm_parts[1], fm_parts[2]

    # Remove any existing (possibly duplicate or broken) cover block from frontmatter
    frontmatter = re.sub(r'\ncover:[\s\S]*?(?=\n\S|\Z)', '', frontmatter)
    frontmatter = frontmatter.rstrip()

    # Append cover block cleanly inside frontmatter
    frontmatter += f'\ncover:\n  image: "{cover_ref}"\n  alt: "Nova"\n'

    content = "---" + frontmatter + "---" + body

    md_file.write_text(content)
    log(f"  Added image: {cover_ref}")
    return True


def main():
    log("=== Scanning for missing images ===")

    missing = get_posts_missing_images()

    if not missing:
        log("All posts have images. Nothing to fix.")
        return

    log(f"Found {len(missing)} posts missing images:")
    for p in missing:
        log(f"  [{p['section']}] {p['title'][:60]}")

    fixed = 0
    failed = 0
    batch = missing[:10]
    log(f"Processing batch of {len(batch)} (of {len(missing)} total)")

    for post in batch:
        log(f"Generating image for: [{post['section']}] {post['title'][:50]}")
        image_path = generate_image_for_post(post)

        if image_path:
            if add_image_to_post(post, image_path):
                fixed += 1
            else:
                failed += 1
        else:
            failed += 1
            log(f"  Image generation failed for: {post['title'][:50]}")

        time.sleep(5)  # Don't hammer SwarmUI

    # Commit and push if we fixed anything
    if fixed > 0:
        try:
            subprocess.run(["git", "add", "-A"], cwd=str(JOURNAL_DIR), capture_output=True, timeout=30)
            result = subprocess.run(
                ["git", "commit", "-m", f"fix: Add {fixed} missing cover images (auto-repair)"],
                cwd=str(JOURNAL_DIR), capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                subprocess.run(["git", "push"], cwd=str(JOURNAL_DIR), capture_output=True, timeout=60)
                log(f"Committed and pushed {fixed} image fixes")
        except Exception as e:
            log(f"Git push failed: {e}")

    # Report
    if fixed > 0 or failed > 0:
        notify(
            f":frame_with_picture: *Image Auto-Repair Complete*\n"
            f"• Fixed: {fixed} posts now have cover images\n"
            f"• Failed: {failed} (SwarmUI issues)\n"
            f"• Total scanned: {len(missing)} posts were missing images"
        )

    log(f"Done. Fixed: {fixed}, Failed: {failed}")


if __name__ == "__main__":
    main()
