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

SLACK_TOKEN   = nova_config.slack_bot_token()
SLACK_CHANNEL = "C0AMNQ5GX70"
SLACK_API     = "https://slack.com/api/chat.postMessage"
SCRIPTS       = Path(__file__).parent
LOGS          = Path.home() / ".openclaw/logs"
TODAY         = date.today().isoformat()
NOW_HOUR      = datetime.now().hour

# Deliveries to verify — (log_file, script_path, check_after_hour, label)
# Only checked if current hour >= check_after_hour
DELIVERIES = [
    (
        LOGS / "morning-brief.log",
        SCRIPTS / "nova_morning_brief.py",
        9,
        "Morning Brief (7am)",
    ),
    (
        LOGS / "mail-deliver-8am.log",
        SCRIPTS / "nova_mail_deliver.py",
        9,
        "Morning Mail Summary (8am)",
    ),
    (
        LOGS / "mail-deliver-6pm.log",
        SCRIPTS / "nova_mail_deliver.py",
        19,
        "Evening Mail Summary (6pm)",
    ),
]


def log(msg):
    print(f"[nova_dead_mans_switch {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text: str):
    payload = json.dumps({"channel": SLACK_CHANNEL, "text": text}).encode()
    req = urllib.request.Request(
        SLACK_API,
        data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
            if not result.get("ok"):
                log(f"Slack error: {result.get('error')}")
    except Exception as e:
        log(f"Slack post failed: {e}")


def log_has_todays_run(log_path: Path) -> bool:
    """Return True if the log file contains an entry from today."""
    try:
        content = log_path.read_text(errors="ignore")
        return TODAY in content
    except FileNotFoundError:
        return False
    except Exception:
        return False


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

    missed = []
    for log_path, script, min_hour, label in DELIVERIES:
        if NOW_HOUR < min_hour:
            log(f"  {label} — too early to check (now={NOW_HOUR}h, min={min_hour}h)")
            continue

        if log_has_todays_run(log_path):
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
