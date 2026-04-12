#!/usr/bin/env python3
"""
ingest_oneonone.py — Ingest all OneOnOne people + meeting notes into Nova's vector memory.
Deletes and re-ingests on every run so updates to existing meetings are always reflected.
Source label: "oneonone"
Author: Jordan Koch / kochj23
"""

import json, urllib.request, urllib.parse, time

ONEONONE = "http://127.0.0.1:37421/api"
MEMORY   = "http://127.0.0.1:18790/remember"
SOURCE   = "oneonone"


def get(path):
    with urllib.request.urlopen(f"{ONEONONE}{path}", timeout=10) as r:
        return json.loads(r.read())


def store(text, metadata={}):
    payload = json.dumps({"text": text, "source": SOURCE, "metadata": metadata}).encode()
    req = urllib.request.Request(MEMORY, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ── Wipe and re-ingest so updates to existing meetings always reflect ──────────
print("Clearing existing oneonone chunks...")
try:
    req = urllib.request.Request(
        f"http://127.0.0.1:18790/forget_all?source={SOURCE}", method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as r:
        result = json.loads(r.read())
        print(f"  Deleted {result.get('deleted', 0)} existing chunks")
except Exception as e:
    print(f"  Clear failed (continuing anyway): {e}")

# ── Load data ──────────────────────────────────────────────────────────────────
people   = get("/people")
meetings = get("/meetings?limit=500")

# Build lookup: UUID → person name
id_to_name = {p["id"]: p["name"].strip() for p in people}

print(f"People: {len(people)}  |  Meetings: {len(meetings)}")
print()

# ── 1. Ingest each person ──────────────────────────────────────────────────────
print("=== PEOPLE ===")
for p in people:
    name  = p.get("name", "").strip()
    title = p.get("title", "")
    dept  = p.get("department", "")
    email = p.get("email", "")
    freq  = p.get("meetingFrequency", "")
    last  = p.get("lastMeetingDate", "")[:10]

    # Natural language profile — embeds much better than key:value pairs
    desc = f"{name} is someone Jordan Koch meets with"
    if freq:  desc += f" {freq.lower()}"
    desc += "."
    if title or dept:
        desc += f" {name}"
        if title: desc += f" is a {title}"
        if dept:  desc += f" in {dept}"
        desc += "."
    if email: desc += f" Their email is {email}."
    if last:  desc += f" Jordan last met with {name} on {last}."

    # Also add a direct name-lookup line so exact name queries hit
    text = f"{desc}\n\nContact: {name}. OneOnOne contact for Jordan Koch."
    if email: text += f" Email: {email}."

    store(text, {"person": name, "type": "person_profile"})
    print(f"  ✓ {name}")

print()

# ── 2. Ingest each meeting ─────────────────────────────────────────────────────
print("=== MEETINGS ===")
stored_meetings = 0
for m in meetings:
    title    = m.get("title", "Untitled").strip()
    date_str = m.get("date", "")[:10]
    notes    = (m.get("notes") or "").strip()
    mtype    = m.get("meetingType", "")
    duration = m.get("duration", 0)
    mins     = int(duration) // 60 if duration else 0

    # Resolve attendee UUIDs to names
    attendee_names = [id_to_name.get(a, a) for a in m.get("attendees", [])]

    # Action items
    action_items = [ai.get("title", "") for ai in m.get("actionItems", []) if ai.get("title")]

    # Decisions / follow-ups
    decisions  = m.get("decisions", [])
    follow_ups = m.get("followUps", [])

    # Skip meetings with no useful content
    if not notes and not action_items and not decisions:
        print(f"  – {date_str} {title[:50]} (no content, skipping)")
        continue

    parts = [f"Meeting: {title}", f"Date: {date_str}"]
    if mtype:            parts.append(f"Type: {mtype}")
    if mins:             parts.append(f"Duration: {mins} minutes")
    if attendee_names:   parts.append(f"Attendees: {', '.join(attendee_names)}")
    if action_items:     parts.append("Action Items:\n" + "\n".join(f"- {a}" for a in action_items))
    if decisions:        parts.append("Decisions:\n" + "\n".join(f"- {d}" for d in decisions))
    if follow_ups:       parts.append("Follow-ups:\n" + "\n".join(f"- {f}" for f in follow_ups))
    if notes:            parts.append(f"Notes:\n{notes[:2000]}")

    # Chunk notes if they're long (>1000 chars)
    base = "\n".join(parts[:7])  # header without full notes
    if len(notes) > 1000:
        # Store header + first chunk
        store(base + f"\nNotes (part 1):\n{notes[:1500]}",
              {"meeting": title, "date": date_str, "attendees": attendee_names, "type": "meeting_notes"})
        # Store remaining notes in chunks
        offset = 1500
        part = 2
        while offset < len(notes):
            chunk = notes[offset:offset+1500]
            store(f"Meeting: {title} ({date_str}) — continued (part {part}):\n{chunk}",
                  {"meeting": title, "date": date_str, "type": "meeting_notes_continued"})
            offset += 1500
            part += 1
        print(f"  ✓ {date_str} {title[:55]} ({part-1} chunks)")
    else:
        store("\n".join(parts), {"meeting": title, "date": date_str, "attendees": attendee_names, "type": "meeting_notes"})
        print(f"  ✓ {date_str} {title[:55]}")

    stored_meetings += 1

print()

# ── Stats ──────────────────────────────────────────────────────────────────────
stats = json.loads(urllib.request.urlopen("http://127.0.0.1:18790/stats", timeout=5).read())
oneonone_count = stats.get("by_source", {}).get(SOURCE, 0)
print(f"Done. '{SOURCE}' source now has {oneonone_count} chunks in Nova's memory.")
print(f"Total memories: {stats.get('count', '?')}")
