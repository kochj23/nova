#!/usr/bin/env python3
"""
nova_this_day.py — "This Day in History" for Nova.

Fetches today's historical events, notable births, and deaths from the
Wikipedia On This Day API. Posts a formatted summary to Slack #nova-chat
and appends the facts to Nova's daily memory file so they can enrich her
2am dream journal.

Wikipedia API: api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{MM}/{DD}
No API key required.

Cron: runs daily at 3pm PT via OpenClaw jobs.json
Written by Jordan Koch.
"""

import json
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
import nova_config


VECTOR_MEM_URL = "http://127.0.0.1:18790/remember"


def vector_remember(text: str, metadata: dict = None):
    """Store text in Nova's vector memory. Silently skips if server is down."""
    try:
        payload = json.dumps({
            "text": text,
            "source": "history",
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            VECTOR_MEM_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"vector_remember skipped: {e}")


SLACK_TOKEN   = nova_config.slack_bot_token()
SLACK_CHANNEL = "C0AMNQ5GX70"  # #nova-chat
SLACK_API     = "https://slack.com/api"
MEMORY_DIR    = Path.home() / ".openclaw" / "workspace" / "memory"

# How many items to pull from each category
MAX_EVENTS = 6
MAX_BIRTHS = 3
MAX_DEATHS = 2


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[nova_this_day {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Wikipedia fetch ───────────────────────────────────────────────────────────

def fetch_on_this_day(month, day):
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{month:02d}/{day:02d}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Nova/1.0 nova_this_day.py",
            "Accept": "application/json",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log(f"Wikipedia HTTP error {e.code}: {e.reason}")
        return None
    except Exception as e:
        log(f"Wikipedia fetch error: {e}")
        return None


def score_event(event):
    """
    Rank events by how interesting they are for dream material.
    Prefer events with a year (not None), longer text, and certain keywords.
    """
    text = event.get("text", "")
    year = event.get("year")
    score = 0
    if year:
        score += 10
    score += min(len(text), 200) // 20  # up to 10 pts for length

    # Boost topics that make good dream fodder
    boost_words = [
        "war", "revolution", "discovery", "invention", "first", "astronaut",
        "space", "moon", "earthquake", "president", "king", "queen", "empire",
        "explosion", "fire", "flood", "coronation", "assassination", "treaty",
        "mystery", "strange", "bizarre", "unknown", "ancient", "ruins",
        "broadcast", "film", "art", "music", "science", "plague",
    ]
    text_lower = text.lower()
    for word in boost_words:
        if word in text_lower:
            score += 3

    return score


def pick_best(items, n, score_fn=None):
    if score_fn:
        items = sorted(items, key=score_fn, reverse=True)
    return items[:n]


# ── Format ────────────────────────────────────────────────────────────────────

def format_slack(month, day, events, births, deaths, date_str):
    lines = []
    lines.append(f"*On This Day -- {date_str}*")
    lines.append(f"_A few things that happened on {date_str} through history_\n")

    if events:
        lines.append("*Events*")
        for e in events:
            year = e.get("year")
            text = e.get("text", "")
            prefix = f"*{year}* — " if year else ""
            lines.append(f"  • {prefix}{text}")

    if births:
        lines.append("\n*Born on this day*")
        for b in births:
            year = b.get("year")
            text = b.get("text", "")
            prefix = f"*{year}* — " if year else ""
            lines.append(f"  • {prefix}{text}")

    if deaths:
        lines.append("\n*Deaths*")
        for d in deaths:
            year = d.get("year")
            text = d.get("text", "")
            prefix = f"*{year}* — " if year else ""
            lines.append(f"  • {prefix}{text}")

    lines.append(f"\n_-- Nova_")
    return "\n".join(lines)


def format_memory(month, day, events, births, deaths, date_str):
    """Format for Nova's memory file — plain text, dream-enriching."""
    lines = []
    lines.append(f"## On This Day in History -- {date_str}")
    lines.append(
        "These events occurred on this calendar date throughout history. "
        "They are available as raw material for tonight's dream."
    )
    lines.append("")

    if events:
        lines.append("### Historical Events")
        for e in events:
            year = e.get("year", "?")
            text = e.get("text", "")
            lines.append(f"- {year}: {text}")

    if births:
        lines.append("")
        lines.append("### Notable Births")
        for b in births:
            year = b.get("year", "?")
            text = b.get("text", "")
            lines.append(f"- {year}: {text}")

    if deaths:
        lines.append("")
        lines.append("### Notable Deaths")
        for d in deaths:
            year = d.get("year", "?")
            text = d.get("text", "")
            lines.append(f"- {year}: {text}")

    lines.append("")
    return "\n".join(lines)


# ── Slack ─────────────────────────────────────────────────────────────────────

def slack_post(text):
    chunks = [text[i:i + 3000] for i in range(0, len(text), 3000)]
    for chunk in chunks:
        data = json.dumps({
            "channel": SLACK_CHANNEL,
            "text":    chunk,
            "mrkdwn":  True,
        }).encode()
        req = urllib.request.Request(
            f"{SLACK_API}/chat.postMessage",
            data=data,
            headers={
                "Authorization": "Bearer " + SLACK_TOKEN,
                "Content-Type": "application/json; charset=utf-8",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    log(f"Slack error: {result.get('error')}")
        except Exception as e:
            log(f"Slack post error: {e}")


# ── Memory ────────────────────────────────────────────────────────────────────

def append_to_memory(content, date_str_ymd):
    """Append the history facts to today's memory file for dream pickup."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memory_file = MEMORY_DIR / f"{date_str_ymd}.md"

    if memory_file.exists():
        existing = memory_file.read_text(encoding="utf-8")
        # Don't duplicate if already written today
        if "On This Day in History" in existing:
            log("Memory file already has history entry — skipping.")
            return
        updated = existing.rstrip() + "\n\n" + content
    else:
        updated = f"# Nova Memory -- {date_str_ymd}\n\n" + content

    memory_file.write_text(updated, encoding="utf-8")
    log(f"Memory updated: {memory_file}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now       = datetime.now()
    month     = now.month
    day       = now.day
    date_str  = now.strftime("%B %d")           # "March 24"
    date_ymd  = now.strftime("%Y-%m-%d")        # "2026-03-24"

    log(f"Fetching On This Day for {date_str}...")
    data = fetch_on_this_day(month, day)
    if not data:
        log("No data returned from Wikipedia — aborting.")
        sys.exit(1)

    raw_events = data.get("events", [])
    raw_births = data.get("births", [])
    raw_deaths = data.get("deaths", [])

    log(f"Raw counts: {len(raw_events)} events, {len(raw_births)} births, {len(raw_deaths)} deaths")

    events = pick_best(raw_events, MAX_EVENTS, score_fn=score_event)
    births = pick_best(raw_births, MAX_BIRTHS, score_fn=score_event)
    deaths = pick_best(raw_deaths, MAX_DEATHS, score_fn=score_event)

    slack_msg    = format_slack(month, day, events, births, deaths, date_str)
    memory_block = format_memory(month, day, events, births, deaths, date_str)

    log("Posting to Slack...")
    slack_post(slack_msg)

    log("Updating Nova memory file...")
    append_to_memory(memory_block, date_ymd)

    log("Storing history in vector memory...")
    for ev in events[:4]:
        year = ev.get("year", "?")
        text = ev.get("text", "")
        if text:
            vector_remember(
                f"On this day ({date_str}), {year}: {text}",
                {"type": "historical_event", "year": str(year), "date": date_ymd},
            )
    for b in births[:2]:
        year = b.get("year", "?")
        text = b.get("text", "")
        if text:
            vector_remember(
                f"Born on {date_str}, {year}: {text}",
                {"type": "birth", "year": str(year), "date": date_ymd},
            )

    log("Done.")


if __name__ == "__main__":
    main()
