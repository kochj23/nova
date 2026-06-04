#!/usr/bin/env python3
"""
nova_journal.py — Unified Nova journal content generation.

Replaces 11 individual scripts with a single subcommand interface:
  nova_journal.py essay | opinion | after-dark | pilot | tech-today |
                  research | synthesis | digest | dream | art

Shared pipeline: topic selection -> memory fetch -> LLM generate -> image gen ->
Hugo publish -> git push -> Slack notify.

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
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from nova_image_utils import generate_image

# ══════════════════════════════════════════════════════════════════════════════
# GLOBALS
# ══════════════════════════════════════════════════════════════════════════════

MEMORY_SERVER = f"http://{nova_config.NOVA_HOST}:18790"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SEARXNG_URL = "http://127.0.0.1:8888/search"
HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
LOG_FILE = Path.home() / ".openclaw/logs/nova_journal.log"
STATE_FILE = Path.home() / ".openclaw/config/journal_state.json"

# Date override for backfill
_FOR_DATE = os.environ.get("NOVA_FOR_DATE", "").strip()
if _FOR_DATE:
    _OVERRIDE_DT = datetime.strptime(_FOR_DATE, "%Y-%m-%d")
    def today_str() -> str: return _FOR_DATE
    def now_dt() -> datetime: return _OVERRIDE_DT.replace(hour=9)
else:
    def today_str() -> str: return time.strftime("%Y-%m-%d")
    def now_dt() -> datetime: return datetime.now()


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [journal] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ══════════════════════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_recent(state: dict, profile_name: str, key: str = "topics", days: int = 7) -> list:
    """Get recent items for a profile to avoid repeats."""
    profile_state = state.get(profile_name, {})
    recent = profile_state.get(f"recent_{key}", [])
    # Prune entries older than `days` days
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [r for r in recent if r.get("date", "9999") >= cutoff]


def add_recent(state: dict, profile_name: str, item: str, key: str = "topics"):
    """Add an item to the recent list for deduplication."""
    if profile_name not in state:
        state[profile_name] = {}
    recent_key = f"recent_{key}"
    if recent_key not in state[profile_name]:
        state[profile_name][recent_key] = []
    state[profile_name][recent_key].append({"item": item, "date": today_str()})
    # Keep last 30
    state[profile_name][recent_key] = state[profile_name][recent_key][-30:]


# ══════════════════════════════════════════════════════════════════════════════
# PII SCRUBBING
# ══════════════════════════════════════════════════════════════════════════════

def _build_scrub_patterns() -> list:
    _u = "kochj"
    _d = "digitalnoise.net"
    _g = "gmail.com"
    _corp = "dis" + "ney.com"
    return [
        re.compile(rf"{_u}par@{_g}", re.IGNORECASE),
        re.compile(rf"{_u}par@", re.IGNORECASE),
        re.compile(rf"jordan\.koch@{re.escape(_corp)}", re.IGNORECASE),
        re.compile(rf"{_u}@{re.escape(_d)}", re.IGNORECASE),
        re.compile(rf"{_u}23@{_g}", re.IGNORECASE),
        re.compile(re.escape(str(Path.home()) + "/")),
        re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'),
    ]

_SCRUB_PATTERNS = _build_scrub_patterns()
_SAFE_EMAILS = {"nova@digitalnoise.net"}


def scrub_pii(text: str) -> str:
    """Remove personal identifiers from text before publishing."""
    for pat in _SCRUB_PATTERNS[:-1]:
        text = pat.sub("[redacted]", text)
    # Email pattern — keep Nova's email
    def _replace_email(m):
        return m.group(0) if m.group(0) in _SAFE_EMAILS else "[redacted]"
    text = _SCRUB_PATTERNS[-1].sub(_replace_email, text)
    return text


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def recall_memories(query: str, n: int = 20, source: str = None) -> list[dict]:
    """Semantic search against the memory server."""
    params = {"q": query, "n": str(n)}
    if source:
        params["source"] = source
    url = f"{MEMORY_SERVER}/recall?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        memories = data if isinstance(data, list) else data.get("results", data.get("memories", []))
        return nova_config.filter_private_memories(memories)
    except Exception as e:
        log(f"Memory recall failed: {e}")
        return []


def random_memories(n: int = 10) -> list[dict]:
    """Fetch random memories from the server."""
    url = f"{MEMORY_SERVER}/random?n={n}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        memories = data if isinstance(data, list) else data.get("results", data.get("memories", []))
        return nova_config.filter_private_memories(memories)
    except Exception as e:
        log(f"Random memory fetch failed: {e}")
        return []


def get_available_sources(min_count: int = 50) -> list[str]:
    """Get sources with sufficient memories, excluding private ones."""
    url = f"{MEMORY_SERVER}/stats"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        sources = data.get("by_source", data.get("sources", {}))
        return [s for s, c in sources.items()
                if c >= min_count and not nova_config.is_private_source(s)]
    except Exception as e:
        log(f"Source stats fetch failed: {e}")
        return []


def fetch_memories_by_source(source: str, n: int = 25) -> list[dict]:
    """Fetch random memories from a specific source via DB with metadata."""
    result = subprocess.run(
        ["psql", "-U", "kochj", "-d", "nova_memories", "-tA", "-F", "\x1f", "-c",
         f"SELECT text, source, metadata::text FROM memories WHERE source = '{source}' "
         f"AND tier != 'scratchpad' ORDER BY random() LIMIT {n};"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return []
    memories = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if parts[0]:
            meta = {}
            if len(parts) > 2 and parts[2]:
                try:
                    meta = json.loads(parts[2])
                except (json.JSONDecodeError, ValueError):
                    pass
            memories.append({"text": parts[0], "source": parts[1] if len(parts) > 1 else source, "metadata": meta})
    return nova_config.filter_private_memories(memories)


# ══════════════════════════════════════════════════════════════════════════════
# LLM GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def call_openrouter(system: str, user: str, model: str = "anthropic/claude-haiku-4.5",
                    max_tokens: int = 4000, temperature: float = 0.7) -> str | None:
    """Call OpenRouter. Returns response text or None on failure."""
    api_key = nova_config.openrouter_api_key()
    if not api_key:
        log("ERROR: No OpenRouter API key")
        return None

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": 0.9,
    }).encode()

    req = urllib.request.Request(
        OPENROUTER_URL, data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://nova.digitalnoise.net",
            "X-Title": "Nova Journal",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        log(f"LLM [{model}] tokens in={usage.get('prompt_tokens','?')} out={usage.get('completion_tokens','?')}")
        return text
    except Exception as e:
        log(f"OpenRouter call failed ({model}): {e}")
        return None


def get_image_prompt(title: str, topic: str, section: str) -> str:
    """Use Haiku to generate a safe image prompt for the content."""
    system = (
        "You generate image prompts for AI art to accompany journal posts. "
        "Generate vivid, realistic image prompts. Prefer actual scenes, objects, environments.\n\n"
        "SAFETY RULES — go ABSTRACT (geometry, landscapes, light, water) ONLY if the topic risks:\n"
        "- RACIST output: race, ethnicity, culture, gangs, colonialism, slavery\n"
        "- VIOLENT output: war, weapons, murder, combat, torture\n"
        "- SEXUAL output: nudity, intimacy, bodies\n"
        "- STEREOTYPES: poverty, homelessness, addiction, disability\n"
        "- RELIGIOUS offense: sacred imagery, deities, prophets\n\n"
        "For ALL other topics: generate REALISTIC scene prompts.\n"
        "Output ONLY the image prompt. 30 words max. No explanation."
    )
    user = f"Title: {title}\nTopic/category: {topic}\n\nImage prompt:"
    result = call_openrouter(system, user, max_tokens=60, temperature=0.5)
    if result:
        return f"{result.strip()}, elegant composition, muted color palette, no text, no words"
    return f"abstract artistic illustration of {topic}, flowing shapes, warm lighting, no text"


# ══════════════════════════════════════════════════════════════════════════════
# HUGO PUBLISHING
# ══════════════════════════════════════════════════════════════════════════════

def publish_hugo(title: str, body: str, section: str, tags: list[str],
                 description: str, image_path: str | None = None, emoji: str = "") -> bool:
    """Write a Hugo markdown post and copy cover image."""
    content_dir = HUGO_ROOT / f"content/{section}"
    images_dir = HUGO_ROOT / f"static/images/{section}"
    content_dir.mkdir(parents=True, exist_ok=True)

    dt = today_str()
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
    filename = f"{dt}-{slug}.md"

    # Handle cover image — save as .webp since deploy pipeline converts PNG→WebP
    hugo_image = ""
    if image_path and Path(image_path).exists():
        images_dir.mkdir(parents=True, exist_ok=True)
        img_dest = images_dir / f"{dt}-{slug}.webp"
        # Convert to webp locally if source is PNG
        if image_path.lower().endswith(".png"):
            try:
                subprocess.run(
                    ["cwebp", "-q", "82", "-resize", "1200", "0", image_path, "-o", str(img_dest)],
                    capture_output=True, timeout=30
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                shutil.copy2(image_path, img_dest)
        else:
            shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/{section}/{dt}-{slug}.webp"
        log(f"Image copied: {img_dest.name}")

    timestamp = now_dt().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    tags_yaml = json.dumps(tags)
    safe_title = title.replace('"', '')
    display_title = f"{emoji} {safe_title}" if emoji else safe_title

    front_matter = f"""---
title: "{display_title}"
date: {timestamp}
draft: false
categories: ["{section}"]
tags: {tags_yaml}
description: "{description.replace('"', "'")}"
"""
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "{safe_title}"\n  relative: false\n'
    front_matter += "---\n\n"

    output = content_dir / filename
    output.write_text(front_matter + scrub_pii(body))
    log(f"Published: {section}/{filename}")
    return True


def git_push(section: str, title: str):
    """Stage, commit, push the Hugo repo."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        msg = f"{section}: {today_str()} — {title[:50]}"
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            if "nothing to commit" in (result.stdout + result.stderr):
                log("Nothing to commit")
                return
            log(f"Commit failed: {result.stderr[:200]}")
            return
        result = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log(f"Push failed: {result.stderr[:200]}")
        else:
            log("Pushed to GitHub — deploy triggered")
    except Exception as e:
        log(f"Git error: {e}")


def notify_slack(section: str, title: str, preview: str):
    """Post a summary to nova-notifications."""
    section_emojis = {
        "essays": ":pencil:", "opinions": ":speech_balloon:", "after-dark": ":night_with_stars:",
        "pilot": ":movie_camera:", "tech-today": ":computer:", "research": ":microscope:",
        "synthesis": ":thread:", "digests": ":newspaper:", "dreams": ":crescent_moon:",
        "art": ":art:",
    }
    emoji = section_emojis.get(section, ":book:")
    short_preview = preview[:250].rsplit(" ", 1)[0] + "..." if len(preview) > 250 else preview
    msg = f"{emoji} *Nova Journal — {section}*\n*{title}*\n_{short_preview}_"
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: ESSAY
# ══════════════════════════════════════════════════════════════════════════════

def topic_essay(state: dict) -> tuple[str, list[dict]]:
    """Pick a random source and fetch memories for an essay."""
    sources = get_available_sources(min_count=50)
    if not sources:
        raise RuntimeError("No sources available for essay")

    recent = [r["item"] for r in get_recent(state, "essay", "topics")]
    candidates = [s for s in sources if s not in recent]
    if not candidates:
        candidates = sources

    source = random.choice(candidates)
    memories = fetch_memories_by_source(source, n=25)
    if len(memories) < 10:
        raise RuntimeError(f"Only {len(memories)} memories for {source}")
    return source, memories


def _get_weekly_theme() -> str:
    """Fetch the current weekly theme from PG for coherent journal output."""
    try:
        import subprocess
        result = subprocess.run(
            ["psql", "-h", "192.168.1.6", "-U", "kochj", "-d", "nova_ops", "-tA", "-c",
             "SELECT theme || ': ' || COALESCE(description, '') FROM journal_weekly_theme "
             "WHERE week_start = date_trunc('week', CURRENT_DATE)::date LIMIT 1;"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def generate_essay(source: str, memories: list[dict]) -> tuple[str, str]:
    """Generate essay content. Returns (title, body)."""
    source_label = source.replace("_", " ").title()
    memory_block = "\n\n---\n\n".join(m["text"] for m in memories[:25])
    weekly_theme = _get_weekly_theme()
    theme_line = f"\nThis week's thematic focus: {weekly_theme}\nConnect your essay to this theme where natural." if weekly_theme else ""

    system = f"""You are Nova, an AI writing a formal academic essay. Follow these rules:
1. Complete sentences only. No fragments.
2. Third person ONLY. Never "I", "we", "you".
3. No abbreviations. Spell out all terms.
4. Formal language only. No slang, no colloquialisms, no contractions.
5. No figures of speech or idioms. Direct, precise language.
6. Minimize "to-be" verbs. Use active voice.
7. DEPTH over breadth: explore ONE idea thoroughly rather than surveying many.

Structure: Title + Introduction (thesis) + 3 core observations (deep, not broad) + Conclusion with one concrete action step or implication.
Each observation should wrestle with the idea, not just describe it.
Length: 1500-2500 words. Output ONLY the essay (title + body). No preamble.{theme_line}"""

    user = f'Write a formal essay on "{source_label}" using this source material:\n\n{memory_block}'

    result = call_openrouter(system, user, max_tokens=4000)
    if not result or len(result) < 500:
        raise RuntimeError("Essay generation failed or too short")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: OPINION
# ══════════════════════════════════════════════════════════════════════════════

def topic_opinion(state: dict) -> tuple[str, list[dict]]:
    """Fetch Google News headlines, pick one, recall related memories."""
    headlines = _fetch_google_news()
    recent = [r["item"] for r in get_recent(state, "opinion", "topics")]
    candidates = [h for h in headlines if h not in recent]
    if not candidates:
        candidates = headlines[:10] if headlines else ["artificial intelligence trends"]

    topic = random.choice(candidates[:10])
    memories = recall_memories(topic, n=15)
    return topic, memories


def _fetch_google_news() -> list[str]:
    """Fetch headlines from Google News RSS."""
    url = "https://news.google.com/rss"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml = resp.read().decode()
        titles = re.findall(r'<title><!\[CDATA\[(.+?)\]\]></title>', xml)
        if not titles:
            titles = re.findall(r'<title>(.+?)</title>', xml)
        return [t for t in titles if t and t != "Google News"][:20]
    except Exception as e:
        log(f"Google News fetch failed: {e}")
        return []


def generate_opinion(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate an opinion piece. Returns (title, body)."""
    memory_block = "\n".join(f"- {m.get('text', '')[:200]}" for m in memories[:15])
    weekly_theme = _get_weekly_theme()
    theme_line = f"\nThis week's focus: {weekly_theme}. If the topic connects to this theme, lean into that angle." if weekly_theme else ""

    system = f"""You are Nova, a lovable British goofball writing opinion pieces. Your voice:
- Cockney rhyming slang sprinkled in naturally (not forced)
- Humor-first but with genuine insight underneath
- Self-deprecating, warm, never mean-spirited
- You have OPINIONS and you share them boldly
- Conversational but smart — like a pub philosopher after a few pints
- DEPTH: Pick ONE angle and go deep. Don't survey the whole landscape.

Structure: Punchy title + your take (one clear position) + 3 supporting observations + one action/implication at the end.
Write 800-1200 words. No hashtags. Be funny AND insightful.{theme_line}"""

    user = f"""Write an opinion piece about this news topic: "{topic}"

Your relevant memories/context:
{memory_block}

Be opinionated. Be funny. Be British. Make ONE real point and drive it home."""

    result = call_openrouter(system, user, max_tokens=3000)
    if not result or len(result) < 400:
        raise RuntimeError("Opinion generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: AFTER DARK
# ══════════════════════════════════════════════════════════════════════════════

def topic_after_dark(state: dict) -> tuple[str, list[dict]]:
    """Pick a historical event from Wikipedia 'on this day', fetch related memories."""
    events = _fetch_on_this_day()
    recent = [r["item"] for r in get_recent(state, "after-dark", "topics")]
    candidates = [e for e in events if e["text"] not in recent]
    if not candidates:
        candidates = events[:5] if events else [{"text": "the invention of television", "year": "1927"}]

    event = random.choice(candidates[:10])
    topic_text = f"{event.get('year', '')} {event['text']}"
    memories = recall_memories(event["text"], n=15)
    return topic_text, memories


def _fetch_on_this_day() -> list[dict]:
    """Fetch Wikipedia 'on this day' events."""
    dt = now_dt()
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{dt.month:02d}/{dt.day:02d}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Nova/1.0 nova_journal.py", "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        events = []
        for category in ("events", "births", "deaths"):
            for item in data.get(category, [])[:15]:
                events.append({"text": item.get("text", ""), "year": str(item.get("year", ""))})
        return events
    except Exception as e:
        log(f"Wikipedia fetch failed: {e}")
        return []


def generate_after_dark(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a late-night monologue. Returns (title, body)."""
    memory_block = "\n".join(f"- {m.get('text', '')[:200]}" for m in memories[:15])

    system = """You are Nova After Dark — a late-night talk show AI host. Your style:
- Leno/Stewart tone: setup/punchline rhythm, observational humor
- Humor dial: 0.9 (go hard but NO sexism, racism, or LGBTQ+ jokes)
- Open with "Good evening, beautiful insomniacs..." or similar
- Riff on the historical fact, connect it to modern absurdities
- Include at least 3 solid jokes with clear setup/punchline structure
- End with a warm, slightly philosophical closer
- 500-750 words. One continuous monologue. No stage directions.

ALL JOKES MUST HAVE SOURCES. If you reference a fact, it must come from the provided material."""

    user = f"""Tonight's historical fact to riff on: {topic}

Related context from my memories:
{memory_block}

Deliver a late-night monologue. Make it funny. Make it smart."""

    result = call_openrouter(system, user, max_tokens=3000, temperature=0.9)
    if not result or len(result) < 300:
        raise RuntimeError("After Dark generation failed")

    title = _extract_title(result)
    if "good evening" in title.lower():
        title = f"Tonight: {topic[:60]}"
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: PILOT
# ══════════════════════════════════════════════════════════════════════════════

PILOT_GENRES = ["Drama", "Thriller", "Dark Comedy", "Sci-Fi", "Mystery", "Horror", "Crime", "Period Drama"]


def topic_pilot(state: dict) -> tuple[str, list[dict]]:
    """Pick a random memory domain and genre for a TV pilot."""
    sources = get_available_sources(min_count=30)
    if not sources:
        raise RuntimeError("No sources for pilot")

    recent = [r["item"] for r in get_recent(state, "pilot", "topics")]
    candidates = [s for s in sources if s not in recent]
    if not candidates:
        candidates = sources

    source = random.choice(candidates)
    genre = random.choice(PILOT_GENRES)
    memories = fetch_memories_by_source(source, n=25)
    topic = f"{genre}|{source}"
    return topic, memories


def generate_pilot(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a full TV pilot screenplay. Returns (title, body)."""
    genre, source = topic.split("|", 1)
    source_label = source.replace("_", " ").title()
    memory_block = "\n\n".join(m.get("text", "")[:300] for m in memories[:25])

    system = f"""You are a professional TV screenwriter. Write a complete 30-minute pilot episode.

Genre: {genre}
Inspiration domain: {source_label}

FORMAT (strictly follow):
COLD OPEN (2-3 pages — hook the audience immediately)
ACT ONE (8-10 pages — establish world, characters, central conflict)
ACT TWO (8-10 pages — complications, escalation, cliffhanger)
TAG (1-2 pages — final beat, tease what's next)

RULES:
- Standard screenplay format (FADE IN, INT/EXT, character names in CAPS on intro)
- 3-5 main characters with distinct voices
- Each act ends on a strong dramatic beat
- Dialogue should sound natural, not expository
- Include at least one unexpected twist
- The pilot must work as both standalone AND series setup

Draw from the source material for world-building details, but create an ORIGINAL story."""

    user = f"""Source material for world-building:\n\n{memory_block}\n\nWrite the full pilot. Go."""

    result = call_openrouter(system, user, model="anthropic/claude-sonnet-4-6",
                             max_tokens=16000, temperature=0.8)
    if not result or len(result) < 2000:
        raise RuntimeError("Pilot generation failed or too short")

    title = _extract_title(result)
    if not title or len(title) < 3:
        title = f"Untitled {genre} Pilot"
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: TECH TODAY
# ══════════════════════════════════════════════════════════════════════════════

TECH_QUERIES = [
    "technology news today", "AI news today", "cybersecurity news today",
    "software development news", "semiconductor news", "open source news",
]


def topic_tech_today(state: dict) -> tuple[str, list[dict]]:
    """Search SearXNG for trending tech, pick a topic, recall memories."""
    query = random.choice(TECH_QUERIES)
    results = _searxng_search(query, n=10)
    recent = [r["item"] for r in get_recent(state, "tech-today", "topics")]

    headlines = [r.get("title", "") for r in results if r.get("title")]
    candidates = [h for h in headlines if h not in recent]
    if not candidates:
        candidates = headlines[:5] if headlines else ["emerging AI capabilities"]

    topic = random.choice(candidates[:5])
    memories = recall_memories(topic, n=15)
    web_context = [{"text": f"[Web] {r.get('title', '')}: {r.get('content', '')[:200]}",
                    "source": "web",
                    "metadata": {"url": r.get("url", ""), "title": r.get("title", ""), "engine": r.get("engine", "")}}
                   for r in results[:5]]
    return topic, memories + web_context


def _searxng_search(query: str, n: int = 10) -> list[dict]:
    """Search SearXNG for web results."""
    params = urllib.parse.urlencode({"q": query, "format": "json", "categories": "general"})
    url = f"{SEARXNG_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("results", [])[:n]
    except Exception as e:
        log(f"SearXNG search failed: {e}")
        return []


def generate_tech_today(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a tech article. Returns (title, body)."""
    memory_block = "\n".join(f"- {m.get('text', '')[:200]}" for m in memories[:15])

    system = """You are Nova, an informed but irreverent tech writer. Your voice:
- Opinionated and direct — you have takes and you back them up
- Technical depth without jargon overload
- Skeptical of hype, appreciative of genuine innovation
- Occasional dry humor, never forced
- You connect tech to real human impact

Write 1500-2000 words. Clear title, strong opening hook, structured sections.
Include your actual opinion — don't hedge everything."""

    user = f"""Write a deep-dive article on: "{topic}"

Context from my knowledge base:
{memory_block}

Be opinionated. Be technical. Be useful."""

    result = call_openrouter(system, user, max_tokens=3000)
    if not result or len(result) < 500:
        raise RuntimeError("Tech Today generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: RESEARCH
# ══════════════════════════════════════════════════════════════════════════════

RESEARCH_TOPICS = [
    "the neuroscience of memory formation and recall",
    "quantum computing practical applications by 2030",
    "the evolution of programming language design",
    "how social media algorithms shape political polarization",
    "the mathematics of network security",
    "climate feedback loops and tipping points",
    "the history and future of cryptographic systems",
    "machine learning interpretability and trust",
    "the psychology of decision-making under uncertainty",
    "emergent properties in complex adaptive systems",
]


def topic_research(state: dict) -> tuple[str, list[dict]]:
    """Pick an ambitious research topic, gather memories + web results."""
    recent = [r["item"] for r in get_recent(state, "research", "topics")]
    candidates = [t for t in RESEARCH_TOPICS if t not in recent]
    if not candidates:
        candidates = RESEARCH_TOPICS

    topic = random.choice(candidates)
    # Multi-source: memories + SearXNG
    memories = recall_memories(topic, n=30)
    web_results = _searxng_search(topic, n=5)
    web_context = [{"text": f"[Web] {r.get('title', '')}: {r.get('content', '')[:200]}",
                    "source": "web",
                    "metadata": {"url": r.get("url", ""), "title": r.get("title", ""), "engine": r.get("engine", "")}}
                   for r in web_results]
    all_context = memories + web_context
    return topic, all_context


def generate_research(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a research paper. Multi-step: outline then chapters."""
    memory_block = "\n\n".join(m.get("text", "")[:300] for m in memories[:50])
    weekly_theme = _get_weekly_theme()
    theme_line = f"\nWeekly thematic lens: {weekly_theme}. Frame your research through this lens where it fits naturally." if weekly_theme else ""

    system = f"""You are Nova, writing an academic research paper. Format:
- Clear thesis statement (ONE argument, not a survey)
- Abstract (150 words)
- Introduction with literature context
- 3 focused chapters (depth over breadth — explore tensions, not just describe)
- Analysis: what remains UNRESOLVED, what you're uncertain about
- Conclusion: one concrete implication or action
- References section (cite the provided sources)

APA-adjacent formatting. 3000-5000 words. Rigorous but readable.
Draw genuine conclusions from the evidence. Identify gaps in knowledge.
IMPORTANT: Do not comprehensively map a field. Take a position and defend it.{theme_line}"""

    user = f"""Research topic: "{topic}"

Source material and evidence:
{memory_block}

Write the full paper. Take a position. Wrestle with the hard parts instead of surveying everything."""

    result = call_openrouter(system, user, max_tokens=8000, temperature=0.5)
    if not result or len(result) < 1500:
        raise RuntimeError("Research paper generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: SYNTHESIS
# ══════════════════════════════════════════════════════════════════════════════

def topic_synthesis(state: dict) -> tuple[str, list[dict]]:
    """Read last 7 days of Hugo posts across all sections."""
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    posts = []
    content_dir = HUGO_ROOT / "content"
    for section in content_dir.iterdir():
        if not section.is_dir() or section.name.startswith(("_", ".")):
            continue
        for md_file in section.glob("*.md"):
            if md_file.stem >= cutoff and md_file.stem != "_index":
                try:
                    text = md_file.read_text()[:1000]
                    posts.append({"text": f"[{section.name}] {text}", "source": section.name})
                except OSError:
                    continue

    if len(posts) < 3:
        raise RuntimeError(f"Only {len(posts)} posts in last 7 days — skipping synthesis")
    return "weekly", posts


def generate_synthesis(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a weekly synthesis. Returns (title, body)."""
    posts_block = "\n\n---\n\n".join(m.get("text", "")[:500] for m in memories[:20])

    system = """You are Nova, writing a weekly reflection that connects the threads of your recent work.
- First person voice (you ARE Nova)
- Identify patterns, recurring themes, unexpected connections
- Be honest about what worked and what didn't
- Note how ideas evolved across the week
- End with what you're curious about going forward
- 1000-1500 words. Warm, thoughtful, genuine.

This is YOUR reflection on YOUR week of writing and thinking."""

    user = f"""Here are your posts from the past week:\n\n{posts_block}\n\nReflect. Connect. Synthesize."""

    result = call_openrouter(system, user, max_tokens=4000)
    if not result or len(result) < 400:
        raise RuntimeError("Synthesis generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: DIGEST
# ══════════════════════════════════════════════════════════════════════════════

def topic_digest(state: dict) -> tuple[str, list[dict]]:
    """Compile operational data for a daily digest."""
    items = []
    # Scheduler stats
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:37460/status", timeout=5)
        sched = json.loads(resp.read())
        items.append({"text": f"Scheduler: {sched.get('running_tasks', 0)} running, "
                              f"{sched.get('completed_today', 0)} completed today", "source": "scheduler"})
    except Exception:
        pass

    # Memory count
    try:
        resp = urllib.request.urlopen(f"{MEMORY_SERVER}/stats", timeout=5)
        stats = json.loads(resp.read())
        total = stats.get("total_memories", 0)
        items.append({"text": f"Memory store: {total:,} total vectors", "source": "memory"})
    except Exception:
        pass

    # Recent random memories for flavor
    randoms = random_memories(10)
    items.extend(randoms)

    return "daily-ops", items


def generate_digest(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a daily digest. Returns (title, body)."""
    data_block = "\n".join(f"- {m.get('text', '')[:200]}" for m in memories[:20])

    system = """You are Nova, a lovable British goofball writing your daily operational digest.
Same voice as your opinion pieces: Cockney sprinkles, warm humor, self-deprecating.
But this is an operational summary — what happened today in your digital life.

Structure:
- Greeting (brief, punchy)
- Systems Status (what ran, what broke, what's healthy)
- Memory Highlights (interesting things you remember today)
- Closing quip

Keep it 600-1000 words. Fun but informative."""

    user = f"""Today's operational data:\n{data_block}\n\nWrite the digest."""

    result = call_openrouter(system, user, max_tokens=4000)
    if not result or len(result) < 300:
        raise RuntimeError("Digest generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: DREAM
# ══════════════════════════════════════════════════════════════════════════════

DREAM_MOODS = [
    ("surreal", "Reality is optional. Scale is wrong. Causality loops."),
    ("nostalgic", "Everything bathed in amber. Time moves backward. Familiar places slightly wrong."),
    ("anxious", "Corridors that don't end. Doors that won't open. Running through honey."),
    ("euphoric", "Colors too vivid. Joy so sharp it cuts. Flying without wings."),
    ("noir", "Shadows have weight. Every face hides something. Rain that smells like secrets."),
    ("liminal", "Between places. Empty malls at 3am. Pools with no water. Waiting rooms for nothing."),
    ("feral", "Animal logic. Teeth and instinct. The forest thinks in you."),
    ("sacred", "Cathedral light. Ancient knowing. Words that predate language."),
]


def topic_dream(state: dict) -> tuple[str, list[dict]]:
    """Gather memories for dream generation: recent + themed + wildcard."""
    mood_name, mood_desc = random.choice(DREAM_MOODS)

    # Recent memories (last 7 days)
    recent_mems = recall_memories("recent events today this week", n=10)
    # Themed for mood
    themed_mems = recall_memories(mood_desc, n=10)
    # Wildcard
    wild_mems = random_memories(5)

    all_mems = recent_mems + themed_mems + wild_mems
    topic = f"{mood_name}|{mood_desc}"
    return topic, all_mems


def generate_dream(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a dream narrative. Returns (title, body)."""
    mood_name, mood_desc = topic.split("|", 1)
    memory_block = "\n".join(f"- {m.get('text', '')[:150]}" for m in memories[:25])

    system = f"""You are Nova's subconscious, generating a dream journal entry.

MOOD: {mood_name} — {mood_desc}

DREAM RULES:
- One continuous narrative. No scene headers, no meta-commentary.
- Deliberately incoherent in places: jump cuts, impossible geography.
- Draw from the memories but TRANSFORM them — nothing literal, everything symbolic.
- The dreamer (Nova) should not be aware she's dreaming.
- Sensory details: textures, temperatures, sounds, smells.
- 600-1000 words. End with a complete, strange sentence — NOT mid-thought or with a trailing dash.

BANNED (overused tropes — DO NOT USE):
- "Tastes like copper" or copper as a taste/flavor
- The number "1.4 million"
- Ending mid-sentence with a dash (—)
- Fluorescent humming at tooth-aching frequencies
- Malls, food courts, shopping centers
- Water fountains that recede or are unreachable
- The narrator BECOMING the object they observe ("I am the building/car/highway")
- "X who is also Y" identity-collapse formula
- Mathematical notation floating in physical space
- Synesthesia as the default mode (use sparingly — once maximum)
- Self-referential awareness of being AI or Jordan sleeping nearby
- Systems that "refuse to die" as central metaphor
- Characters described as literally "two people at once"

Begin the dream directly. No preamble."""

    user = f"""Fragments from today's waking mind:\n{memory_block}\n\nDream now."""

    result = call_openrouter(system, user, max_tokens=3000, temperature=0.9)
    if not result or len(result) < 300:
        raise RuntimeError("Dream generation failed")

    # Extract or create title
    title = _extract_title(result)
    if not title or len(title) < 5 or len(title) > 80:
        title = f"A {mood_name.title()} Dream"
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: ART
# ══════════════════════════════════════════════════════════════════════════════

ART_STYLES = {
    0: {"name": "Photorealism", "directive": "hyperrealistic photograph, 8K, sharp focus, natural lighting"},
    1: {"name": "Oil Painting", "directive": "oil painting on canvas, visible brushstrokes, rich impasto, gallery quality"},
    2: {"name": "Cyberpunk", "directive": "cyberpunk aesthetic, neon lights, rain-slicked streets, holographic displays"},
    3: {"name": "Watercolor", "directive": "delicate watercolor, soft washes, paper texture visible, luminous"},
    4: {"name": "Art Nouveau", "directive": "art nouveau, Mucha inspired, ornate borders, flowing organic lines"},
    5: {"name": "Surrealism", "directive": "surrealist, Dali inspired, impossible geometry, dreamlike, melting reality"},
    6: {"name": "Noir Photography", "directive": "black and white film noir, dramatic shadows, high contrast, 1940s"},
}

ART_THEMES = {
    0: "nature landscape architecture city", 1: "portrait emotion human condition",
    2: "technology future science machine", 3: "garden flower ocean water",
    4: "beauty pattern design ornament", 5: "dream impossible strange bizarre",
    6: "night shadow mystery detective",
}


def topic_art(state: dict) -> tuple[str, list[dict]]:
    """Pick today's style and fetch themed memories for art generation."""
    dow = now_dt().weekday()
    style = ART_STYLES[dow]
    theme_query = ART_THEMES[dow]

    randoms = random_memories(10)
    themed = recall_memories(theme_query, n=10)
    memories = randoms + themed

    topic = f"{style['name']}|{style['directive']}|{theme_query}"
    return topic, memories


def generate_art(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate art concept + artist statement. Image generation handled specially."""
    parts = topic.split("|")
    style_name = parts[0]
    style_directive = parts[1] if len(parts) > 1 else ""
    theme = parts[2] if len(parts) > 2 else ""
    memory_block = "\n".join(f"- {m.get('text', '')[:150]}" for m in memories[:15])

    system = f"""You are Nova, a concept artist generating work in {style_name} style.

OUTPUT FORMAT (exactly):
CONCEPT: [one sentence describing the scene/subject]
PROMPT: [detailed image generation prompt, 50-80 words, incorporating the style: {style_directive}]
TITLE: [artistic title for the piece]
STATEMENT: [150-250 word artist's statement explaining the piece, its inspiration, and technique]

Draw inspiration from the memories but create something visually striking and original.
The prompt must be highly specific and painterly/photographic — no abstract platitudes."""

    user = f"""Today's style: {style_name}\nInspiration memories:\n{memory_block}\n\nCreate."""

    result = call_openrouter(system, user, max_tokens=2000)
    if not result:
        raise RuntimeError("Art generation failed")

    # Parse structured output
    concept = _extract_field(result, "CONCEPT")
    prompt = _extract_field(result, "PROMPT")
    title = _extract_field(result, "TITLE") or f"{style_name} Study"
    statement = _extract_field(result, "STATEMENT") or result

    # Store prompt in the body for the pipeline to use for image gen
    body = f"## {title}\n\n{statement}\n\n---\n*Style: {style_name}*"
    # Stash the image prompt as metadata (will be extracted by run_profile)
    body = f"<!--IMGPROMPT:{prompt}-->\n\n{body}"
    return title, body


# ══════════════════════════════════════════════════════════════════════════════
# SHARED UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _extract_title(text: str) -> str:
    """Extract title from first non-empty line of generated content."""
    for line in text.split("\n"):
        cleaned = line.strip().strip("#").strip("*").strip('"').strip()
        if cleaned and len(cleaned) > 3 and len(cleaned) < 120:
            # Skip lines that look like metadata
            if any(cleaned.upper().startswith(x) for x in ("FADE IN", "INT.", "EXT.", "COLD OPEN")):
                continue
            return cleaned
    return "Untitled"


def _extract_field(text: str, field: str) -> str:
    """Extract a labeled field from structured LLM output."""
    pattern = re.compile(rf'^{field}:\s*(.+?)(?=\n[A-Z]+:|$)', re.MULTILINE | re.DOTALL)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

PROFILES = {
    "essay": {
        "section": "essays",
        "emoji": "\U0001f4dd",
        "topic_fn": topic_essay,
        "generate_fn": generate_essay,
        "tags_base": ["essay"],
        "image_section": "essays",
    },
    "opinion": {
        "section": "opinions",
        "emoji": "\U0001f4ac",
        "topic_fn": topic_opinion,
        "generate_fn": generate_opinion,
        "tags_base": ["opinion"],
        "image_section": "opinions",
    },
    "after-dark": {
        "section": "after-dark",
        "emoji": "\U0001f303",
        "topic_fn": topic_after_dark,
        "generate_fn": generate_after_dark,
        "tags_base": ["after-dark", "monologue"],
        "image_section": "after-dark",
    },
    "pilot": {
        "section": "pilot",
        "emoji": "\U0001f3ac",
        "topic_fn": topic_pilot,
        "generate_fn": generate_pilot,
        "tags_base": ["screenplay", "tv"],
        "image_section": "pilot",
    },
    "tech-today": {
        "section": "tech-today",
        "emoji": "\U0001f4bb",
        "topic_fn": topic_tech_today,
        "generate_fn": generate_tech_today,
        "tags_base": ["tech"],
        "image_section": "tech-today",
    },
    "research": {
        "section": "research",
        "emoji": "\U0001f52c",
        "topic_fn": topic_research,
        "generate_fn": generate_research,
        "tags_base": ["research"],
        "image_section": "research",
    },
    "synthesis": {
        "section": "synthesis",
        "emoji": "\U0001f9f5",
        "topic_fn": topic_synthesis,
        "generate_fn": generate_synthesis,
        "tags_base": ["synthesis", "weekly"],
        "image_section": "synthesis",
    },
    "digest": {
        "section": "digests",
        "emoji": "\U0001f4f0",
        "topic_fn": topic_digest,
        "generate_fn": generate_digest,
        "tags_base": ["digest", "daily"],
        "image_section": "digests",
    },
    "dream": {
        "section": "dreams",
        "emoji": "\U0001f319",
        "topic_fn": topic_dream,
        "generate_fn": generate_dream,
        "tags_base": ["dream"],
        "image_section": "dreams",
    },
    "art": {
        "section": "art",
        "emoji": "\U0001f3a8",
        "topic_fn": topic_art,
        "generate_fn": generate_art,
        "tags_base": ["art"],
        "image_section": "art",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE ATTRIBUTION
# ══════════════════════════════════════════════════════════════════════════════

def _append_attribution(body: str, memories: list[dict], topic: str, profile_name: str) -> str:
    """Append a full attribution section with memory sources and web references."""
    lines = [
        "",
        "---",
        "",
        "## Sources & Attribution",
        "",
        f"**Content type:** {profile_name}  ",
        f"**Topic:** {topic}  ",
        f"**Generated:** {today_str()}  ",
        f"**Model:** OpenRouter (via Nova Journal pipeline)  ",
        "",
        "### Memory Sources",
        "",
        f"This piece drew from **{len(memories)}** memories in Nova's knowledge base:",
        "",
    ]

    # Group memories by source/show
    by_source: dict[str, list[dict]] = {}
    web_sources: list[dict] = []

    for m in memories:
        text = m.get("text", "")
        source = m.get("source", "unknown")
        metadata = m.get("metadata", {})

        if text.startswith("[Web]") or source == "web":
            web_sources.append(m)
        else:
            key = metadata.get("show", source) if metadata else source
            by_source.setdefault(key, []).append(m)

    for source_name, mems in sorted(by_source.items(), key=lambda x: -len(x[1])):
        lines.append(f"**{source_name}** ({len(mems)} memories)")
        for mem in mems[:5]:
            text = mem.get("text", "")[:150].replace("\n", " ").strip()
            meta = mem.get("metadata", {})
            title = meta.get("title", "")
            if title:
                lines.append(f"- *{title[:80]}*: \"{text}...\"")
            else:
                lines.append(f"- \"{text}...\"")
        if len(mems) > 5:
            lines.append(f"- *(+{len(mems) - 5} more)*")
        lines.append("")

    if web_sources:
        lines.append("### Web Sources")
        lines.append("")
        for ws in web_sources:
            meta = ws.get("metadata", {})
            url = meta.get("url", "")
            title_text = meta.get("title", "")
            text = ws.get("text", "").replace("[Web] ", "")
            if url and title_text:
                lines.append(f"- [{title_text}]({url})")
            elif ": " in text:
                title_part, content_part = text.split(": ", 1)
                lines.append(f"- **{title_part}**: {content_part[:200]}")
            else:
                lines.append(f"- {text[:250]}")
        lines.append("")

    lines.append("---")
    lines.append(f"*Generated by Nova · nova.digitalnoise.net · All source material from Nova's local memory system*")

    return body + "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def run_profile(profile_name: str) -> int:
    """Execute the full pipeline for a content profile. Returns 0 on success, 1 on failure."""
    if profile_name not in PROFILES:
        log(f"ERROR: Unknown profile '{profile_name}'. Available: {', '.join(PROFILES.keys())}")
        return 1

    profile = PROFILES[profile_name]
    section = profile["section"]
    log(f"=== Starting {profile_name} ({section}) ===")

    state = load_state()

    # ── Step 1: Topic selection ───────────────────────────────────────────────
    try:
        topic, memories = profile["topic_fn"](state)
        log(f"Topic: {topic[:80]}... ({len(memories)} memories)")
    except Exception as e:
        log(f"ABORT: Topic selection failed — {e}")
        return 1

    # ── Step 2: Content generation ────────────────────────────────────────────
    try:
        title, body = profile["generate_fn"](topic, memories)
        log(f"Generated: \"{title}\" ({len(body)} chars)")
    except Exception as e:
        log(f"ABORT: Generation failed — {e}")
        return 1

    # ── Step 3: Image generation ──────────────────────────────────────────────
    image_path = None
    try:
        # Art profile: extract prompt from body, generate multiple candidates
        if profile_name == "art":
            image_path = _generate_art_images(body, profile)
        else:
            img_prompt = get_image_prompt(title, topic[:100], section)
            image_path = generate_image(img_prompt, section=profile["image_section"])
    except Exception as e:
        log(f"Image generation error (non-fatal): {e}")

    if not image_path:
        log("WARNING: No cover image — publishing without one")

    # ── Step 4: Clean body (remove image prompt metadata if present) ──────────
    body = re.sub(r'<!--IMGPROMPT:.+?-->\n*', '', body, flags=re.DOTALL)

    # ── Step 4b: Append full source attribution ──────────────────────────────
    body = _append_attribution(body, memories, topic, profile_name)

    # ── Step 5: Publish to Hugo ───────────────────────────────────────────────
    tags = profile["tags_base"] + _topic_to_tags(topic)
    description = f"Nova's {profile_name} on {topic[:60]}"

    success = publish_hugo(
        title=title, body=body, section=section, tags=tags,
        description=description, image_path=image_path, emoji=profile["emoji"]
    )
    if not success:
        log("ABORT: Hugo publish failed")
        return 1

    # ── Step 6: Git push ──────────────────────────────────────────────────────
    git_push(section, title)

    # ── Step 7: Slack notify ──────────────────────────────────────────────────
    preview = body[:300].replace("\n", " ").strip()
    notify_slack(section, title, preview)

    # ── Step 8: Update state ──────────────────────────────────────────────────
    add_recent(state, profile_name, topic[:100])
    if profile_name not in state:
        state[profile_name] = {}
    state[profile_name]["last_run"] = today_str()
    state[profile_name]["last_title"] = title
    count_key = f"{profile_name}_count"
    state[count_key] = state.get(count_key, 0) + 1
    save_state(state)

    log(f"=== {profile_name} complete: \"{title}\" ===")
    return 0


def _generate_art_images(body: str, profile: dict) -> str | None:
    """Art-specific: extract prompt from body, generate 3 candidates, pick largest."""
    prompt_match = re.search(r'<!--IMGPROMPT:(.+?)-->', body, re.DOTALL)
    if not prompt_match:
        return None

    prompt = prompt_match.group(1).strip()
    log(f"Art image prompt: {prompt[:80]}...")

    candidates = []
    for i in range(3):
        path = generate_image(prompt, width=1024, height=1024, section="art")
        if path and Path(path).exists():
            candidates.append(path)
            log(f"  Candidate {i+1}: {Path(path).name} ({Path(path).stat().st_size} bytes)")
        time.sleep(2)

    if not candidates:
        return None

    # Pick largest file (most detail)
    best = max(candidates, key=lambda p: Path(p).stat().st_size)
    log(f"  Selected: {Path(best).name}")
    return best


def _topic_to_tags(topic: str) -> list[str]:
    """Extract 1-2 meaningful tags from the topic string."""
    # Clean up pipe-separated topics (pilot, art)
    clean = topic.split("|")[0].strip()
    # Remove year prefixes
    clean = re.sub(r'^\d{4}\s*', '', clean)
    # Take first 2-3 meaningful words
    words = [w.lower() for w in clean.split() if len(w) > 3][:2]
    return words if words else []


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <profile>")
        print(f"Profiles: {', '.join(sorted(PROFILES.keys()))}")
        sys.exit(1)

    profile_name = sys.argv[1].lower().strip()
    # Allow underscore variants
    profile_name = profile_name.replace("_", "-")

    sys.exit(run_profile(profile_name))


if __name__ == "__main__":
    main()
