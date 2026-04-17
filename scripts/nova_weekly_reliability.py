#!/usr/bin/env python3
"""
nova_weekly_reliability.py — Weekly system reliability report.

Runs Sunday at 10 PM. Queries the scheduler's task history for the week,
counts successes/failures/restarts per task, and posts a summary to Slack.

This is the measurement tool — the proof that Nova runs without intervention.

Written by Jordan Koch.
"""

import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, read_logs

SLACK_CHAN = nova_config.SLACK_CHAN  # #nova-chat — Jordan should see this
VECTOR_URL = nova_config.VECTOR_URL
TODAY = datetime.now()
WEEK_AGO = TODAY - timedelta(days=7)


def slack_post(text):
    token = nova_config.slack_bot_token()
    if not token:
        return
    try:
        payload = json.dumps({"channel": SLACK_CHAN, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def get_scheduler_tasks():
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:37460/tasks", timeout=5)
        return json.loads(resp.read())
    except Exception:
        return {}


def get_scheduler_status():
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:37460/status", timeout=5)
        return json.loads(resp.read())
    except Exception:
        return {}


def get_memory_count():
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:18790/stats", timeout=5)
        data = json.loads(resp.read())
        return data.get("count", 0), len(data.get("by_source", {}))
    except Exception:
        return 0, 0


def analyze_logs():
    """Analyze structured logs for the past week."""
    since = WEEK_AGO.isoformat()
    errors = read_logs(n=500, level="error", since=since)
    warns = read_logs(n=500, level="warn", since=since)

    error_sources = {}
    for e in errors:
        src = e.get("source", "unknown")
        error_sources[src] = error_sources.get(src, 0) + 1

    return len(errors), len(warns), error_sources


def main():
    log("Generating weekly reliability report", level=LOG_INFO, source="weekly_reliability")

    status = get_scheduler_status()
    tasks = get_scheduler_tasks()
    mem_count, source_count = get_memory_count()
    error_count, warn_count, error_sources = analyze_logs()

    uptime_h = status.get("uptime_s", 0) / 3600
    total_runs = status.get("total_runs", 0)
    total_failures = status.get("total_failures", 0)
    success_rate = ((total_runs - total_failures) / total_runs * 100) if total_runs else 0

    # Categorize tasks
    healthy = []
    failing = []
    idle = []
    for tid, t in tasks.items():
        if not t.get("enabled", True):
            continue
        runs = t.get("run_count", 0)
        fails = t.get("consecutive_failures", 0)
        if runs == 0:
            idle.append(tid)
        elif fails >= 3:
            failing.append((tid, fails, t.get("last_exit_code", 0)))
        else:
            healthy.append(tid)

    # Build report
    week_str = f"{WEEK_AGO.strftime('%b %d')} — {TODAY.strftime('%b %d, %Y')}"

    lines = [
        f":bar_chart: *Weekly Reliability Report — {week_str}*",
        "",
        f"*Scheduler:*",
        f"  Uptime: {uptime_h:.1f} hours",
        f"  Total runs: {total_runs:,}",
        f"  Failures: {total_failures:,}",
        f"  Success rate: {success_rate:.1f}%",
        "",
        f"*Tasks:* {len(healthy)} healthy, {len(failing)} failing, {len(idle)} idle (of {len(tasks)})",
    ]

    if failing:
        lines.append("")
        lines.append("*Failing tasks:*")
        for tid, fails, exit_code in failing:
            lines.append(f"  :red_circle: {tid} — {fails} consecutive failures (exit {exit_code})")

    if idle:
        lines.append("")
        lines.append(f"*Idle tasks (0 runs):* {', '.join(idle[:10])}")

    # Top 5 most-run tasks
    by_runs = sorted(tasks.items(), key=lambda x: x[1].get("run_count", 0), reverse=True)
    lines.append("")
    lines.append("*Most active tasks:*")
    for tid, t in by_runs[:5]:
        runs = t.get("run_count", 0)
        avg_dur = t.get("last_duration", 0)
        lines.append(f"  {tid}: {runs:,} runs ({avg_dur:.1f}s avg)")

    # Errors
    lines.append("")
    lines.append(f"*Logs:* {error_count} errors, {warn_count} warnings this week")
    if error_sources:
        top_errors = sorted(error_sources.items(), key=lambda x: -x[1])[:5]
        for src, count in top_errors:
            lines.append(f"  {src}: {count} errors")

    # Memory
    lines.append("")
    lines.append(f"*Memory:* {mem_count:,} vectors across {source_count} sources")

    # Services health (quick check)
    services_ok = 0
    services_total = 0
    for name, port in [("Scheduler", 37460), ("Gateway", 18789), ("Memory", 18790), ("Ollama", 11434)]:
        services_total += 1
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
            services_ok += 1
        except Exception:
            try:
                import socket
                s = socket.socket()
                s.settimeout(3)
                s.connect(("127.0.0.1", port))
                s.close()
                services_ok += 1
            except Exception:
                lines.append(f"  :red_circle: {name} (:{port}) DOWN at report time")

    lines.append(f"*Services:* {services_ok}/{services_total} healthy")

    # Verdict
    lines.append("")
    if success_rate >= 99 and not failing:
        lines.append(":white_check_mark: *Verdict: Rock solid.* No intervention needed.")
    elif success_rate >= 95 and len(failing) <= 2:
        lines.append(":large_yellow_circle: *Verdict: Mostly stable.* A few tasks need attention.")
    else:
        lines.append(":red_circle: *Verdict: Needs work.* Check failing tasks and error logs.")

    msg = "\n".join(lines)
    slack_post(msg)

    # Store in memory
    try:
        payload = json.dumps({
            "text": f"Weekly reliability report {week_str}: {success_rate:.1f}% success rate, "
                    f"{total_runs} runs, {total_failures} failures, {len(healthy)} healthy tasks, "
                    f"{mem_count:,} memories.",
            "source": "system",
            "metadata": {"type": "weekly_reliability", "date": TODAY.isoformat(),
                         "success_rate": success_rate, "total_runs": total_runs}
        }).encode()
        req = urllib.request.Request(VECTOR_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

    log(f"Weekly report: {success_rate:.1f}% success, {total_runs} runs, {len(failing)} failing",
        level=LOG_INFO, source="weekly_reliability")


if __name__ == "__main__":
    main()
