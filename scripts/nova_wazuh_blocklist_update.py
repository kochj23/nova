#!/usr/bin/env python3
"""
nova_wazuh_blocklist_update.py — Weekly CDB blocklist update from public threat intel.

Pulls IPs, domains, and hashes from free threat intel feeds and pushes them
to the Wazuh manager's CDB lists. Runs weekly via Nova scheduler.

Sources:
  - abuse.ch Feodo Tracker (C2 IPs)
  - abuse.ch URLhaus (malicious domains)
  - abuse.ch MalwareBazaar (malware hashes)
  - Emerging Threats compromised IPs

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

WAZUH_HOST = "192.168.1.7"
DOCKER_CMD = ["ssh", "-o", "ConnectTimeout=10", f"kochj@{WAZUH_HOST}",
              "PATH=/usr/local/bin:$PATH docker exec single-node-wazuh.manager-1"]

# Free threat intel feeds (no API key required)
FEEDS = {
    "ip-blocklist": [
        ("https://feodotracker.abuse.ch/downloads/ipblocklist_recommended.txt", "feodo_c2"),
        ("https://rules.emergingthreats.net/blockrules/compromised-ips.txt", "et_compromised"),
    ],
    "suspicious-domains": [
        ("https://urlhaus.abuse.ch/downloads/text_online/", "urlhaus_active"),
    ],
    "hash-blocklist": [
        ("https://bazaar.abuse.ch/export/txt/sha256/recent/", "malwarebazaar_recent"),
    ],
}

MAX_IPS = 5000
MAX_DOMAINS = 3000
MAX_HASHES = 2000


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[blocklist-update {ts}] {msg}", flush=True)


def fetch_feed(url, timeout=30):
    """Fetch a text feed and return non-comment, non-empty lines."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nova-ThreatIntel/1.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        lines = resp.read().decode(errors="ignore").splitlines()
        return [l.strip() for l in lines
                if l.strip() and not l.startswith("#") and not l.startswith("//")]
    except Exception as e:
        log(f"  Failed to fetch {url}: {e}")
        return []


def build_cdb_content(list_name):
    """Build CDB list content from all feeds for a given list."""
    entries = {}
    for url, tag in FEEDS.get(list_name, []):
        log(f"  Fetching {tag}...")
        lines = fetch_feed(url)
        for line in lines:
            # CDB format: key:value (value is the tag/source)
            key = line.split()[0] if line.split() else line
            # Skip non-IP/domain/hash looking entries
            if list_name == "ip-blocklist":
                if not all(c in "0123456789." for c in key):
                    continue
            elif list_name == "hash-blocklist":
                if len(key) != 64:  # SHA256
                    continue
            entries[key] = tag
        log(f"    Got {len(lines)} raw lines, {len(entries)} valid entries so far")

    # Apply limits
    limits = {"ip-blocklist": MAX_IPS, "suspicious-domains": MAX_DOMAINS, "hash-blocklist": MAX_HASHES}
    max_entries = limits.get(list_name, 5000)
    if len(entries) > max_entries:
        entries = dict(list(entries.items())[:max_entries])

    # Build CDB format
    cdb_lines = [f"{k}:{v}" for k, v in entries.items()]
    return "\n".join(cdb_lines) + "\n", len(entries)


def push_to_wazuh(list_name, content):
    """Push CDB content to Wazuh manager container."""
    # Write to temp file then docker cp
    try:
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", f"kochj@{WAZUH_HOST}",
             f"PATH=/usr/local/bin:$PATH docker exec -i single-node-wazuh.manager-1 "
             f"tee /var/ossec/etc/lists/{list_name} > /dev/null"],
            input=content.encode(), capture_output=True, timeout=30, check=True
        )
        return True
    except Exception as e:
        log(f"  Failed to push {list_name}: {e}")
        return False


def main():
    log("Starting weekly CDB blocklist update...")
    total_entries = 0

    for list_name in FEEDS:
        log(f"Building {list_name}...")
        content, count = build_cdb_content(list_name)
        if count > 0:
            if push_to_wazuh(list_name, content):
                log(f"  Pushed {count} entries to {list_name}")
                total_entries += count
            else:
                log(f"  FAILED to push {list_name}")
        else:
            log(f"  No entries for {list_name} — feeds may be down")

    if total_entries > 0:
        # Restart Wazuh manager to reload CDB lists
        log("Restarting Wazuh manager to reload lists...")
        try:
            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", f"kochj@{WAZUH_HOST}",
                 "PATH=/usr/local/bin:$PATH docker exec single-node-wazuh.manager-1 /var/ossec/bin/wazuh-control restart"],
                capture_output=True, timeout=60
            )
            log("Wazuh manager restarted")
        except Exception as e:
            log(f"Wazuh restart failed: {e}")

    log(f"Done — {total_entries} total entries across all lists")


if __name__ == "__main__":
    main()
