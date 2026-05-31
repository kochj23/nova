#!/usr/bin/env python3
"""
nova_watcher_engine.py — Persistent watcher evaluation engine.

Runs every 2 minutes via scheduler. Loads all active watchers from PG,
evaluates their conditions, triggers actions on match, respects cooldowns.

Watcher types: http, rss, file, db_query
Actions: slack_notify, queue_for_claude, run_script, send_message

Written by Jordan Koch (via Claude).
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

DB_HOST = "localhost"
DB_NAME = "nova_ops"
DB_USER = "kochj"
LOG_PREFIX = "[watcher-engine]"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{LOG_PREFIX} {ts} {msg}", flush=True)


def db_query(sql: str) -> list:
    result = subprocess.run(
        ["psql", "-h", DB_HOST, "-U", DB_USER, "-d", DB_NAME, "-t", "-A", "-F", "\t", "-c", sql],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        return []
    return [line.split("\t") for line in result.stdout.strip().split("\n") if line.strip()]


def db_exec(sql: str):
    subprocess.run(
        ["psql", "-h", DB_HOST, "-U", DB_USER, "-d", DB_NAME, "-c", sql],
        capture_output=True, text=True, timeout=15
    )


def load_watchers() -> list:
    rows = db_query(
        "SELECT watcher_id, name, watcher_type, target, condition::text, action::text, "
        "interval_s, cooldown_s, last_check, last_triggered, last_value, consecutive_errors "
        "FROM watchers WHERE enabled = true AND status = 'active'"
    )
    watchers = []
    for row in rows:
        if len(row) < 12:
            continue
        watchers.append({
            "id": row[0], "name": row[1], "type": row[2], "target": row[3],
            "condition": json.loads(row[4]) if row[4] else {},
            "action": json.loads(row[5]) if row[5] else {},
            "interval_s": int(row[6]), "cooldown_s": int(row[7]),
            "last_check": row[8] if row[8] else None,
            "last_triggered": row[9] if row[9] else None,
            "last_value": row[10] if row[10] else None,
            "consecutive_errors": int(row[11]),
        })
    return watchers


def is_due(watcher: dict) -> bool:
    if not watcher["last_check"]:
        return True
    try:
        from datetime import datetime, timezone
        last = datetime.fromisoformat(watcher["last_check"].replace("+00", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed >= watcher["interval_s"]
    except Exception:
        return True


def cooldown_active(watcher: dict) -> bool:
    if not watcher["last_triggered"]:
        return False
    try:
        from datetime import datetime, timezone
        last = datetime.fromisoformat(watcher["last_triggered"].replace("+00", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed < watcher["cooldown_s"]
    except Exception:
        return False


# ── Check functions ───────────────────────────────────────────────────────────

def check_http(watcher: dict) -> tuple:
    """Returns (triggered: bool, new_value: str, error: str)"""
    try:
        req = urllib.request.Request(watcher["target"], method="GET")
        req.add_header("User-Agent", "Nova-Watcher/1.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")[:5000]

        cond = watcher["condition"]
        new_value = f"{status}:{hashlib.md5(body.encode()).hexdigest()[:16]}"

        if "status_not" in cond and status == cond["status_not"]:
            return False, new_value, ""
        if "status_not" in cond and status != cond["status_not"]:
            return True, new_value, ""
        if "body_contains" in cond and cond["body_contains"] in body:
            return True, new_value, ""
        if "body_not_contains" in cond and cond["body_not_contains"] not in body:
            return True, new_value, ""
        if "status_code_changed" in cond and watcher["last_value"]:
            old_status = watcher["last_value"].split(":")[0]
            if str(status) != old_status:
                return True, new_value, ""
        if "body_changed" in cond and watcher["last_value"]:
            old_hash = watcher["last_value"].split(":")[-1] if ":" in watcher["last_value"] else ""
            new_hash = new_value.split(":")[-1]
            if old_hash and old_hash != new_hash:
                return True, new_value, ""

        return False, new_value, ""
    except urllib.error.HTTPError as e:
        cond = watcher["condition"]
        if "status_not" in cond and e.code != cond["status_not"]:
            return True, f"{e.code}:error", ""
        return False, f"{e.code}:error", str(e)
    except Exception as e:
        return False, "", str(e)


def check_rss(watcher: dict) -> tuple:
    """Check RSS feed for new items."""
    try:
        req = urllib.request.Request(watcher["target"])
        req.add_header("User-Agent", "Nova-Watcher/1.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")

        import re
        items = re.findall(r'<item[^>]*>.*?</item>', body, re.DOTALL)
        if not items:
            items = re.findall(r'<entry[^>]*>.*?</entry>', body, re.DOTALL)

        current_hash = hashlib.md5(items[0].encode()).hexdigest()[:16] if items else "empty"

        if watcher["last_value"] and watcher["last_value"] != current_hash:
            return True, current_hash, ""
        return False, current_hash, ""
    except Exception as e:
        return False, "", str(e)


def check_file(watcher: dict) -> tuple:
    """Check file for modifications."""
    try:
        p = Path(watcher["target"])
        if not p.exists():
            return False, "missing", "File not found"

        stat = p.stat()
        new_value = f"{stat.st_mtime}:{stat.st_size}"

        cond = watcher["condition"]
        if "modified" in cond and watcher["last_value"] and watcher["last_value"] != new_value:
            return True, new_value, ""
        if "size_exceeds" in cond and stat.st_size > cond["size_exceeds"]:
            return True, new_value, ""

        return False, new_value, ""
    except Exception as e:
        return False, "", str(e)


def check_db_query(watcher: dict) -> tuple:
    """Execute a SQL query and check result against condition."""
    try:
        rows = db_query(watcher["target"])
        result_str = json.dumps(rows)[:1000]
        new_value = hashlib.md5(result_str.encode()).hexdigest()[:16]

        cond = watcher["condition"]
        row_count = len(rows)

        if "row_count_gt" in cond and row_count > cond["row_count_gt"]:
            return True, new_value, ""
        if "row_count_eq" in cond and row_count == cond["row_count_eq"]:
            return True, new_value, ""
        if "value_changed" in cond and watcher["last_value"] and watcher["last_value"] != new_value:
            return True, new_value, ""

        return False, new_value, ""
    except Exception as e:
        return False, "", str(e)


CHECKERS = {
    "http": check_http,
    "rss": check_rss,
    "file": check_file,
    "db_query": check_db_query,
}


# ── Action functions ─────────────────────────────────────────────────────────

def execute_action(watcher: dict, new_value: str):
    action = watcher["action"]
    action_type = action.get("type", "slack_notify")

    if action_type == "slack_notify":
        template = action.get("template", "Watcher '{name}' triggered: {new_value}")
        msg = template.format(name=watcher["name"], new_value=new_value[:200],
                              target=watcher["target"], type=watcher["type"])
        channel = action.get("channel", nova_config.SLACK_NOTIFY)
        nova_config.post_both(f":eye: {msg}", slack_channel=channel)

    elif action_type == "run_script":
        script = action.get("script", "")
        if script and Path(f"{Path.home()}/.openclaw/scripts/{script}").exists():
            subprocess.Popen(
                ["/opt/homebrew/bin/python3", str(Path.home() / f".openclaw/scripts/{script}")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

    elif action_type == "queue_for_claude":
        desc = action.get("description", f"Watcher '{watcher['name']}' triggered")
        ctx = json.dumps({"watcher": watcher["name"], "new_value": new_value[:200]}).replace("'", "''")
        db_exec(f"INSERT INTO claude_queue (session_id, status, priority, description, context) "
                f"VALUES ('watcher', 'queued', 2, '{desc}', '{ctx}')")

    log(f"  Action: {action_type} for '{watcher['name']}'")


# ── Main loop ────────────────────────────────────────────────────────────────

def run():
    watchers = load_watchers()
    if not watchers:
        return

    log(f"Loaded {len(watchers)} active watchers")
    checked = 0
    triggered = 0

    for w in watchers:
        if not is_due(w):
            continue

        checker = CHECKERS.get(w["type"])
        if not checker:
            log(f"  Unknown type '{w['type']}' for '{w['name']}'")
            continue

        fired, new_value, error = checker(w)
        checked += 1

        # Update check state
        if error:
            db_exec(f"UPDATE watchers SET last_check = now(), last_error = '{error[:200]}', "
                    f"consecutive_errors = consecutive_errors + 1, check_count = check_count + 1 "
                    f"WHERE watcher_id = '{w['id']}'")
            if w["consecutive_errors"] + 1 >= 5:
                db_exec(f"UPDATE watchers SET status = 'error' WHERE watcher_id = '{w['id']}'")
                nova_config.post_both(
                    f":warning: Watcher '{w['name']}' disabled after 5 consecutive errors: {error[:100]}",
                    slack_channel=nova_config.SLACK_NOTIFY
                )
        else:
            escaped_value = new_value.replace("'", "''")
            db_exec(f"UPDATE watchers SET last_check = now(), last_value = '{escaped_value}', "
                    f"last_error = NULL, consecutive_errors = 0, check_count = check_count + 1 "
                    f"WHERE watcher_id = '{w['id']}'")

        if fired and not cooldown_active(w):
            triggered += 1
            execute_action(w, new_value)
            db_exec(f"UPDATE watchers SET last_triggered = now(), trigger_count = trigger_count + 1 "
                    f"WHERE watcher_id = '{w['id']}'")
            # Log event
            escaped_old = (w["last_value"] or "").replace("'", "''")
            escaped_new = new_value.replace("'", "''")
            db_exec(f"INSERT INTO watcher_events (watcher_id, event_type, old_value, new_value, action_taken) "
                    f"VALUES ('{w['id']}', 'triggered', '{escaped_old}', '{escaped_new}', "
                    f"'{w['action'].get('type', 'unknown')}')")

    if checked:
        log(f"Done: checked={checked}, triggered={triggered}")


if __name__ == "__main__":
    run()
