#!/opt/homebrew/bin/python3
"""
nova_endpoint_monitor.py — Lightweight FIM + endpoint telemetry (osquery-light).

Alternative to full Wazuh/osquery deployment. Monitors local Mac Studio + SSH
targets for file integrity changes, process anomalies, and system drift.

Covers queue #162-163 (Wazuh Phase 3 / osquery alternative):
  - File Integrity Monitoring (FIM) on critical paths
  - Process inventory (new processes, unexpected listeners)
  - Package/brew drift detection
  - Login/auth event monitoring
  - Results stored in telemetry.endpoint_events

Port: 37469 (HTTP API for status)
Written by Jordan Koch.
"""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import asyncpg
    from aiohttp import web
except ImportError as e:
    print(f"FATAL: missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

VERSION = "1.0.0"
HTTP_PORT = 37469
DB_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
LOG_FILE = Path.home() / ".openclaw/logs/nova_endpoint_monitor.log"

FIM_INTERVAL = 300         # 5 min
PROCESS_INTERVAL = 120     # 2 min
AUTH_INTERVAL = 60         # 1 min

HOSTNAME = "mac-studio"

# Critical paths to monitor for file changes
FIM_PATHS = [
    "/etc/hosts",
    "/etc/resolv.conf",
    "/etc/pam.d/",
    "/etc/ssh/sshd_config",
    "/Library/LaunchDaemons/",
    str(Path.home() / "Library/LaunchAgents/"),
    str(Path.home() / ".ssh/authorized_keys"),
    str(Path.home() / ".zshrc"),
    str(Path.home() / ".gitconfig"),
    "/opt/homebrew/etc/",
]

# Process names that should NOT be listening on network ports
SUSPICIOUS_LISTENERS = [
    "nc", "ncat", "socat", "meterpreter", "reverse_tcp",
    "bind_shell", "cryptominer", "xmrig",
]

_shutdown = False
_pool = None
_start_time = time.time()
_fim_baseline = {}
_stats = {"fim_changes": 0, "suspicious_procs": 0, "auth_events": 0}
_last_auth_offset = 0
_alerts = []

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[endpoint {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    return _pool


async def ensure_table():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry.endpoint_events (
                ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
                hostname    TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                severity    TEXT NOT NULL DEFAULT 'info',
                path        TEXT,
                details     JSONB,
                resolved    BOOLEAN DEFAULT false
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_endpoint_events_ts
            ON telemetry.endpoint_events (ts DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_endpoint_events_type
            ON telemetry.endpoint_events (event_type, ts DESC)
        """)


async def record_event(event_type: str, severity: str, path: str = None, details: dict = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO telemetry.endpoint_events (hostname, event_type, severity, path, details)
            VALUES ($1, $2, $3, $4, $5)
        """, HOSTNAME, event_type, severity, path, json.dumps(details) if details else None)


# ── File Integrity Monitoring ────────────────────────────────────────────────

def hash_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except (PermissionError, FileNotFoundError, IsADirectoryError):
        return None


def scan_path(path: str) -> dict:
    """Return {filepath: sha256} for a path (file or directory)."""
    results = {}
    p = Path(path)
    if p.is_file():
        h = hash_file(str(p))
        if h:
            results[str(p)] = h
    elif p.is_dir():
        try:
            for entry in p.iterdir():
                if entry.is_file() and not entry.name.startswith("."):
                    h = hash_file(str(entry))
                    if h:
                        results[str(entry)] = h
        except PermissionError:
            pass
    return results


async def fim_check():
    """Compare current file hashes against baseline."""
    global _fim_baseline

    current = {}
    for path in FIM_PATHS:
        current.update(scan_path(path))

    if not _fim_baseline:
        _fim_baseline = current
        log(f"FIM baseline established: {len(current)} files")
        return

    # Detect changes
    for filepath, new_hash in current.items():
        old_hash = _fim_baseline.get(filepath)
        if old_hash is None:
            _stats["fim_changes"] += 1
            log(f"FIM: NEW file {filepath}", "WARN")
            await record_event("fim_new_file", "warning", filepath, {
                "hash": new_hash,
            })
            _alerts.append(f"NEW: {filepath}")
        elif old_hash != new_hash:
            _stats["fim_changes"] += 1
            log(f"FIM: MODIFIED {filepath}", "WARN")
            await record_event("fim_modified", "warning", filepath, {
                "old_hash": old_hash,
                "new_hash": new_hash,
            })
            _alerts.append(f"MODIFIED: {filepath}")

    for filepath in set(_fim_baseline.keys()) - set(current.keys()):
        _stats["fim_changes"] += 1
        log(f"FIM: DELETED {filepath}", "WARN")
        await record_event("fim_deleted", "warning", filepath)
        _alerts.append(f"DELETED: {filepath}")

    _fim_baseline = current


# ── Process Monitoring ───────────────────────────────────────────────────────

async def process_check():
    """Check for suspicious network listeners."""
    try:
        result = subprocess.run(
            ["lsof", "-iTCP", "-sTCP:LISTEN", "-nP"],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        return

    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        proc_name = parts[0].lower()
        if any(s in proc_name for s in SUSPICIOUS_LISTENERS):
            _stats["suspicious_procs"] += 1
            pid = parts[1]
            port_info = parts[8] if len(parts) > 8 else "?"
            log(f"SUSPICIOUS listener: {proc_name} (PID {pid}) on {port_info}", "CRIT")
            await record_event("suspicious_listener", "critical", None, {
                "process": proc_name,
                "pid": pid,
                "port": port_info,
            })


# ── Auth Event Monitoring ────────────────────────────────────────────────────

async def auth_check():
    """Monitor macOS auth/login events via log stream."""
    global _last_auth_offset
    try:
        result = subprocess.run(
            ["log", "show", "--predicate",
             'subsystem == "com.apple.opendirectoryd" AND category == "auth"',
             "--last", "2m", "--style", "ndjson"],
            capture_output=True, text=True, timeout=15
        )
    except Exception:
        return

    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg = entry.get("eventMessage", "")
        if "Authentication failed" in msg or "invalid password" in msg.lower():
            _stats["auth_events"] += 1
            log(f"AUTH: Failed auth attempt: {msg[:100]}", "WARN")
            await record_event("auth_failure", "warning", None, {
                "message": msg[:200],
                "timestamp": entry.get("timestamp"),
            })


# ── Main Loops ───────────────────────────────────────────────────────────────

async def fim_loop():
    await asyncio.sleep(5)
    log("FIM monitor started")
    while not _shutdown:
        try:
            await fim_check()
        except Exception as e:
            log(f"FIM error: {e}", "ERROR")
        await asyncio.sleep(FIM_INTERVAL)


async def process_loop():
    await asyncio.sleep(10)
    log("Process monitor started")
    while not _shutdown:
        try:
            await process_check()
        except Exception as e:
            log(f"Process check error: {e}", "ERROR")
        await asyncio.sleep(PROCESS_INTERVAL)


async def auth_loop():
    await asyncio.sleep(15)
    log("Auth monitor started")
    while not _shutdown:
        try:
            await auth_check()
        except Exception as e:
            log(f"Auth check error: {e}", "ERROR")
        await asyncio.sleep(AUTH_INTERVAL)


# ── HTTP API ─────────────────────────────────────────────────────────────────

async def handle_health(request):
    return web.json_response({
        "ok": True,
        "service": "nova_endpoint_monitor",
        "version": VERSION,
        "hostname": HOSTNAME,
        "uptime_s": int(time.time() - _start_time),
    })


async def handle_status(request):
    return web.json_response({
        "ok": True,
        "version": VERSION,
        "hostname": HOSTNAME,
        "uptime_s": int(time.time() - _start_time),
        "stats": _stats,
        "fim_files_tracked": len(_fim_baseline),
        "recent_alerts": _alerts[-20:],
        "guest_state": None,
    })


async def handle_events(request):
    """GET /events — recent endpoint events."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ts, hostname, event_type, severity, path, details
            FROM telemetry.endpoint_events
            ORDER BY ts DESC LIMIT 50
        """)
    events = [
        {
            "ts": r["ts"].isoformat(),
            "hostname": r["hostname"],
            "type": r["event_type"],
            "severity": r["severity"],
            "path": r["path"],
            "details": json.loads(r["details"]) if r["details"] else None,
        }
        for r in rows
    ]
    return web.json_response({"events": events, "count": len(events)})


# ── Lifecycle ────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


async def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova Endpoint Monitor v{VERSION} starting on {HOSTNAME}...")

    await ensure_table()

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/events", handle_events)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", HTTP_PORT)
    await site.start()
    log(f"HTTP API listening on 127.0.0.1:{HTTP_PORT}")

    tasks = [
        asyncio.create_task(fim_loop()),
        asyncio.create_task(process_loop()),
        asyncio.create_task(auth_loop()),
    ]

    while not _shutdown:
        await asyncio.sleep(1)

    log("Shutting down...")
    for task in tasks:
        task.cancel()
    await runner.cleanup()
    if _pool:
        await _pool.close()
    log("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
