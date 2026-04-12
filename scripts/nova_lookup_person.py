#!/usr/bin/env python3
"""
nova_lookup_person.py — Look up a person from Jordan's OneOnOne app.

Usage: nova_lookup_person.py "Jesse Smith"
       nova_lookup_person.py "Dan Mick"

Returns a JSON summary of who the person is and their meeting history.
Nova should call this whenever Jordan asks about a specific person.

Author: Jordan Koch / kochj23
"""

import json
import sys
import urllib.request
from difflib import SequenceMatcher

ONEONONE = "http://127.0.0.1:37421/api"


def get(path):
    try:
        with urllib.request.urlopen(f"{ONEONONE}{path}", timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return None


def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_person(query, people):
    """Fuzzy match a query name against the people list."""
    query = query.strip().lower()
    scored = []
    for p in people:
        name = p.get("name", "").strip()
        score = similarity(query, name.lower())
        # Also check partial (first name / last name match)
        name_parts = name.lower().split()
        query_parts = query.split()
        for part in query_parts:
            if any(similarity(part, np) > 0.85 for np in name_parts):
                score = max(score, 0.75)
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    # Return all matches above threshold (multiple people may match)
    return [(s, p) for s, p in scored if s > 0.5]


def get_person_meetings(person_id, meetings, id_to_name):
    """Get all meetings where this person was an attendee."""
    person_meetings = []
    for m in meetings:
        attendees = m.get("attendees", [])
        # Also search meeting title for the person's name
        if person_id in attendees:
            person_meetings.append(m)
    return person_meetings


def format_meeting(m, id_to_name):
    title    = m.get("title", "Untitled")
    date     = m.get("date", "")[:10]
    notes    = (m.get("notes") or "").strip()
    actions  = [ai.get("title", "") for ai in m.get("actionItems", []) if ai.get("title")]
    attendee_names = [id_to_name.get(a, a) for a in m.get("attendees", [])]

    out = [f"Meeting: {title} ({date})"]
    if attendee_names:
        out.append(f"Attendees: {', '.join(attendee_names)}")
    if actions:
        out.append("Action items: " + "; ".join(actions))
    if notes and notes != "\n\n\n\n\n\n\n":
        # Trim very long notes
        preview = notes[:600].strip()
        if len(notes) > 600:
            preview += "..."
        out.append(f"Notes: {preview}")
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: nova_lookup_person.py \"Person Name\""}))
        sys.exit(1)

    query = " ".join(sys.argv[1:])

    people   = get("/people")
    meetings = get("/meetings?limit=500")

    if not people:
        print(json.dumps({"error": "OneOnOne app not running (port 37421)"}))
        sys.exit(1)

    id_to_name = {p["id"]: p["name"].strip() for p in people}
    matches = find_person(query, people)

    if not matches:
        # Return all people so Nova can tell Jordan who IS in the app
        names = [p["name"].strip() for p in people]
        print(json.dumps({
            "found": False,
            "query": query,
            "message": f"No person matching '{query}' found in Jordan's OneOnOne.",
            "all_contacts": names
        }))
        return

    results = []
    for score, person in matches[:3]:  # top 3 matches
        pid   = person["id"]
        name  = person.get("name", "").strip()
        title = person.get("title", "")
        dept  = person.get("department", "")
        email = person.get("email", "")
        freq  = person.get("meetingFrequency", "")
        last  = person.get("lastMeetingDate", "")[:10]

        person_meetings = get_person_meetings(pid, meetings or [], id_to_name)
        meeting_summaries = [format_meeting(m, id_to_name) for m in person_meetings]

        results.append({
            "match_score": round(score, 2),
            "name": name,
            "title": title,
            "department": dept,
            "email": email,
            "meeting_frequency": freq,
            "last_meeting": last,
            "meeting_count": len(person_meetings),
            "meetings": meeting_summaries
        })

    print(json.dumps({"found": True, "query": query, "matches": results}, indent=2))


if __name__ == "__main__":
    main()
