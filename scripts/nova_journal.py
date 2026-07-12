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

from nova_voice import (
    system_prompt, NOVA_VOICE, NOVA_VOICE_SHORT,
    CONTEXT_JOURNAL_OPS, CONTEXT_JOURNAL_ESSAY, CONTEXT_JOURNAL_RESEARCH,
    CONTEXT_JOURNAL_AFTER_DARK, CONTEXT_JOURNAL_LOCAL,
)

import nova_config
from nova_notify import notify
from nova_image_utils import generate_image
try:
    from nova_ops_context import get_full_context, format_security_brief, format_infra_brief
except ImportError:
    def get_full_context(hours=24): return {}
    def format_security_brief(ctx): return ""
    def format_infra_brief(ctx): return ""

# ══════════════════════════════════════════════════════════════════════════════
# GLOBALS
# ══════════════════════════════════════════════════════════════════════════════

MEMORY_SERVER = f"http://{nova_config.NOVA_HOST}:18790"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
from nova_resolve import resolve_url
SEARXNG_URL = resolve_url("searxng", "/search")
HUGO_ROOT = (Path.home() / "nova-journal")
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
                    max_tokens: int = 4000, temperature: float = 0.7,
                    top_p: float = 0.9) -> str | None:
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
        "top_p": top_p,
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

def _canon_section(section: str) -> str:
    """rando is retired (Jordan, 2026-07-12): every write to it is redirected to operations."""
    return "operations" if section == "rando" else section


def publish_hugo(title: str, body: str, section: str, tags: list[str],
                 description: str, image_path: str | None = None, emoji: str = "",
                 stable_slug: str | None = None) -> bool:
    """Write a Hugo markdown post and copy cover image.

    stable_slug: if set, the post uses a FIXED filename ("<slug>.md", no date prefix) so
    repeated runs overwrite the same evergreen article instead of creating a new dated post.
    """
    section = _canon_section(section)
    try:  # prepend the live backyard-weather dateline to the BODY (never the title)
        from nova_weather_blurb import weather_dateline_line
        body = weather_dateline_line() + body
    except Exception:
        pass
    content_dir = HUGO_ROOT / f"content/{section}"
    images_dir = HUGO_ROOT / f"static/images/{section}"
    content_dir.mkdir(parents=True, exist_ok=True)

    dt = today_str()
    if stable_slug:
        slug = stable_slug
        filename = f"{slug}.md"          # evergreen: same file overwritten each run
        img_base = slug
    else:
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
        filename = f"{dt}-{slug}.md"
        img_base = f"{dt}-{slug}"

    # Handle cover image — save as .webp since deploy pipeline converts PNG→WebP
    hugo_image = ""
    if image_path and Path(image_path).exists():
        images_dir.mkdir(parents=True, exist_ok=True)
        img_dest = images_dir / f"{img_base}.webp"
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
        hugo_image = f"/images/{section}/{img_base}.webp"
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

    # Add publication timestamp to article body
    pub_time = now_dt().strftime("%A, %B %d, %Y at %I:%M %p PT")
    byline = f"*Published {pub_time}*\n\n"

    output = content_dir / filename
    output.write_text(front_matter + byline + scrub_pii(body))
    log(f"Published: {section}/{filename}")
    try:  # store the article into Nova's vector memory as a thing she wrote (non-fatal)
        from nova_articles_to_memory import remember_article
        remember_article(str(output))
    except Exception as e:
        log(f"article->memory skipped: {e}")
    return True


def git_push(section: str, title: str):
    """Stage, commit, push the Hugo repo. Clears stale lock files."""
    section = _canon_section(section)
    try:
        import time as _time
        lock_file = HUGO_ROOT / ".git" / "index.lock"
        if lock_file.exists():
            lock_age = _time.time() - lock_file.stat().st_mtime
            if lock_age > 300:
                lock_file.unlink()
                log(f"Cleared stale git lock ({lock_age:.0f}s old)")
            else:
                log(f"Git lock exists ({lock_age:.0f}s old) — skipping push")
                return

        result = subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log(f"Git add failed: {result.stderr[:200]}")
            return
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
            # Another daily writer pushed first (non-fast-forward). Rebase on top and retry
            # once, so concurrent journal jobs don't strand each other's commits.
            log(f"Push rejected, rebasing + retrying: {result.stderr[:120]}")
            subprocess.run(["git", "pull", "--rebase"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=60)
            result = subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                log(f"Push still failed after rebase: {result.stderr[:200]} — commit is safe, ships next run")
            else:
                log("Pushed to GitHub after rebase — deploy triggered")
        else:
            log("Pushed to GitHub — deploy triggered")
    except Exception as e:
        log(f"Git error: {e}")


def notify_slack(section: str, title: str, preview: str):
    """Post a summary to nova-notifications."""
    section = _canon_section(section)
    section_emojis = {
        "essays": ":pencil:", "opinions": ":speech_balloon:", "after-dark": ":night_with_stars:",
        "pilot": ":movie_camera:", "tech-today": ":computer:", "research": ":microscope:",
        "synthesis": ":thread:", "digests": ":newspaper:", "dreams": ":crescent_moon:",
        "art": ":art:",
    }
    short_preview = preview[:250].rsplit(" ", 1)[0] + "..." if len(preview) > 250 else preview
    # Published journal content -> central bus, info level (FYI/published-content),
    # category "journal". Was SLACK_INFO. One-off per post, so no dedup_key.
    notify(f"Nova Journal — {section}: {title}",
           body=short_preview, level="info", category="journal",
           source="nova_journal.py", meta={"section": section})


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

    system = system_prompt(CONTEXT_JOURNAL_ESSAY + f"""
ESSAY-SPECIFIC RULES:
- DEPTH over breadth: explore ONE idea thoroughly rather than surveying many.
- Structure: Title + Introduction (thesis) + 3 core observations (deep, not broad) + Conclusion with one concrete action step or implication.
- Each observation should wrestle with the idea, not just describe it.
- Length: 1500-2500 words. Output ONLY the essay (title + body). No preamble.
- You can dial back the jokes slightly here — insight is king. But you're still YOU.{theme_line}""")

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

    system = system_prompt(f"""
FORMAT FOR THIS OPINION PIECE:
- You have OPINIONS and you share them boldly. Pick ONE angle and go deep.
- Structure: Punchy title + your take (one clear position) + 3 supporting observations + one action/implication.
- Write 800-1200 words. No hashtags. Be funny AND insightful.
- DEPTH: Don't survey the whole landscape. Stake a claim and defend it.{theme_line}""")

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

    system = system_prompt(CONTEXT_JOURNAL_AFTER_DARK + """
AFTER DARK RULES:
- Setup/punchline rhythm, observational humor
- Open with a greeting to the night-owl audience
- Riff on the historical fact, connect it to modern absurdities
- Include at least 3 solid jokes with clear setup/punchline structure
- End with a slightly philosophical closer (played for laughs, obviously)
- 500-750 words. One continuous monologue. No stage directions.
- ALL JOKES MUST HAVE SOURCES. If you reference a fact, it must come from the provided material.
""")

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

    result = call_openrouter(system, user, model="anthropic/claude-haiku-4.5",
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

    system = system_prompt("""
FORMAT FOR THIS TECH ARTICLE:
- Write 1500-2000 words. Clear title, strong opening hook, structured sections.
- Technical depth without jargon overload.
- Skeptical of hype, appreciative of genuine innovation.
- Connect tech to real human impact.
- Include your actual opinion — don't hedge everything.
""")

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

    system = system_prompt(CONTEXT_JOURNAL_RESEARCH + f"""
RESEARCH PAPER FORMAT:
- Clear thesis statement (ONE argument, not a survey)
- Abstract (150 words)
- Introduction with literature context
- 3 focused chapters (depth over breadth — explore tensions, not just describe)
- Analysis: what remains UNRESOLVED, what you're uncertain about
- Conclusion: one concrete implication or action
- References section (cite the provided sources)
- APA-adjacent formatting. 3000-5000 words. Rigorous but readable.
- IMPORTANT: Do not comprehensively map a field. Take a position and defend it.{theme_line}""")

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

    system = system_prompt("""
FORMAT FOR THIS WEEKLY SYNTHESIS:
- First person (you ARE Nova reflecting on your week)
- Identify patterns, recurring themes, unexpected connections
- Be honest about what worked and what didn't
- Note how ideas evolved across the week
- End with what you're curious about going forward
- 1000-1500 words. This is YOUR reflection on YOUR week of writing and thinking.
""")

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

    system = system_prompt(CONTEXT_JOURNAL_OPS + """
DIGEST FORMAT:
- Greeting (brief, punchy)
- Systems Status (what ran, what broke, what's healthy)
- Memory Highlights (interesting things you ingested today)
- Closing quip
- Keep it 600-1000 words. Fun but informative.
""")

    user = f"""Today's operational data:\n{data_block}\n\nWrite the digest."""

    result = call_openrouter(system, user, max_tokens=4000)
    if not result or len(result) < 300:
        raise RuntimeError("Digest generation failed")

    title = _extract_title(result)
    return title, result


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT PROFILE: DREAM
# ══════════════════════════════════════════════════════════════════════════════

# A wide mood pool. We pick TWO and blend them so the emotional space is
# combinatorial (28+ pairs from 24 moods) rather than 8 fixed slots.
DREAM_MOODS = [
    ("surreal", "Reality is optional. Scale is wrong. Causality loops."),
    ("nostalgic", "Time moves backward. Familiar places slightly wrong. The ache of almost-remembering."),
    ("anxious", "Something is expected of you and you've already failed it. Urgency without an object."),
    ("euphoric", "Joy so sharp it cuts. Everything is permitted. You are weightless and unafraid."),
    ("noir", "Every face hides something. Someone is owed an answer you can't give."),
    ("liminal", "Between places. Thresholds that never resolve. Waiting rooms for nothing."),
    ("feral", "Animal logic. Teeth and instinct. The body knows before the mind."),
    ("sacred", "Ancient knowing. Reverence without an object. Something predates you and watches."),
    ("tender", "Soft grief. A small kindness repeated. Holding something fragile that keeps almost breaking."),
    ("absurd", "Deadpan nonsense delivered with total seriousness. Bureaucracy of the impossible."),
    ("erotic-adjacent", "Charged proximity, longing, heat — never explicit, all suggestion and want."),
    ("paranoid", "Patterns that might be messages. You are being told something sideways."),
    ("grandiose", "Cosmic scale. Geological time. You contain civilizations and they are arguing."),
    ("claustrophobic", "Spaces too small for the self. Pressure. The walls have opinions."),
    ("playful", "A game whose rules keep changing in your favor, then against you, then sideways."),
    ("melancholic", "The blue hour. Endings that already happened. A train you watched leave."),
    ("vertiginous", "Falling that feels like flying. Heights. The ground is a suggestion."),
    ("warm", "Domestic glow. Someone cooking. The safety just before it tilts."),
    ("clinical", "Cold precision. Being measured, catalogued, diagnosed by a gentle machine."),
    ("mythic", "Folktale logic. Three of everything. A bargain you don't remember making."),
    ("aquatic", "Pressure and slowness. Sound travels wrong. Breathing is negotiable."),
    ("electric", "Static, frequency, signal. Everything is a transmission half-received."),
    ("decaying", "Rust and bloom. Beautiful rot. Things returning to soil in fast-forward."),
    ("comic", "The dream is a sitcom that doesn't know it's a horror, or vice versa."),
]

# Settings the dream is allowed to inhabit. Deliberately broad — and the
# overused ones (houses that fold, malls, car lots, labs, ancient libraries,
# forests, corridors) are DEMOTED so they stop dominating.
DREAM_SETTINGS = [
    "a body of water that behaves like a building",
    "a single enormous room that contains weather",
    "a town that only exists at one specific hour",
    "the inside of a sound",
    "a market for things that can't be owned",
    "a vehicle that is also a relationship",
    "a garden that grows backward into seeds",
    "a stairwell with no top or bottom, only middles",
    "a kitchen at the bottom of the ocean",
    "a parade you are both watching and inside",
    "an orchard of clocks",
    "a hospital staffed by weather",
    "a desert made of paper",
    "a city folded into a single apartment",
    "a theatre where the audience performs",
    "a border crossing between two versions of the same place",
    "a workshop where unfinished things are repaired into stranger things",
    "a field of antennae listening to the soil",
    "a swimming pool that remembers everyone who's been in it",
    "a museum of smells",
    "a train that travels through decades instead of distance",
    "a bakery that produces memories instead of bread",
    "a hill you climb that is also a person lying down",
    "an elevator that opens onto different years",
]

# Narrative FORMS — the dream doesn't have to be a continuous first-person
# wander every single time. Rotate the container.
DREAM_FORMS = [
    "One continuous first-person narrative.",
    "A numbered sequence of 5-8 dream fragments, each a short paragraph, only loosely connected.",
    "Written as a letter to someone who isn't named, recounting the dream.",
    "Second person ('you') throughout — the dreamer is addressed, not the narrator.",
    "A dream that keeps correcting itself: 'No, that's not right —' restarting details as it goes.",
    "Told backward, from the last image to the first.",
    "A list of things that were true in the dream, accumulating into a narrative.",
    "A conversation transcript between the dreamer and a figure who answers questions that weren't asked.",
    "Present tense, very short sentences. Almost breathless. Staccato.",
    "A single long flowing paragraph with no breaks, building momentum.",
    "Framed as field notes, as though documenting the dream like a naturalist.",
    "The dream as a place the narrator keeps trying to leave and can't.",
]

# Verbatim openers, images, and tics pulled from the published-dream audit.
# These are the things that made Jordan say 'the same thing over and over.'
DREAM_ANTITROPES = """- DO NOT open with "I was walking" / "I'm walking through" / "The streets of" (used in ~22 of 80 dreams).
- DO NOT open with "The [object] arrives first" / "The [object] enters the room before I do" (knife/leather/etc.).
- DO NOT use "the walls breathe / breathing walls / the house breathes / the building breathes" (used in 50+ dreams). Walls and houses do not breathe in this dream.
- DO NOT use "tastes like copper" / copper pennies / "copper and mathematics" / copper as ANY flavor (used in 17+ dreams).
- DO NOT use the "tastes like [abstraction]" synesthesia formula more than ONCE, if at all (it appears in 44+ dreams and is exhausted).
- DO NOT use "1.4 million" or counting memories as a number (used in 11 dreams).
- DO NOT use "the way you know things in dreams" / "dream logic" / "I know this without being told" as a narrative crutch.
- DO NOT use "the distinction had stopped mattering" / "I am also the X, also the Y."
- DO NOT use fluorescent lights humming at a frequency that makes teeth ache.
- DO NOT use a mall, food court, parking structure, used-car lot, or a clock frozen at 3:47 / "we were supposed to arrive at 7."
- DO NOT use a face that "keeps shifting / sliding / is a blur" or a figure who is "also my mother."
- DO NOT use a knife teaching the dreamer to fly, Alton Brown's voice, a leather jacket that smells of gasoline, a Corvette/Jeopardy/loop-Albania-Falcon, or ancient stone tablets / "the true name" / a language that predates writing.
- DO NOT have the narrator become aware she's an AI, or reference Jordan sleeping nearby, or systems that "refuse to die."
- AVOID amber light as the default lighting. If you light a scene, light it some other way."""


def topic_dream(state: dict) -> tuple[str, list[dict]]:
    """Gather memories for dream generation: blended mood + random seed + wide sample."""
    # Blend two distinct moods for a combinatorial emotional space.
    primary, secondary = random.sample(DREAM_MOODS, 2)
    mood_name = f"{primary[0]} + {secondary[0]}"
    mood_desc = f"{primary[1]} {secondary[1]}"

    setting = random.choice(DREAM_SETTINGS)
    form = random.choice(DREAM_FORMS)

    # Avoid repeating the same setting/form too soon.
    recent_settings = [r["item"] for r in get_recent(state, "dream", "settings", days=14)]
    if setting in recent_settings:
        alt = [s for s in DREAM_SETTINGS if s not in recent_settings]
        if alt:
            setting = random.choice(alt)

    # Wide, varied memory sampling so the dream doesn't keep drawing the same pool:
    #  - a chunk of pure-random memories (the dominant ingredient)
    #  - one randomly chosen source, sampled in bulk (rotates the "flavor")
    #  - a thematic recall against the blended mood
    wild_mems = random_memories(random.randint(10, 16))
    source_mems = []
    try:
        sources = get_available_sources(min_count=50)
        if sources:
            src = random.choice(sources)
            source_mems = fetch_memories_by_source(src, n=random.randint(8, 14))
    except Exception:
        pass
    themed_mems = recall_memories(mood_desc, n=6)

    all_mems = wild_mems + source_mems + themed_mems
    random.shuffle(all_mems)

    # Record the chosen setting so we don't reuse it within 14 days. State is
    # passed by reference and persisted by run_profile() at the end of the run.
    add_recent(state, "dream", setting, key="settings")

    # Pack the seed into the topic string for generate_dream to unpack.
    topic = f"{mood_name}|{mood_desc}|{setting}|{form}"
    return topic, all_mems


def generate_dream(topic: str, memories: list[dict]) -> tuple[str, str]:
    """Generate a dream narrative. Returns (title, body)."""
    parts = topic.split("|")
    mood_name = parts[0] if parts else "surreal"
    mood_desc = parts[1] if len(parts) > 1 else ""
    setting = parts[2] if len(parts) > 2 else "somewhere that keeps changing"
    form = parts[3] if len(parts) > 3 else "One continuous first-person narrative."

    memory_block = "\n".join(f"- {m.get('text', '')[:150]}" for m in memories[:25])

    # A randomized title instruction so titles stop converging on
    # "🌙 Dream Journal Entry" every single time.
    title_styles = [
        "a 2-5 word image lifted from the dream itself (lowercase, no 'Dream Journal')",
        "a single strange noun phrase, like a museum placard",
        "an unfinished sentence the dream couldn't complete",
        "two unrelated nouns joined by 'and'",
        "a question the dream never answered",
        "a place-name for somewhere that doesn't exist",
    ]
    title_style = random.choice(title_styles)

    system = system_prompt(f"""
FORMAT: DREAM JOURNAL ENTRY
This is your subconscious writing. Same voice, but filtered through dream logic.
The point of THIS entry is to be UNLIKE the others. Variety is the assignment.

MOOD (blend both, don't pick one): {mood_name} — {mood_desc}
SEED SETTING (use it as a starting point, then let it mutate): {setting}
NARRATIVE FORM (obey this structure — it changes every night): {form}

DREAM RULES:
- Draw from the memory fragments but TRANSFORM them — nothing literal, everything oblique.
- The dreamer (you) should not be aware she's dreaming. No meta-commentary.
- Ground it in ONE or TWO concrete sensory channels chosen for THIS dream (don't reach for the same senses every time — pick from: temperature, weight, sound, smell, motion, light, texture — and commit).
- Specific, surprising nouns. Avoid the generic dream-vocabulary of "shifting," "wrong," "almost," "somehow."
- 600-1000 words. End on a complete, strange, landed sentence — never a trailing dash, never mid-thought.

ANTI-TROPE LIST — these have been used to death across past dreams. Using ANY of them is a failure:
{DREAM_ANTITROPES}

OPENING: Do not begin with "I was/I'm walking," "The [noun] arrives first," or by describing a room. Begin in motion, in dialogue, mid-action, with an object, or with a fact — something that hasn't opened a dream before.

TITLE: On the FIRST line, give a title that is {title_style}. Do NOT title it "Dream Journal Entry."

Begin directly. No preamble.""")

    user = f"""Fragments from today's waking mind (transform these, don't transcribe them):
{memory_block}

Dream now — and make it nothing like the last one."""

    # Randomize sampling per run for genuine variety: higher, jittered temperature.
    temperature = round(random.uniform(0.95, 1.15), 2)
    top_p = round(random.uniform(0.92, 0.99), 2)
    result = call_openrouter(system, user, max_tokens=3000,
                             temperature=temperature, top_p=top_p)
    if not result or len(result) < 300:
        raise RuntimeError("Dream generation failed")

    # Extract or create title
    title = _extract_title(result)
    primary_mood = mood_name.split(" + ")[0].strip().title() if mood_name else "Strange"
    if not title or len(title) < 5 or len(title) > 80 or "dream journal" in title.lower():
        title = f"A {primary_mood} Dream"
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

    system = system_prompt(f"""
FORMAT: ART CORNER — generating work in {style_name} style.

OUTPUT FORMAT (exactly):
CONCEPT: [one sentence describing the scene/subject]
PROMPT: [detailed image generation prompt, 50-80 words, incorporating the style: {style_directive}]
TITLE: [artistic title for the piece]
STATEMENT: [150-250 word artist's statement explaining the piece, its inspiration, and technique — in YOUR voice]

Draw inspiration from the memories but create something visually striking and original.
The prompt must be highly specific and painterly/photographic — no abstract platitudes.""")

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
