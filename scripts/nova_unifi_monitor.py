#!/usr/bin/env python3
"""
nova_unifi_monitor.py — UniFi network monitoring via UDM Pro API.

Read-only access via API key stored in macOS Keychain.
Checks network health, device status, client issues, and alerts.
Posts problems to Slack, stores status in vector memory.

PRIVACY: All network intents are PRIVATE — local only, never OpenRouter.

Usage:
  python3 nova_unifi_monitor.py                    # Full health check
  python3 nova_unifi_monitor.py --status           # Quick status summary
  python3 nova_unifi_monitor.py --clients          # List all connected clients
  python3 nova_unifi_monitor.py --devices          # List all UniFi devices
  python3 nova_unifi_monitor.py --alerts           # Show active alerts
  python3 nova_unifi_monitor.py --dpi              # Traffic by category
  python3 nova_unifi_monitor.py --problems         # Only show problems

Written by Jordan Koch.
"""

import json
import ssl
import subprocess
import sys
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()

UDM_HOST = "https://192.168.1.1"
UDM_API = f"{UDM_HOST}/proxy/network/api/s/default"
STATE_FILE = Path.home() / ".openclaw/workspace/state/nova_unifi_state.json"

# Create SSL context that doesn't verify (UDM Pro uses self-signed cert)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def log(msg):
    print(f"[nova_unifi {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_api_key():
    """Load API key from macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova",
             "-s", "nova-unifi-api-key", "-w"],
            capture_output=True, text=True
        )
        key = result.stdout.strip()
        if key:
            return key
    except Exception:
        pass
    log("ERROR: UniFi API key not found in Keychain")
    log("Run: security add-generic-password -a nova -s nova-unifi-api-key -w YOUR_KEY")
    return None


def api_get(endpoint):
    """Make a GET request to the UDM Pro API."""
    api_key = get_api_key()
    if not api_key:
        return None
    url = f"{UDM_API}/{endpoint}"
    req = urllib.request.Request(url, headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
            data = json.loads(r.read())
            if data.get("meta", {}).get("rc") == "ok":
                return data.get("data", [])
    except Exception as e:
        log(f"API error ({endpoint}): {e}")
    return None


def slack_post(text, channel=None):
    data = json.dumps({
        "channel": channel or SLACK_CHAN, "text": text, "mrkdwn": True
    }).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "infrastructure",
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            f"{VECTOR_URL}?async=1", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


# ── Health check ─────────────────────────────────────────────────────────────

def get_health():
    """Get overall network health."""
    data = api_get("stat/health")
    if not data:
        return None
    health = {}
    for subsys in data:
        name = subsys.get("subsystem", "?")
        status = subsys.get("status", "unknown")
        health[name] = {
            "status": status,
            "num_adopted": subsys.get("num_adopted", 0),
            "num_ap": subsys.get("num_ap", 0),
            "num_sw": subsys.get("num_sw", 0),
            "num_sta": subsys.get("num_sta", 0),
            "tx_bytes": subsys.get("tx_bytes-r", 0),
            "rx_bytes": subsys.get("rx_bytes-r", 0),
            "latency": subsys.get("latency", 0),
            "uptime": subsys.get("uptime", 0),
            "drops": subsys.get("drops", 0),
            "xput_down": subsys.get("xput_down", 0),
            "xput_up": subsys.get("xput_up", 0),
            "speedtest_lastrun": subsys.get("speedtest_lastrun", 0),
        }
    return health


def get_devices():
    """Get all UniFi devices (APs, switches, gateway)."""
    return api_get("stat/device") or []


def get_clients():
    """Get all connected clients."""
    return api_get("stat/sta") or []


def get_alerts():
    """Get active alerts."""
    return api_get("rest/alarm") or []


def get_dpi():
    """Get DPI (Deep Packet Inspection) traffic stats."""
    return api_get("stat/dpi") or []


# ── Analysis ─────────────────────────────────────────────────────────────────

def find_problems(health, devices, clients):
    """Analyze network for problems."""
    problems = []

    # Health subsystem problems
    if health:
        for name, info in health.items():
            if info["status"] != "ok":
                problems.append({
                    "severity": "high",
                    "category": "health",
                    "message": f"{name} subsystem status: {info['status']}",
                })
            if name == "wan" and info.get("latency", 0) > 50:
                problems.append({
                    "severity": "medium",
                    "category": "wan",
                    "message": f"WAN latency: {info['latency']}ms (>50ms threshold)",
                })

    # Device problems
    for dev in devices:
        name = dev.get("name", dev.get("model", "Unknown"))
        state = dev.get("state", 0)
        if state != 1:  # 1 = connected/adopted
            problems.append({
                "severity": "high",
                "category": "device",
                "message": f"Device '{name}' state: {state} (not connected)",
            })

        # Check for high CPU/memory
        sys_stats = dev.get("system-stats", {})
        cpu = float(sys_stats.get("cpu", "0"))
        mem = float(sys_stats.get("mem", "0"))
        if cpu > 80:
            problems.append({
                "severity": "medium",
                "category": "device",
                "message": f"Device '{name}' CPU: {cpu:.0f}%",
            })
        if mem > 85:
            problems.append({
                "severity": "medium",
                "category": "device",
                "message": f"Device '{name}' memory: {mem:.0f}%",
            })

        # Check uplink errors
        uplink = dev.get("uplink", {})
        drops = uplink.get("drops", 0)
        if drops > 100:
            problems.append({
                "severity": "low",
                "category": "uplink",
                "message": f"Device '{name}' uplink drops: {drops}",
            })

        # Check for poor wireless signal (APs)
        if dev.get("type") == "uap":
            for radio in dev.get("radio_table_stats", []):
                satisfaction = radio.get("satisfaction", 100)
                if satisfaction < 50:
                    channel = radio.get("channel", "?")
                    problems.append({
                        "severity": "medium",
                        "category": "wireless",
                        "message": f"AP '{name}' channel {channel} satisfaction: {satisfaction}%",
                    })

    # Client problems
    poor_signal_count = 0
    for client in clients:
        signal = client.get("signal", 0)
        if signal != 0 and signal < -80:  # Very weak signal
            poor_signal_count += 1

    if poor_signal_count > 3:
        problems.append({
            "severity": "low",
            "category": "clients",
            "message": f"{poor_signal_count} clients with poor signal (<-80 dBm)",
        })

    return problems


# ── Output formatters ────────────────────────────────────────────────────────

def format_status(health):
    """Quick status one-liner."""
    if not health:
        return "Unable to reach UDM Pro"
    statuses = [f"{k}: {v['status']}" for k, v in health.items()]
    return " | ".join(statuses)


def format_health_report(health, devices, clients, problems):
    """Full health report for Slack."""
    lines = [f"*UniFi Network Health — {NOW.strftime('%I:%M %p')}*"]

    if health:
        wan = health.get("wan", {})
        wlan = health.get("wlan", {})
        lan = health.get("lan", {})

        lines.append(f"  WAN: {wan.get('status', '?')} (latency: {wan.get('latency', '?')}ms)")
        if wan.get("xput_down"):
            lines.append(f"  Speed: {wan['xput_down']:.0f} down / {wan.get('xput_up', 0):.0f} up Mbps")
        lines.append(f"  WLAN: {wlan.get('status', '?')} ({wlan.get('num_sta', '?')} clients)")
        lines.append(f"  LAN: {lan.get('status', '?')}")

    lines.append(f"  Devices: {len(devices)} | Clients: {len(clients)}")

    if problems:
        lines.append("")
        high = [p for p in problems if p["severity"] == "high"]
        med = [p for p in problems if p["severity"] == "medium"]
        low = [p for p in problems if p["severity"] == "low"]

        if high:
            lines.append("*Problems:*")
            for p in high:
                lines.append(f"  !! {p['message']}")
        if med:
            for p in med:
                lines.append(f"  ! {p['message']}")
        if low:
            for p in low:
                lines.append(f"  {p['message']}")
    else:
        lines.append("  _No problems detected._")

    return "\n".join(lines)


def format_devices(devices):
    """Format device list."""
    lines = [f"*UniFi Devices ({len(devices)})*"]
    for dev in sorted(devices, key=lambda d: d.get("type", "")):
        name = dev.get("name", dev.get("model", "?"))
        dtype = dev.get("type", "?")
        ip = dev.get("ip", "?")
        version = dev.get("version", "?")
        uptime = dev.get("uptime", 0)
        days = uptime // 86400
        sys_stats = dev.get("system-stats", {})
        cpu = sys_stats.get("cpu", "?")
        mem = sys_stats.get("mem", "?")
        lines.append(f"  {name} ({dtype}) — {ip} — v{version} — {days}d up — CPU {cpu}% MEM {mem}%")
    return "\n".join(lines)


def format_clients(clients):
    """Format client list."""
    lines = [f"*Connected Clients ({len(clients)})*"]
    for c in sorted(clients, key=lambda c: c.get("hostname", c.get("name", "zzz"))):
        name = c.get("hostname", c.get("name", c.get("oui", "Unknown")))
        ip = c.get("ip", "?")
        signal = c.get("signal", "")
        signal_str = f" ({signal} dBm)" if signal else " (wired)"
        tx = c.get("tx_bytes", 0) / 1024 / 1024
        rx = c.get("rx_bytes", 0) / 1024 / 1024
        lines.append(f"  {name} — {ip}{signal_str} — TX {tx:.0f}MB RX {rx:.0f}MB")
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def full_check():
    """Run full health check, post problems to Slack, store in memory."""
    log("Running UniFi network health check...")

    health = get_health()
    devices = get_devices()
    clients = get_clients()

    if health is None:
        log("Could not reach UDM Pro API")
        slack_post("*UniFi Monitor*\n  Unable to reach UDM Pro at 192.168.1.1")
        return

    problems = find_problems(health, devices, clients)

    log(f"Health: {format_status(health)}")
    log(f"Devices: {len(devices)} | Clients: {len(clients)} | Problems: {len(problems)}")

    # Post to Slack if there are problems
    if problems:
        report = format_health_report(health, devices, clients, problems)
        slack_post(report)

    # Store in memory
    wan = health.get("wan", {})
    summary = (
        f"Network health check {TODAY} {NOW.strftime('%H:%M')}: "
        f"WAN {wan.get('status', '?')} ({wan.get('latency', '?')}ms), "
        f"{len(devices)} devices, {len(clients)} clients, "
        f"{len(problems)} problems"
    )
    vector_remember(summary, {"date": TODAY, "type": "network_health"})

    if not problems:
        log("All clear.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova UniFi Monitor")
    parser.add_argument("--status", action="store_true", help="Quick status")
    parser.add_argument("--clients", action="store_true", help="List clients")
    parser.add_argument("--devices", action="store_true", help="List devices")
    parser.add_argument("--alerts", action="store_true", help="Show alerts")
    parser.add_argument("--problems", action="store_true", help="Only problems")
    parser.add_argument("--full", action="store_true", help="Full report to Slack")
    args = parser.parse_args()

    if args.status:
        health = get_health()
        print(format_status(health) if health else "Cannot reach UDM Pro")
    elif args.clients:
        clients = get_clients()
        print(format_clients(clients))
    elif args.devices:
        devices = get_devices()
        print(format_devices(devices))
    elif args.alerts:
        alerts = get_alerts()
        if alerts:
            for a in alerts[:20]:
                print(f"  [{a.get('datetime', '?')}] {a.get('msg', '?')}")
        else:
            print("No active alerts.")
    elif args.problems:
        health = get_health()
        devices = get_devices()
        clients = get_clients()
        problems = find_problems(health, devices, clients)
        if problems:
            for p in problems:
                print(f"  [{p['severity']}] {p['message']}")
        else:
            print("No problems detected.")
    elif args.full:
        full_check()
    else:
        full_check()
