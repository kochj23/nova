#!/usr/bin/env python3
"""
nova_crontab_audit.py — Daily crontab auditor across fleet.

Scans root and user crontabs on all managed hosts. Alerts on new/modified entries.
Posts P1 alert for root crontab changes, P2 for user changes.

Written by Jordan Koch.
"""

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2
import nova_config
from nova_notify import notify

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
STATE_FILE = Path.home() / ".openclaw/workspace/state/crontab_hashes.json"

HOSTS = [
    {"name": "mac-studio", "ip": "127.0.0.1", "local": True},
    {"name": "nuk", "ip": "192.168.1.10", "user": "kochj"},
    {"name": "lts01", "ip": "192.168.1.2", "user": "kochj"},
    {"name": "itunes", "ip": "192.168.1.7", "user": "kochj"},
]


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[crontab-audit {ts}] {msg}", flush=True)


def get_crontabs(host):
    """Get root and user crontabs from a host."""
    entries = {}
    if host.get("local"):
        for user in ["root", "kochj"]:
            try:
                r = subprocess.run(["sudo", "crontab", "-u", user, "-l"],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    entries[user] = r.stdout
            except Exception:
                pass
    else:
        ip = host["ip"]
        user = host["user"]
        for target_user in ["root", user]:
            try:
                cmd = f"sudo crontab -u {target_user} -l 2>/dev/null"
                r = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=10", f"{user}@{ip}", cmd],
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode == 0:
                    entries[target_user] = r.stdout
            except Exception:
                pass
    return entries


def load_state():
    """Load previous crontab hashes."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state):
    """Save current crontab hashes."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def hash_content(content):
    return hashlib.sha256(content.encode()).hexdigest()


def main():
    log("Starting daily crontab audit...")
    prev_state = load_state()
    new_state = {}
    changes = []

    for host in HOSTS:
        name = host["name"]
        log(f"  Checking {name}...")
        crontabs = get_crontabs(host)

        for user, content in crontabs.items():
            key = f"{name}:{user}"
            current_hash = hash_content(content)
            new_state[key] = {"hash": current_hash, "lines": len(content.splitlines())}

            prev = prev_state.get(key, {})
            if prev and prev.get("hash") != current_hash:
                changes.append({
                    "host": name,
                    "user": user,
                    "priority": 1 if user == "root" else 2,
                    "content": content,
                })
                log(f"    CHANGE DETECTED: {key}")
            elif not prev:
                log(f"    New baseline: {key} ({len(content.splitlines())} lines)")

    save_state(new_state)

    if changes:
        for change in changes:
            # Emit alert to the central bus. Root crontab changes are critical
            # (privileged scheduled-task tampering); user changes are warnings.
            priority = change["priority"]
            level = "critical" if priority == 1 else "warning"
            title = (
                f"Crontab change detected on {change['host']} "
                f"({change['user']}, P{priority})"
            )
            body = (
                f"Host: {change['host']} | User: {change['user']} | Priority: P{priority}\n"
                f"```\n{change['content'][:500]}\n```"
            )
            notify(
                title, body=body, level=level, category="security",
                dedup_key=f"crontab-change-{change['host']}-{change['user']}",
                meta={"host": change["host"], "user": change["user"], "priority": priority},
            )

            # Write to shared observations
            try:
                conn = psycopg2.connect(DB_DSN)
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
                    VALUES ('crontab_audit', 'security', %s, %s, %s, %s)
                """, (
                    f"Crontab changed on {change['host']}",
                    f"{change['user']} crontab modified",
                    "critical" if priority == 1 else "warning",
                    json.dumps({"host": change["host"], "user": change["user"]}),
                ))
                conn.commit()
                conn.close()
            except Exception:
                pass

        log(f"  {len(changes)} change(s) detected and alerted")
    else:
        log("  No changes detected")

    log("Done")


if __name__ == "__main__":
    main()
