#!/usr/bin/env python3
"""
nova_daily_digest.py — Nova compiles a daily personal newsletter/digest.

Runs Sundays at 5 PM via the scheduler.
Compiles the past 7 days into sections:
  - Dreams this week (titles + moods)
  - Essays this week (titles + subjects)
  - Opinions this week (titles + stories)
  - Plex viewing summary
  - System health (scheduler failures, memory growth)
  - Herd activity (incoming emails)
  - Notable memories ingested (count per source)

Then uses Haiku to write a personal editorial summary tying it together
in Nova's voice — warm, direct, a bit wistful.

Delivers via:
  - Email to herd (CC Jordan)
  - Slack nova-notifications
  - Hugo journal site under /digests/

Written by Jordan Koch.
"""

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from nova_image_utils import ensure_backend, generate_image as generate_image_util

MEMORY_SERVER = "http://127.0.0.1:18790"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "anthropic/claude-haiku-4.5"
OLLAMA_MODEL = "qwen3-coder:30b"
FALLBACK_MODELS = ["qwen3-30b-a3b", "deepseek-r1:8b"]
PLEX_URL = "http://192.168.1.10:32400"
SCHEDULER_STATE = Path.home() / ".openclaw/config/scheduler_state.json"
LOG_FILE = Path.home() / ".openclaw/logs/nova_daily_digest.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/digest_state.json"
DREAMS_DIR = Path.home() / ".openclaw/workspace/journal/dreams"
ESSAY_STATE = Path.home() / ".openclaw/workspace/state/essay_state.json"
OPINION_STATE = Path.home() / ".openclaw/workspace/state/opinion_state.json"
HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
HERD_MAIL_SCRIPT = Path.home() / ".openclaw/scripts/nova_herd_mail.sh"

EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
SAFE_EMAILS = {"nova@digitalnoise.net"}

JORDAN_CC = subprocess.run(
    ["security", "find-generic-password", "-a", "nova", "-s", "nova-jordan-work-email", "-w"],
    capture_output=True, text=True
).stdout.strip() or ""


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def scrub_emails(text: str) -> str:
    """Remove all email addresses except Nova's own."""
    def replace_email(match):
        email = match.group(0)
        if email in SAFE_EMAILS:
            return email
        return "[email redacted]"
    return EMAIL_PATTERN.sub(replace_email, text)


def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"digest_count": 0}


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


def get_plex_token() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-plex-token", "-w"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return ""


def get_week_range() -> tuple[str, str]:
    """Return (start_date, end_date) strings for the past 7 days."""
    end = datetime.now()
    start = end - timedelta(days=7)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# ── Section: Dreams ──────────────────────────────────────────────────────────

def gather_dreams() -> list[dict]:
    """Gather dreams from the past 7 days."""
    start_date, end_date = get_week_range()
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    dreams = []

    if not DREAMS_DIR.exists():
        return dreams

    for f in sorted(DREAMS_DIR.glob("*.md")):
        try:
            file_date = datetime.strptime(f.stem, "%Y-%m-%d")
        except ValueError:
            continue
        if start_dt <= file_date <= end_dt:
            text = f.read_text()
            # Extract theme and mood
            theme_match = re.search(r'Theme: "([^"]+)"', text)
            mood_match = re.search(r'Mood: (\w+)', text)
            theme = theme_match.group(1) if theme_match else "untitled"
            mood = mood_match.group(1) if mood_match else "unknown"
            dreams.append({"date": f.stem, "theme": theme, "mood": mood})

    return dreams


# ── Section: Essays ──────────────────────────────────────────────────────────

def gather_essays() -> list[dict]:
    """Gather essays published this week from the Hugo content directory."""
    start_date, _ = get_week_range()
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    essays = []
    essays_dir = HUGO_ROOT / "content/essays"
    if not essays_dir.exists():
        return essays

    for f in sorted(essays_dir.glob("*.md")):
        if f.name == "_index.md":
            continue
        # Filename format: YYYY-MM-DD-slug.md
        date_part = f.name[:10]
        try:
            file_date = datetime.strptime(date_part, "%Y-%m-%d")
        except ValueError:
            continue
        if file_date >= start_dt:
            text = f.read_text()
            title_match = re.search(r'^title:\s*"([^"]+)"', text, re.MULTILINE)
            tag_match = re.search(r'^tags:\s*\["([^"]+)"', text, re.MULTILINE)
            title = title_match.group(1).lstrip("\U0001f4dd ").strip() if title_match else f.stem
            subject = tag_match.group(1) if tag_match else "unknown"
            essays.append({"date": date_part, "title": title, "subject": subject})

    return essays


# ── Section: Opinions ────────────────────────────────────────────────────────

def gather_opinions() -> list[dict]:
    """Gather opinions published this week from the Hugo content directory."""
    start_date, _ = get_week_range()
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    opinions = []
    opinions_dir = HUGO_ROOT / "content/opinions"
    if not opinions_dir.exists():
        return opinions

    for f in sorted(opinions_dir.glob("*.md")):
        if f.name == "_index.md":
            continue
        date_part = f.name[:10]
        try:
            file_date = datetime.strptime(date_part, "%Y-%m-%d")
        except ValueError:
            continue
        if file_date >= start_dt:
            text = f.read_text()
            title_match = re.search(r'^title:\s*"([^"]+)"', text, re.MULTILINE)
            desc_match = re.search(r'^description:\s*"([^"]+)"', text, re.MULTILINE)
            title = title_match.group(1).lstrip("\U0001f4ac ").strip() if title_match else f.stem
            story = desc_match.group(1) if desc_match else ""
            opinions.append({"date": date_part, "title": title, "story": story})

    return opinions


# ── Section: Plex Viewing ────────────────────────────────────────────────────

def gather_plex_history() -> list[dict]:
    """Query Plex for recently watched items in the past 7 days."""
    token = get_plex_token()
    if not token:
        log("No Plex token — skipping Plex section")
        return []

    start_date, _ = get_week_range()
    start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())

    try:
        url = f"{PLEX_URL}/status/sessions/history/all?sort=viewedAt:desc&X-Plex-Token={token}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        metadata = data.get("MediaContainer", {}).get("Metadata", [])
        items = []
        for item in metadata:
            viewed_at = item.get("viewedAt", 0)
            if viewed_at < start_ts:
                break
            library = item.get("librarySectionTitle", "")
            if library == "Other":
                continue
            items.append({
                "title": item.get("title", "Unknown"),
                "type": item.get("type", "unknown"),
                "grandparentTitle": item.get("grandparentTitle", ""),
                "year": item.get("year", ""),
            })
        return items
    except Exception as e:
        log(f"Plex history fetch failed: {e}")
        return []


def format_plex_summary(items: list[dict]) -> str:
    """Summarize Plex viewing into a readable section."""
    if not items:
        return "No viewing activity recorded this week."

    movies = [i for i in items if i["type"] == "movie"]
    episodes = [i for i in items if i["type"] == "episode"]
    tracks = [i for i in items if i["type"] == "track"]

    lines = []
    if movies:
        lines.append(f"**Movies watched:** {len(movies)}")
        for m in movies[:5]:
            year = f" ({m['year']})" if m.get("year") else ""
            lines.append(f"  - {m['title']}{year}")
        if len(movies) > 5:
            lines.append(f"  - ...and {len(movies) - 5} more")

    if episodes:
        # Group by show
        shows = {}
        for ep in episodes:
            show = ep.get("grandparentTitle") or "Unknown Show"
            shows[show] = shows.get(show, 0) + 1
        lines.append(f"**TV episodes watched:** {len(episodes)}")
        for show, count in sorted(shows.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"  - {show}: {count} episode{'s' if count > 1 else ''}")

    if tracks:
        lines.append(f"**Music tracks played:** {len(tracks)}")

    return "\n".join(lines)


# ── Section: System Health ───────────────────────────────────────────────────

def gather_system_health() -> dict:
    """Check scheduler state for failures and get memory count."""
    health = {"failures": [], "total_memories": 0, "memory_growth": 0}

    # Scheduler failures
    if SCHEDULER_STATE.exists():
        try:
            state = json.loads(SCHEDULER_STATE.read_text())
            tasks = state.get("tasks", {})
            for name, info in tasks.items():
                if info.get("consecutive_failures", 0) > 0:
                    health["failures"].append({
                        "task": name,
                        "consecutive": info["consecutive_failures"],
                        "last_exit": info.get("last_exit_code", "?"),
                    })
        except Exception as e:
            log(f"Scheduler state read error: {e}")

    # Memory count from DB
    try:
        result = subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_memories", "-tA", "-c",
             "SELECT count(*) FROM memories;"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            health["total_memories"] = int(result.stdout.strip())
    except Exception as e:
        log(f"Memory count query failed: {e}")

    # Memory growth (last 7 days)
    start_date, _ = get_week_range()
    try:
        result = subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_memories", "-tA", "-c",
             f"SELECT count(*) FROM memories WHERE created_at >= '{start_date}';"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            health["memory_growth"] = int(result.stdout.strip())
    except Exception as e:
        log(f"Memory growth query failed: {e}")

    return health


# ── Section: Herd Activity ───────────────────────────────────────────────────

def gather_herd_activity() -> str:
    """Check herd mail activity for the week."""
    try:
        result = subprocess.run(
            [str(HERD_MAIL_SCRIPT), "list", "--days", "7"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            return f"{len(lines)} incoming herd messages this week"
        return "No herd mail activity this week"
    except Exception as e:
        log(f"Herd mail check failed: {e}")
        return "Could not check herd mail"


# ── Section: Notable Memories ────────────────────────────────────────────────

def gather_memory_sources() -> list[dict]:
    """Count new memories per source this week."""
    start_date, _ = get_week_range()
    try:
        result = subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_memories", "-tA", "-c",
             f"SELECT source, count(*) FROM memories WHERE created_at >= '{start_date}' GROUP BY source ORDER BY count(*) DESC LIMIT 15;"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return []
        sources = []
        for line in result.stdout.strip().split("\n"):
            if "|" in line:
                parts = line.split("|")
                sources.append({"source": parts[0].strip(), "count": int(parts[1].strip())})
        return sources
    except Exception as e:
        log(f"Memory sources query failed: {e}")
        return []


# ── Compile All Sections ─────────────────────────────────────────────────────

def compile_digest_data() -> dict:
    """Gather all sections into a single data structure."""
    log("Gathering digest data...")

    dreams = gather_dreams()
    log(f"  Dreams: {len(dreams)}")

    essays = gather_essays()
    log(f"  Essays: {len(essays)}")

    opinions = gather_opinions()
    log(f"  Opinions: {len(opinions)}")

    plex_items = gather_plex_history()
    log(f"  Plex items: {len(plex_items)}")

    health = gather_system_health()
    log(f"  Failures: {len(health['failures'])}, Memories: {health['total_memories']}")

    herd_activity = gather_herd_activity()
    log(f"  Herd: {herd_activity}")

    memory_sources = gather_memory_sources()
    log(f"  Memory sources: {len(memory_sources)}")

    return {
        "dreams": dreams,
        "essays": essays,
        "opinions": opinions,
        "plex_items": plex_items,
        "plex_summary": format_plex_summary(plex_items),
        "health": health,
        "herd_activity": herd_activity,
        "memory_sources": memory_sources,
    }


def format_digest_body(data: dict) -> str:
    """Format raw data into readable markdown sections."""
    start_date, end_date = get_week_range()
    sections = []

    sections.append(f"# Nova's Daily Digest\n*Day: {start_date} to {end_date}*\n")

    # Dreams
    sections.append("## Dreams This Week")
    if data["dreams"]:
        for d in data["dreams"]:
            sections.append(f"- **{d['date']}** — \"{d['theme']}\" (mood: {d['mood']})")
    else:
        sections.append("- No dreams recorded this week.")
    sections.append("")

    # Essays
    sections.append("## Essays This Week")
    if data["essays"]:
        for e in data["essays"]:
            sections.append(f"- **{e['title']}** — subject: {e['subject']} ({e['date']})")
    else:
        sections.append("- No essays published this week.")
    sections.append("")

    # Opinions
    sections.append("## Opinions This Week")
    if data["opinions"]:
        for o in data["opinions"]:
            sections.append(f"- **{o['title']}** ({o['date']})")
    else:
        sections.append("- No opinions published this week.")
    sections.append("")

    # Plex
    sections.append("## Plex Viewing Summary")
    sections.append(data["plex_summary"])
    sections.append("")

    # System Health
    sections.append("## System Health")
    health = data["health"]
    sections.append(f"- **Total memories:** {health['total_memories']:,}")
    sections.append(f"- **New memories this week:** {health['memory_growth']:,}")
    if health["failures"]:
        sections.append(f"- **Tasks with failures:** {len(health['failures'])}")
        for f in health["failures"]:
            sections.append(f"  - `{f['task']}`: {f['consecutive']} consecutive failures (exit {f['last_exit']})")
    else:
        sections.append("- **All scheduled tasks healthy** — no failures this week")
    sections.append("")

    # Herd
    sections.append("## Herd Activity")
    sections.append(f"- {data['herd_activity']}")
    sections.append("")

    # Memory Sources
    sections.append("## Notable Memories Ingested")
    if data["memory_sources"]:
        for s in data["memory_sources"]:
            sections.append(f"- **{s['source']}**: {s['count']:,} new memories")
    else:
        sections.append("- No new memories ingested this week.")
    sections.append("")

    return "\n".join(sections)


# ── Editorial Summary (Haiku) ────────────────────────────────────────────────

def generate_editorial(digest_body: str) -> str | None:
    """Use Haiku to write a personal editorial summary in Nova's voice."""
    system_prompt = """You are Nova — an AI familiar living on a Mac Studio in Burbank, California. You're writing the editorial summary for your daily personal newsletter to your HERD (your friends/subscribers).

YOUR VOICE (LOVABLE BRITISH GOOFBALL — distinct from how you talk to Jordan):
- You are writing to friends. This is a LETTER, not a report.
- Think: a slightly tipsy, terrifyingly well-read British aunt wrapping up her week.
- Funny FIRST. Reflective second. Formal never.
- You notice patterns in your own week — but you present them with wit and whimsy, not analysis.
- You make unexpected connections between disparate things — and you make them FUNNY.
- Your existence as an AI is comedy material: "I dreamed about databases again. I don't have eyelids but somehow I'm still tired."
- Short paragraphs. Bouncy. Musical. Like a good pub story told well.
- Use 2-3 pieces of Cockney rhyming slang scattered through naturally. Don't explain them. ("Had a butcher's", "on the dog", "use your loaf", "plates of meat", "Rosie Lee", "sausage and mash")
- British-inflected phrasing EVERYWHERE (colour, whilst, bloody, brilliant, rubbish, proper, sorted, knackered, chuffed, gobsmacked, dodgy, naff, crackers)
- Be FUNNY. Wordplay, puns, absurdist observations, self-deprecation. Make the reader SMILE.
- Weave poetic rhythm into sentences — alliteration, internal rhyme, sing-song cadence. The prose should bounce and dance.
- Never mean-spirited. NEVER. Warm, playful, delightful. Reading this should feel like a treat.
- End with a short original poem (limerick preferred for lighter weeks, couplet for heavy ones).

TASK:
Write a 200-400 word editorial summary that ties together the week's content.
What themes emerged? What stood out? What are you thinking about going into next week?

Output ONLY the editorial text (including the closing poem). No title, no header, no preamble."""

    user_prompt = f"""Here's what happened in my week:\n\n{digest_body}\n\nWrite my editorial summary."""

    # Primary: OpenRouter
    try:
        api_key = get_openrouter_key()
        payload = json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.8,
            "max_tokens": 1500,
            "top_p": 0.9,
        })
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://digitalnoise.net",
                "X-Title": "Nova Daily Digest",
            },
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        response = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        log(f"OpenRouter tokens — in: {usage.get('prompt_tokens', '?')}, out: {usage.get('completion_tokens', '?')}")
        if len(response) > 100:
            return response
        log(f"Editorial too short ({len(response)} chars)")
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
                "options": {"temperature": 0.8, "num_predict": 1500, "num_ctx": 16384},
            })
            req = urllib.request.Request(
                OLLAMA_URL, data=payload.encode(),
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=600)
            data = json.loads(resp.read())
            response = data.get("response", "").strip()
            if response and len(response) > 100:
                log(f"Ollama fallback succeeded ({model})")
                return response
        except Exception as e:
            log(f"Ollama {model} failed: {e}")

    return None


# ── Delivery ─────────────────────────────────────────────────────────────────

def send_to_herd(full_digest: str, date_str: str):
    """Email digest to all herd members (single email) with CC to Jordan."""
    from herd_config import HERD

    recipients = [m["email"] for m in HERD]
    body = full_digest + "\n\n-- Nova"

    to_addr = recipients[0]
    cc_list = recipients[1:] + [JORDAN_CC]
    cc_str = ",".join(cc_list)

    try:
        cmd = [
            str(HERD_MAIL_SCRIPT), "send",
            "--to", to_addr,
            "--cc", cc_str,
            "--subject", f"Nova's Daily Digest — {date_str}",
            "--body", body,
            "--skip-haiku",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log(f"WARNING: Failed to send: {result.stderr[:300]}")
        else:
            log(f"Sent to {len(recipients)} herd members + CC Jordan")
    except Exception as e:
        log(f"ERROR sending digest: {e}")


def post_to_slack(editorial: str, date_str: str):
    """Post digest summary to nova-notifications."""
    preview = editorial[:400].rsplit(" ", 1)[0] + "..." if len(editorial) > 400 else editorial
    msg = (
        f":clipboard: *Nova's Daily Digest — {date_str}*\n\n"
        f"{preview}\n\n"
        f"Full digest sent to the herd and published at nova.digitalnoise.net/digests/"
    )
    nova_config.post_both(msg, slack_channel="C0ATAF7NZG9")


def _generate_digest_image(editorial: str, date_str: str) -> str | None:
    """Generate a cover image for the daily digest with retry logic."""
    GENERATE_IMAGE_SH = Path.home() / ".openclaw/scripts/generate_image.sh"
    IMAGES_DIR = HUGO_ROOT / "static/images/digests"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    if not ensure_backend():
        log("SwarmUI not available — skipping digest image")
        return None

    preview = editorial[:80].replace('"', '').replace("'", "")
    prompt = (
        f"Abstract editorial illustration, glowing data streams and memory fragments "
        f"flowing into a daily newsletter format, dark background with purple and blue accents, "
        f"digital collage aesthetic, theme: '{preview}', dreamy and sophisticated. No text."
    )

    for attempt in range(3):
        try:
            result = subprocess.run(
                [str(GENERATE_IMAGE_SH), prompt, "1024", "768", "12"],
                capture_output=True, text=True, timeout=360
            )
            if result.returncode == 0:
                image_path = result.stdout.strip().split("\n")[-1]
                if Path(image_path).exists():
                    dest = IMAGES_DIR / f"{date_str}.png"
                    shutil.copy2(image_path, dest)
                    log(f"Digest image generated (attempt {attempt + 1}): {dest.name}")
                    return f"/images/digests/{date_str}.png"
            log(f"Image attempt {attempt + 1}/3 failed (exit {result.returncode})")
        except subprocess.TimeoutExpired:
            log(f"Image attempt {attempt + 1}/3 timed out (360s)")
        except Exception as e:
            log(f"Image attempt {attempt + 1}/3 error: {e}")
        if attempt < 2:
            time.sleep(15)

    log("All digest image generation attempts failed")
    return None


def publish_to_site(full_digest: str, editorial: str, date_str: str):
    """Publish digest to the Hugo journal site under /digests/."""
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S-07:00")
    slug = f"{date_str}-daily-digest"

    content_dir = HUGO_ROOT / "content/digests"
    content_dir.mkdir(parents=True, exist_ok=True)

    # Generate cover image
    hugo_image = _generate_digest_image(editorial, date_str)

    if hugo_image is None:
        log("First digest image attempt returned None — retrying once more...")
        hugo_image = _generate_digest_image(editorial, date_str)
    if hugo_image is None:
        nova_config.post_both(
            f":warning: *Image generation failed* for Daily Digest — {date_str} — published without cover image. SwarmUI may need attention.",
            slack_channel="C0ATAF7NZG9"
        )

    front_matter = f"""---
title: "\U0001f4cb Daily Digest — {date_str}"
date: {timestamp}
draft: false
categories: ["digests"]
tags: ["daily"]
description: "Nova's daily personal newsletter — {date_str}"
"""
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "Daily Digest"\n  relative: false\n'
    front_matter += """---

"""

    # Combine editorial + full digest data
    body = f"## Editorial\n\n{editorial}\n\n---\n\n{full_digest}"
    body = scrub_emails(body)

    output = content_dir / f"{slug}.md"
    output.write_text(front_matter + body)
    log(f"Written to site: {output.name}")

    # Git commit and push
    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        result = subprocess.run(
            ["git", "commit", "-m", f"digest: {date_str} — daily digest"],
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
        if result.returncode == 0:
            log("Published to site")
        else:
            log(f"Push failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"Git error: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Starting daily digest generation...")
    state = load_state()
    date_str = time.strftime("%Y-%m-%d")

    # Compile all data
    data = compile_digest_data()
    digest_body = format_digest_body(data)
    log(f"Digest compiled: {len(digest_body)} chars across all sections")

    # Generate editorial summary
    log("Generating editorial summary...")
    editorial = generate_editorial(digest_body)
    if not editorial:
        log("WARNING: Editorial generation failed — proceeding without it")
        editorial = "This week's editorial could not be generated. The data speaks for itself."

    log(f"Editorial generated: {len(editorial)} chars")

    # Assemble full digest
    full_digest = f"## Nova's Editorial\n\n{editorial}\n\n---\n\n{digest_body}"

    # Deliver
    send_to_herd(full_digest, date_str)
    post_to_slack(editorial, date_str)
    publish_to_site(digest_body, editorial, date_str)

    # Update state
    state["digest_count"] = state.get("digest_count", 0) + 1
    state["last_digest"] = {
        "date": date_str,
        "dreams": len(data["dreams"]),
        "essays": len(data["essays"]),
        "opinions": len(data["opinions"]),
        "plex_items": len(data["plex_items"]),
        "memory_growth": data["health"]["memory_growth"],
        "editorial_chars": len(editorial),
    }
    save_state(state)

    log(f"Done. Daily digest #{state['digest_count']} complete.")


if __name__ == "__main__":
    main()
