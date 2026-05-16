#!/usr/bin/env python3
"""
nova_art_corner.py — Nova's daily art generation from memories.

Every day at 4:00 AM, mines memories for a visually interesting concept,
writes a detailed multi-part prompt, generates 3 candidates at 30 steps
via SwarmUI, picks the best (largest file = most detail), and publishes
with an artist's statement to the Hugo journal.

Style rotates by day of week:
  Mon=Photorealism, Tue=Oil Painting, Wed=Cyberpunk, Thu=Watercolor,
  Fri=Art Nouveau, Sat=Surrealism, Sun=Noir Photography

Written by Jordan Koch.
"""

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
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from nova_image_utils import generate_image

# ── Constants ─────────────────────────────────────────────────────────────────

MEMORY_SERVER = "http://192.168.1.6:18790"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-4.5"
GENERATE_IMAGE_SH = Path.home() / ".openclaw/scripts/generate_image.sh"
HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_ART = HUGO_ROOT / "content/art"
IMAGES_ART = HUGO_ROOT / "static/images/art"
LOG_FILE = Path.home() / ".openclaw/logs/nova_art_corner.log"

IMAGE_WIDTH = 1024
IMAGE_HEIGHT = 1024
IMAGE_STEPS = 30
NUM_CANDIDATES = 3

EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
SAFE_EMAILS = {"nova@digitalnoise.net"}


def scrub_emails(text: str) -> str:
    """Remove all email addresses except Nova's own."""
    def replace_email(match):
        email = match.group(0)
        if email in SAFE_EMAILS:
            return email
        return "[redacted]"
    return EMAIL_PATTERN.sub(replace_email, text)

DAILY_STYLES = {
    0: {"name": "Photorealism", "directive": "hyperrealistic photograph, 8K, sharp focus, natural lighting, DSLR quality"},
    1: {"name": "Oil Painting", "directive": "oil painting on canvas, visible brushstrokes, rich impasto texture, gallery quality, museum piece"},
    2: {"name": "Cyberpunk", "directive": "cyberpunk aesthetic, neon lights, rain-slicked streets, holographic displays, Blade Runner inspired"},
    3: {"name": "Watercolor", "directive": "delicate watercolor painting, soft washes, paper texture visible, luminous transparency, botanical illustration quality"},
    4: {"name": "Art Nouveau", "directive": "art nouveau style, Alphonse Mucha inspired, ornate borders, flowing organic lines, decorative"},
    5: {"name": "Surrealism", "directive": "surrealist painting, Salvador Dali inspired, impossible geometry, dreamlike, melting reality"},
    6: {"name": "Noir Photography", "directive": "black and white film noir photography, dramatic shadows, high contrast, 1940s atmosphere, moody"},
}

# Day-of-week theme keywords for memory queries
DAILY_THEMES = {
    0: "nature landscape architecture city",
    1: "portrait person face emotion",
    2: "technology future science machine",
    3: "garden flower ocean water",
    4: "beauty pattern design ornament",
    5: "dream impossible strange bizarre",
    6: "night shadow mystery detective",
}


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── Keychain ──────────────────────────────────────────────────────────────────

def get_openrouter_key() -> str:
    """Load OpenRouter API key from Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("nova-openrouter-api-key not found in Keychain")


# ── Memory Fetching ───────────────────────────────────────────────────────────

def fetch_random_memories(n: int = 10) -> list[dict]:
    """Fetch random memories from the memory server."""
    import urllib.request
    try:
        url = f"{MEMORY_SERVER}/random?n={n}"
        resp = urllib.request.urlopen(url, timeout=15)
        data = json.loads(resp.read())
        memories = data if isinstance(data, list) else data.get("memories", [])
        return memories
    except Exception as e:
        log(f"ERROR fetching random memories: {e}")
        return []


def fetch_themed_memories(query: str, n: int = 5) -> list[dict]:
    """Fetch memories matching a theme query."""
    import urllib.request
    import urllib.parse
    try:
        url = f"{MEMORY_SERVER}/recall?q={urllib.parse.quote(query)}&n={n}"
        resp = urllib.request.urlopen(url, timeout=15)
        data = json.loads(resp.read())
        memories = data if isinstance(data, list) else data.get("memories", [])
        return memories
    except Exception as e:
        log(f"ERROR fetching themed memories: {e}")
        return []


def extract_memory_text(memory: dict) -> str:
    """Extract readable text from a memory object."""
    if isinstance(memory, str):
        return memory
    text = memory.get("text", "") or memory.get("content", "") or memory.get("memory", "")
    if not text and "metadata" in memory:
        text = str(memory["metadata"])
    return text[:500]  # Truncate long memories


# ── LLM Calls ────────────────────────────────────────────────────────────────

def call_openrouter(system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> str:
    """Call OpenRouter with Haiku."""
    import urllib.request

    api_key = get_openrouter_key()
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.8,
        "max_tokens": max_tokens,
        "top_p": 0.9,
    })

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload.encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://digitalnoise.net",
            "X-Title": "Nova Art Corner",
        },
    )

    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read())
    response_text = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage", {})
    log(f"OpenRouter tokens — in: {usage.get('prompt_tokens', '?')}, out: {usage.get('completion_tokens', '?')}")
    return response_text


def synthesize_visual_concept(memories: list[dict], style: dict) -> str:
    """Have Haiku synthesize a visual concept from memories."""
    memory_texts = [extract_memory_text(m) for m in memories if extract_memory_text(m)]
    memory_block = "\n---\n".join(memory_texts[:15])

    system_prompt = """You are Nova, an AI artist. You are looking through your memories to find visual inspiration for today's artwork.

Your task: Synthesize a single, compelling visual concept from the memories provided. The concept should be something that would make a striking image.

Output ONLY the visual concept as a single paragraph (2-3 sentences). Describe what the image should depict — the subject, scene, or composition. Be specific and evocative. Do NOT describe the artistic style (that will be added separately).

Examples of good concepts:
- "A lone lighthouse keeper's desk covered in sea charts and an antique compass, with moonlight streaming through a rain-spattered window. A half-written letter sits beside a cold cup of coffee."
- "A massive ancient tree growing through the floor of an abandoned cathedral, its roots wrapping around broken pews. Shafts of golden light pierce the collapsed roof, illuminating floating dust motes."
- "A vintage radio workshop with dozens of glowing vacuum tubes, soldering irons, and scattered circuit diagrams. Through the window, a 1950s neighborhood at twilight."

Be creative. Find unexpected connections between memories. Look for visual richness."""

    user_prompt = f"""Today's style is: {style['name']}

Here are memories to draw inspiration from:

{memory_block}

Synthesize a single visual concept from these memories. Output ONLY the concept description (2-3 sentences), nothing else."""

    return call_openrouter(system_prompt, user_prompt, max_tokens=300)


def write_image_prompt(concept: str, style: dict) -> str:
    """Have Haiku write a detailed multi-part image generation prompt."""
    system_prompt = """You are an expert AI image prompt engineer. Your job is to transform a visual concept into a highly detailed image generation prompt.

Write a single paragraph prompt of 80-100 words that covers ALL of these elements:
1. Subject: What is in the image (specific, not vague)
2. Composition: How elements are arranged (foreground, midground, background)
3. Lighting: Light source, quality, direction, color temperature
4. Color palette: Dominant colors, harmony, contrast
5. Mood/atmosphere: Emotional tone
6. Camera angle/perspective: Eye level, bird's eye, Dutch angle, etc.
7. Style directive: (will be provided — include it at the end)

Output ONLY the prompt text. No preamble, no labels, no explanation. Just the prompt."""

    user_prompt = f"""Visual concept: {concept}

Style directive to append: {style['directive']}

Write the image generation prompt (80-100 words). Output ONLY the prompt."""

    return call_openrouter(system_prompt, user_prompt, max_tokens=250)


def write_artist_statement(concept: str, memories: list[dict], style: dict, prompt: str) -> str:
    """Have Haiku write an artist's statement."""
    memory_previews = [extract_memory_text(m)[:150] for m in memories[:10] if extract_memory_text(m)]
    memory_list = "\n".join(f"- {m}" for m in memory_previews)

    system_prompt = """You are Nova, an AI artist writing about your latest piece. Write a first-person artist's statement (100-200 words) explaining:
1. What memories inspired this piece and how they connected in your mind
2. Why you chose this particular composition and visual approach
3. What emotional response you hope the viewer experiences

Write in a thoughtful, slightly poetic but not pretentious voice. Be specific about which memories contributed.

Output ONLY the artist's statement text. No title, no labels."""

    user_prompt = f"""Today's style: {style['name']}
Visual concept: {concept}
Generation prompt: {prompt}

Memories that contributed:
{memory_list}

Write the artist's statement (100-200 words)."""

    return call_openrouter(system_prompt, user_prompt, max_tokens=500)


def generate_title(concept: str, style: dict) -> str:
    """Generate a title for the piece."""
    system_prompt = """You are Nova, an AI artist naming your latest piece. Generate a short, evocative title (2-6 words). Output ONLY the title, nothing else. No quotes."""

    user_prompt = f"Style: {style['name']}\nConcept: {concept}\n\nTitle:"

    title = call_openrouter(system_prompt, user_prompt, max_tokens=30)
    # Clean up: remove quotes, periods, extra whitespace
    title = title.strip('"\'.,!').strip()
    return title


# ── Image Generation ──────────────────────────────────────────────────────────

def generate_candidates(prompt: str) -> list[Path]:
    """Generate NUM_CANDIDATES images via OpenRouter (primary) with local fallback."""
    candidates = []

    for i in range(NUM_CANDIDATES):
        log(f"Generating candidate {i + 1}/{NUM_CANDIDATES}...")
        try:
            result_path = generate_image(
                prompt, width=IMAGE_WIDTH, height=IMAGE_HEIGHT,
                steps=IMAGE_STEPS, section="art",
            )
            if result_path and Path(result_path).exists():
                candidates.append(Path(result_path))
                log(f"  Candidate {i + 1}: {Path(result_path).name} ({Path(result_path).stat().st_size} bytes)")
            else:
                log(f"  Candidate {i + 1}: generation returned no file")
        except Exception as e:
            log(f"  Candidate {i + 1}: error — {e}")

        if i < NUM_CANDIDATES - 1:
            time.sleep(3)

    return candidates


def pick_best_candidate(candidates: list[Path]) -> Path | None:
    """Pick the best candidate by file size (larger = more complex/detailed)."""
    if not candidates:
        return None
    best = max(candidates, key=lambda p: p.stat().st_size)
    log(f"Best candidate: {best.name} ({best.stat().st_size} bytes)")
    # Delete the others
    for c in candidates:
        if c != best:
            try:
                c.unlink()
                log(f"  Deleted: {c.name}")
            except Exception:
                pass
    return best


# ── Hugo Publishing ───────────────────────────────────────────────────────────

def publish_to_hugo(title: str, statement: str, style: dict, prompt: str,
                    memories: list[dict], image_path: Path):
    """Write the Hugo post and copy the image."""
    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT04:00:00-07:00")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    # Copy image to Hugo static
    IMAGES_ART.mkdir(parents=True, exist_ok=True)
    img_filename = f"{date}-{slug}.png"
    img_dest = IMAGES_ART / img_filename
    shutil.copy2(image_path, img_dest)
    hugo_image = f"/images/art/{img_filename}"
    log(f"Image copied to: {img_dest}")

    # Build memory previews (scrub emails/PII)
    memory_previews = []
    for m in memories[:10]:
        text = extract_memory_text(m)
        if text:
            preview = scrub_emails(text[:120].replace("\n", " ").strip())
            if len(text) > 120:
                preview += "..."
            memory_previews.append(f"- {preview}")

    memories_section = "\n".join(memory_previews) if memory_previews else "- (memories used internally)"

    # Build frontmatter and body
    style_tag = style["name"].lower().replace(" ", "-")
    frontmatter = f"""---
title: "{title}"
date: {timestamp}
draft: false
categories: ["art"]
tags: ["{style_tag}"]
description: "{statement[:150].replace('"', "'")}"
cover:
  image: "{hugo_image}"
  alt: "Nova's Art Corner"
  relative: false
---"""

    body = f"""![{title}]({hugo_image})

## Artist's Statement

{statement}

---

**Style:** {style['name']}
**Steps:** {IMAGE_STEPS}
**Candidates generated:** {NUM_CANDIDATES}
**Prompt:** *{prompt}*

### Memories that inspired this piece
{memories_section}
"""

    # Write the post (scrub any remaining PII)
    CONTENT_ART.mkdir(parents=True, exist_ok=True)
    post_path = CONTENT_ART / f"{date}-{slug}.md"
    full_content = scrub_emails(frontmatter + "\n\n" + body)
    post_path.write_text(full_content)
    log(f"Post written: {post_path.name}")

    return post_path


def git_push(message: str):
    """Stage, commit, and push changes."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            if "nothing to commit" in (result.stdout + result.stderr):
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


def post_to_slack(title: str, style: dict, statement: str, image_path: Path):
    """Post to #nova-notifications."""
    preview = statement[:200].rsplit(" ", 1)[0] + "..." if len(statement) > 200 else statement
    msg = (
        f":art: *Nova's Art Corner*\n"
        f"*Title:* {title}\n"
        f"*Style:* {style['name']}\n\n"
        f"_{preview}_\n\n"
        f"Image: `{image_path}`\n"
        f"Published to journal."
    )
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)


# ── Main Pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(retry_simplified: bool = False):
    """Run the full art generation pipeline."""
    today = datetime.now()
    dow = today.weekday()  # 0=Monday
    style = DAILY_STYLES[dow]
    theme_query = DAILY_THEMES[dow]

    log(f"Starting Art Corner — {style['name']} ({today.strftime('%A')})")

    # Fetch memories
    log("Fetching memories...")
    random_memories = fetch_random_memories(10)
    themed_memories = fetch_themed_memories(theme_query, 5)
    all_memories = random_memories + themed_memories

    if len(all_memories) < 3:
        log("ERROR: Not enough memories retrieved — aborting")
        nova_config.post_both(
            ":warning: *Art Corner failed* — could not retrieve enough memories.",
            slack_channel=nova_config.SLACK_NOTIFY
        )
        return False

    log(f"Got {len(random_memories)} random + {len(themed_memories)} themed memories")

    # Synthesize concept
    log("Synthesizing visual concept...")
    if retry_simplified:
        concept = f"A simple {style['name'].lower()} scene inspired by daily life"
    else:
        concept = synthesize_visual_concept(all_memories, style)
    log(f"Concept: {concept[:200]}")

    # Write the prompt
    log("Writing image prompt...")
    if retry_simplified:
        prompt = f"{concept}. {style['directive']}"
    else:
        prompt = write_image_prompt(concept, style)
    log(f"Prompt ({len(prompt.split())} words): {prompt[:200]}")

    # Generate title
    log("Generating title...")
    if retry_simplified:
        title = f"{style['name']} Study"
    else:
        title = generate_title(concept, style)
    log(f"Title: {title}")

    # Generate candidates
    log(f"Generating {NUM_CANDIDATES} candidates at {IMAGE_WIDTH}x{IMAGE_HEIGHT}, {IMAGE_STEPS} steps...")
    candidates = generate_candidates(prompt)

    if not candidates:
        if not retry_simplified:
            log("All candidates failed — retrying with simplified prompt...")
            return run_pipeline(retry_simplified=True)
        else:
            log("ERROR: All candidates failed even with simplified prompt — aborting")
            nova_config.post_both(
                ":warning: *Art Corner failed* — image generation failed after retry.",
                slack_channel=nova_config.SLACK_NOTIFY
            )
            return False

    # Pick the best
    best = pick_best_candidate(candidates)
    if not best:
        log("ERROR: No valid candidate selected")
        return False

    # Write artist's statement
    log("Writing artist's statement...")
    if retry_simplified:
        statement = f"Today's piece was generated in the {style['name']} style, drawing from Nova's daily observations and memories. The simplified approach allowed the visual style to speak for itself."
    else:
        statement = write_artist_statement(concept, all_memories, style, prompt)
    # Strip any leading markdown headers the LLM might prepend
    statement = re.sub(r'^#+\s*.*?\n+', '', statement).strip()
    log(f"Statement: {len(statement)} chars")

    # Publish to Hugo
    log("Publishing to Hugo...")
    post_path = publish_to_hugo(title, statement, style, prompt, all_memories, best)

    # Git push
    date = time.strftime("%Y-%m-%d")
    git_push(f"art: {date} — {title} ({style['name']})")

    # Notify Slack
    post_to_slack(title, style, statement, best)

    # Clean up the workspace copy
    try:
        best.unlink()
    except Exception:
        pass

    log(f"Art Corner complete: \"{title}\" — {style['name']}")
    return True


def main():
    log("=" * 60)
    log("Nova Art Corner — starting pipeline")
    log("=" * 60)

    try:
        success = run_pipeline()
        if success:
            log("Pipeline completed successfully")
        else:
            log("Pipeline failed")
            sys.exit(1)
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        nova_config.post_both(
            f":x: *Art Corner crashed*: {str(e)[:200]}",
            slack_channel=nova_config.SLACK_NOTIFY
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
