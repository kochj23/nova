#!/usr/bin/env python3
"""
nova_journal_stats_poller.py — Collect and persist nova-journal statistics.

Runs every 6h via scheduler. Combines:
  - GitHub Traffic API (views, uniques, top paths, referrers)
  - GitHub Actions run history (deploy feed)
  - Local filesystem scan (post counts, word counts, coverage)
  - Scheduler state (last run time per content job)

Persists to: ~/.openclaw/workspace/state/journal_stats.json
History persisted to: ~/.openclaw/workspace/state/journal_traffic_history.json
  (appends daily buckets so we survive the GitHub 14-day cliff)

Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

STATS_FILE   = Path.home() / ".openclaw/workspace/state/journal_stats.json"
HISTORY_FILE = Path.home() / ".openclaw/workspace/state/journal_traffic_history.json"
CONTENT_DIR  = Path("/Volumes/Data/xcode/nova-journal/content")
REPO         = "kochj23/nova-journal"
LOG_FILE     = Path.home() / ".openclaw/logs/journal_stats_poller.log"

SECTIONS = ["dreams", "essays", "opinions", "after-dark", "tech-today", "research", "digests"]

# Section → (scheduler task id, cron hour, cron minute)
SECTION_SCHEDULES = {
    "dreams":     ("daily_journal",   21, 15),
    "essays":     ("daily_essay",      9,  0),
    "opinions":   ("daily_opinion",   12,  0),
    "after-dark": ("after_dark",      20,  0),
    "tech-today": ("tech_today",      23, 30),
    "research":   ("research_paper",  23, 50),
    "digests":    ("daily_digest",    17,  0),
}


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _gh(endpoint: str) -> dict | list | None:
    """Call GitHub API via gh CLI (uses its stored token)."""
    try:
        r = subprocess.run(
            ["gh", "api", f"/repos/{REPO}/{endpoint}"],
            capture_output=True, text=True, timeout=20
        )
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception as e:
        log(f"gh api {endpoint} failed: {e}")
    return None


def fetch_traffic() -> dict:
    """Fetch views + uniques from GitHub Traffic API."""
    data = _gh("traffic/views") or {}
    return {
        "total_count":   data.get("count", 0),
        "total_uniques": data.get("uniques", 0),
        "days": [
            {
                "date":    v["timestamp"][:10],
                "count":   v["count"],
                "uniques": v["uniques"],
            }
            for v in data.get("views", [])
            if v["count"] > 0
        ],
    }


def fetch_top_paths() -> list[dict]:
    """Fetch top 10 popular paths."""
    raw = _gh("traffic/popular/paths") or []
    result = []
    for p in raw:
        path = p.get("path", "")
        # Infer section from path like /essays/2026-05-09-...
        section = "other"
        for s in SECTIONS:
            if f"/{s}/" in path or path.endswith(f"/{s}"):
                section = s
                break
        result.append({
            "path":    path,
            "title":   p.get("title", path),
            "count":   p.get("count", 0),
            "uniques": p.get("uniques", 0),
            "section": section,
        })
    return result


def fetch_referrers() -> list[dict]:
    """Fetch top referrer sources."""
    raw = _gh("traffic/popular/referrers") or []
    return [
        {"referrer": r.get("referrer", ""), "count": r.get("count", 0), "uniques": r.get("uniques", 0)}
        for r in raw
    ]


def fetch_recent_deploys(limit: int = 10) -> list[dict]:
    """Fetch recent GitHub Actions deploy runs."""
    try:
        r = subprocess.run(
            ["gh", "run", "list", "--repo", REPO, f"--limit={limit}",
             "--json", "createdAt,conclusion,displayTitle,databaseId,url"],
            capture_output=True, text=True, timeout=20
        )
        if r.returncode == 0:
            runs = json.loads(r.stdout)
            return [
                {
                    "id":         run.get("databaseId"),
                    "title":      run.get("displayTitle", "")[:70],
                    "conclusion": run.get("conclusion", "unknown"),
                    "created_at": run.get("createdAt", ""),
                    "url":        run.get("url", ""),
                }
                for run in runs
            ]
    except Exception as e:
        log(f"Deploy history fetch failed: {e}")
    return []


def scan_content() -> dict:
    """Scan local filesystem for content stats per section."""
    now = datetime.now(timezone.utc)
    sections_data = {}

    for section in SECTIONS:
        d = CONTENT_DIR / section
        if not d.exists():
            sections_data[section] = {"post_count": 0, "coverage_7d": [False]*7, "latest_ts": None, "latest_title": ""}
            continue

        posts = [f for f in d.glob("*.md") if f.name != "_index.md"]
        days_with_posts: dict[str, dict] = {}  # date_str -> {count, title, words}
        latest_ts = 0.0
        latest_title = ""

        for f in posts:
            try:
                text = f.read_text(errors="replace")
                parts = text.split("---", 2)
                body = parts[2] if len(parts) >= 3 else text
                words = len(body.split())

                m_date = re.search(r'^date:\s*["\']?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', text, re.MULTILINE)
                m_title = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
                title = m_title.group(1).strip().strip('"\'') if m_title else f.stem

                if m_date:
                    dt = datetime.fromisoformat(m_date.group(1))
                    ts = dt.timestamp()
                    date_str = m_date.group(1)[:10]

                    if date_str not in days_with_posts:
                        days_with_posts[date_str] = {"count": 0, "title": title, "words": 0}
                    days_with_posts[date_str]["count"] += 1
                    days_with_posts[date_str]["words"] += words

                    if ts > latest_ts:
                        latest_ts = ts
                        latest_title = title
            except Exception:
                continue

        # 7-day coverage (index 0 = today)
        coverage_7d = []
        coverage_titles = []
        for i in range(7):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            has = day in days_with_posts
            coverage_7d.append(has)
            coverage_titles.append(days_with_posts[day]["title"] if has else "")

        # Posts this week vs last week
        week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        prev_start = (now - timedelta(days=14)).strftime("%Y-%m-%d")
        posts_this_week = sum(1 for d, v in days_with_posts.items() if d >= week_start)
        posts_last_week = sum(1 for d, v in days_with_posts.items() if prev_start <= d < week_start)
        words_this_week = sum(v["words"] for d, v in days_with_posts.items() if d >= week_start)

        age_hours = (time.time() - latest_ts) / 3600 if latest_ts else 9999

        sections_data[section] = {
            "post_count":      len(posts),
            "coverage_7d":     coverage_7d,
            "coverage_titles": coverage_titles,
            "latest_ts":       latest_ts if latest_ts else None,
            "latest_title":    latest_title,
            "age_hours":       round(age_hours, 1),
            "posts_this_week": posts_this_week,
            "posts_last_week": posts_last_week,
            "words_this_week": words_this_week,
        }

    return sections_data


def get_scheduler_state() -> dict:
    """Read last-run times from scheduler state."""
    sched_file = Path.home() / ".openclaw/config/scheduler_state.json"
    try:
        raw = json.loads(sched_file.read_text())
        tasks = raw.get("tasks", {})
        result = {}
        for section, (task_id, hour, minute) in SECTION_SCHEDULES.items():
            task = tasks.get(task_id, {})
            result[section] = {
                "task_id":             task_id,
                "last_run_ts":         task.get("last_run"),
                "last_exit_code":      task.get("last_exit_code"),
                "consecutive_failures": task.get("consecutive_failures", 0),
                "run_count":           task.get("run_count", 0),
                "scheduled_hour":      hour,
                "scheduled_minute":    minute,
            }
        return result
    except Exception as e:
        log(f"Scheduler state read failed: {e}")
        return {}


def next_run_time(hour: int, minute: int) -> str:
    """Return ISO timestamp of the next scheduled run."""
    now = datetime.now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def update_history(traffic: dict):
    """Append today's traffic to the persistent history file."""
    history = {}
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Merge today's traffic days into history (upsert by date)
    for day in traffic.get("days", []):
        history[day["date"]] = {"count": day["count"], "uniques": day["uniques"]}

    # Also record today's cumulative snapshot even if count=0
    if today not in history:
        history[today] = {"count": 0, "uniques": 0}

    HISTORY_FILE.write_text(json.dumps(history, sort_keys=True, indent=2))
    return history


def build_schedule_panel() -> list[dict]:
    """Build today's content schedule with next-fire times."""
    items = []
    for section, (task_id, hour, minute) in SECTION_SCHEDULES.items():
        nxt = next_run_time(hour, minute)
        items.append({
            "section":  section,
            "task_id":  task_id,
            "fires_at": f"{hour:02d}:{minute:02d}",
            "next_run": nxt,
        })
    return sorted(items, key=lambda x: x["fires_at"])


def main():
    log("Starting journal stats poll...")

    traffic    = fetch_traffic()
    paths      = fetch_top_paths()
    referrers  = fetch_referrers()
    deploys    = fetch_recent_deploys(limit=15)
    content    = scan_content()
    sched      = get_scheduler_state()
    history    = update_history(traffic)
    schedule   = build_schedule_panel()

    # Aggregate totals across sections
    total_posts        = sum(v["post_count"] for v in content.values())
    total_this_week    = sum(v["posts_this_week"] for v in content.values())
    total_words_week   = sum(v["words_this_week"] for v in content.values())

    # Per-section traffic breakdown from top paths
    section_views: dict[str, int] = {s: 0 for s in SECTIONS}
    for p in paths:
        s = p["section"]
        if s in section_views:
            section_views[s] += p["count"]

    stats = {
        "polled_at":         datetime.now(timezone.utc).isoformat(),
        "traffic":           traffic,
        "traffic_history":   history,
        "top_paths":         paths,
        "referrers":         referrers,
        "recent_deploys":    deploys,
        "sections":          content,
        "scheduler":         sched,
        "schedule":          schedule,
        "section_views":     section_views,
        "totals": {
            "posts":             total_posts,
            "posts_this_week":   total_this_week,
            "words_this_week":   total_words_week,
        },
    }

    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATS_FILE.write_text(json.dumps(stats, indent=2, default=str))
    log(f"Stats written: {total_posts} posts, {traffic['total_count']} total views, {len(deploys)} deploys")


if __name__ == "__main__":
    main()
