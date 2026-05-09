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
LAN_IP  = "192.168.1.6"   # Mac Studio LAN IP
PLEX_IP = "192.168.1.10"  # Synology NAS running Plex
NAS_IP  = "192.168.1.11"  # Synology DSM
HDHR_IP = "192.168.1.89"  # HDHomeRun TV tuner
UNIFI_IP = "192.168.1.1"  # UniFi Dream Machine

SERVICES = [
    # name, host, port, launchd_label, is_critical, health_url_path
    # ── Core (critical — Nova can't function without these) ──────────────────
    ("PostgreSQL",    "127.0.0.1", 5432,  "homebrew.mxcl.postgresql@17",         True,  None),
    ("PgBouncer",     "127.0.0.1", 6432,  "net.digitalnoise.pgbouncer",           True,  None),
    ("Redis",         "127.0.0.1", 6379,  "net.digitalnoise.redis",               True,  None),
    ("Ollama",        "127.0.0.1", 11434, None,                                   True,  "/api/version"),
    ("Memory Server", "127.0.0.1", 18790, "net.digitalnoise.nova-memory-server",  True,  "/health"),
    ("Gateway",       "127.0.0.1", 18789, "ai.openclaw.gateway",                  True,  "/health"),
    ("Scheduler",     "127.0.0.1", 37460, "com.nova.scheduler",                   True,  "/status"),
    # ── AI inference (non-critical — can recover from) ───────────────────────
    ("MLX Server",    LAN_IP,      5050,  "net.digitalnoise.mlx-server",          False, "/v1/models"),
    ("SwarmUI",       "127.0.0.1", 7801,  None,                                   False, None),
    ("ComfyUI",       "127.0.0.1", 8188,  None,                                   False, None),
    ("TinyChat",      LAN_IP,      8000,  "net.digitalnoise.tinychat",            False, None),
    ("OpenWebUI",     LAN_IP,      3000,  "net.digitalnoise.openwebui",           False, None),
    ("SearXNG",       "127.0.0.1", 8888,  "net.digitalnoise.searxng",             False, None),
    # ── Channels ─────────────────────────────────────────────────────────────
    ("Signal-cli",    "127.0.0.1", 8080,  None,                                   False, None),
    # ── Nova apps ────────────────────────────────────────────────────────────
    ("NovaControl",   "127.0.0.1", 37400, "net.digitalnoise.NovaControl",         False, "/api/status"),
    ("NovaControl Web","127.0.0.1",37450, "net.digitalnoise.nova-control-web",    False, None),
    ("Big Brother",   "127.0.0.1", 37461, None,                                   False, "/bb/status"),
    # ── External / LAN (monitored but not auto-restarted) ────────────────────
    ("Plex",          PLEX_IP,     32400, None,                                   False, "/web"),
    ("HDHomeRun",     HDHR_IP,     80,    None,                                   False, None),
]

# Services that are monitored (shown in dashboard) but never trigger alerts.
SILENCED_SERVICES = {}

# launchd services to monitor beyond the SERVICES port list.
# Format: (label, friendly_name, can_restart, silence)
# can_restart=True  → Big Brother will kickstart it on failure
# silence=True      → monitor/log but never alert Jordan
LAUNCHD_MONITORED = [
    ("com.nova.healthkit",                  "HealthKit Export",    False, True),   # needs app container, can't auto-fix
    ("com.digitalnoise.nova.general-monitor","General Monitor",    True,  False),
    ("net.digitalnoise.nova-memory-server", "Memory Server",       True,  False),  # also in SERVICES — belt+suspenders
]

# External services — just connectivity checks, no restart capability
EXTERNAL_CHECKS = [
    ("Synology NAS",  NAS_IP,   5001),
    ("UniFi",         UNIFI_IP, 443),
]

# Volume mounts that must be accessible for Nova to function
REQUIRED_MOUNTS = [
    ("/Volumes/Data",     "AI models, Xcode, Nova work"),
    ("/Volumes/MoreData", "PostgreSQL data (1.4M memories)"),
    ("/Volumes/external", "NAS media store"),
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

# Gateway restart cooldown — don't restart more than once per 5 minutes
GATEWAY_RESTART_COOLDOWN = 300  # seconds
_last_gateway_restart: float = 0.0

# Discord 3-strike before restart — timeouts ≠ disconnect
_discord_timeout_count: int = 0
DISCORD_STRIKE_THRESHOLD = 3

# Signal gap tracking — log when signal-cli goes unreachable and for how long
_signal_down_since: float = 0.0   # 0 = currently up

# Journal image repair — hourly check for posts missing cover images
JOURNAL_IMAGE_CHECK_INTERVAL = 3600  # seconds
_last_journal_image_check: float = 0.0
_journal_image_status: dict = {
    "last_run_ts": None,
    "last_run_iso": None,
    "fixed": 0,
    "failed": 0,
    "skipped_swarmui_down": False,
    "last_error": None,
}

# ── Metrics ring buffer (MRTG-style, 7 days × 1-min buckets) ─────────────────
METRICS_MAXLEN = 10080          # 7 × 24 × 60
METRICS_FILE   = Path.home() / ".openclaw/run/bb-metrics.json"
_metrics: deque = deque(maxlen=METRICS_MAXLEN)
_metrics_flush_counter: int = 0
METRICS_FLUSH_EVERY = 10        # persist to disk every N sweeps

def _load_metrics():
    """Restore ring buffer from disk on startup."""
    global _metrics
    if METRICS_FILE.exists():
        try:
            raw = json.loads(METRICS_FILE.read_text())
            if isinstance(raw, list):
                _metrics = deque(raw[-METRICS_MAXLEN:], maxlen=METRICS_MAXLEN)
        except Exception:
            pass

def _flush_metrics():
    """Persist ring buffer to disk (called periodically from _full_sweep)."""
    try:
        METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        METRICS_FILE.write_text(json.dumps(list(_metrics), default=str))
    except Exception as e:
        log(f"metrics flush failed: {e}", level=LOG_WARN, source="big-brother")

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
    """Kill gateway + signal-cli, restart via nova_gateway_start.sh.

    Enforces a 5-minute cooldown to prevent restart loops. Reaps the
    spawned child process after it execs to prevent zombies.
    """
    global _last_gateway_restart
    now = time.time()
    if now - _last_gateway_restart < GATEWAY_RESTART_COOLDOWN:
        remaining = int(GATEWAY_RESTART_COOLDOWN - (now - _last_gateway_restart))
        log(f"Gateway restart skipped — cooldown active ({remaining}s remaining)",
            level=LOG_INFO, source="big-brother")
        return True  # Don't report as failure, just throttled

    _last_gateway_restart = now

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
        # Reap the child after a short wait so it doesn't become a zombie.
        # The gateway execs into node so the shell wrapper exits quickly.
        threading.Thread(
            target=lambda p: p.wait(timeout=30),
            args=(proc,), daemon=True
        ).start()
    except Exception as e:
        log(f"Gateway restart failed: {e}", level=LOG_ERROR, source="big-brother")
        return False

    # Wait up to 45s for gateway to come up, then 30s for channels to settle
    for _ in range(45):
        time.sleep(1)
        if _port_open("127.0.0.1", 18789):
            log("Gateway port up — waiting 30s for channels to settle",
                level=LOG_INFO, source="big-brother")
            time.sleep(30)  # Let Slack/Discord/Signal connect before next channel check
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


def _http_healthy(host: str, port: int, path: str, timeout: float = 10.0) -> bool:
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
            # timeout is NOT a disconnect — skip
        if "discord" in clean:
            if "channels resolved" in clean or "discord ready" in clean or "discord client initialized" in clean:
                status["discord"] = "connected"
            elif "gateway websocket closed" in clean or "enotfound" in clean:
                status["discord"] = "disconnected"
            # fetch-timeout on discord.com is NOT a disconnect — it's a slow API
            elif "fetch timeout" in clean and "discord.com" in clean:
                status["discord"] = "timeout"  # will be handled with strike counting
        if "signal" in clean:
            if "started http server" in clean or "config file lock acquired" in clean:
                status["signal"] = "connected"
            elif "config file is in use" in clean:
                # Lock conflict — real problem, signal-cli can't start
                status["signal"] = "disconnected"
            elif "daemon exited" in clean and "code=0" in clean:
                # Clean exit (code=0) = WebSocket timeout; OpenClaw respawns automatically.
                # Don't treat as disconnected — let OpenClaw handle it.
                pass
            elif "connection closed unexpectedly" in clean:
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
    """Inject NOVA_SLACK_BOT_TOKEN into the slack preprocessor plist if missing.

    Now that the plist uses the secure wrapper script, this function only fires
    if the ProgramArguments still point to the old direct python invocation.
    """
    import plistlib
    plist_path = Path.home() / "Library/LaunchAgents/com.nova.slack-preprocessor.plist"
    if not plist_path.exists():
        return False

    try:
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)

        # Check if plist is already using the secure wrapper script
        args = plist.get("ProgramArguments", [])
        if any("nova_slack_preprocessor_start.sh" in str(a) for a in args):
            return False  # Already secure — wrapper script handles Keychain

        # Legacy plist: still pointing directly to python — needs upgrade
        token = nova_config.slack_bot_token()
        if not token:
            return False

        env = plist.setdefault("EnvironmentVariables", {})
        current = env.get("NOVA_SLACK_BOT_TOKEN", "")
        if current == token:
            return False  # Token already injected

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

def _hnsw_reindex_running() -> bool:
    """Return True if a CONCURRENT HNSW reindex is active in PostgreSQL.

    During a concurrent reindex, HNSW recall queries are slower than normal
    and may time out. This is not a bug — don't alert.
    """
    try:
        result = subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_memories", "-tAc",
             "SELECT count(*) FROM pg_stat_activity WHERE datname='nova_memories' "
             "AND query ILIKE '%REINDEX%' AND state='active';"],
            capture_output=True, text=True, timeout=5,
        )
        return int(result.stdout.strip() or "0") > 0
    except Exception:
        return False


def _check_memory_server_recall() -> bool:
    """Quick recall test — ensures memory server can actually query Postgres.

    Uses a 30s timeout and 2 retries to avoid false alarms during HNSW
    reindex or heavy PG load. Skips entirely if a REINDEX is running.
    Only returns False if all attempts fail AND no reindex is active.
    """
    # Skip recall test when HNSW reindex is running — queries are slow by design
    if _hnsw_reindex_running():
        log("HNSW reindex active — skipping recall check", level=LOG_DEBUG, source="big-brother")
        return True

    for attempt in range(3):
        try:
            url = "http://127.0.0.1:18790/recall?q=test&n=1"
            resp = urllib.request.urlopen(url, timeout=30)
            data = json.loads(resp.read())
            if isinstance(data, list):
                return True
        except Exception:
            if attempt < 2:
                time.sleep(5)
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
    # Gateway
    (r"EPERM.*workspace-state\.json",              "critical", "Gateway",       "EPERM on workspace-state.json"),
    (r"Startup failed.*required secrets",           "critical", "Gateway",       "Gateway secrets unavailable at startup"),
    (r"Unrecognized keys.*bootstrapMaxChars",       "critical", "Gateway",       "openclaw.json invalid config keys"),
    (r"FailoverError.*No API key found for provider","critical", "Gateway",      "OpenRouter API key missing"),
    (r"invalid config.*must NOT have additional",   "critical", "Gateway",       "openclaw.json schema violation"),
    # Channels
    (r"Config file is in use by another instance",  "warning",  "Signal-cli",   "signal-cli lock conflict"),
    (r"socket mode failed to start",                "warning",  "Slack",        "Slack socket mode failed"),
    (r"getaddrinfo ENOTFOUND slack\.com",           "warning",  "Slack",        "Slack DNS resolution failure"),
    # Data stores
    (r"FATAL.*nova_memories",                       "critical", "PostgreSQL",   "PostgreSQL fatal error on nova_memories"),
    (r"redis\.exceptions\.(ConnectionError|TimeoutError)", "critical", "Redis", "Redis connection error"),
    (r"MISCONF.*Redis",                             "critical", "Redis",        "Redis MISCONF — RDB save failing"),
    (r"HNSW index.*not found",                      "warning",  "Memory Server","HNSW index missing — recall degraded"),
    (r"Dead-lettered.*after 3 failures",            "warning",  "Memory Server","Memory ingest dead-lettered item"),
    (r"pg_dump.*error",                             "warning",  "PostgreSQL",   "pg_dump backup error"),
    # Scheduler
    (r"ERROR.*Timed out after \d+s",                "warning",  "Scheduler",    "Scheduler task timeout"),
    (r"consecutive.failures.*[5-9]\d*",             "warning",  "Scheduler",    "Scheduler task repeated failures"),
    (r"slack_bot_token unavailable",                "warning",  "Scheduler",    "Scheduler can't read Slack token from Keychain"),
    # Subagents
    (r"\[ERROR\].*agent-\w+",                       "warning",  "Subagent",     "Subagent error"),
    (r"Subagent.*stale|heartbeat.*missing",         "warning",  "Subagent",     "Subagent stale heartbeat"),
    # System
    (r"OOM|out of memory|cannot allocate",          "critical", "System",       "Out of memory condition"),
    (r"No space left on device",                    "critical", "System",       "Disk full"),
    (r"TimeoutExpired.*launchctl",                  "warning",  "Big Brother",  "launchctl kickstart timed out"),
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
    # Subagent logs
    LOG_DIR / "agent-sentinel.log",
    LOG_DIR / "agent-lookout.log",
    LOG_DIR / "agent-analyst.log",
    LOG_DIR / "agent-librarian.log",
    LOG_DIR / "agent-coder.log",
    LOG_DIR / "agent-briefer.log",
    LOG_DIR / "agent-gardener.log",
    LOG_DIR / "nova_mail_agent.log",
    # Note: big-brother.err.log intentionally excluded — would create feedback loop
    # Service logs
    LOG_DIR / "openwebui" / "openwebui-error.log",
    LOG_DIR / "tinychat-error.log",
    LOG_DIR / "mlx-server-error.log",
    LOG_DIR / "pgbouncer.log",
    LOG_DIR / "nova-control-web-error.log",
    Path("/tmp/nova-livetv.log"),
    Path("/tmp/nova-canary.log"),
    Path("/tmp/nova-channel-scan.log"),
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


# ── Journal Staleness Monitor ─────────────────────────────────────────────────
# Maps section → (scheduler_task_id, stale_threshold_hours, backfill_env_var_needed)
# threshold: how many hours before we declare it stale and trigger a backfill
JOURNAL_SECTIONS = {
    "dreams":     ("daily_journal",  26),
    "essays":     ("daily_essay",    26),
    "opinions":   ("daily_opinion",  26),
    "after-dark": ("after_dark",     26),
    "tech-today": ("tech_today",     26),
    "research":   ("research_paper", 50),   # research runs nightly, wider window
    "digests":    ("daily_digest",   26),
}
JOURNAL_CONTENT_DIR = Path("/Volumes/Data/xcode/nova-journal/content")
_journal_backfill_cooldown: dict = {}   # section -> last_backfill_ts
JOURNAL_BACKFILL_COOLDOWN = 7200        # don't re-trigger same section within 2h


def _latest_journal_entry_age(section: str) -> float | None:
    """Return age in hours of the most recent entry in a journal section, or None if unreadable."""
    section_dir = JOURNAL_CONTENT_DIR / section
    if not section_dir.exists():
        return None
    latest_ts = 0.0
    for md_file in section_dir.glob("*.md"):
        if md_file.name == "_index.md":
            continue
        try:
            text = md_file.read_text(errors="replace")
            m = re.search(r'^date:\s*["\']?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', text, re.MULTILINE)
            if m:
                dt = datetime.fromisoformat(m.group(1))
                ts = dt.timestamp()
                if ts > latest_ts:
                    latest_ts = ts
        except Exception:
            pass
    if latest_ts == 0.0:
        return None
    return (time.time() - latest_ts) / 3600.0


def _check_journal_staleness(issues: list, fixes: list):
    """
    Check every journal section. If the latest entry is older than its threshold,
    trigger a backfill by running the responsible scheduler task via /run/ endpoint.
    Includes a 2-hour per-section cooldown so we don't spam.
    Called from every _full_sweep().
    """
    now = time.time()
    scheduler_up = _port_open("127.0.0.1", 37460)

    for section, (task_id, threshold_h) in JOURNAL_SECTIONS.items():
        age_h = _latest_journal_entry_age(section)
        if age_h is None:
            continue
        if age_h < threshold_h:
            continue

        # Stale — check cooldown
        last_backfill = _journal_backfill_cooldown.get(section, 0.0)
        if now - last_backfill < JOURNAL_BACKFILL_COOLDOWN:
            log(f"[journal-staleness] {section} stale ({age_h:.1f}h) but backfill on cooldown",
                level=LOG_INFO, source="big-brother")
            continue

        log(f"[journal-staleness] {section} stale: {age_h:.1f}h since last entry (threshold: {threshold_h}h)",
            level=LOG_WARN, source="big-brother")

        _record_event("warning",
                      f"Journal {section} stale: {age_h:.1f}h since last entry",
                      f"Triggering scheduler task: {task_id}",
                      "Journal")
        issues.append(f"Journal/{section} stale ({age_h:.1f}h)")

        if scheduler_up:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:37460/run/{task_id}",
                    method="POST", data=b""
                )
                urllib.request.urlopen(req, timeout=5)
                fixes.append(f"Triggered {task_id} for stale {section}")
                _journal_backfill_cooldown[section] = now
                log(f"[journal-staleness] Triggered {task_id} for {section}", level=LOG_INFO, source="big-brother")
            except Exception as e:
                log(f"[journal-staleness] Failed to trigger {task_id}: {e}", level=LOG_WARN, source="big-brother")
        else:
            log(f"[journal-staleness] Scheduler down — cannot trigger {task_id}", level=LOG_WARN, source="big-brother")
            fixes.append(f"Could not trigger {task_id} — scheduler unreachable")


# ── Full Health Sweep ─────────────────────────────────────────────────────────

def _is_maintenance_mode() -> bool:
    """Check Redis maintenance flag set by pg_maintain / manual ops."""
    try:
        import redis
        r = redis.from_url("redis://localhost:6379", decode_responses=True)
        return bool(r.get("nova:maintenance:active"))
    except Exception:
        return False


_ALLOWED_MODELS = {
    "ollama/qwen3-next:80b",
    "ollama/nova:latest",
    "ollama/qwen3-coder:30b",
    "ollama/deepseek-r1:8b",
    "ollama/qwen3-vl:4b",
    "mlx:qwen2.5-32b",
    # Research agent only — intentional cloud use for vague/non-private queries
    "openrouter/qwen/qwen3-235b-a22b-2507",
}
_OPENROUTER_ALLOWED_AGENTS = {"research"}


def _check_journal_images():
    """
    Hourly: scan nova-journal for posts missing cover images and regenerate them.
    Only runs when SwarmUI is up. Updates _journal_image_status for the /bb/journal endpoint.
    Called from _full_sweep() gated by JOURNAL_IMAGE_CHECK_INTERVAL.
    """
    global _last_journal_image_check, _journal_image_status

    now = time.time()
    if now - _last_journal_image_check < JOURNAL_IMAGE_CHECK_INTERVAL:
        return

    _last_journal_image_check = now

    # SwarmUI must be up — no point running if image gen will fail
    if not _port_open("127.0.0.1", 7801):
        _journal_image_status.update({
            "last_run_ts": now,
            "last_run_iso": _now_iso(),
            "skipped_swarmui_down": True,
        })
        log("Journal image check skipped — SwarmUI not running", level=LOG_INFO, source="big-brother")
        return

    script = Path.home() / ".openclaw/scripts/nova_fix_missing_images.py"
    if not script.exists():
        log(f"Journal image script not found: {script}", level=LOG_WARN, source="big-brother")
        return

    log("Running journal image repair scan", level=LOG_INFO, source="big-brother")
    try:
        result = subprocess.run(
            ["python3", str(script)],
            capture_output=True, text=True, timeout=600,
        )
        output = result.stdout + result.stderr

        # Parse summary line from script output: "Done. Fixed: N, Failed: M"
        fixed = 0
        failed = 0
        m = re.search(r"Done\. Fixed: (\d+), Failed: (\d+)", output)
        if m:
            fixed, failed = int(m.group(1)), int(m.group(2))

        _journal_image_status.update({
            "last_run_ts": now,
            "last_run_iso": _now_iso(),
            "fixed": fixed,
            "failed": failed,
            "skipped_swarmui_down": False,
            "last_error": None if result.returncode == 0 else output[-300:],
        })

        if fixed > 0:
            _record_event("info", f"Journal image repair: fixed {fixed} missing covers",
                          f"nova_fix_missing_images.py", "Journal")
            log(f"Journal image repair: fixed={fixed} failed={failed}", level=LOG_INFO, source="big-brother")
        elif failed > 0:
            _record_event("warning", f"Journal image repair: {failed} posts still missing covers",
                          "SwarmUI may be struggling — check /tmp/nova-fix-images.log", "Journal")
            log(f"Journal image repair failures: {failed}", level=LOG_WARN, source="big-brother")
        else:
            log("Journal image scan: all posts have covers", level=LOG_INFO, source="big-brother")

    except subprocess.TimeoutExpired:
        _journal_image_status.update({
            "last_run_ts": now,
            "last_run_iso": _now_iso(),
            "last_error": "Timed out after 600s",
        })
        _record_event("warning", "Journal image repair timed out (600s)",
                      "SwarmUI may be overloaded — check /tmp/nova-fix-images.log", "Journal")
    except Exception as e:
        _journal_image_status.update({
            "last_run_ts": now,
            "last_run_iso": _now_iso(),
            "last_error": str(e),
        })
        log(f"Journal image check error: {e}", level=LOG_ERROR, source="big-brother")


def _check_privacy_routing(issues: list):
    """
    Detect model routing drift — any channel or agent pointing to a cloud
    model other than the explicitly allowed research agent is a PII leak risk.
    Appends violations directly to the issues list so they appear in the sweep report
    and trigger an immediate alert to Jordan.
    """
    try:
        config_path = Path.home() / ".openclaw/openclaw.json"
        with open(config_path) as f:
            config = json.load(f)
    except Exception as e:
        log(f"[privacy] Could not read openclaw.json: {e}", level=LOG_WARN, source="big-brother")
        return

    violations = []

    # Check agents.defaults.model
    defaults_model = config.get("agents", {}).get("defaults", {}).get("model", {})
    primary = defaults_model.get("primary", "") if isinstance(defaults_model, dict) else str(defaults_model)
    if primary and primary not in _ALLOWED_MODELS:
        violations.append(f"agents.defaults.model = `{primary}` — unexpected cloud model")

    # Check per-agent model overrides
    for agent in config.get("agents", {}).get("list", []):
        aid = agent.get("id", "?")
        m = agent.get("model", "")
        if not m:
            continue
        if m not in _ALLOWED_MODELS:
            violations.append(f"agent[{aid}].model = `{m}` — NOT in allowed list")
        elif "openrouter" in m and aid not in _OPENROUTER_ALLOWED_AGENTS:
            violations.append(f"agent[{aid}] using OpenRouter — only `research` agent is permitted")

    # Check per-channel model overrides
    mbc = config.get("channels", {}).get("modelByChannel", {})
    for channel, entries in mbc.items():
        for key, model in entries.items():
            if model not in _ALLOWED_MODELS:
                violations.append(f"channels.{channel}.{key} = `{model}` — NOT in allowed list")
            elif "openrouter" in model:
                violations.append(f"channels.{channel}.{key} → OpenRouter — conversations may leak PII")

    # Check Signal is locked down
    signal_conf = config.get("channels", {}).get("signal", {})
    if signal_conf.get("dmPolicy") != "allowlist":
        violations.append(f"Signal dmPolicy = `{signal_conf.get('dmPolicy', 'unset')}` — must be allowlist")
    if signal_conf.get("groupPolicy") != "allowlist":
        violations.append(f"Signal groupPolicy = `{signal_conf.get('groupPolicy', 'unset')}` — must be allowlist")
    allow_from = signal_conf.get("allowFrom", ["*"])
    if allow_from == ["*"] or "*" in allow_from:
        violations.append("Signal allowFrom = ['*'] — open to anyone, must be restricted to Jordan's number")

    if violations:
        for v in violations:
            issues.append(f":rotating_light: PRIVACY VIOLATION: {v}")
            _record_event("critical", f"Privacy routing violation: {v}",
                          "Check openclaw.json model config immediately", "Privacy")
        log(f"[privacy] {len(violations)} violation(s) detected", level=LOG_ERROR, source="big-brother")
    else:
        log("[privacy] Model routing OK — all channels local", level=LOG_INFO, source="big-brother")


def _record_metrics(issues: list, fixes: list, sweep_start: float):
    """
    Snapshot a single per-minute metrics bucket and append to the ring buffer.
    Called at the end of every _full_sweep(). Collects:
      ts, services_up/down, heal_fixes, issues_count, sweep_duration_ms,
      memory_count, mem_queue, redis_pct, gateway_rss_mb,
      sched_failures, dead_letter, disk_data_gb, disk_more_gb,
      ollama_warm_count, journal_fixed_total
    """
    global _metrics_flush_counter

    bucket: dict = {
        "ts": int(time.time()),
        "services_up":        0,
        "services_down":      0,
        "heal_fixes":         len(fixes),
        "issues_count":       len(issues),
        "sweep_ms":           int((time.time() - sweep_start) * 1000),
        "memory_count":       0,
        "mem_queue":          0,
        "redis_pct":          0.0,
        "gateway_rss_mb":     0.0,
        "sched_failures":     0,
        "dead_letter":        0,
        "disk_data_gb":       0.0,
        "disk_more_gb":       0.0,
        "ollama_warm":        0,
        "journal_fixed":      _journal_image_status.get("fixed", 0),
    }

    # Services up/down from cached status
    with _lock:
        svc = dict(_service_status)
    bucket["services_up"]   = sum(1 for s in svc.values() if s.get("up", True))
    bucket["services_down"] = sum(1 for s in svc.values() if not s.get("up", True))

    # Memory server stats
    try:
        r = urllib.request.urlopen("http://127.0.0.1:18790/stats", timeout=3)
        ms = json.loads(r.read())
        bucket["memory_count"] = ms.get("count", 0)
        bucket["mem_queue"]    = ms.get("queue_length", 0)
        bucket["dead_letter"]  = ms.get("dead_letter_count", 0)
    except Exception:
        pass

    # Redis memory %
    try:
        import redis as _rds
        rc = _rds.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        ri = rc.info("memory")
        if ri.get("maxmemory"):
            bucket["redis_pct"] = round(ri["used_memory"] / ri["maxmemory"] * 100, 1)
    except Exception:
        pass

    # Gateway RSS
    try:
        r2 = subprocess.run(["pgrep", "-f", "^openclaw$"], capture_output=True, text=True)
        pids = [p for p in r2.stdout.strip().split() if p]
        if pids:
            r3 = subprocess.run(["ps", "-o", "rss=", "-p", pids[0]],
                                 capture_output=True, text=True)
            bucket["gateway_rss_mb"] = round(int(r3.stdout.strip() or "0") / 1024, 1)
    except Exception:
        pass

    # Scheduler failures
    try:
        r4 = urllib.request.urlopen("http://127.0.0.1:37460/status", timeout=3)
        sc = json.loads(r4.read())
        bucket["sched_failures"] = sc.get("total_failures", 0)
    except Exception:
        pass

    # Disk free on /Volumes/Data and /Volumes/MoreData
    for vol, key in [("/Volumes/Data", "disk_data_gb"), ("/Volumes/MoreData", "disk_more_gb")]:
        try:
            st = os.statvfs(vol)
            bucket[key] = round(st.f_bavail * st.f_frsize / 1e9, 1)
        except Exception:
            pass

    # Ollama warm model count
    try:
        r5 = urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=3)
        bucket["ollama_warm"] = len(json.loads(r5.read()).get("models", []))
    except Exception:
        pass

    with _lock:
        _metrics.append(bucket)

    _metrics_flush_counter += 1
    if _metrics_flush_counter >= METRICS_FLUSH_EVERY:
        _metrics_flush_counter = 0
        threading.Thread(target=_flush_metrics, daemon=True).start()


def _full_sweep():
    """Run all checks and heal everything that can be healed."""
    _sweep_start = time.time()
    issues = []
    fixes = []

    # ── Skip restarts during maintenance window (pg_maintain sets this flag) ──
    if _is_maintenance_mode():
        log("Maintenance mode active — skipping service restarts this sweep",
            level=LOG_INFO, source="big-brother")
        return

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
            if name in SILENCED_SERVICES:
                log(f"[sweep] {name} down but silenced — skipping alert", level=LOG_INFO, source="big-brother")
                continue
            issues.append(f"{name} (:{port}) DOWN")
            if not critical and name not in ("SwarmUI", "TinyChat"):
                _record_event("warning", f"{name} not responding on :{port}", "No action (non-critical)", name)
                continue

            if protected_running and name not in ("Gateway", "Signal-cli"):
                _queue_restart(name)
                fixes.append(f"Queued restart of {name} (protected task running)")
                _record_event("warning", f"{name} DOWN", "Queued restart", name)
                continue

            def _kickstart(lbl: str, timeout: int = 15) -> bool:
                try:
                    r = subprocess.run(
                        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{lbl}"],
                        capture_output=True, timeout=timeout,
                    )
                    return r.returncode == 0
                except subprocess.TimeoutExpired:
                    log(f"launchctl kickstart timed out for {lbl}", level=LOG_WARN, source="big-brother")
                    return False
                except Exception as e:
                    log(f"launchctl kickstart failed for {lbl}: {e}", level=LOG_ERROR, source="big-brother")
                    return False

            if name == "PostgreSQL":
                try:
                    subprocess.run(["pg_isready"], capture_output=True, timeout=5)
                except Exception:
                    pass
                if label:
                    _kickstart(label)
                fixes.append("Restarted PostgreSQL")
                _record_event("critical", "PostgreSQL DOWN", "Restarted via launchctl", "PostgreSQL")

            elif name == "Redis":
                if label:
                    _kickstart(label)
                fixes.append("Restarted Redis")
                _record_event("critical", "Redis DOWN", "Restarted via launchctl", "Redis")

            elif name == "Memory Server" and label:
                _kickstart(label)
                fixes.append("Restarted Memory Server")
                _record_event("critical", "Memory Server DOWN", "Restarted via launchctl", "Memory Server")

            elif name == "Scheduler":
                if not _check_scheduler_heartbeat():
                    _kickstart("com.nova.scheduler")
                    fixes.append("Restarted Scheduler (stale heartbeat)")
                    _record_event("critical", "Scheduler stale heartbeat", "Kickstarted via launchctl", "Scheduler")

            elif name == "Gateway" or name == "Signal-cli":
                success = _restart_gateway()
                fix_msg = "Restarted Gateway" if success else "FAILED to restart Gateway"
                fixes.append(fix_msg)
                _record_event("critical", f"{name} DOWN",
                              fix_msg, "Gateway")

            elif label:
                _kickstart(label)
                fixes.append(f"Restarted {name}")
                _record_event("warning", f"{name} DOWN", f"Restarted {name}", name)

        else:
            with _lock:
                issue_key = f"{name}_down"
                if issue_key in _alerted_issues:
                    _alerted_issues.discard(issue_key)

    # ── Channel health (only if gateway is up) ───────────────────────────────
    global _discord_timeout_count
    gateway_up = _port_open("127.0.0.1", 18789)
    if gateway_up:
        channels = _check_gateway_log_channels()

        # Discord timeout strike counting — don't restart on a single timeout
        if channels.get("discord") == "timeout":
            _discord_timeout_count += 1
            if _discord_timeout_count >= DISCORD_STRIKE_THRESHOLD:
                log(f"Discord timeout strike {_discord_timeout_count} — treating as disconnect",
                    level=LOG_WARN, source="big-brother")
                channels["discord"] = "disconnected"
                _discord_timeout_count = 0
            else:
                log(f"Discord timeout strike {_discord_timeout_count}/{DISCORD_STRIKE_THRESHOLD} — not restarting yet",
                    level=LOG_INFO, source="big-brother")
                channels["discord"] = "unknown"  # Don't count as disconnected
        elif channels.get("discord") == "connected":
            _discord_timeout_count = 0  # Reset on confirmed connection

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

    # ── Signal gap tracking ───────────────────────────────────────────────────
    global _signal_down_since
    signal_up = _port_open("127.0.0.1", 8080)
    if not signal_up:
        if _signal_down_since == 0.0:
            _signal_down_since = time.time()
            log("Signal-cli went down — starting gap timer", level=LOG_INFO, source="big-brother")
        else:
            gap_s = int(time.time() - _signal_down_since)
            if gap_s > 120:  # Only alert if down >2 min (normal respawn takes <60s)
                _record_event("warning",
                              f"Signal-cli unreachable for {gap_s}s — messages during this window lost",
                              "OpenClaw will auto-respawn; if >5min check signal-cli lock",
                              "Signal-cli")
    else:
        if _signal_down_since > 0.0:
            gap_s = int(time.time() - _signal_down_since)
            if gap_s > 60:
                _notify(f":signal_strength: Signal-cli recovered after {gap_s}s gap. Messages sent during that window may have been lost.")
                log(f"Signal-cli recovered after {gap_s}s", level=LOG_INFO, source="big-brother")
            _signal_down_since = 0.0

    # ── Gateway memory check ───────────────────────────────────────────────────
    gw_pids = []
    try:
        result = subprocess.run(["pgrep", "-f", "^openclaw$"], capture_output=True, text=True)
        gw_pids = [int(p) for p in result.stdout.strip().split() if p]
    except Exception:
        pass
    for pid in gw_pids:
        try:
            result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                                    capture_output=True, text=True)
            rss_kb = int(result.stdout.strip() or "0")
            rss_gb = rss_kb / 1024 / 1024
            if rss_gb > 2.0:
                _record_event("warning",
                              f"Gateway RSS {rss_gb:.1f} GB — possible memory leak",
                              "Restart gateway during next quiet window",
                              "Gateway")
                log(f"Gateway RSS high: {rss_gb:.1f} GB (PID {pid})", level=LOG_WARN,
                    source="big-brother")
        except Exception:
            pass

    # ── Volume mount checks ──────────────────────────────────────────────────
    for mount_path, desc in REQUIRED_MOUNTS:
        p = Path(mount_path)
        if not p.exists() or not p.is_mount():
            issues.append(f"Volume NOT mounted: {mount_path} ({desc})")
            _record_event("critical", f"Volume unmounted: {mount_path}",
                          "Re-mount or check NAS/drive connection", "System")
        else:
            # Check we can actually read it (not just that it's mounted)
            try:
                list(p.iterdir())
            except PermissionError:
                issues.append(f"Volume mounted but unreadable: {mount_path}")
                _record_event("warning", f"Volume unreadable: {mount_path}",
                              "Check TCC/permissions", "System")

    # ── External LAN service checks ──────────────────────────────────────────
    for name, host, port in EXTERNAL_CHECKS:
        if not _port_open(host, port, timeout=5.0):
            issues.append(f"{name} ({host}:{port}) unreachable")
            _record_event("warning", f"{name} unreachable",
                          "Check device power and LAN connection", name)

    # ── Broken launchd services ──────────────────────────────────────────────
    for label, name, can_restart, silenced in LAUNCHD_MONITORED:
        try:
            r = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode != 0:
                continue  # service not loaded at all — skip
            pid_m    = re.search(r'"PID"\s*=\s*(\d+)',         r.stdout)
            exit_m   = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', r.stdout)
            pid      = pid_m.group(1)  if pid_m  else None
            exit_code = int(exit_m.group(1)) if exit_m else None
            if pid is not None or exit_code in (None, 0):
                continue  # running fine
            if silenced:
                log(f"[sweep] {name} ({label}) not running (exit {exit_code}) — silenced",
                    level=LOG_INFO, source="big-brother")
                continue
            if can_restart:
                try:
                    subprocess.run(
                        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                        capture_output=True, timeout=15,
                    )
                    fixes.append(f"Kickstarted {name} ({label})")
                    _record_event("warning", f"{name} crashed (exit {exit_code})",
                                  f"Auto-kickstarted via launchctl", name)
                except subprocess.TimeoutExpired:
                    issues.append(f"{name} crashed (exit {exit_code}) — kickstart timed out")
                    _record_event("critical", f"{name} crashed, kickstart timed out",
                                  "Restart manually", name)
            else:
                issues.append(f"{name} not running (exit {exit_code}) — needs manual fix")
                _record_event("warning", f"{name} crashed (exit {exit_code})",
                              "Cannot auto-restart — check underlying dependency", name)
        except Exception:
            pass

    # ── Redis memory utilization ─────────────────────────────────────────────
    try:
        import redis as _redis
        r = _redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        info = r.info("memory")
        used = info.get("used_memory", 0)
        max_mem = info.get("maxmemory", 0)
        if max_mem > 0:
            pct = used / max_mem * 100
            if pct > 85:
                issues.append(f"Redis memory {pct:.0f}% full ({used//1e6:.0f}MB / {max_mem//1e6:.0f}MB)")
                _record_event("warning", f"Redis at {pct:.0f}% capacity",
                              "Check for cache bloat; allkeys-lru will evict if full", "Redis")
    except Exception:
        pass

    # ── Ollama model warmup state ─────────────────────────────────────────────
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=8)
        ps_data = json.loads(resp.read())
        loaded = [m["name"] for m in ps_data.get("models", [])]
        needed = {"qwen3-next:80b", "qwen3-coder:30b"}
        cold = needed - {m.split(":")[0] + ":" + m.split(":")[1] if ":" in m else m for m in loaded}
        # Only warn during active hours — models unload when idle
        if cold and not _is_quiet_hours():
            log(f"[ollama] Cold models (next request will be slow): {cold}",
                level=LOG_INFO, source="big-brother")
    except Exception:
        pass

    # ── Scheduler per-task failure visibility ────────────────────────────────
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:37460/tasks", timeout=5)
        tasks = json.loads(resp.read())
        if isinstance(tasks, list):
            for t in tasks:
                name = t.get("name", "?")
                fails = t.get("consecutive_failures", 0)
                if fails >= 3:
                    issues.append(f"Scheduler task '{name}' failing: {fails} consecutive failures")
                    _record_event("warning", f"Scheduler task '{name}' {fails} consecutive failures",
                                  "Check scheduler.log for error details", "Scheduler")
    except Exception:
        pass

    # ── Log file size watchdog ────────────────────────────────────────────────
    warn_size_mb = 100
    for log_path in LOG_FILES_TO_WATCH:
        try:
            size_mb = log_path.stat().st_size / 1e6 if log_path.exists() else 0
            if size_mb > warn_size_mb:
                issues.append(f"Log file too large: {log_path.name} ({size_mb:.0f}MB)")
                _record_event("warning", f"{log_path.name} is {size_mb:.0f}MB",
                              "Run nova_log_rotate.py or truncate manually", "System")
        except Exception:
            pass

    # ── Journal staleness monitor (every sweep, auto-triggers backfill if stale) ──
    _check_journal_staleness(issues, fixes)

    # ── Journal cover image repair (hourly, gated by JOURNAL_IMAGE_CHECK_INTERVAL) ──
    _check_journal_images()

    # ── Privacy / PII leak monitor ───────────────────────────────────────────
    _check_privacy_routing(issues)

    # ── Memory dead-letter queue ──────────────────────────────────────────────
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:18790/stats", timeout=5)
        stats = json.loads(resp.read())
        dead = stats.get("dead_letter_count", 0)
        if dead > 10:
            issues.append(f"Memory dead-letter queue has {dead} items — embedding failures")
            _record_event("warning", f"Memory dead-letter queue: {dead} items",
                          "Check /queue/dead-letter endpoint and Ollama embed model", "Memory Server")
    except Exception:
        pass

    # ── Scheduler failure rate ────────────────────────────────────────────────
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:37460/status", timeout=5)
        sched = json.loads(resp.read())
        total_runs = sched.get("total_runs", 0)
        total_fail = sched.get("total_failures", 0)
        if total_runs > 10 and total_fail / total_runs > 0.15:
            issues.append(f"Scheduler failure rate high: {total_fail}/{total_runs} ({total_fail*100//total_runs}%)")
            _record_event("warning", f"Scheduler failure rate: {total_fail}/{total_runs}",
                          "Check ~/.openclaw/logs/scheduler.log for timed-out tasks", "Scheduler")
    except Exception:
        pass

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

    _record_metrics(issues, fixes, _sweep_start)
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
                      fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
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
        elif self.path == "/bb/health":
            # Full health snapshot for the Big Brother dashboard
            with _lock:
                events = list(_heal_events)
                svc    = dict(_service_status)
            uptime = int(time.time() - _start_time)

            # Scheduler stats
            sched_stats = {}
            try:
                r = urllib.request.urlopen("http://127.0.0.1:37460/status", timeout=5)
                sched_stats = json.loads(r.read())
            except Exception:
                pass

            # Memory stats
            mem_stats = {}
            try:
                r = urllib.request.urlopen("http://127.0.0.1:18790/stats", timeout=5)
                mem_stats = json.loads(r.read())
            except Exception:
                pass

            # Ollama model list + warmup state
            ollama_models = []
            ollama_warm = []
            try:
                r = urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=8)
                ollama_models = [m["name"] for m in json.loads(r.read()).get("models", [])]
            except Exception:
                pass
            try:
                r = urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=5)
                ollama_warm = [m["name"] for m in json.loads(r.read()).get("models", [])]
            except Exception:
                pass

            # Redis memory stats
            redis_mem = {}
            try:
                import redis as _rds
                rc = _rds.Redis(host="127.0.0.1", port=6379, decode_responses=True)
                ri = rc.info("memory")
                redis_mem = {
                    "used_mb": round(ri.get("used_memory", 0) / 1e6, 1),
                    "max_mb": round(ri.get("maxmemory", 0) / 1e6, 1),
                    "pct": round(ri.get("used_memory", 0) / ri.get("maxmemory", 1) * 100, 1)
                    if ri.get("maxmemory") else 0,
                }
            except Exception:
                pass

            # Volume mount status
            volume_status = {}
            for mount_path, desc in REQUIRED_MOUNTS:
                p = Path(mount_path)
                volume_status[mount_path] = {
                    "mounted": p.exists() and p.is_mount(),
                    "desc": desc,
                }

            # External service reachability
            external_status = {}
            for name, host, port in EXTERNAL_CHECKS:
                external_status[name] = _port_open(host, port, timeout=5.0)

            # Scheduler per-task detail
            sched_tasks = []
            try:
                r = urllib.request.urlopen("http://127.0.0.1:37460/tasks", timeout=5)
                raw = json.loads(r.read())
                if isinstance(raw, list):
                    sched_tasks = raw
            except Exception:
                pass

            # Privacy routing snapshot
            privacy_ok = True
            privacy_violations = []
            try:
                with open(str(Path.home() / ".openclaw/openclaw.json")) as f:
                    cfg = json.load(f)
                for agent in cfg.get("agents", {}).get("list", []):
                    m = agent.get("model", "")
                    if "openrouter" in m and agent.get("id") not in _OPENROUTER_ALLOWED_AGENTS:
                        privacy_violations.append(f"agent[{agent['id']}] → {m}")
                        privacy_ok = False
                sig = cfg.get("channels", {}).get("signal", {})
                if sig.get("dmPolicy") != "allowlist" or sig.get("groupPolicy") != "allowlist":
                    privacy_violations.append(f"Signal open: dm={sig.get('dmPolicy')} group={sig.get('groupPolicy')}")
                    privacy_ok = False
            except Exception:
                pass

            # Recent errors by service (last 50 events)
            service_errors: dict = {}
            for ev in events[:50]:
                svc_name = ev.get("service", "unknown")
                if ev.get("severity") in ("critical", "warning"):
                    service_errors.setdefault(svc_name, []).append({
                        "ts": ev.get("ts"), "issue": ev.get("issue"), "fix": ev.get("fix"),
                        "severity": ev.get("severity"),
                    })

            self._json({
                "daemon": "big-brother",
                "version": VERSION,
                "pid": os.getpid(),
                "uptime_s": uptime,
                "sweep_interval_s": SWEEP_INTERVAL,
                "services": svc,
                "services_down": [n for n, s in svc.items() if not s.get("up", True)],
                "events_total": len(events),
                "recent_events": events[:20],
                "service_errors": service_errors,
                "scheduler": sched_stats,
                "memory": mem_stats,
                "ollama_models": ollama_models,
                "privacy": {
                    "ok": privacy_ok,
                    "violations": privacy_violations,
                },
                "ollama_models": ollama_models,
                "ollama_warm": ollama_warm,
                "redis_mem": redis_mem,
                "volumes": volume_status,
                "external": external_status,
                "scheduler_tasks": sched_tasks,
                "alerted_count": len(_alerted_issues),
                "pending_restarts": list(_pending_restart),
            })
        elif self.path == "/bb/metrics":
            # Return the full ring buffer (up to 10080 one-minute buckets = 7 days)
            with _lock:
                data = list(_metrics)
            self._json(data)
        elif self.path == "/bb/journal":
            next_run_in = max(0, int(JOURNAL_IMAGE_CHECK_INTERVAL - (time.time() - _last_journal_image_check)))
            self._json({
                **_journal_image_status,
                "check_interval_s": JOURNAL_IMAGE_CHECK_INTERVAL,
                "next_run_in_s": next_run_in,
                "swarmui_up": _port_open("127.0.0.1", 7801),
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/bb/force-check":
            threading.Thread(target=_full_sweep, daemon=True).start()
            self._json({"queued": True})
        elif self.path == "/bb/journal/run":
            # Manually trigger an immediate journal image repair (ignores cooldown)
            global _last_journal_image_check
            _last_journal_image_check = 0.0
            threading.Thread(target=_check_journal_images, daemon=True).start()
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

    # Restore metrics ring buffer from disk
    _load_metrics()
    log(f"Loaded {len(_metrics)} metric buckets from disk", level=LOG_INFO, source="big-brother")

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
