#!/usr/bin/env python3
"""
nova_log_rotate.py — Weekly log rotation for Nova's cron and script logs.

- Trims cron run .jsonl files to last 30 days of entries
- Rotates ~/.openclaw/logs/*.log files larger than 5MB (keeps last 5MB)
- Reports space freed to Slack

Runs weekly on Monday at 3am via launchd.
Written by Jordan Koch.
"""

import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN   = nova_config.slack_bot_token()
SLACK_CHANNEL = "C0ATAF7NZG9"
CRON_RUNS_DIR = Path.home() / ".openclaw/cron/runs"
LOGS_DIR      = Path.home() / ".openclaw/logs"
CUTOFF        = datetime.now() - timedelta(days=30)
MAX_LOG_BYTES = 5 * 1024 * 1024   # 5MB per log file


def log(msg):
    print(f"[nova_log_rotate {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text: str):
    payload = json.dumps({"channel": SLACK_CHANNEL, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            pass
    except Exception as e:
        log(f"Slack post failed: {e}")


def trim_jsonl(path: Path) -> tuple[int, int]:
    """Remove entries older than CUTOFF. Returns (lines_before, lines_after)."""
    try:
        lines = path.read_text().splitlines()
        kept = []
        for line in lines:
            try:
                entry = json.loads(line)
                ts = entry.get("ts", 0) / 1000
                if datetime.fromtimestamp(ts) >= CUTOFF:
                    kept.append(line)
            except Exception:
                kept.append(line)  # keep malformed lines

        if len(kept) < len(lines):
            path.write_text("\n".join(kept) + ("\n" if kept else ""))

        return len(lines), len(kept)
    except Exception as e:
        log(f"Error trimming {path.name}: {e}")
        return 0, 0


def trim_log_file(path: Path) -> int:
    """If log exceeds MAX_LOG_BYTES, keep only the last MAX_LOG_BYTES. Returns bytes freed."""
    try:
        size = path.stat().st_size
        if size <= MAX_LOG_BYTES:
            return 0
        content = path.read_bytes()
        trimmed = content[-MAX_LOG_BYTES:]
        # Find next newline to avoid splitting a line
        newline_pos = trimmed.find(b"\n")
        if newline_pos > 0:
            trimmed = trimmed[newline_pos + 1:]
        path.write_bytes(trimmed)
        freed = size - len(trimmed)
        return freed
    except Exception as e:
        log(f"Error rotating {path.name}: {e}")
        return 0


def main():
    log("Starting log rotation")
    total_freed = 0
    jsonl_trimmed = 0
    jsonl_lines_removed = 0

    # Trim cron run jsonl files
    if CRON_RUNS_DIR.exists():
        for jsonl in sorted(CRON_RUNS_DIR.glob("*.jsonl")):
            before, after = trim_jsonl(jsonl)
            removed = before - after
            if removed > 0:
                log(f"  {jsonl.name}: removed {removed} old entries ({before} → {after})")
                jsonl_trimmed += 1
                jsonl_lines_removed += removed

    # Rotate large log files
    log_files_rotated = 0
    if LOGS_DIR.exists():
        for log_file in sorted(LOGS_DIR.glob("*.log")):
            freed = trim_log_file(log_file)
            if freed > 0:
                log(f"  {log_file.name}: freed {freed / 1024:.0f}KB")
                total_freed += freed
                log_files_rotated += 1

    freed_mb = total_freed / (1024 * 1024)
    log(f"Done — {jsonl_trimmed} jsonl files trimmed ({jsonl_lines_removed} entries removed), "
        f"{log_files_rotated} log files rotated ({freed_mb:.1f}MB freed)")

    if jsonl_trimmed > 0 or log_files_rotated > 0:
        slack_post(
            f"*Nova Log Rotation* 🗂️\n"
            f"• {jsonl_trimmed} cron history files trimmed to 30 days "
            f"({jsonl_lines_removed} old entries removed)\n"
            f"• {log_files_rotated} log files truncated to 5MB "
            f"({freed_mb:.1f}MB freed)"
        )


if __name__ == "__main__":
    main()
