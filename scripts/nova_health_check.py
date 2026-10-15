#!/usr/bin/env python3
"""
nova_health_check.py — Daily self-audit for Nova's cron health.

Reads the unified scheduler API, identifies broken or unreliable jobs, and posts
a Slack summary to #nova-chat. Runs at 6:45am via launchd so Jordan
knows about problems before his day starts — not after he notices them.

Written by Jordan Koch.
"""

import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN   = nova_config.slack_bot_token()
SLACK_CHANNEL = "C0ATAF7NZG9"
LOG_DIR       = Path.home() / ".openclaw/logs"
NOVA_BOT_ID   = "U0ANKLR3SUQ"   # novaslackintegation bot user ID
import sys as _sys
# ponytail: .6 scheduler is :37460, scheduler-core on Linux is :37464 — same /tasks shape
SCHEDULER_API = "http://127.0.0.1:%d/tasks" % (37460 if _sys.platform == "darwin" else 37464)

# Thresholds
MAX_CONSECUTIVE_ERRORS  = 2       # alert after this many consecutive failures
FAST_RUN_THRESHOLD_MS   = 100     # runs shorter than this are suspect (empty promises)
STALE_HOURS             = 26      # job hasn't run in this long despite being scheduled daily
WEEKLY_TASKS            = {"weekly_journal", "self_audit", "weekly_reliability", "alert_patterns", "local_trends", "network_health"}
FAST_RUN_EXEMPT         = {       # jobs that legitimately run fast
    "Nova Disk Check (noon only)",
    "Nova Gateway Watchdog",
    "Nova Home Watchdog",
    "session_watchdog",
    "gateway_watchdog",
    "home_watchdog",
    "subagent_health",
}

# Daily deliveries: one run per ~24h, so the freshness window must exceed 24h plus scheduler
# jitter. 20h false-alarmed every morning (06:45 check vs 07:00 morning_brief, 2026-10-15).
DELIVERY_MAX_AGE_H = 26

# Key deliveries that should appear in Slack within the last DELIVERY_MAX_AGE_H hours
# (label, search_text_fragment)
EXPECTED_SLACK_DELIVERIES = [
    ("Morning Brief",      "Morning Brief"),
    ("Mail Summary",       "Mail Summary"),
    ("Dream Journal",      "Dream Journal"),
    ("Nightly Report",     "Nightly Report"),
]


def log(msg):
    print(f"[nova_health_check {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text: str):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_BB)


def fetch_recent_slack_messages(hours: int = 20) -> list[dict]:
    """Fetch recent messages from #nova-chat posted by the Nova bot."""
    import time
    oldest = str(time.time() - hours * 3600)
    url = (f"https://slack.com/api/conversations.history"
           f"?channel={SLACK_CHANNEL}&oldest={oldest}&limit=200")
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if not data.get("ok"):
            log(f"Slack history error: {data.get('error')}")
            return []
        return [m for m in data.get("messages", []) if m.get("user") == NOVA_BOT_ID
                or m.get("bot_id")]
    except Exception as e:
        log(f"Could not fetch Slack history: {e}")
        return []


def audit_slack_deliveries() -> list[dict]:
    """Check that expected deliveries happened in the last DELIVERY_MAX_AGE_H hours.

    Primary: check scheduler task state for last_run timestamps.
    Fallback: search Slack message history.
    """
    issues = []
    now = datetime.now().timestamp()

    # Map deliveries to scheduler task IDs
    DELIVERY_TASK_MAP = {
        "Morning Brief": "morning_brief",
        "Mail Summary": "mail_deliver_midday",
        "Nightly Report": "nightly_report",
    }

    # Try scheduler API first
    try:
        resp = urllib.request.urlopen(SCHEDULER_API, timeout=5)
        tasks = json.loads(resp.read())

        for label, task_id in DELIVERY_TASK_MAP.items():
            if task_id not in tasks:
                continue  # hosted by the other scheduler (.6 vs scheduler-core); its health check covers it
            task = tasks[task_id]
            last_run = task.get("last_run", 0)
            hours_ago = (now - last_run) / 3600 if last_run else 999
            exit_code = task.get("last_exit_code", -1)

            if hours_ago > DELIVERY_MAX_AGE_H:
                issues.append({
                    "severity": "warning",
                    "name": f"Slack delivery: {label}",
                    "reason": f"Task '{task_id}' last ran {hours_ago:.0f}h ago (expected within {DELIVERY_MAX_AGE_H}h)",
                })
            elif exit_code != 0:
                issues.append({
                    "severity": "warning",
                    "name": f"Slack delivery: {label}",
                    "reason": f"Task '{task_id}' ran but failed (exit {exit_code})",
                })
        return issues
    except Exception:
        pass

    # Fallback: Slack message search
    messages = fetch_recent_slack_messages(hours=DELIVERY_MAX_AGE_H)
    all_text = " ".join(m.get("text", "") for m in messages).lower()

    for label, fragment in EXPECTED_SLACK_DELIVERIES:
        if fragment.lower() not in all_text:
            issues.append({
                "severity": "warning",
                "name": f"Slack delivery: {label}",
                "reason": f"No '{fragment}' message found in #nova-notifications in last {DELIVERY_MAX_AGE_H}h",
            })

    return issues


def audit_jobs() -> list[dict]:
    """Return list of issues found across all scheduled tasks.

    Source: unified scheduler API (37460 on .6, 37464 for scheduler-core on Linux).
    """
    issues = []
    now = datetime.now().timestamp()

    # Try the unified scheduler API first
    scheduler_tasks = {}
    try:
        resp = urllib.request.urlopen(SCHEDULER_API, timeout=5)
        scheduler_tasks = json.loads(resp.read())
    except Exception as e:
        log(f"Scheduler API unavailable: {e} — reporting as critical")

    if scheduler_tasks:
        for task_id, task in scheduler_tasks.items():
            if not task.get("enabled", True):
                continue

            errors = task.get("consecutive_failures", 0)
            last_run = task.get("last_run", 0)
            last_dur = task.get("last_duration", 0)
            last_exit = task.get("last_exit_code", 0)
            hours_since = (now - last_run) / 3600 if last_run else 999

            # Consecutive failures
            if errors >= MAX_CONSECUTIVE_ERRORS:
                issues.append({
                    "severity": "error",
                    "name": task_id,
                    "reason": f"{errors} consecutive failures (exit code {last_exit})",
                })
                continue

            # Suspiciously fast (empty promise) — only for cron tasks
            dur_ms = int(last_dur * 1000)
            is_cron = "cron" in task.get("schedule", "")
            # ponytail: only daily crons can be "stale after 26h"; weekly/monthly (day fields set) are skipped
            _f = task.get("schedule", "").split()[1:]
            is_daily = is_cron and len(_f) == 5 and _f[2] == "*" and _f[4] == "*"
            if (is_cron
                    and last_exit == 0
                    and dur_ms < FAST_RUN_THRESHOLD_MS
                    and dur_ms > 0
                    and task_id not in FAST_RUN_EXEMPT):
                issues.append({
                    "severity": "warning",
                    "name": task_id,
                    "reason": f"Completed in {dur_ms}ms — likely did not execute (empty promise)",
                })

            # Stale — daily cron that hasn't run in 26+ hours (skip weekly tasks)
            if (is_daily and hours_since > STALE_HOURS and last_run > 0
                    and task_id not in WEEKLY_TASKS):
                issues.append({
                    "severity": "warning",
                    "name": task_id,
                    "reason": f"Last ran {hours_since:.0f}h ago — may be stuck or skipped",
                })
        return issues

    # OpenClaw's cron/jobs.json was retired with the legacy gateway; no fallback source exists.
    return [{"severity": "critical", "name": "scheduler API",
             "reason": f"Unreachable at {SCHEDULER_API} — cannot audit scheduled tasks"}]


def format_message(issues: list[dict]) -> str:
    today = datetime.now().strftime("%A, %B %-d")

    if not issues:
        return (
            f"*Nova Health Check — {today}* ✅\n"
            "All cron jobs running normally. Nothing to report."
        )

    errors   = [i for i in issues if i["severity"] == "critical" or i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    lines = [f"*Nova Health Check — {today}*"]

    if errors:
        lines.append(f"\n🔴 *{len(errors)} error{'s' if len(errors) != 1 else ''}:*")
        for i in errors:
            lines.append(f"  • `{i['name']}` — {i['reason']}")

    if warnings:
        lines.append(f"\n🟡 *{len(warnings)} warning{'s' if len(warnings) != 1 else ''}:*")
        for i in warnings:
            lines.append(f"  • `{i['name']}` — {i['reason']}")

    lines.append(f"\n_Run `python3 ~/.openclaw/scripts/nova_health_check.py` to re-check._")

    return "\n".join(lines)


def main():
    log("Starting health check")
    issues = audit_jobs()
    issues += audit_slack_deliveries()

    error_count   = sum(1 for i in issues if i["severity"] in ("error", "critical"))
    warning_count = sum(1 for i in issues if i["severity"] == "warning")
    log(f"Found {error_count} errors, {warning_count} warnings")

    msg = format_message(issues)
    slack_post(msg)
    log("Health check posted to Slack")

    if issues:
        for i in issues:
            log(f"  [{i['severity'].upper()}] {i['name']}: {i['reason']}")


if __name__ == "__main__":
    main()
