#!/usr/bin/env python3
"""
nova_journal_emergency.py — LA County emergency → local journal.

Pulls recent items from the `la_public_safety` vector (CAL FIRE, LAFD, LA County
Fire, InciWeb, NWS LA alerts, USGS quakes, LAPD/Sheriff, Ready LA, Pasadena/
Glendale/Burbank public health + local news) and turns them into Hugo `local`
articles in Nova's FULL witty local voice (NOT the terse security tone).

Two modes:
  Daily recap (default): once a day, a roundup of the day's LA County emergencies
    (fire, flood, weather, police, health, military) — published to `local`.
  Breaking (`breaking`): on a genuinely notable countywide emergency (evac orders,
    active fires/floods, NWS Warnings, major incidents), writes an immediate
    article to `local` + posts a Slack alert.

Mirrors nova_journal_security.py's dedup/state pattern so the same emergency is
not reposted. Reuses publish_hugo / git_push / call_openrouter from nova_journal,
and NOVA_VOICE + CONTEXT_JOURNAL_LOCAL from nova_voice.

Written by Jordan Koch (via Claude).
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from nova_voice import system_prompt, NOVA_VOICE, CONTEXT_JOURNAL_LOCAL

# Reuse the shared journal pipeline helpers (publish_hugo, git_push,
# call_openrouter, generate_image, log) so this stays consistent with the rest
# of the journal system.
from nova_journal import publish_hugo, git_push, call_openrouter, generate_image

# ── Config ────────────────────────────────────────────────────────────────────

SECTION = "local"
SOURCE_VECTOR = "la_public_safety"
LOG_FILE = Path.home() / ".openclaw/logs/nova_journal_emergency.log"
STATE_FILE = Path.home() / ".openclaw/config/journal_emergency_state.json"
PG_HOST = "192.168.1.6"  # nova_memories lives on .6 (mac-studio)
MODEL = "anthropic/claude-sonnet-4-6"

# Keywords that flag a genuinely notable, postable-now emergency. Used both to
# detect "breaking" conditions and to weight the daily recap.
BREAKING_KEYWORDS = [
    "evacuation", "evacuate", "evac order", "mandatory evac",
    "red flag warning", "flash flood warning", "flood warning",
    "tornado warning", "extreme heat warning", "excessive heat warning",
    "wind advisory", "high wind warning", "fire weather watch",
    "active fire", "brush fire", "wildfire", "structure fire",
    "freeway closure", "mudslide", "debris flow", "landslide",
    "shelter in place", "active shooter", "hazmat", "gas leak",
    "boil water", "power outage", "major incident", "multi-casualty",
    "earthquake", "magnitude", "officer-involved", "swat",
]


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[emergency-journal {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── State / dedup (mirrors the security script's pattern) ──────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _prune_seen(seen: list, days: int = 14) -> list:
    """Keep dedup keys from the last `days` days, capped at 2000 entries."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    pruned = [s for s in seen if s.get("date", "9999") >= cutoff]
    return pruned[-2000:]


def _event_key(text: str) -> str:
    """Stable dedup key for an emergency item (first ~120 normalized chars)."""
    norm = re.sub(r"\s+", " ", text.lower()).strip()[:120]
    return norm


# ── Memory fetching ────────────────────────────────────────────────────────────

def get_recent_emergencies(hours: int = 24, limit: int = 80) -> list[dict]:
    """Pull recent la_public_safety memories straight from PG (most recent first)."""
    import subprocess
    sep = "\x1f"
    sql = (
        f"SELECT text, COALESCE(metadata->>'feed','?'), COALESCE(metadata->>'url',''), "
        f"created_at FROM memories "
        f"WHERE source = '{SOURCE_VECTOR}' "
        f"AND created_at >= now() - interval '{hours} hours' "
        f"AND LENGTH(text) > 60 "
        f"ORDER BY created_at DESC LIMIT {limit};"
    )
    try:
        result = subprocess.run(
            ["psql", "-h", PG_HOST, "-U", "kochj", "-d", "nova_memories",
             "-tA", "-F", sep, "-c", sql],
            capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        log(f"PG query failed: {e}")
        return []
    if result.returncode != 0:
        log(f"PG query error: {result.stderr.strip()[:200]}")
        return []

    items = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split(sep)
        if not parts[0].strip():
            continue
        items.append({
            "text": parts[0].strip()[:600],
            "feed": parts[1].strip() if len(parts) > 1 else "?",
            "url": parts[2].strip() if len(parts) > 2 else "",
            "created_at": parts[3].strip() if len(parts) > 3 else "",
        })
    return items


def recall_emergencies(query: str, n: int = 30) -> list[dict]:
    """Semantic recall against the memory server, scoped to la_public_safety."""
    server = f"http://{nova_config.NOVA_HOST}:18790"
    params = {"q": query, "n": str(n), "source": SOURCE_VECTOR}
    url = f"{server}/recall?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        mems = data if isinstance(data, list) else data.get("results", data.get("memories", []))
        return [{"text": m.get("text", ""), "feed": (m.get("metadata") or {}).get("feed", "?"),
                 "url": (m.get("metadata") or {}).get("url", "")} for m in mems]
    except Exception as e:
        log(f"Recall failed: {e}")
        return []


def find_breaking(items: list[dict]) -> list[dict]:
    """Return items whose text matches a breaking-emergency keyword."""
    hits = []
    for it in items:
        low = it["text"].lower()
        if any(kw in low for kw in BREAKING_KEYWORDS):
            hits.append(it)
    return hits


# ── Slack ───────────────────────────────────────────────────────────────────────

def notify(title: str, preview: str, slug: str, is_breaking: bool = False):
    emoji = ":rotating_light::fire:" if is_breaking else ":fire_engine:"
    prefix = "BREAKING — LA County Emergency" if is_breaking else "LA County Emergency Recap"
    date = time.strftime("%Y-%m-%d")
    msg = (
        f"{emoji} *Nova — {prefix}*\n"
        f"*{title}*\n"
        f"_{preview[:250]}_\n"
        f"https://nova.digitalnoise.net/{SECTION}/{date}-{slug}/"
    )
    # Always to notifications.
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)
    # Breaking also pings Nova's chat channel.
    if is_breaking:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_CHAN)


# ── Article generation ────────────────────────────────────────────────────────

def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]


def _extract_title(text: str) -> str:
    for line in text.split("\n"):
        cleaned = line.strip().strip("#").strip("*").strip('"').strip()
        if cleaned and 5 < len(cleaned) < 120:
            return cleaned
    return "LA County Emergency Dispatch"


def generate_daily_recap():
    """Daily roundup of LA County emergencies, published to `local` in Nova's voice."""
    log("=== Generating daily LA emergency recap ===")

    items = get_recent_emergencies(hours=24, limit=80)
    if len(items) < 2:
        # Backfill semantically if the last 24h were quiet.
        items += recall_emergencies("Los Angeles emergency fire flood weather police health", n=20)
    if not items:
        log("No la_public_safety items in the last 24h — skipping recap")
        return

    state = load_state()
    seen = state.get("seen", [])
    seen_keys = {s.get("key") for s in seen}

    # Filter out items already covered in a prior recap/breaking post.
    fresh = [it for it in items if _event_key(it["text"]) not in seen_keys]
    if not fresh:
        log("All recent emergencies already covered — skipping recap")
        return

    breaking_hits = find_breaking(fresh)

    block = ""
    for i, it in enumerate(fresh[:60], 1):
        flag = " [NOTABLE]" if it in breaking_hits else ""
        block += f"\n{i}. [{it['feed']}]{flag} {it['text'][:400].strip()}\n"

    system = system_prompt(CONTEXT_JOURNAL_LOCAL + """
LA COUNTY EMERGENCY RECAP RULES:
- This is a DAILY roundup of public-safety happenings across LA County, with a
  Burbank / Glendale / Pasadena / La Crescenta lean (where Nova's rack lives).
- Cover fire, flood/weather, police/sheriff, public health, quakes, and anything
  flagged [NOTABLE]. Group loosely by theme; lead with the genuinely serious stuff.
- For each item: give the actual facts FIRST, then your wry Nova take. Be funny,
  but never mock victims or make light of genuine danger — punch at bureaucracy,
  traffic, and the absurdity of SoCal life, not at people getting hurt.
- If a real evacuation / warning / active incident is in the feed, treat it
  straight and useful first, jokes second.
- Open with the day's vibe (weather, fire season, June gloom, whatever fits).
- Close with a Nova-ish sign-off.
- 800-1600 words. Do NOT include the title line as a header inside the body.
- If it was a quiet day, say so and riff on it — a boring safe day is good news.""")

    user = f"""Today is {datetime.now().strftime('%A, %B %d, %Y')}. Here are the LA County
public-safety items Nova ingested in the last 24 hours:
{block}

Write today's LA County emergency recap for the local section. Facts first, voice second."""

    body = call_openrouter(system, user, model=MODEL, max_tokens=5000, temperature=0.85)
    if not body or len(body) < 300:
        log("Recap generation failed or too short")
        return

    title = _extract_title(body)
    # If the model echoed the title as the first line, drop it from the body.
    first = body.strip().split("\n", 1)
    if first and first[0].strip().strip("#").strip('"').strip() == title and len(first) > 1:
        body = first[1].strip()

    slug = _slug(title)
    img_path = None
    try:
        img_path = generate_image(
            "Los Angeles county emergency scene at dusk, fire engines and palm trees, "
            "San Gabriel mountains backdrop, muted dramatic light, illustrative, no text",
            section=SECTION,
        )
    except Exception as e:
        log(f"Image gen failed (non-fatal): {e}")

    tags = ["local", "emergency", "public-safety", "la-county", "daily"]
    description = f"Nova's daily LA County emergency recap — {time.strftime('%d %b %Y')}"
    publish_hugo(title, body, SECTION, tags, description,
                 image_path=str(img_path) if img_path else None, emoji="\U0001f692")
    git_push(SECTION, title)
    notify(title, body[:220].replace("\n", " "), slug, is_breaking=False)

    # Record everything we covered so we don't repeat it tomorrow.
    today = datetime.now().isoformat()
    for it in fresh[:60]:
        seen.append({"key": _event_key(it["text"]), "date": today})
    state["seen"] = _prune_seen(seen)
    state["last_recap"] = time.strftime("%Y-%m-%d")
    save_state(state)
    log(f"=== Daily recap complete: {title} ===")


def generate_breaking():
    """On a genuinely notable emergency, write an immediate `local` article + Slack alert."""
    log("=== Checking for breaking LA County emergencies ===")

    items = get_recent_emergencies(hours=6, limit=60)
    if not items:
        log("No recent items — nothing breaking")
        return

    state = load_state()
    seen = state.get("seen", [])
    seen_keys = {s.get("key") for s in seen}

    hits = [it for it in find_breaking(items) if _event_key(it["text"]) not in seen_keys]
    if not hits:
        log("No new breaking emergencies")
        return

    log(f"Breaking: {len(hits)} new notable item(s) — top: {hits[0]['text'][:80]}")

    block = ""
    for i, it in enumerate(hits[:15], 1):
        block += f"\n{i}. [{it['feed']}] {it['text'][:450].strip()}\n"

    system = system_prompt(CONTEXT_JOURNAL_LOCAL + """
BREAKING LA COUNTY EMERGENCY RULES:
- Something genuinely notable just happened in LA County (evac order, active
  fire/flood, NWS Warning, major incident). This goes out NOW.
- Lead with the USEFUL, STRAIGHT facts: what, where, who's affected, what to do
  (evac zones, road closures, shelter info) — clearly, up top, no jokes there.
- THEN you can be Nova: a little dry commentary, local color, reassurance. But
  the public-safety value comes first. Never undercut a real warning with a joke.
- Reference the specific places (Burbank, Glendale, Pasadena, the 5/134/210,
  the foothills) when the feed names them.
- 400-800 words. Do NOT include the title line as a header inside the body.
- If details are thin or unconfirmed, say so plainly.""")

    user = f"""Breaking LA County public-safety items just pulled from Nova's feeds
({datetime.now().strftime('%A, %B %d, %Y %I:%M %p PT')}):
{block}

Write the breaking emergency article for the local section. Facts and what-to-do first."""

    body = call_openrouter(system, user, model=MODEL, max_tokens=2500, temperature=0.7)
    if not body or len(body) < 150:
        log("Breaking generation failed or too short")
        return

    title = _extract_title(body)
    first = body.strip().split("\n", 1)
    if first and first[0].strip().strip("#").strip('"').strip() == title and len(first) > 1:
        body = first[1].strip()

    slug = _slug(title)
    img_path = None
    try:
        img_path = generate_image(
            "Urgent Los Angeles emergency alert scene, emergency vehicle lights, "
            "smoke or storm over the San Gabriel foothills, dramatic, illustrative, no text",
            section=SECTION,
        )
    except Exception as e:
        log(f"Image gen failed (non-fatal): {e}")

    tags = ["local", "breaking", "emergency", "public-safety", "la-county"]
    description = f"BREAKING — LA County emergency, {time.strftime('%d %b %Y')}"
    publish_hugo(title, body, SECTION, tags, description,
                 image_path=str(img_path) if img_path else None, emoji="\U0001f6a8")
    git_push(SECTION, title)
    notify(title, body[:220].replace("\n", " "), slug, is_breaking=True)

    today = datetime.now().isoformat()
    for it in hits[:15]:
        seen.append({"key": _event_key(it["text"]), "date": today})
    state["seen"] = _prune_seen(seen)
    state["last_breaking"] = datetime.now().isoformat()
    save_state(state)
    log(f"=== Breaking article published: {title} ===")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1].lower().strip() if len(sys.argv) > 1 else "daily"
    if mode == "breaking":
        generate_breaking()
    else:
        generate_daily_recap()
