#!/usr/bin/env python3
"""
nova_help_request.py — Queue help requests for the next Claude Code session.

A lightweight module that any Nova script can import to request Claude's help
when encountering errors it cannot resolve on its own.

Usage as module:
    from nova_help_request import request_help
    request_help("code_bug", "daily_essay failing with concatenation error", {
        "file": "nova_daily_essay.py",
        "error": "TypeError: can only concatenate str to str",
        "last_success": "2026-05-14"
    })

Usage as CLI:
    python3 nova_help_request.py code_bug "daily_essay failing" --context '{"file":"x.py"}'

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

PG_DSN = "dbname=nova_ops host=127.0.0.1 port=5432 user=kochj"
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
BRIDGE_SESSION_ID = "claude-bridge-persistent"

# Priority mapping by category
CATEGORY_PRIORITY = {
    "code_bug": 2,
    "config_issue": 2,
    "performance": 3,
    "feature_request": 4,
}

DEFAULT_PRIORITY = 3


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pg_execute(sql: str, params: tuple = ()):
    """Execute a PostgreSQL query. Tries psycopg2, then psycopg, then psql subprocess."""
    try:
        import psycopg2
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.close()
        return True
    except ImportError:
        pass

    try:
        import psycopg
        conn = psycopg.connect(PG_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.close()
        return True
    except ImportError:
        pass

    # Fallback: psql subprocess
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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return result.returncode == 0


def _pg_fetchone(sql: str, params: tuple = ()):
    """Execute a query and return the first row, or None."""
    try:
        import psycopg2
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        conn.close()
        return row
    except ImportError:
        pass

    try:
        import psycopg
        conn = psycopg.connect(PG_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        conn.close()
        return row
    except ImportError:
        pass

    return None


def _redis_publish(channel: str, data: dict):
    """Publish to Redis. Fire-and-forget."""
    try:
        cmd = [
            "redis-cli", "-h", REDIS_HOST, "-p", str(REDIS_PORT),
            "PUBLISH", channel, json.dumps(data)
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except Exception:
        pass


# ── Public API ───────────────────────────────────────────────────────────────

def request_help(category: str, description: str, context: dict = None):
    """Queue a help request for the next Claude Code session.

    Args:
        category: One of 'code_bug', 'config_issue', 'performance', 'feature_request'
        description: Human-readable description of the problem
        context: Optional dict with relevant details (file paths, error messages,
                 log snippets, timestamps, etc.)

    The request is:
        1. Inserted into claude_queue with priority based on category
        2. Published to Redis nova:to_claude for real-time notification
    """
    priority = CATEGORY_PRIORITY.get(category, DEFAULT_PRIORITY)
    ctx = context or {}
    ctx["category"] = category
    ctx["from"] = "nova-help-request"
    ctx["timestamp"] = time.time()
    ctx_json = json.dumps(ctx)

    # Deduplication: don't insert if same description already queued
    existing = _pg_fetchone(
        "SELECT 1 FROM claude_queue WHERE description = %s AND status IN ('queued', 'in_progress')",
        (description,)
    )
    if existing:
        return  # Already queued

    # Insert into claude_queue
    _pg_execute(
        "INSERT INTO claude_queue (session_id, status, priority, description, context, created_at) "
        "VALUES (%s, %s, %s, %s, %s, now())",
        (BRIDGE_SESSION_ID, "queued", priority, description, ctx_json)
    )

    # Publish to Redis for real-time notification
    _redis_publish("nova:to_claude", {
        "type": "help_request",
        "category": category,
        "description": description[:200],
        "priority": priority,
        "context": {k: str(v)[:100] for k, v in (context or {}).items()},
        "ts": time.time(),
    })


def request_help_for_failure(task_id: str, script_path: str, error_tail: str,
                              consecutive_failures: int, last_success: str = None):
    """Convenience wrapper for scheduler task failures.

    Called when a Nova scheduler task has failed consecutively and needs
    Claude's attention. Formats the context appropriately.
    """
    description = f"Scheduler task '{task_id}' failing ({consecutive_failures} consecutive failures)"
    context = {
        "task_id": task_id,
        "file": script_path,
        "error": error_tail[:500] if error_tail else "no error captured",
        "consecutive_failures": consecutive_failures,
    }
    if last_success:
        context["last_success"] = last_success

    request_help("code_bug", description, context)


# ── CLI Interface ────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Queue a help request for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s code_bug "daily_essay failing with TypeError"
  %(prog)s config_issue "Ollama model not loading after update" --context '{"model":"qwen3:30b"}'
  %(prog)s performance "Memory server response time >5s" --context '{"avg_ms":5200}'
"""
    )
    parser.add_argument("category",
                        choices=["code_bug", "config_issue", "performance", "feature_request"],
                        help="Category of the help request")
    parser.add_argument("description", help="Description of the problem")
    parser.add_argument("--context", default=None,
                        help="JSON string with additional context")

    args = parser.parse_args()

    ctx = None
    if args.context:
        try:
            ctx = json.loads(args.context)
        except json.JSONDecodeError:
            print(f"ERROR: --context must be valid JSON, got: {args.context}", file=sys.stderr)
            sys.exit(1)

    request_help(args.category, args.description, ctx)
    priority = CATEGORY_PRIORITY.get(args.category, DEFAULT_PRIORITY)
    print(f"OK — help request queued (category={args.category}, priority={priority})")


if __name__ == "__main__":
    main()
