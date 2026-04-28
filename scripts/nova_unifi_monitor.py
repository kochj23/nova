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
  python3 nova_unifi_monitor.py --rogue            # Detect unknown devices
  python3 nova_unifi_monitor.py --rogue-learn      # Learn all current devices as known
  python3 nova_unifi_monitor.py --wan-history      # Show WAN uptime/outage history
  python3 nova_unifi_monitor.py --wifi-optimize    # WiFi optimization analysis
  python3 nova_unifi_monitor.py --presence         # Who's home (family presence)
  python3 nova_unifi_monitor.py --firmware         # Firmware version check
  python3 nova_unifi_monitor.py --ports            # Switch port utilization
  python3 nova_unifi_monitor.py --snapshot         # Save daily network snapshot
  python3 nova_unifi_monitor.py --trends           # 7-day trend comparison
  python3 nova_unifi_monitor.py --who-home         # JSON presence for HomeKit

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

VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()

UDM_HOST = "https://192.168.1.1"
UDM_API = f"{UDM_HOST}/proxy/network/api/s/default"
STATE_DIR = Path.home() / ".openclaw/workspace/state"
STATE_FILE = STATE_DIR / "nova_unifi_state.json"
KNOWN_DEVICES_FILE = STATE_DIR / "known_network_devices.json"
WAN_HISTORY_FILE = STATE_DIR / "wan_history.json"
PRESENCE_FILE = STATE_DIR / "presence_history.json"
SNAPSHOT_FILE = STATE_DIR / "network_snapshots.json"
PEOPLE_CONFIG_FILE = STATE_DIR / "known_people_devices.json"

# Bandwidth hog threshold (bytes) — 1 GB
BANDWIDTH_HOG_THRESHOLD = 1 * 1024 * 1024 * 1024

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
    nova_config.post_both(text, slack_channel=channel or nova_config.SLACK_NOTIFY)


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
                # -1 means no clients on that radio — not a problem
                if 0 <= satisfaction < 50:
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


# ── State file helpers ──────────────────────────────────────────────────────

def _load_json(path):
    """Load a JSON state file, returning empty dict/list on missing/corrupt."""
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log(f"Warning: corrupt state file {path}: {e}")
    return {}


def _save_json(path, data):
    """Atomically write JSON state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.rename(path)


# ── 1. Rogue device detection ──────────────────────────────────────────────

def rogue_learn():
    """Snapshot all current clients as known devices."""
    clients = get_clients()
    if not clients:
        log("No clients found or API unreachable.")
        return

    known = _load_json(KNOWN_DEVICES_FILE)
    if not isinstance(known, dict):
        known = {}
    existing_count = len(known)

    for c in clients:
        mac = c.get("mac", "").lower()
        if not mac:
            continue
        if mac not in known:
            known[mac] = {
                "name": c.get("hostname", c.get("name", c.get("oui", "Unknown"))),
                "first_seen": NOW.isoformat(),
                "learned_ip": c.get("ip", "?"),
                "oui": c.get("oui", ""),
            }

    _save_json(KNOWN_DEVICES_FILE, known)
    new_count = len(known) - existing_count
    log(f"Known devices: {len(known)} total ({new_count} newly learned)")
    print(f"Known devices file: {KNOWN_DEVICES_FILE}")
    print(f"Total known: {len(known)} | Newly added: {new_count}")


def rogue_check():
    """Compare current clients against known list, alert on unknowns."""
    clients = get_clients()
    if not clients:
        log("No clients found or API unreachable.")
        return

    known = _load_json(KNOWN_DEVICES_FILE)
    if not known:
        log("No known devices file found. Run --rogue-learn first to baseline.")
        print("Run: python3 nova_unifi_monitor.py --rogue-learn")
        return

    # Build AP name lookup from devices
    devices = get_devices()
    ap_map = {}
    for d in devices:
        ap_mac = d.get("mac", "").lower()
        ap_map[ap_mac] = d.get("name", d.get("model", "Unknown AP"))

    rogues = []
    for c in clients:
        mac = c.get("mac", "").lower()
        if mac and mac not in known:
            ap_mac = c.get("ap_mac", "").lower()
            rogues.append({
                "mac": mac,
                "hostname": c.get("hostname", c.get("name", c.get("oui", "Unknown"))),
                "ip": c.get("ip", "?"),
                "ap": ap_map.get(ap_mac, ap_mac or "wired"),
                "oui": c.get("oui", ""),
                "signal": c.get("signal", ""),
            })

    if rogues:
        lines = [f"*Rogue Device Alert — {len(rogues)} unknown device(s)*"]
        for r in rogues:
            sig = f" ({r['signal']} dBm)" if r['signal'] else ""
            lines.append(f"  {r['hostname']} — {r['mac']} — {r['ip']} — AP: {r['ap']}{sig}")
            if r['oui']:
                lines.append(f"    Manufacturer: {r['oui']}")
        msg = "\n".join(lines)
        print(msg)
        slack_post(msg)
        vector_remember(
            f"Rogue devices detected {TODAY}: {len(rogues)} unknown devices on network",
            {"date": TODAY, "type": "rogue_detection", "count": len(rogues)}
        )
    else:
        print(f"All {len(clients)} connected clients are known. No rogues detected.")


# ── 2. WAN outage tracking ─────────────────────────────────────────────────

def wan_log():
    """Log current WAN status and detect transitions."""
    health = get_health()
    if not health:
        log("Cannot reach UDM Pro for WAN check.")
        return

    wan = health.get("wan", {})
    status = wan.get("status", "unknown")
    latency = wan.get("latency", 0)

    history = _load_json(WAN_HISTORY_FILE)
    if not isinstance(history, dict):
        history = {"entries": [], "last_status": None, "last_transition": None}
    if "entries" not in history:
        history["entries"] = []

    entry = {
        "timestamp": NOW.isoformat(),
        "status": status,
        "latency": latency,
        "xput_down": wan.get("xput_down", 0),
        "xput_up": wan.get("xput_up", 0),
    }
    history["entries"].append(entry)

    # Keep last 2000 entries (~7 days at 5-min intervals)
    if len(history["entries"]) > 2000:
        history["entries"] = history["entries"][-2000:]

    # Detect transitions
    prev_status = history.get("last_status")
    if prev_status and prev_status != status:
        transition_time = history.get("last_transition", NOW.isoformat())
        try:
            prev_dt = datetime.fromisoformat(transition_time)
            duration = NOW - prev_dt
            dur_str = str(duration).split(".")[0]  # strip microseconds
        except Exception:
            dur_str = "unknown"

        if status == "ok" and prev_status != "ok":
            msg = f"*WAN Restored* — back online after {dur_str} outage"
            slack_post(msg)
            vector_remember(msg, {"date": TODAY, "type": "wan_restored"})
            log(msg)
        elif status != "ok":
            msg = f"*WAN Down* — internet went offline (was up for {dur_str})"
            slack_post(msg)
            vector_remember(msg, {"date": TODAY, "type": "wan_outage"})
            log(msg)

        history["last_transition"] = NOW.isoformat()

    history["last_status"] = status
    _save_json(WAN_HISTORY_FILE, history)
    return entry


def wan_show_history():
    """Display recent WAN history."""
    history = _load_json(WAN_HISTORY_FILE)
    entries = history.get("entries", [])
    if not entries:
        print("No WAN history recorded yet. Run a health check first.")
        return

    print(f"*WAN History (last {min(50, len(entries))} entries)*")
    for e in entries[-50:]:
        ts = e.get("timestamp", "?")
        if "T" in ts:
            ts = ts.split("T")[1][:8]  # HH:MM:SS
        status = e.get("status", "?")
        latency = e.get("latency", 0)
        marker = " !!" if status != "ok" else ""
        print(f"  {ts} — {status} — {latency}ms{marker}")

    # Show outage summary
    outages = []
    prev = None
    for e in entries:
        if prev and prev.get("status") == "ok" and e.get("status") != "ok":
            outages.append({"start": e["timestamp"]})
        if prev and prev.get("status") != "ok" and e.get("status") == "ok" and outages:
            outages[-1]["end"] = e["timestamp"]
        prev = e

    if outages:
        print(f"\n*Detected Outages: {len(outages)}*")
        for o in outages:
            start = o.get("start", "?")
            end = o.get("end", "ongoing")
            print(f"  {start} → {end}")


# ── 3. Bandwidth hog detection ─────────────────────────────────────────────

def find_bandwidth_hogs(clients):
    """Return list of clients using >1GB in current session."""
    hogs = []
    for c in clients:
        tx = c.get("tx_bytes", 0)
        rx = c.get("rx_bytes", 0)
        total = tx + rx
        if total > BANDWIDTH_HOG_THRESHOLD:
            name = c.get("hostname", c.get("name", c.get("oui", "Unknown")))
            hogs.append({
                "severity": "low",
                "category": "bandwidth",
                "message": (
                    f"Bandwidth hog: {name} — "
                    f"{total / 1024 / 1024 / 1024:.1f} GB "
                    f"(TX {tx / 1024 / 1024:.0f}MB / RX {rx / 1024 / 1024:.0f}MB)"
                ),
            })
    return hogs


# ── 4. WiFi optimization ───────────────────────────────────────────────────

def wifi_optimize():
    """Analyze WiFi and recommend optimizations."""
    devices = get_devices()
    clients = get_clients()

    if not devices:
        log("Cannot reach UDM Pro.")
        return

    aps = [d for d in devices if d.get("type") == "uap"]
    if not aps:
        print("No access points found.")
        return

    # Build AP name map
    ap_map = {}
    for ap in aps:
        ap_mac = ap.get("mac", "").lower()
        ap_map[ap_mac] = ap.get("name", ap.get("model", "Unknown AP"))

    # Clients per AP
    ap_clients = {}
    for c in clients:
        ap_mac = c.get("ap_mac", "").lower()
        if ap_mac:
            ap_name = ap_map.get(ap_mac, ap_mac)
            if ap_name not in ap_clients:
                ap_clients[ap_name] = []
            ap_clients[ap_name].append(c)

    print("*WiFi Optimization Analysis*\n")

    # AP load distribution
    print("*Client Distribution by AP:*")
    for ap_name, cl_list in sorted(ap_clients.items(), key=lambda x: -len(x[1])):
        print(f"  {ap_name}: {len(cl_list)} clients")
        # Signal distribution
        signals = [c.get("signal", 0) for c in cl_list if c.get("signal")]
        if signals:
            avg_sig = sum(signals) / len(signals)
            worst = min(signals)
            print(f"    Signal: avg {avg_sig:.0f} dBm, worst {worst} dBm")
            weak = [c for c in cl_list if c.get("signal", 0) < -75]
            if weak:
                print(f"    Weak clients ({len(weak)}):")
                for w in weak:
                    wn = w.get("hostname", w.get("name", "?"))
                    print(f"      {wn}: {w.get('signal')} dBm")

    # Channel analysis
    print("\n*Channel Utilization:*")
    recommendations = []
    for ap in aps:
        ap_name = ap.get("name", ap.get("model", "?"))
        for radio in ap.get("radio_table_stats", []):
            channel = radio.get("channel", "?")
            cu_total = radio.get("cu_total", 0)
            cu_self_rx = radio.get("cu_self_rx", 0)
            cu_self_tx = radio.get("cu_self_tx", 0)
            satisfaction = radio.get("satisfaction", 100)
            num_sta = radio.get("num_sta", 0)
            radio_name = radio.get("name", radio.get("radio", "?"))

            print(f"  {ap_name} ({radio_name}) — Ch {channel}")
            print(f"    Utilization: {cu_total}% (self RX {cu_self_rx}% TX {cu_self_tx}%)")
            print(f"    Satisfaction: {satisfaction}% | Clients: {num_sta}")

            if cu_total > 60:
                recommendations.append(
                    f"  Consider changing {ap_name} ({radio_name}) from ch {channel} — "
                    f"{cu_total}% utilization is high"
                )
            if satisfaction < 70:
                recommendations.append(
                    f"  {ap_name} ({radio_name}) satisfaction is only {satisfaction}% — "
                    f"check for interference"
                )

    # Band steering analysis
    print("\n*Band Steering:*")
    band_counts = {"2.4GHz": 0, "5GHz": 0, "6GHz": 0, "other": 0}
    for c in clients:
        channel = c.get("channel", 0)
        if not channel:
            continue
        if channel <= 14:
            band_counts["2.4GHz"] += 1
        elif channel <= 177:
            band_counts["5GHz"] += 1
        elif channel > 177:
            band_counts["6GHz"] += 1
    wifi_clients = sum(band_counts.values())
    if wifi_clients > 0:
        for band, count in band_counts.items():
            if count > 0:
                pct = count / wifi_clients * 100
                print(f"  {band}: {count} clients ({pct:.0f}%)")
        if band_counts["2.4GHz"] > band_counts["5GHz"] and band_counts["5GHz"] > 0:
            recommendations.append(
                "  Enable band steering — too many clients on 2.4GHz vs 5GHz"
            )

    if recommendations:
        print("\n*Recommendations:*")
        for r in recommendations:
            print(r)
    else:
        print("\n  _WiFi configuration looks good — no recommendations._")


# ── 5. Presence tracking ───────────────────────────────────────────────────

def _load_known_people():
    """Load known people config; auto-learn from clients on first run."""
    config = _load_json(PEOPLE_CONFIG_FILE)
    if config:
        return config

    # Auto-learn: scan clients for recognizable hostnames
    log("First run: auto-learning people from client hostnames...")
    clients = get_clients()
    people = {}

    # Hostname patterns that suggest a person's device
    # Format: {"Person Name": {"devices": {"mac": "device_description"}, "is_family": bool}}
    person_patterns = [
        # Common Apple device naming: "Jordans-iPhone", "Jordan's MacBook", etc.
        ("Jordan", ["jordan", "jordans", "jordan's", "kochj"]),
        ("Amy", ["amy", "amys", "amy's"]),
        ("Dylan", ["dylan", "dylans", "dylan's"]),
    ]

    found_people = {}
    for c in clients:
        hostname = c.get("hostname", c.get("name", "")).lower()
        mac = c.get("mac", "").lower()
        if not hostname or not mac:
            continue
        for person_name, patterns in person_patterns:
            for pat in patterns:
                if pat in hostname:
                    if person_name not in found_people:
                        found_people[person_name] = {
                            "devices": {},
                            "is_family": True,
                        }
                    device_name = c.get("hostname", c.get("name", "Unknown"))
                    found_people[person_name]["devices"][mac] = device_name
                    break

    if found_people:
        _save_json(PEOPLE_CONFIG_FILE, found_people)
        log(f"Auto-learned {len(found_people)} people:")
        for name, info in found_people.items():
            devs = ", ".join(info["devices"].values())
            log(f"  {name}: {devs}")
        print(f"\nPeople config saved to: {PEOPLE_CONFIG_FILE}")
        print("Edit this file to add/remove devices or people.")
        return found_people

    # No patterns matched — create empty config
    _save_json(PEOPLE_CONFIG_FILE, {})
    log("No recognizable device hostnames found. Edit the config manually.")
    print(f"Config file: {PEOPLE_CONFIG_FILE}")
    return {}


def presence_check(alert=True):
    """Check who's home based on connected devices."""
    people = _load_known_people()
    if not people:
        print("No known people configured. Run --presence to auto-learn, or edit:")
        print(f"  {PEOPLE_CONFIG_FILE}")
        return {}

    clients = get_clients()
    client_macs = {c.get("mac", "").lower() for c in clients}

    # Build client lookup for details
    client_by_mac = {}
    for c in clients:
        client_by_mac[c.get("mac", "").lower()] = c

    # Load previous presence state
    prev_state = _load_json(PRESENCE_FILE)
    if not isinstance(prev_state, dict):
        prev_state = {"people": {}, "events": []}
    if "people" not in prev_state:
        prev_state["people"] = {}
    if "events" not in prev_state:
        prev_state["events"] = []

    current_presence = {}
    arrivals = []
    departures = []

    for person_name, info in people.items():
        person_macs = set(info.get("devices", {}).keys())
        connected_devices = person_macs & client_macs
        is_home = len(connected_devices) > 0

        device_names = []
        for mac in connected_devices:
            cl = client_by_mac.get(mac, {})
            device_names.append(cl.get("hostname", cl.get("name", info["devices"].get(mac, mac))))

        current_presence[person_name] = {
            "home": is_home,
            "devices": list(connected_devices),
            "device_names": device_names,
            "last_seen": NOW.isoformat() if is_home else prev_state.get("people", {}).get(person_name, {}).get("last_seen", "never"),
        }

        # Detect transitions
        was_home = prev_state.get("people", {}).get(person_name, {}).get("home", False)
        if is_home and not was_home:
            arrivals.append(person_name)
        elif not is_home and was_home:
            departures.append(person_name)

    # Print current status
    print(f"*Who's Home — {NOW.strftime('%I:%M %p')}*")
    for name, info in current_presence.items():
        status = "HOME" if info["home"] else "AWAY"
        devs = ", ".join(info["device_names"]) if info["device_names"] else ""
        last = ""
        if not info["home"] and info["last_seen"] != "never":
            last = f" (last seen: {info['last_seen'][:16]})"
        dev_str = f" — {devs}" if devs else ""
        print(f"  {name}: {status}{dev_str}{last}")

    # Record events and alert on transitions
    if arrivals or departures:
        for name in arrivals:
            event = {"person": name, "event": "arrived", "time": NOW.isoformat()}
            prev_state["events"].append(event)
        for name in departures:
            event = {"person": name, "event": "departed", "time": NOW.isoformat()}
            prev_state["events"].append(event)

        # Keep last 500 events
        if len(prev_state["events"]) > 500:
            prev_state["events"] = prev_state["events"][-500:]

        if alert:
            parts = []
            if arrivals:
                parts.append(f"Arrived: {', '.join(arrivals)}")
            if departures:
                parts.append(f"Departed: {', '.join(departures)}")
            msg = f"*Home Presence Update*\n  " + "\n  ".join(parts)
            slack_post(msg)
            vector_remember(
                f"Presence {TODAY}: {'; '.join(parts)}",
                {"date": TODAY, "type": "presence_change"}
            )

    # Save state
    prev_state["people"] = current_presence
    _save_json(PRESENCE_FILE, prev_state)

    return current_presence


# ── 6. Firmware monitoring ──────────────────────────────────────────────────

def firmware_check():
    """Check firmware versions across all devices."""
    devices = get_devices()
    if not devices:
        log("Cannot reach UDM Pro.")
        return

    # Group devices by model
    by_model = {}
    for d in devices:
        model = d.get("model", "unknown")
        if model not in by_model:
            by_model[model] = []
        by_model[model].append(d)

    print("*Firmware Status*\n")

    outdated = []
    for model, devs in sorted(by_model.items()):
        # Find the latest version in this model family
        versions = [d.get("version", "0.0.0") for d in devs]
        latest = max(versions)

        for d in devs:
            name = d.get("name", d.get("model", "?"))
            ver = d.get("version", "?")
            upgradable = d.get("upgradable", False)
            upgrade_to = d.get("upgrade_to_firmware", "")
            state = d.get("state", 0)

            status = "OK"
            if upgradable:
                status = f"UPDATE AVAILABLE → {upgrade_to}"
                outdated.append(f"{name}: {ver} → {upgrade_to}")
            elif ver != latest and len(devs) > 1:
                status = f"BEHIND (latest in group: {latest})"
                outdated.append(f"{name}: {ver} (others at {latest})")

            state_str = "online" if state == 1 else f"state={state}"
            print(f"  {name} ({model}) — v{ver} — {state_str} — {status}")

    if outdated:
        print(f"\n*{len(outdated)} device(s) need attention:*")
        for o in outdated:
            print(f"  !! {o}")
        slack_post(
            f"*Firmware Alert*\n  {len(outdated)} device(s) need updates:\n  " +
            "\n  ".join(outdated)
        )
    else:
        print("\n  _All devices on latest firmware._")


# ── 7. Port utilization ────────────────────────────────────────────────────

def port_utilization():
    """Show switch port status, speed, throughput, and errors."""
    devices = get_devices()
    if not devices:
        log("Cannot reach UDM Pro.")
        return

    switches = [d for d in devices if d.get("type") in ("usw", "ugw")]
    if not switches:
        # Include UDM Pro itself which has switch ports
        switches = [d for d in devices if d.get("port_table")]

    if not switches:
        print("No switches found.")
        return

    print("*Switch Port Utilization*\n")
    error_ports = []

    for sw in switches:
        sw_name = sw.get("name", sw.get("model", "Unknown"))
        ports = sw.get("port_table", [])
        if not ports:
            continue

        print(f"*{sw_name}* ({len(ports)} ports)")
        for p in ports:
            port_idx = p.get("port_idx", "?")
            name = p.get("name", "")
            enabled = p.get("enable", True)
            up = p.get("up", False)
            speed = p.get("speed", 0)
            full_duplex = p.get("full_duplex", False)
            tx_bytes = p.get("tx_bytes-r", 0)
            rx_bytes = p.get("rx_bytes-r", 0)
            tx_errors = p.get("tx_errors", 0)
            rx_errors = p.get("rx_errors", 0)
            tx_dropped = p.get("tx_dropped", 0)
            rx_dropped = p.get("rx_dropped", 0)
            poe = p.get("poe_enable", False)
            poe_power = p.get("poe_power", "")

            if not enabled:
                status = "DISABLED"
            elif up:
                duplex = "FD" if full_duplex else "HD"
                status = f"UP {speed}M {duplex}"
            else:
                status = "DOWN"

            label = f" ({name})" if name else ""
            throughput = ""
            if up and (tx_bytes or rx_bytes):
                tx_mbps = tx_bytes * 8 / 1000000
                rx_mbps = rx_bytes * 8 / 1000000
                throughput = f" — TX {tx_mbps:.1f}Mbps RX {rx_mbps:.1f}Mbps"

            poe_str = ""
            if poe and poe_power:
                poe_str = f" — PoE {poe_power}W"

            total_errors = tx_errors + rx_errors + tx_dropped + rx_dropped
            error_str = ""
            if total_errors > 0:
                error_str = f" — ERRORS: tx_err={tx_errors} rx_err={rx_errors} tx_drop={tx_dropped} rx_drop={rx_dropped}"
                error_ports.append(f"{sw_name} port {port_idx}{label}: {total_errors} errors")

            print(f"  Port {port_idx}{label}: {status}{throughput}{poe_str}{error_str}")

        print()

    if error_ports:
        print(f"*{len(error_ports)} port(s) with errors:*")
        for ep in error_ports:
            print(f"  !! {ep}")


# ── 8. VPN monitoring (integrated into health) ─────────────────────────────

def vpn_status(health=None):
    """Check VPN subsystem status."""
    if health is None:
        health = get_health()
    if not health:
        return []

    vpn_info = health.get("vpn", {})
    problems = []
    if vpn_info:
        status = vpn_info.get("status", "unknown")
        if status != "ok" and status != "unknown":
            problems.append({
                "severity": "medium",
                "category": "vpn",
                "message": f"VPN subsystem status: {status}",
            })
    return problems


# ── 9. DPI traffic analysis ────────────────────────────────────────────────

def dpi_analysis():
    """Show DPI traffic by category and top clients."""
    dpi_data = get_dpi()
    clients = get_clients()

    if not dpi_data:
        print("No DPI data available (may not be enabled).")
        return

    print("*DPI Traffic Analysis*\n")

    # Aggregate by category
    categories = {}
    for entry in dpi_data:
        cat = entry.get("cat", 0)
        cat_name = _dpi_category_name(cat)
        tx = entry.get("tx_bytes", 0)
        rx = entry.get("rx_bytes", 0)
        total = tx + rx
        if cat_name not in categories:
            categories[cat_name] = 0
        categories[cat_name] += total

    if categories:
        print("*Top Traffic Categories:*")
        sorted_cats = sorted(categories.items(), key=lambda x: -x[1])
        for name, total_bytes in sorted_cats[:15]:
            gb = total_bytes / 1024 / 1024 / 1024
            mb = total_bytes / 1024 / 1024
            if gb >= 1:
                print(f"  {name}: {gb:.1f} GB")
            else:
                print(f"  {name}: {mb:.0f} MB")

    # Top clients by traffic
    if clients:
        print("\n*Top Clients by Traffic:*")
        client_traffic = []
        for c in clients:
            tx = c.get("tx_bytes", 0)
            rx = c.get("rx_bytes", 0)
            total = tx + rx
            name = c.get("hostname", c.get("name", c.get("oui", "Unknown")))
            client_traffic.append((name, total, tx, rx))

        client_traffic.sort(key=lambda x: -x[1])
        for name, total, tx, rx in client_traffic[:15]:
            gb = total / 1024 / 1024 / 1024
            mb = total / 1024 / 1024
            if gb >= 1:
                print(f"  {name}: {gb:.1f} GB (TX {tx/1024/1024:.0f}MB / RX {rx/1024/1024:.0f}MB)")
            elif mb >= 1:
                print(f"  {name}: {mb:.0f} MB")


def _dpi_category_name(cat_id):
    """Map DPI category ID to human-readable name."""
    # UniFi DPI categories
    dpi_cats = {
        0: "Instant Messaging",
        1: "P2P",
        3: "File Transfer",
        4: "Streaming Media",
        5: "Mail & Calendar",
        6: "Voice over IP",
        7: "Database",
        8: "Games",
        9: "Network Management",
        10: "Remote Access",
        11: "Social Media",
        12: "Software Update",
        13: "Web",
        14: "Security & VPN",
        15: "E-Commerce",
        17: "Business Apps",
        18: "Network Protocols",
        19: "IoT Automation",
        20: "Transport",
        23: "Health & Fitness",
        24: "News & Media",
        255: "Unknown",
    }
    return dpi_cats.get(cat_id, f"Category {cat_id}")


# ── 10. Network snapshots & trends ─────────────────────────────────────────

def save_snapshot():
    """Save daily network snapshot."""
    health = get_health()
    devices = get_devices()
    clients = get_clients()

    if not health:
        log("Cannot reach UDM Pro for snapshot.")
        return

    problems = find_problems(health, devices, clients)
    wan = health.get("wan", {})

    snapshot = {
        "date": TODAY,
        "timestamp": NOW.isoformat(),
        "device_count": len(devices),
        "client_count": len(clients),
        "problem_count": len(problems),
        "wan_status": wan.get("status", "?"),
        "wan_latency": wan.get("latency", 0),
        "xput_down": wan.get("xput_down", 0),
        "xput_up": wan.get("xput_up", 0),
        "wan_drops": wan.get("drops", 0),
        "total_tx_bytes": sum(c.get("tx_bytes", 0) for c in clients),
        "total_rx_bytes": sum(c.get("rx_bytes", 0) for c in clients),
        "wifi_clients": sum(1 for c in clients if c.get("is_wired") is False or c.get("signal")),
        "wired_clients": sum(1 for c in clients if c.get("is_wired") is True),
        "problems": [p["message"] for p in problems],
    }

    snapshots = _load_json(SNAPSHOT_FILE)
    if not isinstance(snapshots, dict):
        snapshots = {"snapshots": []}
    if "snapshots" not in snapshots:
        snapshots["snapshots"] = []

    # Replace today's snapshot if already exists
    snapshots["snapshots"] = [s for s in snapshots["snapshots"] if s.get("date") != TODAY]
    snapshots["snapshots"].append(snapshot)

    # Keep 90 days
    if len(snapshots["snapshots"]) > 90:
        snapshots["snapshots"] = snapshots["snapshots"][-90:]

    _save_json(SNAPSHOT_FILE, snapshots)

    print(f"*Network Snapshot — {TODAY}*")
    print(f"  Devices: {snapshot['device_count']} | Clients: {snapshot['client_count']}")
    print(f"  WAN: {snapshot['wan_status']} ({snapshot['wan_latency']}ms)")
    if snapshot["xput_down"]:
        print(f"  Speed: {snapshot['xput_down']:.0f}/{snapshot['xput_up']:.0f} Mbps")
    total_traffic_gb = (snapshot["total_tx_bytes"] + snapshot["total_rx_bytes"]) / 1024 / 1024 / 1024
    print(f"  Total client traffic: {total_traffic_gb:.1f} GB")
    print(f"  Problems: {snapshot['problem_count']}")
    print(f"  Saved to: {SNAPSHOT_FILE}")

    vector_remember(
        f"Network snapshot {TODAY}: {snapshot['device_count']} devices, "
        f"{snapshot['client_count']} clients, {snapshot['wan_latency']}ms latency, "
        f"{snapshot['problem_count']} problems",
        {"date": TODAY, "type": "network_snapshot"}
    )


def show_trends():
    """Show 7-day network trends."""
    snapshots = _load_json(SNAPSHOT_FILE)
    entries = snapshots.get("snapshots", [])
    if not entries:
        print("No snapshots recorded yet. Run --snapshot first.")
        return

    # Get last 7 days
    recent = entries[-7:]

    print(f"*Network Trends (last {len(recent)} snapshots)*\n")
    print(f"  {'Date':<12} {'Devices':>8} {'Clients':>8} {'Latency':>8} {'Down':>8} {'Up':>8} {'Problems':>8}")
    print(f"  {'─'*12} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8}")

    for s in recent:
        dt = s.get("date", "?")
        devs = s.get("device_count", "?")
        cls = s.get("client_count", "?")
        lat = s.get("wan_latency", "?")
        down = s.get("xput_down", 0)
        up = s.get("xput_up", 0)
        probs = s.get("problem_count", "?")

        lat_str = f"{lat}ms" if isinstance(lat, (int, float)) else lat
        down_str = f"{down:.0f}M" if isinstance(down, (int, float)) and down > 0 else "—"
        up_str = f"{up:.0f}M" if isinstance(up, (int, float)) and up > 0 else "—"

        print(f"  {dt:<12} {str(devs):>8} {str(cls):>8} {lat_str:>8} {down_str:>8} {up_str:>8} {str(probs):>8}")

    # Summary
    if len(recent) >= 2:
        first = recent[0]
        last = recent[-1]
        print(f"\n*Changes ({first.get('date', '?')} → {last.get('date', '?')}):*")

        for metric, label in [
            ("client_count", "Clients"),
            ("device_count", "Devices"),
            ("wan_latency", "Latency"),
            ("problem_count", "Problems"),
        ]:
            v1 = first.get(metric, 0)
            v2 = last.get(metric, 0)
            if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                diff = v2 - v1
                if diff != 0:
                    direction = "+" if diff > 0 else ""
                    unit = "ms" if metric == "wan_latency" else ""
                    print(f"  {label}: {v1} → {v2} ({direction}{diff}{unit})")


# ── 11. Who's home (HomeKit JSON output) ───────────────────────────────────

def who_home_json():
    """Output JSON presence data suitable for HomeKit automation triggers."""
    people = _load_known_people()
    if not people:
        print(json.dumps({"error": "No known people configured"}, indent=2))
        return

    clients = get_clients()
    client_macs = {c.get("mac", "").lower() for c in clients}

    result = {
        "timestamp": NOW.isoformat(),
        "anyone_home": False,
        "people": {},
    }

    for person_name, info in people.items():
        if not info.get("is_family", False):
            continue
        person_macs = set(info.get("devices", {}).keys())
        connected = person_macs & client_macs
        is_home = len(connected) > 0

        result["people"][person_name] = {
            "home": is_home,
            "device_count": len(connected),
            "devices": [info["devices"].get(m, m) for m in connected],
        }
        if is_home:
            result["anyone_home"] = True

    print(json.dumps(result, indent=2))
    return result


# ── Enhanced full_check with new integrations ──────────────────────────────

def full_check_v2():
    """Extended full health check with bandwidth hogs, VPN, WAN tracking, and presence."""
    log("Running UniFi network health check (extended)...")

    health = get_health()
    devices = get_devices()
    clients = get_clients()

    if health is None:
        log("Could not reach UDM Pro API")
        slack_post("*UniFi Monitor*\n  Unable to reach UDM Pro at 192.168.1.1")
        return

    problems = find_problems(health, devices, clients)

    # 3. Bandwidth hog detection — DISABLED from regular checks
    # Bandwidth report runs nightly at 23:50 via nova_bandwidth_report.py
    # Cameras routinely use 50-100GB which is normal, not a problem
    # hogs = find_bandwidth_hogs(clients)
    # problems.extend(hogs)

    # 8. VPN monitoring — add to problems
    vpn_problems = vpn_status(health)
    problems.extend(vpn_problems)

    log(f"Health: {format_status(health)}")
    log(f"Devices: {len(devices)} | Clients: {len(clients)} | Problems: {len(problems)}")

    # Post to Slack if there are problems
    if problems:
        report = format_health_report(health, devices, clients, problems)
        slack_post(report)

    # 2. WAN history logging
    wan_log()

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
    # Original modes
    parser.add_argument("--status", action="store_true", help="Quick status")
    parser.add_argument("--clients", action="store_true", help="List clients")
    parser.add_argument("--devices", action="store_true", help="List devices")
    parser.add_argument("--alerts", action="store_true", help="Show alerts")
    parser.add_argument("--problems", action="store_true", help="Only problems")
    parser.add_argument("--full", action="store_true", help="Full report to Slack")
    # New modes
    parser.add_argument("--rogue", action="store_true", help="Detect unknown/rogue devices")
    parser.add_argument("--rogue-learn", action="store_true", help="Learn all current clients as known")
    parser.add_argument("--wan-history", action="store_true", help="Show WAN uptime/outage history")
    parser.add_argument("--wifi-optimize", action="store_true", help="WiFi optimization analysis")
    parser.add_argument("--presence", action="store_true", help="Who's home (family presence)")
    parser.add_argument("--firmware", action="store_true", help="Firmware version check")
    parser.add_argument("--ports", action="store_true", help="Switch port utilization")
    parser.add_argument("--dpi", action="store_true", help="DPI traffic analysis")
    parser.add_argument("--snapshot", action="store_true", help="Save daily network snapshot")
    parser.add_argument("--trends", action="store_true", help="7-day trend comparison")
    parser.add_argument("--who-home", action="store_true", help="JSON presence for HomeKit")
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
        hogs = find_bandwidth_hogs(clients)
        problems.extend(hogs)
        vpn_probs = vpn_status(health)
        problems.extend(vpn_probs)
        if problems:
            for p in problems:
                print(f"  [{p['severity']}] {p['message']}")
        else:
            print("No problems detected.")
    elif args.rogue:
        rogue_check()
    elif args.rogue_learn:
        rogue_learn()
    elif args.wan_history:
        wan_show_history()
    elif args.wifi_optimize:
        wifi_optimize()
    elif args.presence:
        presence_check()
    elif args.firmware:
        firmware_check()
    elif args.ports:
        port_utilization()
    elif args.dpi:
        dpi_analysis()
    elif args.snapshot:
        save_snapshot()
    elif args.trends:
        show_trends()
    elif args.who_home:
        who_home_json()
    elif args.full:
        full_check_v2()
    else:
        full_check_v2()
