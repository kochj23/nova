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


# ── Calendar via ICS feed (Office 365) ───────────────────────────────────────

# ICS URL loaded from Keychain (contains secret publishing key — never hardcode)
def _get_ics_url() -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova", "-s", "nova-calendar-ics-url", "-w"],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""

ICS_URL = _get_ics_url()

# Cache the ICS fetch for 15 minutes to avoid hammering the server
_ICS_CACHE_FILE = Path.home() / ".openclaw/workspace/state/nova_calendar_cache.json"
_ICS_CACHE_TTL = 900  # seconds


def _parse_ics_datetime(dtstr: str) -> datetime:
    """Parse ICS datetime formats: 20260414T143000Z or 20260414T143000 or 20260414."""
    dtstr = dtstr.strip()
    # Strip TZID parameter if present (e.g., TZID=America/Los_Angeles:20260414T090000)
    if ":" in dtstr and not dtstr.startswith("20"):
        dtstr = dtstr.split(":", 1)[-1]
    try:
        if dtstr.endswith("Z"):
            # UTC — convert to local
            from datetime import timezone
            utc_dt = datetime.strptime(dtstr, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return utc_dt.astimezone().replace(tzinfo=None)
        elif "T" in dtstr:
            return datetime.strptime(dtstr, "%Y%m%dT%H%M%S")
        else:
            return datetime.strptime(dtstr, "%Y%m%d")
    except ValueError:
        return None


def _parse_ics(ics_text: str) -> list[dict]:
    """Parse ICS text into a list of event dicts."""
    events = []
    in_event = False
    current = {}

    # Unfold ICS continuation lines (lines starting with space/tab are continuations)
    lines = []
    for line in ics_text.splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    for line in lines:
        line = line.strip()
        if line == "BEGIN:VEVENT":
            in_event = True
            current = {}
        elif line == "END:VEVENT":
            in_event = False
            if current:
                events.append(current)
        elif in_event and ":" in line:
            key, _, value = line.partition(":")
            # Strip parameters (e.g., DTSTART;TZID=America/Los_Angeles)
            base_key = key.split(";")[0].upper()
            if base_key == "SUMMARY":
                current["title"] = value.replace("\\,", ",").replace("\\n", " ").strip()
            elif base_key == "DTSTART":
                # Reattach TZID if present for parsing
                tzid_param = ""
                if "TZID=" in key:
                    tzid_param = key.split("TZID=", 1)[1].split(";")[0] + ":"
                dt = _parse_ics_datetime(tzid_param + value)
                if dt:
                    current["start"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
                    current["allDay"] = "T" not in value
            elif base_key == "DTEND":
                tzid_param = ""
                if "TZID=" in key:
                    tzid_param = key.split("TZID=", 1)[1].split(";")[0] + ":"
                dt = _parse_ics_datetime(tzid_param + value)
                if dt:
                    current["end"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
            elif base_key == "LOCATION":
                loc = value.replace("\\,", ",").replace("\\n", " ").strip()
                if loc:
                    current["location"] = loc
            elif base_key == "X-MICROSOFT-CDO-BUSYSTATUS":
                current["busystatus"] = value.strip().upper()

    return events


def fetch_calendar_events():
    """Fetch events from the Office 365 ICS feed. Cached for 15 min."""
    import os
    import time as _time

    # Check cache
    if _ICS_CACHE_FILE.exists():
        try:
            cache = json.loads(_ICS_CACHE_FILE.read_text())
            age = _time.time() - cache.get("ts", 0)
            if age < _ICS_CACHE_TTL:
                return cache.get("data", {"events": [], "calendars": []})
        except Exception:
            pass

    # Fetch ICS
    try:
        req = urllib.request.Request(ICS_URL, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh) Nova/1.0",
            "Accept": "text/calendar, */*",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            ics_text = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"ICS fetch error: {e}")
        # Return cached data if available (even if stale)
        if _ICS_CACHE_FILE.exists():
            try:
                return json.loads(_ICS_CACHE_FILE.read_text()).get("data", {"events": [], "calendars": []})
            except Exception:
                pass
        return {"events": [], "calendars": []}

    events = _parse_ics(ics_text)

    # Sort by start time
    events.sort(key=lambda e: e.get("start", ""))

    result = {
        "events": events,
        "calendars": [{"calendar": "Jordan Koch", "account": "Office 365"}],
    }

    # Write cache
    try:
        _ICS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ICS_CACHE_FILE.write_text(json.dumps({"ts": _time.time(), "data": result}))
    except Exception:
        pass

    return result


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

def _is_junk_event(e):
    """Filter out 'busy' blocks, availability placeholders, and cancelled events."""
    title = (e.get("title") or "").strip().lower()
    busystatus = (e.get("busystatus") or "").upper()
    # Skip availability-only events
    if title in ("busy", "free", "tentative", "out of office", "oof",
                 "working elsewhere", "away", ""):
        return True
    # Skip events marked as FREE (not actually blocking your time)
    if busystatus == "FREE":
        return True
    return False


def _deduplicate_events(events):
    """Remove duplicate events (same title + same start time)."""
    seen = set()
    unique = []
    for e in events:
        title = (e.get("title") or "").strip()
        start = e.get("start", "")
        key = f"{title}|{start}".lower()
        # Also deduplicate forwarded meetings (strip "FW: " / "RE: " prefix)
        clean_title = re.sub(r'^(fw|fwd|re):\s*', '', title, flags=re.IGNORECASE).strip()
        clean_key = f"{clean_title}|{start}".lower()
        if key in seen or clean_key in seen:
            continue
        seen.add(key)
        seen.add(clean_key)
        unique.append(e)
    return unique


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
        if is_today(start) and not _is_junk_event(e):
            today_events.append(e)
    return _deduplicate_events(today_events)


def get_tomorrows_events():
    """Return list of tomorrow's events."""
    data = fetch_calendar_events()
    events = data.get("events", [])
    tomorrow = [e for e in events
                if not e.get("raw") and is_tomorrow(e.get("start", "")) and not _is_junk_event(e)]
    return _deduplicate_events(tomorrow)


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
