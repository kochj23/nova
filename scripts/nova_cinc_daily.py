#!/usr/bin/env python3
"""
nova_cinc_daily.py — Daily CINC operations: inventory, updates, drift detection.

Runs daily at 3 AM. For each managed node:
  1. Collect software inventory (brew/apt/snap packages)
  2. Apply OS/package updates (full auto on Linux, brew upgrade on Mac)
  3. Run CINC converge to enforce desired state
  4. Detect drift (configs changed outside CINC)
  5. Report results to PG, Slack, and shared observations

Written by Jordan Koch.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2
import psycopg2.extras
import nova_config

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
SSH_OPTS = ["-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no"]


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[cinc-daily {ts}] {msg}", flush=True)


def pg_connect():
    return psycopg2.connect(DB_DSN)


def ssh_cmd(host, user, cmd, timeout=60):
    """Run command on remote host via SSH."""
    try:
        r = subprocess.run(
            ["ssh"] + SSH_OPTS + [f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def get_nodes():
    """Get enabled nodes from PG."""
    conn = pg_connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM cinc_node_configs WHERE enabled = TRUE;")
    nodes = cur.fetchall()
    conn.close()
    return nodes


# ── Step 1: Software Inventory ────────────────────────────────────────────────

def collect_inventory(node):
    """Collect package list from a node."""
    name = node["node_name"]
    host = str(node["node_ip"])
    user = node["ssh_user"]
    os_family = node["os_family"]

    packages = []

    if os_family == "macos":
        # Homebrew packages
        # Use local command if this is the local machine
        if host in ("127.0.0.1", "192.168.1.6"):
            try:
                r = subprocess.run(["/opt/homebrew/bin/brew", "list", "--versions"], capture_output=True, text=True, timeout=30)
                rc, out = r.returncode, r.stdout
            except Exception:
                rc, out = 1, ""
        else:
            rc, out, _ = ssh_cmd(host, user, "/opt/homebrew/bin/brew list --versions 2>/dev/null")
        if rc == 0:
            for line in out.strip().split("\n"):
                if line.strip():
                    parts = line.split()
                    if len(parts) >= 2:
                        packages.append({
                            "name": parts[0],
                            "version": parts[-1],
                            "source": "homebrew",
                        })

    elif os_family == "linux":
        # dpkg packages
        rc, out, _ = ssh_cmd(host, user,
                             "dpkg-query -W -f='${Package}|${Version}|${Architecture}\\n' 2>/dev/null",
                             timeout=30)
        if rc == 0:
            for line in out.strip().split("\n"):
                parts = line.split("|")
                if len(parts) >= 2:
                    packages.append({
                        "name": parts[0],
                        "version": parts[1],
                        "source": "apt",
                        "arch": parts[2] if len(parts) > 2 else None,
                    })

    # Store in PG
    if packages:
        conn = pg_connect()
        cur = conn.cursor()
        for pkg in packages:
            cur.execute("""
                INSERT INTO software_inventory (host_name, package_name, version, source, arch)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING;
            """, (name, pkg["name"], pkg["version"], pkg["source"], pkg.get("arch")))
        conn.commit()
        conn.close()

    log(f"  {name}: {len(packages)} packages inventoried")
    return packages


# ── Step 2: Apply Updates ─────────────────────────────────────────────────────

def apply_updates(node):
    """Apply OS/package updates to a node."""
    name = node["node_name"]
    host = str(node["node_ip"])
    user = node["ssh_user"]
    os_family = node["os_family"]

    updates_applied = []

    if os_family == "linux":
        # Full auto update
        log(f"  {name}: Running apt update + upgrade...")
        rc, out, err = ssh_cmd(host, user,
                               "sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq 2>&1 | grep -E 'upgraded|installed|removed'",
                               timeout=300)
        if rc == 0 and out.strip():
            updates_applied.append({"action": "apt-upgrade", "output": out.strip()[:500]})
            log(f"  {name}: {out.strip()}")

        # Also run dist-upgrade for kernel/security patches
        rc, out, _ = ssh_cmd(host, user,
                             "sudo DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y -qq 2>&1 | grep -E 'upgraded|installed'",
                             timeout=300)
        if rc == 0 and out.strip():
            updates_applied.append({"action": "dist-upgrade", "output": out.strip()[:500]})

        # Clean up
        ssh_cmd(host, user, "sudo apt-get autoremove -y -qq && sudo apt-get autoclean -qq", timeout=60)

    elif os_family == "macos":
        # Homebrew upgrade
        log(f"  {name}: Running brew upgrade...")
        rc, out, _ = ssh_cmd(host, user,
                             "/opt/homebrew/bin/brew update -q 2>/dev/null && /opt/homebrew/bin/brew upgrade 2>&1 | grep -E 'Upgrading|Pouring|==>.*Upgrading'",
                             timeout=300)
        if rc == 0 and out.strip():
            updates_applied.append({"action": "brew-upgrade", "output": out.strip()[:500]})
            log(f"  {name}: {out.strip()[:200]}")
        else:
            log(f"  {name}: All packages up to date")

    # Record updates in PG
    if updates_applied:
        conn = pg_connect()
        cur = conn.cursor()
        for u in updates_applied:
            cur.execute("""
                INSERT INTO package_updates (host_name, package_name, new_version, source, status)
                VALUES (%s, %s, %s, %s, 'applied');
            """, (name, u["action"], u["output"][:200], os_family))
        conn.commit()
        conn.close()

    return updates_applied


# ── Step 3: CINC Converge ─────────────────────────────────────────────────────

def run_converge(node):
    """Run CINC converge on a node (delegates to nova_cinc_orchestrate.py)."""
    name = node["node_name"]
    start = time.time()

    try:
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "nova_cinc_orchestrate.py"),
             "converge", "--node", name],
            capture_output=True, text=True, timeout=300
        )
        duration = time.time() - start
        success = r.returncode == 0

        # Parse resources updated from output
        resources = 0
        for line in r.stdout.split("\n"):
            if "resources updated" in line.lower():
                try:
                    resources = int(line.split(":")[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass

        # Record run
        conn = pg_connect()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO cinc_runs (node_name, status, duration_s, resources_updated, error)
            VALUES (%s, %s, %s, %s, %s);
        """, (name, "success" if success else "failed", duration, resources,
              r.stderr[:500] if not success else None))

        # Update node config
        cur.execute("""
            UPDATE cinc_node_configs SET last_converge = NOW(), last_status = %s, updated_at = NOW()
            WHERE node_name = %s;
        """, ("success" if success else "failed", name))

        conn.commit()
        conn.close()

        log(f"  {name}: converge {'OK' if success else 'FAILED'} ({duration:.1f}s, {resources} resources)")
        return success

    except Exception as e:
        log(f"  {name}: converge exception: {e}")
        return False


# ── Step 4: Drift Detection ───────────────────────────────────────────────────

def detect_drift(node):
    """Check for configuration drift on a node."""
    name = node["node_name"]
    host = str(node["node_ip"])
    user = node["ssh_user"]
    os_family = node["os_family"]

    drift_items = []

    if os_family == "linux":
        # Check if key services are running
        services = ["wazuh-agent", "sshd"]
        for svc in services:
            rc, _, _ = ssh_cmd(host, user, f"systemctl is-active {svc} 2>/dev/null")
            if rc != 0:
                drift_items.append({"type": "service_down", "service": svc})

        # Check if rkhunter is installed
        rc, _, _ = ssh_cmd(host, user, "which rkhunter 2>/dev/null")
        if rc != 0:
            drift_items.append({"type": "package_missing", "package": "rkhunter"})

    elif os_family == "macos":
        # Check launchd services
        services_to_check = ["net.digitalnoise.nova-memory-server", "com.nova.scheduler"]
        for svc in services_to_check:
            rc, out, _ = ssh_cmd(host, user, f"launchctl list 2>/dev/null | grep {svc}")
            if rc != 0 or not out.strip():
                drift_items.append({"type": "service_missing", "service": svc})

    if drift_items:
        conn = pg_connect()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO cinc_runs (node_name, status, drift_detected, drift_details)
            VALUES (%s, 'drift', TRUE, %s);
        """, (name, json.dumps(drift_items)))

        # Write as shared observation
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES ('cinc', 'security', %s, %s, 'warning', %s);
        """, (
            f"Configuration drift on {name}",
            f"{len(drift_items)} drift items: {', '.join(d.get('service', d.get('package', '?')) for d in drift_items)}",
            json.dumps({"node": name, "drift": drift_items}),
        ))

        conn.commit()
        conn.close()
        log(f"  {name}: DRIFT DETECTED — {len(drift_items)} items")
    else:
        log(f"  {name}: no drift")

    return drift_items


# ── Reporting ─────────────────────────────────────────────────────────────────

def send_report(results):
    """Post daily CINC summary to Slack."""
    lines = [":gear: *CINC Daily Operations Report*", ""]

    total_pkgs = sum(r.get("inventory_count", 0) for r in results)
    total_updates = sum(len(r.get("updates", [])) for r in results)
    total_drift = sum(len(r.get("drift", [])) for r in results)
    failed_converge = [r["node"] for r in results if not r.get("converge_ok", True)]

    lines.append(f"*Fleet:* {len(results)} nodes converged")
    lines.append(f"*Inventory:* {total_pkgs} packages cataloged")
    lines.append(f"*Updates applied:* {total_updates}")
    lines.append(f"*Drift detected:* {total_drift} items")

    if failed_converge:
        lines.append(f"*:warning: Failed converge:* {', '.join(failed_converge)}")

    if total_drift > 0:
        lines.append("")
        lines.append("*Drift details:*")
        for r in results:
            if r.get("drift"):
                for d in r["drift"]:
                    lines.append(f"  • {r['node']}: {d.get('type', '?')} — {d.get('service', d.get('package', '?'))}")

    if total_updates > 0:
        lines.append("")
        lines.append("*Updates:*")
        for r in results:
            if r.get("updates"):
                for u in r["updates"]:
                    lines.append(f"  • {r['node']}: {u.get('action', '?')}")

    try:
        nova_config.post_both("\n".join(lines))
    except Exception as e:
        log(f"Slack post failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=== CINC Daily Operations starting ===")

    nodes = get_nodes()
    log(f"Managing {len(nodes)} nodes")

    results = []

    for node in nodes:
        name = node["node_name"]
        log(f"\n── {name} ({node['node_ip']}, {node['os_family']}) ──")

        result = {"node": name}

        # Inventory
        pkgs = collect_inventory(node)
        result["inventory_count"] = len(pkgs)

        # Updates
        updates = apply_updates(node)
        result["updates"] = updates

        # Converge
        ok = run_converge(node)
        result["converge_ok"] = ok

        # Drift detection
        drift = detect_drift(node)
        result["drift"] = drift

        results.append(result)

    # Report
    send_report(results)

    log("\n=== CINC Daily Operations complete ===")


if __name__ == "__main__":
    main()
