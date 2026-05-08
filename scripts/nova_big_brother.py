#!/usr/bin/env python3
"""
nova_big_brother.py — Big Brother: the self-healing enforcer for all Nova systems.

Persistent daemon (NOT a cron job) using macOS kqueue to watch log files in
real time. Detects failures within seconds and heals before Jordan notices.

Replaces:
  nova_watchdog.py        (service monitoring + subagent heartbeats)
  nova_gateway_health.py  (gateway/channel health + workspace management)

Scope of responsibility:
  ─ Core infrastructure: PostgreSQL, Redis, Ollama, Memory Server, Gateway,
    Scheduler, MLX Server, TinyChat, OpenWebUI, Signal-cli
  ─ Subagents: Sentinel, Lookout, Analyst, Librarian, Coder
  ─ Channels: Slack socket mode, Discord WebSocket, Signal
  ─ Log error detection: gateway, scheduler, memory server, nova.jsonl
  ─ Gateway workspace EPERM auto-kickstart
  ─ Gateway auth-profiles.json drift (wrong format)
  ─ signal-cli lock conflicts
  ─ PostgreSQL idle connection cleanup
  ─ Disk space warnings on /Volumes/Data + /Volumes/MoreData
  ─ Image generation (SwarmUI port 7801)
  ─ Slack preprocessor TCC token injection

Safe-restart policy:
  If a PROTECTED long-running task is detected via Scheduler API, Big Brother
  queues the restart and fires it only when the task finishes.

Notification paths (no dependency on gateway being alive):
  Primary: nova_config.post_both() — Slack HTTP + Discord HTTP
  Fallback (gateway dead): raw Slack HTTP + signal-cli direct

Diagnostics API (consumed by NovaControl Diagnostics tab):
  GET  http://127.0.0.1:37461/bb/status        — daemon health + summary
  GET  http://127.0.0.1:37461/bb/events?n=100  — recent heal events
  GET  http://127.0.0.1:37461/bb/services      — per-service status
  POST http://127.0.0.1:37461/bb/force-check   — manual full check now

Written by Jordan Koch.
"""

import fcntl
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN, LOG_DEBUG

# ── Constants ─────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
PID_FILE = Path.home() / ".openclaw/run/big-brother.pid"
STATE_FILE = Path.home() / ".openclaw/run/big-brother-state.json"
LOG_DIR = Path.home() / ".openclaw/logs"
SCRIPTS = Path.home() / ".openclaw/scripts"
WORKSPACE = Path.home() / ".openclaw/workspace"
API_PORT = 37461

# How often to run a full health sweep (seconds)
SWEEP_INTERVAL = 60

# Quiet hours — only alert on NEW issues (not repeats) between 10pm and 8am
QUIET_START = 22
QUIET_END = 8

# Disk space minimum in GB — warn below this
DISK_WARN_GB = 10.0

# Services to monitor
SERVICES = [
    # name, host, port, launchd_label, is_critical, health_url_path
    ("PostgreSQL",    "127.0.0.1", 5432,  "homebrew.mxcl.postgresql@17",         True,  None),
    ("Redis",         "127.0.0.1", 6379,  "net.digitalnoise.redis",               True,  None),
    ("Ollama",        "127.0.0.1", 11434, None,                                   True,  "/api/tags"),
    ("Memory Server", "127.0.0.1", 18790, "net.digitalnoise.nova-memory-server",  True,  "/health"),
    ("Gateway",       "127.0.0.1", 18789, "ai.openclaw.gateway",                  True,  "/health"),
    ("Scheduler",     "127.0.0.1", 37460, "com.nova.scheduler",                   True,  "/status"),
    ("MLX Server",    "127.0.0.1", 5050,  "net.digitalnoise.mlx-server",          False, "/health"),
    ("SwarmUI",       "127.0.0.1", 7801,  None,                                   False, None),
    ("TinyChat",      "127.0.0.1", 8000,  "net.digitalnoise.tinychat",            False, None),
    ("OpenWebUI",     "127.0.0.1", 3000,  "net.digitalnoise.openwebui",           False, None),
    ("Signal-cli",    "127.0.0.1", 8080,  None,                                   False, None),
    ("NovaControl",   "127.0.0.1", 37400, "net.digitalnoise.NovaControl",         False, "/api/status"),
]

SUBAGENTS = ["sentinel", "lookout", "analyst", "librarian", "coder"]

# Tasks that must not be interrupted mid-run
PROTECTED_TASK_PATTERNS = [
    "ingest", "reindex", "maintain", "pg_maintain", "pg_backup",
    "nova_reembed", "bulk", "hnsw",
]

# ── State ─────────────────────────────────────────────────────────────────────

_heal_events: deque = deque(maxlen=500)   # (ts, severity, issue, fix, service)
_service_status: dict = {}                # service_name -> {up, last_seen, restarts, last_error}
_pending_restart: list = []               # service names waiting for protected task to finish
_alerted_issues: set = set()             # issues seen in quiet hours (suppress repeats)
_start_time = time.time()
_lock = threading.Lock()
_shutdown = threading.Event()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_quiet_hours() -> bool:
    h = datetime.now().hour
    if QUIET_START > QUIET_END:
        return h >= QUIET_START or h < QUIET_END
    return QUIET_START <= h < QUIET_END


def _record_event(severity: str, issue: str, fix: str, service: str = ""):
    event = {
        "ts": _now_iso(),
        "severity": severity,  # critical / warning / info
        "issue": issue,
        "fix": fix,
        "service": service,
    }
    with _lock:
        _heal_events.appendleft(event)
    log(f"[{severity}] {issue} → {fix}", level=LOG_WARN if severity != "info" else LOG_INFO,
        source="big-brother", extra={"service": service})
    _save_state()


def _save_state():
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "pid": os.getpid(),
            "started": _now_iso(),
            "uptime_s": int(time.time() - _start_time),
            "events_total": len(_heal_events),
            "service_status": dict(_service_status),
        }
        STATE_FILE.write_text(json.dumps(state, default=str))
    except Exception:
        pass


# ── Notification ─────────────────────────────────────────────────────────────

def _notify(message: str, is_critical: bool = False):
    """Post to all channels. Falls back to raw HTTP + signal-cli if gateway is dead."""
    # Always try nova_config first (Slack HTTP + Discord HTTP — no gateway dep)
    try:
        nova_config.post_both(message, slack_channel=nova_config.SLACK_NOTIFY)
        return
    except Exception as e:
        log(f"Primary notify failed: {e}", level=LOG_WARN, source="big-brother")

    # Fallback: raw Slack HTTP
    token = nova_config.slack_bot_token()
    if token:
        try:
            data = json.dumps({
                "channel": nova_config.SLACK_NOTIFY,
                "text": message,
            }).encode()
            req = urllib.request.Request(
                f"{nova_config.SLACK_API}/chat.postMessage",
                data=data,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"Slack fallback failed: {e}", level=LOG_ERROR, source="big-brother")

    # Fallback: signal-cli direct
    if is_critical:
        try:
            subprocess.run(
                ["/opt/homebrew/bin/signal-cli", "--account", nova_config.NOVA_SIGNAL,
                 "send", "-m", message[:1000], "-r", nova_config.JORDAN_SIGNAL],
                capture_output=True, timeout=15,
            )
        except Exception as e:
            log(f"Signal fallback failed: {e}", level=LOG_ERROR, source="big-brother")


def _maybe_notify(issue_key: str, message: str, is_critical: bool = False):
    """Suppress duplicate alerts during quiet hours. Always alert on first occurrence."""
    with _lock:
        is_new = issue_key not in _alerted_issues
        if is_new:
            _alerted_issues.add(issue_key)

    if is_new or not _is_quiet_hours():
        _notify(message, is_critical=is_critical)


# ── Protected Task Check ──────────────────────────────────────────────────────

def _is_protected_task_running() -> bool:
    """Check Scheduler API for a currently-running protected task."""
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:37460/status", timeout=5)
        data = json.loads(resp.read())
        if data.get("tasks_running", 0) == 0:
            return False
        # Get running task names if possible
        try:
            tresp = urllib.request.urlopen("http://127.0.0.1:37460/tasks", timeout=5)
            tasks = json.loads(tresp.read())
            task_list = tasks if isinstance(tasks, list) else tasks.get("tasks", [])
            for t in task_list:
                if t.get("status") == "running":
                    name = t.get("name", "").lower()
                    if any(p in name for p in PROTECTED_TASK_PATTERNS):
                        log(f"Protected task running: {t.get('name')}", level=LOG_INFO,
                            source="big-brother")
                        return True
        except Exception:
            # Can't get task names — if any task is running, be conservative
            return True
    except Exception:
        pass
    return False


def _queue_restart(service_name: str):
    with _lock:
        if service_name not in _pending_restart:
            _pending_restart.append(service_name)
    log(f"Queued restart of {service_name} (waiting for protected task)",
        level=LOG_INFO, source="big-brother")


def _flush_pending_restarts():
    with _lock:
        pending = list(_pending_restart)
        _pending_restart.clear()
    for svc in pending:
        log(f"Firing queued restart: {svc}", level=LOG_INFO, source="big-brother")
        _do_restart(svc)


# ── Service Restart Logic ─────────────────────────────────────────────────────

def _do_restart(service_name: str) -> bool:
    """Restart a service. Returns True on success."""
    entry = next((s for s in SERVICES if s[0] == service_name), None)
    if not entry:
        return False

    name, host, port, label, critical, health_path = entry

    # Gateway needs special handling due to macOS Tahoe launchd bug
    if name == "Gateway":
        return _restart_gateway()

    # Signal-cli is managed by the gateway
    if name == "Signal-cli":
        subprocess.run(["pkill", "-f", "signal-cli"], capture_output=True)
        time.sleep(2)
        return _restart_gateway()

    if label:
        uid = os.getuid()
        try:
            result = subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0:
                time.sleep(8)
                return _port_open(host, port)
        except Exception:
            pass
        # Fallback: stop then start
        try:
            subprocess.run(["launchctl", "stop", label], capture_output=True, timeout=5)
            time.sleep(3)
            subprocess.run(["launchctl", "start", label], capture_output=True, timeout=5)
            time.sleep(8)
            return _port_open(host, port)
        except Exception:
            return False

    return False


def _restart_gateway() -> bool:
    """Kill gateway + signal-cli, restart via nova_gateway_start.sh."""
    subprocess.run(["pkill", "-9", "-f", "^openclaw$"], capture_output=True)
    subprocess.run(["pkill", "-f", "signal-cli"], capture_output=True)
    time.sleep(3)
    start_script = SCRIPTS / "nova_gateway_start.sh"
    gw_log = LOG_DIR / "gateway.log"
    gw_err = LOG_DIR / "gateway.err.log"
    try:
        proc = subprocess.Popen(
            ["/bin/zsh", str(start_script)],
            stdout=open(str(gw_log), "a"),
            stderr=open(str(gw_err), "a"),
            start_new_session=True,
        )
        log(f"Gateway restart initiated (PID {proc.pid})", level=LOG_INFO, source="big-brother")
    except Exception as e:
        log(f"Gateway restart failed: {e}", level=LOG_ERROR, source="big-brother")
        return False
    # Wait up to 30s for gateway to come up
    for _ in range(30):
        time.sleep(1)
        if _port_open("127.0.0.1", 18789):
            return True
    return False


# ── Port / Health Checks ──────────────────────────────────────────────────────

def _port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex((host, port))
        s.close()
        return result == 0
    except Exception:
        return False


def _http_healthy(host: str, port: int, path: str, timeout: float = 5.0) -> bool:
    try:
        url = f"http://{host}:{port}{path}"
        resp = urllib.request.urlopen(url, timeout=timeout)
        return resp.status in (200, 204)
    except Exception:
        return False


def _service_is_up(name: str, host: str, port: int, health_path) -> bool:
    if health_path:
        return _http_healthy(host, port, health_path)
    return _port_open(host, port)


# ── Slack Socket Mode Check ───────────────────────────────────────────────────

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

def _check_gateway_log_channels() -> dict:
    """Parse recent gateway log for channel state. Returns {slack, discord, signal}."""
    status = {"slack": "unknown", "discord": "unknown", "signal": "unknown"}

    log_file = LOG_DIR / "gateway.log"
    if not log_file.exists():
        return status

    try:
        lines = log_file.read_text(errors="replace").split("\n")[-200:]
    except Exception:
        return status

    for line in lines:
        clean = _ANSI_RE.sub("", line).lower()
        if "slack" in clean:
            if "socket mode connected" in clean:
                status["slack"] = "connected"
            elif "socket disconnected" in clean or "socket mode disconnected" in clean:
                status["slack"] = "disconnected"
        if "discord" in clean:
            if "channels resolved" in clean or "discord ready" in clean or "discord client initialized" in clean:
                status["discord"] = "connected"
            elif "gateway websocket closed" in clean or "enotfound" in clean:
                status["discord"] = "disconnected"
        if "signal" in clean:
            if "started http server" in clean or "config file lock acquired" in clean:
                status["signal"] = "connected"
            elif "connection closed unexpectedly" in clean or "config file is in use" in clean:
                status["signal"] = "disconnected"

    return status


# ── Gateway EPERM Check ───────────────────────────────────────────────────────

def _check_gateway_eperm() -> bool:
    """Check for workspace-state.json EPERM in gateway runtime log (last 3 min)."""
    runtime_log = Path(f"/tmp/openclaw/openclaw-{datetime.now().strftime('%Y-%m-%d')}.log")
    if not runtime_log.exists():
        return False

    cutoff = time.time() - 180
    try:
        with open(runtime_log, errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    msg = str(d.get("0", "")) + str(d.get("1", ""))
                    ts_str = d.get("_meta", {}).get("date", "")
                    if "workspace-state.json" in msg and "EPERM" in msg and ts_str:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if dt.timestamp() >= cutoff:
                            return True
                except Exception:
                    pass
    except Exception:
        pass
    return False


# ── auth-profiles.json drift ──────────────────────────────────────────────────

def _check_auth_profiles() -> bool:
    """Return True if any agent auth-profiles.json is in the wrong format."""
    agents_dir = Path.home() / ".openclaw/agents"
    if not agents_dir.exists():
        return False
    for profile in agents_dir.glob("*/agent/auth-profiles.json"):
        try:
            data = json.loads(profile.read_text())
            # Good format: {"version": ..., "profiles": [...]}
            # Bad format:  {"openrouter": {"apiKey": "..."}}
            if "profiles" not in data:
                return True
            for p in data.get("profiles", []):
                for v in p.get("credentials", {}).values():
                    if isinstance(v, str) and v.startswith("${"):
                        return True  # Unexpanded env var
        except Exception:
            pass
    return False


def _fix_auth_profiles():
    """Run openclaw doctor --fix with Keychain secrets loaded."""
    env = _load_keychain_env()
    try:
        subprocess.run(
            ["openclaw", "doctor", "--fix"],
            env={**os.environ, **env},
            capture_output=True, timeout=60,
        )
    except Exception as e:
        log(f"doctor --fix failed: {e}", level=LOG_ERROR, source="big-brother")


def _load_keychain_env() -> dict:
    secrets = {
        "NOVA_OPENROUTER_API_KEY": "nova-openrouter-api-key",
        "NOVA_SLACK_BOT_TOKEN": "nova-slack-bot-token",
        "NOVA_SLACK_APP_TOKEN": "nova-slack-app-token",
        "NOVA_GATEWAY_AUTH_TOKEN": "nova-gateway-auth-token",
        "NOVA_DISCORD_TOKEN": "nova-discord-token",
    }
    env = {}
    for var, svc in secrets.items():
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova", "-s", svc, "-w"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            env[var] = result.stdout.strip()
    return env


# ── Subagent Heartbeats ───────────────────────────────────────────────────────

def _check_subagent_heartbeats() -> list:
    """Returns list of stale agent names."""
    try:
        import redis
        r = redis.from_url("redis://localhost:6379", decode_responses=True)
        stale = []
        for name in SUBAGENTS:
            status = r.get(f"nova:agent:{name}:status")
            if status != "running":
                stale.append(name)
        return stale
    except Exception:
        return []


def _restart_subagent(name: str):
    try:
        subprocess.run(
            ["/bin/zsh", str(SCRIPTS / "nova_subagent_ctl.sh"), "restart", name],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        log(f"Failed to restart subagent {name}: {e}", level=LOG_ERROR, source="big-brother")


# ── Slack Preprocessor TCC Fix ────────────────────────────────────────────────

def _fix_slack_preprocessor_tcc():
    """Inject NOVA_SLACK_BOT_TOKEN into the slack preprocessor plist if missing."""
    import plistlib
    plist_path = Path.home() / "Library/LaunchAgents/com.nova.slack-preprocessor.plist"
    if not plist_path.exists():
        return False

    token = nova_config.slack_bot_token()
    if not token:
        return False

    try:
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)

        env = plist.setdefault("EnvironmentVariables", {})
        current = env.get("NOVA_SLACK_BOT_TOKEN", "")
        if current == token:
            return False  # Already correct

        env["NOVA_SLACK_BOT_TOKEN"] = token
        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)

        uid = os.getuid()
        label = "com.nova.slack-preprocessor"
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, timeout=5)
        time.sleep(1)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], capture_output=True, timeout=5)
        subprocess.run(["launchctl", "start", label], capture_output=True, timeout=5)
        return True
    except Exception as e:
        log(f"TCC fix failed: {e}", level=LOG_ERROR, source="big-brother")
        return False


# ── PostgreSQL Maintenance ────────────────────────────────────────────────────

def _cleanup_postgres_idle():
    """Kill PG connections idle for >2h."""
    try:
        result = subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_memories", "-tAc",
             "SELECT count(*) FROM pg_stat_activity WHERE state='idle' "
             "AND query_start < NOW() - INTERVAL '2 hours' AND pid != pg_backend_pid();"],
            capture_output=True, text=True, timeout=10,
        )
        idle = int(result.stdout.strip() or "0")
        if idle > 3:
            subprocess.run(
                ["psql", "-U", "kochj", "-d", "nova_memories", "-c",
                 "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                 "WHERE state='idle' AND query_start < NOW() - INTERVAL '2 hours' "
                 "AND pid != pg_backend_pid();"],
                capture_output=True, timeout=10,
            )
            log(f"Cleaned {idle} idle PG connections", level=LOG_INFO, source="big-brother")
    except Exception:
        pass


# ── Disk Space Check ──────────────────────────────────────────────────────────

def _check_disk_space() -> list:
    """Returns list of warning strings for low-space volumes."""
    warnings = []
    volumes = [
        ("/Volumes/Data", "Data volume (AI models)"),
        ("/Volumes/MoreData", "MoreData volume (PostgreSQL)"),
        (str(Path.home()), "Main SSD (~/)"),
    ]
    for path, label in volumes:
        try:
            stat = os.statvfs(path)
            free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
            if free_gb < DISK_WARN_GB:
                warnings.append(f"{label}: only {free_gb:.1f}GB free")
        except Exception:
            pass
    return warnings


# ── Nova Memory / Redis Health ────────────────────────────────────────────────

def _check_memory_server_recall() -> bool:
    """Quick recall test — ensures memory server can actually query Postgres."""
    try:
        url = "http://127.0.0.1:18790/recall?q=test&n=1"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        return isinstance(data, list)  # Should return array (possibly empty)
    except Exception:
        return False


def _check_redis_memory_cache() -> bool:
    """Ensure Redis is actually storing/retrieving keys (not just pinging)."""
    try:
        import redis
        r = redis.from_url("redis://localhost:6379")
        test_key = "big-brother:health-check"
        r.set(test_key, "ok", ex=10)
        val = r.get(test_key)
        return val == b"ok"
    except Exception:
        return False


# ── Scheduler Heartbeat Check ─────────────────────────────────────────────────

def _check_scheduler_heartbeat() -> bool:
    """Return False if scheduler heartbeat file is stale >10 min."""
    hb = Path.home() / ".openclaw/config/scheduler_heartbeat"
    if not hb.exists():
        return True  # Not started yet — not a failure
    try:
        age = time.time() - float(hb.read_text().strip())
        return age < 600
    except Exception:
        return True


# ── Log Error Scanner ─────────────────────────────────────────────────────────

_SEEN_ERRORS: dict = {}  # log_file -> last_position

ERROR_PATTERNS = [
    # Pattern, severity, service, friendly description
    (r"EPERM.*workspace-state\.json", "critical", "Gateway", "EPERM on workspace-state.json"),
    (r"Startup failed.*required secrets", "critical", "Gateway", "Gateway secrets unavailable at startup"),
    (r"Config file is in use by another instance", "warning", "Signal-cli", "signal-cli lock conflict"),
    (r"Unrecognized keys.*bootstrapMaxChars", "critical", "Gateway", "openclaw.json invalid config keys"),
    (r"FailoverError.*No API key found for provider", "critical", "Gateway", "OpenRouter API key missing"),
    (r"FATAL.*nova_memories", "critical", "PostgreSQL", "PostgreSQL fatal error on nova_memories"),
    (r"redis\.exceptions\.(ConnectionError|TimeoutError)", "critical", "Redis", "Redis connection error"),
    (r"HNSW index.*not found", "warning", "Memory Server", "HNSW index missing — recall degraded"),
    (r"pg_dump.*error", "warning", "PostgreSQL", "pg_dump backup error"),
    (r"CRITICAL.*nova_watchdog\|CRITICAL.*scheduler", "critical", "Scheduler", "Scheduler critical error"),
    (r"OOM|out of memory|cannot allocate", "critical", "System", "Out of memory condition"),
]

_COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), sev, svc, desc)
                      for p, sev, svc, desc in ERROR_PATTERNS]

LOG_FILES_TO_WATCH = [
    LOG_DIR / "gateway.err.log",
    LOG_DIR / "gateway.log",
    LOG_DIR / "scheduler.log",
    LOG_DIR / "memory-server-error.log",
    LOG_DIR / "nova.jsonl",
    LOG_DIR / "daily-journal.log",
]


def _scan_log_file(log_file: Path) -> list:
    """Scan new lines of a log file for error patterns. Returns list of (sev, svc, desc, line)."""
    found = []
    key = str(log_file)
    pos = _SEEN_ERRORS.get(key, 0)

    if not log_file.exists():
        return found

    try:
        size = log_file.stat().st_size
        if size < pos:
            pos = 0  # File was rotated

        with open(log_file, errors="replace") as f:
            f.seek(pos)
            new_content = f.read()
            _SEEN_ERRORS[key] = f.tell()

        if not new_content:
            return found

        for line in new_content.splitlines():
            for pat, sev, svc, desc in _COMPILED_PATTERNS:
                if pat.search(line):
                    found.append((sev, svc, desc, line[:200]))
                    break

    except Exception:
        pass

    return found


# ── Full Health Sweep ─────────────────────────────────────────────────────────

def _full_sweep():
    """Run all checks and heal everything that can be healed."""
    issues = []
    fixes = []
    protected_running = _is_protected_task_running()

    # ── Flush pending restarts if protection lifted ──────────────────────────
    if not protected_running and _pending_restart:
        _flush_pending_restarts()

    # ── Log file scan (proactive — catches errors before service checks) ─────
    for lf in LOG_FILES_TO_WATCH:
        for sev, svc, desc, line_excerpt in _scan_log_file(lf):
            issues.append(f"{svc}: {desc}")
            log(f"Log error detected [{svc}] {desc}: {line_excerpt}", level=LOG_WARN,
                source="big-brother")
            # Auto-heal from log signals
            if desc == "EPERM on workspace-state.json":
                uid = os.getuid()
                subprocess.run(
                    ["launchctl", "kickstart", "-k", f"gui/{uid}/ai.openclaw.gateway"],
                    capture_output=True, timeout=15,
                )
                fixes.append("Kickstarted gateway (EPERM)")
                _record_event("critical", "Gateway EPERM workspace-state.json",
                              "Kickstarted gateway", "Gateway")

            elif desc == "signal-cli lock conflict":
                subprocess.run(["pkill", "-f", "signal-cli"], capture_output=True)
                time.sleep(2)
                fixes.append("Killed stale signal-cli (lock conflict)")
                _record_event("warning", "signal-cli lock conflict", "Killed stale signal-cli", "Signal-cli")

            elif desc == "openclaw.json invalid config keys":
                _fix_auth_profiles()
                fixes.append("Ran openclaw doctor --fix (bad config keys)")
                _record_event("critical", "openclaw.json invalid keys", "openclaw doctor --fix", "Gateway")

            elif desc == "OpenRouter API key missing":
                _fix_auth_profiles()
                fixes.append("Ran openclaw doctor --fix (missing API key)")
                _record_event("critical", "OpenRouter API key missing", "openclaw doctor --fix", "Gateway")

    # ── Service port checks ──────────────────────────────────────────────────
    for name, host, port, label, critical, health_path in SERVICES:
        up = _service_is_up(name, host, port, health_path)
        with _lock:
            prev = _service_status.get(name, {}).get("up", True)
            _service_status[name] = {
                "up": up,
                "last_seen": _now_iso() if up else _service_status.get(name, {}).get("last_seen"),
                "restarts": _service_status.get(name, {}).get("restarts", 0),
                "last_error": None if up else f"Not responding on :{port}",
            }

        if not up:
            issues.append(f"{name} (:{port}) DOWN")
            if not critical and name not in ("SwarmUI", "TinyChat"):
                _record_event("warning", f"{name} not responding on :{port}", "No action (non-critical)", name)
                continue

            if protected_running and name not in ("Gateway", "Signal-cli"):
                _queue_restart(name)
                fixes.append(f"Queued restart of {name} (protected task running)")
                _record_event("warning", f"{name} DOWN", "Queued restart", name)
                continue

            if name == "PostgreSQL":
                subprocess.run(["pg_isready"], capture_output=True, timeout=5)
                if label:
                    uid = os.getuid()
                    subprocess.run(
                        ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                        capture_output=True, timeout=15,
                    )
                fixes.append("Restarted PostgreSQL")
                _record_event("critical", "PostgreSQL DOWN", "Restarted via launchctl", "PostgreSQL")

            elif name == "Redis":
                if label:
                    uid = os.getuid()
                    subprocess.run(
                        ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                        capture_output=True, timeout=15,
                    )
                fixes.append("Restarted Redis")
                _record_event("critical", "Redis DOWN", "Restarted via launchctl", "Redis")

            elif name == "Memory Server" and label:
                uid = os.getuid()
                subprocess.run(
                    ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                    capture_output=True, timeout=15,
                )
                fixes.append("Restarted Memory Server")
                _record_event("critical", "Memory Server DOWN", "Restarted via launchctl", "Memory Server")

            elif name == "Scheduler":
                if not _check_scheduler_heartbeat():
                    uid = os.getuid()
                    subprocess.run(
                        ["launchctl", "kickstart", "-k", f"gui/{uid}/com.nova.scheduler"],
                        capture_output=True, timeout=15,
                    )
                    fixes.append("Restarted Scheduler (stale heartbeat)")
                    _record_event("critical", "Scheduler stale heartbeat", "Kickstarted via launchctl", "Scheduler")

            elif name == "Gateway" or name == "Signal-cli":
                success = _restart_gateway()
                fix_msg = "Restarted Gateway" if success else "FAILED to restart Gateway"
                fixes.append(fix_msg)
                _record_event("critical", f"{name} DOWN",
                              fix_msg, "Gateway")

            elif label:
                uid = os.getuid()
                subprocess.run(
                    ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                    capture_output=True, timeout=15,
                )
                fixes.append(f"Restarted {name}")
                _record_event("warning", f"{name} DOWN", f"Restarted {name}", name)

        else:
            with _lock:
                issue_key = f"{name}_down"
                if issue_key in _alerted_issues:
                    _alerted_issues.discard(issue_key)

    # ── Channel health (only if gateway is up) ───────────────────────────────
    gateway_up = _port_open("127.0.0.1", 18789)
    if gateway_up:
        channels = _check_gateway_log_channels()
        disconnected = [ch for ch, st in channels.items() if st == "disconnected"]
        if disconnected:
            issues.append(f"Channels disconnected: {', '.join(disconnected)}")
            _record_event("critical",
                          f"Channels disconnected: {', '.join(disconnected)}",
                          "Restarting gateway",
                          "Gateway")
            if not protected_running:
                success = _restart_gateway()
                if success:
                    fixes.append(f"Restarted gateway (channels: {', '.join(disconnected)})")
                else:
                    fixes.append("FAILED to restart gateway for channel reconnect")

        # EPERM check
        if _check_gateway_eperm():
            uid = os.getuid()
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/ai.openclaw.gateway"],
                capture_output=True, timeout=15,
            )
            fixes.append("Kickstarted gateway (EPERM workspace-state.json)")
            _record_event("critical", "Gateway EPERM", "Kickstarted gateway", "Gateway")

    # ── auth-profiles.json drift ─────────────────────────────────────────────
    if _check_auth_profiles():
        issues.append("auth-profiles.json wrong format")
        _fix_auth_profiles()
        fixes.append("Ran openclaw doctor --fix (auth-profiles format)")
        _record_event("critical", "auth-profiles.json drift", "openclaw doctor --fix", "Gateway")

    # ── Slack preprocessor TCC ───────────────────────────────────────────────
    if _fix_slack_preprocessor_tcc():
        fixes.append("Injected Slack token into preprocessor plist (TCC fix)")
        _record_event("warning", "Slack preprocessor missing token",
                      "Injected token into plist + reloaded", "Slack")

    # ── Memory server functional check ───────────────────────────────────────
    mem_up = _port_open("127.0.0.1", 18790)
    if mem_up and not _check_memory_server_recall():
        issues.append("Memory server port up but recall failing (PG/Redis likely unhealthy)")
        _record_event("warning", "Memory recall failing despite server up",
                      "Check PostgreSQL + Redis", "Memory Server")

    if mem_up and not _check_redis_memory_cache():
        issues.append("Redis cache not storing/retrieving keys")
        _record_event("warning", "Redis functional check failed",
                      "Check Redis config + net.digitalnoise.redis", "Redis")

    # ── Subagent heartbeats ───────────────────────────────────────────────────
    stale = _check_subagent_heartbeats()
    for agent in stale:
        issues.append(f"Subagent {agent} stale/missing")
        _restart_subagent(agent)
        fixes.append(f"Restarted subagent {agent}")
        _record_event("warning", f"Subagent {agent} stale", f"Restarted via subagent_ctl.sh", agent)

    # ── PostgreSQL idle cleanup ───────────────────────────────────────────────
    if _port_open("127.0.0.1", 5432):
        _cleanup_postgres_idle()

    # ── Disk space ───────────────────────────────────────────────────────────
    disk_warnings = _check_disk_space()
    for dw in disk_warnings:
        issues.append(f"Low disk: {dw}")
        _record_event("warning", f"Low disk space: {dw}", "No auto-fix — manual cleanup needed", "System")

    # ── Notify ───────────────────────────────────────────────────────────────
    if issues:
        is_critical = any(
            sev == "critical"
            for ev in list(_heal_events)[:10]
            if ev.get("ts", "") > _now_iso()[:13]  # within this hour
            for sev in [ev.get("severity", "")]
        )

        msg = ":robot_face: *Big Brother Report*\n"
        msg += "\n".join(f"  :red_circle: {i}" for i in issues)
        if fixes:
            msg += "\n*Healed:*\n"
            msg += "\n".join(f"  :white_check_mark: {f}" for f in fixes)

        issue_key = "::".join(sorted(issues))
        _maybe_notify(issue_key, msg, is_critical=any("DOWN" in i for i in issues))
        log(f"Sweep: {len(issues)} issues, {len(fixes)} fixes", level=LOG_WARN, source="big-brother")
    else:
        with _lock:
            _alerted_issues.clear()
        log("All systems healthy", level=LOG_INFO, source="big-brother")

    _save_state()


# ── kqueue Log Watcher ────────────────────────────────────────────────────────

def _log_watcher_thread():
    """
    Use kqueue to watch log files for new writes.
    On any write event, run a targeted check (not full sweep).
    """
    try:
        import select
        kq = select.kqueue()
    except Exception:
        log("kqueue unavailable — skipping real-time log watching", level=LOG_WARN,
            source="big-brother")
        return

    watched_fds = {}
    for lf in LOG_FILES_TO_WATCH:
        try:
            lf.parent.mkdir(parents=True, exist_ok=True)
            lf.touch(exist_ok=True)
            fd = os.open(str(lf), os.O_RDONLY | os.O_NONBLOCK)
            watched_fds[fd] = lf
        except Exception:
            pass

    if not watched_fds:
        return

    kevents = [
        select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                      flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                      fflags=select.NOTE_WRITE | select.NOTE_EXTEND)
        for fd in watched_fds
    ]

    last_scan = 0
    SCAN_DEBOUNCE = 5  # Don't scan more than once per 5s from log events

    while not _shutdown.is_set():
        try:
            events = kq.control(kevents, 32, timeout=2)
            if events and (time.time() - last_scan) > SCAN_DEBOUNCE:
                last_scan = time.time()
                for lf in LOG_FILES_TO_WATCH:
                    for sev, svc, desc, line_excerpt in _scan_log_file(lf):
                        log(f"Realtime log event [{svc}] {desc}", level=LOG_WARN,
                            source="big-brother")
                        # For critical errors, trigger full sweep immediately
                        if sev == "critical":
                            threading.Thread(target=_full_sweep, daemon=True).start()
                            break
        except Exception:
            time.sleep(1)

    for fd in watched_fds:
        try:
            os.close(fd)
        except Exception:
            pass


# ── HTTP Diagnostics API ──────────────────────────────────────────────────────

class BBHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Suppress HTTPServer access logs

    def do_GET(self):
        if self.path == "/bb/status":
            self._json({
                "daemon": "big-brother",
                "version": VERSION,
                "pid": os.getpid(),
                "uptime_s": int(time.time() - _start_time),
                "events_total": len(_heal_events),
                "services_down": [n for n, s in _service_status.items() if not s.get("up", True)],
                "pending_restarts": list(_pending_restart),
                "alerted_count": len(_alerted_issues),
            })
        elif self.path.startswith("/bb/events"):
            n = 100
            if "n=" in self.path:
                try:
                    n = int(self.path.split("n=")[1].split("&")[0])
                except Exception:
                    pass
            with _lock:
                events = list(_heal_events)[:n]
            self._json(events)
        elif self.path == "/bb/services":
            with _lock:
                self._json(dict(_service_status))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/bb/force-check":
            threading.Thread(target=_full_sweep, daemon=True).start()
            self._json({"queued": True})
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _api_server_thread():
    try:
        server = HTTPServer(("127.0.0.1", API_PORT), BBHandler)
        server.timeout = 1
        log(f"Diagnostics API on 127.0.0.1:{API_PORT}", level=LOG_INFO, source="big-brother")
        while not _shutdown.is_set():
            server.handle_request()
        server.server_close()
    except Exception as e:
        log(f"API server error: {e}", level=LOG_ERROR, source="big-brother")


# ── PID file management ───────────────────────────────────────────────────────

def _write_pid():
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def _cleanup_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
        STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Signal Handlers ───────────────────────────────────────────────────────────

def _handle_sigterm(signum, frame):
    log("SIGTERM received — shutting down", level=LOG_INFO, source="big-brother")
    _shutdown.set()


def _handle_sigusr1(signum, frame):
    """Force a sweep immediately."""
    log("SIGUSR1 — forced sweep", level=LOG_INFO, source="big-brother")
    threading.Thread(target=_full_sweep, daemon=True).start()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    signal.signal(signal.SIGUSR1, _handle_sigusr1)

    _write_pid()
    log(f"Big Brother v{VERSION} starting (PID {os.getpid()})", level=LOG_INFO, source="big-brother")

    # Seed log file positions so we don't flood on startup
    for lf in LOG_FILES_TO_WATCH:
        if lf.exists():
            try:
                _SEEN_ERRORS[str(lf)] = lf.stat().st_size
            except Exception:
                pass

    # Start background threads
    threading.Thread(target=_api_server_thread, daemon=True, name="api").start()
    threading.Thread(target=_log_watcher_thread, daemon=True, name="kqueue-watcher").start()

    # Initial sweep after 10s (let services settle)
    time.sleep(10)
    _full_sweep()

    # Main loop
    last_sweep = time.time()
    while not _shutdown.is_set():
        now = time.time()
        if now - last_sweep >= SWEEP_INTERVAL:
            last_sweep = now
            threading.Thread(target=_full_sweep, daemon=True, name="sweep").start()
        time.sleep(1)

    _cleanup_pid()
    log("Big Brother stopped", level=LOG_INFO, source="big-brother")


if __name__ == "__main__":
    main()
