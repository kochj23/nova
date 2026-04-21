#!/usr/bin/env python3
"""
nova_bujo.py — Nova's Bullet Journal system.

A structured rapid-logging system for tasks, events, and notes.
Integrates with Nova's vector memory, Slack notifications, and git.

"The BuJo is not about how it looks; it's about how it makes you think."
  — Ryder Carroll

Data stores in ~/.openclaw/bujo/:
  daily.json      — tasks, events, notes keyed by date
  monthly.json    — monthly goals, themes, reflections
  future.json     — forward-looking items by target month
  collections/    — custom categorized lists (birthdays.json, etc.)

Written by Jordan Koch.
"""

import argparse
import json
import os
import subprocess
import sys
import uuid
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

BUJO_DIR       = Path.home() / ".openclaw" / "bujo"
DAILY_FILE     = BUJO_DIR / "daily.json"
MONTHLY_FILE   = BUJO_DIR / "monthly.json"
FUTURE_FILE    = BUJO_DIR / "future.json"
COLLECTIONS    = BUJO_DIR / "collections"
SCRIPTS_DIR    = Path.home() / ".openclaw" / "scripts"

# ── Config (import nova_config for Slack/vector) ────────────────────────────

sys.path.insert(0, str(SCRIPTS_DIR))
try:
    import nova_config
    SLACK_TOKEN   = nova_config.slack_bot_token()
    SLACK_NOTIFY  = nova_config.SLACK_NOTIFY       # #nova-notifications
    VECTOR_URL    = nova_config.VECTOR_URL          # http://127.0.0.1:18790/remember
except ImportError:
    SLACK_TOKEN   = ""
    SLACK_NOTIFY  = "C0ATAF7NZG9"
    VECTOR_URL    = "http://127.0.0.1:18790/remember"

SLACK_API = "https://slack.com/api"
TODAY     = date.today().isoformat()
STALE_DAYS    = 5
STUCK_MIGRATES = 3


# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[nova_bujo {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Init ─────────────────────────────────────────────────────────────────────

def ensure_bujo_dir():
    """Create bujo directory structure and init git repo on first run."""
    BUJO_DIR.mkdir(parents=True, exist_ok=True)
    COLLECTIONS.mkdir(parents=True, exist_ok=True)

    for f in [DAILY_FILE, MONTHLY_FILE, FUTURE_FILE]:
        if not f.exists():
            f.write_text("{}\n")

    git_dir = BUJO_DIR / ".git"
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=str(BUJO_DIR),
                       capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=str(BUJO_DIR),
                       capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "feat(bujo): Initialize bullet journal"],
                       cwd=str(BUJO_DIR), capture_output=True)
        log("Initialized git repo in bujo/")


# ── Data I/O ─────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text().strip()
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        log(f"WARNING: Corrupt JSON in {path}, starting fresh")
        return {}


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def short_id() -> str:
    """Generate a human-friendly 8-char UUID."""
    return uuid.uuid4().hex[:8]


# ── Git ──────────────────────────────────────────────────────────────────────

def git_commit(message: str):
    """Stage all changes in bujo/ and commit."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(BUJO_DIR),
                       capture_output=True, check=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(BUJO_DIR), capture_output=True
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(BUJO_DIR), capture_output=True, check=True
            )
    except subprocess.CalledProcessError as e:
        log(f"Git commit failed: {e}")


# ── Vector Memory ────────────────────────────────────────────────────────────

def remember(text: str, tags: list | None = None):
    """Store a summary in Nova's vector memory."""
    payload = {
        "text": text,
        "source": "bujo",
        "metadata": {
            "privacy": "local-only",
            "tags": tags or [],
            "date": TODAY,
        },
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        VECTOR_URL, data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"Vector memory store failed (non-fatal): {e}")


# ── Slack ────────────────────────────────────────────────────────────────────

def slack_post(text: str, channel: str | None = None):
    """Post a message to Slack as Nova."""
    token = SLACK_TOKEN
    if not token:
        log("No Slack token available — skipping post")
        return
    chan = channel or SLACK_NOTIFY
    data = json.dumps({"channel": chan, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            if not resp.get("ok"):
                log(f"Slack error: {resp.get('error', 'unknown')}")
    except Exception as e:
        log(f"Slack post failed: {e}")


# ── Data Access Helpers ──────────────────────────────────────────────────────

def get_day(data: dict, day: str) -> dict:
    """Get or initialize a day's entry."""
    if day not in data:
        data[day] = {"tasks": [], "events": [], "notes": []}
    for key in ("tasks", "events", "notes"):
        if key not in data[day]:
            data[day][key] = []
    return data[day]


def find_task(data: dict, task_id: str) -> tuple:
    """Find a task by short ID across all dates. Returns (date_key, task_index, task)."""
    for day_key, day_data in data.items():
        for i, task in enumerate(day_data.get("tasks", [])):
            if task.get("id", "").startswith(task_id):
                return day_key, i, task
    return None, None, None


# ── Stale / Stuck Detection ─────────────────────────────────────────────────

def is_stale(task: dict) -> bool:
    """Task is stale if open for more than STALE_DAYS days."""
    if task.get("status") != "open":
        return False
    created = datetime.fromisoformat(task["created_at"]).date()
    return (date.today() - created).days > STALE_DAYS


def is_stuck(task: dict) -> bool:
    """Task is stuck if migrated STUCK_MIGRATES or more times."""
    return len(task.get("migration_history", [])) >= STUCK_MIGRATES


def get_all_open_tasks(data: dict) -> list:
    """Collect all open tasks across all dates."""
    results = []
    for day_key, day_data in data.items():
        for task in day_data.get("tasks", []):
            if task.get("status") == "open":
                results.append((day_key, task))
    return results


def get_stale_tasks(data: dict) -> list:
    """All stale tasks (open > STALE_DAYS)."""
    return [(d, t) for d, t in get_all_open_tasks(data) if is_stale(t)]


def get_stuck_tasks(data: dict) -> list:
    """All stuck tasks (migrated STUCK_MIGRATES+ times)."""
    return [(d, t) for d, t in get_all_open_tasks(data) if is_stuck(t)]


# ── Formatters ───────────────────────────────────────────────────────────────

PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "low": "⚪"}
STATUS_ICON   = {"open": "○", "completed": "✕", "cancelled": "—", "migrated": "→"}


def fmt_task(task: dict, show_date: bool = False) -> str:
    p = PRIORITY_ICON.get(task.get("priority", "low"), "⚪")
    s = STATUS_ICON.get(task.get("status", "open"), "?")
    tags = " ".join(f"#{t}" for t in task.get("tags", []))
    date_str = f" ({task.get('created_at', '')[:10]})" if show_date else ""
    flags = ""
    if is_stale(task):
        flags += " [STALE]"
    if is_stuck(task):
        flags += " [STUCK]"
    return f"  {s} {p} [{task['id'][:8]}] {task['title']}{date_str} {tags}{flags}".rstrip()


def fmt_event(event: dict) -> str:
    tags = " ".join(f"#{t}" for t in event.get("tags", []))
    notes = f" — {event['notes']}" if event.get("notes") else ""
    return f"  ● [{event['id'][:8]}] {event['title']} ({event['date']}) {tags}{notes}".rstrip()


def fmt_note(note: dict) -> str:
    tags = " ".join(f"#{t}" for t in note.get("tags", []))
    return f"  · [{note['id'][:8]}] {note['text']} {tags}".rstrip()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Commands
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_add(args):
    """Add a task, event, or note."""
    data = load_json(DAILY_FILE)

    if args.type == "task":
        day = get_day(data, TODAY)
        task = {
            "id": short_id(),
            "title": args.text,
            "priority": args.priority or "medium",
            "tags": args.tag or [],
            "status": "open",
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "migration_history": [],
        }
        day["tasks"].append(task)
        save_json(DAILY_FILE, data)
        git_commit(f"bujo: Add task — {task['title']}")
        remember(f"New task: {task['title']} (priority: {task['priority']})", task["tags"])
        print(f"Added task [{task['id']}]: {task['title']}")

    elif args.type == "event":
        event_date = args.date or TODAY
        day = get_day(data, event_date)
        event = {
            "id": short_id(),
            "title": args.text,
            "date": event_date,
            "tags": args.tag or [],
            "notes": "",
        }
        day["events"].append(event)
        save_json(DAILY_FILE, data)
        git_commit(f"bujo: Add event — {event['title']}")
        remember(f"Event on {event_date}: {event['title']}", event["tags"])
        print(f"Added event [{event['id']}]: {event['title']} on {event_date}")

    elif args.type == "note":
        day = get_day(data, TODAY)
        note = {
            "id": short_id(),
            "text": args.text,
            "tags": args.tag or [],
            "date": TODAY,
        }
        day["notes"].append(note)
        save_json(DAILY_FILE, data)
        git_commit(f"bujo: Add note")
        print(f"Added note [{note['id']}]: {note['text']}")


def cmd_complete(args):
    """Mark a task as completed."""
    data = load_json(DAILY_FILE)
    day_key, idx, task = find_task(data, args.task_id)
    if task is None:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)
    task["status"] = "completed"
    task["completed_at"] = datetime.now().isoformat()
    data[day_key]["tasks"][idx] = task
    save_json(DAILY_FILE, data)
    git_commit(f"bujo: Complete task — {task['title']}")
    remember(f"Completed task: {task['title']}", task.get("tags", []))
    print(f"Completed [{task['id'][:8]}]: {task['title']}")


def cmd_cancel(args):
    """Cancel a task."""
    data = load_json(DAILY_FILE)
    day_key, idx, task = find_task(data, args.task_id)
    if task is None:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)
    task["status"] = "cancelled"
    data[day_key]["tasks"][idx] = task
    save_json(DAILY_FILE, data)
    git_commit(f"bujo: Cancel task — {task['title']}")
    print(f"Cancelled [{task['id'][:8]}]: {task['title']}")


def cmd_migrate(args):
    """Migrate a task to a new date."""
    data = load_json(DAILY_FILE)
    day_key, idx, task = find_task(data, args.task_id)
    if task is None:
        print(f"Task {args.task_id} not found.")
        sys.exit(1)
    if task["status"] != "open":
        print(f"Task {args.task_id} is {task['status']}, cannot migrate.")
        sys.exit(1)

    target_date = args.to
    # Record migration
    task["migration_history"].append({
        "from": day_key,
        "to": target_date,
        "migrated_at": datetime.now().isoformat(),
    })
    task["status"] = "migrated"
    data[day_key]["tasks"][idx] = task

    # Create new open copy on target date
    new_task = {
        "id": task["id"],
        "title": task["title"],
        "priority": task["priority"],
        "tags": task["tags"],
        "status": "open",
        "created_at": task["created_at"],
        "completed_at": None,
        "migration_history": list(task["migration_history"]),
    }
    target_day = get_day(data, target_date)
    target_day["tasks"].append(new_task)

    save_json(DAILY_FILE, data)
    migrations = len(new_task["migration_history"])
    git_commit(f"bujo: Migrate task — {task['title']} → {target_date}")
    msg = f"Migrated [{task['id'][:8]}]: {task['title']} → {target_date} (migration #{migrations})"
    if migrations >= STUCK_MIGRATES:
        msg += f"\n  ⚠️  This task has been migrated {migrations} times — it's STUCK. Do it, delegate it, or drop it."
    print(msg)


def cmd_list(args):
    """List entries with optional filters."""
    data = load_json(DAILY_FILE)

    # Determine which dates to show
    if args.stale:
        # Show stale tasks across all dates
        stale = get_stale_tasks(data)
        stuck = get_stuck_tasks(data)
        if not stale and not stuck:
            print("No stale or stuck tasks. Clean slate.")
            return
        if stale:
            print(f"=== Stale Tasks (open > {STALE_DAYS} days) ===")
            for d, t in sorted(stale, key=lambda x: x[1].get("created_at", "")):
                print(fmt_task(t, show_date=True))
        if stuck:
            print(f"\n=== Stuck Tasks (migrated {STUCK_MIGRATES}+ times) ===")
            for d, t in sorted(stuck, key=lambda x: len(x[1].get("migration_history", [])), reverse=True):
                migs = len(t.get("migration_history", []))
                print(f"{fmt_task(t, show_date=True)}  [{migs} migrations]")
        return

    # Filter by date
    target_date = args.date or TODAY
    if target_date == "today":
        target_date = TODAY
    if target_date not in data:
        print(f"No entries for {target_date}.")
        return

    day_data = data[target_date]
    status_filter = args.status
    tag_filter = args.tag_filter

    print(f"=== {target_date} ===")

    # Tasks
    tasks = day_data.get("tasks", [])
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    if tag_filter:
        tasks = [t for t in tasks if tag_filter in t.get("tags", [])]
    if tasks:
        print("\nTasks:")
        for t in tasks:
            print(fmt_task(t))

    # Events
    events = day_data.get("events", [])
    if tag_filter:
        events = [e for e in events if tag_filter in e.get("tags", [])]
    if events:
        print("\nEvents:")
        for e in events:
            print(fmt_event(e))

    # Notes
    notes = day_data.get("notes", [])
    if tag_filter:
        notes = [n for n in notes if tag_filter in n.get("tags", [])]
    if notes:
        print("\nNotes:")
        for n in notes:
            print(fmt_note(n))

    if not tasks and not events and not notes:
        print("  (empty)")


def cmd_stale(args):
    """Show all stale and stuck tasks."""
    data = load_json(DAILY_FILE)
    stale = get_stale_tasks(data)
    stuck = get_stuck_tasks(data)

    if not stale and not stuck:
        print("No stale or stuck tasks. Clean slate.")
        return

    if stale:
        print(f"=== Stale Tasks (open > {STALE_DAYS} days) ===")
        for d, t in sorted(stale, key=lambda x: x[1].get("created_at", "")):
            age = (date.today() - datetime.fromisoformat(t["created_at"]).date()).days
            print(f"{fmt_task(t, show_date=True)}  [{age}d old]")

    if stuck:
        print(f"\n=== Stuck Tasks (migrated {STUCK_MIGRATES}+ times) ===")
        for d, t in sorted(stuck, key=lambda x: len(x[1].get("migration_history", [])), reverse=True):
            migs = len(t.get("migration_history", []))
            print(f"{fmt_task(t, show_date=True)}  [{migs} migrations]")

    total = len(stale) + len(stuck)
    print(f"\n{total} task(s) need attention.")


def cmd_month(args):
    """Manage monthly goals, themes, and reflections."""
    data = load_json(MONTHLY_FILE)
    month_key = date.today().strftime("%Y-%m")

    if month_key not in data:
        data[month_key] = {"goals": [], "themes": [], "reflections": []}

    if args.goal:
        data[month_key]["goals"].append({
            "text": args.goal,
            "added": datetime.now().isoformat(),
            "status": "active",
        })
        save_json(MONTHLY_FILE, data)
        git_commit(f"bujo: Monthly goal — {args.goal}")
        remember(f"Monthly goal ({month_key}): {args.goal}", ["monthly", "goal"])
        print(f"Added monthly goal: {args.goal}")

    elif args.theme:
        data[month_key]["themes"].append({
            "text": args.theme,
            "added": datetime.now().isoformat(),
        })
        save_json(MONTHLY_FILE, data)
        git_commit(f"bujo: Monthly theme — {args.theme}")
        print(f"Set monthly theme: {args.theme}")

    elif args.reflect:
        data[month_key]["reflections"].append({
            "text": args.reflect,
            "date": TODAY,
        })
        save_json(MONTHLY_FILE, data)
        git_commit(f"bujo: Monthly reflection")
        remember(f"Monthly reflection ({month_key}): {args.reflect}", ["monthly", "reflection"])
        print(f"Added reflection for {month_key}.")

    else:
        # Display current month
        m = data.get(month_key, {})
        print(f"=== {month_key} ===")
        if m.get("themes"):
            print("\nThemes:")
            for t in m["themes"]:
                print(f"  ◆ {t['text']}")
        if m.get("goals"):
            print("\nGoals:")
            for g in m["goals"]:
                status = "✓" if g.get("status") == "done" else "○"
                print(f"  {status} {g['text']}")
        if m.get("reflections"):
            print("\nReflections:")
            for r in m["reflections"]:
                print(f"  — {r['text']} ({r['date']})")
        if not m.get("themes") and not m.get("goals") and not m.get("reflections"):
            print("  (nothing logged yet)")


def cmd_future(args):
    """Add a forward-looking item to a target month."""
    data = load_json(FUTURE_FILE)
    month = args.month
    if month not in data:
        data[month] = []

    item = {
        "id": short_id(),
        "text": args.text,
        "added": datetime.now().isoformat(),
        "status": "pending",
    }
    data[month].append(item)
    save_json(FUTURE_FILE, data)
    git_commit(f"bujo: Future item ({month}) — {args.text}")
    remember(f"Future log ({month}): {args.text}", ["future"])
    print(f"Added to future log ({month}): {args.text}")


def cmd_collection(args):
    """Manage custom collections."""
    name = args.name
    collection_file = COLLECTIONS / f"{name}.json"

    if args.action == "add":
        data = load_json(collection_file)
        if "items" not in data:
            data = {"name": name, "items": []}
        item = {
            "id": short_id(),
            "text": args.text,
            "added": datetime.now().isoformat(),
        }
        data["items"].append(item)
        save_json(collection_file, data)
        git_commit(f"bujo: Add to collection '{name}' — {args.text}")
        print(f"Added to {name}: {args.text}")

    elif args.action == "list":
        data = load_json(collection_file)
        items = data.get("items", [])
        if not items:
            print(f"Collection '{name}' is empty.")
            return
        print(f"=== {name} ===")
        for item in items:
            print(f"  [{item['id'][:8]}] {item['text']}")

    elif args.action == "remove":
        data = load_json(collection_file)
        items = data.get("items", [])
        found = False
        for i, item in enumerate(items):
            if item["id"].startswith(args.text):
                removed = items.pop(i)
                data["items"] = items
                save_json(collection_file, data)
                git_commit(f"bujo: Remove from collection '{name}' — {removed['text']}")
                print(f"Removed from {name}: {removed['text']}")
                found = True
                break
        if not found:
            print(f"Item {args.text} not found in collection '{name}'.")
            sys.exit(1)


def cmd_digest(args):
    """Generate daily digest for morning brief."""
    data = load_json(DAILY_FILE)
    monthly = load_json(MONTHLY_FILE)
    month_key = date.today().strftime("%Y-%m")

    lines = [f"*📓 Bullet Journal — {TODAY}*\n"]

    # Today's tasks
    day_data = data.get(TODAY, {})
    open_tasks = [t for t in day_data.get("tasks", []) if t.get("status") == "open"]
    if open_tasks:
        lines.append("*Today's Tasks:*")
        for t in sorted(open_tasks, key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(x.get("priority", "low"), 9)):
            lines.append(fmt_task(t))
    else:
        lines.append("_No tasks scheduled for today._")

    # Today's events
    events = day_data.get("events", [])
    if events:
        lines.append("\n*Events:*")
        for e in events:
            lines.append(fmt_event(e))

    # Stale/stuck tasks
    stale = get_stale_tasks(data)
    stuck = get_stuck_tasks(data)
    if stale:
        lines.append(f"\n*⚠️ Stale Tasks ({len(stale)}):*")
        for d, t in stale[:5]:
            age = (date.today() - datetime.fromisoformat(t["created_at"]).date()).days
            lines.append(f"{fmt_task(t, show_date=True)}  [{age}d]")
        if len(stale) > 5:
            lines.append(f"  ...and {len(stale) - 5} more")
    if stuck:
        lines.append(f"\n*🚧 Stuck Tasks ({len(stuck)}):*")
        for d, t in stuck[:3]:
            migs = len(t.get("migration_history", []))
            lines.append(f"{fmt_task(t, show_date=True)}  [{migs} migrations]")

    # Monthly themes/goals
    m = monthly.get(month_key, {})
    if m.get("themes"):
        lines.append(f"\n*Monthly Theme:* {m['themes'][-1]['text']}")
    active_goals = [g for g in m.get("goals", []) if g.get("status") == "active"]
    if active_goals:
        lines.append(f"*Active Goals ({len(active_goals)}):*")
        for g in active_goals:
            lines.append(f"  ○ {g['text']}")

    # Stats
    all_open = get_all_open_tasks(data)
    lines.append(f"\n_Open: {len(all_open)} | Stale: {len(stale)} | Stuck: {len(stuck)}_")

    digest_text = "\n".join(lines)
    print(digest_text)

    # Post to Slack
    if not args.quiet:
        slack_post(digest_text)
        log("Digest posted to Slack #nova-notifications")

    return digest_text


def cmd_weekly(args):
    """Weekly review: stale tasks, goal progress, synthesis."""
    data = load_json(DAILY_FILE)
    monthly = load_json(MONTHLY_FILE)
    month_key = date.today().strftime("%Y-%m")

    lines = [f"*📓 Weekly Review — week of {TODAY}*\n"]

    # Tasks completed this week
    week_start = date.today() - timedelta(days=date.today().weekday())
    completed_this_week = []
    for day_key, day_data in data.items():
        for task in day_data.get("tasks", []):
            if task.get("status") == "completed" and task.get("completed_at"):
                completed_date = datetime.fromisoformat(task["completed_at"]).date()
                if completed_date >= week_start:
                    completed_this_week.append(task)

    if completed_this_week:
        lines.append(f"*Completed This Week ({len(completed_this_week)}):*")
        for t in completed_this_week:
            lines.append(f"  ✕ {t['title']}")
    else:
        lines.append("_No tasks completed this week._")

    # Open tasks
    all_open = get_all_open_tasks(data)
    lines.append(f"\n*Open Tasks ({len(all_open)}):*")
    for d, t in sorted(all_open, key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(x[1].get("priority", "low"), 9)):
        lines.append(fmt_task(t, show_date=True))

    # Stale
    stale = get_stale_tasks(data)
    if stale:
        lines.append(f"\n*⚠️ Stale ({len(stale)}) — decide: do, delegate, or drop:*")
        for d, t in stale:
            age = (date.today() - datetime.fromisoformat(t["created_at"]).date()).days
            lines.append(f"{fmt_task(t, show_date=True)}  [{age}d old]")

    # Stuck
    stuck = get_stuck_tasks(data)
    if stuck:
        lines.append(f"\n*🚧 Stuck ({len(stuck)}) — migrated too many times:*")
        for d, t in stuck:
            migs = len(t.get("migration_history", []))
            lines.append(f"{fmt_task(t, show_date=True)}  [{migs} migrations]")

    # Monthly goals
    m = monthly.get(month_key, {})
    active_goals = [g for g in m.get("goals", []) if g.get("status") == "active"]
    if active_goals:
        lines.append(f"\n*Monthly Goal Progress:*")
        for g in active_goals:
            lines.append(f"  ○ {g['text']}")

    # Summary stats
    total_tasks = sum(len(d.get("tasks", [])) for d in data.values())
    total_completed = sum(
        1 for d in data.values()
        for t in d.get("tasks", [])
        if t.get("status") == "completed"
    )
    completion_rate = (total_completed / total_tasks * 100) if total_tasks > 0 else 0
    lines.append(f"\n*Stats:* {total_completed}/{total_tasks} tasks completed ({completion_rate:.0f}%) | "
                 f"{len(stale)} stale | {len(stuck)} stuck")

    review_text = "\n".join(lines)
    print(review_text)

    # Store weekly synthesis in vector memory
    synthesis = (
        f"Weekly review {TODAY}: "
        f"{len(completed_this_week)} completed, {len(all_open)} open, "
        f"{len(stale)} stale, {len(stuck)} stuck. "
        f"Completion rate: {completion_rate:.0f}%."
    )
    remember(synthesis, ["weekly-review"])

    # Post to Slack
    if not args.quiet:
        slack_post(review_text)
        log("Weekly review posted to Slack #nova-notifications")

    return review_text


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Parser
# ═══════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nova_bujo",
        description="Nova's Bullet Journal — rapid logging for tasks, events, and notes.",
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # ── add ───────────────────────────────────────────────────────────────
    p_add = sub.add_parser("add", help="Add a task, event, or note")
    p_add.add_argument("type", choices=["task", "event", "note"])
    p_add.add_argument("text", help="Title or text of the entry")
    p_add.add_argument("--priority", choices=["high", "medium", "low"], default=None,
                       help="Priority (tasks only)")
    p_add.add_argument("--date", default=None, help="Date (events only, YYYY-MM-DD)")
    p_add.add_argument("--tag", action="append", default=None, help="Tag (repeatable)")
    p_add.set_defaults(func=cmd_add)

    # ── complete ──────────────────────────────────────────────────────────
    p_complete = sub.add_parser("complete", help="Mark a task as completed")
    p_complete.add_argument("task_id", help="Task ID (first 8 chars)")
    p_complete.set_defaults(func=cmd_complete)

    # ── cancel ────────────────────────────────────────────────────────────
    p_cancel = sub.add_parser("cancel", help="Cancel a task")
    p_cancel.add_argument("task_id", help="Task ID (first 8 chars)")
    p_cancel.set_defaults(func=cmd_cancel)

    # ── migrate ───────────────────────────────────────────────────────────
    p_migrate = sub.add_parser("migrate", help="Migrate a task to a new date")
    p_migrate.add_argument("task_id", help="Task ID (first 8 chars)")
    p_migrate.add_argument("--to", required=True, help="Target date (YYYY-MM-DD)")
    p_migrate.set_defaults(func=cmd_migrate)

    # ── list ──────────────────────────────────────────────────────────────
    p_list = sub.add_parser("list", help="List entries for a date")
    p_list.add_argument("--date", default=None, help="Date to show (YYYY-MM-DD or 'today')")
    p_list.add_argument("--status", choices=["open", "completed", "cancelled", "migrated"],
                        default=None, help="Filter by status")
    p_list.add_argument("--tag", dest="tag_filter", default=None, help="Filter by tag")
    p_list.add_argument("--stale", action="store_true", help="Show stale/stuck tasks")
    p_list.set_defaults(func=cmd_list)

    # ── stale ─────────────────────────────────────────────────────────────
    p_stale = sub.add_parser("stale", help="Show all stale and stuck tasks")
    p_stale.set_defaults(func=cmd_stale)

    # ── month ─────────────────────────────────────────────────────────────
    p_month = sub.add_parser("month", help="Monthly goals, themes, reflections")
    p_month.add_argument("--goal", default=None, help="Add a monthly goal")
    p_month.add_argument("--theme", default=None, help="Set monthly theme")
    p_month.add_argument("--reflect", default=None, help="Add a reflection")
    p_month.set_defaults(func=cmd_month)

    # ── future ────────────────────────────────────────────────────────────
    p_future = sub.add_parser("future", help="Add to future log")
    p_future.add_argument("text", help="Future item text")
    p_future.add_argument("--month", required=True, help="Target month (YYYY-MM)")
    p_future.set_defaults(func=cmd_future)

    # ── collection ────────────────────────────────────────────────────────
    p_coll = sub.add_parser("collection", help="Manage custom collections")
    p_coll.add_argument("name", help="Collection name (e.g., birthdays, projects)")
    p_coll.add_argument("action", choices=["add", "list", "remove"],
                        help="Action to perform")
    p_coll.add_argument("text", nargs="?", default=None,
                        help="Item text (for add) or item ID (for remove)")
    p_coll.set_defaults(func=cmd_collection)

    # ── digest ────────────────────────────────────────────────────────────
    p_digest = sub.add_parser("digest", help="Daily summary for morning brief")
    p_digest.add_argument("--quiet", action="store_true", help="Don't post to Slack")
    p_digest.set_defaults(func=cmd_digest)

    # ── weekly ────────────────────────────────────────────────────────────
    p_weekly = sub.add_parser("weekly", help="Weekly review")
    p_weekly.add_argument("--quiet", action="store_true", help="Don't post to Slack")
    p_weekly.set_defaults(func=cmd_weekly)

    return parser


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    ensure_bujo_dir()
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
