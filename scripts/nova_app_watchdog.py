#!/usr/bin/env python3
"""
nova_app_watchdog.py — Proactive app health monitoring with auto-restart.

Pings all known Nova API app ports. If an app was previously running and
stops responding, it:
  1. Alerts Jordan on Slack
  2. Attempts auto-restart via `open -a AppName`
  3. Verifies the restart worked
  4. Logs everything to vector memory

State is tracked so it only alerts on transitions (up → down, down → up),
not on every check cycle.

Cron: every 5 min
Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()
STATE_FILE = Path.home() / ".openclaw/workspace/state/nova_app_watchdog_state.json"

# Apps to monitor: (port, app_name, bundle_name_for_open, critical)
# critical=True means auto-restart, critical=False means alert-only
MONITORED_APPS = [
    (37400, "NovaControl",      "NovaControl",      True),
    (37422, "MLXCode",          "MLX Code",         False),
    (37423, "NMAPScanner",      "NMAPScanner",      False),
    (37424, "RsyncGUI",         "RsyncGUI",         False),
    (37443, "TopGUI",           "TopGUI",            False),
    (37445, "ytdlp-gui",        "ytdlp-gui",        False),
    (37446, "DotSync",          "Dot Sync",          False),
]

# Infrastructure services monitored by App Watchdog.
# NOTE: Memory Server (18790), Gateway (18789/18792), PostgreSQL, Redis are
# handled exclusively by Big Brother which has proper dependency-aware restart
# logic. Duplicating them here causes double-alerts. Only monitor services
# that BB does NOT restart (Ollama is managed by the app, not launchd).
INFRA_SERVICES = [
    (11434, "Ollama", "open -a Ollama"),
]

# Require consecutive down checks before alerting (prevents flapping on brief blips)
INFRA_CONFIRM_CHECKS = 2  # Must be down for 2 consecutive checks (10 min) before alerting

# Cooldown: don't re-alert for same app within this many seconds
ALERT_COOLDOWN = 600  # 10 minutes
# Don't auto-restart more than this many times per hour
MAX_RESTARTS_PER_HOUR = 3


def log(msg):
    print(f"[nova_app_watchdog {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text, channel=None):
    nova_config.post_both(text, slack_channel=channel or nova_config.SLACK_NOTIFY)


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "app_watchdog", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"apps": {}, "restarts": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def check_port(port, timeout=3):
    """Check if an app is responding on a port. Returns (alive, status_info, elapsed_s)."""
    start = time.time()
    try:
        url = f"http://127.0.0.1:{port}/api/status"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            version = data.get("version", data.get("app", "ok"))
            return True, str(version), time.time() - start
    except urllib.error.URLError:
        return False, "connection refused", time.time() - start
    except Exception as e:
        # Port is open but /api/status may not exist — still alive
        try:
            url = f"http://127.0.0.1:{port}/"
            urllib.request.urlopen(url, timeout=timeout)
            return True, "responding (no status endpoint)", time.time() - start
        except urllib.error.HTTPError:
            # Got an HTTP response (even 404) — port is alive
            return True, "responding", time.time() - start
        except Exception:
            return False, str(e), time.time() - start


def check_infra_port(port, timeout=3):
    """Check infrastructure service ports (may not have /api/status)."""
    # Service-to-host mapping: some services bind to LAN IP, not loopback
    hosts = {
        11434: "127.0.0.1",
    }
    endpoints = {
        11434: "/api/version",
    }
    host     = hosts.get(port, "127.0.0.1")
    endpoint = endpoints.get(port, "/")
    try:
        url = f"http://{host}:{port}{endpoint}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, "ok"
    except urllib.error.HTTPError:
        return True, "responding"
    except Exception:
        return False, "down"


def capture_diagnostics(port, app_name):
    """Capture diagnostic info before restart for post-mortem analysis."""
    diag_dir = Path.home() / ".openclaw/logs/diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    diag_file = diag_dir / f"{app_name}_{timestamp}.txt"

    lines = [f"Pre-restart diagnostic for {app_name} (port {port})", f"Time: {timestamp}", ""]

    # Check what's on the port
    try:
        result = subprocess.run(["lsof", "-i", f":{port}"], capture_output=True, text=True, timeout=5)
        lines.append("=== lsof ===")
        lines.append(result.stdout or "(empty)")
    except Exception:
        pass

    # Check process status
    try:
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        app_procs = [l for l in result.stdout.splitlines() if app_name.lower() in l.lower()]
        lines.append("=== processes ===")
        lines.extend(app_procs or ["(no matching processes)"])
    except Exception:
        pass

    # Check memory pressure
    try:
        result = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=5)
        lines.append("=== memory_pressure ===")
        lines.append(result.stdout[:200] if result.stdout else "(empty)")
    except Exception:
        pass

    diag_file.write_text("\n".join(lines))
    return str(diag_file)


def restart_app(app_name, bundle_name):
    """Attempt to restart a macOS app via `open -a`."""
    try:
        log(f"Attempting restart: open -a '{bundle_name}'")
        result = subprocess.run(
            ["open", "-a", bundle_name],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            log(f"Restart command sent for {app_name}")
            return True
        else:
            log(f"Restart failed: {result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"Restart exception: {e}")
        return False


def restart_infra(name, command):
    """Attempt to restart an infrastructure service."""
    try:
        log(f"Restarting infra: {name} via: {command}")
        import shlex
        result = subprocess.run(
            shlex.split(command) if isinstance(command, str) else command,
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except Exception as e:
        log(f"Infra restart failed: {e}")
        return False


def count_recent_restarts(state):
    """Count restarts in the last hour."""
    cutoff = time.time() - 3600
    recent = [r for r in state.get("restarts", []) if r.get("ts", 0) > cutoff]
    state["restarts"] = recent  # prune old entries
    return len(recent)


def main():
    log("Running app watchdog check...")
    state = load_state()
    apps_state = state.get("apps", {})
    now_ts = time.time()
    alerts = []
    recoveries = []

    # ── Check macOS apps ─────────────────────────────────────────────────────
    for port, app_name, bundle_name, critical in MONITORED_APPS:
        alive, info, elapsed = check_port(port)
        key = str(port)

        # OneOnOne preemptive restart: if alive but responding very slowly (>2s),
        # the HTTP server is likely hanging — restart before it goes fully dead.
        if port == 37400 and alive and elapsed > 2.0:
            log(f"OneOnOne responding slowly ({elapsed:.1f}s) — preemptive restart")
            diag_path = capture_diagnostics(port, app_name)
            log(f"Diagnostics captured: {diag_path}")
            if count_recent_restarts(state) < MAX_RESTARTS_PER_HOUR:
                restart_app(app_name, bundle_name)
                state.setdefault("restarts", []).append({
                    "ts": now_ts, "app": app_name, "success": True, "reason": "slow_response"
                })
                alerts.append(f"*{app_name}* (:{port}) responding slowly ({elapsed:.1f}s) — *preemptive restart*")
                vector_remember(
                    f"{app_name} preemptively restarted on {TODAY} at {NOW.strftime('%H:%M')} "
                    f"due to slow response ({elapsed:.1f}s)",
                    {"date": TODAY, "type": "preemptive_restart", "app": app_name}
                )
                # Skip normal alive/down handling — we already dealt with it
                apps_state[key] = {"alive": True, "info": "preemptive restart", "last_seen": now_ts, "last_alert": 0}
                continue
        prev = apps_state.get(key, {})
        was_alive = prev.get("alive", None)
        last_alert = prev.get("last_alert", 0)

        if alive:
            if was_alive is False:
                # Recovery! Was down, now up
                recoveries.append(f"*{app_name}* (:{port}) is back up — {info}")
                vector_remember(
                    f"{app_name} recovered on {TODAY} at {NOW.strftime('%H:%M')}",
                    {"date": TODAY, "type": "app_recovery", "app": app_name}
                )
            apps_state[key] = {"alive": True, "info": info, "last_seen": now_ts, "last_alert": 0}
        else:
            # App is down
            if was_alive is not False and (now_ts - last_alert) > ALERT_COOLDOWN:
                # Transition to down or cooldown expired — alert
                alerts.append(f"*{app_name}* (:{port}) is DOWN — {info}")

                # Auto-restart if critical and we haven't exhausted restart budget
                restarted = False
                if critical and count_recent_restarts(state) < MAX_RESTARTS_PER_HOUR:
                    diag_path = capture_diagnostics(port, app_name)
                    log(f"Diagnostics captured: {diag_path}")
                    restarted = restart_app(app_name, bundle_name)
                    state.setdefault("restarts", []).append({
                        "ts": now_ts, "app": app_name, "success": restarted
                    })
                    if restarted:
                        alerts[-1] += " — *auto-restart attempted*"
                        # Wait a moment and re-check
                        time.sleep(5)
                        alive_now, _, _ = check_port(port, timeout=5)
                        if alive_now:
                            alerts[-1] += " (confirmed back up)"
                        else:
                            alerts[-1] += " (still starting...)"

                vector_remember(
                    f"{app_name} went down on {TODAY} at {NOW.strftime('%H:%M')}. "
                    f"Auto-restart: {'yes' if restarted else 'no'}",
                    {"date": TODAY, "type": "app_crash", "app": app_name}
                )
                apps_state[key] = {"alive": False, "info": info, "last_seen": prev.get("last_seen", 0),
                                   "last_alert": now_ts}
            else:
                # Already alerted recently, just update state
                apps_state[key] = {**prev, "alive": False, "info": info}

    # ── Check infrastructure services ────────────────────────────────────────
    for port, name, restart_cmd in INFRA_SERVICES:
        alive, info = check_infra_port(port)
        key = f"infra_{port}"
        prev = apps_state.get(key, {})
        was_alive = prev.get("alive", None)
        last_alert = prev.get("last_alert", 0)
        down_checks = prev.get("down_checks", 0)

        if alive:
            if was_alive is False:
                recoveries.append(f"*{name}* (:{port}) is back up")
            apps_state[key] = {"alive": True, "last_seen": now_ts, "last_alert": 0, "down_checks": 0}
        else:
            down_checks += 1
            if down_checks >= INFRA_CONFIRM_CHECKS and (now_ts - last_alert) > ALERT_COOLDOWN:
                alerts.append(f"*{name}* (:{port}) is DOWN")
                if count_recent_restarts(state) < MAX_RESTARTS_PER_HOUR:
                    restarted = restart_infra(name, restart_cmd)
                    state.setdefault("restarts", []).append({
                        "ts": now_ts, "app": name, "success": restarted
                    })
                    if restarted:
                        alerts[-1] += " — *auto-restart attempted*"

                apps_state[key] = {"alive": False, "last_seen": prev.get("last_seen", 0),
                                   "last_alert": now_ts, "down_checks": down_checks}
            else:
                apps_state[key] = {**prev, "alive": False, "down_checks": down_checks}

    # ── Post results ─────────────────────────────────────────────────────────
    state["apps"] = apps_state
    save_state(state)

    if alerts:
        msg = "*App Watchdog Alert*\n" + "\n".join(f"  {a}" for a in alerts)
        if recoveries:
            msg += "\n\n*Recovered:*\n" + "\n".join(f"  {r}" for r in recoveries)
        slack_post(msg)
        log(f"Posted {len(alerts)} alert(s), {len(recoveries)} recovery(s)")
    elif recoveries:
        msg = "*App Watchdog — Recovery*\n" + "\n".join(f"  {r}" for r in recoveries)
        slack_post(msg)
        log(f"Posted {len(recoveries)} recovery(s)")
    else:
        # Count alive
        alive_count = sum(1 for v in apps_state.values() if v.get("alive"))
        total = len(MONITORED_APPS) + len(INFRA_SERVICES)
        log(f"All clear. {alive_count}/{total} services up.")


# ── Status report (for manual checks) ───────────────────────────────────────

def status_report():
    """Print a full status report of all monitored services."""
    print(f"App Watchdog Status — {NOW.strftime('%Y-%m-%d %H:%M')}\n")
    print("macOS Apps:")
    for port, name, _, critical in MONITORED_APPS:
        alive, info, elapsed = check_port(port)
        icon = "UP" if alive else "DOWN"
        crit = " [critical]" if critical else ""
        print(f"  {icon}  {name} (:{port}){crit} — {info} ({elapsed:.1f}s)")

    print("\nInfrastructure:")
    for port, name, _ in INFRA_SERVICES:
        alive, info = check_infra_port(port)
        icon = "UP" if alive else "DOWN"
        print(f"  {icon}  {name} (:{port}) — {info}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova App Watchdog")
    parser.add_argument("--status", action="store_true", help="Print full status report")
    parser.add_argument("--reset", action="store_true", help="Reset state (clear alert history)")
    args = parser.parse_args()

    if args.status:
        status_report()
    elif args.reset:
        STATE_FILE.unlink(missing_ok=True)
        print("State reset.")
    else:
        main()
