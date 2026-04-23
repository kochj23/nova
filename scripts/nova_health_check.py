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
SLACK_CHANNEL = "C0ATAF7NZG9"
JOBS_FILE     = Path.home() / ".openclaw/cron/jobs.json"
LOG_DIR       = Path.home() / ".openclaw/logs"
NOVA_BOT_ID   = "U0ANKLR3SUQ"   # novaslackintegation bot user ID

# Thresholds
MAX_CONSECUTIVE_ERRORS  = 2       # alert after this many consecutive failures
FAST_RUN_THRESHOLD_MS   = 100     # runs shorter than this are suspect (empty promises)
STALE_HOURS             = 26      # job hasn't run in this long despite being scheduled daily
FAST_RUN_EXEMPT         = {       # jobs that legitimately run fast
    "Nova Disk Check (noon only)",
    "Nova Gateway Watchdog",
    "Nova Home Watchdog",
}

# Key deliveries that should appear in Slack within the last 20 hours
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
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


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
    """Check that expected bot messages appeared in Slack in the last 20 hours."""
    issues = []
    messages = fetch_recent_slack_messages(hours=20)
    all_text = " ".join(m.get("text", "") for m in messages).lower()

    for label, fragment in EXPECTED_SLACK_DELIVERIES:
        if fragment.lower() not in all_text:
            issues.append({
                "severity": "warning",
                "name": f"Slack delivery: {label}",
                "reason": f"No '{fragment}' message found in #nova-chat in last 20h",
            })

    return issues


def _load_run_history(job_id: str) -> dict:
    """Read the JSONL run file for a job and return last-run info.

    Returns dict with keys: lastRunAtMs, lastRunStatus, lastDurationMs,
    lastError, consecutiveErrors.  Falls back to empty/zero values.
    """
    runs_dir = JOBS_FILE.parent / "runs"
    jsonl = runs_dir / f"{job_id}.jsonl"
    if not jsonl.exists():
        return {}

    last_ts = 0
    last_status = ""
    last_dur = 0
    last_err = ""
    consecutive_errors = 0

    try:
        with open(jsonl) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = entry.get("ts", 0)
                if ts >= last_ts:
                    action = entry.get("action", "")
                    if action == "finished":
                        last_ts = ts
                        last_status = entry.get("status", "ok")
                        last_dur = entry.get("durationMs", 0)
                        last_err = entry.get("error", "")
                    elif action == "started":
                        # update ts even for starts so we know it attempted
                        if ts > last_ts:
                            last_ts = ts

        # Count consecutive errors from end of file
        entries = []
        with open(jsonl) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        for entry in reversed(entries):
            if entry.get("action") == "finished":
                if entry.get("status") == "error":
                    consecutive_errors += 1
                else:
                    break
    except Exception:
        pass

    return {
        "lastRunAtMs": last_ts,
        "lastRunStatus": last_status,
        "lastDurationMs": last_dur,
        "lastError": last_err,
        "consecutiveErrors": consecutive_errors,
    }


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
        job_id = job.get("id", "")

        # Read state from JSONL run history (primary) with jobs.json state as fallback
        run_info = _load_run_history(job_id) if job_id else {}
        state  = job.get("state", {})
        errors = run_info.get("consecutiveErrors", state.get("consecutiveErrors", 0))
        status = run_info.get("lastRunStatus", state.get("lastRunStatus", ""))
        dur    = run_info.get("lastDurationMs", state.get("lastDurationMs", 0))
        last   = run_info.get("lastRunAtMs", state.get("lastRunAtMs", 0))
        err    = run_info.get("lastError", state.get("lastError", ""))
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
