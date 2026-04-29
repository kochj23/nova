#!/usr/bin/env python3
"""
nova_dead_mans_switch.py — Verify critical deliveries actually happened.

Runs at 9:15am and 7:15pm. Checks whether launchd agents actually wrote
output for today's scheduled deliveries. If a delivery is missing, runs
the script immediately and alerts Jordan via Slack.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import urllib.request
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SCRIPTS       = Path(__file__).parent
TODAY         = date.today().isoformat()
NOW_HOUR      = datetime.now().hour

SCHEDULER_API = "http://127.0.0.1:37460/tasks"

# Deliveries to verify — (scheduler_task_id, script_path, check_after_hour, label)
DELIVERIES = [
    (
        "morning_brief",
        SCRIPTS / "nova_morning_brief.py",
        9,
        "Morning Brief (7am)",
    ),
    (
        "mail_deliver_am",
        SCRIPTS / "nova_mail_deliver.py",
        9,
        "Morning Mail Summary (8am)",
    ),
    (
        "mail_deliver_pm",
        SCRIPTS / "nova_mail_deliver.py",
        19,
        "Evening Mail Summary (6pm)",
    ),
]


def log(msg):
    print(f"[nova_dead_mans_switch {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text: str):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def get_scheduler_tasks() -> dict:
    """Query scheduler API for task states."""
    try:
        with urllib.request.urlopen(SCHEDULER_API, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"Scheduler API unreachable: {e}")
        return {}


def task_ran_today(tasks: dict, task_id: str) -> bool:
    """Check if a task ran successfully today via scheduler state."""
    task = tasks.get(task_id, {})
    last_run = task.get("last_run", 0)
    if last_run == 0:
        return False
    last_run_date = datetime.fromtimestamp(last_run).strftime("%Y-%m-%d")
    ran_today = last_run_date == TODAY
    succeeded = task.get("last_exit_code", -1) == 0
    return ran_today and succeeded


def run_script(script_path: Path) -> bool:
    """Run a script directly. Returns True on success."""
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        log(f"Ran {script_path.name}: exit={result.returncode}")
        if result.stdout:
            log(f"  stdout: {result.stdout.strip()[:200]}")
        if result.returncode != 0 and result.stderr:
            log(f"  stderr: {result.stderr.strip()[:200]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"Timeout running {script_path.name}")
        return False
    except Exception as e:
        log(f"Error running {script_path.name}: {e}")
        return False


def main():
    log(f"Dead man's switch — checking deliveries for {TODAY} (hour={NOW_HOUR})")

    tasks = get_scheduler_tasks()
    if not tasks:
        log("Could not reach scheduler API — skipping")
        return

    missed = []
    for task_id, script, min_hour, label in DELIVERIES:
        if NOW_HOUR < min_hour:
            log(f"  {label} — too early to check (now={NOW_HOUR}h, min={min_hour}h)")
            continue

        if task_ran_today(tasks, task_id):
            log(f"  {label} — delivered ✓")
        else:
            log(f"  {label} — MISSING, running now")
            success = run_script(script)
            missed.append((label, success))

    if missed:
        lines = ["*Dead Man's Switch — Missed Deliveries Recovered*\n"]
        for label, success in missed:
            icon = "✅" if success else "❌"
            lines.append(f"{icon} `{label}` — was missing, ran now ({'ok' if success else 'FAILED'})")
        slack_post("\n".join(lines))
        log(f"Recovered {len(missed)} missed deliveries")
    else:
        log("All deliveries confirmed — nothing to recover")


if __name__ == "__main__":
    main()
