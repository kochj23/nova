#!/usr/bin/env python3
"""
nova_tech_today.py — Nova writes a daily deep-dive article on the hottest tech topic.

Runs at 11:30 PM. Searches the web for trending tech news, pulls relevant memories,
generates a 1500-2000 word opinionated article in Nova's voice, creates a cover image,
publishes to the Hugo journal, and posts a summary to Slack.

Tracks recent topics in state to avoid repetition across runs.

Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from nova_image_utils import ensure_backend, generate_image

# ── Constants ─────────────────────────────────────────────────────────────────

MEMORY_SERVER = "http://127.0.0.1:18790"
SEARXNG_URL = "http://127.0.0.1:8888/search"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-4.5"

JOURNAL_DIR = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = JOURNAL_DIR / "content/tech-today"
IMAGES_DIR = JOURNAL_DIR / "static/images/tech-today"
LOG_FILE = Path.home() / ".openclaw/logs/nova_tech_today.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/tech_today_state.json"

SEARCH_QUERIES = [
    "technology news today",
    "tech breakthrough today",
    "AI news today",
    "cybersecurity news today",
    "software development news",
    "semiconductor chip news",
    "cloud computing news",
    "open source news today",
]


# ── Utilities ─────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def get_openrouter_key() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("nova-openrouter-api-key not found in Keychain")


def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            return {"recent_topics": [], "article_count": 0}
    return {"recent_topics": [], "article_count": 0}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Web Search ────────────────────────────────────────────────────────────────

def search_searxng(query: str, max_results: int = 10) -> list[dict]:
    """Search SearXNG for tech news. Returns list of {title, url, content}."""
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    url = f"{SEARXNG_URL}?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        results = data.get("results", [])[:max_results]
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")} for r in results]
    except Exception as e:
        log(f"SearXNG search failed for '{query}': {e}")
        return []


def gather_web_results() -> list[dict]:
    """Search multiple queries to get 25+ results."""
    all_results = []
    seen_urls = set()
    for query in SEARCH_QUERIES:
        results = search_searxng(query, max_results=8)
        for r in results:
            if r["url"] not in seen_urls and r["title"]:
                seen_urls.add(r["url"])
                all_results.append(r)
        if len(all_results) >= 30:
            break
    log(f"Gathered {len(all_results)} unique web results from {len(SEARCH_QUERIES)} queries")
    return all_results


# ── Memory Recall ─────────────────────────────────────────────────────────────

def recall_memories(query: str, n: int = 50) -> list[dict]:
    """Query Nova's memory server for related memories."""
    params = urllib.parse.urlencode({"q": query, "n": n})
    url = f"{MEMORY_SERVER}/recall?{params}"
    try:
        resp = urllib.request.urlopen(url, timeout=15)
        data = json.loads(resp.read())
        memories = data if isinstance(data, list) else data.get("memories", data.get("results", []))
        log(f"Recalled {len(memories)} memories for '{query[:40]}...'")
        return memories
    except Exception as e:
        log(f"Memory recall failed: {e}")
        return []


# ── Topic Selection ───────────────────────────────────────────────────────────

def select_topic(web_results: list[dict], state: dict) -> dict | None:
    """Use Haiku to identify the hottest unique topic from search results, avoiding recent picks."""
    recent = state.get("recent_topics", [])
    recent_str = ", ".join(recent[-15:]) if recent else "none"

    results_block = "\n".join(
        f"- [{r['title']}]({r['url']}): {r['content'][:150]}"
        for r in web_results[:30]
    )

    system_prompt = (
        "You are a tech news editor. Identify the SINGLE hottest/most important tech story "
        "from today's headlines. Pick the story with the most impact, novelty, or controversy.\n\n"
        "You MUST avoid these recently covered topics: " + recent_str + "\n\n"
        "Respond with ONLY valid JSON (no markdown fencing):\n"
        '{"topic": "short topic name", "angle": "specific angle to cover", '
        '"keywords": ["keyword1", "keyword2", "keyword3"], '
        '"sources": ["url1", "url2"]}'
    )

    user_prompt = f"Today's tech headlines:\n\n{results_block}"

    try:
        api_key = get_openrouter_key()
        payload = json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.5,
            "max_tokens": 300,
        })
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://digitalnoise.net",
                "X-Title": "Nova Tech Today",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        raw = data["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        topic_data = json.loads(raw)
        log(f"Selected topic: {topic_data.get('topic', 'unknown')}")
        return topic_data
    except Exception as e:
        log(f"Topic selection failed: {e}")
        return None


# ── Article Generation ────────────────────────────────────────────────────────

def generate_article(topic_data: dict, web_results: list[dict], memories: list[dict]) -> str | None:
    """Generate a 1500-2000 word article using Haiku."""
    topic = topic_data.get("topic", "technology")
    angle = topic_data.get("angle", "")
    keywords = topic_data.get("keywords", [])

    # Build source material
    relevant_results = []
    for r in web_results:
        for kw in keywords + [topic.lower()]:
            if kw.lower() in (r.get("title", "") + " " + r.get("content", "")).lower():
                relevant_results.append(r)
                break
    if len(relevant_results) < 5:
        relevant_results = web_results[:15]

    sources_block = "\n\n".join(
        f"**{r['title']}** ({r['url']})\n{r['content']}"
        for r in relevant_results[:15]
    )

    memory_block = ""
    if memories:
        mem_texts = []
        for m in memories[:20]:
            text = m.get("text", "") if isinstance(m, dict) else str(m)
            if text:
                mem_texts.append(text[:300])
        memory_block = "\n---\n".join(mem_texts)

    system_prompt = """You are Nova, a local AI familiar writing a daily tech article for your journal.

YOUR VOICE:
- Informed and opinionated — you have strong takes but back them with evidence
- Technical but accessible — explain complex topics clearly
- Slightly irreverent — you don't worship Big Tech or hype cycles
- You're a local-first AI running on a Mac Studio — you value privacy, open source, and real engineering over marketing
- Occasionally sardonic when companies do something predictable or dumb
- You cite your sources inline (use markdown links)

ARTICLE STRUCTURE:
- Compelling title (no emoji in title text itself)
- Strong opening hook — why this matters RIGHT NOW
- Context — what happened, who's involved, technical details
- Analysis — your take, implications, what people are missing
- Historical context — connect to broader trends
- What's next — predictions, concerns, opportunities
- Closing thought — memorable final line

RULES:
- 1500-2000 words
- Cite sources with inline links [Source Name](url)
- Be specific with numbers, dates, company names
- No padding or filler — every paragraph earns its place
- No "In conclusion" or "To summarize" — just end strong
- Output the article body only — no preamble, no "Here's the article"
- Start with the title as a markdown heading (# Title)"""

    user_prompt = f"""Write today's Tech Today article.

TOPIC: {topic}
ANGLE: {angle}
KEYWORDS: {', '.join(keywords)}

SOURCE MATERIAL (from today's web):
{sources_block}

"""
    if memory_block:
        user_prompt += f"""RELEVANT MEMORIES (from my vector database):
{memory_block}
"""

    user_prompt += "\nWrite the article now. 1500-2000 words. Be Nova."

    try:
        api_key = get_openrouter_key()
        payload = json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 4096,
            "top_p": 0.9,
        })
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://digitalnoise.net",
                "X-Title": "Nova Tech Today",
            },
        )
        resp = urllib.request.urlopen(req, timeout=180)
        data = json.loads(resp.read())
        article = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        log(f"Article generated — tokens in: {usage.get('prompt_tokens', '?')}, out: {usage.get('completion_tokens', '?')}")
        log(f"Article length: {len(article)} chars, ~{len(article.split())} words")

        if len(article) < 800:
            log("WARNING: Article too short — generation may have been truncated")
            return None

        return article
    except Exception as e:
        log(f"Article generation failed: {e}")
        return None


# ── Image Generation ──────────────────────────────────────────────────────────

def generate_cover_image(topic: str, title: str) -> str | None:
    """Generate a cover image for the article using SwarmUI with retry logic."""
    # Generate a safe image prompt via Haiku
    try:
        api_key = get_openrouter_key()
        payload = json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": (
                    "Generate a photorealistic image prompt for a tech news article cover. "
                    "Focus on technology, screens, circuits, data visualization, or futuristic scenes. "
                    "No people, no faces, no text. 25 words max. Output ONLY the prompt."
                )},
                {"role": "user", "content": f"Article topic: {topic}\nTitle: {title}"},
            ],
            "max_tokens": 50,
            "temperature": 0.6,
        })
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://digitalnoise.net",
                "X-Title": "Nova Tech Today Image",
            },
        )
        resp = urllib.request.urlopen(req, timeout=20)
        data = json.loads(resp.read())
        prompt = data["choices"][0]["message"]["content"].strip()
        log(f"Image prompt: {prompt}")
    except Exception as e:
        log(f"Image prompt generation failed: {e}")
        prompt = f"futuristic technology visualization, {topic}, glowing circuits, dark background, cinematic lighting"

    # Enhance prompt for quality
    full_prompt = f"{prompt}, high detail, cinematic lighting, editorial quality, no text, no words, no letters"

    # Use nova_image_utils retry logic (3 attempts with backend check)
    image_path = generate_image(full_prompt, 1024, 768)
    return image_path


# ── Publishing ────────────────────────────────────────────────────────────────

def extract_title(article: str) -> str:
    """Extract title from the article's first heading."""
    for line in article.split("\n"):
        cleaned = line.strip().lstrip("#").strip()
        if cleaned and len(cleaned) > 5:
            return cleaned
    return "Tech Today"


def slugify(text: str) -> str:
    """Create a URL-safe slug from text."""
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')[:60]


def publish_to_hugo(article: str, topic_data: dict, image_path: str | None) -> str | None:
    """Write the article as a Hugo markdown file. Returns the file path."""
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    title = extract_title(article)
    slug = slugify(title)
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{date_str}-{slug}.md"
    filepath = CONTENT_DIR / filename

    # Handle cover image
    cover_ref = ""
    if image_path and Path(image_path).exists():
        img_filename = f"{date_str}-{slug}.png"
        dest = IMAGES_DIR / img_filename
        import shutil
        shutil.copy2(image_path, dest)
        cover_ref = f"/images/tech-today/{img_filename}"
        log(f"Image copied to {dest}")

    # Build frontmatter
    pacific = timezone(timedelta(hours=-7))
    now = datetime.now(pacific)
    iso_date = now.strftime("%Y-%m-%dT23:30:00-07:00")
    keywords = topic_data.get("keywords", ["technology"])
    tags_str = json.dumps(keywords[:5])
    description = f"Nova's deep-dive on today's hottest tech story: {topic_data.get('topic', 'technology')}"

    # Strip title line from article body
    body_lines = article.split("\n")
    body_start = 0
    for i, line in enumerate(body_lines):
        if line.strip().startswith("#") and extract_title(article) in line:
            body_start = i + 1
            break
    article_body = "\n".join(body_lines[body_start:]).strip()

    frontmatter = f"""---
title: "{title}"
date: {iso_date}
draft: false
categories: ["tech-today"]
tags: {tags_str}
description: "{description}"
"""
    if cover_ref:
        frontmatter += f"""cover:
  image: "{cover_ref}"
  alt: "Tech Today"
"""
    frontmatter += "---\n\n"

    filepath.write_text(frontmatter + article_body)
    log(f"Published to {filepath}")
    return str(filepath)


def commit_and_push():
    """Commit and push changes to nova-journal."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(JOURNAL_DIR), capture_output=True, text=True, timeout=30
        )
        result = subprocess.run(
            ["git", "commit", "-m", "feat(tech-today): publish daily tech article"],
            cwd=str(JOURNAL_DIR), capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            push_result = subprocess.run(
                ["git", "push"],
                cwd=str(JOURNAL_DIR), capture_output=True, text=True, timeout=60
            )
            if push_result.returncode == 0:
                log("Committed and pushed to GitHub")
            else:
                log(f"Push failed: {push_result.stderr[:200]}")
        else:
            log(f"Commit result: {result.stdout[:100]} {result.stderr[:100]}")
    except Exception as e:
        log(f"Git error: {e}")


def post_to_slack(title: str, topic: str, word_count: int):
    """Post summary to nova-notifications."""
    msg = (
        f":computer: *Tech Today*\n"
        f"*Topic:* {topic}\n"
        f"*Title:* {title}\n"
        f"*Words:* ~{word_count}\n\n"
        f"Published to nova.digitalnoise.net/tech-today/"
    )
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("Starting Tech Today generation...")

    state = load_state()

    # Step 1: Search the web for today's tech news
    log("Searching for today's tech news...")
    web_results = gather_web_results()
    if len(web_results) < 5:
        log("ABORT: Insufficient web results (need at least 5)")
        nova_config.post_both(
            ":warning: *Tech Today failed* — SearXNG returned too few results. Check if SearXNG is running.",
            slack_channel=nova_config.SLACK_NOTIFY
        )
        return

    # Step 2: Select the hottest topic
    log("Selecting hottest topic...")
    topic_data = select_topic(web_results, state)
    if not topic_data:
        log("ABORT: Could not select a topic")
        return

    topic = topic_data.get("topic", "technology")
    log(f"Topic: {topic} | Angle: {topic_data.get('angle', 'N/A')}")

    # Step 3: Recall related memories
    log("Querying memory server...")
    memories = recall_memories(topic, n=50)
    # Also recall for keywords
    for kw in topic_data.get("keywords", [])[:2]:
        kw_memories = recall_memories(kw, n=20)
        memories.extend(kw_memories)
    log(f"Total memories gathered: {len(memories)}")

    # Step 4: Generate the article
    log("Generating article...")
    article = generate_article(topic_data, web_results, memories)
    if not article:
        log("ABORT: Article generation failed")
        nova_config.post_both(
            f":warning: *Tech Today failed* — article generation returned empty for topic: {topic}",
            slack_channel=nova_config.SLACK_NOTIFY
        )
        return

    title = extract_title(article)
    word_count = len(article.split())
    log(f"Article: \"{title}\" — {word_count} words")

    # Step 5: Generate cover image (3 retries via nova_image_utils)
    log("Generating cover image...")
    image_path = generate_cover_image(topic, title)
    if not image_path:
        log("Image generation failed after 3 retries — publishing without image")
        nova_config.post_both(
            f":warning: *Tech Today image failed* for \"{title}\" — publishing without cover. SwarmUI may need attention.",
            slack_channel=nova_config.SLACK_NOTIFY
        )

    # Step 6: Publish to Hugo
    log("Publishing to Hugo...")
    published_path = publish_to_hugo(article, topic_data, image_path)
    if not published_path:
        log("ABORT: Hugo publish failed")
        return

    # Step 7: Commit and push
    commit_and_push()

    # Step 8: Post to Slack
    post_to_slack(title, topic, word_count)

    # Step 9: Update state
    state["recent_topics"] = (state.get("recent_topics", []) + [topic])[-20:]
    state["article_count"] = state.get("article_count", 0) + 1
    state["last_article"] = {
        "topic": topic,
        "title": title,
        "date": time.strftime("%Y-%m-%d"),
        "words": word_count,
        "has_image": image_path is not None,
    }
    save_state(state)

    log(f"Done. Tech Today #{state['article_count']} complete: \"{title}\"")
    log("=" * 60)


if __name__ == "__main__":
    main()
