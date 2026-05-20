#!/usr/bin/env python3
"""
nova_incident_triage.py — Pre-analysis layer between Big Brother and Claude Code.

When Big Brother detects an issue it cannot auto-fix, this module gathers
context (log tails, scheduler history, related service status, heal attempts)
and writes a structured incident report to claude_queue.

This gives Claude Code a full picture on arrival instead of a bare description.

Called by Big Brother's _escalate_to_claude() — not run standalone.

Written by Jordan Koch.
"""

import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN

# ── Configuration ────────────────────────────────────────────────────────────

PG_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
LOG_DIR = Path.home() / ".openclaw/logs"
SCRIPTS_DIR = Path.home() / ".openclaw/scripts"
BRIDGE_SESSION_ID = "claude-bridge-persistent"

# Map service names to their log files (relative to LOG_DIR)
SERVICE_LOG_MAP = {
    "PostgreSQL":    ["nova.jsonl"],
    "PgBouncer":     ["nova.jsonl"],
    "Redis":         ["nova.jsonl"],
    "Ollama":        ["ollama-serve.log", "ollama-serve-error.log"],
    "Memory Server": ["memory-server.log", "memory-server-error.log"],
    "Gateway v2":    ["nova_gateway_v2.log", "nova_gateway_v2_error.log"],
    "Scheduler":     ["scheduler.log"],
    "MLX Server":    ["nova.jsonl"],
    "SwarmUI":       ["nova.jsonl"],
    "ComfyUI":       ["comfyui-watchdog.log"],
    "TinyChat":      ["nova.jsonl"],
    "OpenWebUI":     ["nova.jsonl"],
    "SearXNG":       ["nova.jsonl"],
    "Signal-cli":    ["signal-cli.log"],
    "NovaControl":   ["nova.jsonl"],
    "Slack":         ["slack-preprocessor.log"],
    "Big Brother":   ["big-brother.log", "big-brother.err.log"],
}

# Service dependency graph — which services depend on which
SERVICE_DEPENDENCIES = {
    "Gateway v2":    ["PostgreSQL", "Redis", "Ollama", "Memory Server"],
    "Memory Server": ["PostgreSQL", "Ollama"],
    "Scheduler":     ["PostgreSQL", "Redis"],
    "MLX Server":    [],
    "Ollama":        [],
    "PostgreSQL":    [],
    "Redis":         [],
    "Signal-cli":    [],
    "NovaControl":   ["PostgreSQL", "Redis"],
    "OpenWebUI":     ["Ollama"],
    "TinyChat":      ["Ollama"],
    "SwarmUI":       [],
    "ComfyUI":       [],
    "PgBouncer":     ["PostgreSQL"],
    "Slack":         ["Gateway v2", "Redis"],
}

# Port map for quick connectivity checks
SERVICE_PORTS = {
    "PostgreSQL":    ("127.0.0.1", 5432),
    "PgBouncer":     ("127.0.0.1", 6432),
    "Redis":         ("127.0.0.1", 6379),
    "Ollama":        ("127.0.0.1", 11434),
    "Memory Server": ("192.168.1.6", 18790),
    "Gateway v2":    ("127.0.0.1", 18792),
    "Scheduler":     ("192.168.1.6", 37460),
    "MLX Server":    ("192.168.1.6", 5050),
    "SwarmUI":       ("127.0.0.1", 7801),
    "ComfyUI":       ("127.0.0.1", 8188),
    "TinyChat":      ("192.168.1.6", 8000),
    "OpenWebUI":     ("192.168.1.6", 3000),
    "SearXNG":       ("127.0.0.1", 8888),
    "Signal-cli":    ("127.0.0.1", 8080),
    "NovaControl":   ("127.0.0.1", 37400),
}


# ── Utilities ────────────────────────────────────────────────────────────────

def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return []
        lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
        return [line.split("\x1f") for line in lines]
    except (subprocess.TimeoutExpired, Exception):
        return []


def _pg_execute(sql: str, params: tuple = ()):
    """Execute a PostgreSQL write via psql subprocess."""
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


# ── Context Gathering Functions ──────────────────────────────────────────────

def _get_log_tail(service_name: str, lines: int = 30) -> str:
    """Get the last N lines from the service's log file(s)."""
    log_files = SERVICE_LOG_MAP.get(service_name, ["nova.jsonl"])
    combined = []

    for log_file in log_files:
        path = LOG_DIR / log_file
        if not path.exists():
            continue
        try:
            result = subprocess.run(
                ["tail", "-n", str(lines), str(path)],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                combined.append(f"--- {log_file} (last {lines} lines) ---")
                combined.append(result.stdout.strip())
        except (subprocess.TimeoutExpired, Exception):
            combined.append(f"--- {log_file}: read failed ---")

    return "\n".join(combined) if combined else "No log files found"


def _get_recent_runs(service_name: str, count: int = 5) -> list:
    """Get recent scheduler runs for tasks related to this service."""
    # Map service names to likely task script patterns
    service_script_patterns = {
        "Gateway v2": "gateway",
        "Memory Server": "memory",
        "Scheduler": "scheduler",
        "Ollama": "ollama",
        "MLX Server": "mlx",
        "Signal-cli": "signal",
        "Slack": "slack",
        "NovaControl": "control",
    }

    pattern = service_script_patterns.get(service_name, service_name.lower().replace(" ", "_"))

    rows = _pg_query(
        f"SELECT task_id, task_script, started_at, duration_ms, exit_code, status, error_tail "
        f"FROM scheduler_runs "
        f"WHERE task_script ILIKE '%{pattern}%' "
        f"ORDER BY started_at DESC LIMIT %s",
        (str(count),)
    )

    runs = []
    for row in rows:
        if len(row) >= 7:
            runs.append({
                "task_id": row[0],
                "script": row[1],
                "started_at": row[2],
                "duration_ms": row[3],
                "exit_code": row[4],
                "status": row[5],
                "error_tail": row[6] if row[6] else None,
            })
    return runs


def _check_related_services(service_name: str) -> dict:
    """Check the status of services that depend on or are depended upon."""
    related = {}

    # Check dependencies (services this one needs)
    deps = SERVICE_DEPENDENCIES.get(service_name, [])
    for dep in deps:
        port_info = SERVICE_PORTS.get(dep)
        if port_info:
            host, port = port_info
            related[dep] = {
                "role": "dependency",
                "up": _port_open(host, port),
                "endpoint": f"{host}:{port}",
            }

    # Check dependents (services that need this one)
    for svc, svc_deps in SERVICE_DEPENDENCIES.items():
        if service_name in svc_deps and svc != service_name:
            port_info = SERVICE_PORTS.get(svc)
            if port_info:
                host, port = port_info
                related[svc] = {
                    "role": "dependent",
                    "up": _port_open(host, port),
                    "endpoint": f"{host}:{port}",
                }

    return related


def _suggest_fix(service_name: str, issue_description: str) -> list:
    """Pattern-match the issue and suggest likely fixes."""
    suggestions = []
    desc_lower = issue_description.lower()

    # GPU contention
    if "gpu" in desc_lower or "metal" in desc_lower or "contention" in desc_lower:
        suggestions.append("Check mlx_whisper processes: ps aux | grep mlx_whisper")
        suggestions.append("Consider killing or deferring gpu_heavy scheduler tasks")
        suggestions.append("Check Ollama Metal allocation: curl http://127.0.0.1:11434/api/ps")
        suggestions.append("If persistent, restart Ollama: launchctl kickstart -k system/homebrew.mxcl.ollama")

    # SMTP / email
    if "smtp" in desc_lower or "mail" in desc_lower or "email" in desc_lower:
        suggestions.append("Check Keychain entry: security find-generic-password -s nova-smtp-app-password -a nova")
        suggestions.append("Verify Gmail app password is still valid (Google security page)")
        suggestions.append("Check network connectivity to smtp.gmail.com:587")

    # Timeout / connectivity
    if "timeout" in desc_lower or "unreachable" in desc_lower or "not responding" in desc_lower:
        suggestions.append("Check if dependent services are responsive: Ollama, PG, Redis")
        suggestions.append("Check network: ping 127.0.0.1 and ping 192.168.1.6")
        suggestions.append("Review launchd service status: launchctl list | grep nova")

    # Crash loop
    if "crash" in desc_lower or "loop" in desc_lower:
        suggestions.append("Check recent code changes: git log --oneline -5 in ~/.openclaw/scripts")
        suggestions.append("Review error logs for stack traces or import errors")
        suggestions.append("Check disk space: df -h /Volumes/Data /Volumes/MoreData")
        suggestions.append("Verify Python dependencies: pip list | grep -i [relevant_package]")

    # Memory / OOM
    if "memory" in desc_lower or "oom" in desc_lower or "killed" in desc_lower:
        suggestions.append("Check system memory: vm_stat | head -10")
        suggestions.append("Check for Ollama model leak: curl http://127.0.0.1:11434/api/ps")
        suggestions.append("Review memory-hungry processes: ps aux --sort=-%mem | head -10")

    # Disk space
    if "disk" in desc_lower or "space" in desc_lower or "full" in desc_lower:
        suggestions.append("Check volumes: df -h /Volumes/Data /Volumes/MoreData /")
        suggestions.append("Clean Xcode derived data: rm -rf ~/Library/Developer/Xcode/DerivedData")
        suggestions.append("Check log rotation: du -sh ~/.openclaw/logs/")
        suggestions.append("Look for large temp files: find /tmp -size +100M 2>/dev/null")

    # Dead letters / embedding
    if "dead-letter" in desc_lower or "embedding" in desc_lower:
        suggestions.append("Run dead letter replay: python3 ~/.openclaw/scripts/nova_dead_letter_replay.py")
        suggestions.append("Check Ollama embed model: curl http://127.0.0.1:11434/api/ps")
        suggestions.append("Check Memory Server health: curl http://192.168.1.6:18790/health")

    # Signal
    if "signal" in desc_lower:
        suggestions.append("Check signal-cli lock: ls -la ~/.local/share/signal-cli/data/*.lock")
        suggestions.append("Kill stale signal processes: pkill -f signal-cli")
        suggestions.append("Restart signal-cli: signal-cli -a +13233645436 daemon --socket /tmp/signal-cli.sock")

    # PostgreSQL
    if "postgres" in desc_lower or "pg" in desc_lower:
        suggestions.append("Check PG status: pg_isready -h 127.0.0.1 -p 5432")
        suggestions.append("Check volume mount: ls /Volumes/MoreData/postgresql@17")
        suggestions.append("Check idle connections: psql -c 'SELECT count(*) FROM pg_stat_activity'")
        suggestions.append("Review postmaster.pid: cat /Volumes/MoreData/postgresql@17/postmaster.pid")

    # Gateway / workspace
    if "gateway" in desc_lower or "workspace" in desc_lower or "eperm" in desc_lower:
        suggestions.append("Check workspace permissions: ls -la ~/.openclaw/workspace/")
        suggestions.append("Check gateway health: curl http://127.0.0.1:18792/health")
        suggestions.append("Review auth-profiles.json format")

    # Generic fallback
    if not suggestions:
        suggestions.append(f"Check service logs: tail -50 ~/.openclaw/logs/{service_name.lower().replace(' ', '-')}.log")
        suggestions.append("Check launchd status: launchctl list | grep -i nova")
        suggestions.append("Review Big Brother heal history: curl http://192.168.1.6:37461/bb/events?n=20")

    return suggestions


def _get_bb_heal_history(service_name: str) -> list:
    """Get Big Brother's recent heal attempts for this service."""
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://192.168.1.6:37461/bb/events?n=50", timeout=5)
        events = json.loads(resp.read())
        if isinstance(events, dict) and "events" in events:
            events = events["events"]
        # Filter to this service
        relevant = []
        svc_lower = service_name.lower()
        for ev in events:
            if isinstance(ev, dict):
                ev_svc = ev.get("service", "").lower()
                ev_issue = ev.get("issue", "").lower()
                if svc_lower in ev_svc or svc_lower in ev_issue:
                    relevant.append({
                        "ts": ev.get("ts"),
                        "severity": ev.get("severity"),
                        "issue": ev.get("issue"),
                        "fix": ev.get("fix"),
                    })
            if len(relevant) >= 10:
                break
        return relevant
    except Exception:
        return []


# ── Main Triage Function ─────────────────────────────────────────────────────

def triage_incident(service_name: str, issue_description: str, raw_error: str = None, priority: int = 3):
    """Gather context and write a pre-analyzed incident report for Claude.

    Called by Big Brother's _escalate_to_claude() instead of writing
    directly to claude_queue. This enriches the escalation with full
    context so Claude Code can act immediately.

    Args:
        service_name:      Name of the affected service (e.g. "Gateway v2", "Ollama")
        issue_description: Human-readable description of what's wrong
        raw_error:         Optional raw error text from Big Brother detection
        priority:          Queue priority (1=critical, 5=low). Default 3.
    """
    log(f"[triage] Starting incident triage for {service_name}: {issue_description[:80]}",
        level=LOG_INFO, source="incident-triage")

    # Gather all context
    report = {
        "service": service_name,
        "issue": issue_description,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "priority": priority,
        "log_tail": _get_log_tail(service_name, lines=30),
        "recent_runs": _get_recent_runs(service_name, count=5),
        "related_services": _check_related_services(service_name),
        "suggested_actions": _suggest_fix(service_name, issue_description),
        "bb_attempts": _get_bb_heal_history(service_name),
    }

    if raw_error:
        report["raw_error"] = raw_error[:2000]  # Cap at 2KB

    # Check if the root cause might be a dependency
    related = report["related_services"]
    down_deps = [name for name, info in related.items()
                 if info.get("role") == "dependency" and not info.get("up")]
    if down_deps:
        report["likely_root_cause"] = f"Dependency failure: {', '.join(down_deps)} are down"
        report["suggested_actions"].insert(0,
            f"FIX DEPENDENCIES FIRST: {', '.join(down_deps)} are not responding")

    # Write to claude_queue with structured context
    _queue_incident(report)

    log(f"[triage] Incident report queued for Claude — {service_name} "
        f"(priority {priority}, {len(report['suggested_actions'])} suggestions)",
        level=LOG_INFO, source="incident-triage")


def _queue_incident(report: dict):
    """Write the triaged incident to claude_queue with full context JSON."""
    description = f"INCIDENT: {report['service']} — {report['issue']}"
    context_json = json.dumps(report, default=str)

    # Ensure bridge session exists
    _pg_execute(
        "INSERT INTO claude_sessions (session_id, project, status, summary) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (session_id) DO NOTHING",
        (BRIDGE_SESSION_ID, "nova-claude-bridge", "active",
         "Persistent session for nova_claude_bridge.py integration")
    )

    # Deduplication check — don't insert if same service + issue already queued
    existing = _pg_query(
        "SELECT 1 FROM claude_queue "
        "WHERE description = %s AND status IN ('queued', 'in_progress')",
        (description,)
    )
    if existing:
        log(f"[triage] Incident already queued, skipping: {description[:80]}",
            level=LOG_INFO, source="incident-triage")
        return

    # Insert into claude_queue
    success = _pg_execute(
        "INSERT INTO claude_queue (session_id, status, priority, description, context) "
        "VALUES (%s, %s, %s, %s, %s)",
        (BRIDGE_SESSION_ID, "queued", str(report.get("priority", 3)),
         description, context_json)
    )

    if not success:
        log(f"[triage] Failed to write incident to claude_queue",
            level=LOG_ERROR, source="incident-triage")

    # Also publish to Redis for real-time notification
    try:
        cmd = [
            "redis-cli", "-h", "127.0.0.1", "-p", "6379",
            "PUBLISH", "nova:to_claude",
            json.dumps({
                "type": "incident",
                "source": "incident-triage",
                "service": report["service"],
                "content": report["issue"][:500],
                "priority": report.get("priority", 3),
                "ts": time.time(),
            })
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except Exception:
        pass  # Fire-and-forget


# ── Standalone Usage (testing) ───────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test incident triage")
    parser.add_argument("service", help="Service name (e.g. 'Ollama', 'Gateway v2')")
    parser.add_argument("issue", help="Issue description")
    parser.add_argument("--priority", type=int, default=3, help="Priority 1-5")
    parser.add_argument("--dry-run", action="store_true", help="Print report without queuing")
    args = parser.parse_args()

    if args.dry_run:
        report = {
            "service": args.service,
            "issue": args.issue,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "priority": args.priority,
            "log_tail": _get_log_tail(args.service, lines=30),
            "recent_runs": _get_recent_runs(args.service, count=5),
            "related_services": _check_related_services(args.service),
            "suggested_actions": _suggest_fix(args.service, args.issue),
            "bb_attempts": _get_bb_heal_history(args.service),
        }
        print(json.dumps(report, indent=2, default=str))
    else:
        triage_incident(args.service, args.issue, priority=args.priority)
        print("OK — incident triaged and queued")
