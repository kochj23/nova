#!/usr/bin/env python3
"""
nova_regenerate_all_images.py — Replace all journal images with OpenRouter-generated ones.

Reads each post's frontmatter (title, description, tags) to build a prompt,
generates a new image via OpenRouter with the section-matched model, and
replaces the existing file in static/images/.

Written by Jordan Koch.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_image_utils import _openrouter_generate, SECTION_MODEL_MAP

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
IMAGES_ROOT = HUGO_ROOT / "static/images"
CONTENT_ROOT = HUGO_ROOT / "content"

SECTIONS = ["art", "after-dark", "dreams", "essays", "opinions", "research", "tech-today", "digests"]

SECTION_PROMPT_STYLE = {
    "art": "Create a museum-quality artistic painting. Style: {style}. Subject: {title}. {desc}",
    "after-dark": "Create a dramatic, moody nighttime scene for a late-night history monologue. Topic: {title}. {desc}. Cinematic lighting, dark atmosphere, historical.",
    "dreams": "Create a surreal, dreamlike abstract painting. Ethereal, mysterious, symbolic. Theme: {title}. {desc}",
    "essays": "Create an elegant, intellectual cover image. Academic yet visually striking. Topic: {title}. {desc}",
    "opinions": "Create a bold, editorial-style cover image with strong visual metaphor. Topic: {title}. {desc}",
    "research": "Create a sophisticated academic journal cover illustration. Scientific, detailed, precise. Topic: {title}. {desc}",
    "tech-today": "Create a modern tech-themed cover image with clean lines, digital aesthetic. Topic: {title}. {desc}",
    "digests": "Create a clean, minimal daily digest cover. Abstract geometric shapes, professional. Theme: {title}.",
}

def extract_frontmatter(filepath: Path) -> dict:
    """Extract title, description, and tags from Hugo frontmatter."""
    text = filepath.read_text(errors="ignore")
    fm = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().split("\n"):
                if line.startswith("title:"):
                    fm["title"] = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("description:"):
                    fm["description"] = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("tags:"):
                    fm["tags"] = line.split(":", 1)[1].strip()
    return fm


def build_prompt(section: str, fm: dict) -> str:
    """Build an image generation prompt from post metadata."""
    title = fm.get("title", "Untitled")
    desc = fm.get("description", "")[:150]
    tags = fm.get("tags", "")
    style = ""

    if section == "art":
        if "oil" in tags.lower():
            style = "Oil painting, visible brushstrokes, rich impasto"
        elif "watercolor" in tags.lower():
            style = "Delicate watercolor, soft washes, luminous"
        elif "noir" in tags.lower():
            style = "Black and white film noir photography"
        elif "cyberpunk" in tags.lower():
            style = "Cyberpunk aesthetic, neon lights, futuristic"
        elif "photorealism" in tags.lower():
            style = "Hyperrealistic photograph, 8K, sharp focus"
        elif "art-nouveau" in tags.lower():
            style = "Art nouveau, Alphonse Mucha inspired, ornate"
        elif "surrealism" in tags.lower():
            style = "Surrealist painting, impossible geometry, dreamlike"
        else:
            style = "Oil painting, gallery quality, museum piece"

    template = SECTION_PROMPT_STYLE.get(section, "Create a cover image for: {title}. {desc}")
    return template.format(title=title, desc=desc, style=style)


def find_post_for_image(section: str, image_name: str) -> Path | None:
    """Find the content .md file that corresponds to an image filename."""
    content_dir = CONTENT_ROOT / section
    stem = image_name.replace(".png", "")

    # Try exact match first
    for md in content_dir.glob("*.md"):
        if md.name == "_index.md":
            continue
        md_stem = md.stem
        if md_stem == stem or stem.startswith(md_stem) or md_stem.startswith(stem):
            return md

    # Try date-based match for dreams/digests (e.g., "2026-04-05.png" → "2026-04-05.md")
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", stem)
    if date_match:
        date_prefix = date_match.group(1)
        for md in content_dir.glob(f"{date_prefix}*.md"):
            if md.name != "_index.md":
                return md

    return None


def regenerate_section(section: str, dry_run: bool = False) -> tuple[int, int]:
    """Regenerate all images for a section. Returns (success, failed) counts."""
    images_dir = IMAGES_ROOT / section
    if not images_dir.exists():
        return 0, 0

    images = sorted(images_dir.glob("*.png"))
    success = 0
    failed = 0

    print(f"\n{'='*60}")
    print(f"  Section: {section} — {len(images)} images")
    print(f"{'='*60}")

    for img_path in images:
        post = find_post_for_image(section, img_path.name)
        if post:
            fm = extract_frontmatter(post)
        else:
            fm = {"title": img_path.stem.replace("-", " ").replace("2026 ", "").strip()}

        prompt = build_prompt(section, fm)
        title = fm.get("title", img_path.stem)[:50]

        if dry_run:
            print(f"  [DRY] {img_path.name} → {prompt[:80]}...")
            success += 1
            continue

        print(f"  Generating: {title}...", end=" ", flush=True)

        result = _openrouter_generate(prompt, section)
        if result and Path(result).exists():
            # Replace the existing image
            import shutil
            shutil.copy2(result, img_path)
            Path(result).unlink(missing_ok=True)
            size_kb = img_path.stat().st_size // 1024
            print(f"✓ ({size_kb}KB)")
            success += 1
        else:
            print("✗ FAILED")
            failed += 1

        # Rate limit — be gentle with the API
        time.sleep(2)

    return success, failed


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("DRY RUN — no images will be generated")

    total_success = 0
    total_failed = 0

    for section in SECTIONS:
        s, f = regenerate_section(section, dry_run=dry_run)
        total_success += s
        total_failed += f

    print(f"\n{'='*60}")
    print(f"  COMPLETE: {total_success} succeeded, {total_failed} failed")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
