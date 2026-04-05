#!/usr/bin/env python3
"""
nova_health_check.py — Daily self-audit for Nova's cron health.

Reads cron/jobs.json, identifies broken or unreliable jobs, and posts
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
SLACK_CHANNEL = "C0AMNQ5GX70"
SLACK_API     = "https://slack.com/api/chat.postMessage"
JOBS_FILE     = Path.home() / ".openclaw/cron/jobs.json"
LOG_DIR       = Path.home() / ".openclaw/logs"

# Thresholds
MAX_CONSECUTIVE_ERRORS  = 2       # alert after this many consecutive failures
FAST_RUN_THRESHOLD_MS   = 100     # runs shorter than this are suspect (empty promises)
STALE_HOURS             = 26      # job hasn't run in this long despite being scheduled daily
FAST_RUN_EXEMPT         = {       # jobs that legitimately run fast
    "Nova Disk Check (noon only)",
    "Nova Gateway Watchdog",
    "Nova Home Watchdog",
}


def log(msg):
    print(f"[nova_health_check {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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
                log(f"Slack post failed: {result.get('error')}")
    except Exception as e:
        log(f"Slack post error: {e}")


def audit_jobs() -> list[dict]:
    """Return list of issues found across all cron jobs."""
    issues = []
    now_ms = datetime.now().timestamp() * 1000

    try:
        data = json.loads(JOBS_FILE.read_text())
    except Exception as e:
        return [{"severity": "critical", "name": "cron/jobs.json", "reason": f"Cannot read: {e}"}]

    for job in data.get("jobs", []):
        if not job.get("enabled", True):
            continue

        name   = job.get("name", "unknown")
        state  = job.get("state", {})
        errors = state.get("consecutiveErrors", 0)
        status = state.get("lastRunStatus", "")
        dur    = state.get("lastDurationMs", 0)
        last   = state.get("lastRunAtMs", 0)
        err    = state.get("lastError", "")
        hours_since = (now_ms - last) / 3_600_000 if last else 999

        # Consecutive failures
        if errors >= MAX_CONSECUTIVE_ERRORS:
            issues.append({
                "severity": "error",
                "name": name,
                "reason": f"{errors} consecutive errors — {err[:80] if err else status}",
            })
            continue  # don't double-report

        # Last run errored
        if status == "error":
            issues.append({
                "severity": "warning",
                "name": name,
                "reason": f"Last run failed — {err[:80] if err else '(no detail)'}",
            })
            continue

        # Suspiciously fast (empty promise)
        if (status == "ok"
                and dur < FAST_RUN_THRESHOLD_MS
                and name not in FAST_RUN_EXEMPT
                and dur > 0):
            issues.append({
                "severity": "warning",
                "name": name,
                "reason": f"Completed in {dur}ms — likely did not execute (empty promise)",
            })

        # Stale — scheduled daily but hasn't run recently
        sched = job.get("schedule", {})
        is_daily = (
            sched.get("kind") == "cron"
            and "* * *" in sched.get("expr", "")
        )
        if is_daily and hours_since > STALE_HOURS and last > 0:
            issues.append({
                "severity": "warning",
                "name": name,
                "reason": f"Last ran {hours_since:.0f}h ago — may be stuck or skipped",
            })

    return issues


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
