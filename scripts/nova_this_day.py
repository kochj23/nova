#!/usr/bin/env python3
"""
nova_this_day.py — "This Day" unified digest for Nova.

Combines two sections into ONE Slack message posted at 3:00 PM daily:

  1. This Day in History — Wikipedia On This Day API
     (events, births, deaths with scoring for dream material)

  2. This Day in Your Life — Personal memories from Nova's vector memory
     (optimized: 3 broad queries, not 78+ per-year queries)

Also stores facts in vector memory and appends to the daily memory file
so they can enrich Nova's 2am dream journal.

Wikipedia API: api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{MM}/{DD}
No API key required.

Cron: runs daily at 3:00 PM PT via OpenClaw jobs.json
Written by Jordan Koch.
"""

import json
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR


VECTOR_URL = "http://127.0.0.1:18790"
VECTOR_MEM_URL = f"{VECTOR_URL}/remember"
MEMORY_DIR = Path.home() / ".openclaw" / "workspace" / "memory"

# How many items to pull from each Wikipedia category
MAX_EVENTS = 6
MAX_BIRTHS = 3
MAX_DEATHS = 2


# ── Vector memory helpers ────────────────────────────────────────────────────


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
        log(f"vector_remember skipped: {e}", level=LOG_INFO, source="this_day")


def vector_recall(query, n=10, source=None):
    """Semantic search of vector memory."""
    params = f"q={urllib.parse.quote(query)}&n={n}"
    if source:
        params += f"&source={source}"
    try:
        url = f"{VECTOR_URL}/recall?{params}"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        return data.get("memories", [])
    except Exception:
        return []


def vector_search(query, n=10, source=None):
    """Text search of vector memory."""
    params = f"q={urllib.parse.quote(query)}&n={n}"
    if source:
        params += f"&source={source}"
    try:
        url = f"{VECTOR_URL}/search?{params}"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        return data.get("memories", [])
    except Exception:
        return []


# ── Wikipedia fetch & scoring ────────────────────────────────────────────────


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
        log(f"Wikipedia HTTP error {e.code}: {e.reason}", level=LOG_ERROR, source="this_day")
        return None
    except Exception as e:
        log(f"Wikipedia fetch error: {e}", level=LOG_ERROR, source="this_day")
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


# ── Personal memory search ───────────────────────────────────────────────────


def find_memories_for_date(month, day, month_day_str, current_year):
    """Search for personal memories from this date across all years.

    Optimized: uses 3 broad queries instead of 78+ per-year queries.
    """
    memories_by_year = {}

    queries = [
        (vector_search, f"{month:02d}-{day:02d}", 20),
        (vector_search, month_day_str, 20),
        (vector_recall, f"what happened on {month_day_str}", 15),
    ]

    all_results = []
    for fn, query, n in queries:
        results = fn(query, n=n)
        all_results.extend(results)

    # Bucket results by year
    for mem in all_results:
        text = mem.get("text", "")
        for year in range(2000, current_year):
            if str(year) in text:
                memories_by_year.setdefault(year, []).append({
                    "text": text[:300],
                    "source": mem.get("source", "?"),
                    "score": mem.get("score", 0),
                })
                break

    # Deduplicate within each year
    for year in memories_by_year:
        seen = set()
        unique = []
        for mem in memories_by_year[year]:
            key = mem["text"][:80]
            if key not in seen:
                seen.add(key)
                unique.append(mem)
        memories_by_year[year] = sorted(unique, key=lambda m: -m.get("score", 0))[:3]

    return memories_by_year


# ── Formatting ───────────────────────────────────────────────────────────────


def format_history_slack(events, births, deaths, date_str):
    """Format Wikipedia history section for Slack."""
    lines = []
    lines.append(f":scroll: *This Day in History -- {date_str}*")
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

    return "\n".join(lines)


def _clean_memory_text(text, source):
    """Clean up raw memory text for display.

    Email archive entries often contain full headers (Date:, From:, To:, Subject:).
    Extract just the useful info and cap at 150 chars.
    """
    text = text.strip().replace("\n", " ").strip()

    # Detect raw email headers and extract subject + sender
    if source in ("email_archive", "email") or text.startswith(("Date:", "From:", "Email subject archive")):
        subject = ""
        sender = ""
        for part in text.replace("; ", "\n").replace("  ", "\n").split("\n"):
            part = part.strip()
            if part.startswith("Subject:"):
                subject = part[len("Subject:"):].strip()
            elif part.startswith("SUBJ:"):
                subject = part[len("SUBJ:"):].strip()
            elif part.startswith("From:"):
                sender = part[len("From:"):].strip()
            elif part.startswith("FROM:"):
                sender = part[len("FROM:"):].strip()
        # Also try regex extraction from single-line format
        if not subject:
            import re
            subj_match = re.search(r"(?:Subject|SUBJ):\s*(.+?)(?:\s*(?:From|FROM|To|TO|Date):|$)", text)
            if subj_match:
                subject = subj_match.group(1).strip()
        if not sender:
            import re
            from_match = re.search(r"(?:From|FROM):\s*(.+?)(?:\s*(?:Subject|SUBJ|To|TO|Date):|$)", text)
            if from_match:
                sender = from_match.group(1).strip()

        if subject or sender:
            if subject and sender:
                text = f"Email: {subject} (from {sender})"
            elif subject:
                text = f"Email: {subject}"
            elif sender:
                text = f"Email from {sender}"

    # Cap at 150 characters
    if len(text) > 150:
        text = text[:147] + "..."
    return text


def format_personal_slack(memories_by_year, month_day_str, current_year):
    """Format personal memory section for Slack."""
    lines = []
    lines.append(f":hourglass_flowing_sand: *This Day in Your Life -- {month_day_str}*")

    if not memories_by_year:
        lines.append("")
        lines.append(
            f"Nothing found for this date across your memories. "
            f"Some dates are quiet. That's okay."
        )
        return "\n".join(lines)

    lines.append("")

    for year in sorted(memories_by_year.keys()):
        years_ago = current_year - year
        label = f"{years_ago} year{'s' if years_ago != 1 else ''} ago" if years_ago > 0 else "This year"
        lines.append(f"*{year}* _{label}_")

        for mem in memories_by_year[year]:
            source = mem["source"]
            text = _clean_memory_text(mem["text"], source)

            source_emoji = {
                "email_archive": ":email:",
                "imessage": ":iphone:",
                "music": ":notes:",
                "video": ":tv:",
                "calendar": ":date:",
                "document": ":page_facing_up:",
            }.get(source, ":brain:")

            lines.append(f"  {source_emoji} {text}")
        lines.append("")

    lines.append(f"_Searched {current_year - 2000} years of memories for {month_day_str}_")
    return "\n".join(lines)


def format_memory_file(events, births, deaths, date_str):
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


# ── Slack ────────────────────────────────────────────────────────────────────


def slack_post(text):
    """Post to Slack, splitting long messages into 3000-char chunks."""
    chunks = [text[i:i + 3000] for i in range(0, len(text), 3000)]
    for chunk in chunks:
        nova_config.post_both(chunk, slack_channel=nova_config.SLACK_NOTIFY)


# ── Memory file ──────────────────────────────────────────────────────────────


def append_to_memory(content, date_str_ymd):
    """Append the history facts to today's memory file for dream pickup."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memory_file = MEMORY_DIR / f"{date_str_ymd}.md"

    if memory_file.exists():
        existing = memory_file.read_text(encoding="utf-8")
        if "On This Day in History" in existing:
            log("Memory file already has history entry — skipping.", level=LOG_INFO, source="this_day")
            return
        updated = existing.rstrip() + "\n\n" + content
    else:
        updated = f"# Nova Memory -- {date_str_ymd}\n\n" + content

    memory_file.write_text(updated, encoding="utf-8")
    log(f"Memory updated: {memory_file}", level=LOG_INFO, source="this_day")


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    now = datetime.now()
    today = date.today()
    month = now.month
    day = now.day
    current_year = today.year
    date_str = now.strftime("%B %d")       # "April 29"
    date_ymd = now.strftime("%Y-%m-%d")    # "2026-04-29"

    # ── Section 1: Wikipedia history ─────────────────────────────────────

    log(f"Fetching On This Day for {date_str}...", level=LOG_INFO, source="this_day")
    wiki_data = fetch_on_this_day(month, day)

    history_block = ""
    events, births, deaths = [], [], []

    if wiki_data:
        raw_events = wiki_data.get("events", [])
        raw_births = wiki_data.get("births", [])
        raw_deaths = wiki_data.get("deaths", [])
        log(f"Raw counts: {len(raw_events)} events, {len(raw_births)} births, {len(raw_deaths)} deaths",
            level=LOG_INFO, source="this_day")

        events = pick_best(raw_events, MAX_EVENTS, score_fn=score_event)
        births = pick_best(raw_births, MAX_BIRTHS, score_fn=score_event)
        deaths = pick_best(raw_deaths, MAX_DEATHS, score_fn=score_event)

        history_block = format_history_slack(events, births, deaths, date_str)
    else:
        log("No data returned from Wikipedia — history section will be empty.",
            level=LOG_ERROR, source="this_day")
        history_block = f":scroll: *This Day in History -- {date_str}*\n_Wikipedia was unavailable. Try again later._"

    # ── Section 2: Personal memories ─────────────────────────────────────

    log(f"Searching personal memories for {date_str}...", level=LOG_INFO, source="this_day")
    memories_by_year = find_memories_for_date(month, day, date_str, current_year)
    log(f"Found memories from {len(memories_by_year)} years", level=LOG_INFO, source="this_day")

    personal_block = format_personal_slack(memories_by_year, date_str, current_year)

    # ── Compose and post unified message ─────────────────────────────────

    divider = "\n─────────────────────────────\n"
    unified_msg = f"*:calendar: On This Day — {date_str}*\n{divider}{history_block}{divider}{personal_block}\n\n_— Nova_"

    log("Posting unified This Day message to Slack...", level=LOG_INFO, source="this_day")
    slack_post(unified_msg)

    # ── Store in memory file for dream pickup ────────────────────────────

    if events or births or deaths:
        log("Updating Nova memory file...", level=LOG_INFO, source="this_day")
        memory_content = format_memory_file(events, births, deaths, date_str)
        append_to_memory(memory_content, date_ymd)

    # ── Store personal memories in memory file for dream pickup ────────
    if memories_by_year:
        mem_lines = [f"\n\n## This Day in Your Life"]
        for year in sorted(memories_by_year.keys()):
            years_ago = current_year - year
            for mem in memories_by_year[year][:2]:
                mem_lines.append(f"- {year} ({years_ago}y ago): {mem['text'][:150]}")
        mem_lines.append("")
        personal_mem_block = "\n".join(mem_lines)
        mem_file = MEMORY_DIR / f"{date_ymd}.md"
        try:
            if mem_file.exists():
                existing = mem_file.read_text(encoding="utf-8")
                if "This Day in Your Life" not in existing:
                    mem_file.write_text(existing.rstrip() + personal_mem_block, encoding="utf-8")
            else:
                mem_file.write_text(f"# Nova Memory -- {date_ymd}\n" + personal_mem_block, encoding="utf-8")
        except Exception:
            pass

    # ── Store history in vector memory ───────────────────────────────────

    log("Storing history in vector memory...", level=LOG_INFO, source="this_day")
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

    # ── Store time machine digest in vector memory ───────────────────────

    if memories_by_year:
        try:
            summary = f"Memory Time Machine {date_str}: found memories from {sorted(memories_by_year.keys())}"
            payload = json.dumps({
                "text": summary,
                "source": "dream",
                "metadata": {"type": "time_machine", "date": today.isoformat()}
            }).encode()
            req = urllib.request.Request(
                VECTOR_MEM_URL, data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    log("Done.", level=LOG_INFO, source="this_day")


if __name__ == "__main__":
    main()
