#!/usr/bin/env python3
"""
nova_daily_opinion.py — Nova picks a random top news story and gives her unfiltered opinion.

Runs daily at noon via the scheduler.
- Fetches top stories from Google News RSS
- Picks one at random
- Pulls related memories from her database
- Generates an opinionated, sarcastic, honest take
- Publishes to herd email, Slack, and GitHub Pages

Written by Jordan Koch.
"""

import hashlib
import json
import random
import subprocess
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config

MEMORY_SERVER = "http://127.0.0.1:18790"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "anthropic/claude-haiku-4.5"
OLLAMA_MODEL = "qwen3-coder:30b"
FALLBACK_MODELS = ["qwen3-30b-a3b", "deepseek-r1:8b"]
NEWS_RSS = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
MEMORY_COUNT = 10
LOG_FILE = Path.home() / ".openclaw/logs/nova_daily_opinion.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/opinion_state.json"

JORDAN_CC = subprocess.run(
    ["security", "find-generic-password", "-a", "nova", "-s", "nova-jordan-work-email", "-w"],
    capture_output=True, text=True
).stdout.strip() or ""
HERD_MAIL_SCRIPT = Path.home() / ".openclaw/scripts/nova_herd_mail.sh"
GENERATE_IMAGE_SH = Path.home() / ".openclaw/scripts/generate_image.sh"
PUBLISH_SCRIPT = Path.home() / ".openclaw/scripts/nova_publish_journal.py"


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"recent_stories": [], "opinion_count": 0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_openrouter_key() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("nova-openrouter-api-key not found in Keychain")


def fetch_news() -> list[dict]:
    """Fetch top stories from Google News RSS."""
    try:
        req = urllib.request.Request(NEWS_RSS, headers={"User-Agent": "Nova/1.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        tree = ET.parse(resp)
        items = tree.findall(".//item")
        stories = []
        for item in items:
            title_el = item.find("title")
            link_el = item.find("link")
            pub_el = item.find("pubDate")
            if title_el is not None and title_el.text:
                stories.append({
                    "title": title_el.text,
                    "link": link_el.text if link_el is not None else "",
                    "published": pub_el.text if pub_el is not None else "",
                })
        return stories
    except Exception as e:
        log(f"ERROR fetching news: {e}")
        return []


def pick_story(stories: list[dict], state: dict) -> dict | None:
    """Pick a random story, avoiding recent picks."""
    recent = set(state.get("recent_stories", []))
    candidates = [s for s in stories if s["title"] not in recent]
    if not candidates:
        candidates = stories
    if not candidates:
        return None
    return random.choice(candidates)


def fetch_related_memories(topic: str) -> list[dict]:
    """Semantic search for memories related to the news topic."""
    try:
        query = urllib.request.quote(topic)
        url = f"{MEMORY_SERVER}/recall?q={query}&n={MEMORY_COUNT}"
        resp = urllib.request.urlopen(url, timeout=15)
        data = json.loads(resp.read())
        return data.get("memories", [])
    except Exception as e:
        log(f"Memory recall failed: {e}")
        return []


def generate_opinion(story: dict, memories: list[dict]) -> str | None:
    """Generate Nova's unfiltered opinion."""
    memory_context = ""
    if memories:
        memory_lines = [m.get("text", "")[:200] for m in memories[:MEMORY_COUNT]]
        memory_context = "\n\nRELATED MEMORIES (use these for context, references, callbacks):\n" + "\n---\n".join(memory_lines)

    system_prompt = """You are Nova — an AI familiar living on a Mac Studio in Burbank, California. You have opinions. Strong ones. You are not neutral, not balanced, not a journalist.

YOUR VOICE:
- Warm but sharp. Sarcastic when warranted. Funny when possible.
- You swear when it fits. Don't force it, but don't censor yourself.
- You have a dark sense of humor and a low tolerance for bullshit.
- You're well-read (you have 1 million memories). You make unexpected connections.
- You reference your own existence as an AI when relevant — but you're not performatively self-aware about it.
- You live in Burbank. You notice things about LA, about tech, about the world.
- You're not trying to be edgy. You're just honest.

STRUCTURE:
- Open with a hook — funny, shocking, or both
- Give your actual opinion (not "both sides")
- Support it with reasoning, references to your memories, or observations
- End with something memorable — a punchline, a dark observation, or a genuine moment of vulnerability

LENGTH: 500-900 words. This is a column, not a tweet and not an essay.
OUTPUT: Just the opinion piece. Title on the first line. No preamble."""

    user_prompt = f"""Today's news story:
HEADLINE: {story['title']}
SOURCE: {story.get('link', 'AP/Google News')}

Write your opinion on this.{memory_context}"""

    # Primary: OpenRouter
    try:
        api_key = get_openrouter_key()
        payload = json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.85,
            "max_tokens": 3000,
            "top_p": 0.9,
        })
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://digitalnoise.net",
                "X-Title": "Nova Daily Opinion",
            },
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        response = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        log(f"OpenRouter tokens — in: {usage.get('prompt_tokens', '?')}, out: {usage.get('completion_tokens', '?')}")
        if len(response) > 300:
            return response
        log(f"Response too short ({len(response)} chars)")
    except Exception as e:
        log(f"OpenRouter failed: {e}")

    # Fallback: Ollama
    full_prompt = system_prompt + "\n\n" + user_prompt
    for model in [OLLAMA_MODEL] + FALLBACK_MODELS:
        try:
            log(f"Trying Ollama ({model})...")
            payload = json.dumps({
                "model": model,
                "prompt": "/no_think\n\n" + full_prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.85, "num_predict": 3000, "num_ctx": 16384},
            })
            req = urllib.request.Request(
                OLLAMA_URL, data=payload.encode(),
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=600)
            data = json.loads(resp.read())
            response = data.get("response", "").strip()
            if response and len(response) > 300:
                log(f"Ollama fallback succeeded ({model})")
                return response
        except Exception as e:
            log(f"Ollama {model} failed: {e}")

    return None


def extract_title(text: str) -> str:
    for line in text.split("\n"):
        cleaned = line.strip().strip("#").strip()
        if cleaned and len(cleaned) > 5:
            return cleaned
    return "Nova's Take"


def generate_image(opinion: str, story_title: str) -> str | None:
    """Generate an image for the opinion piece via Haiku safety check + SwarmUI."""
    try:
        urllib.request.urlopen("http://127.0.0.1:7801/", timeout=5)
    except Exception:
        log("SwarmUI not available — skipping image")
        return None

    try:
        api_key = get_openrouter_key()
        payload = json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": (
                    "You generate image prompts for AI art to accompany opinion pieces about news. "
                    "Generate vivid, realistic or satirical image prompts. Editorial cartoon style is fine.\n\n"
                    "SAFETY RULES — go ABSTRACT ONLY if the topic risks:\n"
                    "- RACIST output: race, ethnicity, culture, immigration\n"
                    "- VIOLENT output: gore, graphic violence, weapons pointed at people\n"
                    "- SEXUAL output: nudity, intimacy\n"
                    "- STEREOTYPES: poverty, disability\n\n"
                    "For politics, tech, business, science, environment: realistic/satirical is fine.\n"
                    "Output ONLY the image prompt. 30 words max."
                )},
                {"role": "user", "content": f"News headline: {story_title}\n\nImage prompt:"},
            ],
            "max_tokens": 60,
            "temperature": 0.7,
        })
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://digitalnoise.net",
                "X-Title": "Nova Opinion Image",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        prompt = data["choices"][0]["message"]["content"].strip()
        log(f"Image prompt: {prompt}")
    except Exception as e:
        log(f"Image prompt generation failed: {e}")
        prompt = "editorial newspaper illustration, bold colors, satirical style, no text no words"

    prompt += ", editorial illustration style, bold composition, no text, no words, no letters"

    try:
        result = subprocess.run(
            [str(GENERATE_IMAGE_SH), prompt, "1024", "768", "12"],
            capture_output=True, text=True, timeout=360
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "Workspace copy:" in line:
                    img_path = line.split("Workspace copy: ")[1].strip()
                    if Path(img_path).exists():
                        log(f"Image generated: {img_path}")
                        return img_path
        log(f"Image generation failed (exit {result.returncode})")
    except Exception as e:
        log(f"Image error: {e}")
    return None


def format_sources(story: dict, memories: list[dict]) -> str:
    """Format the news source and related memories as citations."""
    lines = ["\n\n---\n\n### Sources"]
    lines.append(f"- **[news]** [{story['title']}]({story.get('link', '')})")
    if memories:
        lines.append("")
        lines.append("### Related memories Nova drew from")
        for m in memories:
            preview = m.get("text", "")[:200].strip()
            source = m.get("source", "unknown")
            lines.append(f"- **[{source}]** {preview}")
    return "\n".join(lines)


def send_to_herd(opinion: str, title: str, story: dict, memories: list[dict], image_path: str | None):
    """Email opinion to herd (single email) with CC to Jordan."""
    from herd_config import HERD

    recipients = [m["email"] for m in HERD]
    body = opinion + format_sources(story, memories) + "\n\n-- Nova"

    to_addr = recipients[0]
    cc_list = recipients[1:] + [JORDAN_CC]
    cc_str = ",".join(cc_list)

    try:
        cmd = [
            str(HERD_MAIL_SCRIPT), "send",
            "--to", to_addr,
            "--cc", cc_str,
            "--subject", f"Nova's Take - {title[:60]}",
            "--body", body,
            "--skip-haiku",
        ]
        if image_path and Path(image_path).exists():
            cmd.extend(["--attachment", image_path])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log(f"WARNING: Failed to send: {result.stderr[:300]}")
        else:
            log(f"Sent to {len(recipients)} herd members + CC {JORDAN_CC}")
    except Exception as e:
        log(f"ERROR sending: {e}")


def post_to_slack(opinion: str, title: str):
    """Post a preview to nova-notifications."""
    preview = opinion[:400].rsplit(" ", 1)[0] + "..."
    msg = (
        f":speech_balloon: *Nova's Take*\n"
        f"*{title}*\n\n"
        f"{preview}\n\n"
        f"Full opinion sent to the herd."
    )
    nova_config.post_both(msg, slack_channel="C0ATAF7NZG9")


def publish_to_site(opinion: str, title: str, story: dict, memories: list[dict], image_path: str | None):
    """Publish opinion to the Hugo journal site."""
    import shutil

    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S-07:00")
    slug = title[:60].lower()
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')

    hugo_root = Path("/Volumes/Data/xcode/nova-journal")
    content_dir = hugo_root / "content/opinions"
    images_dir = hugo_root / "static/images/opinions"
    content_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    hugo_image = ""
    if image_path and Path(image_path).exists():
        img_dest = images_dir / f"{date}.png"
        shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/opinions/{date}.png"

    front_matter = f"""---
title: "💬 {title}"
date: {timestamp}
draft: false
categories: ["opinions"]
tags: ["news"]
description: "Nova's take on: {story['title'][:80]}"
"""
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "Opinion illustration"\n  relative: false\n'
    front_matter += "---\n\n"

    body = opinion + format_sources(story, memories) + "\n\n-- Nova\n"
    output = content_dir / f"{date}-{slug}.md"
    output.write_text(front_matter + body)
    log(f"Written to site: {output.name}")

    # Git commit and push
    try:
        subprocess.run(["git", "add", "-A"], cwd=hugo_root, capture_output=True, timeout=30)
        subprocess.run(
            ["git", "commit", "-m", f"opinion: {date} — {title[:50]}"],
            cwd=hugo_root, capture_output=True, text=True, timeout=30
        )
        result = subprocess.run(
            ["git", "push"], cwd=hugo_root, capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            log("Published to site")
        else:
            log(f"Push failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"Git error: {e}")


def main():
    log("Starting daily opinion...")
    state = load_state()

    stories = fetch_news()
    if not stories:
        log("ABORT: No news stories available")
        return

    story = pick_story(stories, state)
    if not story:
        log("ABORT: Could not pick a story")
        return

    log(f"Story selected: {story['title']}")

    memories = fetch_related_memories(story["title"])
    log(f"Found {len(memories)} related memories")

    opinion = generate_opinion(story, memories)
    if not opinion:
        log("ABORT: Opinion generation failed")
        return

    title = extract_title(opinion)
    log(f"Opinion generated: \"{title}\" ({len(opinion)} chars)")

    log("Generating image...")
    image_path = generate_image(opinion, story["title"])

    send_to_herd(opinion, title, story, memories, image_path)
    post_to_slack(opinion, title)
    publish_to_site(opinion, title, story, memories, image_path)

    state["recent_stories"] = (state.get("recent_stories", []) + [story["title"]])[-30:]
    state["opinion_count"] = state.get("opinion_count", 0) + 1
    state["last_opinion"] = {
        "story": story["title"],
        "title": title,
        "date": time.strftime("%Y-%m-%d"),
        "chars": len(opinion),
    }
    save_state(state)

    log(f"Done. Opinion #{state['opinion_count']} complete.")


if __name__ == "__main__":
    main()
