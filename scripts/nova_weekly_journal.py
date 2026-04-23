#!/usr/bin/env python3

"""
Nova Weekly Journal — posted to Slack every Sunday at 5 PM PT.

Summarizes the past 7 days from Postgres (nova_memories):
memory volume by source, infra health trends, security camera
activity, app uptime, local news highlights, dream log, and
notable email volume.

Written by Jordan Koch.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.expanduser("~/.openclaw/logs/weekly-journal.log")),
        logging.StreamHandler(),
    ],
)

NOW = datetime.now()
TODAY = NOW.strftime("%Y-%m-%d")
WEEK_AGO = (NOW - timedelta(days=7)).strftime("%Y-%m-%d")
DB = "nova_memories"
STATE_DIR = Path.home() / ".openclaw/workspace/state"


def _query(sql):
    try:
        result = subprocess.run(
            ["psql", "-U", "kochj", "-d", DB, "-tAc", sql],
            capture_output=True, text=True, timeout=15,
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


def section_memory_volume():
    total = _query_field(
        f"SELECT count(*) FROM memories WHERE created_at >= '{WEEK_AGO}'"
    )
    rows = _query(
        f"SELECT source, count(*) as c FROM memories "
        f"WHERE created_at >= '{WEEK_AGO}' "
        f"GROUP BY source ORDER BY c DESC LIMIT 10"
    )
    if not rows:
        return None

    lines = [f"• {int(total or 0):,} new memories this week"]
    for row in rows:
        parts = row.rsplit("|", 1)
        if len(parts) == 2:
            source = parts[0].strip()
            count = int(parts[1].strip())
            lines.append(f"  - {source}: {count:,}")

    return "*Memory Volume:*\n" + "\n".join(lines)


def section_infra_summary():
    lines = []

    # NAS current state
    nas = _load_state("nova_synology_state.json")
    if nas:
        model = nas.get("model", "NAS")
        vols = nas.get("volumes", "?")
        problems = nas.get("problem_count", 0)
        status = "healthy" if problems == 0 else f"{problems} problem(s)"
        lines.append(f"• NAS ({model}): {vols} — {status}")

    # NAS health checks this week — any problems?
    nas_problems = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'infrastructure' AND text LIKE 'NAS health%' "
        f"AND text NOT LIKE '%0 problems' "
        f"AND created_at >= '{WEEK_AGO}' "
        f"ORDER BY created_at LIMIT 5"
    )
    if nas_problems:
        lines.append(f"• NAS issues detected on {len(nas_problems)} check(s):")
        for row in nas_problems[:3]:
            if ":" in row:
                content = row.split(":", 1)[1].strip()
                if len(content) > 100:
                    content = content[:100] + "..."
                lines.append(f"  - {content}")
    else:
        check_count = _query_field(
            f"SELECT count(*) FROM memories "
            f"WHERE source = 'infrastructure' AND text LIKE 'NAS health%' "
            f"AND created_at >= '{WEEK_AGO}'"
        )
        lines.append(f"• NAS: {check_count or 0} health checks, all clear")

    # Network checks
    net_problems = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'infrastructure' AND text LIKE 'Network health%' "
        f"AND text NOT LIKE '%0 problems' "
        f"AND created_at >= '{WEEK_AGO}' "
        f"ORDER BY created_at LIMIT 5"
    )
    net_count = _query_field(
        f"SELECT count(*) FROM memories "
        f"WHERE source = 'infrastructure' AND text LIKE 'Network health%' "
        f"AND created_at >= '{WEEK_AGO}'"
    )
    if net_problems:
        lines.append(f"• Network: {len(net_problems)} issue(s) out of {net_count or '?'} checks")
    else:
        lines.append(f"• Network: {net_count or 0} checks, all clear")

    if not lines:
        return None
    return "*Infrastructure:*\n" + "\n".join(lines)


def section_app_health():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'app_watchdog' AND created_at >= '{WEEK_AGO}' "
        f"ORDER BY created_at"
    )
    if not rows:
        return "*App Health:*\n• No outages this week"

    downs = [r for r in rows if "went down" in r]
    recoveries = [r for r in rows if "recovered" in r]

    # Group by app
    app_outages = {}
    for d in downs:
        app_name = d.split(" went down")[0].strip()
        app_outages[app_name] = app_outages.get(app_name, 0) + 1

    lines = [f"• {len(downs)} outage(s), {len(recoveries)} recovery(ies)"]
    for app, count in sorted(app_outages.items(), key=lambda x: -x[1]):
        lines.append(f"  - {app}: {count} outage(s)")

    return "*App Health:*\n" + "\n".join(lines)


def section_security():
    # Daily breakdown
    rows = _query(
        f"SELECT date(created_at AT TIME ZONE 'America/Los_Angeles') as day, count(*) "
        f"FROM memories WHERE source = 'security' AND created_at >= '{WEEK_AGO}' "
        f"GROUP BY day ORDER BY day"
    )
    total = _query_field(
        f"SELECT count(*) FROM memories "
        f"WHERE source = 'security' AND created_at >= '{WEEK_AGO}'"
    )
    if not total or total == "0":
        return None

    # Top cameras
    cam_rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'security' AND created_at >= '{WEEK_AGO}'"
    )
    cameras = {}
    for row in cam_rows:
        if "Protect event on " in row:
            cam = row.split("Protect event on ", 1)[1].split(":")[0].strip()
            cameras[cam] = cameras.get(cam, 0) + 1

    lines = [f"• {int(total):,} Protect events across {len(cameras)} cameras"]

    # Daily totals
    if rows:
        day_parts = []
        for row in rows:
            parts = row.rsplit("|", 1)
            if len(parts) == 2:
                day = parts[0].strip()
                count = int(parts[1].strip())
                short_day = datetime.strptime(day, "%Y-%m-%d").strftime("%a")
                day_parts.append(f"{short_day}: {count}")
        lines.append(f"• Daily: {', '.join(day_parts)}")

    # Top 5 cameras
    if cameras:
        top = sorted(cameras.items(), key=lambda x: -x[1])[:5]
        lines.append("• Busiest cameras:")
        for cam, n in top:
            lines.append(f"  - {cam}: {n}")

    return "*Security Cameras:*\n" + "\n".join(lines)


def section_local_news():
    glendale_count = _query_field(
        f"SELECT count(*) FROM memories "
        f"WHERE source = 'local' AND created_at >= '{WEEK_AGO}'"
    )
    burbank_count = _query_field(
        f"SELECT count(*) FROM memories "
        f"WHERE source = 'burbank' AND created_at >= '{WEEK_AGO}'"
    )
    g = int(glendale_count or 0)
    b = int(burbank_count or 0)
    if g + b == 0:
        return None

    # Top posts — query score from within the text field
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source IN ('local', 'burbank') AND created_at >= '{WEEK_AGO}' "
        f"AND text LIKE '%Score:%' "
        f"ORDER BY created_at DESC LIMIT 100"
    )

    # psql -tAc preserves newlines within a field, so rejoin and re-split on "Reddit r/"
    raw = "\n".join(rows)
    posts = [("Reddit r/" + chunk) for chunk in raw.split("Reddit r/")[1:]]

    scored = []
    for post in posts:
        first_line = post.split("\n")[0].strip()
        score = 0
        for line in post.split("\n"):
            if line.strip().startswith("Score:"):
                try:
                    score = int(line.strip().split("Score:")[1].strip().split(",")[0].strip())
                except (ValueError, IndexError):
                    pass
                break
        title = first_line
        if len(title) > 80:
            title = title[:80].rsplit(" ", 1)[0] + "..."
        scored.append((score, title))

    # Deduplicate by title, keeping highest score
    seen = {}
    for score, title in scored:
        if title not in seen or score > seen[title]:
            seen[title] = score
    deduped = sorted(seen.items(), key=lambda x: -x[1])
    top = deduped[:5]

    lines = [f"• {g} Glendale + {b} Burbank posts ingested"]
    if top:
        lines.append("• Top posts:")
        for title, score in top:
            lines.append(f"  - {title} ({score} upvotes)")

    return "*Local News:*\n" + "\n".join(lines)


def section_dreams():
    rows = _query(
        f"SELECT text FROM memories "
        f"WHERE source = 'dream' AND created_at >= '{WEEK_AGO}' "
        f"ORDER BY created_at"
    )
    if not rows:
        return None

    lines = [f"• {len(rows)} dream(s) recorded"]
    for row in rows:
        text = row
        if ":" in text:
            text = text.split(":", 1)[1].strip()
        if len(text) > 150:
            text = text[:150].rsplit(" ", 1)[0] + "..."
        lines.append(f"  - _{text}_")

    return "*Dream Log:*\n" + "\n".join(lines)


def section_email_volume():
    rows = _query(
        f"SELECT date(created_at AT TIME ZONE 'America/Los_Angeles') as day, count(*) "
        f"FROM memories WHERE source = 'email' AND created_at >= '{WEEK_AGO}' "
        f"GROUP BY day ORDER BY day"
    )
    total = _query_field(
        f"SELECT count(*) FROM memories "
        f"WHERE source = 'email' AND created_at >= '{WEEK_AGO}'"
    )
    if not total or total == "0":
        return None

    lines = [f"• {int(total):,} emails processed"]
    if rows:
        day_parts = []
        for row in rows:
            parts = row.rsplit("|", 1)
            if len(parts) == 2:
                day = parts[0].strip()
                count = int(parts[1].strip())
                short_day = datetime.strptime(day, "%Y-%m-%d").strftime("%a")
                day_parts.append(f"{short_day}: {count}")
        lines.append(f"• Daily: {', '.join(day_parts)}")

    return "*Email Volume:*\n" + "\n".join(lines)


def section_scheduler():
    state = _load_state("../config/scheduler_state.json")
    if not state:
        return None

    tasks = state.get("tasks", {})
    total_runs = sum(t.get("run_count", 0) for t in tasks.values())
    failures = {
        name: info.get("consecutive_failures", 0)
        for name, info in tasks.items()
        if info.get("consecutive_failures", 0) >= 3
    }

    lines = [f"• {total_runs:,} total task runs, {len(tasks)} tasks configured"]
    if failures:
        lines.append(f"• {len(failures)} task(s) with recurring failures:")
        for name, count in sorted(failures.items(), key=lambda x: -x[1]):
            lines.append(f"  - {name}: {count} consecutive failures")
    else:
        lines.append("• All tasks healthy")

    return "*Scheduler:*\n" + "\n".join(lines)


def generate_weekly():
    week_start = (NOW - timedelta(days=7)).strftime("%b %d")
    week_end = NOW.strftime("%b %d, %Y")

    sections = [
        f"*Nova Weekly Journal — {week_start} to {week_end}*",
        section_memory_volume(),
        section_infra_summary(),
        section_app_health(),
        section_security(),
        section_email_volume(),
        section_scheduler(),
        section_local_news(),
        section_dreams(),
    ]

    parts = [s for s in sections if s]

    if len(parts) <= 1:
        parts.append("_Quiet week — no significant events recorded._")

    return "\n\n".join(parts)


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_CHAN)


if __name__ == "__main__":
    logging.info("Starting weekly journal generation")
    try:
        journal = generate_weekly()
        print(journal)
        print()
        slack_post(journal)
    except Exception as e:
        logging.error(f"Weekly journal generation failed: {e}")
        slack_post(f"Nova Weekly Journal failed: {e}")
    logging.info("Weekly journal process completed")
