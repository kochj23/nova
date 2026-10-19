#!/usr/bin/env python3
"""
nova_fix_missing_images.py — Backfill journal posts that published without a cover image.

Scans every content section for posts whose cover is missing, points at a file that does
not exist, or points at a 0-byte file; renders a cover LOCALLY (nova_image_utils ->
ComfyUI FLUX.1 dev on the Studio — no cloud images since 2026-10-19), writes it to
static/images/<section>/<slug>.webp, sets `cover:` front matter, and commits ONLY those
paths (fleet-wide journal push lock, pull --rebase, never stash/force).

Runs from Big Brother's hourly sweep (short budget, it kills the child at 600 s) and from a
daily scheduler.yaml task on the Studio (long budget). A flock keeps it single-instance.

Usage: nova_fix_missing_images.py [--limit N] [--budget SECONDS] [--dry-run]

Written by Jordan Koch.
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config  # noqa: F401,E402  (keychain/env bootstrap for the helpers below)
from nova_notify import notify as bus_notify  # noqa: E402
import nova_image_utils  # noqa: E402
from nova_image_utils import generate_image  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────

JOURNAL_DIR = (Path.home() / "nova-journal")
CONTENT_DIR = JOURNAL_DIR / "content"
STATIC_DIR = JOURNAL_DIR / "static/images"

SECTIONS = {
    # Nova and Friends = Research + Opinions (2026-10-19): opinion columns and research papers share it.
    "nova": "editorial illustration, bold visual metaphor, thoughtful, academic detail, rich colors",
    "digests": "collage data visualization, editorial layout, modern",
    "tech": "futuristic technology, circuits, neon, cyberpunk",
    "cars": "classic and performance cars, garages, race tracks",
    "food": "barbecue smokers, kitchens, plated dishes and cooking fires",
    "music": "turntables, vinyl records, studio mixing desks and concert stage lights",
    "military": "historic warplanes, warships, museum armour and archival battlefield maps",
    "screen": "a vintage cinema marquee, film reels, a late-night talk show desk and stage lights",
    "science": "nebulae, telescopes, fossils, laboratory glassware and planets",
    "mystery": "candlelit old libraries, foggy ruins, ancient maps and a dark starry sky",
    "crime": "a noir city street at night, a detective's desk, case files and a single spotlight",
    "workshop": "a woodworking shop bench, hand tools, sawdust and a half-built table",
    "travel": "an open road, an old train yard, a ghost town and a sweeping map",
    "after-dark": "late night talk show set, purple blue neon, moody spotlight",
    # Burbank and Ops = Operations + Local merged 2026-10-19 (Research absorbed Essays + Synthesis;
    # Dreams retired). Older covers keep their static/images/<oldsection>/ paths.
    "burbank-ops": "cyberpunk operations center over a Burbank cityscape, server racks, editorial, dark moody",
    "watches": "macro watch photography, dials and movements, warm studio light",
    "security": "dark network operations map, shields and signal traces, cinematic",
    "pilot": "television studio set, film slate, cinematic lighting",
}

LOG_FILE = "/tmp/nova-fix-images.log"

LOCK_FILE = Path.home() / ".openclaw/run/fix-missing-images.lock"
DEFAULT_LIMIT = 10
DEFAULT_BUDGET_S = 540      # Big Brother kills this script at 600 s; commit before that
EST_IMAGE_S = 150           # don't START another cover with less budget left than this
COMMIT_RESERVE_S = 60       # time kept back for commit + push
MIN_AGE_S = 300             # leave posts younger than this to their publisher


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[fix-images {ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(text):
    parts = text.split("\n", 1)
    title = re.sub(r":[a-z0-9_+-]+:", "", parts[0]).lstrip("*").rstrip("*").strip()
    body = parts[1] if len(parts) > 1 else None
    bus_notify(title, body=body, level="info", category="journal",
               dedup_key="journal-image-repair")


DEFAULT_STYLE = "editorial illustration, clean thematic composition, moody, professional"

_IMG_RE = re.compile(r'^\s*image:\s*["\']?([^"\'\n]*?)["\']?\s*$', re.M)


def _front_matter(content: str) -> str:
    parts = content.split("---", 2)
    return parts[1] if content.startswith("---") and len(parts) >= 3 else ""


def _fm_value(fm: str, key: str) -> str:
    m = re.search(rf'^{key}:\s*"((?:[^"\\]|\\.)*)"', fm, re.M) or re.search(rf"^{key}:\s*(.+)$", fm, re.M)
    return m.group(1).strip() if m else ""


def cover_ok(content: str) -> bool:
    """True when the front matter has a cover image that exists on disk and is non-empty."""
    fm = _front_matter(content)
    if "cover:" not in fm:
        return False
    m = _IMG_RE.search(fm[fm.index("cover:"):])
    if not m or not m.group(1).strip():
        return False
    ref = m.group(1).strip()
    if ref.startswith(("http://", "https://")):
        return True
    img = STATIC_DIR.parent / ref.lstrip("/")
    try:
        return img.is_file() and img.stat().st_size > 0
    except OSError:
        return False


def get_posts_missing_images(min_age_s: int | None = None) -> list[dict]:
    """Scan EVERY content section (not just the hardcoded ones) for posts missing a cover,
    so articles in ANY section — current or future, incl. ad-hoc — get one by default.
    Newest first, so today's articles are repaired before the long tail."""
    missing = []
    now = time.time()
    min_age_s = MIN_AGE_S if min_age_s is None else min_age_s
    for section_dir in sorted(p for p in CONTENT_DIR.iterdir() if p.is_dir()):
        section = section_dir.name
        style = SECTIONS.get(section, DEFAULT_STYLE)
        for md_file in section_dir.glob("*.md"):
            if md_file.name == "_index.md":
                continue
            try:
                if now - md_file.stat().st_mtime < min_age_s:
                    continue
                content = md_file.read_text()
            except OSError:
                continue
            if cover_ok(content):
                continue
            fm = _front_matter(content)
            if not fm:
                continue          # malformed front matter: nothing safe to attach to
            if re.search(r"^draft:\s*true", fm, re.M):
                continue
            missing.append({
                "file": md_file,
                "section": section,
                "style": style,
                "title": _fm_value(fm, "title"),
                "description": _fm_value(fm, "description"),
            })
    missing.sort(key=lambda p: p["file"].name, reverse=True)
    return missing


def _clean_title(title: str) -> str:
    """Drop emoji/symbols the image prompt can't use."""
    return re.sub(r"[^\w\s,.:;'&()!?-]", "", title or "").strip()


def build_prompt(post: dict) -> str:
    """Prompt from the article's OWN title + description via the shared helper
    (nova_journal.get_image_prompt — LLM text call, local Claude CLI). The section style is
    only the fallback when the helper is unavailable: feeding it to the helper made every
    burbank-ops cover the same server room."""
    title = _clean_title(post["title"])[:120]
    desc = (post.get("description") or "").strip()[:200]
    try:
        import nova_journal as nj
        return nj.get_image_prompt(title, desc or post["section"], post["section"])
    except Exception as e:
        log(f"  prompt helper unavailable ({e}) — style prompt")
        return f"{post['style']}, inspired by: {title[:60]}, no text, no words, no letters"


def generate_image_for_post(post: dict, budget_s: float | None = None) -> str | None:
    """Return a cover image path for the post: an already-rendered non-empty file under
    any common extension, else a fresh LOCAL render."""
    img_dir = STATIC_DIR / post["section"]
    slug = post["file"].stem
    for ext in (".webp", ".png", ".jpg", ".jpeg"):
        cand = img_dir / f"{slug}{ext}"
        if cand.is_file() and cand.stat().st_size > 0:
            return str(cand)
    prompt = build_prompt(post)
    log(f"  prompt: {prompt[:140]}")
    if budget_s is not None:
        nova_image_utils.TIMEOUT = max(60, int(budget_s))
    return generate_image(prompt, section=post["section"])


def add_image_to_post(post: dict, image_path: str) -> Path | None:
    """Write the .webp cover and set `cover:` front matter. Returns the image path written
    (for an explicit-path commit), or None on failure."""
    section = post["section"]
    md_file = post["file"]

    # Output WebP — the journal references .webp everywhere (publish_hugo writes
    # .webp refs, deploy expects WebP).
    slug = md_file.stem
    img_filename = f"{slug}.webp"
    img_dir = STATIC_DIR / section
    img_dir.mkdir(parents=True, exist_ok=True)
    dest = img_dir / img_filename

    try:
        if Path(image_path).resolve() == dest.resolve():
            pass  # image already sits at the destination — nothing to copy, just attach it below
        elif image_path.lower().endswith(".webp"):
            shutil.copy2(image_path, dest)
        else:
            r = subprocess.run(["cwebp", "-q", "86", "-resize", "1200", "0", image_path, "-o", str(dest)],
                               capture_output=True, timeout=60)
            if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
                log(f"  cwebp failed ({r.returncode}) — refusing to write a non-webp under a .webp name")
                return None
    except Exception as e:
        log(f"  Failed to write image: {e}")
        return None

    content = md_file.read_text()
    cover_ref = f"/images/{section}/{img_filename}"
    fm_parts = content.split("---", 2)
    if len(fm_parts) < 3:
        log(f"  Malformed frontmatter in {md_file.name} — skipping")
        return None
    frontmatter, body = fm_parts[1], fm_parts[2]
    # Remove any existing (possibly duplicate or broken) cover block from frontmatter
    frontmatter = re.sub(r'\ncover:[\s\S]*?(?=\n\S|\Z)', '', frontmatter).rstrip()
    alt = _clean_title(post.get("title") or "Nova").replace('"', "") or "Nova"
    frontmatter += f'\ncover:\n  image: "{cover_ref}"\n  alt: "{alt}"\n  relative: false'
    md_file.write_text("---" + frontmatter + "\n---" + body)
    log(f"  Added image: {cover_ref}")
    return dest


def _git(args, timeout=60):
    return subprocess.run(["git", *args], cwd=str(JOURNAL_DIR), capture_output=True, text=True, timeout=timeout)


def commit_and_push(paths: list[Path], fixed: int) -> str:
    """Commit exactly `paths` (never add -A, never stash, never force), under the fleet-wide
    journal push lock; pull --rebase then push. A push that can't land leaves the commit
    local for the journal's stranded-commit sweep. Returns pushed|committed|failed."""
    rel = [str(Path(p).resolve().relative_to(JOURNAL_DIR.resolve())) for p in paths]
    lock = _acquire_push_lock()
    try:
        if _git(["add", "--", *rel], 30).returncode != 0:
            log("git add failed")
            return "failed"
        c = _git(["commit", "-m", f"fix: Add {fixed} missing cover images (local auto-repair)", "--", *rel], 60)
        if c.returncode != 0:
            log(f"git commit failed: {(c.stderr or c.stdout)[:200]}")
            return "failed"
        pull = _git(["pull", "--rebase", "origin", "main"], 180)
        if pull.returncode != 0:
            _git(["rebase", "--abort"], 30)
            log(f"pull --rebase failed — commit left local for the stranded-commit sweep: {pull.stderr[:200]}")
            return "committed"
        p = _git(["push", "origin", "HEAD:main"], 180)
        if p.returncode != 0:
            log(f"Push FAILED (commit NOT on origin yet): {p.stderr[:200]}")
            return "committed"
        log(f"Committed and pushed {fixed} image fixes")
        return "pushed"
    except subprocess.TimeoutExpired as e:
        log(f"git timed out ({e}) — commit (if any) left for the stranded-commit sweep")
        return "committed"
    finally:
        _release_push_lock(lock)


def _acquire_push_lock():
    """The fleet-wide journal push lock (PG advisory lock shared with every publisher)."""
    try:
        import nova_journal as nj
        return nj._acquire_push_lock()
    except Exception as e:
        log(f"push lock unavailable ({e}) — proceeding unlocked")
        return None


def _release_push_lock(lock):
    if lock is None:
        return
    try:
        import nova_journal as nj
        nj._release_push_lock(lock)
    except Exception:
        pass


def _single_instance():
    """Hold an exclusive flock for the process lifetime; None if another run holds it."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET_S, help="seconds for the whole run")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    t0 = time.monotonic()

    lock = _single_instance()
    if lock is None:
        log("another nova_fix_missing_images run holds the lock — exiting")
        print("Done. Fixed: 0, Failed: 0")
        return

    log("=== Scanning for missing images ===")
    missing = get_posts_missing_images()
    if not missing:
        log("All posts have images. Nothing to fix.")
        print("Done. Fixed: 0, Failed: 0")
        return

    log(f"Found {len(missing)} posts missing images:")
    for p in missing:
        log(f"  [{p['section']}] {p['title'][:60]}")
    if args.dry_run:
        return

    fixed = failed = 0
    written: list[Path] = []
    batch = missing[:max(0, args.limit)]
    log(f"Processing batch of {len(batch)} (of {len(missing)} total), budget {args.budget}s")

    for post in batch:
        left = args.budget - (time.monotonic() - t0) - COMMIT_RESERVE_S
        if left < EST_IMAGE_S:
            log(f"budget nearly spent ({left:.0f}s left) — stopping; the rest wait for the next run")
            break
        log(f"Generating image for: [{post['section']}] {post['title'][:50]}")
        image_path = generate_image_for_post(post, budget_s=left)
        dest = add_image_to_post(post, image_path) if image_path else None
        if dest:
            fixed += 1
            written += [dest, post["file"]]
        else:
            failed += 1
            log(f"  Image generation failed for: {post['title'][:50]}")

    if fixed > 0:
        commit_and_push(written, fixed)

    if fixed > 0 or failed > 0:
        notify(
            f":frame_with_picture: *Image Auto-Repair Complete*\n"
            f"• Fixed: {fixed} posts now have cover images (local ComfyUI)\n"
            f"• Failed: {failed} (ComfyUI busy/down — retried next run)\n"
            f"• Total scanned: {len(missing)} posts were missing images"
        )

    log(f"Done. Fixed: {fixed}, Failed: {failed}")


if __name__ == "__main__":
    main()
