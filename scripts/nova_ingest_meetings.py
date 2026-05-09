#!/usr/bin/env python3
"""
nova_ingest_meetings.py — Pull meetings from OneOnOne app API and store in Nova's memory.

Fetches all meetings from the OneOnOne app (port 37400), resolves attendee UUIDs
to names, and ingests each meeting as a structured memory into the PostgreSQL
vector store (port 18790).

Can be run manually or scheduled via OpenClaw cron.

Usage:
  python3 nova_ingest_meetings.py              # Ingest all meetings
  python3 nova_ingest_meetings.py --since 7    # Only last 7 days
  python3 nova_ingest_meetings.py --dry-run    # Preview without storing

Written by Jordan Koch.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

ONEONONE_API = "http://127.0.0.1:37400"
MEMORY_API = "http://127.0.0.1:18790"
SOURCE = "oneonone_meetings"


def fetch_json(url):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def post_json(url, data):
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_people():
    people = fetch_json(f"{ONEONONE_API}/api/people")
    return {p["id"]: p for p in people}


def resolve_attendees(attendee_ids, people_map):
    names = []
    for uid in attendee_ids:
        person = people_map.get(uid)
        if person:
            name = person.get("name", "Unknown")
            title = person.get("title", "")
            if title:
                names.append(f"{name} ({title})")
            else:
                names.append(name)
        else:
            names.append(uid[:8])
    return names


def format_meeting_text(meeting, attendees):
    parts = []

    title = meeting.get("title", "Untitled Meeting")
    date = meeting.get("date", "")
    if date:
        try:
            dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            date_str = date[:10]
    else:
        date_str = "unknown date"

    meeting_type = meeting.get("meetingType", "")
    duration_min = meeting.get("duration", 0) // 60

    parts.append(f"Meeting: {title}")
    parts.append(f"Date: {date_str}")
    if meeting_type:
        parts.append(f"Type: {meeting_type}")
    if duration_min:
        parts.append(f"Duration: {duration_min} minutes")
    if attendees:
        parts.append(f"Attendees: {', '.join(attendees)}")

    notes = meeting.get("notes", "")
    if notes:
        parts.append(f"\nNotes:\n{notes}")

    action_items = meeting.get("actionItems", [])
    if action_items:
        parts.append("\nAction Items:")
        for item in action_items:
            if isinstance(item, dict):
                parts.append(f"  - {item.get('text', item.get('title', str(item)))}")
            else:
                parts.append(f"  - {item}")

    decisions = meeting.get("decisions", [])
    if decisions:
        parts.append("\nDecisions:")
        for dec in decisions:
            if isinstance(dec, dict):
                parts.append(f"  - {dec.get('text', dec.get('title', str(dec)))}")
            else:
                parts.append(f"  - {dec}")

    follow_ups = meeting.get("followUps", [])
    if follow_ups:
        parts.append("\nFollow-ups:")
        for fu in follow_ups:
            if isinstance(fu, dict):
                parts.append(f"  - {fu.get('text', fu.get('title', str(fu)))}")
            else:
                parts.append(f"  - {fu}")

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Ingest OneOnOne meetings into Nova memory")
    parser.add_argument("--since", type=int, help="Only ingest meetings from last N days")
    parser.add_argument("--dry-run", action="store_true", help="Preview without storing")
    args = parser.parse_args()

    print(f"Fetching people from OneOnOne...")
    people_map = fetch_people()
    print(f"  Found {len(people_map)} people")

    print(f"Fetching meetings from OneOnOne...")
    meetings = fetch_json(f"{ONEONONE_API}/api/meetings")
    print(f"  Found {len(meetings)} meetings")

    if args.since:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.since)
        filtered = []
        for m in meetings:
            date_str = m.get("date", "")
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    if dt >= cutoff:
                        filtered.append(m)
                except Exception:
                    filtered.append(m)
        meetings = filtered
        print(f"  Filtered to {len(meetings)} meetings (last {args.since} days)")

    ingested = 0
    skipped = 0

    for meeting in meetings:
        meeting_id = meeting.get("id", "")
        title = meeting.get("title", "Untitled")
        date = meeting.get("date", "")
        notes = meeting.get("notes", "")

        if not notes and not meeting.get("actionItems") and not meeting.get("decisions"):
            skipped += 1
            continue

        attendees = resolve_attendees(meeting.get("attendees", []), people_map)
        text = format_meeting_text(meeting, attendees)

        if args.dry_run:
            print(f"\n{'='*60}")
            print(f"WOULD INGEST: {title} ({date[:10] if date else '?'})")
            print(f"  Attendees: {', '.join(attendees[:5])}")
            print(f"  Text length: {len(text)} chars")
            print(f"  First 200: {text[:200]}...")
            ingested += 1
            continue

        metadata = {
            "meeting_id": meeting_id,
            "title": title,
            "date": date,
            "meeting_type": meeting.get("meetingType", ""),
            "attendees": attendees,
            "tags": meeting.get("tags", []),
        }

        try:
            result = post_json(f"{MEMORY_API}/remember", {
                "text": text,
                "source": SOURCE,
                "metadata": metadata,
            })
            ingested += 1
            print(f"  ✓ {title} ({date[:10] if date else '?'}) → {result.get('id', '?')[:8]}")
        except Exception as e:
            print(f"  ✗ {title}: {e}", file=sys.stderr)

    print(f"\nDone. Ingested: {ingested}, Skipped (empty): {skipped}")


if __name__ == "__main__":
    main()
