#!/usr/bin/env python3
"""
nova_app_suggestions.py — Contextual app usage intelligence.

Tracks which of Jordan's apps are running throughout the day,
learns usage patterns over time, and proactively surfaces
relevant information:
  - "NMAPScanner hasn't been run in 2 weeks"
  - "You usually open MLXCode around this time"
  - "OneOnOne has 3 action items from last week"
  - "RsyncGUI last synced 5 days ago"

Stores daily snapshots in a JSON log for pattern learning.

Cron: every 4 hours (check), 8am (morning suggestions)
Written by Jordan Koch.
"""

import json
import subprocess
import sys
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
HOUR = NOW.hour
TODAY = date.today().isoformat()
DAY_NAME = NOW.strftime("%A")
DATA_FILE = Path.home() / ".openclaw" / "workspace" / "app_usage_log.json"

# ── App definitions ──────────────────────────────────────────────────────────

# (port, name, bundle, data_endpoint, stale_days_threshold)
# stale_days_threshold: if app hasn't been seen running in this many days, suggest it
APPS = [
    (37421, "OneOnOne",       "OneOnOne",       "/api/oneonone/actionitems?completed=false", 3),
    (37422, "MLXCode",        "MLX Code",       "/api/conversations",                         7),
    (37423, "NMAPScanner",    "NMAPScanner",    "/api/scan/results",                          14),
    (37424, "RsyncGUI",       "RsyncGUI",       None,                                         7),
    (37432, "HomekitControl", "HomekitControl",  "/api/status",                                1),
    (37443, "TopGUI",         "TopGUI",          None,                                        14),
    (37445, "ytdlp-gui",      "ytdlp-gui",      None,                                        14),
    (37446, "DotSync",        "Dot Sync",        None,                                         7),
]


def log(msg):
    print(f"[nova_app_suggestions {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


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
            "text": text, "source": "app_suggestions", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Data persistence ─────────────────────────────────────────────────────────

def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"snapshots": [], "suggestions_sent": {}}


def save_data(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep 60 days of snapshots
    cutoff = (NOW - timedelta(days=60)).isoformat()
    data["snapshots"] = [s for s in data.get("snapshots", []) if s.get("date", "") > cutoff]
    DATA_FILE.write_text(json.dumps(data, indent=2))


# ── App status checks ────────────────────────────────────────────────────────

def check_app(port, timeout=3):
    """Check if an app is running. Returns (alive, status_data)."""
    try:
        url = f"http://127.0.0.1:{port}/api/status"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, json.loads(r.read())
    except Exception:
        return False, {}


def get_app_data(port, endpoint, timeout=5):
    """Fetch app-specific data from an endpoint."""
    if not endpoint:
        return None
    try:
        url = f"http://127.0.0.1:{port}{endpoint}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ── Pattern analysis ─────────────────────────────────────────────────────────

def analyze_patterns(data):
    """Analyze usage patterns from historical snapshots."""
    snapshots = data.get("snapshots", [])
    if len(snapshots) < 7:
        return {}  # Not enough data yet

    # Count how often each app is seen running on each day of week + time block
    day_hour_usage = {}  # {app_name: {day_name: {hour_block: count}}}
    last_seen = {}       # {app_name: last_date_seen_running}

    for snap in snapshots:
        snap_date = snap.get("date", "")
        snap_hour = snap.get("hour", 12)
        snap_day = snap.get("day", "Monday")
        hour_block = "morning" if snap_hour < 12 else "afternoon" if snap_hour < 17 else "evening"

        for app_name in snap.get("running", []):
            day_hour_usage.setdefault(app_name, {}).setdefault(snap_day, {})
            day_hour_usage[app_name][snap_day][hour_block] = \
                day_hour_usage[app_name][snap_day].get(hour_block, 0) + 1
            if snap_date > last_seen.get(app_name, ""):
                last_seen[app_name] = snap_date

    return {
        "day_hour_usage": day_hour_usage,
        "last_seen": last_seen,
    }


def generate_suggestions(data, running_apps, patterns):
    """Generate contextual suggestions based on current state and patterns."""
    suggestions = []
    last_seen = patterns.get("last_seen", {})
    day_hour = patterns.get("day_hour_usage", {})
    hour_block = "morning" if HOUR < 12 else "afternoon" if HOUR < 17 else "evening"

    for port, name, bundle, endpoint, stale_threshold in APPS:
        is_running = name in running_apps

        # ── Stale app check ──────────────────────────────────────────────
        if not is_running:
            last = last_seen.get(name, "")
            if last:
                days_since = (date.today() - date.fromisoformat(last)).days
                if days_since >= stale_threshold:
                    suggestions.append({
                        "type": "stale",
                        "app": name,
                        "message": f"*{name}* hasn't been used in {days_since} days",
                        "priority": "low",
                    })

            # ── Pattern-based suggestion ─────────────────────────────────
            app_patterns = day_hour.get(name, {})
            today_pattern = app_patterns.get(DAY_NAME, {})
            if today_pattern.get(hour_block, 0) >= 3:  # Used 3+ times at this day/time
                suggestions.append({
                    "type": "pattern",
                    "app": name,
                    "message": f"You usually run *{name}* on {DAY_NAME} {hour_block}s",
                    "priority": "low",
                })

        # ── App-specific intelligence ────────────────────────────────────
        if is_running and endpoint:
            app_data = get_app_data(port, endpoint)
            if app_data:
                if name == "OneOnOne":
                    # Check for overdue action items
                    items = app_data if isinstance(app_data, list) else app_data.get("actionItems", [])
                    if len(items) > 0:
                        suggestions.append({
                            "type": "actionable",
                            "app": name,
                            "message": f"*OneOnOne* has {len(items)} open action item(s)",
                            "priority": "medium",
                        })
                elif name == "NMAPScanner":
                    # Check for security warnings
                    results = app_data if isinstance(app_data, list) else app_data.get("results", [])
                    warnings = [r for r in results if r.get("severity") in ("high", "critical")]
                    if warnings:
                        suggestions.append({
                            "type": "actionable",
                            "app": name,
                            "message": f"*NMAPScanner* has {len(warnings)} security warning(s)",
                            "priority": "high",
                        })

    return suggestions


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Checking app usage and generating suggestions...")
    data = load_data()

    # ── Take a snapshot of what's running ────────────────────────────────────
    running_apps = []
    for port, name, bundle, endpoint, stale in APPS:
        alive, _ = check_app(port)
        if alive:
            running_apps.append(name)

    snapshot = {
        "date": TODAY,
        "hour": HOUR,
        "day": DAY_NAME,
        "running": running_apps,
    }
    data.setdefault("snapshots", []).append(snapshot)
    log(f"Snapshot: {len(running_apps)} apps running — {', '.join(running_apps) or 'none'}")

    # ── Analyze patterns ─────────────────────────────────────────────────────
    patterns = analyze_patterns(data)
    suggestions = generate_suggestions(data, running_apps, patterns)

    # ── Filter already-sent suggestions (once per day per suggestion) ────────
    sent_today = data.get("suggestions_sent", {})
    new_suggestions = []
    for s in suggestions:
        key = f"{TODAY}_{s['app']}_{s['type']}"
        if key not in sent_today:
            new_suggestions.append(s)
            sent_today[key] = NOW.isoformat()

    # Clean old sent records
    week_ago = (NOW - timedelta(days=7)).isoformat()
    sent_today = {k: v for k, v in sent_today.items() if v > week_ago}
    data["suggestions_sent"] = sent_today
    save_data(data)

    # ── Post suggestions ─────────────────────────────────────────────────────
    if new_suggestions:
        # Sort by priority
        priority_order = {"high": 0, "medium": 1, "low": 2}
        new_suggestions.sort(key=lambda s: priority_order.get(s["priority"], 2))

        lines = [f"*App Intelligence — {NOW.strftime('%I:%M %p')}*"]
        for s in new_suggestions[:5]:  # Cap at 5 to avoid noise
            icon = {"stale": "💤", "pattern": "🔮", "actionable": "📌"}.get(s["type"], "💡")
            lines.append(f"  {icon} {s['message']}")

        slack_post("\n".join(lines))
        log(f"Posted {len(new_suggestions)} suggestion(s)")
    else:
        log("No new suggestions.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova App Suggestions")
    parser.add_argument("--suggest", action="store_true", help="Generate and post suggestions (default)")
    parser.add_argument("--snapshot", action="store_true", help="Take a snapshot only (no suggestions)")
    parser.add_argument("--patterns", action="store_true", help="Print learned patterns")
    args = parser.parse_args()

    if args.snapshot:
        data = load_data()
        running = []
        for port, name, *_ in APPS:
            alive, _ = check_app(port)
            if alive:
                running.append(name)
                print(f"  UP  {name} (:{port})")
            else:
                print(f"  --  {name} (:{port})")
        data.setdefault("snapshots", []).append({
            "date": TODAY, "hour": HOUR, "day": DAY_NAME, "running": running
        })
        save_data(data)
    elif args.patterns:
        data = load_data()
        patterns = analyze_patterns(data)
        last_seen = patterns.get("last_seen", {})
        print("Last seen running:")
        for app, dt in sorted(last_seen.items()):
            days = (date.today() - date.fromisoformat(dt)).days
            print(f"  {app}: {dt} ({days} days ago)")
    else:
        main()
