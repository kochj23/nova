#!/usr/bin/env python3
"""
nova_cinc_orchestrate.py — SSH-based configuration orchestrator for Nova's network.

Pushes configurations to managed nodes, runs convergence (CINC or shell-based),
tracks drift, and reports status. Works with or without CINC installed.

Modes:
  - CINC mode: Push cookbooks via rsync, run cinc-client --local-mode
  - Shell mode: Execute idempotent shell scripts via SSH (fallback for CINC-less nodes)

Usage:
  nova_cinc_orchestrate.py converge                    # converge all enabled nodes
  nova_cinc_orchestrate.py converge --node mac-studio  # single node
  nova_cinc_orchestrate.py drift                       # check all nodes for drift
  nova_cinc_orchestrate.py status                      # show node status from DB
  nova_cinc_orchestrate.py add <ip> <name> <os>        # register a new node

Written by Jordan Koch.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

CINC_BASE = Path("/Volumes/Data/AI/cinc")
COOKBOOKS = CINC_BASE / "cookbooks"
NODES_DIR = CINC_BASE / "nodes"
DB_DSN = "dbname=nova_ops user=kochj host=127.0.0.1"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
REMOTE_BASE = "/opt/nova-config"
LOG_FILE = Path.home() / ".openclaw/logs/nova_orchestrate.log"


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[orchestrate {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(text):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception:
        pass


# ── Database ──────────────────────────────────────────────────────────────────

def db_query(sql, params=None):
    """Execute a query and return rows."""
    try:
        import psycopg2
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor()
        cur.execute(sql, params or ())
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            rows = []
        conn.commit()
        conn.close()
        return rows
    except Exception as e:
        log(f"DB error: {e}", "ERROR")
        return []


def db_exec(sql, params=None):
    """Execute a statement (no return)."""
    try:
        import psycopg2
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor()
        cur.execute(sql, params or ())
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"DB exec error: {e}", "ERROR")


# ── SSH Helpers ───────────────────────────────────────────────────────────────

def ssh_exec(user, host, command, timeout=60):
    """Execute a command on a remote host via SSH. Returns (exit_code, stdout, stderr)."""
    cmd = ["ssh"] + SSH_OPTS + [f"{user}@{host}", command]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "SSH command timed out"
    except Exception as e:
        return -1, "", str(e)


def ssh_test(user, host):
    """Test SSH connectivity. Returns True if reachable."""
    code, out, _ = ssh_exec(user, host, "echo ok", timeout=10)
    return code == 0 and "ok" in out


def rsync_push(user, host, local_path, remote_path):
    """Push a directory to remote host via rsync."""
    cmd = ["rsync", "-avz", "--delete", "-e", f"ssh {' '.join(SSH_OPTS)}",
           f"{local_path}/", f"{user}@{host}:{remote_path}/"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode == 0
    except Exception:
        return False


# ── Node Management ───────────────────────────────────────────────────────────

def get_nodes(node_filter=None):
    """Get enabled nodes from database."""
    if node_filter:
        return db_query(
            "SELECT * FROM cinc_node_configs WHERE enabled = true AND node_name = %s",
            (node_filter,))
    return db_query("SELECT * FROM cinc_node_configs WHERE enabled = true ORDER BY node_name")


def add_node(ip, name, os_family, run_list=None):
    """Register a new node."""
    run_list = run_list or ["nova_base"]
    db_exec(
        "INSERT INTO cinc_node_configs (node_name, node_ip, os_family, run_list) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (node_name) DO UPDATE SET "
        "node_ip = EXCLUDED.node_ip, os_family = EXCLUDED.os_family, updated_at = now()",
        (name, ip, os_family, json.dumps(run_list)))
    log(f"Registered node: {name} ({ip}, {os_family})")


# ── Convergence ───────────────────────────────────────────────────────────────

def converge_node(node, dry_run=False):
    """Converge a single node. Returns (success, resources_updated, output)."""
    name = node["node_name"]
    ip = str(node["node_ip"])
    user = node.get("ssh_user", "kochj")
    os_family = node["os_family"]
    run_list = node.get("run_list", [])

    run_id = str(uuid.uuid4())[:12]
    start = time.time()
    mode_label = "drift-check" if dry_run else "converge"

    log(f"[{name}] Starting {mode_label}...")

    # Record run start
    db_exec(
        "INSERT INTO deployment_runs (run_id, node_name, node_ip, run_type, triggered_by) "
        "VALUES (%s, %s, %s, %s, 'orchestrator')",
        (run_id, name, ip, "drift_check" if dry_run else "converge"))

    # Local node (this machine)
    if ip in ("127.0.0.1", "192.168.1.6"):
        return _converge_local(node, run_id, start, dry_run)

    # Test SSH
    if not ssh_test(user, ip):
        duration = time.time() - start
        db_exec(
            "UPDATE deployment_runs SET ended_at = now(), status = 'failure', "
            "exit_code = -1, duration_s = %s, error_output = 'SSH unreachable' "
            "WHERE run_id = %s", (duration, run_id))
        log(f"[{name}] SSH unreachable", "ERROR")
        return False, 0, "SSH unreachable"

    # Push shell configs based on run_list
    resources = 0
    output_lines = []

    for recipe in (run_list if isinstance(run_list, list) else json.loads(run_list)):
        recipe_name = recipe.replace("recipe[", "").replace("]", "")
        rc, out, err = _apply_recipe_shell(user, ip, os_family, recipe_name, dry_run)
        if rc == 0:
            # Count changes
            changes = out.count("[changed]") + out.count("[created]")
            resources += changes
            output_lines.append(f"{recipe_name}: {changes} changes")
        else:
            output_lines.append(f"{recipe_name}: FAILED ({err[:100]})")

    duration = time.time() - start
    success = all("FAILED" not in l for l in output_lines)
    status = "success" if success else "failure"

    db_exec(
        "UPDATE deployment_runs SET ended_at = now(), status = %s, "
        "exit_code = %s, duration_s = %s, resources_updated = %s "
        "WHERE run_id = %s",
        (status, 0 if success else 1, duration, resources, run_id))
    db_exec(
        "UPDATE cinc_node_configs SET last_converge = now(), last_status = %s, "
        "updated_at = now() WHERE node_name = %s",
        (status, name))

    log(f"[{name}] {mode_label} {status}: {resources} resources, {duration:.1f}s")
    return success, resources, "\n".join(output_lines)


def _converge_local(node, run_id, start, dry_run):
    """Converge the local machine using direct commands."""
    name = node["node_name"]
    resources = 0
    output_lines = []

    # Check SSH hardening
    sshd_config = Path("/etc/ssh/sshd_config")
    if sshd_config.exists():
        content = sshd_config.read_text()
        if "PermitRootLogin no" in content and "PasswordAuthentication no" in content:
            output_lines.append("ssh_hardening: [ok] already hardened")
        else:
            output_lines.append("ssh_hardening: [drift] not fully hardened")
            resources += 1

    # Check syslog forwarder
    forwarder_plist = Path("/Library/LaunchDaemons/net.digitalnoise.nova-syslog-forwarder.plist")
    if forwarder_plist.exists():
        output_lines.append("syslog_forwarder: [ok] plist exists")
    else:
        output_lines.append("syslog_forwarder: [drift] plist missing")
        resources += 1

    duration = time.time() - start
    status = "success"

    db_exec(
        "UPDATE deployment_runs SET ended_at = now(), status = %s, "
        "exit_code = 0, duration_s = %s, resources_updated = %s "
        "WHERE run_id = %s",
        (status, duration, resources, run_id))
    db_exec(
        "UPDATE cinc_node_configs SET last_converge = now(), last_status = %s, "
        "updated_at = now() WHERE node_name = %s",
        (status, name))

    output = "\n".join(output_lines)
    log(f"[{name}] local check: {resources} drift items, {duration:.1f}s")
    return True, resources, output


def _apply_recipe_shell(user, ip, os_family, recipe_name, dry_run):
    """Apply a recipe via SSH shell commands (idempotent)."""
    if recipe_name == "nova_base":
        return _recipe_nova_base(user, ip, os_family, dry_run)
    elif recipe_name == "nova_monitoring":
        return _recipe_nova_monitoring(user, ip, os_family, dry_run)
    elif recipe_name == "nova_security":
        return _recipe_nova_security(user, ip, os_family, dry_run)
    elif recipe_name == "nova_linux":
        return _recipe_nova_linux(user, ip, os_family, dry_run)
    elif recipe_name == "nova_macos":
        return 0, "nova_macos: [skipped] macOS managed locally", ""
    else:
        return 0, f"{recipe_name}: [skipped] no shell handler", ""


def _recipe_nova_base(user, ip, os_family, dry_run):
    """nova_base: SSH hardening + syslog forwarding."""
    checks = []

    if os_family == "linux":
        # Check SSH config
        rc, out, _ = ssh_exec(user, ip,
            "grep -q '^PermitRootLogin no' /etc/ssh/sshd_config && "
            "grep -q '^PasswordAuthentication no' /etc/ssh/sshd_config && echo ok || echo drift")
        if "ok" in out:
            checks.append("[ok] SSH hardened")
        else:
            checks.append("[drift] SSH not hardened")
            if not dry_run:
                ssh_exec(user, ip,
                    "sudo sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config && "
                    "sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && "
                    "sudo systemctl reload sshd")
                checks[-1] = "[changed] SSH hardened"

        # Check syslog forwarding
        rc, out, _ = ssh_exec(user, ip,
            "test -f /etc/rsyslog.d/60-nova.conf && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] syslog forwarding configured")
        else:
            checks.append("[drift] syslog forwarding missing")
            if not dry_run:
                ssh_exec(user, ip,
                    "echo '*.* @192.168.1.6:1514' | sudo tee /etc/rsyslog.d/60-nova.conf > /dev/null && "
                    "sudo systemctl restart rsyslog")
                checks[-1] = "[created] syslog forwarding"

    return 0, "\n".join(checks), ""


def _recipe_nova_monitoring(user, ip, os_family, dry_run):
    """nova_monitoring: Install and configure snmpd."""
    checks = []

    if os_family == "linux":
        # Check snmpd installed
        rc, out, _ = ssh_exec(user, ip, "dpkg -l snmpd 2>/dev/null | grep -q '^ii' && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] snmpd installed")
        else:
            checks.append("[drift] snmpd not installed")
            if not dry_run:
                ssh_exec(user, ip, "sudo apt-get install -y snmpd snmp > /dev/null 2>&1", timeout=120)
                checks[-1] = "[changed] snmpd installed"

        # Check snmpd config allows community
        rc, out, _ = ssh_exec(user, ip,
            "grep -q 'rocommunity public' /etc/snmp/snmpd.conf && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] snmpd community configured")
        else:
            checks.append("[drift] snmpd community not configured")
            if not dry_run:
                ssh_exec(user, ip,
                    "echo 'rocommunity public 192.168.1.0/24' | sudo tee -a /etc/snmp/snmpd.conf > /dev/null && "
                    "sudo systemctl restart snmpd")
                checks[-1] = "[changed] snmpd community configured"

        # Check snmpd listening on all interfaces
        rc, out, _ = ssh_exec(user, ip,
            "grep -q '^agentAddress udp:161' /etc/snmp/snmpd.conf && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] snmpd listening on all interfaces")
        else:
            checks.append("[drift] snmpd only on localhost")
            if not dry_run:
                ssh_exec(user, ip,
                    "sudo sed -i 's/^agentAddress.*/agentAddress udp:161/' /etc/snmp/snmpd.conf && "
                    "sudo systemctl restart snmpd")
                checks[-1] = "[changed] snmpd listening on all interfaces"

    return 0, "\n".join(checks), ""


def _recipe_nova_security(user, ip, os_family, dry_run):
    """nova_security: Install rkhunter, chkrootkit, AIDE, osquery."""
    checks = []

    if os_family == "linux":
        # rkhunter
        rc, out, _ = ssh_exec(user, ip, "which rkhunter > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] rkhunter installed")
        else:
            checks.append("[drift] rkhunter not installed")
            if not dry_run:
                ssh_exec(user, ip, "sudo apt-get install -y rkhunter > /dev/null 2>&1", timeout=120)
                ssh_exec(user, ip, "sudo rkhunter --propupd > /dev/null 2>&1", timeout=60)
                checks[-1] = "[changed] rkhunter installed + baseline set"

        # chkrootkit
        rc, out, _ = ssh_exec(user, ip, "which chkrootkit > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] chkrootkit installed")
        else:
            checks.append("[drift] chkrootkit not installed")
            if not dry_run:
                ssh_exec(user, ip, "sudo apt-get install -y chkrootkit > /dev/null 2>&1", timeout=120)
                checks[-1] = "[changed] chkrootkit installed"

        # AIDE
        rc, out, _ = ssh_exec(user, ip, "which aide > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] aide installed")
        else:
            checks.append("[drift] aide not installed")
            if not dry_run:
                ssh_exec(user, ip,
                    "sudo apt-get install -y aide > /dev/null 2>&1 && "
                    "sudo aideinit > /dev/null 2>&1 && "
                    "sudo cp /var/lib/aide/aide.db.new /var/lib/aide/aide.db 2>/dev/null",
                    timeout=180)
                checks[-1] = "[changed] aide installed + database initialized"

        # osquery
        rc, out, _ = ssh_exec(user, ip, "which osqueryi > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] osquery installed")
        else:
            checks.append("[drift] osquery not installed")
            if not dry_run:
                ssh_exec(user, ip,
                    "curl -fsSL https://pkg.osquery.io/deb/pubkey.gpg | sudo gpg --dearmor -o /usr/share/keyrings/osquery-archive-keyring.gpg 2>/dev/null && "
                    "echo 'deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/osquery-archive-keyring.gpg] https://pkg.osquery.io/deb deb main' | sudo tee /etc/apt/sources.list.d/osquery.list > /dev/null && "
                    "sudo apt-get update -qq > /dev/null 2>&1 && "
                    "sudo apt-get install -y osquery > /dev/null 2>&1",
                    timeout=180)
                checks[-1] = "[changed] osquery installed"

    elif os_family == "macos":
        # rkhunter via brew (remote macOS)
        rc, out, _ = ssh_exec(user, ip,
            "/opt/homebrew/bin/brew list rkhunter > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] rkhunter installed")
        else:
            checks.append("[drift] rkhunter not installed")
            if not dry_run:
                ssh_exec(user, ip,
                    "/opt/homebrew/bin/brew install rkhunter > /dev/null 2>&1",
                    timeout=180)
                checks[-1] = "[changed] rkhunter installed"

        # osquery via brew
        rc, out, _ = ssh_exec(user, ip,
            "/opt/homebrew/bin/brew list osquery > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] osquery installed")
        else:
            checks.append("[drift] osquery not installed")
            if not dry_run:
                ssh_exec(user, ip,
                    "/opt/homebrew/bin/brew install osquery > /dev/null 2>&1",
                    timeout=180)
                checks[-1] = "[changed] osquery installed"

    return 0, "\n".join(checks), ""


def _recipe_nova_linux(user, ip, os_family, dry_run):
    """nova_linux: Hardening, fail2ban, UFW, unattended-upgrades."""
    checks = []

    if os_family != "linux":
        return 0, "nova_linux: [skipped] not linux", ""

    # fail2ban
    rc, out, _ = ssh_exec(user, ip, "dpkg -l fail2ban 2>/dev/null | grep -q '^ii' && echo ok || echo missing")
    if "ok" in out:
        checks.append("[ok] fail2ban installed")
    else:
        checks.append("[drift] fail2ban not installed")
        if not dry_run:
            ssh_exec(user, ip, "sudo apt-get install -y fail2ban > /dev/null 2>&1", timeout=120)
            checks[-1] = "[changed] fail2ban installed"

    # ufw
    rc, out, _ = ssh_exec(user, ip, "sudo ufw status | grep -q 'Status: active' && echo ok || echo inactive")
    if "ok" in out:
        checks.append("[ok] UFW active")
    else:
        checks.append("[drift] UFW not active")
        if not dry_run:
            ssh_exec(user, ip,
                "sudo apt-get install -y ufw > /dev/null 2>&1 && "
                "sudo ufw allow 22 > /dev/null && "
                "sudo ufw allow 161 > /dev/null && "
                "sudo ufw allow from 192.168.1.6 > /dev/null && "
                "echo y | sudo ufw enable > /dev/null 2>&1")
            checks[-1] = "[changed] UFW enabled with rules"

    # unattended-upgrades
    rc, out, _ = ssh_exec(user, ip,
        "dpkg -l unattended-upgrades 2>/dev/null | grep -q '^ii' && echo ok || echo missing")
    if "ok" in out:
        checks.append("[ok] unattended-upgrades installed")
    else:
        checks.append("[drift] unattended-upgrades not installed")
        if not dry_run:
            ssh_exec(user, ip, "sudo apt-get install -y unattended-upgrades > /dev/null 2>&1", timeout=120)
            checks[-1] = "[changed] unattended-upgrades installed"

    return 0, "\n".join(checks), ""


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_converge(args):
    """Converge all enabled nodes (or specific node)."""
    nodes = get_nodes(args.node)
    if not nodes:
        log("No enabled nodes found")
        return

    log(f"Converging {len(nodes)} node(s)...")
    results = []

    for node in nodes:
        success, resources, output = converge_node(node, dry_run=args.dry_run)
        results.append((node["node_name"], success, resources))
        if output:
            for line in output.split("\n"):
                if line.strip():
                    log(f"  {line}")

    # Summary
    total = len(results)
    passed = sum(1 for _, s, _ in results if s)
    total_resources = sum(r for _, _, r in results)

    summary = (
        f":gear: *Orchestrator {'Drift Check' if args.dry_run else 'Converge'}*\n"
        f"  Nodes: {passed}/{total} OK\n"
        f"  Resources {'drifted' if args.dry_run else 'updated'}: {total_resources}"
    )
    notify(summary)
    log(f"Complete: {passed}/{total} OK, {total_resources} resources")


def cmd_drift(args):
    """Check all nodes for configuration drift (dry-run converge)."""
    args.dry_run = True
    args.node = getattr(args, "node", None)
    cmd_converge(args)


def cmd_status(args):
    """Show node status from database."""
    rows = db_query(
        "SELECT node_name, node_ip, os_family, last_status, last_converge, enabled "
        "FROM cinc_node_configs ORDER BY node_name")
    if not rows:
        print("No nodes registered")
        return

    print(f"{'Name':<15} {'IP':<16} {'OS':<8} {'Status':<10} {'Last Converge':<20} {'Enabled'}")
    print("-" * 85)
    for r in rows:
        lc = r["last_converge"].strftime("%Y-%m-%d %H:%M") if r["last_converge"] else "never"
        print(f"{r['node_name']:<15} {str(r['node_ip']):<16} {r['os_family']:<8} "
              f"{r['last_status'] or 'unknown':<10} {lc:<20} {'yes' if r['enabled'] else 'no'}")


def cmd_add(args):
    """Register a new node."""
    add_node(args.ip, args.name, args.os_family, args.run_list.split(",") if args.run_list else None)
    print(f"Added: {args.name} ({args.ip}, {args.os_family})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nova configuration orchestrator")
    sub = parser.add_subparsers(dest="command")

    p_converge = sub.add_parser("converge", help="Converge nodes")
    p_converge.add_argument("--node", help="Specific node name")
    p_converge.add_argument("--dry-run", action="store_true", help="Show what would change")

    p_drift = sub.add_parser("drift", help="Check for drift (dry-run)")
    p_drift.add_argument("--node", help="Specific node name")

    sub.add_parser("status", help="Show node status")

    p_add = sub.add_parser("add", help="Register a new node")
    p_add.add_argument("ip", help="Node IP address")
    p_add.add_argument("name", help="Node name")
    p_add.add_argument("os_family", choices=["macos", "linux"], help="OS family")
    p_add.add_argument("--run-list", help="Comma-separated recipes", default="nova_base")

    args = parser.parse_args()

    if args.command == "converge":
        cmd_converge(args)
    elif args.command == "drift":
        cmd_drift(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "add":
        cmd_add(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
