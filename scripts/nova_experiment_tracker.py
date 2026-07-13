#!/usr/bin/env python3
"""
nova_experiment_tracker.py — Nag system for forgotten experimental containers.

Checks all Docker hosts for containers not in the protected list that have been
running beyond a threshold. Posts reminders to #nova-notifications.

Runs daily via scheduler.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_notify import notify

HOSTS = [
    {"name": "nuk", "ip": "192.168.1.10", "sudo": True},
]

PROTECTED_CONTAINERS = {
    "plex", "homebridge-homebridge-1", "searxng", "tinychat",
    "single-node-wazuh.manager-1", "single-node-wazuh.dashboard-1",
    "single-node-wazuh.indexer-1", "homebridge",
}

NAG_AFTER_HOURS = 24


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[experiment-tracker {ts}] {msg}", flush=True)


def check_host(host):
    """Check for unprotected long-running containers on a host."""
    ip = host["ip"]
    sudo = "sudo " if host.get("sudo") else ""

    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", f"kochj@{ip}",
             f"{sudo}docker ps --format '{{{{.Names}}}}\\t{{{{.RunningFor}}}}\\t{{{{.Image}}}}'"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0:
            return []
    except Exception as e:
        log(f"Failed to check {host['name']}: {e}")
        return []

    experiments = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, running_for, image = parts[0], parts[1], parts[2]

        if name in PROTECTED_CONTAINERS:
            continue

        # Parse running time
        hours = _parse_running_time(running_for)
        if hours > NAG_AFTER_HOURS:
            experiments.append({
                "host": host["name"],
                "container": name,
                "image": image,
                "running_hours": hours,
                "running_for": running_for,
            })

    return experiments


def _parse_running_time(running_for):
    """Parse Docker's 'Up X hours/days' format to hours."""
    running_for = running_for.lower()
    if "day" in running_for:
        try:
            days = int(running_for.split()[0])
            return days * 24
        except (ValueError, IndexError):
            return 0
    elif "hour" in running_for:
        try:
            return int(running_for.split()[0])
        except (ValueError, IndexError):
            return 0
    elif "minute" in running_for:
        return 0
    elif "week" in running_for:
        try:
            return int(running_for.split()[0]) * 168
        except (ValueError, IndexError):
            return 0
    return 0


def main():
    log("Checking for forgotten experimental containers...")
    all_experiments = []

    for host in HOSTS:
        experiments = check_host(host)
        all_experiments.extend(experiments)

    if not all_experiments:
        log("No forgotten experiments found.")
        return

    # Build nag message
    lines = [
        ":test_tube: *Experiment Tracker — Forgotten Containers*",
        "",
        f"Found {len(all_experiments)} container(s) running beyond {NAG_AFTER_HOURS}h threshold:",
        "",
    ]

    for exp in all_experiments:
        lines.append(f"  • `{exp['container']}` on {exp['host']} — {exp['running_for']} ({exp['image']})")

    lines.append("")
    lines.append("_These containers are not in the protected list. If they're still needed, add them. Otherwise: `docker stop <name>`_")

    body = "\n".join(lines[1:]).strip()
    notify(
        f"Experiment Tracker — {len(all_experiments)} forgotten container(s)",
        body=body,
        level="warning",
        category="docker",
        dedup_key="experiment-tracker-forgotten-containers",
    )
    log(f"Posted nag for {len(all_experiments)} experiment(s)")


if __name__ == "__main__":
    main()
