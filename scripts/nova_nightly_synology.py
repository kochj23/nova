#!/usr/bin/env python3
"""
nova_nightly_synology.py — Nightly Synology NAS health digest.

Runs at 11:30 PM via launchd. Pulls status, storage, disks, services,
and security from the existing nova_synology_monitor.py and formats
a single comprehensive Slack report.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR

TODAY = datetime.now().strftime("%A, %B %d")
SYNOLOGY_SCRIPT = Path(__file__).parent / "nova_synology_monitor.py"


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def run_synology(mode):
    """Run synology monitor in JSON mode and return parsed data."""
    try:
        result = subprocess.run(
            [sys.executable, str(SYNOLOGY_SCRIPT), f"--{mode}", "--json"],
            capture_output=True, text=True, timeout=30
        )
        if result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as e:
        log(f"Synology {mode} failed: {e}", level=LOG_ERROR, source="nightly_synology")
    return None


def format_bytes(b):
    if b >= 1024**4:
        return f"{b/1024**4:.1f} TB"
    elif b >= 1024**3:
        return f"{b/1024**3:.1f} GB"
    elif b >= 1024**2:
        return f"{b/1024**2:.0f} MB"
    return f"{b/1024:.0f} KB"


def main():
    log("Nightly Synology digest starting", level=LOG_INFO, source="nightly_synology")

    lines = [f"*:nas: Nightly NAS Report — {TODAY}*", ""]

    # System status
    status = run_synology("status")
    if status:
        model = status.get("model", "RS1221+")
        dsm = status.get("dsm_version", "?")
        uptime_d = status.get("uptime_seconds", 0) // 86400
        cpu_pct = status.get("cpu_load", "?")
        ram_pct = status.get("ram_used_percent", "?")
        temp = status.get("temperature", "?")
        overall = status.get("overall_status", "normal")
        emoji = ":large_green_circle:" if overall == "normal" else ":warning:"

        lines.append(f"{emoji} *System:* {model} / DSM {dsm} / Uptime: {uptime_d}d")
        lines.append(f"  CPU: {cpu_pct}% / RAM: {ram_pct}% / Temp: {temp}°C")
    else:
        lines.append(":red_circle: *System:* Could not reach NAS at 192.168.1.11")

    # Storage / RAID
    storage = run_synology("storage")
    if storage:
        volumes = storage.get("volumes", [])
        for vol in volumes:
            name = vol.get("name", "?")
            total = vol.get("total_bytes", 0)
            used = vol.get("used_bytes", 0)
            pct = (used / total * 100) if total else 0
            raid = vol.get("raid_type", "?")
            raid_status = vol.get("status", "normal")
            bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
            emoji = ":white_check_mark:" if raid_status == "normal" else ":warning:"
            lines.append(f"  {emoji} *{name}* ({raid}): {bar} {pct:.0f}% — {format_bytes(used)} / {format_bytes(total)}")
    lines.append("")

    # Disks
    disks = run_synology("disks")
    if disks:
        disk_list = disks.get("disks", [])
        hot_disks = [d for d in disk_list if d.get("temperature", 0) > 45]
        bad_disks = [d for d in disk_list if d.get("status", "") != "normal"]

        lines.append(f"*Disks:* {len(disk_list)} drives")
        if bad_disks:
            for d in bad_disks:
                lines.append(f"  :red_circle: {d.get('name','?')}: {d.get('status','?')} — {d.get('model','')}")
        if hot_disks:
            for d in hot_disks:
                lines.append(f"  :thermometer: {d.get('name','?')}: {d.get('temperature',0)}°C")

        temps = [d.get("temperature", 0) for d in disk_list if d.get("temperature")]
        if temps:
            lines.append(f"  Temps: {min(temps)}°C – {max(temps)}°C (avg {sum(temps)//len(temps)}°C)")

        if not bad_disks and not hot_disks:
            lines.append(f"  :white_check_mark: All drives healthy")
    lines.append("")

    # Security
    security = run_synology("security")
    if security:
        failed = security.get("failed_logins_24h", 0)
        blocked = security.get("blocked_ips", 0)
        if failed or blocked:
            lines.append(f"*Security:* :shield: {failed} failed login(s), {blocked} blocked IP(s)")
        else:
            lines.append("*Security:* :shield: Clean — no failed logins or blocked IPs")
    lines.append("")

    # Services
    services = run_synology("services")
    if services:
        packages = services.get("packages", [])
        running = [p for p in packages if p.get("status") == "running"]
        stopped = [p for p in packages if p.get("status") != "running" and p.get("status") != "stopped"]
        lines.append(f"*Services:* {len(running)} running")
        if stopped:
            for s in stopped:
                lines.append(f"  :warning: {s.get('name','?')}: {s.get('status','?')}")

    # Network
    network = run_synology("network")
    if network:
        interfaces = network.get("interfaces", [])
        for iface in interfaces:
            name = iface.get("name", "?")
            speed = iface.get("speed", 0)
            if speed:
                speed_str = f"{speed // 1000}Gbps" if speed >= 1000 else f"{speed}Mbps"
                lines.append(f"  :globe_with_meridians: {name}: {speed_str}")

    msg = "\n".join(lines)
    slack_post(msg)
    log("Nightly Synology digest posted", level=LOG_INFO, source="nightly_synology")

    # Store in memory
    try:
        import urllib.request
        payload = json.dumps({
            "text": f"Synology NAS report {TODAY}: " + " ".join(l.replace("*","").strip() for l in lines[2:8] if l.strip()),
            "source": "infrastructure",
            "metadata": {"type": "synology_nightly", "date": datetime.now().isoformat()}
        }).encode()
        req = urllib.request.Request(nova_config.VECTOR_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    main()
