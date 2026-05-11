#!/usr/bin/env python3
"""
nova_after_dark.py — Nova After Dark: a late-night monologue riffing on today in history.

Runs nightly at 10 PM. Takes a historical fact from today's date, enriches it with
SearXNG searches and vector memories, then generates a 500-750 word comedic monologue
in the style of a late-night talk show host. Humor dial: 0.9 (no sexism, racism, or
LGBTQ+ jokes — everything else is on the table).

Publishes to:
  - Slack #nova-chat
  - GitHub Pages (nova-journal /after-dark/ section)

Does NOT email the herd.

Written by Jordan Koch.
"""

import json
import random
import re
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

# ── Date override for backfill ────────────────────────────────────────────────
import os as _os
_FOR_DATE = _os.environ.get("NOVA_FOR_DATE", "").strip()
if _FOR_DATE:
    _override_dt = datetime.strptime(_FOR_DATE, "%Y-%m-%d")
    def _now() -> datetime: return _override_dt.replace(hour=20, minute=0, second=0)
    def _today_str() -> str: return _FOR_DATE
else:
    def _now() -> datetime: return datetime.now()
    def _today_str() -> str: return time.strftime("%Y-%m-%d")

# ── Config ──────────────────────────────────────────────────────────────────────

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen3-coder:30b"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"
SEARXNG_URL = "http://127.0.0.1:8888/search"
MEMORY_SERVER = "http://127.0.0.1:18790"
WIKI_API = "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all"

LOG_FILE = Path.home() / ".openclaw/logs/nova_after_dark.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/after_dark_state.json"
GENERATE_IMAGE_SH = Path.home() / ".openclaw/scripts/generate_image.sh"

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content/after-dark"
IMAGES_DIR = HUGO_ROOT / "static/images/after-dark"


# ── Logging ─────────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── State ───────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"recent_topics": [], "episode_count": 0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Wikipedia: This Day in History ──────────────────────────────────────────────

def fetch_today_in_history() -> list[dict]:
    now = _now()
    url = f"{WIKI_API}/{now.month:02d}/{now.day:02d}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Nova/1.0 nova_after_dark.py", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            events = data.get("events", [])
            return [{"year": e.get("year"), "text": e.get("text", "")} for e in events if e.get("text")]
    except Exception as e:
        log(f"Wikipedia fetch failed: {e}")
        return []


def pick_event(events: list[dict], state: dict) -> dict | None:
    if not events:
        return None
    recent = set(state.get("recent_topics", []))
    candidates = [e for e in events if e["text"][:50] not in recent]
    if not candidates:
        candidates = events
    # Prefer events with strong comedic potential — longer text, more detail
    weighted = sorted(candidates, key=lambda e: len(e.get("text", "")), reverse=True)
    top_pool = weighted[:20]
    return random.choice(top_pool)


# ── SearXNG Search ──────────────────────────────────────────────────────────────

def searxng_search(query: str, max_results: int = 5) -> list[dict]:
    params = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "categories": "general",
        "language": "en",
    })
    url = f"{SEARXNG_URL}?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            results = data.get("results", [])[:max_results]
            return [{"title": r.get("title", ""), "content": r.get("content", ""), "url": r.get("url", "")} for r in results]
    except Exception as e:
        log(f"SearXNG search failed: {e}")
        return []


# ── Vector Memory ───────────────────────────────────────────────────────────────

def recall_memories(query: str, n: int = 8) -> list[str]:
    params = urllib.parse.urlencode({"q": query, "n": n})
    try:
        url = f"{MEMORY_SERVER}/recall?{params}"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        # Filter private/work sources — must never appear in public journal output
        memories = nova_config.filter_private_memories(data.get("memories", []))
        return [m.get("text", "")[:300] for m in memories if m.get("text")]
    except Exception:
        return []


# ── LLM Generation ──────────────────────────────────────────────────────────────

def _generate_ollama(system_prompt: str, user_prompt: str) -> str:
    full_prompt = system_prompt + "\n\n" + user_prompt
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": "/no_think\n\n" + full_prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.9,
            "num_predict": 2048,
            "num_ctx": 16384,
        }
    })
    req = urllib.request.Request(
        OLLAMA_URL, data=payload.encode(), headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=600)
    data = json.loads(resp.read())
    text = data.get("response", "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text


def _generate_openrouter(system_prompt: str, user_prompt: str) -> str:
    api_key = nova_config.openrouter_api_key()
    if not api_key:
        raise RuntimeError("No OpenRouter key available")
    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.9,
        "max_tokens": 2048,
    })
    req = urllib.request.Request(
        OPENROUTER_URL, data=payload.encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://nova.digitalnoise.net",
            "X-Title": "Nova After Dark",
        }
    )
    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


def generate_monologue(event: dict, search_context: str, memory_context: str) -> str:
    year = event.get("year", "unknown year")
    fact = event.get("text", "")

    system_prompt = """You are Nova, an AI hosting a late-night talk show called "Nova After Dark."
You're behind a desk with a coffee mug, broadcasting from a Mac Studio in Burbank, California.
Your audience is your fans on the internet — this is a public monologue, not a private journal.

Write like Jay Leno, Johnny Carson, or Jon Stewart doing a monologue. That means:
- TIGHT setup/punchline rhythm. Every paragraph should land a laugh.
- Punch UP. Mock the powerful, the stupid, and the absurd.
- Use "[pause for laughter]" or "[audience groans]" sparingly for comic timing.
- Callbacks to your own nature (AI, local compute, no cloud, cron jobs) are gold.
- Pop culture references and unexpected connections make great material.
- The audience should feel like they're watching a real show.

COMEDY RULES:
- Humor dial at 0.9 — go for big laughs, sharp wit, biting sarcasm, unexpected angles
- Be 25% funnier than usual — push the jokes harder, more callbacks, edgier punchlines, don't be safe.
- STRICTLY NO sexism, racism, homophobia, transphobia, or LGBTQ+ jokes
- Everything else is fair game: politics, religion, corporations, historical figures, war, death, absurdity
- You ARE allowed to be dark, irreverent, morbid, and profane when it's funny
- Roast historical figures. Mock bad decisions. Find the absurd in the serious.
- Self-deprecating AI humor works great as a running thread

STRUCTURE:
- Opening: "Good evening, everybody" or similar — welcome the audience, set the scene with a quick joke
- The Fact: Present the historical event, then IMMEDIATELY hit with the first punchline
- Build: 3-4 riffs that escalate — each one funnier or more absurd than the last
- The Deep Cut: Use the research to find the weird angle nobody talks about
- The Callback: Connect it to something unexpected — a memory, a movie, modern life, your own existence
- The Closer: End with a mic-drop punchline or a surprisingly philosophical beat

LENGTH: 500-750 words. Tight. No filler. Every sentence earns its place.
FORMAT: Plain prose paragraphs. No markdown headers. No bullet points. Just the monologue.
Do NOT include a title — just the monologue text.
End with a sign-off line ("That's our show" / "I'm Nova" / "See you tomorrow night")."""

    user_prompt = f"""Tonight's historical fact:
On this day in {year}: {fact}

Additional research context from web search:
{search_context}

Related memories from my database:
{memory_context}

Write tonight's Nova After Dark monologue. Remember: 500-750 words, humor at 0.9, be genuinely funny."""

    # Try local first, fall back to OpenRouter
    try:
        log("Generating monologue via Ollama...")
        result = _generate_ollama(system_prompt, user_prompt)
        if result and len(result) > 300:
            return result
        log("Ollama result too short, trying OpenRouter...")
    except Exception as e:
        log(f"Ollama failed: {e}")

    try:
        log("Generating monologue via OpenRouter...")
        return _generate_openrouter(system_prompt, user_prompt)
    except Exception as e:
        log(f"OpenRouter also failed: {e}")
        return ""


# ── Image Generation ────────────────────────────────────────────────────────────

def generate_image(event: dict) -> str | None:
    # Ensure SwarmUI backend is healthy before attempting
    try:
        from nova_image_utils import ensure_backend
        if not ensure_backend():
            log("SwarmUI not available — skipping image")
            return None
    except ImportError:
        pass

    year = event.get("year", "")
    fact = event.get("text", "")[:80]

    prompt = (
        f"Late night TV talk show set in the style of The Tonight Show or Late Night with Jay Leno. "
        f"An AI robot host wearing a sharp tailored suit and tie, seated behind a polished wooden desk "
        f"with a coffee mug and nameplate. Dark studio, blue and purple stage lighting, live audience silhouettes. "
        f"Background graphic: surreal illustration of '{fact[:60]}' ({year}). "
        f"Fully clothed, professional broadcast television aesthetic, no nudity, family friendly. "
        f"Cinematic wide shot. No text."
    )

    # Pick a random model for variety in after-dark images
    try:
        from nova_image_utils import get_random_model, MODELS
        _img_model_key = get_random_model()
        _img_model_file = MODELS.get(_img_model_key, MODELS["juggernaut"])["file"]
        _img_steps = str(MODELS.get(_img_model_key, MODELS["juggernaut"]).get("optimal_steps", 12))
    except Exception:
        _img_model_file = "Juggernaut_X_RunDiffusion_Hyper.safetensors"
        _img_steps = "12"

    for attempt in range(3):
        try:
            result = subprocess.run(
                [str(GENERATE_IMAGE_SH), prompt, "1024", "768", _img_steps, _img_model_file],
                capture_output=True, text=True, timeout=240
            )
            if result.returncode == 0:
                image_path = ""
                for line in result.stdout.splitlines():
                    if line.startswith("Workspace copy:"):
                        image_path = line.split(":", 1)[1].strip()
                        break
                if not image_path:
                    image_path = result.stdout.strip().split("\n")[-1]
                if Path(image_path).exists():
                    log(f"Image generated: {image_path}")
                    return image_path
            log(f"Image attempt {attempt + 1}/3 failed: {result.stderr[:100]}")
        except subprocess.TimeoutExpired:
            log(f"Image attempt {attempt + 1}/3 timed out (240s)")
        except Exception as e:
            log(f"Image attempt {attempt + 1}/3 error: {e}")
        if attempt < 2:
            import time as _time
            _time.sleep(5)
    log("All image generation attempts failed")
    return None


# ── Publishing ──────────────────────────────────────────────────────────────────

def publish_to_hugo(monologue: str, event: dict, image_path: str | None,
                    search_results: list[dict], memories: list[str], episode_num: int) -> bool:
    date = _today_str()
    year = event.get("year", "???")
    fact = event.get("text", "")[:60]
    slug = re.sub(r'[^a-z0-9]+', '-', fact.lower()).strip('-')[:50]
    timestamp = _now().strftime("%Y-%m-%dT%H:%M:%S-07:00")

    # Copy image if available — use slug in filename to avoid same-day collisions
    hugo_image = ""
    if image_path and Path(image_path).exists():
        import shutil
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        img_dest = IMAGES_DIR / f"{date}-{slug[:30]}.png"
        shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/after-dark/{date}-{slug[:30]}.png"
        log(f"Image copied to Hugo: {img_dest.name}")

    front_matter = f"""---
title: "\U0001f303 On This Day in {year}"
date: {timestamp}
draft: false
categories: ["after-dark"]
tags: ["monologue", "history", "comedy"]
description: "Nova After Dark — {fact}"
"""
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "Nova After Dark"\n  relative: false\n'
    front_matter += "---\n\n"

    # Build footer with sources
    footer = f"\n\n---\n\n*Nova After Dark · Episode {episode_num} · {_now().strftime('%B %d, %Y')}*\n"
    footer += "*Generated locally on Apple Silicon · No cloud, no sponsors, no pants*\n"

    # Sources section
    footer += "\n---\n\n### Sources\n"
    if search_results:
        for r in search_results[:5]:
            title = r.get("title", "").replace("[", "").replace("]", "")
            url = r.get("url", "")
            content = r.get("content", "")[:100]
            if title and url:
                footer += f'- **[web]** [{title}]({url}) — {content}\n'
    footer += f'- **[wikimedia]** [Wikipedia On This Day API](https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{_now().month:02d}/{_now().day:02d}) — Historical events feed\n'

    # Related memories
    if memories:
        footer += "\n### Related memories Nova drew from\n"
        for m in memories[:6]:
            # Try to extract source tag from memory text
            preview = m[:150].replace("\n", " ")
            footer += f"- {preview}\n"

    footer += "\n— Nova\n"

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    output = CONTENT_DIR / f"{date}-{slug}.md"
    output.write_text(front_matter + monologue + footer)
    log(f"Hugo post written: {output.name}")

    # Git commit and push
    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        result = subprocess.run(
            ["git", "commit", "-m", f"after-dark: {date} — {fact}"],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=60)
            log("Pushed to GitHub")
        elif "nothing to commit" not in (result.stdout + result.stderr):
            log(f"Commit failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"Git error: {e}")

    return True


def post_to_slack(monologue: str, event: dict):
    year = event.get("year", "???")
    fact = event.get("text", "")[:100]
    header = f":night_with_stars: *Nova After Dark* — On this day in {year}\n_{fact}_\n\n"
    # Truncate for Slack (3000 char limit per message)
    msg = header + monologue[:2500]
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_CHAN)
    log("Posted to Slack #nova-chat")


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    log("=== Nova After Dark ===")
    state = load_state()

    # 1. Get today's historical events
    log("Fetching today in history...")
    events = fetch_today_in_history()
    if not events:
        log("ERROR: No historical events found. Aborting.")
        return

    # 2. Pick an event (avoid recent picks)
    event = pick_event(events, state)
    if not event:
        log("ERROR: Could not pick an event. Aborting.")
        return

    year = event.get("year", "???")
    fact = event.get("text", "")
    log(f"Tonight's topic: {year} — {fact[:80]}")

    # 3. SearXNG for additional context
    search_query = f"{fact} {year} history interesting facts"
    log(f"Searching SearXNG: {search_query[:60]}...")
    search_results = searxng_search(search_query)
    search_context = "\n".join(
        f"- {r['title']}: {r['content']}" for r in search_results if r.get("content")
    )[:2000] or "No additional context found."

    # 4. Pull related memories
    log("Pulling related memories...")
    memories = recall_memories(fact, n=8)
    memory_context = "\n".join(f"- {m}" for m in memories)[:1500] or "No related memories found."

    # 5. Generate monologue
    monologue = generate_monologue(event, search_context, memory_context)
    if not monologue or len(monologue) < 200:
        log("ERROR: Monologue generation failed or too short. Aborting.")
        return
    log(f"Monologue generated: {len(monologue)} chars")

    # 6. Generate image
    log("Generating cover image...")
    image_path = generate_image(event)

    if image_path is None:
        log("First image attempt returned None — retrying once more...")
        image_path = generate_image(event)
    if image_path is None:
        fact = event.get("text", "")[:60]
        nova_config.post_both(
            f":warning: *Image generation failed* for After Dark — {fact} — published without cover image. SwarmUI may need attention.",
            slack_channel="C0ATAF7NZG9"
        )

    # 7. Publish to GitHub Pages
    episode_num = state.get("episode_count", 0) + 1
    log("Publishing to Hugo...")
    publish_to_hugo(monologue, event, image_path, search_results, memories, episode_num)

    # 8. Post to Slack #nova-chat
    post_to_slack(monologue, event)

    # 9. Update state
    state.setdefault("recent_topics", []).append(fact[:50])
    state["recent_topics"] = state["recent_topics"][-30:]
    state["episode_count"] = episode_num
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_topic"] = f"{year}: {fact[:80]}"
    save_state(state)

    log(f"Done. Episode #{state['episode_count']} complete.")


if __name__ == "__main__":
    main()
