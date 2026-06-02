#!/usr/bin/env python3
"""
nova_security_surface_monitor.py — Personal attack surface monitoring.

Periodic checks:
- Shodan: query for Jordan's domains/IPs — detect newly exposed services
- Certificate Transparency: monitor crt.sh for new certs issued for your domains
- DNS changes: detect unexpected record changes
- Port exposure: basic port scan of known infrastructure

Runs weekly (Sunday 6am). Alerts immediately if something unexpected appears.

Written by Jordan Koch (via Claude).
"""

import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

LOG_FILE = Path.home() / ".openclaw/logs/security_surface_monitor.log"
STATE_FILE = Path.home() / ".openclaw/workspace/state/surface_monitor_state.json"
JOURNAL_SCRIPT = Path.home() / ".openclaw/scripts/nova_journal_security.py"

# Domains and IPs to monitor
MONITORED_DOMAINS = [
    "digitalnoise.net",
    "nova.digitalnoise.net",
]

# Expected services (anything else is unexpected)
EXPECTED_SERVICES = {
    "digitalnoise.net": ["53/tcp", "80/tcp", "443/tcp", "8080/tcp", "8443/tcp"],
}


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[surface-mon {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"certs_seen": [], "last_check": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fire_alert(trigger: str, details: str):
    log(f"🚨 SURFACE ALERT: {trigger}")
    try:
        subprocess.run(
            [sys.executable, str(JOURNAL_SCRIPT), "breaking", trigger, details],
            timeout=300, capture_output=True
        )
    except Exception as e:
        log(f"Alert fire failed: {e}")


# ── Certificate Transparency ─────────────────────────────────────────────────

def check_cert_transparency(state: dict) -> list:
    """Check crt.sh for new certificates issued for monitored domains."""
    alerts = []
    seen_certs = set(state.get("certs_seen", []))

    for domain in MONITORED_DOMAINS:
        url = f"https://crt.sh/?q=%25.{domain}&output=json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Nova-SurfaceMon/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                certs = json.loads(resp.read())
        except Exception as e:
            log(f"crt.sh check failed for {domain}: {e}")
            continue

        new_certs = []
        for cert in certs[:20]:
            cert_id = str(cert.get("id", ""))
            if cert_id in seen_certs:
                continue
            seen_certs.add(cert_id)

            common_name = cert.get("common_name", "")
            issuer = cert.get("issuer_name", "")
            not_before = cert.get("not_before", "")
            not_after = cert.get("not_after", "")

            # Check if this cert was issued recently (within last 7 days)
            try:
                issued = datetime.strptime(not_before, "%Y-%m-%dT%H:%M:%S")
                if (datetime.now() - issued).days > 7:
                    continue
            except (ValueError, TypeError):
                continue

            new_certs.append({
                "cn": common_name,
                "issuer": issuer,
                "not_before": not_before,
                "not_after": not_after,
            })

        if new_certs:
            # Check if any are unexpected (not Let's Encrypt / known CA)
            unexpected = [c for c in new_certs
                          if "Let's Encrypt" not in c.get("issuer", "")
                          and "R3" not in c.get("issuer", "")
                          and "E1" not in c.get("issuer", "")]
            if unexpected:
                trigger = f"Unexpected certificate issued for {domain}"
                details = "\n".join(
                    f"- CN={c['cn']} Issuer={c['issuer']} Valid={c['not_before']}"
                    for c in unexpected
                )
                alerts.append((trigger, details))
            else:
                log(f"  {domain}: {len(new_certs)} new cert(s) — all from expected CAs")

    state["certs_seen"] = list(seen_certs)[-500:]
    return alerts


# ── DNS Record Monitor ────────────────────────────────────────────────────────

def check_dns_records(state: dict) -> list:
    """Check for unexpected DNS record changes."""
    alerts = []
    prev_records = state.get("dns_records", {})
    current_records = {}

    for domain in MONITORED_DOMAINS:
        records = {}
        for rtype in ["A", "AAAA", "MX", "NS", "TXT", "CNAME"]:
            try:
                result = subprocess.run(
                    ["dig", "+short", rtype, domain],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 and result.stdout.strip():
                    records[rtype] = sorted(result.stdout.strip().split("\n"))
            except Exception:
                pass

        current_records[domain] = records

        # Compare with previous
        if domain in prev_records:
            prev = prev_records[domain]
            for rtype, values in records.items():
                if rtype in prev and prev[rtype] != values:
                    trigger = f"DNS change detected: {domain} {rtype} record"
                    details = f"Previous: {prev[rtype]}\nCurrent: {values}"
                    alerts.append((trigger, details))
                    log(f"  DNS CHANGE: {domain} {rtype}: {prev[rtype]} → {values}")
            # Check for removed records
            for rtype in prev:
                if rtype not in records and prev[rtype]:
                    trigger = f"DNS record removed: {domain} {rtype}"
                    details = f"Previous value: {prev[rtype]}\nCurrent: (empty)"
                    alerts.append((trigger, details))

    state["dns_records"] = current_records
    return alerts


# ── Basic Port Exposure Check ─────────────────────────────────────────────────

def check_port_exposure(state: dict) -> list:
    """Quick check for unexpected open ports on known infrastructure."""
    alerts = []
    # Only check external-facing infrastructure
    # Uses nmap if available, otherwise skips
    try:
        subprocess.run(["which", "nmap"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        log("nmap not available — skipping port scan")
        return []

    for domain, expected in EXPECTED_SERVICES.items():
        try:
            result = subprocess.run(
                ["nmap", "-Pn", "--top-ports", "100", "-T4", "--open", domain],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                continue

            # Parse open ports
            open_ports = re.findall(r'(\d+/tcp)\s+open', result.stdout)
            unexpected = [p for p in open_ports if p not in expected]

            if unexpected:
                trigger = f"Unexpected open ports on {domain}"
                details = f"Expected: {expected}\nFound open: {open_ports}\nUnexpected: {unexpected}"
                alerts.append((trigger, details))
                log(f"  UNEXPECTED PORTS on {domain}: {unexpected}")
            else:
                log(f"  {domain}: ports nominal ({open_ports})")

        except subprocess.TimeoutExpired:
            log(f"  nmap timeout for {domain}")
        except Exception as e:
            log(f"  nmap error for {domain}: {e}")

    return alerts


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    log("=== Attack surface monitor starting ===")
    state = load_state()
    all_alerts = []

    all_alerts.extend(check_cert_transparency(state))
    all_alerts.extend(check_dns_records(state))
    all_alerts.extend(check_port_exposure(state))

    state["last_check"] = datetime.now().isoformat()
    save_state(state)

    if all_alerts:
        log(f"Found {len(all_alerts)} surface alert(s)")
        for trigger, details in all_alerts[:3]:
            fire_alert(trigger, details)
            time.sleep(5)
    else:
        log("Attack surface nominal — no unexpected changes")

    log("=== Attack surface monitor complete ===")


if __name__ == "__main__":
    run()
