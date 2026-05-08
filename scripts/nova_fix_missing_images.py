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
from nova_image_utils import ensure_backend, generate_image

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
}

LOG_FILE = "/tmp/nova-fix-images.log"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[fix-images {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def notify(text):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception:
        pass


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

    # Create image filename
    slug = md_file.stem
    img_filename = f"{slug}.png"
    img_dir = STATIC_DIR / section
    img_dir.mkdir(parents=True, exist_ok=True)
    dest = img_dir / img_filename

    try:
        shutil.copy2(image_path, dest)
    except Exception as e:
        log(f"  Failed to copy image: {e}")
        return False

    # Update frontmatter
    content = md_file.read_text()
    cover_ref = f"/images/{section}/{img_filename}"

    if "cover:" in content and "image:" in content:
        # Replace existing broken reference
        content = re.sub(
            r'cover:\s*\n\s*image:\s*"[^"]*"',
            f'cover:\n  image: "{cover_ref}"',
            content
        )
    else:
        # Add cover block after description or last frontmatter field before ---
        content = re.sub(
            r'(---\s*\n)',
            f'cover:\n  image: "{cover_ref}"\n  alt: "Nova"\n\\1',
            content,
            count=1  # Only match the closing ---
        )
        # More reliable: insert before the closing ---
        parts = content.split("---")
        if len(parts) >= 3:
            parts[1] = parts[1].rstrip() + f'\ncover:\n  image: "{cover_ref}"\n  alt: "Nova"\n'
            content = "---".join(parts)

    md_file.write_text(content)
    log(f"  Added image: {cover_ref}")
    return True


def main():
    log("=== Scanning for missing images ===")

    # Check SwarmUI first
    if not ensure_backend():
        log("SwarmUI not available — skipping this run")
        return

    missing = get_posts_missing_images()

    if not missing:
        log("All posts have images. Nothing to fix.")
        return

    log(f"Found {len(missing)} posts missing images:")
    for p in missing:
        log(f"  [{p['section']}] {p['title'][:60]}")

    fixed = 0
    failed = 0

    for post in missing:
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
