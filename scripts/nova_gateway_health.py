#!/usr/bin/env python3
"""
nova_gateway_health.py — Hourly gateway health check + auto-repair.

Checks:
1. Gateway process alive on port 18789
2. Slack socket mode connected
3. Discord websocket connected
4. Signal daemon running
5. Workspace MD files under bootstrap budget (100K total, no single file over 5K)

If channels are disconnected, restarts the gateway automatically.
Posts detailed diagnostics to #nova-notifications.

Written by Jordan Koch.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

GATEWAY_PORT = 18789
GATEWAY_LOG = "/tmp/nova-gateway.log"
WORKSPACE_DIR = Path.home() / ".openclaw/workspace"
MAX_TOTAL_WORKSPACE = 100000  # 100K chars total
MAX_SINGLE_FILE = 5000  # 5K per file
BOOTSTRAP_BUDGET = 100000

NODE_BIN = "/opt/homebrew/opt/node/bin/node"
GATEWAY_ENTRY = "/opt/homebrew/lib/node_modules/openclaw/dist/entry.js"

LOG_FILE = "/tmp/nova-gateway-health.log"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[gw-health {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def notify(text):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception:
        pass


# ── Gateway Health Checks ─────────────────────────────────────────────────────

def check_gateway_process() -> bool:
    """Check if gateway is listening on port."""
    result = subprocess.run(
        ["lsof", "-i", f":{GATEWAY_PORT}", "-P", "-n"],
        capture_output=True, text=True,
    )
    return "LISTEN" in result.stdout


def check_gateway_health() -> dict:
    """Check gateway /health endpoint."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{GATEWAY_PORT}/health", timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return {"ok": False, "status": "unreachable"}


def check_channel_status() -> dict:
    """Parse gateway log for channel connection status."""
    status = {"slack": "unknown", "discord": "unknown", "signal": "unknown"}

    if not Path(GATEWAY_LOG).exists():
        return status

    # Read last 100 lines of gateway log
    result = subprocess.run(
        ["tail", "-100", GATEWAY_LOG],
        capture_output=True, text=True,
    )
    lines = result.stdout

    # Check for recent disconnect/connect events (last state wins)
    for line in lines.split("\n"):
        # Strip ANSI codes
        import re
        clean = re.sub(r'\x1b\[[0-9;]*m', '', line)

        if "slack" in clean.lower():
            if "socket mode connected" in clean:
                status["slack"] = "connected"
            elif "socket disconnected" in clean or "http request failed" in clean:
                status["slack"] = "disconnected"

        if "discord" in clean.lower():
            if "channels resolved" in clean or "client initialized" in clean:
                status["discord"] = "connected"
            elif "Gateway websocket closed" in clean or "ENOTFOUND" in clean:
                status["discord"] = "disconnected"

        if "signal" in clean.lower():
            if "Started HTTP server" in clean or "DaemonCommand" in clean:
                status["signal"] = "connected"
            elif "Connection closed unexpectedly" in clean:
                status["signal"] = "disconnected"

    return status


def get_gateway_uptime() -> str:
    """Get how long the gateway has been running."""
    result = subprocess.run(
        ["ps", "-o", "etime=", "-p", str(get_gateway_pid())],
        capture_output=True, text=True,
    )
    return result.stdout.strip() or "unknown"


def get_gateway_pid() -> int:
    result = subprocess.run(
        ["lsof", "-i", f":{GATEWAY_PORT}", "-t"],
        capture_output=True, text=True,
    )
    pids = result.stdout.strip().split("\n")
    return int(pids[0]) if pids and pids[0] else 0


# ── Gateway Restart ───────────────────────────────────────────────────────────

def restart_gateway() -> bool:
    """Kill and restart the gateway with Keychain secrets."""
    log("Restarting gateway...")

    # Kill existing
    pid = get_gateway_pid()
    if pid:
        subprocess.run(["kill", str(pid)], capture_output=True)
        time.sleep(3)

    # Load secrets
    env = os.environ.copy()
    secrets = {
        "NOVA_OPENROUTER_API_KEY": "nova-openrouter-api-key",
        "NOVA_SLACK_BOT_TOKEN": "nova-slack-bot-token",
        "NOVA_SLACK_APP_TOKEN": "nova-slack-app-token",
        "NOVA_GATEWAY_AUTH_TOKEN": "nova-gateway-auth-token",
        "NOVA_DISCORD_TOKEN": "nova-discord-token",
    }
    for env_var, keychain_svc in secrets.items():
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova", "-s", keychain_svc, "-w"],
            capture_output=True, text=True,
        )
        val = result.stdout.strip()
        if val:
            env[env_var] = val
        else:
            log(f"WARNING: Keychain entry {keychain_svc} not found")

    # Start gateway
    with open(GATEWAY_LOG, "w") as log_file:
        subprocess.Popen(
            [NODE_BIN, GATEWAY_ENTRY, "gateway", "--port", str(GATEWAY_PORT)],
            stdout=log_file, stderr=log_file,
            env=env, start_new_session=True,
        )

    # Wait for startup
    time.sleep(15)
    return check_gateway_process()


# ── Workspace Size Management ─────────────────────────────────────────────────

def check_workspace_sizes() -> list[str]:
    """Check workspace MD files are within budget. Trim oversized ones."""
    issues = []
    total_size = 0
    archive_dir = WORKSPACE_DIR / "archive"
    archive_dir.mkdir(exist_ok=True)

    for md_file in sorted(WORKSPACE_DIR.glob("*.md")):
        size = md_file.stat().st_size
        total_size += size

        if size > MAX_SINGLE_FILE:
            # Trim the file to MAX_SINGLE_FILE
            content = md_file.read_text()
            if len(content) > MAX_SINGLE_FILE:
                # Keep first MAX_SINGLE_FILE chars, add truncation note
                trimmed = content[:MAX_SINGLE_FILE - 100] + f"\n\n<!-- Trimmed from {len(content)} to {MAX_SINGLE_FILE} chars by gateway health -->\n"
                md_file.write_text(trimmed)
                issues.append(f"Trimmed {md_file.name}: {size} → {MAX_SINGLE_FILE} chars")
                log(f"Trimmed {md_file.name}: {size} → {MAX_SINGLE_FILE}")

    if total_size > MAX_TOTAL_WORKSPACE:
        issues.append(f"Total workspace: {total_size:,} chars (budget: {MAX_TOTAL_WORKSPACE:,})")
        # Move smallest non-essential files to archive
        files_by_size = sorted(WORKSPACE_DIR.glob("*.md"), key=lambda f: f.stat().st_size)
        essential = {"MEMORY.md", "IDENTITY.md", "TOOLS.md", "SOUL.md", "USER.md", "STATUS.md", "BOOT.md", "AGENTS.md", "USER_PROFILE.md", "ALERT.md"}
        for f in files_by_size:
            if f.name not in essential and total_size > MAX_TOTAL_WORKSPACE:
                f.rename(archive_dir / f.name)
                total_size -= f.stat().st_size if f.exists() else 0
                issues.append(f"Archived {f.name} (non-essential, over budget)")
                log(f"Archived {f.name}")

    return issues


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=== Gateway Health Check ===")

    # 1. Check workspace sizes
    ws_issues = check_workspace_sizes()

    # 2. Check gateway process
    process_alive = check_gateway_process()
    if not process_alive:
        log("Gateway process NOT running — restarting...")
        notify(":red_circle: *Gateway DOWN* — process not found on port 18789. Restarting...")
        success = restart_gateway()
        if success:
            log("Gateway restarted successfully")
            notify(":white_check_mark: *Gateway Restarted* — process recovered")
        else:
            log("FATAL: Gateway restart failed")
            notify(":x: *Gateway Restart FAILED* — manual intervention needed")
        return

    # 3. Check health endpoint
    health = check_gateway_health()
    if not health.get("ok"):
        log(f"Gateway health check failed: {health}")

    # 4. Check channel connectivity
    channels = check_channel_status()
    uptime = get_gateway_uptime()
    disconnected = [ch for ch, st in channels.items() if st == "disconnected"]

    log(f"Channels: {channels}")
    log(f"Uptime: {uptime}")

    if disconnected:
        log(f"Disconnected channels: {disconnected} — restarting gateway...")
        notify(
            f":warning: *Gateway Channel Disconnect Detected*\n"
            f"• Slack: {channels['slack']}\n"
            f"• Discord: {channels['discord']}\n"
            f"• Signal: {channels['signal']}\n"
            f"• Uptime: {uptime}\n"
            f"• Action: Restarting gateway..."
        )
        success = restart_gateway()
        if success:
            time.sleep(10)
            new_channels = check_channel_status()
            notify(
                f":white_check_mark: *Gateway Restarted*\n"
                f"• Slack: {new_channels['slack']}\n"
                f"• Discord: {new_channels['discord']}\n"
                f"• Signal: {new_channels['signal']}"
            )
        else:
            notify(":x: *Gateway Restart FAILED* — manual intervention needed")
    else:
        log("All channels healthy")

    # 5. Report workspace issues if any
    if ws_issues:
        notify(
            f":file_folder: *Workspace Size Management*\n"
            + "\n".join(f"• {i}" for i in ws_issues)
        )


if __name__ == "__main__":
    main()
