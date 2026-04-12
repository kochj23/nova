#!/usr/bin/env python3
"""
nova_morning_brief.py — Nova's proactive 7am morning briefing.

HAL: "Good morning, Dave. I've just picked up a fault in the AE-35 unit."

Pulls: weather, email priorities, meetings, GitHub, system status.
Posts to Slack AND speaks through Jordan's bedroom HomePod.

Cron: 7am PT daily
Written by Jordan Koch.
"""

import json
import re
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, date
from pathlib import Path
import nova_config

SLACK_TOKEN  = nova_config.slack_bot_token()
SLACK_CHAN   = "C0AMNQ5GX70"
SLACK_API    = "https://slack.com/api"
SCRIPTS      = Path.home() / ".openclaw" / "scripts"
WORKSPACE    = Path.home() / ".openclaw" / "workspace"
MEMORY_DIR   = WORKSPACE / "memory"
VECTOR_URL   = "http://127.0.0.1:18790/remember"
TODAY        = date.today().isoformat()
NOW          = datetime.now()

# Voice output disabled — was randomly triggering during meetings (2026-04-09)


def log(msg):
    print(f"[nova_morning_brief {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Slack ─────────────────────────────────────────────────────────────────────

def slack_post(text):
    data = json.dumps({"channel": SLACK_CHAN, "text": text, "mrkdwn": True}).encode()
    req  = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


# ── HomePod TTS (DISABLED 2026-04-09 — randomly triggering during meetings) ──


# ── Weather ───────────────────────────────────────────────────────────────────

def get_weather():
    try:
        req = urllib.request.Request(
            "https://wttr.in/burbank,ca?format=%C+%t+feels+%f+humidity+%h",
            headers={"User-Agent": "curl/7.0"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode().strip()
        # Parse to human-friendly
        temp_match = re.search(r"([+-]?\d+)°C", raw)
        if temp_match:
            c = int(temp_match.group(1))
            f = round(c * 9/5 + 32)
            raw = raw.replace(temp_match.group(0), f"{f}°F")
        return raw
    except Exception as e:
        return f"weather unavailable ({e})"


# ── Email priorities ──────────────────────────────────────────────────────────

def get_email_priorities():
    """Pull HIGH priority items from last night's memory file."""
    try:
        mem_file = MEMORY_DIR / f"{TODAY}.md"
        # Try yesterday if today's not written yet (brief runs before nightly report)
        if not mem_file.exists():
            from datetime import timedelta
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            mem_file = MEMORY_DIR / f"{yesterday}.md"

        if not mem_file.exists():
            return []

        content = mem_file.read_text(encoding="utf-8")
        highs = []
        for line in content.splitlines():
            if "🔴 HIGH" in line or "HIGH" in line:
                clean = re.sub(r"[*_🔴]", "", line).strip()
                if clean:
                    highs.append(clean[:120])
        return highs[:3]
    except Exception:
        return []


# ── Calendar events today (via nova_calendar.py) ────────────────────────────

def get_calendar_events():
    """Pull today's events from all calendar accounts via nova_calendar.py."""
    try:
        from nova_calendar import get_todays_events, format_time
        events = get_todays_events()
        formatted = []
        for e in events:
            if e.get("raw"):
                formatted.append(e.get("title", "")[:60])
                continue
            title = e.get("title", "Untitled")
            if e.get("allDay"):
                formatted.append(f"(all day) {title[:55]}")
            else:
                start = e.get("start", "")
                time_str = format_time(start) if start else ""
                formatted.append(f"{time_str} {title[:50]}".strip())
        return formatted
    except Exception as e:
        log(f"Calendar import error: {e} — falling back to OneOnOne")
        return get_meetings_oneonone()


def get_meetings_oneonone():
    """Fallback: get meetings from OneOnOne app."""
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "2", "http://127.0.0.1:37421/api/oneonone/meetings?limit=5"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []

        data = json.loads(r.stdout)
        meetings = data if isinstance(data, list) else data.get("meetings", [])
        today_meetings = []
        for m in meetings:
            date_str = m.get("date", "") or m.get("startTime", "") or m.get("created_at", "")
            if TODAY in str(date_str):
                title = m.get("title") or m.get("name") or "Meeting"
                today_meetings.append(title[:60])
        return today_meetings[:3]
    except Exception:
        return []


# Legacy alias for backward compatibility
get_meetings = get_calendar_events


# ── GitHub overnight ──────────────────────────────────────────────────────────

def get_github_overnight():
    """Check for new stars, issues, or PRs on focus projects since yesterday."""
    focus_repos = ["kochj23/MLXCode", "kochj23/NMAPScanner", "kochj23/RsyncGUI"]
    notes = []
    try:
        for repo in focus_repos:
            r = subprocess.run(
                ["gh", "repo", "view", repo, "--json", "stargazerCount,openIssues"],
                capture_output=True, text=True, timeout=8
            )
            if r.returncode == 0 and r.stdout.strip():
                d = json.loads(r.stdout)
                stars = d.get("stargazerCount", 0)
                issues = d.get("openIssues", {})
                issue_count = issues.get("totalCount", 0) if isinstance(issues, dict) else 0
                short_name = repo.split("/")[1]
                if issue_count > 0:
                    notes.append(f"{short_name}: {stars} stars, {issue_count} open issues")
                else:
                    notes.append(f"{short_name}: {stars} stars")
    except Exception:
        pass
    return notes


# ── System health ─────────────────────────────────────────────────────────────

def get_system_health():
    """Quick check: memory server up, key apps running."""
    issues = []
    try:
        r = urllib.request.urlopen("http://127.0.0.1:18790/health", timeout=3)
        data = json.loads(r.read())
        mem_count = data.get("count", 0)
    except Exception:
        issues.append("vector memory server is down")
        mem_count = 0

    for port, name in [(37432, "HomekitControl"), (37421, "OneOnOne")]:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2)
        except Exception:
            issues.append(f"{name} app is not running")

    return issues, mem_count


# ── Vector memory ─────────────────────────────────────────────────────────────

def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({"text": text, "source": "morning_brief",
                              "metadata": metadata or {}}).encode()
        req = urllib.request.Request(VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Building morning brief...")
    day_name = NOW.strftime("%A")
    date_fmt  = NOW.strftime("%B %d")

    weather    = get_weather()
    emails     = get_email_priorities()
    meetings   = get_calendar_events()
    gh_notes   = get_github_overnight()
    issues, mem_count = get_system_health()

    # ── Spoken brief (concise, warm, HomePod-friendly) ──
    spoken_parts = [
        f"Good morning Jordan. It's {day_name}, {date_fmt}.",
        f"In Burbank: {weather}.",
    ]
    if emails:
        spoken_parts.append(
            f"You have {len(emails)} high priority email{'s' if len(emails) > 1 else ''}. "
            + ("Top item: " + emails[0].split(":")[-1].strip()[:80] if emails else "")
        )
    else:
        spoken_parts.append("No urgent emails overnight.")

    if meetings:
        spoken_parts.append(
            f"You have {len(meetings)} meeting{'s' if len(meetings) > 1 else ''} today: "
            + ", ".join(meetings) + "."
        )
    else:
        spoken_parts.append("No meetings on the calendar today.")

    if issues:
        spoken_parts.append("One thing: " + ", ".join(issues) + ".")

    spoken_parts.append("Have a good one.")
    spoken_brief = " ".join(spoken_parts)

    # ── Slack brief (richer, with GitHub) ──
    slack_lines = [
        f"*🌅 Good morning, Jordan — {day_name}, {date_fmt}*",
        f"🌤 *Weather:* {weather}",
        "",
    ]
    if emails:
        slack_lines.append("*🔴 Priority emails:*")
        for e in emails:
            slack_lines.append(f"  • {e}")
        slack_lines.append("")
    else:
        slack_lines.append("📭 No urgent emails overnight.")
        slack_lines.append("")

    if meetings:
        slack_lines.append("*📅 Meetings today:*")
        for m in meetings:
            slack_lines.append(f"  • {m}")
        slack_lines.append("")

    if gh_notes:
        slack_lines.append("*🐙 GitHub (focus projects):*")
        for n in gh_notes:
            slack_lines.append(f"  • {n}")
        slack_lines.append("")

    if issues:
        slack_lines.append("*⚠️ System alerts:*")
        for i in issues:
            slack_lines.append(f"  • {i}")
        slack_lines.append("")

    slack_lines.append(f"_Vector memory: {mem_count} memories stored_")
    slack_lines.append("_— Nova_")

    # Deliver
    log("Posting to Slack...")
    slack_post("\n".join(slack_lines))

    # Voice output disabled (2026-04-09)

    # Store brief in vector memory
    summary = f"Morning brief {TODAY}: {weather}. Emails: {len(emails)} urgent. Meetings: {', '.join(meetings) or 'none'}. GitHub: {'; '.join(gh_notes) or 'no activity'}."
    vector_remember(summary, {"date": TODAY, "type": "morning_brief"})

    log("Done.")


if __name__ == "__main__":
    main()
