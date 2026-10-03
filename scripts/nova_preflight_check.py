#!/usr/bin/env python3
"""
nova_preflight_check.py — Pre-flight validation before installing software on core servers.

Checks resource headroom, running services, and potential conflicts before
allowing a new package/container to be deployed. Called by deployment scripts
or manually via: python3 nova_preflight_check.py --host nova-core5 --package openwebui

Returns exit code 0 = safe to proceed, 1 = blocked.

Written by Jordan Koch.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2

DB_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

HOSTS = {
    "mac-studio": {"ip": "192.168.1.6", "min_cpu": 20, "min_mem": 20, "min_disk": 15},
    "nova-core5": {"ip": "192.168.1.10", "min_cpu": 30, "min_mem": 25, "min_disk": 20},
    "mac-mini": {"ip": "192.168.1.77", "min_cpu": 20, "min_mem": 20, "min_disk": 15},
    "nova-core": {"ip": "192.168.1.2", "min_cpu": 30, "min_mem": 25, "min_disk": 20},
}


def get_current_headroom(host):
    """Get latest capacity snapshot for a host."""
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()
    cur.execute("""
        SELECT cpu_headroom_pct, mem_headroom_pct, disk_worst_pct, overall_status
        FROM capacity_snapshots
        WHERE device_name = %s
        ORDER BY ts DESC LIMIT 1
    """, (host,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "cpu_headroom": row[0],
            "mem_headroom": row[1],
            "disk_used": row[2],
            "status": row[3],
        }
    return None


def check_docker_resources(host_ip):
    """Check Docker resource usage on remote host."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", f"kochj@{host_ip}",
             "sudo docker stats --no-stream --format '{{json .}}' 2>/dev/null"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0 and r.stdout.strip():
            containers = []
            for line in r.stdout.strip().split('\n'):
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            return containers
    except Exception:
        pass
    return []


def run_preflight(host, package=None):
    """Run all pre-flight checks. Returns (pass: bool, messages: list)."""
    messages = []
    passed = True

    config = HOSTS.get(host)
    if not config:
        messages.append(f"WARN: Unknown host '{host}' — no thresholds defined")
        config = {"ip": None, "min_cpu": 25, "min_mem": 20, "min_disk": 20}

    # Check 1: Current headroom
    headroom = get_current_headroom(host)
    if not headroom:
        messages.append(f"WARN: No capacity data for {host} — cannot verify headroom")
    else:
        if headroom["status"] == "CRIT":
            messages.append(f"BLOCK: {host} is in CRITICAL status — do not install")
            passed = False
        if headroom["cpu_headroom"] is not None and headroom["cpu_headroom"] < config["min_cpu"]:
            messages.append(f"BLOCK: CPU headroom {headroom['cpu_headroom']:.1f}% < minimum {config['min_cpu']}%")
            passed = False
        elif headroom["cpu_headroom"] is not None:
            messages.append(f"OK: CPU headroom {headroom['cpu_headroom']:.1f}%")
        if headroom["mem_headroom"] is not None and headroom["mem_headroom"] < config["min_mem"]:
            messages.append(f"BLOCK: Memory headroom {headroom['mem_headroom']:.1f}% < minimum {config['min_mem']}%")
            passed = False
        elif headroom["mem_headroom"] is not None:
            messages.append(f"OK: Memory headroom {headroom['mem_headroom']:.1f}%")
        if headroom["disk_used"] is not None and (100 - headroom["disk_used"]) < config["min_disk"]:
            messages.append(f"BLOCK: Disk free {100 - headroom['disk_used']:.1f}% < minimum {config['min_disk']}%")
            passed = False
        elif headroom["disk_used"] is not None:
            messages.append(f"OK: Disk {headroom['disk_used']:.1f}% used")

    # Check 2: Docker container count (if applicable)
    if config.get("ip"):
        containers = check_docker_resources(config["ip"])
        if containers:
            messages.append(f"INFO: {len(containers)} Docker containers running")
            high_cpu = [c for c in containers if float(c.get("CPUPerc", "0%").rstrip('%')) > 50]
            if high_cpu:
                names = [c.get("Name", "?") for c in high_cpu]
                messages.append(f"WARN: High CPU containers: {', '.join(names)}")

    # Check 3: Recent incidents on this host
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM shared_observations
            WHERE subject ILIKE %s AND severity IN ('critical', 'warning')
            AND observed_at > NOW() - INTERVAL '2 hours'
        """, (f"%{host}%",))
        recent_issues = cur.fetchone()[0]
        conn.close()
        if recent_issues > 3:
            messages.append(f"WARN: {recent_issues} recent issues on {host} in last 2h — proceed with caution")
    except Exception:
        pass

    return passed, messages


def main():
    parser = argparse.ArgumentParser(description="Pre-flight check for software installation")
    parser.add_argument("--host", required=True, help="Target host name")
    parser.add_argument("--package", help="Package/container being installed")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    passed, messages = run_preflight(args.host, args.package)

    if args.json:
        print(json.dumps({"passed": passed, "messages": messages}))
    else:
        status = "PASS" if passed else "BLOCKED"
        print(f"\n{'='*50}")
        print(f"Pre-flight Check: {args.host} — {status}")
        print(f"{'='*50}")
        for msg in messages:
            print(f"  {msg}")
        print(f"{'='*50}\n")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
