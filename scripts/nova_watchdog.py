#!/usr/bin/env python3
"""
nova_watchdog.py — Self-healing infrastructure watchdog.

Monitors critical services and restarts them if down. This is the
watchdog that watches the scheduler, gateway, and system services.

Checks:
  - Unified scheduler (port 37460)
  - OpenClaw gateway (port 18789)
  - Memory server (port 18790)
  - Redis (port 6379)
  - PostgreSQL (port 5432)
  - Ollama (port 11434)
  - Subagent heartbeats (Redis keys)
  - PostgreSQL idle connection cleanup

Runs every 5 minutes via the scheduler. If the scheduler itself is down,
a tiny launchd watchdog plist (com.nova.watchdog) restarts it.

Written by Jordan Koch.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN


def check_port(host, port, timeout=5):
    """Check if a service is responding on a port."""
    try:
        url = f"http://{host}:{port}/health"
        resp = urllib.request.urlopen(url, timeout=timeout)
        return resp.status == 200
    except Exception:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            return False


def restart_launchd(label):
    """Restart a launchd service."""
    uid = os.getuid()
    try:
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                       capture_output=True, timeout=15)
        log(f"Restarted {label}", level=LOG_INFO, source="watchdog")
        return True
    except Exception as e:
        # Fallback: stop + start
        try:
            subprocess.run(["launchctl", "stop", label], capture_output=True, timeout=5)
            time.sleep(2)
            subprocess.run(["launchctl", "start", label], capture_output=True, timeout=5)
            return True
        except Exception:
            log(f"Failed to restart {label}: {e}", level=LOG_ERROR, source="watchdog")
            return False


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def check_scheduler():
    """Verify scheduler is running and healthy."""
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:37460/status", timeout=5)
        data = json.loads(resp.read())
        if data.get("status") == "running":
            return True, data
    except Exception:
        pass
    return False, {}


def check_subagent_heartbeats():
    """Check Redis heartbeats for subagents."""
    try:
        import redis
        r = redis.from_url("redis://localhost:6379", decode_responses=True)
        agents = ["analyst", "coder", "lookout", "librarian", "sentinel"]
        stale = []
        for name in agents:
            status = r.get(f"nova:agent:{name}:status")
            if status != "running":
                stale.append(name)
        return stale
    except Exception:
        return []


def cleanup_postgres_idle():
    """Kill PostgreSQL connections idle for more than 2 hours."""
    try:
        result = subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_memories", "-tAc",
             "SELECT count(*) FROM pg_stat_activity WHERE state='idle' "
             "AND query_start < NOW() - INTERVAL '2 hours' AND pid != pg_backend_pid();"],
            capture_output=True, text=True, timeout=10
        )
        idle_count = int(result.stdout.strip() or "0")
        if idle_count > 3:
            subprocess.run(
                ["psql", "-U", "kochj", "-d", "nova_memories", "-c",
                 "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                 "WHERE state='idle' AND query_start < NOW() - INTERVAL '2 hours' "
                 "AND pid != pg_backend_pid();"],
                capture_output=True, timeout=10
            )
            log(f"Cleaned {idle_count} idle PG connections", level=LOG_INFO, source="watchdog")
    except Exception:
        pass


def main():
    issues = []
    fixes = []

    # Check critical services
    # Canonical launchd labels — verified 2026-04-22
    services = [
        ("Scheduler", "127.0.0.1", 37460, "com.nova.scheduler"),
        ("Gateway", "127.0.0.1", 18789, "ai.openclaw.gateway"),
        ("Memory Server", "127.0.0.1", 18790, "net.digitalnoise.nova-memory-server"),
        ("Ollama", "127.0.0.1", 11434, None),  # Managed by Ollama.app
        ("OpenWebUI", "127.0.0.1", 3000, "net.digitalnoise.openwebui"),
        ("TinyChat", "127.0.0.1", 8000, "net.digitalnoise.tinychat"),
    ]

    for name, host, port, label in services:
        if not check_port(host, port):
            issues.append(f"{name} (:{port}) DOWN")
            if label:
                if restart_launchd(label):
                    fixes.append(f"Restarted {name}")
                    time.sleep(10)  # Wait for restart
                    if not check_port(host, port):
                        # launchd restart failed — try nohup wrapper for gateway
                        if "gateway" in label.lower():
                            try:
                                subprocess.Popen(
                                    ["/bin/zsh", str(Path.home() / ".openclaw/scripts/nova_gateway_start.sh")],
                                    stdout=open(str(Path.home() / ".openclaw/logs/gateway.log"), "a"),
                                    stderr=open(str(Path.home() / ".openclaw/logs/gateway.err.log"), "a"),
                                    start_new_session=True,
                                )
                                fixes[-1] = f"Restarted {name} via wrapper"
                            except Exception:
                                fixes[-1] = f"FAILED to restart {name}"
                else:
                    fixes.append(f"FAILED to restart {name}")

    # Check Redis
    try:
        import redis
        r = redis.from_url("redis://localhost:6379")
        r.ping()
    except Exception:
        issues.append("Redis DOWN")
        restart_launchd("net.digitalnoise.redis")
        fixes.append("Restarted Redis")

    # Check PostgreSQL
    try:
        result = subprocess.run(
            ["pg_isready"], capture_output=True, timeout=5
        )
        if result.returncode != 0:
            issues.append("PostgreSQL DOWN")
            restart_launchd("homebrew.mxcl.postgresql@17")
            fixes.append("Restarted PostgreSQL")
    except Exception:
        issues.append("PostgreSQL check failed")

    # Check subagent heartbeats
    stale_agents = check_subagent_heartbeats()
    for agent in stale_agents:
        issues.append(f"Subagent {agent} stale")
        try:
            subprocess.run(
                ["/bin/zsh", str(Path.home() / ".openclaw/scripts/nova_subagent_ctl.sh"),
                 "restart", agent],
                capture_output=True, timeout=15
            )
            fixes.append(f"Restarted subagent {agent}")
        except Exception:
            fixes.append(f"FAILED to restart subagent {agent}")

    # Cleanup idle PG connections
    cleanup_postgres_idle()

    # Report
    if issues:
        msg = ":wrench: *Watchdog Report*\n"
        msg += "\n".join(f"  :red_circle: {i}" for i in issues)
        if fixes:
            msg += "\n" + "\n".join(f"  :gear: {f}" for f in fixes)
        slack_post(msg)
        log(f"Issues: {issues}, Fixes: {fixes}", level=LOG_WARN, source="watchdog")
    else:
        log("All services healthy", level=LOG_INFO, source="watchdog")


if __name__ == "__main__":
    main()
