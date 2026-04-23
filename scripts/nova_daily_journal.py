#!/usr/bin/env python3

"""
Nova Daily Journal — nightly summary posted to Slack at 9 PM PT.

Pulls directly from Postgres (nova_memories) by source and date
instead of semantic search. Sections: calendar, infra, security,
app health, local news, dreams, and historical tidbits.

Written by Jordan Koch.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.expanduser("~/.openclaw/logs/daily-journal.log")),
        logging.StreamHandler(),
    ],
)

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
TODAY = datetime.now().strftime("%Y-%m-%d")
DB = "nova_memories"
STATE_DIR = Path.home() / ".openclaw/workspace/state"


def _query(sql):
    try:
        result = subprocess.run(
            ["psql", "-U", "kochj", "-d", DB, "-tAc", sql],
            capture_output=True, text=True, timeout=10,
        )
        rows = [r for r in result.stdout.strip().split("\n") if r]
        return rows
    except Exception as e:
        logging.warning(f"DB query failed: {e}")
        return []


def _query_field(sql):
    rows = _query(sql)
    return rows[0] if rows else None


def _load_state(name):
    path = STATE_DIR / name
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def section_calendar():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'calendar' AND created_at >= '{TODAY}' "
        f"ORDER BY created_at LIMIT 3"
    )
    if not rows:
        return None
    # Calendar entries are usually "Calendar for DATE: N events — ..."
    # Deduplicate and pick the most complete one
    best = max(rows, key=len)
    # Strip the prefix if present
    if "—" in best:
        events_part = best.split("—", 1)[1].strip()
        events = [e.strip() for e in events_part.split(",")]
        lines = [f"• {e}" for e in events if e]
        return "*Today's Calendar:*\n" + "\n".join(lines)
    return f"*Today's Calendar:*\n{best}"


def section_morning_brief():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'morning_brief' AND created_at >= '{TODAY}' "
        f"ORDER BY created_at DESC LIMIT 1"
    )
    if not rows:
        return None
    brief = rows[0]
    # Extract weather from brief: "Morning brief DATE: Sunny +55°F ..."
    if ":" in brief:
        content = brief.split(":", 1)[1].strip()
        parts = []
        # Weather
        if "°F" in content:
            weather = content.split(".")[0]
            parts.append(f"• Weather: {weather.strip()}")
        # Meetings
        if "Meetings:" in content:
            meetings = content.split("Meetings:", 1)[1].strip()
            parts.append(f"• Meetings: {meetings.split('.')[0].strip()}")
        if parts:
            return "*Morning Brief:*\n" + "\n".join(parts)
    return None


def section_infrastructure():
    lines = []

    # NAS state
    nas = _load_state("nova_synology_state.json")
    if nas:
        model = nas.get("model", "NAS")
        cpu = nas.get("cpu_pct", "?")
        ram = nas.get("ram_pct", "?")
        vols = nas.get("volumes", "?")
        problems = nas.get("problem_count", 0)
        status = "all clear" if problems == 0 else f"{problems} problem(s)"
        lines.append(f"• NAS ({model}): CPU {cpu}%, RAM {ram}%, {vols} — {status}")

    # Network — latest from DB
    net_rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'infrastructure' AND text LIKE 'Network health%' "
        f"AND created_at >= '{TODAY}' ORDER BY created_at DESC LIMIT 1"
    )
    if net_rows:
        row = net_rows[0]
        # "Network health check 2026-04-22 16:23: WAN ok ..."
        # Split on the time pattern to get just the status part
        if "WAN" in row:
            content = row[row.index("WAN"):]
            lines.append(f"• Network: {content}")
        elif ":" in row:
            content = row.split(":", 1)[1].strip()
            lines.append(f"• Network: {content}")

    # App outages
    app_rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'app_watchdog' AND created_at >= '{TODAY}' "
        f"ORDER BY created_at"
    )
    if app_rows:
        downs = [r for r in app_rows if "went down" in r]
        recoveries = [r for r in app_rows if "recovered" in r]
        if downs:
            apps_affected = set()
            for d in downs:
                # "OneOnOne went down on DATE at TIME..."
                app_name = d.split(" went down")[0].strip()
                apps_affected.add(app_name)
            lines.append(
                f"• App outages: {', '.join(sorted(apps_affected))} "
                f"({len(downs)} down, {len(recoveries)} recovered)"
            )
    else:
        lines.append("• Apps: no outages")

    if not lines:
        return None
    return "*Infrastructure:*\n" + "\n".join(lines)


def section_security():
    count = _query_field(
        f"SELECT count(*) FROM memories "
        f"WHERE source = 'security' AND created_at >= '{TODAY}'"
    )
    if not count or count == "0":
        return None

    # Get unique cameras with events
    cam_rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'security' AND created_at >= '{TODAY}'"
    )
    cameras = {}
    for row in cam_rows:
        # "Protect event on Exterior - Front Right: ..."
        if "Protect event on " in row:
            cam = row.split("Protect event on ", 1)[1].split(":")[0].strip()
            cameras[cam] = cameras.get(cam, 0) + 1

    lines = [f"• {int(count)} Protect events across {len(cameras)} cameras"]
    if cameras:
        top = sorted(cameras.items(), key=lambda x: -x[1])[:5]
        for cam, n in top:
            lines.append(f"  - {cam}: {n} events")

    return "*Security Cameras:*\n" + "\n".join(lines)


def section_local_news():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source IN ('local', 'burbank') AND created_at >= '{TODAY}' "
        f"ORDER BY created_at LIMIT 10"
    )
    if not rows:
        return None

    highlights = []
    for row in rows:
        # "Reddit r/glendale: Title\nFlair: ...\nScore: N, Comments: N ..."
        first_line = row.split("\n")[0].strip()
        if "Reddit r/" in first_line:
            # Extract sub and title
            after_reddit = first_line.split("Reddit ", 1)[1]
            highlights.append(f"• {after_reddit}")
        if len(highlights) >= 5:
            break

    local_count = len([r for r in rows if "r/glendale" in r.lower()])
    burbank_count = len([r for r in rows if "r/burbank" in r.lower()])

    header = f"*Local News ({local_count} Glendale, {burbank_count} Burbank):*"
    return header + "\n" + "\n".join(highlights)


def section_dream():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'dream' AND created_at >= '{TODAY}' "
        f"ORDER BY created_at LIMIT 1"
    )
    if not rows:
        return None
    text = rows[0]
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    # Truncate to first ~200 chars
    if len(text) > 200:
        text = text[:200].rsplit(" ", 1)[0] + "..."
    return f"*Dream Journal:*\n_{text}_"


def section_this_day():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'history' AND created_at >= '{TODAY}' "
        f"ORDER BY created_at LIMIT 3"
    )
    if not rows:
        return None
    lines = []
    for row in rows:
        # "On this day (April 22), 1944: ..."
        if "On this day" in row:
            after = row.split(")," , 1)
            if len(after) == 2:
                year_event = after[1].strip()
                if len(year_event) > 120:
                    year_event = year_event[:120].rsplit(" ", 1)[0] + "..."
                lines.append(f"• {year_event}")
        elif "Born on" in row:
            if len(row) > 120:
                row = row[:120].rsplit(" ", 1)[0] + "..."
            lines.append(f"• {row}")
        if len(lines) >= 2:
            break
    if not lines:
        return None
    return "*On This Day:*\n" + "\n".join(lines)


def section_scheduler_health():
    state = _load_state("../config/scheduler_state.json")
    if not state:
        return None

    tasks = state.get("tasks", {})
    failures = {}
    for name, info in tasks.items():
        consec = info.get("consecutive_failures", 0)
        if consec >= 3:
            failures[name] = consec

    total_runs = sum(t.get("run_count", 0) for t in tasks.values())

    if failures:
        lines = [f"• Scheduler: {total_runs} total runs, {len(failures)} tasks struggling:"]
        for name, count in sorted(failures.items(), key=lambda x: -x[1]):
            lines.append(f"  - {name}: {count} consecutive failures")
        return "*Scheduler:*\n" + "\n".join(lines)
    else:
        return f"*Scheduler:*\n• {total_runs} total runs today, all tasks healthy"


def generate_journal():
    sections = [
        f"*Nova Daily Journal — {TODAY}*",
        section_morning_brief(),
        section_calendar(),
        section_infrastructure(),
        section_security(),
        section_scheduler_health(),
        section_local_news(),
        section_dream(),
        section_this_day(),
    ]

    parts = [s for s in sections if s]

    if len(parts) <= 1:
        parts.append("_Quiet day — no significant events recorded._")

    return "\n\n".join(parts)


def slack_post(text):
    data = json.dumps({
        "channel": SLACK_CHAN, "text": text, "mrkdwn": True
    }).encode()
    req_cmd = [
        "curl", "-s", "-X", "POST",
        f"{SLACK_API}/chat.postMessage",
        "-H", f"Authorization: Bearer {SLACK_TOKEN}",
        "-H", "Content-Type: application/json; charset=utf-8",
        "-d", data.decode(),
    ]
    try:
        subprocess.run(req_cmd, capture_output=True, timeout=15)
        logging.info("Daily journal sent to Slack")
    except Exception as e:
        logging.error(f"Slack post failed: {e}")


if __name__ == "__main__":
    logging.info("Starting daily journal generation")
    try:
        journal = generate_journal()
        print(journal)
        print()
        slack_post(journal)
    except Exception as e:
        logging.error(f"Journal generation failed: {e}")
        slack_post(f"Nova Daily Journal failed: {e}")
    logging.info("Daily journal process completed")
