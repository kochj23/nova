#!/usr/bin/env python3
"""
nova_relationship_tracker.py — Track relationship health across Jordan's contacts.

Checks two groups:
  1. OneOnOne 1:1 contacts — uses lastMeetingDate + meeting cadence
  2. Herd members — uses email archive search + herd mail logs

Posts a digest to Slack #nova-chat when anyone is overdue.
Can also run in --quiet mode (only posts if there are overdue contacts).

Usage:
  nova_relationship_tracker.py             # post full digest to Slack
  nova_relationship_tracker.py --quiet     # post only if overdue contacts exist
  nova_relationship_tracker.py --report    # print to stdout only

Author: Jordan Koch / kochj23
"""

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config

ONEONONE_URL = "http://127.0.0.1:37421/api"
MEMORY_URL   = "http://127.0.0.1:18790"
SLACK_TOKEN  = nova_config.slack_bot_token()
SLACK_CHAN    = nova_config.SLACK_CHAN

NOW = datetime.now(timezone.utc)

# How long before a relationship is considered "at risk" by meeting cadence
CADENCE_THRESHOLDS = {
    "Weekly":    timedelta(days=10),   # weekly → warn after 10 days
    "Bi-weekly": timedelta(days=18),   # biweekly → warn after 18 days
    "Monthly":   timedelta(days=45),   # monthly → warn after 45 days
    "Quarterly": timedelta(days=100),
}
DEFAULT_THRESHOLD = timedelta(days=21)

# Herd contact thresholds — warn if no contact in N days
HERD_THRESHOLD = timedelta(days=14)


def get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[tracker] GET {url} failed: {e}", file=sys.stderr)
        return None


def days_since(iso_str):
    """Return days since an ISO datetime string, or None if unparseable."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (NOW - dt).days
    except Exception:
        return None


def extract_latest_date_from_notes(notes: str) -> datetime | None:
    """
    Parse dates embedded in meeting notes like '4/07/26:', '3/31/26', '03/28/26:'.
    Jordan uses M/D/YY or MM/DD/YY format at the start of each update block.
    Returns the most recent date found, or None.
    """
    if not notes:
        return None
    # Match M/D/YY or MM/DD/YY (2-digit year, assumed 2000s)
    pattern = r'\b(\d{1,2}/\d{1,2}/\d{2})\b'
    found = []
    for m in re.finditer(pattern, notes):
        try:
            dt = datetime.strptime(m.group(1), "%m/%d/%y").replace(tzinfo=timezone.utc)
            # Sanity check: must be between 2020 and today
            if datetime(2020, 1, 1, tzinfo=timezone.utc) <= dt <= NOW:
                found.append(dt)
        except ValueError:
            pass
    return max(found) if found else None


def last_meeting_date_for_person(person_id: str, meetings: list) -> tuple[datetime | None, str]:
    """
    Find the most recent actual meeting date for a person by:
    1. Scanning notes of all their meetings for embedded dates (M/D/YY)
    2. Falling back to updatedAt of their meetings
    Returns (most_recent_datetime, source_label)
    """
    candidates = []
    for m in meetings:
        if person_id not in m.get("attendees", []):
            continue
        notes = m.get("notes", "") or ""
        # Try to parse dates from notes
        dt = extract_latest_date_from_notes(notes)
        if dt:
            candidates.append((dt, f"notes of '{m.get('title','')[:40]}'"))
        # Also consider updatedAt as fallback
        updated = m.get("updatedAt", "")
        if updated:
            try:
                dt2 = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                candidates.append((dt2, f"updatedAt of '{m.get('title','')[:40]}'"))
            except ValueError:
                pass

    if not candidates:
        return None, "no meetings found"

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0]


def last_email_contact(name: str) -> tuple[int | None, str]:
    """Search email archive + email source for most recent contact with a person.
    Returns (days_ago, most_recent_subject_or_snippet)."""
    try:
        # Use /search for name lookup — much better than /recall for proper names
        url = f"{MEMORY_URL}/search?q={urllib.parse.quote(name)}&n=20&source=email"
        data = get(url)
        results = (data or {}).get("results", [])

        # Also check email_archive but cap results
        url2 = f"{MEMORY_URL}/search?q={urllib.parse.quote(name)}&n=10&source=email_archive"
        data2 = get(url2)
        results += (data2 or {}).get("results", [])

        if not results:
            return None, "no email history found"

        # Find most recent by created_at
        most_recent = None
        snippet = ""
        for r in results:
            created = r.get("created_at", "")
            if not most_recent or created > most_recent:
                most_recent = created
                text = r.get("text", "")
                # Pull subject from email text if present
                for line in text.splitlines():
                    if "subject:" in line.lower():
                        snippet = line.strip()[:80]
                        break
                if not snippet:
                    snippet = text[:80].strip()

        ago = days_since(most_recent) if most_recent else None
        return ago, snippet

    except Exception as e:
        return None, f"error: {e}"


def check_oneonone_contacts():
    """Check all OneOnOne contacts for overdue 1:1s.
    Uses dates parsed from meeting notes (Jordan updates the same recurring meeting)
    rather than the lastMeetingDate field which is often not set.
    """
    people   = get(f"{ONEONONE_URL}/people")
    meetings = get(f"{ONEONONE_URL}/meetings?limit=500")
    if not people:
        return [], []

    overdue = []
    ok      = []

    for p in people:
        name  = p.get("name", "").strip()
        freq  = p.get("meetingFrequency", "")

        # Skip Jordan himself
        if "jordan" in name.lower():
            continue

        threshold = CADENCE_THRESHOLDS.get(freq, DEFAULT_THRESHOLD)

        # Find most recent meeting date from notes (primary) or lastMeetingDate (fallback)
        last_dt, source = last_meeting_date_for_person(p["id"], meetings or [])

        # Final fallback: lastMeetingDate field
        if last_dt is None and p.get("lastMeetingDate"):
            try:
                last_dt = datetime.fromisoformat(
                    p["lastMeetingDate"].replace("Z", "+00:00"))
                source = "lastMeetingDate field"
            except ValueError:
                pass

        days_ago  = (NOW - last_dt).days if last_dt else None
        last_date = last_dt.strftime("%Y-%m-%d") if last_dt else "never"

        entry = {
            "name":      name,
            "title":     p.get("title", ""),
            "dept":      p.get("department", ""),
            "frequency": freq,
            "last_date": last_date,
            "days_ago":  days_ago,
            "threshold": threshold.days,
            "source":    source,
        }

        if days_ago is None:
            entry["status"] = "never met"
            overdue.append(entry)
        elif days_ago > threshold.days:
            entry["status"] = "overdue"
            overdue.append(entry)
        else:
            entry["status"] = "ok"
            ok.append(entry)

    overdue.sort(key=lambda x: (x["days_ago"] or 9999), reverse=True)
    return overdue, ok


def check_herd_contacts():
    """Check herd members for recent contact."""
    try:
        sys.path.insert(0, str(Path.home() / ".openclaw"))
        from herd_config import HERD
        herd = HERD
    except ImportError:
        return [], []

    overdue = []
    ok      = []

    for member in herd:
        name  = member.get("name", "").strip()
        email = member.get("email", "")
        if not name:
            continue

        days_ago, snippet = last_email_contact(name)

        entry = {
            "name":     name,
            "email":    email,
            "days_ago": days_ago,
            "snippet":  snippet,
        }

        if days_ago is None or days_ago > HERD_THRESHOLD.days:
            entry["status"] = "overdue"
            overdue.append(entry)
        else:
            entry["status"] = "ok"
            ok.append(entry)

    overdue.sort(key=lambda x: (x["days_ago"] or 9999), reverse=True)
    return overdue, ok


def format_slack_message(oo_overdue, oo_ok, herd_overdue, herd_ok):
    """Build a Slack message with the relationship digest."""
    lines = [f"*Relationship Check — {NOW.strftime('%A %b %d')}* 🤝"]
    lines.append("")

    total_overdue = len(oo_overdue) + len(herd_overdue)
    if total_overdue == 0:
        lines.append("✅ All relationships are current. Everyone's been reached recently.")
        return "\n".join(lines)

    # OneOnOne overdue
    if oo_overdue:
        lines.append(f"*📅 Overdue 1:1s ({len(oo_overdue)})*")
        for p in oo_overdue:
            if p["days_ago"] is None:
                age = "never met"
            else:
                age = f"{p['days_ago']}d ago"
            title_dept = " · ".join(filter(None, [p["title"], p["dept"]]))
            cadence = f" (should be {p['frequency'].lower()})" if p["frequency"] else ""
            lines.append(f"  • *{p['name']}* — last met {age}{cadence}"
                         + (f" · _{title_dept}_" if title_dept else ""))
        lines.append("")

    # Herd overdue
    if herd_overdue:
        lines.append(f"*✉️ Herd — no contact recently ({len(herd_overdue)})*")
        for h in herd_overdue:
            if h["days_ago"] is None:
                age = "no email history found"
            else:
                age = f"{h['days_ago']}d ago"
            lines.append(f"  • *{h['name']}* — last contact {age}")
        lines.append("")

    # Summary of what's OK
    all_ok = [p["name"] for p in oo_ok] + [h["name"] for h in herd_ok]
    if all_ok:
        lines.append(f"_✓ Current: {', '.join(all_ok)}_")

    return "\n".join(lines)


def post_slack(text):
    payload = json.dumps({
        "channel": SLACK_CHAN,
        "text": text,
        "mrkdwn": True
    }).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        }
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.loads(r.read())
    if not result.get("ok"):
        print(f"[tracker] Slack post failed: {result.get('error')}", file=sys.stderr)
    return result.get("ok", False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet",  action="store_true", help="Only post if overdue contacts exist")
    parser.add_argument("--report", action="store_true", help="Print to stdout, no Slack")
    args = parser.parse_args()

    print("[tracker] Checking OneOnOne contacts...", file=sys.stderr)
    oo_overdue, oo_ok = check_oneonone_contacts()

    print("[tracker] Checking herd contacts...", file=sys.stderr)
    herd_overdue, herd_ok = check_herd_contacts()

    total_overdue = len(oo_overdue) + len(herd_overdue)
    print(f"[tracker] {total_overdue} overdue, "
          f"{len(oo_ok) + len(herd_ok)} current", file=sys.stderr)

    message = format_slack_message(oo_overdue, oo_ok, herd_overdue, herd_ok)

    if args.report:
        print(message)
        return

    if args.quiet and total_overdue == 0:
        print("[tracker] All current — nothing to post.", file=sys.stderr)
        return

    post_slack(message)
    print("[tracker] Posted to Slack.", file=sys.stderr)


if __name__ == "__main__":
    main()
