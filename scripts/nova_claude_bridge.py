#!/usr/bin/env python3
"""
nova_claude_bridge.py — Integration layer between Claude Code and Nova.

Provides bidirectional messaging, decision log reading, task triggering,
and a shared Redis scratchpad between Claude Code sessions and Nova.

Subcommands:
  send <message>         — Send a message to Nova tagged as from claude-code
  receive [--limit N]    — Get Nova's recent messages/responses for claude-code
  decisions [--since Xh] — View Nova's recent decisions (scheduler runs, model choices)
  trigger <task_id>      — Trigger a Nova scheduler task on demand
  scratch read <key>     — Read a value from the shared scratchpad
  scratch write <key> <value> [--ttl seconds] — Write a value to the shared scratchpad
  scratch list           — List all scratchpad keys
  handoff <summary>      — Write session handoff for Nova (queue + Redis + message)
  editing <filepath>     — Mark a file as being edited (prevents Nova restarts)
  done-editing <filepath>— Clear the edit lock on a file
  editing-status         — Show all files currently locked for editing
  review <filepath>      — Ask Nova to review recent changes to a file

Written by Jordan Koch.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Configuration ────────────────────────────────────────────────────────────

GATEWAY_HTTP = "http://127.0.0.1:18792"
SCHEDULER_HTTP = "http://127.0.0.1:37460"
PG_DSN = "dbname=nova_ops host=127.0.0.1 port=5432 user=kochj"
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
SCRATCHPAD_PREFIX = "nova:scratchpad:"
SCRATCHPAD_TTL = 86400  # 24 hours default
HANDOFF_KEY = "nova:scratchpad:claude_handoff"
HANDOFF_TTL = 172800  # 48 hours
EDITING_PREFIX = "nova:editing:"
EDITING_TTL = 1800  # 30 minutes auto-expire


# ── Utilities ────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[nova_claude_bridge {ts}] {level}: {msg}", flush=True)


def pg_query(sql: str, params: tuple = (), fetchall: bool = True):
    """Execute a PostgreSQL query using psycopg2 (or psycopg)."""
    try:
        import psycopg2
        conn = psycopg2.connect(PG_DSN)
    except ImportError:
        try:
            import psycopg
            conn = psycopg.connect(PG_DSN)
        except ImportError:
            # Fallback: use psql subprocess
            return _pg_query_subprocess(sql, params)

    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql, params)
        if fetchall and cur.description:
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return [dict(zip(cols, row)) for row in rows]
        return []
    finally:
        conn.close()


def _pg_query_subprocess(sql: str, params: tuple = ()):
    """Fallback: run query via psql when no Python PG driver is available."""
    # Simple parameter substitution for psql (only supports %s style)
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
        "-F", "\x1f",  # unit separator as field delimiter
        "-c", query
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"psql error: {result.stderr.strip()}", "ERROR")
        return []

    lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
    if not lines:
        return []

    # Parse column names from the query (best effort)
    # For subprocess fallback, we return raw dicts with index keys
    # unless we can parse column names from the SQL
    rows = []
    for line in lines:
        fields = line.split("\x1f")
        rows.append(fields)
    return rows


def redis_cmd(*args) -> str:
    """Execute a Redis command via redis-cli and return output."""
    cmd = ["redis-cli", "-h", REDIS_HOST, "-p", str(REDIS_PORT)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(f"Redis error: {result.stderr.strip()}")
    return result.stdout.strip()


SLACK_CLAUDE_CHANNEL = "C0B3RSRR0DD"


def _post_slack_claude(text: str):
    """Post to #nova-claude Slack channel. Fire-and-forget."""
    try:
        token = subprocess.run(
            ["security", "find-generic-password", "-s", "nova-slack-bot-token", "-w"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if not token:
            return
        data = json.dumps({"channel": SLACK_CLAUDE_CHANNEL, "text": text, "mrkdwn": True}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def http_request(url: str, method: str = "GET", data: dict = None, timeout: int = 10) -> dict:
    """Make an HTTP request and return parsed JSON response."""
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return {"error": f"HTTP {e.code}: {e.reason}", "body": body_text}
    except urllib.error.URLError as e:
        return {"error": f"Connection failed: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


# ── Bridge session ID ────────────────────────────────────────────────────────

BRIDGE_SESSION_ID = "claude-bridge-persistent"


def ensure_bridge_session():
    """Ensure the persistent bridge session exists in claude_sessions."""
    pg_query(
        "INSERT INTO claude_sessions (session_id, project, status, summary) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (session_id) DO NOTHING",
        (BRIDGE_SESSION_ID, "nova-claude-bridge", "active",
         "Persistent session for nova_claude_bridge.py integration"),
        fetchall=False
    )


# ── Ensure claude_messages table exists ──────────────────────────────────────

def ensure_messages_table():
    """Create claude_messages table if it doesn't exist."""
    ddl_statements = [
        "CREATE TABLE IF NOT EXISTS claude_messages ("
        "id SERIAL PRIMARY KEY, "
        "direction TEXT NOT NULL, "
        "sender TEXT NOT NULL, "
        "message TEXT NOT NULL, "
        "metadata JSONB DEFAULT '{}', "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS idx_claude_messages_dir "
        "ON claude_messages(direction, created_at DESC)",
    ]
    for stmt in ddl_statements:
        try:
            pg_query(stmt, fetchall=False)
        except Exception:
            pass


# ── Subcommand: send ─────────────────────────────────────────────────────────

def cmd_send(args):
    """Send a message to Nova tagged as from claude-code."""
    message = " ".join(args.message)
    if not message:
        log("No message provided.", "ERROR")
        sys.exit(1)

    ensure_bridge_session()
    ensure_messages_table()

    # Store in PG for audit trail
    pg_query(
        "INSERT INTO claude_messages (direction, sender, message, metadata) "
        "VALUES (%s, %s, %s, %s)",
        ("to_nova", "claude-code", message, json.dumps({"timestamp": time.time()})),
        fetchall=False
    )

    # Insert into claude_queue for Nova to pick up
    pg_query(
        "INSERT INTO claude_queue (session_id, status, priority, description, context) "
        "VALUES (%s, %s, %s, %s, %s)",
        (BRIDGE_SESSION_ID, "queued", 3, f"message:{message}", json.dumps({
            "from": "claude-code",
            "type": "message",
            "timestamp": time.time()
        })),
        fetchall=False
    )

    # Post to #nova-claude Slack channel
    _post_slack_claude(f"*Claude Code:* {message[:2000]}")

    log(f"Message sent to Nova: {message[:80]}{'...' if len(message) > 80 else ''}")
    print(f"OK — message queued for Nova ({len(message)} chars)")


# ── Subcommand: receive ──────────────────────────────────────────────────────

def cmd_receive(args):
    """Get Nova's recent messages/responses for claude-code."""
    limit = args.limit or 10
    ensure_messages_table()

    # Check for responses from Nova
    rows = pg_query(
        "SELECT id, message, metadata, created_at "
        "FROM claude_messages "
        "WHERE direction = 'from_nova' "
        "ORDER BY created_at DESC LIMIT %s",
        (limit,)
    )

    # Also check completed queue items (Nova's responses to our messages)
    queue_rows = pg_query(
        "SELECT id, description, outcome, completed_at "
        "FROM claude_queue "
        "WHERE session_id = %s AND status = 'completed' "
        "ORDER BY completed_at DESC LIMIT %s",
        (BRIDGE_SESSION_ID, limit)
    )

    if not rows and not queue_rows:
        print("No messages from Nova found.")
        return

    if rows:
        print(f"=== Messages from Nova (last {limit}) ===")
        if isinstance(rows[0], dict):
            for row in rows:
                ts = row.get("created_at", "?")
                msg = row.get("message", "")
                print(f"  [{ts}] {msg}")
        else:
            for row in rows:
                print(f"  {row}")

    if queue_rows:
        print(f"\n=== Completed Queue Items ===")
        if isinstance(queue_rows[0], dict):
            for row in queue_rows:
                ts = row.get("completed_at", "?")
                desc = row.get("description", "")
                outcome = row.get("outcome", "")
                print(f"  [{ts}] {desc}")
                if outcome:
                    print(f"    -> {outcome}")
        else:
            for row in queue_rows:
                print(f"  {row}")


# ── Subcommand: decisions ────────────────────────────────────────────────────

def cmd_decisions(args):
    """Show Nova's recent decisions from scheduler runs and gateway log."""
    since = args.since or "1h"

    # Parse time offset
    unit = since[-1]
    try:
        value = int(since[:-1])
    except ValueError:
        log(f"Invalid --since value: {since}. Use format like '1h', '30m', '2d'.", "ERROR")
        sys.exit(1)

    if unit == "h":
        seconds_ago = value * 3600
    elif unit == "m":
        seconds_ago = value * 60
    elif unit == "d":
        seconds_ago = value * 86400
    else:
        log(f"Unknown time unit: {unit}. Use h/m/d.", "ERROR")
        sys.exit(1)

    cutoff_epoch_ms = (int(time.time()) - seconds_ago) * 1000

    # Query scheduler_runs (started_at is bigint epoch in MILLISECONDS)
    runs = pg_query(
        "SELECT task_id, task_script, task_group, started_at, ended_at, "
        "duration_ms, exit_code, status, error_tail, was_retry, retry_recovered "
        "FROM scheduler_runs "
        "WHERE started_at > %s "
        "ORDER BY started_at DESC LIMIT 50",
        (cutoff_epoch_ms,)
    )

    # Query gateway decisions (model choices, latency)
    cutoff_epoch = int(time.time()) - seconds_ago
    gw_rows = pg_query(
        "SELECT session_id, backend_used, model_used, prompt_length, "
        "response_length, latency_ms, fallback_used, created_at "
        "FROM gateway_query_log "
        "WHERE created_at > to_timestamp(%s) "
        "ORDER BY created_at DESC LIMIT 20",
        (cutoff_epoch,)
    )

    print(f"=== Nova Decisions (since {since} ago) ===\n")

    if runs:
        print(f"--- Scheduler Runs ({len(runs)} tasks executed) ---")
        if isinstance(runs[0], dict):
            for r in runs:
                status_icon = "OK" if r.get("exit_code") == 0 else "FAIL"
                duration = r.get("duration_ms", 0)
                duration_str = f"{duration}ms" if duration and duration < 10000 else f"{(duration or 0)/1000:.1f}s"
                # started_at is millisecond epoch
                raw_ts = r.get("started_at")
                if isinstance(raw_ts, (int, float)) and raw_ts > 1e12:
                    started = datetime.fromtimestamp(raw_ts / 1000, tz=timezone.utc).strftime("%H:%M:%S")
                elif isinstance(raw_ts, (int, float)):
                    started = datetime.fromtimestamp(raw_ts, tz=timezone.utc).strftime("%H:%M:%S")
                else:
                    started = str(raw_ts or "?")
                task = r.get("task_id", "?")
                print(f"  [{started}] {status_icon} {task} ({duration_str})")
                if r.get("error_tail"):
                    print(f"           error: {r['error_tail'][:120]}")
                if r.get("retry_recovered"):
                    print(f"           (recovered on retry)")
        else:
            for r in runs:
                print(f"  {r}")
    else:
        print("--- No scheduler runs in this period ---")

    if gw_rows:
        print(f"\n--- Gateway Model Decisions ({len(gw_rows)} queries) ---")
        if isinstance(gw_rows[0], dict):
            for g in gw_rows:
                model = g.get("model_used", "?")
                backend = g.get("backend_used", "?")
                latency = g.get("latency_ms", "?")
                fallback = " [FALLBACK]" if g.get("fallback_used") else ""
                ts = g.get("created_at", "?")
                print(f"  [{ts}] {backend}/{model} — {latency}ms{fallback}")
        else:
            for g in gw_rows:
                print(f"  {g}")
    else:
        print("\n--- No gateway queries in this period ---")


# ── Subcommand: trigger ──────────────────────────────────────────────────────

def cmd_trigger(args):
    """Trigger a Nova scheduler task on demand."""
    task_id = args.task_id

    # Use the scheduler's /run/<task_id> endpoint
    result = http_request(f"{SCHEDULER_HTTP}/run/{task_id}", method="GET")

    if result.get("queued"):
        print(f"OK — task '{task_id}' triggered on scheduler")
    elif result.get("error") == "unknown task":
        # List available tasks
        tasks = http_request(f"{SCHEDULER_HTTP}/tasks")
        if isinstance(tasks, dict) and "error" not in tasks:
            available = sorted(tasks.keys())
            print(f"ERROR: Unknown task '{task_id}'")
            print(f"\nAvailable tasks ({len(available)}):")
            for t in available:
                info = tasks[t]
                status = "running" if info.get("running") else "idle"
                print(f"  {t} [{status}] — {info.get('schedule', '?')}")
        else:
            print(f"ERROR: Unknown task '{task_id}' (couldn't fetch task list)")
        sys.exit(1)
    elif "error" in result:
        # Connection failed or other error — fallback to queue
        log(f"Scheduler HTTP failed ({result['error']}), using queue fallback.", "WARN")
        ensure_bridge_session()
        pg_query(
            "INSERT INTO claude_queue (session_id, status, priority, description, context) "
            "VALUES (%s, %s, %s, %s, %s)",
            (BRIDGE_SESSION_ID, "queued", 2, f"trigger:{task_id}", json.dumps({
                "from": "claude-code",
                "type": "trigger",
                "task_id": task_id,
                "timestamp": time.time()
            })),
            fetchall=False
        )
        print(f"Task '{task_id}' queued via claude_queue (scheduler unreachable)")
    else:
        print(f"Scheduler response: {json.dumps(result, indent=2)}")


# ── Subcommand: scratch ──────────────────────────────────────────────────────

def cmd_scratch(args):
    """Shared scratchpad operations using Redis."""
    action = args.scratch_action

    if action == "write":
        if not args.key or not args.value:
            log("Both key and value required for scratch write.", "ERROR")
            sys.exit(1)
        key = SCRATCHPAD_PREFIX + args.key
        value = " ".join(args.value) if isinstance(args.value, list) else args.value
        ttl = args.ttl or SCRATCHPAD_TTL

        redis_cmd("SET", key, value, "EX", str(ttl))
        print(f"OK — wrote '{args.key}' (TTL: {ttl}s)")

    elif action == "read":
        if not args.key:
            log("Key required for scratch read.", "ERROR")
            sys.exit(1)
        key = SCRATCHPAD_PREFIX + args.key
        value = redis_cmd("GET", key)
        if value == "(nil)" or not value:
            print(f"Key '{args.key}' not found (expired or never set)")
            sys.exit(1)
        else:
            print(value)

    elif action == "list":
        # List all keys in the scratchpad namespace
        keys_raw = redis_cmd("KEYS", f"{SCRATCHPAD_PREFIX}*")
        if not keys_raw or keys_raw == "(empty array)" or keys_raw == "(empty list or set)":
            print("Scratchpad is empty.")
            return

        keys = [k for k in keys_raw.split("\n") if k.strip()]
        print(f"=== Scratchpad ({len(keys)} keys) ===")
        for full_key in sorted(keys):
            short_key = full_key.replace(SCRATCHPAD_PREFIX, "", 1)
            ttl_val = redis_cmd("TTL", full_key)
            value = redis_cmd("GET", full_key)
            preview = value[:60] + "..." if len(value) > 60 else value
            print(f"  {short_key} (TTL: {ttl_val}s) = {preview}")

    elif action == "delete":
        if not args.key:
            log("Key required for scratch delete.", "ERROR")
            sys.exit(1)
        key = SCRATCHPAD_PREFIX + args.key
        result = redis_cmd("DEL", key)
        if result == "1":
            print(f"OK — deleted '{args.key}'")
        else:
            print(f"Key '{args.key}' not found")

    else:
        log(f"Unknown scratch action: {action}", "ERROR")
        sys.exit(1)


# ── Subcommand: handoff ─────────────────────────────────────────────────────

def cmd_handoff(args):
    """Write a session handoff summary for Nova to pick up."""
    summary = " ".join(args.summary)
    if not summary:
        log("No handoff summary provided.", "ERROR")
        sys.exit(1)

    ensure_bridge_session()
    ensure_messages_table()

    description = f"HANDOFF: {summary}"

    # 1. Write to claude_queue with status='queued', priority=1
    pg_query(
        "INSERT INTO claude_queue (session_id, status, priority, description, context) "
        "VALUES (%s, %s, %s, %s, %s)",
        (BRIDGE_SESSION_ID, "queued", 1, description, json.dumps({
            "from": "claude-code",
            "type": "handoff",
            "timestamp": time.time(),
        })),
        fetchall=False
    )

    # 2. Write to Redis with 48h TTL
    redis_cmd("SET", HANDOFF_KEY, summary, "EX", str(HANDOFF_TTL))

    # 3. Send a message to Nova via claude_messages
    handoff_message = f"Claude Code session ending. Handoff: {summary}"
    pg_query(
        "INSERT INTO claude_messages (direction, sender, message, metadata) "
        "VALUES (%s, %s, %s, %s)",
        ("to_nova", "claude-code", handoff_message, json.dumps({
            "type": "handoff",
            "timestamp": time.time(),
        })),
        fetchall=False
    )

    log(f"Handoff written: {summary[:80]}{'...' if len(summary) > 80 else ''}")
    print(f"OK — handoff queued for Nova ({len(summary)} chars)")
    print(f"  • claude_queue: priority 1, status queued")
    print(f"  • Redis: {HANDOFF_KEY} (TTL {HANDOFF_TTL}s = 48h)")
    print(f"  • claude_messages: notification sent")


# ── Subcommand: editing ─────────────────────────────────────────────────────

def cmd_editing(args):
    """Mark a file as being edited by Claude Code (prevents Nova restarts)."""
    filepath = args.filepath
    if not filepath:
        log("No filepath provided.", "ERROR")
        sys.exit(1)

    key = f"{EDITING_PREFIX}{filepath}"
    redis_cmd("SET", key, json.dumps({
        "editor": "claude-code",
        "started": time.time(),
        "filepath": filepath,
    }), "EX", str(EDITING_TTL))

    log(f"Edit lock set: {filepath} (TTL {EDITING_TTL}s)")
    print(f"OK — edit lock set on {filepath} (auto-expires in {EDITING_TTL // 60} min)")


# ── Subcommand: done-editing ────────────────────────────────────────────────

def cmd_done_editing(args):
    """Clear the edit lock on a file."""
    filepath = args.filepath
    if not filepath:
        log("No filepath provided.", "ERROR")
        sys.exit(1)

    key = f"{EDITING_PREFIX}{filepath}"
    result = redis_cmd("DEL", key)
    if result == "1":
        log(f"Edit lock cleared: {filepath}")
        print(f"OK — edit lock cleared for {filepath}")
    else:
        print(f"No active edit lock found for {filepath}")


# ── Subcommand: editing-status ──────────────────────────────────────────────

def cmd_editing_status(args):
    """Show all files currently locked for editing by Claude Code."""
    keys_raw = redis_cmd("KEYS", f"{EDITING_PREFIX}*")
    if not keys_raw or keys_raw in ("(empty array)", "(empty list or set)"):
        print("No files currently being edited.")
        return

    keys = [k for k in keys_raw.split("\n") if k.strip()]
    print(f"=== Files Being Edited ({len(keys)}) ===")
    for full_key in sorted(keys):
        filepath = full_key.replace(EDITING_PREFIX, "", 1)
        ttl_val = redis_cmd("TTL", full_key)
        value = redis_cmd("GET", full_key)
        try:
            data = json.loads(value)
            started = data.get("started", 0)
            elapsed = int(time.time() - started) if started else 0
            elapsed_str = f"{elapsed // 60}m {elapsed % 60}s ago"
        except (json.JSONDecodeError, TypeError):
            elapsed_str = "unknown"
        print(f"  {filepath}")
        print(f"    TTL: {ttl_val}s remaining | Started: {elapsed_str}")


# ── Subcommand: review ─────────────────────────────────────────────────────

def cmd_review(args):
    """Ask Nova to review recent changes to a file."""
    filepath = args.filepath
    if not filepath:
        log("No filepath provided.", "ERROR")
        sys.exit(1)

    # Get the git diff for the file (try multiple strategies)
    from pathlib import Path
    scripts_dir = str(Path.home() / ".openclaw/scripts")
    diff_text = ""

    # Strategy 1: diff against HEAD~1
    result = subprocess.run(
        ["git", "-C", scripts_dir, "diff", "HEAD~1", "--", filepath],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0 and result.stdout.strip():
        diff_text = result.stdout.strip()

    # Strategy 2: unstaged changes
    if not diff_text:
        result = subprocess.run(
            ["git", "-C", scripts_dir, "diff", "--", filepath],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            diff_text = result.stdout.strip()

    # Strategy 3: staged changes
    if not diff_text:
        result = subprocess.run(
            ["git", "-C", scripts_dir, "diff", "--cached", "--", filepath],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            diff_text = result.stdout.strip()

    if not diff_text:
        print(f"No changes found for {filepath} (checked HEAD~1, unstaged, and staged)")
        sys.exit(1)

    # Truncate extremely large diffs
    max_diff_chars = 8000
    if len(diff_text) > max_diff_chars:
        diff_text = diff_text[:max_diff_chars] + "\n\n... [diff truncated at 8000 chars] ..."

    # Build the review prompt for Nova
    review_prompt = (
        f"Review this diff to {filepath}. Check for: conflicts with my runtime state, "
        f"broken assumptions about scheduling/config, potential bugs, missing error handling. "
        f"Be specific about what concerns you.\n\n"
        f"```diff\n{diff_text}\n```"
    )

    ensure_bridge_session()
    ensure_messages_table()

    # Store the review request in claude_messages for Nova's LLM to pick up
    metadata = json.dumps({
        "type": "code_review",
        "file": filepath,
        "timestamp": time.time(),
        "diff_lines": diff_text.count("\n"),
    })

    pg_query(
        "INSERT INTO claude_messages (direction, sender, message, metadata) "
        "VALUES (%s, %s, %s, %s)",
        ("to_nova", "claude-code", review_prompt, metadata),
        fetchall=False
    )

    # Also queue it so Nova's gateway picks it up actively
    pg_query(
        "INSERT INTO claude_queue (session_id, status, priority, description, context) "
        "VALUES (%s, %s, %s, %s, %s)",
        (BRIDGE_SESSION_ID, "queued", 3,
         f"CODE_REVIEW: {filepath}",
         json.dumps({
             "from": "claude-code",
             "type": "code_review",
             "file": filepath,
             "diff": diff_text,
             "prompt": review_prompt,
             "timestamp": time.time(),
         })),
        fetchall=False
    )

    # Notify via Redis for real-time pickup
    try:
        redis_cmd("PUBLISH", "nova:to_claude", json.dumps({
            "type": "code_review_request",
            "file": filepath,
            "ts": time.time(),
        }))
    except Exception:
        pass  # Fire-and-forget

    lines_changed = diff_text.count("\n+") + diff_text.count("\n-")
    print(f"OK — review request sent to Nova for {filepath}")
    print(f"  • Diff: ~{lines_changed} lines changed")
    print(f"  • claude_messages: review prompt stored")
    print(f"  • claude_queue: queued for processing")
    print(f"  • Check response with: {sys.argv[0]} receive")


# ── CLI Setup ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Nova-Claude Code integration bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s send "Hey Nova, what's the scheduler status?"
  %(prog)s receive --limit 5
  %(prog)s decisions --since 2h
  %(prog)s trigger daily_essay
  %(prog)s scratch write "current_task" "Fixing memory leak in HomeKit"
  %(prog)s scratch read "current_task"
  %(prog)s scratch list
  %(prog)s handoff "Fixed gateway v2 timeout bug. Still need to test Signal reconnect."
  %(prog)s editing ~/.openclaw/scripts/nova_gateway_v2.py
  %(prog)s done-editing ~/.openclaw/scripts/nova_gateway_v2.py
  %(prog)s editing-status
  %(prog)s review ~/.openclaw/scripts/nova_gateway_v2.py
"""
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # send
    p_send = subparsers.add_parser("send", help="Send a message to Nova")
    p_send.add_argument("message", nargs="+", help="Message text")
    p_send.set_defaults(func=cmd_send)

    # receive
    p_recv = subparsers.add_parser("receive", help="Get Nova's recent messages")
    p_recv.add_argument("--limit", type=int, default=10, help="Max messages to show (default: 10)")
    p_recv.set_defaults(func=cmd_receive)

    # decisions
    p_dec = subparsers.add_parser("decisions", help="View Nova's recent decisions")
    p_dec.add_argument("--since", default="1h", help="Time window (e.g. 1h, 30m, 2d)")
    p_dec.set_defaults(func=cmd_decisions)

    # trigger
    p_trig = subparsers.add_parser("trigger", help="Trigger a scheduler task")
    p_trig.add_argument("task_id", help="Task ID to trigger")
    p_trig.set_defaults(func=cmd_trigger)

    # scratch
    p_scratch = subparsers.add_parser("scratch", help="Shared scratchpad (Redis)")
    p_scratch.add_argument("scratch_action", choices=["read", "write", "list", "delete"],
                           help="Scratchpad action")
    p_scratch.add_argument("key", nargs="?", help="Key name")
    p_scratch.add_argument("value", nargs="*", help="Value (for write)")
    p_scratch.add_argument("--ttl", type=int, default=None,
                           help=f"TTL in seconds (default: {SCRATCHPAD_TTL})")
    p_scratch.set_defaults(func=cmd_scratch)

    # handoff
    p_handoff = subparsers.add_parser("handoff", help="Write session handoff for Nova")
    p_handoff.add_argument("summary", nargs="+", help="Handoff summary text")
    p_handoff.set_defaults(func=cmd_handoff)

    # editing
    p_editing = subparsers.add_parser("editing", help="Mark a file as being edited")
    p_editing.add_argument("filepath", help="Absolute path to the file being edited")
    p_editing.set_defaults(func=cmd_editing)

    # done-editing
    p_done_editing = subparsers.add_parser("done-editing", help="Clear edit lock on a file")
    p_done_editing.add_argument("filepath", help="Absolute path to the file")
    p_done_editing.set_defaults(func=cmd_done_editing)

    # editing-status
    p_editing_status = subparsers.add_parser("editing-status", help="Show files currently locked")
    p_editing_status.set_defaults(func=cmd_editing_status)

    # review
    p_review = subparsers.add_parser("review", help="Ask Nova to review recent changes to a file")
    p_review.add_argument("filepath", help="Path to the file to review (git diff HEAD~1)")
    p_review.set_defaults(func=cmd_review)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        log(f"Unhandled error: {e}", "ERROR")
        sys.exit(1)


if __name__ == "__main__":
    main()
