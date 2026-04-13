#!/usr/bin/env python3
"""
nova_calendar.py — Calendar awareness for Nova.

Reads today's (and tomorrow's) events from ALL calendars across all
configured email accounts via macOS Calendar (EventKit via osascript).

Can be:
  - Called standalone for a Slack digest
  - Imported by nova_morning_brief.py for the daily brief
  - Run via cron every 30 min to warn about upcoming meetings

Cron: every 30 min (for upcoming meeting alerts), 7am (for morning brief)
Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()
STATE_FILE = Path.home() / ".openclaw/workspace/state/nova_calendar_state.json"


def log(msg):
    print(f"[nova_calendar {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text, channel=None):
    data = json.dumps({
        "channel": channel or SLACK_CHAN, "text": text, "mrkdwn": True
    }).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "calendar", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Calendar via AppleScript (EventKit) ──────────────────────────────────────

CALENDAR_SWIFT = r'''
import EventKit
import Foundation

let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)

// Request access synchronously
if #available(macOS 14.0, *) {
    store.requestFullAccessToEvents { granted, error in
        sem.signal()
    }
} else {
    store.requestAccess(to: .event) { granted, error in
        sem.signal()
    }
}
_ = sem.wait(timeout: .now() + 5)

// Date range: today 00:00 to day-after-tomorrow 00:00
let cal = Calendar.current
let startOfDay = cal.startOfDay(for: Date())
guard let endDate = cal.date(byAdding: .day, value: 2, to: startOfDay) else {
    print("{\"events\":[],\"calendars\":[]}")
    exit(0)
}

// Calendars
let allCalendars = store.calendars(for: .event)
var calendarList: [[String: String]] = []
for c in allCalendars {
    calendarList.append(["calendar": c.title, "account": c.source.title])
}

// Events
let predicate = store.predicateForEvents(withStart: startOfDay, end: endDate, calendars: nil)
let events = store.events(matching: predicate).sorted { $0.startDate < $1.startDate }

let fmt = DateFormatter()
fmt.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
fmt.timeZone = TimeZone.current

var eventList: [[String: Any]] = []
for e in events {
    var dict: [String: Any] = [
        "title": e.title ?? "Untitled",
        "calendar": e.calendar.title,
        "account": e.calendar.source.title,
        "allDay": e.isAllDay,
        "start": fmt.string(from: e.startDate),
        "end": fmt.string(from: e.endDate),
    ]
    if let loc = e.location, !loc.isEmpty { dict["location"] = loc }
    eventList.append(dict)
}

let result: [String: Any] = ["events": eventList, "calendars": calendarList]
if let data = try? JSONSerialization.data(withJSONObject: result),
   let str = String(data: data, encoding: .utf8) {
    print(str)
} else {
    print("{\"events\":[],\"calendars\":[]}")
}
'''


def fetch_calendar_events():
    """Run Swift EventKit script and return parsed events."""
    # Write Swift source to a temp file and compile/run
    swift_file = Path.home() / ".openclaw/workspace/state/nova_calendar_events.swift"
    swift_file.write_text(CALENDAR_SWIFT)
    try:
        result = subprocess.run(
            ["swift", str(swift_file)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log(f"Swift error: {result.stderr.strip()[:200]}")
            return fetch_calendar_icalbuddy()
        raw = result.stdout.strip()
        if not raw:
            return {"events": [], "calendars": []}
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"JSON parse error: {e}")
        return fetch_calendar_icalbuddy()
    except subprocess.TimeoutExpired:
        log("Swift script timed out")
        return {"events": [], "calendars": []}
    except Exception as e:
        log(f"Calendar fetch error: {e}")
        return {"events": [], "calendars": []}


def fetch_calendar_icalbuddy():
    """Fallback: use icalBuddy CLI tool if AppleScript/EventKit fails."""
    try:
        result = subprocess.run(
            ["icalBuddy", "-b", "", "-nc", "-nrd",
             "-df", "%Y-%m-%dT%H:%M:%S", "-tf", "",
             "-iep", "title,datetime,location,calendar",
             "-po", "datetime,title,location,calendar",
             "eventsFrom:today", "to:tomorrow"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            log("icalBuddy not available")
            return {"events": [], "calendars": []}

        events = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # icalBuddy outputs: "datetime - datetime title (calendar)"
            events.append({"title": line, "raw": True})
        return {"events": events, "calendars": []}
    except FileNotFoundError:
        log("icalBuddy not installed — install with: brew install ical-buddy")
        return {"events": [], "calendars": []}
    except Exception as e:
        log(f"icalBuddy error: {e}")
        return {"events": [], "calendars": []}


# ── Event processing ─────────────────────────────────────────────────────────

def parse_time(iso_str):
    """Parse ISO datetime string to datetime object."""
    try:
        return datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def is_today(iso_str):
    return iso_str and iso_str.startswith(TODAY)


def is_tomorrow(iso_str):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    return iso_str and iso_str.startswith(tomorrow)


def format_time(iso_str):
    """Format '2026-04-12T14:30:00' as '2:30 PM'."""
    dt = parse_time(iso_str)
    if dt:
        return dt.strftime("%-I:%M %p")
    return iso_str


def minutes_until(iso_str):
    """Minutes from now until the event start."""
    dt = parse_time(iso_str)
    if dt:
        return (dt - NOW).total_seconds() / 60
    return None


# ── Public API (for morning brief import) ────────────────────────────────────

def get_todays_events():
    """Return list of today's events as dicts. Used by morning brief."""
    data = fetch_calendar_events()
    events = data.get("events", [])
    today_events = []
    for e in events:
        if e.get("raw"):
            today_events.append(e)
            continue
        start = e.get("start", "")
        if is_today(start):
            today_events.append(e)
    return today_events


def get_tomorrows_events():
    """Return list of tomorrow's events."""
    data = fetch_calendar_events()
    events = data.get("events", [])
    return [e for e in events if not e.get("raw") and is_tomorrow(e.get("start", ""))]


def get_calendars():
    """Return list of all calendar accounts."""
    data = fetch_calendar_events()
    return data.get("calendars", [])


def format_event_line(e):
    """Format a single event for Slack display."""
    if e.get("raw"):
        return f"  * {e['title']}"
    title = e.get("title", "Untitled")
    start = e.get("start", "")
    end = e.get("end", "")
    cal = e.get("calendar", "")
    location = e.get("location", "")
    all_day = e.get("allDay", False)

    if all_day:
        time_str = "All day"
    else:
        time_str = format_time(start)
        if end:
            time_str += f" - {format_time(end)}"

    line = f"  * {time_str} — *{title}*"
    if cal:
        line += f" _({cal})_"
    if location:
        line += f"\n    Location: {location}"
    return line


# ── Upcoming meeting alerts ──────────────────────────────────────────────────

def load_alert_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"alerted": []}


def save_alert_state(state):
    STATE_FILE.write_text(json.dumps(state))


def check_upcoming_alerts():
    """Check for meetings starting in the next 15-30 minutes. Alert once per event."""
    state = load_alert_state()
    alerted = set(state.get("alerted", []))
    today_events = get_todays_events()
    new_alerts = []

    for e in today_events:
        if e.get("raw") or e.get("allDay"):
            continue
        start = e.get("start", "")
        title = e.get("title", "Untitled")
        event_key = f"{start}_{title}"
        mins = minutes_until(start)

        if mins is not None and 0 < mins <= 30 and event_key not in alerted:
            alert_text = f"*{title}* starts in {int(mins)} minutes"
            loc = e.get("location", "")
            if loc:
                alert_text += f" — {loc}"
            new_alerts.append(alert_text)
            alerted.add(event_key)

    if new_alerts:
        msg = "*Upcoming:*\n" + "\n".join(f"  {a}" for a in new_alerts)
        slack_post(msg, channel=nova_config.JORDAN_DM)

    # Clean old entries (keep only today's)
    alerted = {k for k in alerted if k.startswith(TODAY)}
    state["alerted"] = list(alerted)
    save_alert_state(state)


# ── Full digest ──────────────────────────────────────────────────────────────

def calendar_digest():
    """Build a full calendar digest for Slack. Returns the text."""
    data = fetch_calendar_events()
    calendars = data.get("calendars", [])
    events = data.get("events", [])

    today_events = [e for e in events if not e.get("raw") and is_today(e.get("start", ""))]
    tomorrow_events = [e for e in events if not e.get("raw") and is_tomorrow(e.get("start", ""))]
    all_day = [e for e in today_events if e.get("allDay")]
    timed = [e for e in today_events if not e.get("allDay")]

    lines = [f"*Calendar — {NOW.strftime('%A, %B %d')}*"]

    # Show configured calendars
    if calendars:
        accounts = sorted(set(c.get("account", "?") for c in calendars))
        lines.append(f"_Calendars: {', '.join(accounts)}_")
        lines.append("")

    if not today_events and not tomorrow_events:
        lines.append("  _No events today or tomorrow._")
        return "\n".join(lines)

    if all_day:
        lines.append("*All day:*")
        for e in all_day:
            lines.append(format_event_line(e))
        lines.append("")

    if timed:
        lines.append("*Today:*")
        for e in timed:
            lines.append(format_event_line(e))
        lines.append("")

    if not today_events:
        lines.append("_No events today._")
        lines.append("")

    if tomorrow_events:
        lines.append(f"*Tomorrow ({(date.today() + timedelta(days=1)).strftime('%A')}):*")
        for e in tomorrow_events[:5]:
            lines.append(format_event_line(e))

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nova Calendar")
    parser.add_argument("--digest", action="store_true", help="Post full digest to Slack")
    parser.add_argument("--alerts", action="store_true", help="Check for upcoming meeting alerts")
    parser.add_argument("--list-calendars", action="store_true", help="List all calendar accounts")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if args.list_calendars:
        calendars = get_calendars()
        if calendars:
            for c in calendars:
                print(f"  {c.get('calendar', '?')} ({c.get('account', '?')})")
        else:
            print("  No calendars found (check Calendar.app permissions)")
        return

    if args.json:
        data = fetch_calendar_events()
        print(json.dumps(data, indent=2))
        return

    if args.alerts:
        log("Checking upcoming meeting alerts...")
        check_upcoming_alerts()
        log("Done.")
        return

    # Default: full digest to Slack
    log("Building calendar digest...")
    digest = calendar_digest()
    slack_post(digest)

    # Store in vector memory
    today_events = get_todays_events()
    if today_events:
        titles = [e.get("title", "?") for e in today_events if not e.get("raw")]
        summary = f"Calendar for {TODAY}: {len(today_events)} events — {', '.join(titles[:5])}"
        vector_remember(summary, {"date": TODAY, "type": "calendar_digest"})

    log(f"Posted digest with {len(today_events)} events.")


if __name__ == "__main__":
    main()
