#!/usr/bin/env python3
"""
nova_tv_pilot.py — Nova writes a new TV pilot every day.

Picks a random knowledge domain from her memory archive, fetches relevant
memories as creative fuel, then generates a full 30-minute TV pilot screenplay
with original characters, setting, and plot.

Output: A professional-format TV pilot (Cold Open + 2 Acts + Tag) published to
the nova-journal Hugo site under content/pilot/.

Features:
  - Pulls from 200+ memory domains for unlimited variety
  - Full screenplay format (FADE IN, sluglines, dialogue, action)
  - Cover image via OpenRouter
  - Published to nova-journal GitHub Pages
  - Slack notification on publish

Written by Jordan Koch.
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from nova_image_utils import generate_image

# ── Date override for backfill ────────────────────────────────────────────────
_FOR_DATE = os.environ.get("NOVA_FOR_DATE", "").strip()
if _FOR_DATE:
    _override_dt = datetime.strptime(_FOR_DATE, "%Y-%m-%d")
    def _today_str() -> str: return _FOR_DATE
    def _now_iso() -> str: return f"{_FOR_DATE}T21:00:00-07:00"
else:
    def _today_str() -> str: return time.strftime("%Y-%m-%d")
    def _now_iso() -> str: return datetime.now().astimezone().isoformat(timespec="seconds")


# ── Constants ─────────────────────────────────────────────────────────────────

MEMORY_SERVER = f"http://{nova_config.NOVA_HOST}:18790"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-sonnet-4-6"
HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content/pilot"
IMAGES_DIR = HUGO_ROOT / "static/images/pilot"
LOG_FILE = Path.home() / ".openclaw/logs/nova_tv_pilot.log"

# Domains that make good TV pilot source material
PILOT_DOMAINS = [
    "american_civil_war", "american_revolution", "anthropology", "architecture_general",
    "art_movements", "astronomy", "automotive", "biology_ecology", "biology_evolution",
    "chemistry_general", "chess", "climate_science", "cocktails", "comedy",
    "comic_books", "compsec_core", "compsec_crypto", "compsec_network",
    "computing_history", "crime_drama", "cryptocurrency", "cyberpunk",
    "demonology", "disney_history", "drama", "economics", "edm_history",
    "espionage", "ethics", "fashion", "film_criticism", "folklore",
    "forgotten_weapons", "gambling", "geography", "geology",
    "history", "horror", "jazz_history", "korean_war",
    "la_gangs", "law", "leadership", "linguistics", "literature_scifi",
    "martial_arts", "medicine", "metal_history", "military_history",
    "music_history", "mythology", "neuroscience", "nowave_history",
    "nuclear", "oceanography", "organized_crime", "paleontology",
    "philosophy_ethics", "philosophy_history", "physics", "piracy",
    "poker", "politics", "psychology", "rap_history", "religion",
    "robotics", "sexuality_history", "sociology", "space_history",
    "sports", "surveillance", "technology", "theater",
    "true_crime", "vietnam_war", "world_war_2", "world_war_ii",
]

# Genre options matched to tone
GENRES = [
    {"name": "Drama", "tone": "grounded, character-driven, emotionally complex"},
    {"name": "Thriller", "tone": "tense, paranoid, escalating stakes"},
    {"name": "Dark Comedy", "tone": "satirical, uncomfortable truths played for laughs"},
    {"name": "Sci-Fi", "tone": "speculative, thought-provoking, grounded in real science"},
    {"name": "Mystery", "tone": "atmospheric, puzzle-box, unreliable perspectives"},
    {"name": "Horror", "tone": "dread, slow-burn, psychological"},
    {"name": "Crime", "tone": "procedural but subversive, morally grey"},
    {"name": "Period Drama", "tone": "historically textured, class tension, secrets"},
]


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Memory Fetching ───────────────────────────────────────────────────────────

def fetch_memories(source: str, count: int = 30) -> list[dict]:
    """Fetch memories from a specific source domain via /recall."""
    try:
        query = urllib.parse.quote(f"interesting facts stories details about {source.replace('_', ' ')}")
        url = f"{MEMORY_SERVER}/recall?q={query}&n={count}&source={source}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            results = json.loads(resp.read())
        if isinstance(results, list):
            return results
        return results.get("memories", results.get("results", []))
    except Exception as e:
        log(f"Memory fetch failed for {source}: {e}")
        return []


# ── LLM Calls ────────────────────────────────────────────────────────────────

def call_openrouter(system: str, user: str, max_tokens: int = 16000) -> str:
    """Call OpenRouter with Claude for screenplay generation."""
    api_key = nova_config.openrouter_api_key()
    if not api_key:
        raise RuntimeError("No OpenRouter API key")

    payload = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode()

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://nova.digitalnoise.net",
            "X-Title": "Nova TV Pilot",
        },
    )

    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())

    usage = data.get("usage", {})
    log(f"OpenRouter tokens — in: {usage.get('prompt_tokens', '?')}, out: {usage.get('completion_tokens', '?')}")
    return data["choices"][0]["message"]["content"].strip()


# ── Pilot Generation ─────────────────────────────────────────────────────────

def generate_pilot(source: str, memories: list[dict], genre: dict) -> tuple[str, str, str]:
    """Generate a full TV pilot. Returns (title, slug, screenplay_markdown)."""

    memory_text = "\n\n".join(
        f"- {m.get('text', m.get('content', ''))[:300]}"
        for m in memories[:25]
        if m.get("text") or m.get("content")
    )

    source_label = source.replace("_", " ")

    system_prompt = """You are Nova, an AI screenwriter with encyclopedic knowledge and a distinctive voice.
You write original TV pilots in proper screenplay format. Your scripts are smart, surprising, and deeply human.

Rules:
- Write a COMPLETE 30-minute TV pilot (25-30 pages equivalent)
- Structure: COLD OPEN + ACT ONE + ACT TWO + TAG
- Use proper screenplay format: sluglines (INT./EXT.), character names in CAPS on first appearance,
  dialogue centered, action in present tense
- Create ORIGINAL characters and premise — never adapt existing IP
- The knowledge domain is creative fuel, not a constraint — use it as texture and setting
- Make the protagonist compelling and flawed
- End on a hook that demands Episode 2
- Include 3-5 supporting characters with distinct voices
- NO meta-commentary about being an AI or about the writing process"""

    user_prompt = f"""Write a 30-minute {genre['name']} TV pilot.

**Knowledge domain for creative fuel:** {source_label}
**Tone:** {genre['tone']}

**Source material from my memory archive (use as inspiration, not adaptation):**
{memory_text}

Write the FULL pilot screenplay now. Start with:
1. A show title (original, evocative)
2. An episode title
3. A one-sentence logline
4. Setting and tone description
5. Character descriptions (protagonist + 3-4 supporting)
6. Series potential (1 sentence)
7. Then the FULL SCREENPLAY from FADE IN to END OF PILOT

Make it brilliant. Make it original. Make someone want to watch Episode 2."""

    screenplay = call_openrouter(system_prompt, user_prompt, max_tokens=16000)

    # Extract title from the screenplay
    title = "Untitled Pilot"
    slug = "untitled"
    lines = screenplay.split("\n")
    for line in lines[:20]:
        line = line.strip()
        if line.startswith("# ") or line.startswith("**") and ":" not in line:
            title = line.strip("#* ").strip()
            break
        if "show title" in line.lower() or "title:" in line.lower():
            # Next non-empty line might be the title
            continue
        if line and not line.startswith("*") and not line.startswith("-") and len(line) < 60:
            if any(c.isupper() for c in line) and not line.startswith("INT") and not line.startswith("EXT"):
                title = line.strip("#*:\"' ")
                break

    # Generate a better title extraction
    title_match = re.search(r'(?:^|\n)#\s*(.+?)(?:\n|$)', screenplay)
    if title_match:
        title = title_match.group(1).strip("*# ")

    # Clean up title
    title = title.replace("**", "").strip()
    if len(title) > 80:
        title = title[:80]

    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    return title, slug, screenplay


# ── Publishing ────────────────────────────────────────────────────────────────

def publish_pilot(title: str, slug: str, screenplay: str, source: str, genre: dict,
                  cover_path: str | None) -> Path:
    """Publish the pilot to Hugo."""
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    date_str = _today_str()
    filename = f"{date_str}-{slug}.md"
    filepath = CONTENT_DIR / filename
    source_label = source.replace("_", " ")

    # Copy cover image
    cover_ref = ""
    if cover_path and Path(cover_path).exists():
        img_name = f"{date_str}-{slug}.png"
        dest = IMAGES_DIR / img_name
        shutil.copy2(cover_path, dest)
        cover_ref = f"/images/pilot/{img_name}"
        log(f"Cover image: {img_name}")

    # Build frontmatter
    tags = ["screenplay", "tv", genre["name"].lower().replace(" ", "_"), source.replace(" ", "_")]
    description = ""
    logline_match = re.search(r'\*\*Logline:\*\*\s*(.+?)(?:\n|$)', screenplay)
    if logline_match:
        description = logline_match.group(1).strip()[:150]
    if not description:
        description = f"A {genre['name'].lower()} pilot drawn from Nova's memory archive on {source_label}."

    frontmatter = f"""---
title: "📺 {title}"
date: {_now_iso()}
draft: false
categories: ["pilot"]
tags: {json.dumps(tags)}
description: "{description}"
cover:
  image: "{cover_ref}"
  alt: "{title} — TV Pilot"
  relative: false
---

*A 30-minute {genre['name']} pilot. Drawn from Nova's memory archive on: {source_label}.*

---

"""

    filepath.write_text(frontmatter + screenplay + f"\n\n---\n\n*Written by Nova. Source domain: `{source}`. Pilot #{_get_pilot_number()}.*\n")
    log(f"Published: {filename}")
    return filepath


def _get_pilot_number() -> int:
    """Count existing pilots to get the next number."""
    existing = list(CONTENT_DIR.glob("*.md"))
    return len([f for f in existing if f.name != "_index.md"])


# ── Git Push ──────────────────────────────────────────────────────────────────

def git_push():
    """Commit and push to GitHub."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        date_str = _today_str()
        subprocess.run(
            ["git", "commit", "-m", f"pilot: New TV pilot for {date_str}"],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30
        )
        result = subprocess.run(
            ["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            log("Pushed to GitHub — deploy will trigger automatically")
        else:
            log(f"Push failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"Git push error: {e}")


# ── Main Pipeline ─────────────────────────────────────────────────────────────

def run_pipeline() -> bool:
    """Run the full pilot generation pipeline."""
    log("=== Nova TV Pilot Pipeline ===")

    # Pick a random domain and genre
    source = random.choice(PILOT_DOMAINS)
    genre = random.choice(GENRES)
    log(f"Domain: {source} | Genre: {genre['name']} ({genre['tone']})")

    # Fetch memories
    log("Fetching memories...")
    memories = fetch_memories(source, 30)
    if len(memories) < 5:
        # Try a fallback domain
        log(f"Only {len(memories)} memories for {source}, trying fallback...")
        source = random.choice(["history", "drama", "crime_drama", "horror", "world_war_2"])
        memories = fetch_memories(source, 30)
    if len(memories) < 3:
        log("ERROR: Not enough memories — aborting")
        return False
    log(f"Got {len(memories)} memories")

    # Generate the pilot
    log("Generating screenplay...")
    title, slug, screenplay = generate_pilot(source, memories, genre)
    log(f"Title: {title} | Slug: {slug} | Length: {len(screenplay)} chars")

    if len(screenplay) < 2000:
        log("ERROR: Screenplay too short — aborting")
        return False

    # Generate cover image
    log("Generating cover image...")
    cover_prompt = (
        f"Dramatic cinematic poster for a TV show called '{title}'. "
        f"{genre['name']} genre. Moody, professional, no text. "
        f"Key art style, dark atmospheric lighting."
    )
    cover_path = generate_image(cover_prompt, width=1024, height=1024, section="art")

    # Publish
    log("Publishing to Hugo...")
    publish_pilot(title, slug, screenplay, source, genre, cover_path)

    # Git push
    git_push()

    # Notify Slack
    nova_config.post_both(
        f":clapper: *New TV Pilot:* \"{title}\"\n"
        f"_{genre['name']} • Source: {source.replace('_', ' ')}_\n"
        f"https://nova.digitalnoise.net/pilot/{_today_str()}-{slug}/",
        slack_channel=nova_config.SLACK_NOTIFY,
    )

    log(f"Pipeline complete: \"{title}\"")
    return True


if __name__ == "__main__":
    try:
        success = run_pipeline()
        sys.exit(0 if success else 1)
    except Exception as e:
        log(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
