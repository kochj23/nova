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
  ─ Metal GPU contention detection (Ollama vs mlx_whisper deadlock prevention)

Safe-restart policy:
  If a PROTECTED long-running task is detected via Scheduler API, Big Brother
  queues the restart and fires it only when the task finishes.

Notification paths (no dependency on gateway being alive):
  Primary: nova_config.post_both() — Slack HTTP + Discord HTTP
  Fallback (gateway dead): raw Slack HTTP + signal-cli direct

Diagnostics API (consumed by NovaControl Diagnostics tab):
  GET  http://192.168.1.6:37461/bb/status        — daemon health + summary
  GET  http://192.168.1.6:37461/bb/events?n=100  — recent heal events
  GET  http://192.168.1.6:37461/bb/services      — per-service status
  GET  http://192.168.1.6:37461/bb/gpu           — Metal GPU contention status
  POST http://192.168.1.6:37461/bb/force-check   — manual full check now

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
from enum import Enum
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

# How often to run a full health sweep (seconds).
# 90s gives enough breathing room to detect transitions within 2 sweeps
# while not hammering Slack when the per-issue cooldown has edge cases.
SWEEP_INTERVAL = 90

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
    ("Gateway v2",    "127.0.0.1", 18792, "net.digitalnoise.nova-gateway-v2",     True,  "/health"),
    # OpenClaw intentionally stopped — silenced, kept for fallback reference only
    # ("Gateway (OC)",  "127.0.0.1", 18789, "ai.openclaw.gateway",                  False, "/health"),
    ("Scheduler",     "127.0.0.1", 37460, "com.nova.scheduler",                   True,  "/status"),
    # ── AI inference (non-critical — can recover from) ───────────────────────
    ("MLX Server",    "127.0.0.1", 5050,  "net.digitalnoise.mlx-server",          False, "/v1/models"),
    ("SwarmUI",       "127.0.0.1", 7801,  None,                                   False, None),
    ("ComfyUI",       "127.0.0.1", 8188,  None,                                   False, None),
    ("TinyChat",      "192.168.1.10", 8000, None,                                  False, None),
    ("OpenWebUI",     "192.168.1.6", 3000,  "net.digitalnoise.openwebui",           False, None),
    ("SearXNG",       "192.168.1.10", 8080, None,                                  False, None),
    # ── Channels ─────────────────────────────────────────────────────────────
    ("Signal-cli",    "127.0.0.1", 8080,  None,                                   False, None),
    # ── Nova apps ────────────────────────────────────────────────────────────
    ("NovaControl",   "127.0.0.1", 37400, "net.digitalnoise.NovaControl",         False, "/api/status"),
    ("NovaControl Web","127.0.0.1", 37450, "net.digitalnoise.nova-control-web",    False, None),
    # BB cannot self-check — removed to prevent false alerts
    # ("Big Brother",   "127.0.0.1", 37461, None,                                   False, "/bb/status"),
    ("Nova Syslog",   "127.0.0.1", 37462, "net.digitalnoise.nova-syslog",         False, "/health"),
    # ── External / LAN (monitored but not auto-restarted) ────────────────────
    ("Plex",          PLEX_IP,     32400, None,                                   False, "/web"),
    ("HDHomeRun",     HDHR_IP,     80,    None,                                   False, None),
    ("UNAS Pro 8",    "192.168.1.69", 443, None,                                  False, None),  # HTTPS+auth required; TCP port check only
    # ── TV-Movies macmini (192.168.1.7) ──────────────────────────────────────
    ("Grafana (TV)",  "192.168.1.7", 3000, None,                                  False, "/api/health"),
    ("go2rtc (TV)",   "192.168.1.7", 1984, None,                                  False, None),
    ("Homebridge (TV)","192.168.1.7", 8581, None,                                  False, None),
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
    ("Wazuh",         "192.168.1.7", 9200),
]

# Wazuh SIEM — poll for high-severity alerts
WAZUH_INDEXER_URL = "https://192.168.1.7:9200"
WAZUH_INDEXER_USER = "admin"
WAZUH_INDEXER_PASS = "admin"
WAZUH_ALERT_LEVEL_THRESHOLD = 10  # Only surface alerts at level 10+
WAZUH_POLL_INTERVAL = 300  # Poll every 5 minutes (not every sweep)

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

# Per-issue alert rate limiting — prevents the same root cause from
# hammering Slack every 60s. Maps stable issue key -> last alert timestamp.
_issue_last_alerted: dict = {}
ISSUE_ALERT_COOLDOWN = 600           # 10 min between identical alerts (services)
DIGEST_MODE = True                   # Buffer all alerts and post one hourly summary
DIGEST_INTERVAL = 3600               # 1 hour between digest posts
SCHEDULER_ALERT_COOLDOWN = 14400     # 4h between scheduler task failure alerts (they run daily/weekly)

# Digest buffer
_digest_buffer: list = []            # [(timestamp, message, is_critical)]
_last_digest_post: float = time.time()

# Per-service kickstart grace period — after a kickstart, skip port checks
# for this many seconds to prevent EADDRINUSE false-crash cascade.
_service_kickstart_at: dict = {}   # service_name -> timestamp of last kickstart
SERVICE_STARTUP_GRACE = 30         # seconds to skip port checks after kickstart

# Dead-letter tracking — only alert if count is NEW or GROWING
_dead_letter_last_count: int = 0
_dead_letter_last_alerted: float = 0.0
DEAD_LETTER_ALERT_COOLDOWN = 1800   # 30 min between dead-letter alerts
DEAD_LETTER_THRESHOLD = 10          # alert threshold

# Gateway restart cooldown — don't restart more than once per 5 minutes
GATEWAY_RESTART_COOLDOWN = 300  # seconds
_last_gateway_restart: float = 0.0

# Per-service crash-loop detection — track restart timestamps in a sliding window
# If a service is restarted 3+ times in 5 minutes WITH healthy dependencies,
# it's a real bug. Set a 10-minute cooldown to stop the spam loop.
_CRASH_LOOP_WINDOW   = 300   # 5 min sliding window
_CRASH_LOOP_MAX      = 3     # max restarts before declaring crash-loop
_CRASH_LOOP_COOLDOWN = 600   # 10 min cooldown after crash-loop detected
_service_restart_times: dict = {}    # service_name -> deque of restart timestamps
_service_crash_loop_until: dict = {} # service_name -> timestamp when cooldown expires

# Discord 3-strike before restart — timeouts ≠ disconnect
_discord_timeout_count: int = 0

# ── Claude Queue Escalation Tracking ────────────────────────────────────────
# Track persistent service downtime for escalation to claude_queue.
# Maps service_name -> timestamp of first continuous downtime detection.
_service_down_since: dict = {}            # service_name -> first_down_ts
SERVICE_ESCALATION_THRESHOLD = 900        # 15 minutes of continuous downtime

# PostgreSQL-specific downtime tracking (separate from service checks)
_pg_down_since: float = 0.0              # 0 = currently up
PG_ESCALATION_THRESHOLD = 300            # 5 minutes

# GPU escalation — track if whisper kill already failed to resolve
_gpu_escalated_this_cycle: bool = False

# Internet outage tracking — suppress channel-disconnect storm when WAN is down
_internet_down: bool = False
_internet_down_since: float = 0.0
_internet_down_alerted: bool = False
_wazuh_last_poll: float = 0.0
_wazuh_last_alert_id: str = ""
DISCORD_STRIKE_THRESHOLD = 3

# External LAN check failure dampening — require 2 consecutive failures before alerting.
# Prevents a single port-open timeout from triggering a false alarm.
_external_fail_counts: dict = {}   # name → consecutive failure count
EXTERNAL_FAIL_THRESHOLD = 2        # must fail this many sweeps in a row to alert

# ── Score-History Confirmation (Frigate pattern) ─────────────────────────────
# Instead of alerting on a single failed check, maintain a sliding window of
# recent results per service. Only alert when the MEDIAN indicates failure
# (3 of 5 checks must fail). Kills single-blip alert fatigue.
from collections import deque
SCORE_HISTORY_LEN = 5              # sliding window size
SCORE_FAIL_THRESHOLD = 3           # must fail this many within window to confirm down
_service_score_history: dict = {}  # name → deque of bool (True=up, False=down)

# ── Adaptive Sweep Frequency (Frigate "stationary object" pattern) ───────────
# Reduce check frequency for stable services, increase for troubled ones.
# A service healthy for 6+ hours → relax to 300s checks.
# On failure or neighbor failure → snap to 30s checks.
HEALTHY_STRETCH_S = 6 * 3600      # 6 hours before relaxing
RELAXED_INTERVAL = 300             # 5 min between checks for stable services
HEIGHTENED_INTERVAL = 30           # 30s checks for recently-troubled services
_service_check_interval: dict = {} # name → current interval in seconds
_service_last_checked: dict = {}   # name → timestamp of last actual check
_service_healthy_since: dict = {}  # name → timestamp when service became healthy

# How far back to look in the gateway log for channel state (seconds).
# Lines older than this are ignored — prevents stale "websocket closed" lines from
# triggering a restart loop after the gateway successfully reconnects.
GATEWAY_LOG_WINDOW_SECS = 120

# ── Health State Classification ──────────────────────────────────────────────
# Replaces binary up/down with nuanced states for smarter remediation.

class HealthState:
    HEALTHY = "healthy"       # Normal operation
    SLOW = "slow"             # Responding but latency > threshold (leave alone)
    STUCK = "stuck"           # Process alive, no progress for extended period (nudge first)
    CRASHED = "crashed"       # Process dead, was expected to be running (respawn)
    CONTENDED = "contended"   # Resource contention (find the culprit, not the victim)


# ── Escalation Tier System ───────────────────────────────────────────────────
# Prevents notification spam by tracking per-issue escalation state with
# cooldowns, auto-bumps, and suppression counts.

_escalations: dict = {}  # key: issue_id -> {severity, first_seen, last_notified, notify_count, suppressed_count}

ESCALATION_RULES = {
    "info":     {"initial_cooldown": 300,  "max_notifications": 3,  "bump_after": 3600},   # 5min cooldown, bump to warning after 1h
    "warning":  {"initial_cooldown": 600,  "max_notifications": 5,  "bump_after": 7200},   # 10min cooldown, bump to critical after 2h
    "critical": {"initial_cooldown": 300,  "max_notifications": 10, "bump_after": None},   # 5min cooldown, no further bump
}

_SEVERITY_ORDER = ["info", "warning", "critical"]


def _next_severity(current: str) -> str:
    """Bump severity one level up. Returns same if already at max."""
    idx = _SEVERITY_ORDER.index(current) if current in _SEVERITY_ORDER else 0
    if idx < len(_SEVERITY_ORDER) - 1:
        return _SEVERITY_ORDER[idx + 1]
    return current


def should_notify(issue_id: str, severity: str) -> tuple:
    """Escalation-aware notification gate.

    Returns (should_send: bool, modified_message_suffix: str).
    Tracks state per issue_id. Handles cooldowns, auto-bumps, and resolution.
    """
    now = time.time()

    with _lock:
        if issue_id not in _escalations:
            # First detection — notify immediately
            _escalations[issue_id] = {
                "severity": severity,
                "first_seen": now,
                "last_notified": now,
                "notify_count": 1,
                "suppressed_count": 0,
            }
            return (True, "")

        state = _escalations[issue_id]
        rules = ESCALATION_RULES.get(state["severity"], ESCALATION_RULES["warning"])

        # Check if severity should auto-bump
        if rules["bump_after"] is not None:
            if now - state["first_seen"] > rules["bump_after"]:
                old_sev = state["severity"]
                state["severity"] = _next_severity(old_sev)
                log(f"[escalation] {issue_id} bumped {old_sev} -> {state['severity']} "
                    f"(ongoing {int((now - state['first_seen']) / 60)}m)",
                    level=LOG_WARN, source="big-brother")
                # Bump triggers immediate notification
                state["last_notified"] = now
                state["notify_count"] += 1
                duration_m = int((now - state["first_seen"]) / 60)
                suffix = f" [ESCALATED to {state['severity']} after {duration_m}m]"
                return (True, suffix)

        # Check cooldown
        cooldown = rules["initial_cooldown"]
        if now - state["last_notified"] < cooldown:
            state["suppressed_count"] += 1
            return (False, "")

        # Check max notifications
        if state["notify_count"] >= rules["max_notifications"]:
            state["suppressed_count"] += 1
            return (False, "")

        # Cooldown expired and under max — notify with context
        state["last_notified"] = now
        state["notify_count"] += 1
        duration_m = int((now - state["first_seen"]) / 60)
        suppressed = state["suppressed_count"]
        suffix = f" (ongoing {duration_m}m"
        if suppressed > 0:
            suffix += f", suppressed {suppressed} alerts"
        suffix += ")"
        return (True, suffix)


def _resolve_escalation(issue_id: str) -> tuple:
    """Mark an issue as resolved. Returns (was_tracked: bool, message_suffix: str).

    Call this when a previously-detected issue clears.
    """
    with _lock:
        if issue_id not in _escalations:
            return (False, "")
        state = _escalations.pop(issue_id)
        duration_m = int((time.time() - state["first_seen"]) / 60)
        suffix = f" RESOLVED after {duration_m}m"
        return (True, suffix)


# ── Fresh-Eyes Canary Check (Boot-Dog Pattern) ───────────────────────────────
# Every 10 minutes, ask a local LLM to review system metrics for anomalies
# that rule-based logic might miss.

_last_canary: float = 0.0
CANARY_INTERVAL = 600  # 10 minutes


def _build_metrics_summary() -> str:
    """Build a compact metrics summary for the canary LLM to review."""
    lines = []

    # Services status
    with _lock:
        svc = dict(_service_status)
    up_count = sum(1 for s in svc.values() if s.get("up", True))
    down_count = sum(1 for s in svc.values() if not s.get("up", True))
    down_names = [n for n, s in svc.items() if not s.get("up", True)]
    lines.append(f"Services: {up_count}/{up_count + down_count} up"
                 + (f" ({', '.join(down_names)} down)" if down_names else ""))

    # GPU status
    ollama_lat = _gpu_contention_status.get("ollama_latency_ms")
    contention = _gpu_contention_status.get("contention_active", False)
    lines.append(f"GPU: Ollama {ollama_lat}ms, contention={'yes' if contention else 'no'}")

    # Scheduler
    try:
        resp = urllib.request.urlopen(f"http://{LAN_IP}:37460/status", timeout=3)
        sc = json.loads(resp.read())
        total_tasks = sc.get("total_tasks", 0)
        failing = sc.get("total_failures", 0)
        lines.append(f"Scheduler: {failing} failures out of {total_tasks} tasks")
    except Exception:
        lines.append("Scheduler: unreachable")

    # Memory server
    try:
        resp = urllib.request.urlopen(f"http://{LAN_IP}:18790/stats", timeout=3)
        ms = json.loads(resp.read())
        lines.append(f"Memory: {ms.get('count', 0):,} vectors, queue: {ms.get('queue_length', 0)}, "
                     f"dead letter: {ms.get('dead_letter_count', 0)}")
    except Exception:
        lines.append("Memory: unreachable")

    # Disk
    for vol, label in [("/Volumes/Data", "Data"), ("/Volumes/MoreData", "MoreData")]:
        try:
            st = os.statvfs(vol)
            total_gb = (st.f_blocks * st.f_frsize) / 1e9
            free_gb = (st.f_bavail * st.f_frsize) / 1e9
            used_pct = int(100 * (1 - st.f_bavail / max(st.f_blocks, 1)))
            lines.append(f"Disk /{label}: {used_pct}% used ({free_gb:.0f}GB free)")
        except Exception:
            pass

    # Redis
    try:
        import redis as _rds
        rc = _rds.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        ri = rc.info("memory")
        used_mb = ri.get("used_memory", 0) / 1e6
        max_mb = ri.get("maxmemory", 0) / 1e6
        pct = (ri["used_memory"] / ri["maxmemory"] * 100) if ri.get("maxmemory") else 0
        lines.append(f"Redis: {pct:.1f}% ({used_mb:.1f}MB/{max_mb:.0f}MB)")
    except Exception:
        lines.append("Redis: unreachable")

    # Gateway uptime
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:18792/health", timeout=3)
        gw = json.loads(resp.read())
        uptime_h = gw.get("uptime_s", 0) / 3600
        sessions = gw.get("sessions", 0)
        lines.append(f"Gateway: live, {sessions} session(s), uptime {uptime_h:.1f}h")
    except Exception:
        lines.append("Gateway: unreachable or no health data")

    # Recent escalation count
    with _lock:
        active_escalations = len(_escalations)
    if active_escalations > 0:
        lines.append(f"Active escalations: {active_escalations}")

    return "\n".join(lines)


def _check_wazuh_alerts(issues: list):
    """Poll Wazuh indexer for high-severity alerts since last check."""
    global _wazuh_last_poll, _wazuh_last_alert_id
    import base64
    import ssl

    now = time.time()
    if now - _wazuh_last_poll < WAZUH_POLL_INTERVAL:
        return
    _wazuh_last_poll = now

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        creds = base64.b64encode(
            f"{WAZUH_INDEXER_USER}:{WAZUH_INDEXER_PASS}".encode()
        ).decode()

        query = json.dumps({
            "size": 20,
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": {
                "bool": {
                    "must": [
                        {"range": {"rule.level": {"gte": WAZUH_ALERT_LEVEL_THRESHOLD}}},
                        {"range": {"timestamp": {"gte": "now-5m"}}},
                    ]
                }
            }
        }).encode()

        req = urllib.request.Request(
            f"{WAZUH_INDEXER_URL}/wazuh-alerts-*/_search",
            data=query,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        data = json.loads(resp.read())

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            return

        new_alerts = []
        for hit in hits:
            src = hit.get("_source", {})
            alert_id = hit.get("_id", "")
            if alert_id == _wazuh_last_alert_id:
                break
            rule = src.get("rule", {})
            agent = src.get("agent", {})
            new_alerts.append({
                "level": rule.get("level", 0),
                "desc": rule.get("description", "unknown"),
                "agent": agent.get("name", "unknown"),
                "groups": rule.get("groups", []),
            })

        if hits:
            _wazuh_last_alert_id = hits[0].get("_id", "")

        if not new_alerts:
            return

        for alert in new_alerts[:5]:
            level = alert["level"]
            desc = alert["desc"]
            agent_name = alert["agent"]
            groups = ", ".join(alert["groups"][:3])
            issues.append(f"Wazuh L{level} [{agent_name}]: {desc}")
            _record_event(
                "critical" if level >= 12 else "warning",
                f"Wazuh alert L{level} on {agent_name}: {desc}",
                f"Groups: {groups}. Check Wazuh dashboard: https://192.168.1.7",
                "Wazuh",
            )

        if len(new_alerts) > 5:
            issues.append(f"Wazuh: {len(new_alerts) - 5} more high-severity alerts (check dashboard)")

        log(f"[wazuh] {len(new_alerts)} high-severity alert(s) in last 5m",
            level=LOG_WARN, source="big-brother")

    except Exception as e:
        log(f"[wazuh] Poll failed (non-fatal): {e}", level=LOG_INFO, source="big-brother")


def _canary_check() -> str | None:
    """Ask a local model if anything looks wrong that rules might miss.

    Returns a concern string if something is off, None otherwise.
    Only runs every CANARY_INTERVAL seconds. Degrades gracefully on failure.
    """
    global _last_canary

    now = time.time()
    if now - _last_canary < CANARY_INTERVAL:
        return None
    _last_canary = now

    summary = _build_metrics_summary()

    prompt = (
        "You are a systems watchdog. Review these metrics and report ONLY if something "
        "is genuinely concerning (not normal fluctuations). Be extremely terse - one "
        "sentence max. If everything looks fine, respond with just \"OK\".\n\n"
        f"{summary}"
    )

    try:
        payload = json.dumps({
            "model": "deepseek-r1:8b",
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 100},
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        result = data.get("response", "").strip()
        # Strip <think>...</think> blocks from reasoning models
        result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()
        if result and result.upper() != "OK" and len(result) > 5:
            log(f"[canary] Concern detected: {result[:200]}", level=LOG_WARN, source="big-brother")
            return result
        else:
            log("[canary] All clear", level=LOG_DEBUG, source="big-brother")
    except Exception as e:
        log(f"[canary] Check failed (degrading gracefully): {e}", level=LOG_INFO, source="big-brother")
    return None


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

# ── GPU Contention Monitoring ────────────────────────────────────────────────
# Metal GPU deadlocks occur when multiple GPU-heavy processes compete.
# ollama runner + mlx_whisper + mlx_lm.server = Metal contention storm.
# mlx_lm.server is always-on (the librarian) and excluded from kill targets.
GPU_CONTENTION_THRESHOLD = 2         # N+ GPU procs in U/UN state = contention
GPU_CONTENTION_DURATION = 60         # seconds in contention before acting
GPU_KILL_COOLDOWN = 300              # 5 min between kills
OLLAMA_LATENCY_TIMEOUT = 90          # seconds — 30B model cold-load can take 45-60s on Metal

_gpu_contention_first_seen: float = 0.0   # timestamp when contention first detected (0 = not in contention)
_gpu_last_kill: float = 0.0               # timestamp of last whisper kill action
_gpu_contention_status: dict = {
    "contention_active": False,
    "contention_since": None,
    "procs_stuck": 0,
    "last_kill_ts": None,
    "kills_total": 0,
    "ollama_latency_ms": None,
    "ollama_hung": False,
}

# ── Scheduler Task Auto-Remediation ──────────────────────────────────────────
# Tracks which tasks have already been auto-fixed this daemon lifetime to avoid loops.
SCHEDULER_YAML = Path.home() / ".openclaw/config/scheduler.yaml"
TIMEOUT_AUTOTUNE_MULTIPLIER = 1.5   # Multiply observed max duration by this
TIMEOUT_AUTOTUNE_MIN_FAILURES = 3   # Require N consecutive timeout failures before tuning
TIMEOUT_AUTOTUNE_COOLDOWN = 86400   # Don't re-tune same task within 24h
IMAGE_BACKEND_RESTART_COOLDOWN = 600  # 10 min between image backend restarts
CODE_BUG_ESCALATION_COOLDOWN = 3600   # Don't re-escalate same script within 1h

_timeout_autotune_last: dict = {}       # task_id -> last_autotune_timestamp
_image_backend_last_restart: float = 0.0
_code_bug_escalation_last: dict = {}    # script_path -> last_escalation_timestamp

# Error patterns that indicate code-level bugs (not infra issues)
_CODE_BUG_PATTERNS = [
    "NameError:",
    "ImportError:",
    "ModuleNotFoundError:",
    "SyntaxError:",
    "AttributeError:",
    "TypeError: ",  # trailing space to avoid matching "TimeoutError"
    "IndentationError:",
]

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
    """Post to all channels. In digest mode, buffers non-critical alerts for hourly summary."""
    global _last_digest_post

    if DIGEST_MODE and not is_critical:
        with _lock:
            _digest_buffer.append((time.time(), message))
        return

    # Immediate post (critical alerts bypass digest, or digest mode off)
    _notify_immediate(message, is_critical)


def _notify_immediate(message: str, is_critical: bool = False):
    """Post to all channels immediately. Falls back to raw HTTP + signal-cli if gateway is dead."""
    # Local macOS notification — always fires regardless of Slack/Discord
    clean = message.replace(":rotating_light:", "").replace(":wrench:", "").replace(":x:", "").replace("*", "").strip()
    nova_config.notify_local("Nova — Big Brother", clean[:200], critical=is_critical)

    try:
        nova_config.post_both(message, slack_channel=nova_config.SLACK_BB)
        return
    except Exception as e:
        log(f"Primary notify failed: {e}", level=LOG_WARN, source="big-brother")

    token = nova_config.slack_bot_token()
    if token:
        try:
            data = json.dumps({
                "channel": nova_config.SLACK_BB,
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

    if is_critical:
        try:
            subprocess.run(
                ["/opt/homebrew/bin/signal-cli", "--account", nova_config.NOVA_SIGNAL,
                 "send", "-m", message[:1000], "-r", nova_config.JORDAN_SIGNAL],
                capture_output=True, timeout=15,
            )
        except Exception as e:
            log(f"Signal fallback failed: {e}", level=LOG_ERROR, source="big-brother")


def _flush_digest():
    """Post buffered alerts as one hourly summary."""
    global _last_digest_post
    now = time.time()
    if now - _last_digest_post < DIGEST_INTERVAL:
        return
    _last_digest_post = now

    with _lock:
        if not _digest_buffer:
            return
        events = _digest_buffer.copy()
        _digest_buffer.clear()

    # Deduplicate and count
    issue_counts: dict = {}
    resolved = 0
    for ts, msg in events:
        if "RESOLVED" in msg:
            resolved += 1
            continue
        key = msg[:80]
        if key not in issue_counts:
            issue_counts[key] = {"msg": msg, "count": 1, "first": ts, "last": ts}
        else:
            issue_counts[key]["count"] += 1
            issue_counts[key]["last"] = ts

    if not issue_counts and resolved == 0:
        return

    lines = [":robot_face: *Big Brother Hourly Digest*"]
    if issue_counts:
        lines.append(f"  :red_circle: {len(issue_counts)} issues ({sum(v['count'] for v in issue_counts.values())} events)")
        for info in sorted(issue_counts.values(), key=lambda x: -x["count"])[:10]:
            count_str = f" (x{info['count']})" if info["count"] > 1 else ""
            lines.append(f"    • {info['msg'][:100]}{count_str}")
    if resolved:
        lines.append(f"  :white_check_mark: {resolved} auto-resolved")
    if not issue_counts:
        lines.append("  :white_check_mark: All clear — nothing unresolved")

    _notify_immediate("\n".join(lines))


def _maybe_notify(issue_key: str, message: str, is_critical: bool = False,
                  cooldown: int = 1800):
    """Rate-limited notification. Fires on first occurrence; suppresses repeats
    for `cooldown` seconds (default 30 min) regardless of time of day.

    This replaces the old quiet-hours-only suppression which let alerts fire
    every sweep during waking hours.
    """
    now = time.time()
    with _lock:
        last = _issue_last_alerted.get(issue_key, 0)
        if now - last < cooldown:
            return   # still in cooldown — suppress
        _issue_last_alerted[issue_key] = now
        _alerted_issues.add(issue_key)

    _notify(message, is_critical=is_critical)


# ── Redis notification to Claude Code ────────────────────────────────────────

def _redis_notify_claude(event_type: str, content: str, priority: int = 3):
    """Publish a notification to Redis nova:to_claude channel.

    Fire-and-forget — never crashes Big Brother if Redis is unavailable.
    Used for real-time notification when Claude Code has an active subscriber.
    """
    try:
        import redis
        r = redis.from_url("redis://127.0.0.1:6379", decode_responses=True)
        r.publish("nova:to_claude", json.dumps({
            "type": event_type,
            "source": "big-brother",
            "content": content[:500],
            "priority": priority,
            "ts": time.time(),
        }))
    except Exception:
        pass  # Fire-and-forget — Redis down is not a BB problem


# ── Claude Queue Escalation ──────────────────────────────────────────────────

def _escalate_to_claude(issue_description: str, priority: int = 3, service_name: str = ""):
    """Triage and escalate an incident to claude_queue for Claude Code attention.

    Called when Big Brother encounters issues it cannot auto-fix, or issues
    that persist after repeated heal attempts. The next Claude Code session
    will pick up the queued item and investigate.

    Uses nova_incident_triage to gather context (log tails, scheduler history,
    related service status, heal attempts) before writing to the queue.
    Falls back to direct queue insert if triage module fails.

    Also publishes to Redis nova:to_claude for real-time notification if
    Claude Code happens to be active with a subscriber.
    """
    # Try the triage module first — provides rich context for Claude
    try:
        from nova_incident_triage import triage_incident
        # Infer service name from description if not provided
        svc = service_name
        if not svc:
            # Best-effort extraction from common BB description patterns
            for known_svc in ("PostgreSQL", "Ollama", "Gateway v2", "Memory Server",
                              "Scheduler", "Redis", "Signal-cli", "MLX Server",
                              "SwarmUI", "ComfyUI", "TinyChat", "OpenWebUI",
                              "NovaControl", "PgBouncer", "SearXNG", "Slack"):
                if known_svc.lower() in issue_description.lower():
                    svc = known_svc
                    break
            if not svc:
                svc = "Unknown"
        triage_incident(svc, issue_description, priority=priority)
        log(f"[escalate] Triaged and queued for Claude Code: {issue_description[:100]}",
            level=LOG_WARN, source="big-brother")
    except Exception as e:
        # Fallback: direct insert if triage module fails
        log(f"[escalate] Triage failed ({e}), falling back to direct queue insert",
            level=LOG_WARN, source="big-brother")
        try:
            import psycopg2
            conn = psycopg2.connect("postgresql://kochj@127.0.0.1:5432/nova_ops")
            cur = conn.cursor()
            # Deduplication check
            cur.execute(
                "SELECT 1 FROM claude_queue WHERE description = %s AND status IN ('queued', 'in_progress')",
                (issue_description,)
            )
            if cur.fetchone():
                conn.close()
                return  # already escalated
            cur.execute(
                "INSERT INTO claude_queue (session_id, status, description, priority, created_at) "
                "VALUES ('claude-bridge-persistent', 'queued', %s, %s, now())",
                (f"INCIDENT: Unknown — {issue_description}", priority)
            )
            conn.commit()
            conn.close()
            log(f"[escalate] Queued for Claude Code (fallback): {issue_description[:100]}",
                level=LOG_WARN, source="big-brother")
        except Exception as e2:
            # Don't crash BB if PG is down — this is best-effort
            log(f"[escalate] Failed to insert into claude_queue: {e2}",
                level=LOG_WARN, source="big-brother")

    # Publish to Redis for real-time notification (fire-and-forget)
    _redis_notify_claude("escalation", issue_description, priority)


# ── Protected Task Check ──────────────────────────────────────────────────────

def _is_protected_task_running() -> bool:
    """Check Scheduler API for a currently-running protected task."""
    try:
        resp = urllib.request.urlopen(f"http://{LAN_IP}:37460/status", timeout=5)
        data = json.loads(resp.read())
        if data.get("tasks_running", 0) == 0:
            return False
        # Get running task names if possible
        try:
            tresp = urllib.request.urlopen(f"http://{LAN_IP}:37460/tasks", timeout=5)
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


# ── Claude Code conflict avoidance ───────────────────────────────────────────

# Map service names to their primary script files so we can check edit locks.
SERVICE_SCRIPT_MAP = {
    "Gateway v2":    str(SCRIPTS / "nova_gateway_v2.py"),
    "Scheduler":     str(SCRIPTS / "nova_scheduler.py"),
    "Memory Server": str(SCRIPTS / "nova_memory_server.py"),
    "Big Brother":   str(SCRIPTS / "nova_big_brother.py"),
    "MLX Server":    str(SCRIPTS / "nova_mlx_server.py"),
}


def _is_file_being_edited(script_path: str) -> bool:
    """Check if Claude Code is currently editing this file via Redis lock."""
    try:
        import redis
        r = redis.from_url("redis://localhost:6379", decode_responses=True)
        return bool(r.exists(f"nova:editing:{script_path}"))
    except Exception:
        return False


def _is_service_being_edited(service_name: str) -> bool:
    """Check if the script associated with a service is being edited by Claude.

    Also checks all nova:editing:* keys against the service's script path
    as a fallback in case the exact mapping isn't defined.
    """
    # Direct mapping check
    script_path = SERVICE_SCRIPT_MAP.get(service_name)
    if script_path and _is_file_being_edited(script_path):
        return True

    # Broad check: see if ANY editing lock mentions this service's scripts
    try:
        import redis
        r = redis.from_url("redis://localhost:6379", decode_responses=True)
        editing_keys = r.keys("nova:editing:*")
        if not editing_keys:
            return False

        # Check if any locked file is in the scripts directory and relates to this service
        service_lower = service_name.lower().replace(" ", "_").replace("-", "_")
        for key in editing_keys:
            filepath = key.replace("nova:editing:", "", 1)
            if service_lower in filepath.lower().replace("-", "_"):
                return True
    except Exception:
        pass

    return False


def _notify_claude_editing_conflict(service_name: str):
    """Notify Claude via Redis that Big Brother needs to restart a service
    but is deferring because Claude is editing it."""
    try:
        import redis
        r = redis.from_url("redis://localhost:6379", decode_responses=True)
        r.publish("nova:to_claude", json.dumps({
            "type": "restart_deferred",
            "source": "big-brother",
            "content": (
                f"I need to restart {service_name} but you're editing the script. "
                f"Let me know when you're done."
            ),
            "service": service_name,
            "ts": time.time(),
        }))
    except Exception:
        pass


# ── Service Restart Logic ─────────────────────────────────────────────────────

def _do_restart(service_name: str) -> bool:
    """Restart a service. Returns True on success.

    Checks for Claude Code edit locks before restarting — if the associated
    script is being edited, defers the restart and notifies Claude.
    """
    # ── Conflict avoidance: check if Claude is editing this service's script
    if _is_service_being_edited(service_name):
        log(f"Deferring restart of {service_name} — Claude is editing the script",
            level=LOG_WARN, source="big-brother")
        _record_event("info", f"Restart deferred: {service_name}",
                      "Claude Code is editing the script", service_name)
        _notify_claude_editing_conflict(service_name)
        _queue_restart(service_name)
        return True  # Report as handled (deferred, not failed)

    entry = next((s for s in SERVICES if s[0] == service_name), None)
    if not entry:
        return False

    name, host, port, label, critical, health_path = entry

    # Gateway needs special handling due to macOS Tahoe launchd bug
    if name in ("Gateway v2", "Gateway"):
        # Restart gateway v2 (primary); fall back to OpenClaw restart if v2 label not found
        if name == "Gateway v2":
            uid = os.getuid()
            try:
                result = subprocess.run(
                    ["launchctl", "kickstart", "-k", f"gui/{uid}/net.digitalnoise.nova-gateway-v2"],
                    capture_output=True, timeout=15,
                )
                return result.returncode == 0
            except Exception:
                pass
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


def _check_internet() -> bool:
    """Quick DNS probe to verify internet connectivity. Returns True if internet is up."""
    import socket
    try:
        socket.setdefaulttimeout(3)
        socket.getaddrinfo("dns.google", 443)
        return True
    except Exception:
        return False


def _handle_internet_state(issues: list, fixes: list):
    """
    Detect internet outages and fire a single alert instead of per-restart
    channel-disconnect spam. Modifies issues/fixes in place.
    Updates the global _internet_down / _internet_down_alerted state.
    """
    global _internet_down, _internet_down_since, _internet_down_alerted

    internet_up = _check_internet()

    if not internet_up and not _internet_down:
        # Internet just went down
        _internet_down = True
        _internet_down_since = time.time()
        _internet_down_alerted = False
        log("Internet DOWN — will suppress channel-disconnect alerts",
            level=LOG_WARN, source="big-brother")

    if not internet_up and _internet_down and not _internet_down_alerted:
        # Fire one alert
        duration_s = int(time.time() - _internet_down_since)
        issues.append("Internet connection DOWN")
        _record_event("critical", "Internet connection DOWN",
                      "Waiting for WAN to recover", "Network")
        _internet_down_alerted = True
        log("Internet DOWN alert fired", level=LOG_WARN, source="big-brother")

    if internet_up and _internet_down:
        # Internet came back
        down_secs = int(time.time() - _internet_down_since)
        _internet_down = False
        _internet_down_alerted = False
        fixes.append(f"Internet restored after {down_secs // 60}m {down_secs % 60}s")
        _record_event("critical", "Internet DOWN",
                      f"Internet restored after {down_secs // 60}m {down_secs % 60}s",
                      "Network")
        log(f"Internet restored after {down_secs}s", level=LOG_INFO, source="big-brother")


def _check_crash_loop(service_name: str) -> bool:
    """Return True if this service is in a crash-loop cooldown (should NOT restart).

    Tracks restart timestamps in a 5-minute sliding window. If a service has been
    restarted 3+ times in that window (with healthy dependencies), it's declared a
    crash-loop and given a 10-minute cooldown to prevent Slack spam.
    """
    now = time.time()

    # Check if still in cooldown
    cooldown_until = _service_crash_loop_until.get(service_name, 0)
    if now < cooldown_until:
        remaining = int(cooldown_until - now)
        log(f"[crash-loop] {service_name} in cooldown for {remaining}s more — skipping restart",
            level=LOG_INFO, source="big-brother")
        return True  # in cooldown — do not restart

    # Track this restart attempt in the sliding window
    if service_name not in _service_restart_times:
        _service_restart_times[service_name] = deque()
    window = _service_restart_times[service_name]
    window.append(now)
    # Prune events older than the window
    while window and window[0] < now - _CRASH_LOOP_WINDOW:
        window.popleft()

    if len(window) >= _CRASH_LOOP_MAX:
        # Crash-loop detected
        _service_crash_loop_until[service_name] = now + _CRASH_LOOP_COOLDOWN
        window.clear()
        log(f"[crash-loop] {service_name} restarted {_CRASH_LOOP_MAX}x in {_CRASH_LOOP_WINDOW}s — "
            f"crash-loop detected, cooling down for {_CRASH_LOOP_COOLDOWN}s",
            level=LOG_ERROR, source="big-brother")
        _maybe_notify(
            f"crash_loop_{service_name}",
            f":rotating_light: *Crash-loop detected: {service_name}*\n"
            f"Restarted {_CRASH_LOOP_MAX}+ times in {_CRASH_LOOP_WINDOW//60} min despite healthy dependencies.\n"
            f"Auto-restart paused for {_CRASH_LOOP_COOLDOWN//60} min. Check logs for root cause.",
            is_critical=True,
            cooldown=_CRASH_LOOP_COOLDOWN,
        )
        # Escalate to Claude Code — BB cannot fix crash-loops
        _escalate_to_claude(
            f"{service_name} in crash-loop: restarted {_CRASH_LOOP_MAX}+ times in "
            f"{_CRASH_LOOP_WINDOW//60} min with healthy dependencies. "
            f"Check ~/.openclaw/logs/ for {service_name.lower().replace(' ', '-')} error logs. "
            f"Likely a code bug or config issue, not an infra problem.",
            priority=2
        )
        return True  # in cooldown — do not restart this time

    return False  # not in crash-loop — proceed with restart


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

    # Notify #nova-notifications about degraded mode BEFORE restarting
    # This uses raw Slack HTTP (no gateway dependency)
    _maybe_notify(
        "gateway-degraded-mode",
        "⚠️ Gateway restarting — Nova is in degraded mode for ~30 seconds. "
        "Channels remain connected, messages will be answered without memory context.",
        is_critical=False,
        cooldown=300,  # Don't spam — 5 min cooldown matches gateway restart cooldown
    )

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
            # Notify Claude that gateway was restarted (may affect active work)
            _redis_notify_claude(
                "service_restart",
                "Big Brother restarted Nova Gateway v2. "
                "Slack/Discord/Signal/Claude channels reconnecting. "
                "Session state in memory was reset.",
                priority=2,
            )
            return True
    return False


# ── Scheduler Task Auto-Remediation Functions ────────────────────────────────

def _autotune_task_timeout(task_id: str, current_timeout: int, issues: list, fixes: list,
                           error_tail: str = "") -> bool:
    """Bump a task's timeout in scheduler.yaml if it consistently times out.

    Reads recent runs from the scheduler API, checks if the task is timing out
    repeatedly, and increases the timeout to 1.5× the current value.
    Returns True if a fix was applied.
    """
    global _timeout_autotune_last
    now = time.time()

    # Cooldown check
    if task_id in _timeout_autotune_last:
        if now - _timeout_autotune_last[task_id] < TIMEOUT_AUTOTUNE_COOLDOWN:
            return False

    try:
        import yaml
        cfg_text = SCHEDULER_YAML.read_text()
        cfg = yaml.safe_load(cfg_text)
        tasks_cfg = cfg.get("tasks", cfg)
        task_cfg = tasks_cfg.get(task_id)
        if not task_cfg:
            return False

        old_timeout = task_cfg.get("timeout", 600)

        # Guard: if the error says "Timed out after Xs" but X < current config,
        # the config was already bumped (perhaps manually). Don't bump again.
        if error_tail:
            import re as _re_local
            m = _re_local.search(r"Timed out after (\d+)s", error_tail)
            if m:
                errored_timeout = int(m.group(1))
                if errored_timeout < old_timeout:
                    return False

        new_timeout = int(old_timeout * TIMEOUT_AUTOTUNE_MULTIPLIER)
        # Cap at 12 hours to prevent runaway growth
        new_timeout = min(new_timeout, 43200)

        if new_timeout <= old_timeout:
            return False

        # Rewrite the YAML line (preserve formatting by doing string replacement)
        old_line = f"    timeout: {old_timeout}"
        new_line = f"    timeout: {new_timeout}"
        if old_line not in cfg_text:
            return False

        # Find the right occurrence (after the task_id line)
        task_header = f"  {task_id}:"
        header_pos = cfg_text.find(task_header)
        if header_pos == -1:
            return False
        timeout_pos = cfg_text.find(old_line, header_pos)
        if timeout_pos == -1:
            return False
        # Make sure we're still within this task block (next task starts with "  <word>:")
        next_task_pos = cfg_text.find("\n  ", timeout_pos + len(old_line))
        if next_task_pos == -1:
            next_task_pos = len(cfg_text)

        cfg_text = cfg_text[:timeout_pos] + new_line + cfg_text[timeout_pos + len(old_line):]
        SCHEDULER_YAML.write_text(cfg_text)

        _timeout_autotune_last[task_id] = now
        fix_msg = f"Auto-tuned timeout for '{task_id}': {old_timeout}s → {new_timeout}s"
        fixes.append(fix_msg)
        _record_event("info", f"Task '{task_id}' timing out ({TIMEOUT_AUTOTUNE_MIN_FAILURES}+ consecutive)",
                      fix_msg, "Scheduler")
        log(f"[autotune] {fix_msg}", level=LOG_INFO, source="big-brother")
        nova_config.post_both(f":wrench: *Big Brother auto-fix:* {fix_msg}\nScheduler restart needed to apply.")

        # Trigger scheduler reload
        _reload_scheduler()
        return True

    except Exception as e:
        log(f"[autotune] Failed for {task_id}: {e}", level=LOG_WARN, source="big-brother")
        return False


def _restart_image_backend(task_id: str, issues: list, fixes: list) -> bool:
    """Restart SwarmUI/ComfyUI when a gpu_heavy task fails and the backend is unresponsive.

    Checks if the image generation backends are actually healthy before restarting.
    Returns True if a restart was triggered.
    """
    global _image_backend_last_restart
    now = time.time()

    if now - _image_backend_last_restart < IMAGE_BACKEND_RESTART_COOLDOWN:
        return False

    # Check if ComfyUI (primary image gen) is responsive
    comfyui_up = _port_open("127.0.0.1", 8188)
    swarmui_up = _port_open("127.0.0.1", 7801)

    if comfyui_up and swarmui_up:
        # Both backends are up — the failure is likely a model issue, not infra
        return False

    restarted = []
    uid = os.getuid()

    if not comfyui_up:
        try:
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.jordankoch.comfyui"],
                capture_output=True, timeout=15,
            )
            restarted.append("ComfyUI")
        except Exception:
            pass

    if not swarmui_up:
        try:
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.jordankoch.swarmui"],
                capture_output=True, timeout=15,
            )
            restarted.append("SwarmUI")
        except Exception:
            pass

    if restarted:
        _image_backend_last_restart = now
        fix_msg = f"Restarted image backend ({', '.join(restarted)}) after '{task_id}' failure"
        fixes.append(fix_msg)
        _record_event("info", f"Image backend down when gpu_heavy task '{task_id}' failed",
                      fix_msg, "SwarmUI")
        log(f"[image-heal] {fix_msg}", level=LOG_INFO, source="big-brother")
        nova_config.post_both(f":art: *Big Brother auto-fix:* {fix_msg}")
        return True

    return False


def _escalate_code_bug(task_id: str, script: str, error_tail: str, issues: list) -> bool:
    """Escalate code-level bugs (NameError, ImportError, etc.) to Claude Code queue.

    Parses the traceback to extract the file path and error, then queues a
    targeted fix request for Claude Code.
    Returns True if escalation was sent.
    """
    global _code_bug_escalation_last
    now = time.time()

    script_key = script or task_id
    if script_key in _code_bug_escalation_last:
        if now - _code_bug_escalation_last[script_key] < CODE_BUG_ESCALATION_COOLDOWN:
            return False

    # Extract the actual error type and message from the tail
    error_type = None
    for pattern in _CODE_BUG_PATTERNS:
        if pattern in error_tail:
            error_type = pattern.rstrip(": ")
            break

    if not error_type:
        return False

    # Build a targeted fix request
    script_path = str(Path.home() / f".openclaw/scripts/{script}") if script else "unknown"

    description = (
        f"CODE BUG in scheduler task '{task_id}' ({script}):\n"
        f"Error: {error_type}\n"
        f"Traceback tail:\n{error_tail[-500:]}\n\n"
        f"Script path: {script_path}\n"
        f"Fix the {error_type} in the script. Check imports, variable names, and function signatures."
    )

    _code_bug_escalation_last[script_key] = now
    _escalate_to_claude(description, priority=2, service_name="Scheduler")
    _record_event("warning", f"Code bug in '{task_id}': {error_type}",
                  "Escalated to Claude Code queue for auto-fix", "Scheduler")
    log(f"[code-bug] Escalated {error_type} in {script} to Claude Code",
        level=LOG_WARN, source="big-brother")
    nova_config.post_both(
        f":bug: *Big Brother detected code bug:* `{error_type}` in `{script}`\n"
        f"Escalated to Claude Code queue for auto-fix."
    )
    return True


def _reload_scheduler():
    """Restart the scheduler daemon to pick up config changes."""
    try:
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/com.nova.scheduler"],
            capture_output=True, timeout=15,
        )
        log("[autotune] Scheduler restarted to apply config changes",
            level=LOG_INFO, source="big-brother")
    except Exception as e:
        log(f"[autotune] Failed to restart scheduler: {e}",
            level=LOG_WARN, source="big-brother")


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


def _score_history_confirms_down(name: str, current_up: bool) -> bool:
    """Push current check result to sliding window. Returns True only if confirmed down."""
    if name not in _service_score_history:
        _service_score_history[name] = deque(maxlen=SCORE_HISTORY_LEN)
    _service_score_history[name].append(current_up)
    history = _service_score_history[name]
    if len(history) < 2:
        return not current_up
    fail_count = sum(1 for r in history if not r)
    return fail_count >= SCORE_FAIL_THRESHOLD


def _get_service_interval(name: str) -> int:
    """Get adaptive check interval for a service."""
    return _service_check_interval.get(name, SWEEP_INTERVAL)


def _should_check_now(name: str) -> bool:
    """Returns True if enough time has elapsed since last check for this service."""
    now = time.time()
    interval = _get_service_interval(name)
    last = _service_last_checked.get(name, 0)
    return (now - last) >= interval


def _update_adaptive_interval(name: str, is_up: bool):
    """Adjust service check interval based on health history."""
    now = time.time()
    _service_last_checked[name] = now

    if is_up:
        if name not in _service_healthy_since:
            _service_healthy_since[name] = now
        healthy_duration = now - _service_healthy_since[name]
        if healthy_duration >= HEALTHY_STRETCH_S:
            _service_check_interval[name] = RELAXED_INTERVAL
        else:
            _service_check_interval[name] = SWEEP_INTERVAL
    else:
        _service_healthy_since.pop(name, None)
        _service_check_interval[name] = HEIGHTENED_INTERVAL


def _heighten_correlated(name: str):
    """When a service fails, heighten check frequency for its dependencies."""
    try:
        from nova_incident_triage import SERVICE_DEPENDENCIES
        deps = SERVICE_DEPENDENCIES.get(name, [])
        for dep in deps:
            _service_check_interval[dep] = HEIGHTENED_INTERVAL
            _service_healthy_since.pop(dep, None)
    except ImportError:
        pass


# ── Slack Socket Mode Check ───────────────────────────────────────────────────

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

def _check_gateway_log_channels() -> dict:
    """Parse recent gateway log for channel state. Returns {slack, discord, signal}.

    Only considers log lines timestamped within GATEWAY_LOG_WINDOW_SECS seconds.
    This prevents stale 'websocket closed' lines from appearing as a current
    disconnect after the gateway has already successfully reconnected — which was
    causing an infinite restart loop (close line stays in log → BB restarts gateway
    → new close line written → repeat).
    """
    status = {"slack": "unknown", "discord": "unknown", "signal": "unknown"}

    log_file = LOG_DIR / "gateway.log"
    if not log_file.exists():
        return status

    try:
        lines = log_file.read_text(errors="replace").split("\n")[-400:]
    except Exception:
        return status

    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - GATEWAY_LOG_WINDOW_SECS

    # ISO timestamp pattern used by OpenClaw gateway: 2026-05-12T12:26:54.061-07:00
    _ts_re = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))')

    for line in lines:
        clean = _ANSI_RE.sub("", line)

        # Parse line timestamp — skip lines older than the window
        ts_m = _ts_re.match(clean)
        if ts_m:
            try:
                ts_str = ts_m.group(1)
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts.timestamp() < cutoff:
                    continue  # line is too old — ignore
            except Exception:
                pass  # unparseable timestamp — include the line (safe fallback)

        lower = clean.lower()

        if "slack" in lower:
            if "socket mode connected" in lower:
                status["slack"] = "connected"
            elif "socket disconnected" in lower or "socket mode disconnected" in lower:
                status["slack"] = "disconnected"

        if "discord" in lower:
            if "channels resolved" in lower or "discord ready" in lower or "discord client initialized" in lower:
                status["discord"] = "connected"
            elif "gateway websocket closed" in lower or "enotfound" in lower:
                status["discord"] = "disconnected"
            elif "fetch timeout" in lower and "discord.com" in lower:
                status["discord"] = "timeout"

        if "signal" in lower:
            if "started http server" in lower or "config file lock acquired" in lower:
                status["signal"] = "connected"
            elif "config file is in use" in lower:
                status["signal"] = "disconnected"
            elif "daemon exited" in lower and "code=0" in lower:
                pass  # clean exit — OpenClaw respawns, not our problem
            elif "connection closed unexpectedly" in lower:
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
        r = redis.from_url("redis://127.0.0.1:6379", decode_responses=True)
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
        ("/Volumes/external", "External media volume"),
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

    Response shape: {"memories": [...], "query": "...", "count": N}
    A valid response may have zero results — count=0 still means PG is reachable.
    """
    # Skip recall test when HNSW reindex is running — queries are slow by design
    if _hnsw_reindex_running():
        log("HNSW reindex active — skipping recall check", level=LOG_DEBUG, source="big-brother")
        return True

    for attempt in range(3):
        try:
            url = f"http://{LAN_IP}:18790/recall?q=health_check_probe&n=1"
            resp = urllib.request.urlopen(url, timeout=30)
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            data = json.loads(resp.read())
            # Accept both old list format and current dict format
            if isinstance(data, list):
                return True
            if isinstance(data, dict) and "memories" in data:
                return True
            # Unexpected format but server responded — treat as healthy
            log(f"Recall check: unexpected response shape {type(data).__name__} — "
                f"treating as healthy to avoid false alarm", level=LOG_WARN, source="big-brother")
            return True
        except urllib.error.HTTPError as e:
            # 500 = PG/query error; 503 = memory server overloaded
            log(f"Recall check HTTP {e.code} (attempt {attempt+1}/3)", level=LOG_WARN, source="big-brother")
            if attempt < 2:
                time.sleep(5)
        except Exception as e:
            log(f"Recall check failed (attempt {attempt+1}/3): {e}", level=LOG_WARN, source="big-brother")
            if attempt < 2:
                time.sleep(5)
    return False


def _check_redis_memory_cache() -> bool:
    """Ensure Redis is actually storing/retrieving keys (not just pinging)."""
    try:
        import redis
        r = redis.from_url("redis://127.0.0.1:6379")
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
# Maps section → (scheduler_task_id, stale_threshold_hours)
# threshold: how many hours before we declare it stale and trigger a backfill
# For scheduled content (MWF etc), we also check if today is a scheduled day
# and the task hasn't run yet — see _is_overdue_today()
JOURNAL_SECTIONS = {
    "dreams":     ("daily_journal",  26),
    "essays":     ("journal_essay",  26),
    "opinions":   ("daily_opinion",  26),
    "after-dark": ("after_dark",     26),
    "tech-today": ("tech_today",     26),
    "research":   ("research_paper", 50),
    "digests":    ("daily_digest",   26),
}

# Tasks with non-daily schedules: (task_id -> days_of_week as isoweekday 1=Mon)
JOURNAL_SCHEDULE_DAYS = {
    "journal_essay": {1, 3, 5},  # Mon, Wed, Fri
}
JOURNAL_OVERDUE_AFTER_HOUR = 10  # If it's a scheduled day and past 10am, it's overdue
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


def _is_overdue_today(task_id: str) -> bool:
    """Check if a task was supposed to run today but hasn't yet."""
    schedule_days = JOURNAL_SCHEDULE_DAYS.get(task_id)
    if not schedule_days:
        return False
    now_dt = datetime.now()
    if now_dt.isoweekday() not in schedule_days:
        return False
    if now_dt.hour < JOURNAL_OVERDUE_AFTER_HOUR:
        return False
    return True


def _check_journal_staleness(issues: list, fixes: list):
    """
    Check every journal section. If the latest entry is older than its threshold,
    OR if it's a scheduled day and the task is overdue (past 10am, hasn't run),
    trigger a backfill by running the responsible scheduler task via /run/ endpoint.
    Includes a 2-hour per-section cooldown so we don't spam.
    Called from every _full_sweep().
    """
    now = time.time()
    scheduler_up = _port_open(LAN_IP,      37460)

    for section, (task_id, threshold_h) in JOURNAL_SECTIONS.items():
        age_h = _latest_journal_entry_age(section)
        if age_h is None:
            continue

        overdue_today = _is_overdue_today(task_id)
        if age_h < threshold_h and not overdue_today:
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
                    f"http://{LAN_IP}:37460/run/{task_id}",
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


# ── GPU Contention Detection ─────────────────────────────────────────────────

def _get_gpu_stuck_processes() -> list:
    """Find GPU-heavy processes in uninterruptible sleep (state U/UN).

    Returns list of dicts: {pid, user, state, command, age_s}
    Excludes mlx_lm.server which is always-on and allowed.
    """
    GPU_PATTERNS = ['mlx_whisper', 'ollama runner']
    stuck = []
    try:
        result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            # Skip mlx_lm.server — it's the always-on librarian, not a contention source
            if 'mlx_lm.server' in line or 'mlx_lm/server' in line:
                continue
            if not any(p in line for p in GPU_PATTERNS):
                continue
            parts = line.split()
            if len(parts) < 8:
                continue
            state = parts[7]  # STAT column in ps aux
            if 'U' in state:
                stuck.append({
                    'pid': int(parts[1]),
                    'user': parts[0],
                    'state': state,
                    'command': ' '.join(parts[10:])[:120],
                })
    except Exception as e:
        log(f"[gpu] ps aux failed: {e}", level=LOG_WARN, source="big-brother")
    return stuck


def _check_ollama_latency() -> float | None:
    """Ping Ollama with a 1-token generate to measure GPU responsiveness.

    Returns latency in milliseconds, or None if Ollama is unreachable.
    If latency exceeds OLLAMA_LATENCY_TIMEOUT, the GPU is likely hung.
    """
    if not _port_open("127.0.0.1", 11434):
        return None

    payload = json.dumps({
        "model": "qwen3:30b-a3b",
        "prompt": "hi",
        "stream": False,
        "options": {"num_predict": 1},
    }).encode()

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        start = time.time()
        resp = urllib.request.urlopen(req, timeout=OLLAMA_LATENCY_TIMEOUT + 5)
        elapsed_ms = (time.time() - start) * 1000
        resp.read()  # consume body
        return elapsed_ms
    except Exception as e:
        # Timeout or connection error = GPU is hung or Ollama crashed
        elapsed_ms = (time.time() - start) * 1000 if 'start' in dir() else None
        log(f"[gpu] Ollama latency probe failed after {elapsed_ms:.0f}ms: {e}" if elapsed_ms
            else f"[gpu] Ollama latency probe failed: {e}",
            level=LOG_WARN, source="big-brother")
        return float('inf')  # Treat timeout as infinite latency


def _classify_gpu_health() -> tuple:
    """Classify GPU/Ollama health state using HealthState classification.

    Returns (state: str, context: dict) where context contains diagnostic info.
    Classification logic:
      - HEALTHY: Ollama responding normally (<3s latency)
      - SLOW: Ollama responding but latency 1-3s (leave alone, just log)
      - STUCK: Process alive but no progress for extended period (nudge)
      - CRASHED: Ollama port dead (respawn)
      - CONTENDED: Resource contention from another process (find and kill culprit)
    """
    context = {
        "ollama_latency_ms": None,
        "ollama_port_up": False,
        "stuck_procs": [],
        "gpu_hog_pids": [],
    }

    # Check if Ollama is even running
    if not _port_open("127.0.0.1", 11434):
        return (HealthState.CRASHED, context)

    context["ollama_port_up"] = True

    # Measure Ollama latency
    latency = _check_ollama_latency()
    context["ollama_latency_ms"] = round(latency) if latency and latency != float('inf') else None

    # Check for stuck GPU processes
    stuck_procs = _get_gpu_stuck_processes()
    context["stuck_procs"] = stuck_procs

    # If Ollama can't respond at all (timeout/inf), determine why
    if latency is not None and latency == float('inf'):
        # Ollama port is up but inference timed out — is it contention or stuck?
        # Look for GPU hogs: processes using >50% CPU that aren't Ollama itself
        gpu_hogs = _find_gpu_hogs()
        context["gpu_hog_pids"] = gpu_hogs
        if gpu_hogs or len(stuck_procs) >= GPU_CONTENTION_THRESHOLD:
            return (HealthState.CONTENDED, context)
        else:
            return (HealthState.STUCK, context)

    # Ollama responded — check latency thresholds
    if latency is not None:
        if latency > (OLLAMA_LATENCY_TIMEOUT * 1000):
            # Over hard timeout — contention or stuck
            gpu_hogs = _find_gpu_hogs()
            context["gpu_hog_pids"] = gpu_hogs
            if gpu_hogs or len(stuck_procs) >= GPU_CONTENTION_THRESHOLD:
                return (HealthState.CONTENDED, context)
            return (HealthState.STUCK, context)
        elif latency > 3000:
            # 3-30s: slow but responding — just log, don't act
            return (HealthState.SLOW, context)

    return (HealthState.HEALTHY, context)


def _find_gpu_hogs() -> list:
    """Find processes hogging GPU/CPU that aren't Ollama or mlx_lm.server.

    Returns list of dicts: {pid, cpu_pct, command}
    Looks for Metal/GPU-heavy processes using >50% CPU.
    """
    hogs = []
    try:
        result = subprocess.run(
            ['ps', 'aux', '--sort=-%cpu'] if sys.platform != 'darwin'
            else ['ps', '-eo', 'pid,%cpu,command', '-r'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines()[1:20]:  # Top 20 by CPU
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                cpu_pct = float(parts[1])
            except (ValueError, IndexError):
                continue
            command = parts[2] if len(parts) > 2 else ""

            # Skip Ollama itself, mlx_lm.server (librarian), and system processes
            if any(skip in command for skip in ['ollama', 'mlx_lm.server', 'mlx_lm/server',
                                                  'kernel_task', 'WindowServer', 'big_brother']):
                continue

            # Look for GPU-heavy candidates: mlx_whisper, Metal processes, high CPU
            is_gpu_candidate = any(p in command for p in ['mlx_whisper', 'metal', 'gpu',
                                                           'comfyui', 'swarmui', 'stable-diffusion'])
            if cpu_pct > 50.0 and is_gpu_candidate:
                hogs.append({"pid": pid, "cpu_pct": cpu_pct, "command": command[:120]})
    except Exception as e:
        log(f"[gpu] Failed to find GPU hogs: {e}", level=LOG_WARN, source="big-brother")
    return hogs


def _check_gpu_contention(issues: list, fixes: list):
    """Detect Metal GPU contention using HealthState classification and targeted remediation.

    Called every sweep from _full_sweep(). Uses classified health state:
      - HEALTHY/SLOW: No action needed
      - CRASHED: Restart Ollama
      - CONTENDED: Find the actual GPU hog and kill IT (not blindly kill whisper)
      - STUCK: Nudge Ollama (unload/reload model)
    """
    global _gpu_contention_first_seen, _gpu_last_kill, _gpu_contention_status

    now = time.time()

    # ── Classify GPU health state ──
    state, ctx = _classify_gpu_health()

    ollama_latency = ctx["ollama_latency_ms"]
    stuck_procs = ctx["stuck_procs"]
    num_stuck = len(stuck_procs)

    # Update status for diagnostics API
    _gpu_contention_status.update({
        "contention_active": state in (HealthState.CONTENDED, HealthState.STUCK),
        "health_state": state,
        "procs_stuck": num_stuck,
        "ollama_latency_ms": ollama_latency,
        "ollama_hung": state in (HealthState.CONTENDED, HealthState.STUCK, HealthState.CRASHED),
        "gpu_hogs": ctx["gpu_hog_pids"],
    })

    # ── HEALTHY or SLOW: no action needed ──
    if state == HealthState.HEALTHY:
        if _gpu_contention_first_seen > 0:
            log("[gpu] Contention cleared — GPU healthy", level=LOG_INFO, source="big-brother")
            _resolve_escalation("gpu_contention")
        _gpu_contention_first_seen = 0.0
        _gpu_contention_status["contention_since"] = None
        return

    if state == HealthState.SLOW:
        log(f"[gpu] Ollama slow ({ollama_latency}ms) but responding — leaving alone",
            level=LOG_INFO, source="big-brother")
        if _gpu_contention_first_seen > 0:
            # Was in contention, now just slow — clear timer
            _gpu_contention_first_seen = 0.0
            _gpu_contention_status["contention_since"] = None
            _resolve_escalation("gpu_contention")
        return

    # ── CRASHED: Ollama port is dead — restart it ──
    if state == HealthState.CRASHED:
        issues.append("Ollama CRASHED (port 11434 not responding)")
        _record_event("critical", "Ollama CRASHED — port dead",
                      "Will be restarted by Ollama auto-restart block", "GPU")
        _gpu_contention_first_seen = 0.0
        # Don't handle restart here — the main service check loop handles Ollama restart
        return

    # ── CONTENDED or STUCK: need timing and action ──

    # Start or continue contention timer
    if _gpu_contention_first_seen == 0.0:
        _gpu_contention_first_seen = now
        _gpu_contention_status["contention_since"] = _now_iso()
        log(f"[gpu] {state} detected: {num_stuck} stuck procs, latency={ollama_latency}ms, "
            f"hogs={len(ctx['gpu_hog_pids'])}",
            level=LOG_WARN, source="big-brother")
        return  # Wait for duration threshold

    contention_duration = now - _gpu_contention_first_seen
    if contention_duration < GPU_CONTENTION_DURATION:
        log(f"[gpu] {state} ongoing ({contention_duration:.0f}s / {GPU_CONTENTION_DURATION}s threshold)",
            level=LOG_INFO, source="big-brother")
        return

    # ── Duration threshold exceeded — take action ──

    # Check kill cooldown
    if now - _gpu_last_kill < GPU_KILL_COOLDOWN:
        remaining = int(GPU_KILL_COOLDOWN - (now - _gpu_last_kill))
        log(f"[gpu] {state} active but kill on cooldown ({remaining}s remaining)",
            level=LOG_INFO, source="big-brother")
        issues.append(f"GPU {state} ({num_stuck} stuck procs) — kill cooldown {remaining}s")
        return

    # ── CONTENDED: Find the actual culprit and kill it ──
    if state == HealthState.CONTENDED:
        gpu_hogs = ctx["gpu_hog_pids"]
        killed = 0

        if gpu_hogs:
            # Kill the identified GPU hogs (the CULPRIT, not the victim)
            for hog in gpu_hogs:
                try:
                    os.kill(hog["pid"], signal.SIGKILL)
                    killed += 1
                    log(f"[gpu] Killed GPU hog PID {hog['pid']} ({hog['command'][:60]})",
                        level=LOG_WARN, source="big-brother")
                except ProcessLookupError:
                    pass
                except Exception as e:
                    log(f"[gpu] Failed to kill hog PID {hog['pid']}: {e}",
                        level=LOG_ERROR, source="big-brother")
        else:
            # No specific hog found — fall back to killing whisper processes
            whisper_pids = []
            try:
                result = subprocess.run(['pgrep', '-f', 'mlx_whisper'],
                                        capture_output=True, text=True, timeout=5)
                whisper_pids = [int(p) for p in result.stdout.strip().split() if p]
            except Exception:
                pass

            if whisper_pids:
                whisper_pids.sort(reverse=True)
                for pid in whisper_pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        killed += 1
                        log(f"[gpu] Killed mlx_whisper PID {pid} (fallback — no specific hog found)",
                            level=LOG_WARN, source="big-brother")
                    except ProcessLookupError:
                        pass
                    except Exception as e:
                        log(f"[gpu] Failed to kill PID {pid}: {e}",
                            level=LOG_ERROR, source="big-brother")

        if killed > 0:
            _gpu_last_kill = now
            _gpu_contention_first_seen = 0.0
            _gpu_contention_status["last_kill_ts"] = _now_iso()
            _gpu_contention_status["kills_total"] = _gpu_contention_status.get("kills_total", 0) + killed

            hog_desc = ", ".join(f"PID {h['pid']} ({h['command'][:40]})" for h in gpu_hogs) if gpu_hogs else "mlx_whisper (fallback)"
            fix_msg = f"Killed {killed} GPU hog(s): {hog_desc}"
            fixes.append(fix_msg)
            _record_event("warning",
                          f"GPU CONTENDED: {num_stuck} stuck procs, latency={ollama_latency}ms",
                          fix_msg, "GPU")

            send, suffix = should_notify("gpu_contention", "warning")
            if send:
                _notify(
                    f":warning: *GPU contention resolved* — killed {killed} process(es) "
                    f"hogging Metal GPU.\n"
                    f"Culprit: {hog_desc}\n"
                    f"Contention lasted {contention_duration:.0f}s{suffix}",
                )
        else:
            # Nothing to kill — escalate
            issues.append("GPU CONTENDED but no killable process found")
            _record_event("warning", "GPU CONTENDED — no killable hog found",
                          "Escalating to Claude Code", "GPU")
            send, suffix = should_notify("gpu_contention", "critical")
            if send:
                _notify(
                    f":warning: *GPU contended* — Ollama inference timed out but no GPU hog "
                    f"process found to kill.\nMetal may be deadlocked.{suffix}",
                    is_critical=True,
                )
            _escalate_to_claude(
                "GPU contention detected but no killable process found. "
                "Ollama inference is timing out. May need Ollama restart or Metal reset. "
                "Check `ps -eo pid,%cpu,command -r | head -20` and Ollama logs.",
                priority=2
            )
            _gpu_last_kill = now  # Prevent spam

    # ── STUCK: Ollama alive but frozen — try to nudge it ──
    elif state == HealthState.STUCK:
        issues.append(f"GPU STUCK — Ollama alive but not making progress ({contention_duration:.0f}s)")
        _record_event("warning", "GPU STUCK — Ollama frozen",
                      "Attempting Ollama model unload to free Metal", "GPU")

        # Try to unload all models to reset Metal state
        try:
            # List loaded models and unload them
            resp = urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=5)
            ps_data = json.loads(resp.read())
            for model in ps_data.get("models", []):
                model_name = model.get("name", "")
                if model_name:
                    unload_payload = json.dumps({"model": model_name, "keep_alive": 0}).encode()
                    req = urllib.request.Request(
                        "http://127.0.0.1:11434/api/generate",
                        data=unload_payload,
                        headers={"Content-Type": "application/json"},
                    )
                    urllib.request.urlopen(req, timeout=10)
                    log(f"[gpu] Unloaded model {model_name} to free Metal",
                        level=LOG_INFO, source="big-brother")
            fixes.append("Unloaded Ollama models to reset stuck Metal GPU")
            _gpu_last_kill = now
            _gpu_contention_first_seen = 0.0
        except Exception as e:
            log(f"[gpu] Failed to unload models: {e}", level=LOG_WARN, source="big-brother")
            # If unload fails too, escalate
            send, suffix = should_notify("gpu_contention", "critical")
            if send:
                _notify(
                    f":warning: *GPU stuck* — Ollama frozen and model unload failed.\n"
                    f"May need manual `ollama stop` or process kill.{suffix}",
                    is_critical=True,
                )
            _escalate_to_claude(
                "Ollama GPU stuck — inference frozen and model unload attempt failed. "
                "Port 11434 is up but generate requests time out. "
                "Try: pkill ollama && open -a Ollama. Check Metal driver state.",
                priority=2
            )
            _gpu_last_kill = now


# ── Full Health Sweep ─────────────────────────────────────────────────────────

def _is_maintenance_mode() -> bool:
    """Check Redis global maintenance flag set by pg_maintain / manual ops."""
    try:
        import redis
        r = redis.from_url("redis://127.0.0.1:6379", decode_responses=True)
        return bool(r.get("nova:maintenance:active"))
    except Exception:
        return False


def _is_service_in_maintenance(service_name: str) -> bool:
    """Check per-service maintenance flag: nova:maintenance:service:<name>.

    Set via /bb/maintenance API or manually:
      redis-cli SET nova:maintenance:service:"Memory Server" 1 EX 3600
    Cleared automatically on TTL expiry or via /bb/maintenance/clear.
    """
    try:
        import redis
        r = redis.from_url("redis://127.0.0.1:6379", decode_responses=True)
        safe_name = service_name.replace(" ", "_").lower()
        return bool(r.get(f"nova:maintenance:service:{safe_name}"))
    except Exception:
        return False


_ALLOWED_MODELS = {
    "ollama/qwen3-next:80b",      # original primary — removed from Ollama, will be restored
    "ollama/qwen3:30b-a3b",       # interim replacement while qwen3-next:80b unavailable
    "ollama/nova:latest",
    "ollama/qwen3-coder:30b",
    "ollama/deepseek-r1:8b",
    "ollama/qwen3-vl:4b",
    "mlx:qwen2.5-32b",
    # Research agent only — intentional cloud use for vague/non-private queries
    "openrouter/qwen/qwen3-235b-a22b-2507",
}
_OPENROUTER_ALLOWED_AGENTS = {"research", "main", "chat"}


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
        import psycopg2 as _pg2
        _conn = _pg2.connect("postgresql://kochj@127.0.0.1:5432/nova_ops")
        _cur = _conn.cursor()
        _cur.execute("SELECT content FROM nova_documents WHERE category='nova_config' AND name='openclaw.json'")
        row = _cur.fetchone()
        _conn.close()
        if not row:
            log("[privacy] openclaw.json not found in nova_ops — skipping", level=LOG_WARN, source="big-brother")
            return
        config = json.loads(row[0])
    except Exception as e:
        log(f"[privacy] Could not load openclaw.json from PG: {e}", level=LOG_WARN, source="big-brother")
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
        "gpu_stuck_procs":    _gpu_contention_status.get("procs_stuck", 0),
        "gpu_contention":     1 if _gpu_contention_status.get("contention_active") else 0,
        "ollama_latency_ms":  _gpu_contention_status.get("ollama_latency_ms"),
        "journal_fixed":      _journal_image_status.get("fixed", 0),
    }

    # Services up/down from cached status
    with _lock:
        svc = dict(_service_status)
    bucket["services_up"]   = sum(1 for s in svc.values() if s.get("up", True))
    bucket["services_down"] = sum(1 for s in svc.values() if not s.get("up", True))

    # Memory server stats
    try:
        r = urllib.request.urlopen(f"http://{LAN_IP}:18790/stats", timeout=3)
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
            bucket["redis_pct"] = round(ri["used_memory"] / ri["maxmemory"] * 100, 2)
    except Exception:
        pass

    # Gateway RSS (nova_gateway_v2.py)
    try:
        r2 = subprocess.run(["pgrep", "-f", "nova_gateway_v2"], capture_output=True, text=True)
        pids = [p for p in r2.stdout.strip().split() if p]
        if pids:
            r3 = subprocess.run(["ps", "-o", "rss=", "-p", pids[0]],
                                 capture_output=True, text=True)
            bucket["gateway_rss_mb"] = round(int(r3.stdout.strip() or "0") / 1024, 1)
    except Exception:
        pass

    # Scheduler failures
    try:
        r4 = urllib.request.urlopen(f"http://{LAN_IP}:37460/status", timeout=3)
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

    # ── Check maintenance mode — suppress restarts + alerts, still collect metrics ──
    maintenance_active = _is_maintenance_mode()
    if maintenance_active:
        log("Maintenance mode active — checks running, restarts + Slack alerts suppressed",
            level=LOG_WARN, source="big-brother")

    protected_running = _is_protected_task_running()

    # ── Flush pending restarts if protection lifted ──────────────────────────
    if not protected_running and _pending_restart:
        _flush_pending_restarts()

    # ── Log file scan (proactive — catches errors before service checks) ─────
    # Deduplicate per (svc, desc) within a single sweep — prevents a burst of
    # identical log lines (e.g. 50 "dead-lettered item" entries) from producing
    # 50 identical issue entries in one Slack message.
    _log_issues_this_sweep: set = set()
    for lf in LOG_FILES_TO_WATCH:
        for sev, svc, desc, line_excerpt in _scan_log_file(lf):
            dedup_key = f"{svc}:{desc}"
            if dedup_key in _log_issues_this_sweep:
                continue   # same pattern already appended this sweep
            _log_issues_this_sweep.add(dedup_key)
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
        # Skip port check if we just kicked this service — it may not be bound yet
        if time.time() - _service_kickstart_at.get(name, 0) < SERVICE_STARTUP_GRACE:
            log(f"[sweep] {name} in startup grace period — skipping port check",
                level=LOG_INFO, source="big-brother")
            continue

        # Adaptive frequency: skip if not enough time elapsed for this service
        if not _should_check_now(name):
            continue

        up = _service_is_up(name, host, port, health_path)
        _update_adaptive_interval(name, up)

        # Score-history confirmation: don't act on single-blip failures
        confirmed_down = _score_history_confirms_down(name, up)

        with _lock:
            prev = _service_status.get(name, {}).get("up", True)
            _service_status[name] = {
                "up": up,
                "last_seen": _now_iso() if up else _service_status.get(name, {}).get("last_seen"),
                "restarts": _service_status.get(name, {}).get("restarts", 0),
                "last_error": None if up else f"Not responding on :{port}",
                "check_interval_s": _get_service_interval(name),
                "recent_checks": list(_service_score_history.get(name, [])),
            }

        if not confirmed_down:
            if not up:
                log(f"[sweep] {name} check failed but not confirmed (score-history: "
                    f"{list(_service_score_history.get(name, []))})",
                    level=LOG_INFO, source="big-brother")
            continue

        # Heighten correlated services when one goes down
        _heighten_correlated(name)

        if not up:
            if name in SILENCED_SERVICES:
                log(f"[sweep] {name} down but silenced — skipping alert", level=LOG_INFO, source="big-brother")
                continue
            issues.append(f"{name} (:{port}) DOWN")

            # Global or per-service maintenance brake — record but don't restart or alert
            if maintenance_active or _is_service_in_maintenance(name):
                reason = "global maintenance" if maintenance_active else "per-service maintenance"
                log(f"[sweep] {name} DOWN but {reason} active — skipping restart",
                    level=LOG_WARN, source="big-brother")
                _record_event("warning", f"{name} DOWN ({reason})", "Skipped restart — maintenance brake", name)
                continue

            if not critical and name not in ("SwarmUI", "TinyChat"):
                _record_event("warning", f"{name} not responding on :{port}", "No action (non-critical)", name)
                continue

            if protected_running and name not in ("Gateway", "Signal-cli"):
                _queue_restart(name)
                fixes.append(f"Queued restart of {name} (protected task running)")
                _record_event("warning", f"{name} DOWN", "Queued restart", name)
                continue

            # Claude Code conflict avoidance — defer if script is being edited
            if _is_service_being_edited(name):
                log(f"[sweep] {name} DOWN but Claude is editing — deferring restart",
                    level=LOG_WARN, source="big-brother")
                _notify_claude_editing_conflict(name)
                _queue_restart(name)
                fixes.append(f"Deferred restart of {name} (Claude editing script)")
                _record_event("info", f"Restart deferred: {name}",
                              "Claude Code is editing the script", name)
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
                # Use pg_ctl which handles stale postmaster.pid from crashes
                pg_ctl = "/opt/homebrew/opt/postgresql@17/bin/pg_ctl"
                pg_data = "/Volumes/MoreData/postgresql@17"
                pg_log  = f"{pg_data}/homebrew-log/postgresql@17.log"
                try:
                    subprocess.run(
                        [pg_ctl, "start", "-D", pg_data, "-l", pg_log, "-w"],
                        capture_output=True, timeout=30,
                    )
                except Exception:
                    if label:
                        _kickstart(label)
                fixes.append("Restarted PostgreSQL")
                _record_event("critical", "PostgreSQL DOWN", "Restarted via pg_ctl", "PostgreSQL")

            elif name == "Redis":
                if label:
                    _kickstart(label)
                fixes.append("Restarted Redis")
                _record_event("critical", "Redis DOWN", "Restarted via launchctl", "Redis")

            elif name == "Memory Server" and label:
                # Dependency check: Memory Server needs PG and Redis healthy.
                # If either dependency is down, restarting Memory Server just causes
                # another crash-loop — suppress the restart and let the dependency
                # fix cascade naturally on the next sweep.
                pg_up    = _port_open("127.0.0.1", 5432)
                redis_up = _port_open("127.0.0.1", 6379)
                if not pg_up:
                    log("Memory Server DOWN but PostgreSQL also down — skipping restart, waiting for PG",
                        level=LOG_WARN, source="big-brother")
                    _record_event("warning", "Memory Server DOWN (PG dependency also down)",
                                  "Skipped restart — will retry after PG recovers", "Memory Server")
                elif not redis_up:
                    log("Memory Server DOWN but Redis also down — skipping restart, waiting for Redis",
                        level=LOG_WARN, source="big-brother")
                    _record_event("warning", "Memory Server DOWN (Redis dependency also down)",
                                  "Skipped restart — will retry after Redis recovers", "Memory Server")
                elif _check_crash_loop("Memory Server"):
                    # Crash-loop cooldown active — already logged and alerted inside _check_crash_loop
                    pass
                else:
                    # Double-check port is truly free before kickstarting —
                    # avoids EADDRINUSE (exit 256) when the process is mid-startup
                    if _port_open(LAN_IP, 18790):
                        log("Memory Server port is up — skipping kickstart (false alarm)",
                            level=LOG_INFO, source="big-brother")
                    else:
                        _service_kickstart_at["Memory Server"] = time.time()
                        _kickstart(label)
                        fixes.append("Restarted Memory Server")
                        _record_event("critical", "Memory Server DOWN", "Restarted via launchctl", "Memory Server")

            elif name == "Scheduler":
                if not _check_scheduler_heartbeat():
                    _kickstart("com.nova.scheduler")
                    fixes.append("Restarted Scheduler (stale heartbeat)")
                    _record_event("critical", "Scheduler stale heartbeat", "Kickstarted via launchctl", "Scheduler")

            elif name == "Gateway" or name == "Signal-cli":
                if not _check_crash_loop("Gateway"):
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

    # ── Notify Claude about critical service restarts ────────────────────────
    # If we restarted any services that Claude might be depending on, publish
    # a Redis notification so an active Claude session knows immediately.
    if fixes:
        _redis_notify_claude(
            "service_restart",
            f"Big Brother performed service actions: {'; '.join(fixes)}. "
            f"Some services may have been temporarily unavailable.",
            priority=2,
        )

    # ── Persistent service downtime escalation to Claude Queue ───────────────
    # Track services that remain down across sweeps. If a service stays down
    # for >15 minutes despite BB's heal attempts, escalate to claude_queue.
    global _pg_down_since
    now_ts = time.time()
    for name, host, port, label, critical, health_path in SERVICES:
        svc_up = _service_status.get(name, {}).get("up", True)
        if not svc_up:
            if name not in _service_down_since:
                _service_down_since[name] = now_ts
            elif now_ts - _service_down_since[name] > SERVICE_ESCALATION_THRESHOLD:
                down_min = int((now_ts - _service_down_since[name]) / 60)
                _escalate_to_claude(
                    f"{name} has been down for {down_min}+ minutes after Big Brother's "
                    f"auto-heal attempts. Port {port} on {host} not responding. "
                    f"Check launchd label '{label or 'N/A'}' and service logs.",
                    priority=1 if critical else 3
                )
                # Reset timer so we don't re-escalate every sweep (dedup handles it anyway)
                _service_down_since[name] = now_ts
        else:
            # Service recovered — clear downtime tracking
            _service_down_since.pop(name, None)

    # ── PostgreSQL-specific escalation (>5 min unreachable) ──────────────────
    pg_up = _port_open("127.0.0.1", 5432)
    if not pg_up:
        if _pg_down_since == 0.0:
            _pg_down_since = now_ts
        elif now_ts - _pg_down_since > PG_ESCALATION_THRESHOLD:
            down_min = int((now_ts - _pg_down_since) / 60)
            _escalate_to_claude(
                f"PostgreSQL unreachable for {down_min}+ minutes. Port 5432 not responding. "
                f"Nova memory system, session logging, and all DB-dependent services are impacted. "
                f"Check pg_ctl status, postmaster.pid, and /Volumes/MoreData/postgresql@17 volume mount.",
                priority=1
            )
            _pg_down_since = now_ts  # Reset so dedup handles repeat prevention
    else:
        _pg_down_since = 0.0

    # ── Internet outage detection (runs before channel checks) ──────────────
    _handle_internet_state(issues, fixes)

    # ── Channel health (only if gateway is up and internet is up) ────────────
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
        # Discord has a known persistent @buape/carbon WebSocket bug — restarts don't fix it
        # and cause Signal gaps + cascade failures. Only restart for Slack or Signal outages.
        restartable_disconnects = [ch for ch in disconnected if ch != "discord"]
        if disconnected:
            # Suppress per-restart channel alerts when internet is down —
            # the disconnect is caused by WAN loss, not a Nova config problem.
            if _internet_down:
                log(f"Channel disconnects suppressed — internet is DOWN",
                    level=LOG_INFO, source="big-brother")
            elif maintenance_active:
                log(f"Channel disconnects suppressed — maintenance mode active",
                    level=LOG_INFO, source="big-brother")
            elif not restartable_disconnects:
                # Discord-only — log quietly, don't restart
                log(f"Discord disconnected (known @buape/carbon bug) — not restarting gateway",
                    level=LOG_INFO, source="big-brother")
            else:
                issues.append(f"Channels disconnected: {', '.join(restartable_disconnects)}")
                _record_event("critical",
                              f"Channels disconnected: {', '.join(restartable_disconnects)}",
                              "Restarting gateway",
                              "Gateway")
                if not protected_running and not _check_crash_loop("Gateway"):
                    success = _restart_gateway()
                    if success:
                        fixes.append(f"Restarted gateway (channels: {', '.join(restartable_disconnects)})")
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
    mem_up = _port_open(LAN_IP, 18790)
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
                continue

            # Check for noowners — APFS volumes remounted after crash/reboot
            # often lose owner tracking. PostgreSQL requires ownership and will
            # silently fail with "Operation not permitted" on every file open.
            # diskutil info parses mount options; 'noowners' flag is the tell.
            if "MoreData" in mount_path or "Data" in mount_path:
                try:
                    r = subprocess.run(
                        ["diskutil", "info", mount_path],
                        capture_output=True, text=True, timeout=5
                    )
                    if "Owners:                    Disabled" in r.stdout:
                        issues.append(
                            f":no_entry: Volume {mount_path} ownership DISABLED — "
                            f"run: sudo diskutil enableOwnership {mount_path}"
                        )
                        _record_event(
                            "critical",
                            f"Volume {mount_path} noowners flag active",
                            "Run: sudo diskutil enableOwnership " + mount_path,
                            "System",
                        )
                        # Rate limit via the sweep's issue key system — no separate _maybe_notify
                        # (the old inline call was firing every sweep, bypassing cooldowns)
                except Exception:
                    pass

    # ── External LAN service checks ──────────────────────────────────────────
    for name, host, port in EXTERNAL_CHECKS:
        if not _port_open(host, port, timeout=5.0):
            with _lock:
                _external_fail_counts[name] = _external_fail_counts.get(name, 0) + 1
                count = _external_fail_counts[name]
            if count >= EXTERNAL_FAIL_THRESHOLD:
                issues.append(f"{name} ({host}:{port}) unreachable")
                _record_event("warning", f"{name} unreachable ({count} consecutive failures)",
                              "Check device power and LAN connection", name)
            else:
                log(f"[sweep] {name} ({host}:{port}) check failed ({count}/{EXTERNAL_FAIL_THRESHOLD}) — not alerting yet",
                    level=LOG_INFO, source="big-brother")
        else:
            with _lock:
                if _external_fail_counts.get(name, 0) > 0:
                    log(f"[sweep] {name} recovered after {_external_fail_counts[name]} failure(s)",
                        level=LOG_INFO, source="big-brother")
                _external_fail_counts[name] = 0

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
                # Claude Code conflict avoidance
                if _is_service_being_edited(name):
                    log(f"[sweep] {name} crashed but Claude is editing — deferring restart",
                        level=LOG_WARN, source="big-brother")
                    _notify_claude_editing_conflict(name)
                    fixes.append(f"Deferred restart of {name} (Claude editing script)")
                    _record_event("info", f"Restart deferred: {name}",
                                  "Claude Code is editing the script", name)
                else:
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
        r = _redis.Redis(host=LAN_IP, port=6379, decode_responses=True)
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
        needed = {"qwen3:30b-a3b", "qwen3-coder:30b"}  # qwen3-next:80b replaced by 30b-a3b interim
        cold = needed - {m.split(":")[0] + ":" + m.split(":")[1] if ":" in m else m for m in loaded}
        # Only warn during active hours — models unload when idle
        if cold and not _is_quiet_hours():
            log(f"[ollama] Cold models (next request will be slow): {cold}",
                level=LOG_INFO, source="big-brother")
    except Exception:
        pass

    # ── GPU contention detection (Metal deadlock prevention) ─────────────────
    _check_gpu_contention(issues, fixes)

    # ── Scheduler per-task failure detection + auto-remediation ──────────────
    try:
        resp = urllib.request.urlopen(f"http://{LAN_IP}:37460/tasks", timeout=5)
        tasks = json.loads(resp.read())
        task_items = tasks.items() if isinstance(tasks, dict) else [(t.get("name", "?"), t) for t in tasks]
        for task_id, t in task_items:
            fails = t.get("consecutive_failures", 0)
            if fails < TIMEOUT_AUTOTUNE_MIN_FAILURES:
                continue

            script = t.get("script", "")
            is_gpu_heavy = t.get("gpu_heavy", False)
            issues.append(f"Scheduler task '{task_id}' failing: {fails} consecutive failures")

            # Fetch the most recent failed run's error_tail for diagnosis
            error_tail = ""
            try:
                rresp = urllib.request.urlopen(
                    f"http://{LAN_IP}:37460/runs/{task_id}", timeout=5)
                runs = json.loads(rresp.read())
                for run in (runs if isinstance(runs, list) else []):
                    if run.get("exit_code") != 0 and run.get("error_tail"):
                        error_tail = run["error_tail"]
                        break
            except Exception:
                pass

            # ── Remediation 1: Timeout auto-tuning ─────────────────────────
            if "Timed out after" in error_tail:
                if _autotune_task_timeout(task_id, 0, issues, fixes, error_tail):
                    continue  # Fixed — skip further remediation for this task

            # ── Remediation 2: Image backend restart for gpu_heavy tasks ───
            if is_gpu_heavy and error_tail:
                if _restart_image_backend(task_id, issues, fixes):
                    continue

            # ── Remediation 3: Code bug escalation to Claude Code ──────────
            if error_tail and any(p in error_tail for p in _CODE_BUG_PATTERNS):
                if _escalate_code_bug(task_id, script, error_tail, issues):
                    continue

            # ── Fallback: alert only (no auto-fix available) ───────────────
            _record_event("warning", f"Scheduler task '{task_id}' {fails} consecutive failures",
                          "Check scheduler.log for error details", "Scheduler")
            _redis_notify_claude(
                "scheduler_failure",
                f"Scheduler task '{task_id}' has {fails} consecutive failures. "
                f"Error: {error_tail[:200] if error_tail else 'no error tail'}",
                priority=2,
            )
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
    # Only alert if count is above threshold AND either new or growing since last alert.
    # A static stale queue (same N items for weeks) is not actionable noise.
    global _dead_letter_last_count, _dead_letter_last_alerted
    try:
        resp = urllib.request.urlopen(f"http://{LAN_IP}:18790/stats", timeout=5)
        stats = json.loads(resp.read())
        dead = stats.get("dead_letter_count", 0)
        now = time.time()
        is_growing = dead > _dead_letter_last_count
        cooldown_expired = now - _dead_letter_last_alerted > DEAD_LETTER_ALERT_COOLDOWN
        if dead > DEAD_LETTER_THRESHOLD and (is_growing or cooldown_expired):
            issues.append(f"Memory dead-letter queue: {dead} items (embedding failures)")
            _record_event("warning", f"Memory dead-letter queue: {dead} items",
                          "Run: nova_dead_letter_replay.py  Check Ollama embed model", "Memory Server")
            _dead_letter_last_alerted = now
            # Escalate to Claude Code if dead-lettering is heavy (>10 items in this sweep)
            _escalate_to_claude(
                f"Memory server dead-lettering heavily ({dead} items this sweep). "
                f"Check PG connection and memory_server.log. May need embedding model "
                f"reload or nova_dead_letter_replay.py run. "
                f"Logs: ~/.openclaw/logs/memory-server-error.log",
                priority=3
            )
        _dead_letter_last_count = dead
    except Exception:
        pass

    # ── Scheduler failure rate ────────────────────────────────────────────────
    try:
        resp = urllib.request.urlopen(f"http://{LAN_IP}:37460/status", timeout=5)
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

    # Escalate disk space <5GB to Claude Queue (BB can't auto-clean)
    _DISK_ESCALATE_GB = 5.0
    for path, label in [("/Volumes/Data", "Data volume"), ("/Volumes/MoreData", "MoreData volume"),
                        (str(Path.home()), "Main SSD")]:
        try:
            stat = os.statvfs(path)
            free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
            if free_gb < _DISK_ESCALATE_GB:
                _escalate_to_claude(
                    f"Disk space critical: {label} ({path}) has only {free_gb:.1f}GB free. "
                    f"Services will start crashing from disk pressure. "
                    f"Need manual cleanup — check Docker images, Xcode DerivedData, "
                    f"old model files, log rotation.",
                    priority=1
                )
        except Exception:
            pass

    # ── Critical disk: auto-engage maintenance mode to stop restart cascade ──
    # When main SSD drops below 5GB, service crashes are caused by disk pressure,
    # not actual service bugs. Engaging global maintenance mode stops Big Brother
    # from spamming Slack with restart loops while the underlying cause is fixed.
    try:
        stat = os.statvfs(str(Path.home()))
        home_free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if home_free_gb < 5.0:
            import redis as _rds
            rc = _rds.Redis(host="127.0.0.1", port=6379, decode_responses=True)
            if not rc.get("nova:maintenance:active"):
                rc.setex("nova:maintenance:active", 3600, "1")  # 1h TTL
                log(f"CRITICAL: main SSD only {home_free_gb:.1f}GB free — auto-engaged maintenance mode (1h)",
                    level=LOG_ERROR, source="big-brother")
                _notify(
                    f":no_entry: *Disk critical: {home_free_gb:.1f}GB free on main SSD*\n"
                    f"Auto-engaged maintenance mode for 1h to prevent restart cascade.\n"
                    f"Service crashes are likely caused by disk pressure, not software bugs.\n"
                    f"Free up space, then run: `bb-maintenance off`",
                    is_critical=True,
                )
    except Exception:
        pass

    # ── Ollama auto-restart if port is down ──────────────────────────────────
    if not _port_open("127.0.0.1", 11434):
        try:
            subprocess.run(["open", "-a", "Ollama"], capture_output=True, timeout=10)
            fixes.append("Opened Ollama.app (was not running)")
            _record_event("critical", "Ollama (:11434) DOWN", "Launched Ollama.app via `open -a`", "Ollama")
        except Exception as exc:
            _record_event("critical", "Ollama (:11434) DOWN", f"Failed to launch Ollama.app: {exc}", "Ollama")

    # ── SwarmUI backend error detection ──────────────────────────────────────
    try:
        resp = urllib.request.urlopen(
            "http://127.0.0.1:7801/API/GetStatus", timeout=6
        )
        swarm = json.loads(resp.read())
        # SwarmUI returns {"status": "running"} or {"status": "error", "error": "..."}
        if swarm.get("status") == "error":
            err = swarm.get("error", "unknown error")
            _maybe_notify(
                "swarmui_backend_error",
                f":warning: *SwarmUI backend error* — {err[:120]}\n"
                f"Art Corner and image generation will fail until resolved.",
                is_critical=False,
            )
            _record_event("warning", f"SwarmUI backend error: {err[:80]}",
                          "Check SwarmUI logs; may need model reload", "SwarmUI")
    except Exception:
        pass

    # ── Synology + UNAS state file staleness ─────────────────────────────────
    _STATE_DIR = Path.home() / ".openclaw/workspace/state"
    _NAS_STATES = [
        (_STATE_DIR / "nova_synology_state.json", "Synology monitor", 7200),   # 2h
        (_STATE_DIR / "nova_unas_status.json",    "UNAS Pro monitor",  600),   # 10m
    ]
    for state_path, label, max_age in _NAS_STATES:
        try:
            if state_path.exists():
                age = time.time() - state_path.stat().st_mtime
                if age > max_age:
                    age_min = int(age // 60)
                    issues.append(f"{label} state stale ({age_min}m since last update)")
                    _record_event("warning", f"{label} state stale ({age_min}m)",
                                  "Check scheduler — monitor may not be running", label)
            else:
                issues.append(f"{label} state file missing — monitor not yet run")
                _record_event("warning", f"{label} state file missing",
                              "Run nova_synology_monitor.py or nova_unas_monitor.py manually", label)
        except Exception:
            pass

    # ── External volume disk space (/Volumes/external, /Volumes/NAS) ─────────
    for ext_vol, label in [("/Volumes/external", "External media (/Volumes/external)"),
                            ("/Volumes/NAS", "NAS mount (/Volumes/NAS)")]:
        try:
            st = os.statvfs(ext_vol)
            free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
            total_gb = (st.f_blocks * st.f_frsize) / (1024 ** 3)
            used_pct = 100 * (1 - st.f_bavail / max(st.f_blocks, 1))
            if used_pct > 90:
                issues.append(f"{label}: {used_pct:.0f}% full ({free_gb:.0f}GB free)")
                _record_event("warning", f"{label} {used_pct:.0f}% full",
                              "Delete old recordings or expand storage", "Storage")
        except Exception:
            pass  # not mounted — volume mount check handles this

    # ── Scheduler script existence check ─────────────────────────────────────
    # Catches the case where a script was deleted but its scheduler task remains.
    try:
        resp = urllib.request.urlopen(f"http://{LAN_IP}:37460/tasks", timeout=5)
        tasks = json.loads(resp.read())
        if isinstance(tasks, list):
            for t in tasks:
                script = t.get("script", "")
                if not script:
                    continue
                script_path = SCRIPTS / script
                if not script_path.exists():
                    task_name = t.get("name", script)
                    _maybe_notify(
                        f"missing_script_{script}",
                        f":x: *Scheduler script missing*: `{script}`\n"
                        f"Task `{task_name}` will fail every run until restored.",
                        is_critical=False,
                    )
                    _record_event("warning", f"Scheduler script missing: {script}",
                                  "Restore script or disable task in scheduler.yaml", "Scheduler")
    except Exception:
        pass

    # ── Kernel zone map usage (crash prevention — 2026-06-11 incident) ─────
    try:
        zp = subprocess.run(["sudo", "-n", "zprint"], capture_output=True, text=True, timeout=10)
        if zp.returncode == 0:
            for line in zp.stdout.splitlines():
                if line.startswith("data.kalloc.1024 "):
                    parts = line.split()
                    cur_size = parts[2] if len(parts) > 2 else "0K"
                    size_mb = float(cur_size.rstrip("KMG"))
                    if "G" in cur_size:
                        size_mb *= 1024
                    elif "K" in cur_size:
                        size_mb /= 1024
                    if size_mb > 5120:  # 5GB threshold
                        issues.append(f"KERNEL ZONE ALERT: data.kalloc.1024 at {size_mb:.0f}MB (>5GB)")
                        _record_event("critical",
                                      f"Kernel zone data.kalloc.1024 at {size_mb:.0f}MB — approaching zone map exhaustion",
                                      "Reboot soon to prevent kernel panic. See postmortem 2026-06-11.",
                                      "System")
                        _notify(
                            f":rotating_light: *KERNEL ZONE ALERT*\n"
                            f"`data.kalloc.1024` is at {size_mb:.0f}MB (threshold: 5GB)\n"
                            f"This zone leaked to 20GB before the 2026-06-11 kernel panic.\n"
                            f"Consider rebooting before zone map exhaustion.",
                            is_critical=True,
                        )
                    elif size_mb > 2048:  # 2GB warning
                        _record_event("warning",
                                      f"Kernel zone data.kalloc.1024 elevated: {size_mb:.0f}MB",
                                      "Monitor for growth — may indicate kernel memory leak",
                                      "System")
                    break
    except Exception:
        pass

    # ── Wazuh SIEM alert polling (every 5 min) ─────────────────────────────
    _check_wazuh_alerts(issues)

    # ── Fresh-Eyes Canary Check (every 10 min, not every sweep) ────────────
    # Ask a local LLM to review metrics for anomalies rules might miss.
    try:
        canary_result = _canary_check()
        if canary_result:
            issues.append(f"Canary: {canary_result[:200]}")
            _record_event("info", f"Canary concern: {canary_result[:100]}",
                          "LLM-detected anomaly — review recommended", "Canary")
            send, suffix = should_notify("canary_concern", "info")
            if send:
                _notify(f"\U0001F426 *Canary check:* {canary_result[:300]}{suffix}")
    except Exception as e:
        log(f"[canary] Exception in canary check (non-fatal): {e}",
            level=LOG_INFO, source="big-brother")

    # ── Resolve cleared escalations ──────────────────────────────────────────
    # Check which tracked escalation issues are no longer present this sweep
    # and send resolution notifications.
    active_issue_keys = set()
    for issue in issues:
        stable_key = re.sub(r'\d+', 'N', issue)
        active_issue_keys.add(f"issue_{stable_key}")

    with _lock:
        tracked_keys = list(_escalations.keys())
    for esc_key in tracked_keys:
        if esc_key.startswith("issue_") and esc_key not in active_issue_keys:
            was_tracked, resolve_suffix = _resolve_escalation(esc_key)
            if was_tracked and resolve_suffix:
                _notify(f":white_check_mark: {esc_key.replace('issue_', '')}{resolve_suffix}")

    # ── Notify ───────────────────────────────────────────────────────────────
    # Separate: fixes-only (healed, no remaining issues) vs real issues.
    # Real issues use the escalation tier system so a single stuck condition
    # doesn't fire every 90s forever.
    now = time.time()

    if fixes and not issues:
        # All-clear with heals — post once
        heal_msg = ":white_check_mark: *Big Brother healed*\n"
        heal_msg += "\n".join(f"  :wrench: {f}" for f in fixes)
        _notify(heal_msg)
        with _lock:
            _alerted_issues.clear()
        log(f"Sweep: all clear. {len(fixes)} heals applied.", level=LOG_INFO, source="big-brother")

    elif issues:
        is_critical = any(
            sev == "critical"
            for ev in list(_heal_events)[:10]
            if ev.get("ts", "") > _now_iso()[:13]  # within this hour
            for sev in [ev.get("severity", "")]
        )

        if maintenance_active:
            log(f"Sweep (maintenance): {len(issues)} issues suppressed — "
                f"{', '.join(issues[:3])}{'...' if len(issues) > 3 else ''}",
                level=LOG_WARN, source="big-brother")
        else:
            # Per-issue escalation-tier gating: only include issues that pass
            # the should_notify() check (handles cooldowns, dedup, auto-bumps).
            alertable = []
            for issue in issues:
                # Use a stable key derived from the issue text (strip volatile counts)
                stable_key = re.sub(r'\d+', 'N', issue)
                issue_id = f"issue_{stable_key}"

                # Determine severity tier for this issue
                if "DOWN" in issue or "CRASHED" in issue or "PRIVACY" in issue:
                    severity = "critical"
                elif "Scheduler task" in issue or "stale" in issue or "GPU" in issue:
                    severity = "warning"
                else:
                    severity = "info"

                send, suffix = should_notify(issue_id, severity)
                if send:
                    alertable.append(f"{issue}{suffix}")
                else:
                    log(f"Suppressed (escalation tier): {issue}", level=LOG_INFO,
                        source="big-brother")

            if alertable or fixes:
                msg = ":robot_face: *Big Brother Report*\n"
                msg += "\n".join(f"  :red_circle: {i}" for i in alertable)
                if not alertable and fixes:
                    # Only fixes, no new issues to report — skip this message
                    pass
                else:
                    if fixes:
                        msg += "\n*Healed:*\n"
                        msg += "\n".join(f"  :white_check_mark: {f}" for f in fixes)
                    _notify(msg, is_critical=any("DOWN" in i for i in alertable))

            log(f"Sweep: {len(issues)} issues ({len(alertable)} alerted via escalation), "
                f"{len(fixes)} fixes", level=LOG_WARN, source="big-brother")

        # Clear stale issue keys for issues that have resolved (legacy system compat)
        active_keys = {re.sub(r'\d+', 'N', i) for i in issues}
        stale = [k for k in list(_issue_last_alerted) if k not in active_keys]
        for k in stale:
            del _issue_last_alerted[k]

    else:
        with _lock:
            _alerted_issues.clear()
        _issue_last_alerted.clear()
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
            # Fetch maintenance info from Redis for the status response
            maint_info = {"global": False, "global_ttl_s": -1, "services": {}}
            try:
                import redis as _rds
                rc = _rds.Redis(host="127.0.0.1", port=6379, decode_responses=True)
                maint_info["global"] = bool(rc.get("nova:maintenance:active"))
                maint_info["global_ttl_s"] = rc.ttl("nova:maintenance:active")
                for key in rc.scan_iter("nova:maintenance:service:*"):
                    svc = key.replace("nova:maintenance:service:", "")
                    maint_info["services"][svc] = rc.ttl(key)
            except Exception:
                pass
            self._json({
                "daemon": "big-brother",
                "version": VERSION,
                "pid": os.getpid(),
                "uptime_s": int(time.time() - _start_time),
                "events_total": len(_heal_events),
                "services_down": [n for n, s in _service_status.items() if not s.get("up", True)],
                "pending_restarts": list(_pending_restart),
                "alerted_count": len(_alerted_issues),
                "maintenance": maint_info,
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
                r = urllib.request.urlopen(f"http://{LAN_IP}:37460/status", timeout=5)
                sched_stats = json.loads(r.read())
            except Exception:
                pass

            # Memory stats
            mem_stats = {}
            try:
                r = urllib.request.urlopen(f"http://{LAN_IP}:18790/stats", timeout=5)
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
                r = urllib.request.urlopen(f"http://{LAN_IP}:37460/tasks", timeout=5)
                raw = json.loads(r.read())
                if isinstance(raw, list):
                    sched_tasks = raw
            except Exception:
                pass

            # Privacy routing snapshot
            privacy_ok = True
            privacy_violations = []
            try:
                import psycopg2 as _pg2b
                _cnn = _pg2b.connect("postgresql://kochj@127.0.0.1:5432/nova_ops")
                _cur2 = _cnn.cursor()
                _cur2.execute("SELECT content FROM nova_documents WHERE category='nova_config' AND name='openclaw.json'")
                _row2 = _cur2.fetchone()
                _cnn.close()
                if _row2:
                    cfg = json.loads(_row2[0])
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
                "gpu": _gpu_contention_status,
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
        elif self.path == "/bb/gpu":
            # GPU contention status — quick check for Metal deadlock state
            stuck_procs = _get_gpu_stuck_processes()
            self._json({
                **_gpu_contention_status,
                "stuck_procs": stuck_procs,
                "kill_cooldown_remaining_s": max(0, int(GPU_KILL_COOLDOWN - (time.time() - _gpu_last_kill))),
            })
        elif self.path == "/bb/maintenance":
            # List all active maintenance flags (global + per-service)
            try:
                import redis as _rds
                rc = _rds.Redis(host="127.0.0.1", port=6379, decode_responses=True)
                global_flag = bool(rc.get("nova:maintenance:active"))
                global_ttl  = rc.ttl("nova:maintenance:active")
                svc_flags = {}
                for key in rc.scan_iter("nova:maintenance:service:*"):
                    svc = key.replace("nova:maintenance:service:", "")
                    svc_flags[svc] = {"active": True, "ttl_s": rc.ttl(key)}
                self._json({
                    "global_maintenance": global_flag,
                    "global_ttl_s": global_ttl,
                    "service_maintenance": svc_flags,
                    "note": "POST /bb/maintenance/set to activate, POST /bb/maintenance/clear to deactivate",
                })
            except Exception as e:
                self._json({"error": str(e)})
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
        elif self.path == "/bb/maintenance/set":
            # Body: {"service": "Memory Server", "ttl": 3600}
            # service=null or omitted → global flag
            # Sets the maintenance brake. Big Brother will NOT restart the named service
            # (or any service if global) until the TTL expires or /clear is called.
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                ttl  = int(body.get("ttl", 3600))
                svc  = body.get("service")
                import redis as _rds
                rc = _rds.Redis(host="127.0.0.1", port=6379, decode_responses=True)
                if svc:
                    safe = svc.replace(" ", "_").lower()
                    rc.setex(f"nova:maintenance:service:{safe}", ttl, "1")
                    key_set = f"nova:maintenance:service:{safe}"
                else:
                    rc.setex("nova:maintenance:active", ttl, "1")
                    key_set = "nova:maintenance:active"
                log(f"Maintenance brake SET for {'global' if not svc else svc} (TTL {ttl}s)",
                    level=LOG_WARN, source="big-brother")
                self._json({"set": key_set, "ttl_s": ttl, "service": svc or "global"})
            except Exception as e:
                self._json({"error": str(e)})
        elif self.path == "/bb/maintenance/clear":
            # Body: {"service": "Memory Server"} or {} for global
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                svc  = body.get("service")
                import redis as _rds
                rc = _rds.Redis(host="127.0.0.1", port=6379, decode_responses=True)
                if svc:
                    safe = svc.replace(" ", "_").lower()
                    rc.delete(f"nova:maintenance:service:{safe}")
                    cleared = f"nova:maintenance:service:{safe}"
                else:
                    rc.delete("nova:maintenance:active")
                    cleared = "nova:maintenance:active"
                log(f"Maintenance brake CLEARED for {'global' if not svc else svc}",
                    level=LOG_INFO, source="big-brother")
                self._json({"cleared": cleared, "service": svc or "global"})
            except Exception as e:
                self._json({"error": str(e)})
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
        if DIGEST_MODE:
            _flush_digest()
        time.sleep(1)

    _cleanup_pid()
    log("Big Brother stopped", level=LOG_INFO, source="big-brother")


if __name__ == "__main__":
    main()
