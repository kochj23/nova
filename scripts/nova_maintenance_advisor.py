#!/usr/bin/env python3
"""
nova_maintenance_advisor.py — Proactive maintenance analysis for Nova systems.

Runs weekly (Sunday 8am PT) as a scheduler task. Analyzes system metrics
and trends, then writes non-urgent maintenance suggestions to claude_queue
when something looks concerning.

Checks:
  1. Scheduler failure trends (increasing failure rate week-over-week)
  2. Inference latency trends (p95 increasing significantly)
  3. Disk space on all volumes (warn before Big Brother's critical threshold)
  4. Vector DB size growth rate
  5. Silently failing tasks (consecutive_failures > 0 but < 3)
  6. Redis memory usage and stale keys

Only writes suggestions if something actually looks concerning.
Priority 4 (non-urgent) — these are maintenance reminders, not incidents.

Scheduler config (add to scheduler.yaml):
  maintenance_advisor:
    script: nova_maintenance_advisor.py
    schedule: cron 0 8 * * 0
    timeout: 120
    overlap: skip

Written by Jordan Koch.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN

# ── Configuration ────────────────────────────────────────────────────────────

BRIDGE_SESSION_ID = "claude-bridge-persistent"
MAINTENANCE_PRIORITY = 4  # Non-urgent

# Thresholds
FAILURE_RATE_INCREASE_THRESHOLD = 0.10  # 10% increase in failure rate triggers suggestion
LATENCY_INCREASE_THRESHOLD = 0.40       # 40% p95 increase triggers suggestion
DISK_WARN_GB = 15.0                     # Suggest cleanup when below 15GB (before BB's 5GB critical)
VECTOR_GROWTH_WARN_PERCENT = 20.0       # 20% growth in a week
REDIS_MEMORY_WARN_MB = 512              # Warn if Redis using >512MB
SILENT_FAILURE_MIN = 1                  # consecutive_failures >= 1 and < 3


# ── Database Utilities ───────────────────────────────────────────────────────

def _pg_query(sql: str, params: tuple = ()):
    """Execute a PostgreSQL query via psql subprocess."""
    query = sql
    for p in params:
        if isinstance(p, str):
            escaped = p.replace("'", "''")
            query = query.replace("%s", f"'{escaped}'", 1)
        elif p is None:
            query = query.replace("%s", "NULL", 1)
        else:
            query = query.replace("%s", str(p), 1)

    cmd = [
        "psql", "-h", "127.0.0.1", "-d", "nova_ops", "-t", "-A",
        "-F", "\x1f",
        "-c", query
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return []
        lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
        return [line.split("\x1f") for line in lines]
    except (subprocess.TimeoutExpired, Exception):
        return []


def _pg_execute(sql: str, params: tuple = ()):
    """Execute a write query."""
    query = sql
    for p in params:
        if isinstance(p, str):
            escaped = p.replace("'", "''")
            query = query.replace("%s", f"'{escaped}'", 1)
        elif p is None:
            query = query.replace("%s", "NULL", 1)
        else:
            query = query.replace("%s", str(p), 1)

    cmd = ["psql", "-h", "127.0.0.1", "-d", "nova_ops", "-c", query]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


# ── Check Functions ──────────────────────────────────────────────────────────

def check_failure_trends() -> list:
    """Compare this week's scheduler failure rate to last week's."""
    suggestions = []

    now_ms = int(time.time() * 1000)
    one_week_ms = 7 * 24 * 3600 * 1000
    two_weeks_ms = 14 * 24 * 3600 * 1000

    # This week's stats
    rows = _pg_query(
        f"SELECT count(*) AS total, "
        f"count(*) FILTER (WHERE status = 'failure') AS failures "
        f"FROM scheduler_runs "
        f"WHERE started_at > {now_ms - one_week_ms}"
    )
    if not rows or not rows[0]:
        return suggestions

    this_week_total = int(rows[0][0]) if rows[0][0] else 0
    this_week_failures = int(rows[0][1]) if rows[0][1] else 0

    if this_week_total < 50:
        return suggestions  # Not enough data

    # Last week's stats
    rows = _pg_query(
        f"SELECT count(*) AS total, "
        f"count(*) FILTER (WHERE status = 'failure') AS failures "
        f"FROM scheduler_runs "
        f"WHERE started_at > {now_ms - two_weeks_ms} AND started_at <= {now_ms - one_week_ms}"
    )
    if not rows or not rows[0]:
        return suggestions

    last_week_total = int(rows[0][0]) if rows[0][0] else 0
    last_week_failures = int(rows[0][1]) if rows[0][1] else 0

    if last_week_total < 50:
        return suggestions  # Not enough data for comparison

    this_rate = this_week_failures / this_week_total
    last_rate = last_week_failures / last_week_total if last_week_total > 0 else 0

    rate_increase = this_rate - last_rate
    if rate_increase > FAILURE_RATE_INCREASE_THRESHOLD:
        suggestions.append({
            "description": (
                f"MAINTENANCE: Scheduler failure rate increased "
                f"{rate_increase*100:.1f}% this week "
                f"({this_week_failures}/{this_week_total} = {this_rate*100:.1f}% vs "
                f"last week {last_week_failures}/{last_week_total} = {last_rate*100:.1f}%). "
                f"Check scheduler.log for recurring failures."
            ),
            "context": {
                "metric": "scheduler_failure_rate",
                "this_week_rate": round(this_rate, 4),
                "last_week_rate": round(last_rate, 4),
                "increase": round(rate_increase, 4),
                "this_week_total": this_week_total,
                "this_week_failures": this_week_failures,
            }
        })

    # Check which specific tasks are failing most
    failing_tasks = _pg_query(
        f"SELECT task_id, count(*) AS fail_count "
        f"FROM scheduler_runs "
        f"WHERE started_at > {now_ms - one_week_ms} AND status = 'failure' "
        f"GROUP BY task_id ORDER BY fail_count DESC LIMIT 5"
    )
    if failing_tasks and int(failing_tasks[0][1] if len(failing_tasks[0]) > 1 else 0) > 5:
        top_failures = [(row[0], int(row[1])) for row in failing_tasks if len(row) >= 2]
        top_str = ", ".join(f"{t[0]}({t[1]}x)" for t in top_failures[:3])
        suggestions.append({
            "description": (
                f"MAINTENANCE: Top failing tasks this week: {top_str}. "
                f"Consider investigating root cause or adjusting timeouts."
            ),
            "context": {
                "metric": "top_failing_tasks",
                "tasks": [{"task_id": t[0], "failures": t[1]} for t in top_failures],
            }
        })

    return suggestions


def check_latency_trends() -> list:
    """Check inference latency p95 for degradation."""
    suggestions = []

    # This week's p95
    rows = _pg_query(
        "SELECT backend, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) AS p95 "
        "FROM inference_latency "
        "WHERE timestamp > now() - interval '7 days' AND status = 'success' "
        "GROUP BY backend"
    )
    if not rows:
        return suggestions

    this_week = {}
    for row in rows:
        if len(row) >= 2 and row[1]:
            try:
                this_week[row[0]] = float(row[1])
            except (ValueError, TypeError):
                pass

    # Last week's p95
    rows = _pg_query(
        "SELECT backend, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) AS p95 "
        "FROM inference_latency "
        "WHERE timestamp > now() - interval '14 days' "
        "AND timestamp <= now() - interval '7 days' "
        "AND status = 'success' "
        "GROUP BY backend"
    )
    last_week = {}
    for row in rows:
        if len(row) >= 2 and row[1]:
            try:
                last_week[row[0]] = float(row[1])
            except (ValueError, TypeError):
                pass

    # Compare
    for backend, current_p95 in this_week.items():
        prev_p95 = last_week.get(backend)
        if prev_p95 and prev_p95 > 0:
            increase_pct = (current_p95 - prev_p95) / prev_p95
            if increase_pct > LATENCY_INCREASE_THRESHOLD:
                suggestions.append({
                    "description": (
                        f"MAINTENANCE: Inference latency p95 increased "
                        f"{increase_pct*100:.0f}% this week ({backend}). "
                        f"Current p95: {current_p95:.0f}ms, last week: {prev_p95:.0f}ms. "
                        f"Consider model reload or KV cache clear."
                    ),
                    "context": {
                        "metric": "inference_latency",
                        "backend": backend,
                        "current_p95": round(current_p95),
                        "last_week_p95": round(prev_p95),
                        "increase_percent": round(increase_pct * 100, 1),
                    }
                })

    return suggestions


def check_disk_space() -> list:
    """Check disk space on all volumes — warn before it becomes critical."""
    suggestions = []

    volumes = [
        ("/Volumes/Data", "Data volume (AI models, Xcode, Nova)"),
        ("/Volumes/MoreData", "MoreData volume (PostgreSQL, vectors)"),
        (str(Path.home()), "Main SSD"),
    ]

    for path, label in volumes:
        try:
            stat = os.statvfs(path)
            free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
            total_gb = (stat.f_blocks * stat.f_frsize) / (1024 ** 3)
            used_pct = ((total_gb - free_gb) / total_gb) * 100 if total_gb > 0 else 0

            if free_gb < DISK_WARN_GB:
                suggestions.append({
                    "description": (
                        f"MAINTENANCE: {label} has {free_gb:.1f}GB free "
                        f"({used_pct:.0f}% used). Consider cleanup before it becomes critical. "
                        f"Check: large log files, old model downloads, Xcode DerivedData, "
                        f"pip/brew caches."
                    ),
                    "context": {
                        "metric": "disk_space",
                        "path": path,
                        "label": label,
                        "free_gb": round(free_gb, 2),
                        "total_gb": round(total_gb, 2),
                        "used_percent": round(used_pct, 1),
                    }
                })
        except (OSError, Exception):
            pass

    return suggestions


def check_vector_growth() -> list:
    """Check vector DB growth rate."""
    suggestions = []

    # Current count
    rows = _pg_query(
        "SELECT count(*) FROM nova_memories"
    )
    if not rows or not rows[0]:
        return suggestions

    try:
        current_count = int(rows[0][0])
    except (ValueError, IndexError):
        return suggestions

    # Count from a week ago (approximate via ID progression or timestamp)
    rows = _pg_query(
        "SELECT count(*) FROM nova_memories "
        "WHERE created_at <= now() - interval '7 days'"
    )
    if not rows or not rows[0]:
        return suggestions

    try:
        last_week_count = int(rows[0][0])
    except (ValueError, IndexError):
        return suggestions

    new_this_week = current_count - last_week_count
    if last_week_count > 0:
        growth_pct = (new_this_week / last_week_count) * 100
        if growth_pct > VECTOR_GROWTH_WARN_PERCENT:
            suggestions.append({
                "description": (
                    f"MAINTENANCE: Vector DB grew {growth_pct:.1f}% this week "
                    f"(+{new_this_week:,} vectors, total: {current_count:,}). "
                    f"Consider reviewing ingest sources or running deduplication."
                ),
                "context": {
                    "metric": "vector_growth",
                    "current_count": current_count,
                    "last_week_count": last_week_count,
                    "new_this_week": new_this_week,
                    "growth_percent": round(growth_pct, 1),
                }
            })

    return suggestions


def check_silent_failures() -> list:
    """Find scheduler tasks that are failing quietly (not yet escalated by BB)."""
    suggestions = []

    # Look for tasks with recent failures but not yet at BB's escalation threshold
    # BB escalates at consecutive_failures >= 3 — we want to catch 1-2
    rows = _pg_query(
        "SELECT DISTINCT ON (task_id) task_id, task_script, status, error_tail, "
        "consecutive_failures_at_start, started_at "
        "FROM scheduler_runs "
        "WHERE started_at > %s "
        "AND status = 'failure' "
        "AND consecutive_failures_at_start >= 1 "
        "AND consecutive_failures_at_start < 3 "
        "ORDER BY task_id, started_at DESC",
        (str(int((time.time() - 7 * 24 * 3600) * 1000)),)
    )

    if not rows:
        return suggestions

    silently_failing = []
    for row in rows:
        if len(row) >= 5:
            silently_failing.append({
                "task_id": row[0],
                "script": row[1],
                "error_tail": (row[3] or "")[:200],
                "consecutive_failures": int(row[4]) if row[4] else 0,
            })

    if silently_failing:
        task_list = ", ".join(t["task_id"] for t in silently_failing[:5])
        suggestions.append({
            "description": (
                f"MAINTENANCE: {len(silently_failing)} task(s) silently failing "
                f"(not yet at BB escalation threshold): {task_list}. "
                f"Investigate before they become persistent failures."
            ),
            "context": {
                "metric": "silent_failures",
                "tasks": silently_failing[:10],
            }
        })

    return suggestions


def check_redis_health() -> list:
    """Check Redis memory usage and stale key patterns."""
    suggestions = []

    try:
        # Get Redis memory info
        result = subprocess.run(
            ["redis-cli", "-h", "127.0.0.1", "-p", "6379", "INFO", "memory"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return suggestions

        info = result.stdout
        used_memory_mb = 0
        for line in info.split("\n"):
            if line.startswith("used_memory:"):
                used_bytes = int(line.split(":")[1].strip())
                used_memory_mb = used_bytes / (1024 * 1024)
                break

        if used_memory_mb > REDIS_MEMORY_WARN_MB:
            suggestions.append({
                "description": (
                    f"MAINTENANCE: Redis memory usage is {used_memory_mb:.0f}MB "
                    f"(threshold: {REDIS_MEMORY_WARN_MB}MB). "
                    f"Check for key leaks: redis-cli --bigkeys"
                ),
                "context": {
                    "metric": "redis_memory",
                    "used_mb": round(used_memory_mb, 1),
                    "threshold_mb": REDIS_MEMORY_WARN_MB,
                }
            })

        # Check total key count for unusual growth
        result = subprocess.run(
            ["redis-cli", "-h", "127.0.0.1", "-p", "6379", "DBSIZE"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            # Output: "db0:keys=1234,expires=100,avg_ttl=0" or "(integer) 1234"
            output = result.stdout.strip()
            if "keys=" in output:
                key_count = int(output.split("keys=")[1].split(",")[0])
            elif "(integer)" in output:
                key_count = int(output.split(")")[-1].strip())
            else:
                key_count = 0

            if key_count > 10000:
                suggestions.append({
                    "description": (
                        f"MAINTENANCE: Redis has {key_count:,} keys. "
                        f"Check for stale keys without TTL: "
                        f"redis-cli --scan --pattern '*' | head -50"
                    ),
                    "context": {
                        "metric": "redis_keys",
                        "key_count": key_count,
                    }
                })

    except (subprocess.TimeoutExpired, Exception) as e:
        log(f"Redis health check failed: {e}", level=LOG_WARN, source="maintenance-advisor")

    return suggestions


# ── Queue Suggestions ────────────────────────────────────────────────────────

def _queue_suggestion(suggestion: dict):
    """Write a maintenance suggestion to claude_queue."""
    # Ensure bridge session exists
    _pg_execute(
        "INSERT INTO claude_sessions (session_id, project, status, summary) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (session_id) DO NOTHING",
        (BRIDGE_SESSION_ID, "nova-claude-bridge", "active",
         "Persistent session for nova_claude_bridge.py integration")
    )

    description = suggestion["description"]
    context_json = json.dumps(suggestion.get("context", {}), default=str)

    # Deduplication — don't queue if similar suggestion already pending
    existing = _pg_query(
        "SELECT 1 FROM claude_queue "
        "WHERE description = %s AND status IN ('queued', 'in_progress')",
        (description,)
    )
    if existing:
        return False

    success = _pg_execute(
        "INSERT INTO claude_queue (session_id, status, priority, description, context) "
        "VALUES (%s, %s, %s, %s, %s)",
        (BRIDGE_SESSION_ID, "queued", str(MAINTENANCE_PRIORITY),
         description, context_json)
    )
    return success


# ── Main ─────────────────────────────────────────────────────────────────────

def run_analysis():
    """Run all maintenance checks and queue any concerns found."""
    log("Starting weekly maintenance analysis", level=LOG_INFO, source="maintenance-advisor")

    all_suggestions = []

    # Run all checks
    checks = [
        ("scheduler_failures", check_failure_trends),
        ("inference_latency", check_latency_trends),
        ("disk_space", check_disk_space),
        ("vector_growth", check_vector_growth),
        ("silent_failures", check_silent_failures),
        ("redis_health", check_redis_health),
    ]

    for check_name, check_fn in checks:
        try:
            results = check_fn()
            if results:
                all_suggestions.extend(results)
                log(f"  [{check_name}] {len(results)} concern(s) found",
                    level=LOG_INFO, source="maintenance-advisor")
            else:
                log(f"  [{check_name}] OK — no concerns",
                    level=LOG_INFO, source="maintenance-advisor")
        except Exception as e:
            log(f"  [{check_name}] check failed: {e}",
                level=LOG_WARN, source="maintenance-advisor")

    # Queue suggestions
    queued_count = 0
    for suggestion in all_suggestions:
        if _queue_suggestion(suggestion):
            queued_count += 1

    log(f"Maintenance analysis complete: {len(all_suggestions)} concerns found, "
        f"{queued_count} queued for Claude Code",
        level=LOG_INFO, source="maintenance-advisor")

    # Summary for scheduler stdout capture
    if all_suggestions:
        print(f"Maintenance advisor: {len(all_suggestions)} concern(s) found, "
              f"{queued_count} queued (priority {MAINTENANCE_PRIORITY})")
        for s in all_suggestions:
            print(f"  • {s['description'][:120]}")
    else:
        print("Maintenance advisor: All systems nominal — no concerns this week.")

    return all_suggestions


if __name__ == "__main__":
    run_analysis()
