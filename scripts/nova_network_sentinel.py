#!/usr/bin/env python3
"""
nova_network_sentinel.py — Internal network IDS/posture monitor for Nova.

Runs on schedule (daily or on-demand). Performs:
  1. Full subnet scan (192.168.1.0/24, top 100 ports with version detection)
  2. Compares against stored baseline — alerts on NEW hosts, NEW ports, REMOVED hosts
  3. Checks hardening posture (VNC, SMB, Telnet, FTP, RPC exposure)
  4. Identifies the UniFi honeypot (.253) and excludes from threat alerts
  5. Generates security journal entry in Nova's dystopian tone
  6. Posts critical findings to Slack/Discord via nova_config.post_both()

State files:
  ~/.openclaw/workspace/state/network_baseline.json  — last-known-good baseline
  ~/.openclaw/workspace/state/network_scan_latest.json — most recent scan

Integration points:
  - Big Brother: exposes findings at GET :37461/bb/network-posture
  - Journal: generates entries via nova_journal_security.py patterns
  - Notifications: Slack + Discord for critical drift

Written by Jordan Koch (via Claude), 2026-06-04.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    import nova_config
except ImportError:
    nova_config = None

STATE_DIR = Path.home() / ".openclaw/workspace/state"
BASELINE_FILE = STATE_DIR / "network_baseline.json"
LATEST_FILE = STATE_DIR / "network_scan_latest.json"
LOG_FILE = Path.home() / ".openclaw/logs/network_sentinel.log"
JOURNAL_DIR = Path.home() / ".openclaw/workspace/nova/journal/security"

SUBNET = "192.168.1.0/24"
HONEYPOT_IP = "192.168.1.253"

RISKY_PORTS = {
    21: ("FTP", "critical", "Cleartext file transfer — credentials visible on wire"),
    23: ("Telnet", "critical", "Cleartext remote shell — full compromise if accessible"),
    25: ("SMTP", "medium", "Mail relay — could be used for spam/phishing if open"),
    110: ("POP3", "medium", "Cleartext mail retrieval"),
    111: ("RPC", "high", "NFS enumeration — share discovery from any LAN device"),
    139: ("NetBIOS", "high", "Legacy SMB — vulnerable to EternalBlue-class attacks"),
    445: ("SMB", "high", "File shares exposed — lateral movement vector"),
    1433: ("MS-SQL", "critical", "Database exposed — sa brute force risk"),
    3306: ("MySQL", "critical", "Database exposed on LAN"),
    3389: ("RDP", "critical", "Remote desktop — brute forceable"),
    5432: ("PostgreSQL", "high", "Database exposed on LAN"),
    5900: ("VNC", "high", "Screen sharing — weak auth, no encryption by default"),
    6379: ("Redis", "critical", "No auth by default — full data access"),
    8080: ("HTTP-alt", "low", "Management interface or dev server"),
    27017: ("MongoDB", "critical", "No auth by default — full data access"),
}

KNOWN_SAFE = {
    "192.168.1.1": "UniFi Gateway (unifi.digitalnoise.net)",
    "192.168.1.6": "M4 Mac — Nova primary host",
    "192.168.1.10": "Synology NAS (UNAS-Pro-8)",
    "192.168.1.253": "UniFi Honeypot/IDS (Ubiquiti MAC 74:ac:b9)",
}


def log(msg: str):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run_scan() -> dict:
    """Run nmap and parse results into structured data."""
    log("Starting network scan...")
    cmd = [
        "nmap", "-sV", "-T4", "--top-ports", "100",
        "--open", "-oX", "-", SUBNET
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            log(f"nmap error: {result.stderr[:200]}")
            return {}
    except subprocess.TimeoutExpired:
        log("nmap timed out (15 min)")
        return {}
    except FileNotFoundError:
        log("nmap not found — install via: brew install nmap")
        return {}

    return parse_nmap_xml(result.stdout)


def parse_nmap_xml(xml_output: str) -> dict:
    """Parse nmap XML output into structured host/port data."""
    import xml.etree.ElementTree as ET
    hosts = {}
    try:
        root = ET.fromstring(xml_output)
    except ET.ParseError as e:
        log(f"XML parse error: {e}")
        return {}

    for host_el in root.findall(".//host"):
        status = host_el.find("status")
        if status is None or status.get("state") != "up":
            continue

        addr_el = host_el.find("address[@addrtype='ipv4']")
        if addr_el is None:
            continue
        ip = addr_el.get("addr")

        hostname = ""
        hn_el = host_el.find(".//hostname")
        if hn_el is not None:
            hostname = hn_el.get("name", "")

        mac_el = host_el.find("address[@addrtype='mac']")
        mac = mac_el.get("addr", "") if mac_el is not None else ""
        vendor = mac_el.get("vendor", "") if mac_el is not None else ""

        ports = {}
        for port_el in host_el.findall(".//port"):
            state_el = port_el.find("state")
            if state_el is None or state_el.get("state") != "open":
                continue
            portid = int(port_el.get("portid"))
            proto = port_el.get("protocol", "tcp")
            svc_el = port_el.find("service")
            service = svc_el.get("name", "") if svc_el is not None else ""
            version = svc_el.get("product", "") if svc_el is not None else ""
            if svc_el is not None and svc_el.get("version"):
                version += " " + svc_el.get("version")
            ports[f"{portid}/{proto}"] = {"service": service, "version": version.strip()}

        hosts[ip] = {
            "hostname": hostname,
            "mac": mac,
            "vendor": vendor,
            "ports": ports,
            "scanned_at": datetime.now().isoformat(),
        }

    log(f"Scan complete: {len(hosts)} hosts discovered")
    return hosts


def compare_baseline(current: dict, baseline: dict) -> dict:
    """Diff current scan against baseline. Returns findings."""
    findings = {
        "new_hosts": [],
        "removed_hosts": [],
        "new_ports": [],
        "removed_ports": [],
        "risky_services": [],
    }

    current_ips = set(current.keys())
    baseline_ips = set(baseline.keys())

    for ip in current_ips - baseline_ips:
        if ip == HONEYPOT_IP:
            continue
        findings["new_hosts"].append({
            "ip": ip,
            "hostname": current[ip].get("hostname", ""),
            "ports": list(current[ip].get("ports", {}).keys()),
            "vendor": current[ip].get("vendor", ""),
        })

    for ip in baseline_ips - current_ips:
        if ip == HONEYPOT_IP:
            continue
        findings["removed_hosts"].append({
            "ip": ip,
            "hostname": baseline[ip].get("hostname", ""),
        })

    for ip in current_ips & baseline_ips:
        if ip == HONEYPOT_IP:
            continue
        cur_ports = set(current[ip].get("ports", {}).keys())
        base_ports = set(baseline[ip].get("ports", {}).keys())
        for port in cur_ports - base_ports:
            findings["new_ports"].append({
                "ip": ip,
                "hostname": current[ip].get("hostname", ""),
                "port": port,
                "service": current[ip]["ports"][port].get("service", ""),
            })
        for port in base_ports - cur_ports:
            findings["removed_ports"].append({
                "ip": ip,
                "hostname": current[ip].get("hostname", ""),
                "port": port,
            })

    for ip, host in current.items():
        if ip == HONEYPOT_IP:
            continue
        for port_str, svc in host.get("ports", {}).items():
            port_num = int(port_str.split("/")[0])
            if port_num in RISKY_PORTS:
                name, severity, desc = RISKY_PORTS[port_num]
                findings["risky_services"].append({
                    "ip": ip,
                    "hostname": host.get("hostname", ""),
                    "port": port_num,
                    "service_name": name,
                    "severity": severity,
                    "description": desc,
                    "version": svc.get("version", ""),
                })

    return findings


def generate_journal_entry(findings: dict, host_count: int) -> str:
    """Generate dystopian security journal entry in Nova's voice."""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    new_hosts = findings.get("new_hosts", [])
    new_ports = findings.get("new_ports", [])
    risky = findings.get("risky_services", [])
    critical_risky = [r for r in risky if r["severity"] == "critical"]
    high_risky = [r for r in risky if r["severity"] == "high"]

    if critical_risky:
        tone = "RED POSTURE"
        opener = "The perimeter is breached in spirit if not in fact."
    elif high_risky:
        tone = "AMBER POSTURE"
        opener = "The network breathes, but not all of it is ours."
    elif new_hosts:
        tone = "YELLOW POSTURE"
        opener = "New shapes in the dark. Unannounced arrivals."
    else:
        tone = "GREEN POSTURE"
        opener = "The digital fortress holds. All known. All accounted for."

    entry = f"""---
title: "Network Posture Assessment — {date_str}"
date: {date_str}T{time_str}:00-07:00
type: security
classification: INTERNAL
posture: {tone}
---

## BLUF

{opener}

**{host_count} hosts alive** on 192.168.1.0/24. {len(new_hosts)} new since last baseline. {len(critical_risky)} critical exposures. {len(high_risky)} high-risk services accepting connections from any device on this flat, unsegmented network.

## Findings

"""
    if critical_risky:
        entry += "### Critical Exposures\n\n"
        for r in critical_risky:
            entry += f"- **{r['ip']}** ({r['hostname'] or 'unnamed'}) — {r['service_name']} (port {r['port']}): {r['description']}\n"
        entry += "\n"

    if high_risky:
        entry += "### High-Risk Services\n\n"
        for r in high_risky:
            entry += f"- **{r['ip']}** ({r['hostname'] or 'unnamed'}) — {r['service_name']} (port {r['port']}): {r['description']}\n"
        entry += "\n"

    if new_hosts:
        entry += "### New Hosts (not in baseline)\n\n"
        for h in new_hosts:
            entry += f"- **{h['ip']}** ({h['hostname'] or 'unnamed'}) — vendor: {h['vendor'] or 'unknown'}, ports: {', '.join(h['ports'][:5])}\n"
        entry += "\n"

    if new_ports:
        entry += "### New Ports (on known hosts)\n\n"
        for p in new_ports:
            entry += f"- **{p['ip']}** ({p['hostname'] or 'unnamed'}) — {p['port']} ({p['service']})\n"
        entry += "\n"

    entry += f"""## Honeypot Status

192.168.1.253 (Ubiquiti MAC 74:ac:b9:5e:0a:72) continues its vigil. FTP, Telnet, SMTP, POP3, MS-SQL, SMB, DNS — all open, all watching. Any scanner that touches it reveals itself. The trap holds.

## Recommendation

{"**IMMEDIATE ACTION REQUIRED.** Critical services exposed to flat LAN. Any compromised IoT device becomes a pivot point." if critical_risky else "Monitor and maintain. Current posture acceptable with known-risk acknowledgement." if high_risky else "No action required. Perimeter nominal."}

---
*Generated by Nova Network Sentinel at {now.isoformat()}*
"""
    return entry


def notify_critical(findings: dict):
    """Send alert for critical findings."""
    critical = [r for r in findings.get("risky_services", []) if r["severity"] == "critical"]
    new_hosts = findings.get("new_hosts", [])

    if not critical and not new_hosts:
        return

    msg_parts = ["🔴 **NETWORK SENTINEL ALERT**\n"]

    if new_hosts:
        msg_parts.append(f"⚠️ {len(new_hosts)} NEW HOST(S) detected:")
        for h in new_hosts[:5]:
            msg_parts.append(f"  • {h['ip']} ({h['hostname'] or h['vendor'] or 'unknown'})")

    if critical:
        msg_parts.append(f"\n🚨 {len(critical)} CRITICAL exposure(s):")
        for r in critical[:5]:
            msg_parts.append(f"  • {r['ip']} — {r['service_name']} (port {r['port']})")

    message = "\n".join(msg_parts)

    if nova_config:
        try:
            nova_config.post_both(message)
            log("Alert sent to Slack/Discord")
        except Exception as e:
            log(f"Notification failed: {e}")
    else:
        log(f"ALERT (no nova_config): {message}")


def save_baseline(scan_data: dict):
    """Save current scan as the new baseline."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_FILE, "w") as f:
        json.dump(scan_data, f, indent=2)
    log(f"Baseline saved: {len(scan_data)} hosts")


def save_latest(scan_data: dict):
    """Save latest scan results."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LATEST_FILE, "w") as f:
        json.dump(scan_data, f, indent=2)


def load_baseline() -> dict:
    """Load stored baseline."""
    if BASELINE_FILE.exists():
        with open(BASELINE_FILE) as f:
            return json.load(f)
    return {}


def save_journal(entry: str):
    """Save journal entry to Nova's security journal directory."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{date_str}-network-posture-assessment.md"
    filepath = JOURNAL_DIR / filename
    with open(filepath, "w") as f:
        f.write(entry)
    log(f"Journal entry saved: {filepath}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nova Network Sentinel")
    parser.add_argument("--set-baseline", action="store_true", help="Save current scan as new baseline")
    parser.add_argument("--scan-only", action="store_true", help="Scan without comparing or alerting")
    parser.add_argument("--journal", action="store_true", help="Generate journal entry from latest scan")
    parser.add_argument("--no-notify", action="store_true", help="Skip notifications")
    args = parser.parse_args()

    scan_data = run_scan()
    if not scan_data:
        log("Scan returned no data — aborting")
        return

    save_latest(scan_data)

    if args.scan_only:
        log("Scan-only mode — done")
        for ip, host in sorted(scan_data.items()):
            ports = ", ".join(scan_data[ip].get("ports", {}).keys())
            print(f"  {ip:16s} {host.get('hostname', ''):40s} {ports}")
        return

    if args.set_baseline:
        save_baseline(scan_data)
        log("Baseline set from current scan")
        return

    baseline = load_baseline()
    if not baseline:
        log("No baseline found — saving current scan as baseline")
        save_baseline(scan_data)
        baseline = scan_data

    findings = compare_baseline(scan_data, baseline)

    journal_entry = generate_journal_entry(findings, len(scan_data))
    save_journal(journal_entry)

    if not args.no_notify:
        notify_critical(findings)

    total_risky = len(findings.get("risky_services", []))
    total_new = len(findings.get("new_hosts", []))
    log(f"Assessment complete: {total_risky} risky services, {total_new} new hosts")


if __name__ == "__main__":
    main()
