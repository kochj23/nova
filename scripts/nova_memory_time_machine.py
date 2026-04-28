#!/usr/bin/env python3
"""
nova_memory_time_machine.py — "This Day in Jordan's Life"

Searches Nova's vector memory for what happened on this date in previous
years. Posts a nostalgic/reflective digest to Slack alongside the regular
"This Day in History" (Wikipedia).

Runs daily at 3:15 PM via launchd (afternoon reflection, not morning rush).

Written by Jordan Koch.
"""

import json
import sys
import urllib.request
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR

VECTOR_URL = "http://127.0.0.1:18790"
TODAY = date.today()
MONTH_DAY = TODAY.strftime("%B %d")  # "April 17"
CURRENT_YEAR = TODAY.year


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def recall(query, n=10, source=None):
    """Search vector memory."""
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


def search(query, n=10, source=None):
    """Text search vector memory."""
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


import urllib.parse


def find_memories_for_date(month, day):
    """Search for memories from this date across all years."""
    memories_by_year = {}

    # Search strategies:
    # 1. Date string patterns: "April 17", "04/17", "04-17", "2024-04-17"
    date_patterns = [
        f"{MONTH_DAY}",                          # "April 17"
        f"{month:02d}/{day:02d}",                 # "04/17"
        f"{month:02d}-{day:02d}",                 # "04-17"
    ]

    # Search specific years
    for year in range(2000, CURRENT_YEAR):
        year_date = f"{year}-{month:02d}-{day:02d}"
        year_patterns = [
            year_date,                             # "2024-04-17"
            f"{MONTH_DAY}, {year}",                # "April 17, 2024"
            f"{month:02d}/{day:02d}/{year}",       # "04/17/2024"
        ]

        for pattern in year_patterns:
            results = search(pattern, n=5)
            for mem in results:
                text = mem.get("text", "")
                # Verify this memory actually references this year
                if str(year) in text or year_date in text:
                    memories_by_year.setdefault(year, []).append({
                        "text": text[:300],
                        "source": mem.get("source", "?"),
                        "score": mem.get("score", 0),
                    })

    # Also try semantic search for this date
    for pattern in date_patterns:
        results = recall(f"what happened on {pattern}", n=10)
        for mem in results:
            text = mem.get("text", "")
            # Try to extract year from the memory text
            for year in range(2000, CURRENT_YEAR):
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
        memories_by_year[year] = unique[:3]  # Max 3 per year

    return memories_by_year


def main():
    log(f"Memory Time Machine — {MONTH_DAY}", level=LOG_INFO, source="time_machine")

    month = TODAY.month
    day = TODAY.day

    memories_by_year = find_memories_for_date(month, day)

    if not memories_by_year:
        log("No memories found for this date", level=LOG_INFO, source="time_machine")
        # Still post a message
        slack_post(
            f":hourglass_flowing_sand: *This Day in Your Life — {MONTH_DAY}*\n\n"
            f"Nothing found for this date across your memories. "
            f"Some dates are quiet. That's okay."
        )
        return

    lines = [f":hourglass_flowing_sand: *This Day in Your Life — {MONTH_DAY}*", ""]

    for year in sorted(memories_by_year.keys()):
        years_ago = CURRENT_YEAR - year
        label = f"{years_ago} year{'s' if years_ago != 1 else ''} ago" if years_ago > 0 else "This year"
        lines.append(f"*{year}* _{label}_")

        for mem in memories_by_year[year]:
            source = mem["source"]
            text = mem["text"].strip()
            # Clean up the text for display
            text = text.replace("\n", " ").strip()
            if len(text) > 250:
                text = text[:247] + "..."

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

    lines.append(f"_Searched {CURRENT_YEAR - 2000} years of memories for {MONTH_DAY}_")

    msg = "\n".join(lines)
    slack_post(msg)

    # Store the digest in memory
    try:
        summary = f"Memory Time Machine {MONTH_DAY}: found memories from {sorted(memories_by_year.keys())}"
        payload = json.dumps({
            "text": summary,
            "source": "dream",
            "metadata": {"type": "time_machine", "date": TODAY.isoformat()}
        }).encode()
        req = urllib.request.Request(f"{nova_config.VECTOR_URL}", data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

    log(f"Posted: {len(memories_by_year)} years with memories", level=LOG_INFO, source="time_machine")


if __name__ == "__main__":
    main()
