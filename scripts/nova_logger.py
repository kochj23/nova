"""
nova_logger.py — Centralized structured logging for all Nova scripts.

Writes JSON-lines to a rolling log file. All scripts should import this
instead of using bare print() statements.

Usage:
    from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN, LOG_DEBUG

    log("Script started", level=LOG_INFO, source="nova_nightly_report")
    log("Failed to connect", level=LOG_ERROR, source="nova_mail_agent",
        extra={"host": "smtp.gmail.com", "error": str(e)})

Log file: ~/.openclaw/logs/nova.jsonl  (auto-rotated at 50 MB, keeps 5 files)

Written by Jordan Koch.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Log Levels ───────────────────────────────────────────────────────────────
LOG_DEBUG = "debug"
LOG_INFO  = "info"
LOG_WARN  = "warn"
LOG_ERROR = "error"
LOG_FATAL = "fatal"

_LEVEL_ORDER = {LOG_DEBUG: 0, LOG_INFO: 1, LOG_WARN: 2, LOG_ERROR: 3, LOG_FATAL: 4}

# ── Config ───────────────────────────────────────────────────────────────────
LOG_DIR        = Path.home() / ".openclaw" / "logs"
LOG_FILE       = LOG_DIR / "nova.jsonl"
MAX_SIZE_BYTES = 50 * 1024 * 1024   # 50 MB per file
MAX_FILES      = 5                   # Keep 5 rotated files
MIN_LEVEL      = os.environ.get("NOVA_LOG_LEVEL", LOG_INFO)

# ── Ensure directory exists ──────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Rotation ─────────────────────────────────────────────────────────────────
def _rotate():
    """Rotate log file when it exceeds MAX_SIZE_BYTES."""
    if not LOG_FILE.exists():
        return
    if LOG_FILE.stat().st_size < MAX_SIZE_BYTES:
        return

    # Shift existing rotated files: nova.jsonl.4 → nova.jsonl.5, etc.
    for i in range(MAX_FILES, 0, -1):
        src = LOG_DIR / f"nova.jsonl.{i}"
        dst = LOG_DIR / f"nova.jsonl.{i + 1}"
        if src.exists():
            if i == MAX_FILES:
                src.unlink()  # Delete oldest
            else:
                src.rename(dst)

    # Current → .1
    LOG_FILE.rename(LOG_DIR / "nova.jsonl.1")


# ── Core Logger ──────────────────────────────────────────────────────────────
def log(message: str, *, level: str = LOG_INFO, source: str = "",
        extra: dict = None):
    """Write a structured JSON log line.

    Args:
        message: Human-readable log message.
        level:   One of LOG_DEBUG, LOG_INFO, LOG_WARN, LOG_ERROR, LOG_FATAL.
        source:  Script or module name (e.g. "nova_nightly_report").
        extra:   Optional dict of additional structured data.
    """
    # Level filtering
    if _LEVEL_ORDER.get(level, 1) < _LEVEL_ORDER.get(MIN_LEVEL, 1):
        return

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "source": source or _guess_source(),
        "msg": message,
    }
    if extra:
        entry["extra"] = extra

    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))

    # Write to file
    try:
        _rotate()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # Don't crash the calling script if logging fails

    # Also print to stderr for cron/launchd capture
    if level in (LOG_ERROR, LOG_FATAL):
        print(f"[{level.upper()}] {entry['source']}: {message}", file=sys.stderr)
    elif level == LOG_WARN:
        print(f"[WARN] {entry['source']}: {message}", file=sys.stderr)


def _guess_source() -> str:
    """Infer source from the calling script's filename."""
    import inspect
    for frame_info in inspect.stack():
        fname = Path(frame_info.filename).stem
        if fname != "nova_logger" and fname != "<stdin>":
            return fname
    return "unknown"


# ── Query Helper (for /api/logs) ─────────────────────────────────────────────
def read_logs(n: int = 100, level: str = None, source: str = None,
              since: str = None) -> list:
    """Read the last N log entries, optionally filtered.

    Args:
        n:      Max entries to return (newest first).
        level:  Filter to this level or higher.
        source: Filter to this source name.
        since:  ISO timestamp — only return entries after this time.

    Returns:
        List of log entry dicts, newest first.
    """
    results = []
    min_ord = _LEVEL_ORDER.get(level, 0) if level else 0

    # Read from current + rotated files until we have enough
    files = [LOG_FILE] + sorted(LOG_DIR.glob("nova.jsonl.[0-9]*"))
    for f in files:
        if not f.exists():
            continue
        try:
            lines = f.read_text(encoding="utf-8").strip().split("\n")
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Filters
            if min_ord and _LEVEL_ORDER.get(entry.get("level"), 0) < min_ord:
                continue
            if source and entry.get("source") != source:
                continue
            if since and entry.get("ts", "") < since:
                continue
            results.append(entry)
            if len(results) >= n:
                return results
    return results
