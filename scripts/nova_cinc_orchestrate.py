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
from nova_notify import notify as nova_notify

# ── Config ────────────────────────────────────────────────────────────────────

CINC_BASE = Path("/Volumes/Data/AI/cinc")
COOKBOOKS = CINC_BASE / "cookbooks"
NODES_DIR = CINC_BASE / "nodes"
import nova_dsn as _nova_dsn  # noqa: E402
DB_DSN = _nova_dsn.pg_dsn("nova_ops")   # was a hardcoded host string (2026-10-15, #60)
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


def notify(text, level="info", category="scheduler", dedup_key=None):
    parts = text.split("\n", 1)
    title = parts[0].lstrip(": ").replace("gear:", "").strip()
    body = parts[1] if len(parts) > 1 else None
    nova_notify(title, body=body, level=level, category=category, dedup_key=dedup_key)


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


def is_local(ip) -> bool:
    """Is `ip` this machine? Shares nova_cinc_daily.is_local_host (own addresses); falls back to
    loopback only, so a missing helper never SSHes a host into itself silently."""
    try:
        from nova_cinc_daily import is_local_host
        return bool(is_local_host(ip))
    except Exception:  # noqa: BLE001
        return str(ip).split("/")[0] in ("127.0.0.1", "localhost", "::1")


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

    # Local node (this machine) — decided from this host's own addresses, not a hardcoded .6
    # (from scheduler-core on .2 every Mac + nova-core itself was "SSH unreachable", #60)
    if is_local(ip):
        return _converge_local(node, run_id, start, dry_run)

    # Test SSH. Unreachable is NOT a failed deployment — nothing was changed — so it is recorded as
    # status 'unreachable' and stays out of the change-failure rate (152 of 152 "failures" in the
    # 30 days to 2026-10-15 were this; Little Mister, 2026-10-15, #60).
    if not ssh_test(user, ip):
        duration = time.time() - start
        db_exec(
            "UPDATE deployment_runs SET ended_at = now(), status = 'unreachable', "
            "exit_code = -1, duration_s = %s, error_output = 'SSH unreachable' "
            "WHERE run_id = %s", (duration, run_id))
        log(f"[{name}] SSH unreachable", "ERROR")
        return False, 0, "SSH unreachable"

    resources, output_lines = _run_recipes(node, dry_run)

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


def _run_recipes(node, dry_run):
    """Run the node's run_list in shell mode. Returns (resources, output_lines).
    The OS actually answering `uname -s` wins over the registered os_family (nova-core4 / .250 is a Mac
    registered as linux: it got apt/ufw/systemctl recipes). A node whose registry is wrong is only
    drift-checked, never changed, until cinc_node_configs is fixed (its run_list was written for the
    other OS, e.g. nova_dotfiles would start overwriting a Mac's ~/.zshrc). Per-check [drift]/[changed]/[failed] lines are
    kept under each recipe summary so the drift report says WHAT drifted."""
    name, ip = node["node_name"], str(node["node_ip"])
    user = node.get("ssh_user", "kochj")
    os_family = node["os_family"]
    run_list = node.get("run_list", [])
    run_list = run_list if isinstance(run_list, list) else json.loads(run_list)
    recipes = [r.replace("recipe[", "").replace("]", "") for r in run_list]
    output_lines = []
    _, uname, _ = _run(user, ip, "uname -s", timeout=15)
    detected = detect_os(uname, os_family)
    if detected != os_family:
        log(f"[{name}] registered os_family={os_family} but host reports {detected}; using {detected}", "WARN")
        output_lines.append(f"os_family: [warn] registered {os_family}, host is {detected} "
                            "(fix cinc_node_configs; drift-check only until then)")
        os_family = detected
        dry_run = True
    resources = 0
    for recipe_name in recipes:
        rc, out, err = _apply_recipe_shell(user, ip, os_family, recipe_name, dry_run,
                                           node_name=name, run_list=recipes)
        if rc == 0:
            # Count changes (a drift-check reports "[drift]" lines instead of applying them)
            changes = out.count("[changed]") + out.count("[created]") + out.count("[drift]")
            resources += changes
            output_lines.append(f"{recipe_name}: {changes} changes")
            output_lines += [f"    {l.strip()}" for l in out.splitlines()
                             if l.strip().startswith(("[drift]", "[changed]", "[created]", "[failed]"))]
        else:
            output_lines.append(f"{recipe_name}: FAILED ({err[:100]})")
    return resources, output_lines


def _converge_local(node, run_id, start, dry_run):
    """Converge the local machine: the same recipes, run through a local shell instead of SSH."""
    name = node["node_name"]
    resources, output_lines = _run_recipes(node, dry_run)

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


def _apply_recipe_shell(user, ip, os_family, recipe_name, dry_run, node_name=None, run_list=()):
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
        return _recipe_nova_macos(user, ip, os_family, dry_run, run_list)
    elif recipe_name == "nova_dotfiles":
        return _recipe_nova_dotfiles(user, ip, os_family, dry_run)
    elif recipe_name == "nova_wazuh":
        return _recipe_nova_wazuh(user, ip, os_family, dry_run, node_name)
    elif recipe_name == "nova_zsh":
        return _recipe_nova_zsh(user, ip, os_family, dry_run)
    else:
        return 0, f"{recipe_name}: [skipped] no shell handler", ""


def _recipe_nova_base(user, ip, os_family, dry_run):
    """nova_base: SSH hardening (01-nova-nopassword.conf drop-in, verified with sshd -T), fleet
    ssh_known_hosts, syslog forwarding (rsyslog on Linux, the launchd forwarder on macOS).
    2026-11-01: the old check grepped the main sshd_config and sed-edited it; with cloud-init's
    50-cloud-init.conf / macOS 100-macos.conf the FIRST value read wins, so only a drop-in that sorts
    first (01-) is authoritative."""
    checks = []
    checks += _check_sshd_dropin(user, ip, os_family, dry_run)
    checks += _check_known_hosts(user, ip, os_family, dry_run)

    if os_family == "macos":
        checks += _check_mac_syslog_forwarder(user, ip, dry_run)

    if os_family == "linux":
        # Check syslog forwarding
        rc, out, _ = _run(user, ip,
            "test -f /etc/rsyslog.d/60-nova.conf && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] syslog forwarding configured")
        else:
            checks.append("[drift] syslog forwarding missing")
            if not dry_run:
                _run(user, ip,
                    "echo '*.* @192.168.1.2:1514' | sudo tee /etc/rsyslog.d/60-nova.conf > /dev/null && "
                    "sudo systemctl restart rsyslog")
                checks[-1] = "[created] syslog forwarding"

    return 0, "\n".join(checks), ""


def _recipe_nova_monitoring(user, ip, os_family, dry_run):
    """nova_monitoring: Install and configure snmpd."""
    checks = []

    if os_family == "linux":
        # Check snmpd installed
        rc, out, _ = _run(user, ip, "dpkg -l snmpd 2>/dev/null | grep -q '^ii' && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] snmpd installed")
        else:
            checks.append("[drift] snmpd not installed")
            if not dry_run:
                _run(user, ip, "sudo apt-get install -y snmpd snmp > /dev/null 2>&1", timeout=120)
                checks[-1] = "[changed] snmpd installed"

        # systemonly views + exactly one LAN community line (read with sudo: file is 0600 root; the old
        # unprivileged grep appended a duplicate community line every night). One rewrite fixes both.
        checks += _check_linux_snmp(user, ip, dry_run)

        # Check snmpd listening on all interfaces
        rc, out, _ = _run(user, ip,
            "sudo -n grep -qi '^agentaddress *udp:161' /etc/snmp/snmpd.conf && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] snmpd listening on all interfaces")
        else:
            checks.append("[drift] snmpd only on localhost")
            if not dry_run:
                _run(user, ip,
                    "sudo sed -i 's/^agentaddress.*/agentaddress udp:161,udp6:[::1]:161/I' /etc/snmp/snmpd.conf && "
                    "sudo systemctl restart snmpd")
                checks[-1] = "[changed] snmpd listening on all interfaces"

    elif os_family == "macos":
        checks += _check_mac_snmpd(user, ip, dry_run)

    return 0, "\n".join(checks), ""


def _recipe_nova_security(user, ip, os_family, dry_run):
    """nova_security: Install rkhunter, chkrootkit, AIDE, osquery."""
    checks = []

    if os_family == "linux":
        # rkhunter
        rc, out, _ = _run(user, ip, "which rkhunter > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] rkhunter installed")
        else:
            checks.append("[drift] rkhunter not installed")
            if not dry_run:
                _run(user, ip, "sudo apt-get install -y rkhunter > /dev/null 2>&1", timeout=120)
                _run(user, ip, "sudo rkhunter --propupd > /dev/null 2>&1", timeout=60)
                checks[-1] = "[changed] rkhunter installed + baseline set"

        # chkrootkit
        rc, out, _ = _run(user, ip, "which chkrootkit > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] chkrootkit installed")
        else:
            checks.append("[drift] chkrootkit not installed")
            if not dry_run:
                _run(user, ip, "sudo apt-get install -y chkrootkit > /dev/null 2>&1", timeout=120)
                checks[-1] = "[changed] chkrootkit installed"

        # AIDE
        rc, out, _ = _run(user, ip, "which aide > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] aide installed")
        else:
            checks.append("[drift] aide not installed")
            if not dry_run:
                _run(user, ip,
                    "sudo apt-get install -y aide > /dev/null 2>&1 && "
                    "sudo aideinit > /dev/null 2>&1 && "
                    "sudo cp /var/lib/aide/aide.db.new /var/lib/aide/aide.db 2>/dev/null",
                    timeout=180)
                checks[-1] = "[changed] aide installed + database initialized"

        # osquery
        rc, out, _ = _run(user, ip, "which osqueryi > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] osquery installed")
        else:
            checks.append("[drift] osquery not installed")
            if not dry_run:
                _run(user, ip,
                    "curl -fsSL https://pkg.osquery.io/deb/pubkey.gpg | sudo gpg --dearmor -o /usr/share/keyrings/osquery-archive-keyring.gpg 2>/dev/null && "
                    "echo 'deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/osquery-archive-keyring.gpg] https://pkg.osquery.io/deb deb main' | sudo tee /etc/apt/sources.list.d/osquery.list > /dev/null && "
                    "sudo apt-get update -qq > /dev/null 2>&1 && "
                    "sudo apt-get install -y osquery > /dev/null 2>&1",
                    timeout=180)
                checks[-1] = "[changed] osquery installed"

    elif os_family == "macos":
        # rkhunter via brew (remote macOS)
        rc, out, _ = _run(user, ip,
            "/opt/homebrew/bin/brew list rkhunter > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] rkhunter installed")
        else:
            checks.append("[drift] rkhunter not installed")
            if not dry_run:
                _run(user, ip,
                    "/opt/homebrew/bin/brew install rkhunter > /dev/null 2>&1",
                    timeout=180)
                checks[-1] = "[changed] rkhunter installed"

        # osquery via brew
        rc, out, _ = _run(user, ip,
            "/opt/homebrew/bin/brew list osquery > /dev/null 2>&1 && echo ok || echo missing")
        if "ok" in out:
            checks.append("[ok] osquery installed")
        else:
            checks.append("[drift] osquery not installed")
            if not dry_run:
                _run(user, ip,
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
    rc, out, _ = _run(user, ip, "dpkg -l fail2ban 2>/dev/null | grep -q '^ii' && echo ok || echo missing")
    if "ok" in out:
        checks.append("[ok] fail2ban installed")
    else:
        checks.append("[drift] fail2ban not installed")
        if not dry_run:
            _run(user, ip, "sudo apt-get install -y fail2ban > /dev/null 2>&1", timeout=120)
            checks[-1] = "[changed] fail2ban installed"

    # ufw
    rc, out, _ = _run(user, ip, "sudo ufw status | grep -q 'Status: active' && echo ok || echo inactive")
    if "ok" in out:
        checks.append("[ok] UFW active")
    else:
        checks.append("[drift] UFW not active")
        if not dry_run:
            _run(user, ip,
                "sudo apt-get install -y ufw > /dev/null 2>&1 && "
                "sudo ufw allow 22 > /dev/null && "
                "sudo ufw allow 161 > /dev/null && "
                "sudo ufw allow from 192.168.1.6 > /dev/null && "
                "echo y | sudo ufw enable > /dev/null 2>&1")
            checks[-1] = "[changed] UFW enabled with rules"

    # unattended-upgrades
    rc, out, _ = _run(user, ip,
        "dpkg -l unattended-upgrades 2>/dev/null | grep -q '^ii' && echo ok || echo missing")
    if "ok" in out:
        checks.append("[ok] unattended-upgrades installed")
    else:
        checks.append("[drift] unattended-upgrades not installed")
        if not dry_run:
            _run(user, ip, "sudo apt-get install -y unattended-upgrades > /dev/null 2>&1", timeout=120)
            checks[-1] = "[changed] unattended-upgrades installed"

    checks += _check_timezone(user, ip, dry_run)
    checks += _check_dns_search(user, ip, dry_run)
    checks += _check_nas(user, ip, dry_run)

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
    notify(summary, level="info", category="scheduler",
           dedup_key=f"orchestrate-{'drift' if args.dry_run else 'converge'}")
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


# ── Dotfiles Recipe ──────────────────────────────────────────────────────────

DOTFILES_SOURCE = Path.home()
DOTFILES_SYNC = [
    ".zshrc",
    ".p10k.zsh",
    ".nova-term-colors.zsh",
    ".vimrc",
    ".gitconfig",
    ".fzf.zsh",
    ".fzf.bash",
    ".profile",
    ".bash_profile",
]
DOTFILES_DIRS = [
    ".oh-my-zsh",
    ".vim",
    ".git-templates",
]
DOTFILES_EXCLUDE = [
    ".oh-my-zsh/cache/",
    ".oh-my-zsh/log/",
]

# Core Homebrew packages that every macOS node should have
BREW_CORE_PACKAGES = [
    "bash", "bat", "coreutils", "diffutils", "direnv", "findutils",
    "fzf", "gawk", "gh", "git", "gnu-sed", "gnu-tar", "grep", "gzip",
    "htop", "jq", "less", "make", "nano", "net-snmp", "node",
    "openssl@3", "postgresql@17", "python@3.14", "readline", "rkhunter",
    "rsync", "screen", "sqlite", "thefuck", "tmux", "tree", "watch",
    "wget", "xz", "zip", "zstd",
]


def _recipe_nova_dotfiles(user, ip, os_family, dry_run):
    """nova_dotfiles: Sync shell configs from mac-studio (source of truth) to remote macOS nodes."""
    if os_family != "macos":
        return 0, "nova_dotfiles: [skipped] macOS only", ""

    if ip in ("127.0.0.1", "192.168.1.6"):
        return 0, "nova_dotfiles: [ok] this is the source machine", ""

    checks = []
    changes = 0

    # Sync individual dotfiles
    for dotfile in DOTFILES_SYNC:
        src = DOTFILES_SOURCE / dotfile
        if not src.exists():
            continue

        rc, out, _ = _run(user, ip, f"test -f ~/{dotfile} && echo exists || echo missing")
        if "missing" in out:
            if dry_run:
                checks.append(f"[drift] {dotfile} missing on target")
                changes += 1
            else:
                cmd = ["rsync", "-az", "-e", f"ssh {' '.join(SSH_OPTS)}",
                       str(src), f"{user}@{ip}:~/{dotfile}"]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    checks.append(f"[created] {dotfile}")
                    changes += 1
                else:
                    checks.append(f"[failed] {dotfile}: {r.stderr[:80]}")
        else:
            # File exists — check if content matches
            rc, remote_hash, _ = _run(user, ip, f"shasum -a 256 ~/{dotfile} | cut -d' ' -f1")
            local_hash = subprocess.run(
                ["shasum", "-a", "256", str(src)],
                capture_output=True, text=True).stdout.split()[0]

            if remote_hash.strip() == local_hash.strip():
                checks.append(f"[ok] {dotfile}")
            else:
                if dry_run:
                    checks.append(f"[drift] {dotfile} differs")
                    changes += 1
                else:
                    cmd = ["rsync", "-az", "-e", f"ssh {' '.join(SSH_OPTS)}",
                           str(src), f"{user}@{ip}:~/{dotfile}"]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    if r.returncode == 0:
                        checks.append(f"[changed] {dotfile}")
                        changes += 1
                    else:
                        checks.append(f"[failed] {dotfile}: {r.stderr[:80]}")

    # Sync directories (OMZ, vim, git-templates)
    for dirname in DOTFILES_DIRS:
        src_dir = DOTFILES_SOURCE / dirname
        if not src_dir.exists():
            continue

        exclude_args = []
        for excl in DOTFILES_EXCLUDE:
            if excl.startswith(dirname):
                exclude_args.extend(["--exclude", excl.replace(dirname + "/", "")])

        rc, out, _ = _run(user, ip, f"test -d ~/{dirname} && echo exists || echo missing")
        if "missing" in out:
            if dry_run:
                checks.append(f"[drift] {dirname}/ missing on target")
                changes += 1
            else:
                cmd = ["rsync", "-az", "--delete", "-e", f"ssh {' '.join(SSH_OPTS)}"] + \
                      exclude_args + [f"{src_dir}/", f"{user}@{ip}:~/{dirname}/"]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if r.returncode == 0:
                    checks.append(f"[created] {dirname}/")
                    changes += 1
                else:
                    checks.append(f"[failed] {dirname}/: {r.stderr[:80]}")
        else:
            # Directory exists — rsync with --dry-run to check for drift
            cmd = ["rsync", "-az", "--delete", "--dry-run", "--itemize-changes",
                   "-e", f"ssh {' '.join(SSH_OPTS)}"] + exclude_args + \
                  [f"{src_dir}/", f"{user}@{ip}:~/{dirname}/"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            diffs = [l for l in r.stdout.strip().splitlines() if l and not l.startswith(".d")]
            if not diffs:
                checks.append(f"[ok] {dirname}/")
            else:
                if dry_run:
                    checks.append(f"[drift] {dirname}/ ({len(diffs)} files differ)")
                    changes += 1
                else:
                    cmd = ["rsync", "-az", "--delete", "-e", f"ssh {' '.join(SSH_OPTS)}"] + \
                          exclude_args + [f"{src_dir}/", f"{user}@{ip}:~/{dirname}/"]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    if r.returncode == 0:
                        checks.append(f"[changed] {dirname}/ ({len(diffs)} files)")
                        changes += 1
                    else:
                        checks.append(f"[failed] {dirname}/: {r.stderr[:80]}")

    # Ensure ~/.vim/undodir exists
    if not dry_run:
        _run(user, ip, "mkdir -p ~/.vim/undodir")

    # Sync core Homebrew packages
    rc, remote_pkgs, _ = _run(user, ip,
        "/opt/homebrew/bin/brew list --formula 2>/dev/null", timeout=30)
    if rc == 0:
        installed = set(remote_pkgs.strip().splitlines())
        missing = [p for p in BREW_CORE_PACKAGES if p not in installed]
        if missing:
            if dry_run:
                checks.append(f"[drift] {len(missing)} brew packages missing: {', '.join(missing[:10])}")
                changes += 1
            else:
                pkg_str = " ".join(missing)
                rc2, out2, err2 = _run(user, ip,
                    f"/opt/homebrew/bin/brew install {pkg_str} 2>&1 | tail -5",
                    timeout=600)
                if rc2 == 0:
                    checks.append(f"[changed] installed {len(missing)} brew packages")
                    changes += len(missing)
                else:
                    checks.append(f"[partial] brew install: {err2[:100]}")
                    changes += 1
        else:
            checks.append(f"[ok] all {len(BREW_CORE_PACKAGES)} core brew packages present")
    else:
        checks.append("[skipped] brew not available on target")

    return 0, "\n".join(checks), ""


# ── Fleet baseline (hand-fixed 2026-11-01, enforced here so it cannot drift back) ──────────────
# Every check below follows the recipe style: probe first, report [ok]/[drift], fix only when not
# dry_run, never append duplicates, back up a file before rewriting it (<file>.bak-YYYYMMDD).
# The pure helpers (no I/O) are unit-tested in tests/test_nova_cinc_orchestrate_baseline.py.

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"   # /Volumes/nas/nova-fs/config on .6
KNOWN_HOSTS = "/etc/ssh/ssh_known_hosts"
KNOWN_HOSTS_SRC = CONFIG_DIR / "ssh_known_hosts"
MACOS_CFG_DIR = CONFIG_DIR / "macos"
ZSH_COOKBOOK_FILES = COOKBOOKS / "nova_zsh" / "files"
_SEP = "---NOVA-SEP---"

SSHD_DROPIN_NAME = "01-nova-nopassword.conf"
SSHD_DROPIN = f"/etc/ssh/sshd_config.d/{SSHD_DROPIN_NAME}"
SSHD_DIRECTIVES = ["PasswordAuthentication no", "KbdInteractiveAuthentication no", "PermitRootLogin no"]
SSHD_DROPIN_CONTENT = (
    "# Nova fleet SSH baseline (2026-11-01): keys only. Sorts before 50-cloud-init.conf / "
    "100-macos.conf, and sshd keeps the FIRST value.\n" + "\n".join(SSHD_DIRECTIVES) + "\n")

SNMP_VIEW_OIDS = [".1.3.6.1.2.1.1", ".1.3.6.1.2.1.25.1", ".1.3.6.1.2.1.2", ".1.3.6.1.2.1.25", ".1.3.6.1.4.1"]
SNMP_COMMUNITY_LINE = "rocommunity public 192.168.1.0/24"
MAC_SNMPD_CONF = "/etc/snmp/snmpd.conf"
MAC_SNMPD_LABEL = "org.net-snmp.snmpd"
MAC_SNMPD_PLIST = "/System/Library/LaunchDaemons/org.net-snmp.snmpd.plist"
SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"

SYSLOG_FWD_LABEL = "net.digitalnoise.nova-syslog-forwarder"
SYSLOG_FWD_PLIST = f"/Library/LaunchDaemons/{SYSLOG_FWD_LABEL}.plist"
SYSLOG_FWD_SCRIPT = "/usr/local/bin/nova_syslog_forwarder.sh"

WAZUH_MANAGER = "192.168.1.2"
WAZUH_VERSION = "4.9.2-1"
WAZUH_DEB_URL = f"https://packages.wazuh.com/4.x/apt/pool/main/w/wazuh-agent/wazuh-agent_{WAZUH_VERSION}_amd64.deb"
WAZUH_PKG_URL = f"https://packages.wazuh.com/4.x/macos/wazuh-agent-{WAZUH_VERSION}.arm64.pkg"
# macOS agents we may (re)install. .6 / .7 are enrolled as Office-M4-2.local / TV-Movies-3.local:
# check only, never reinstall (a reinstall would re-enroll them under a new name).
WAZUH_MAC_AGENT_NAMES = {"192.168.1.250": "nova-core4", "192.168.1.252": "nova-core6", "192.168.1.77": "nova-core10"}

TIMEZONE = "America/Los_Angeles"
SEARCH_DOMAIN = "digitalnoise.net"
RESOLVED_DROPIN = "/etc/systemd/resolved.conf.d/10-nova-search.conf"
# (mount point, fstype, source prefix) — fstab is CHECK ONLY, never written by the orchestrator
FSTAB_REQUIRED = [
    ("/nova", "cifs", "//192.168.1.19/"),
    ("/mnt/nas", "cifs", "//192.168.1.19/"),
    ("/mnt/nas-external", "cifs", "//192.168.1.19/"),
    ("/mnt/data", "nfs", "192.168.1.19:/var/nfs/shared/nas/Data"),
]

ZSH_COLORS_FILE = ".nova-term-colors.zsh"
ZSH_COLORS_LINE = ("[[ -f ~/.nova-term-colors.zsh ]] && source ~/.nova-term-colors.zsh   "
                   "# shell=white/black, claude=white/green")
OMZ_URL = "https://github.com/ohmyzsh/ohmyzsh.git"
P10K_URL = "https://github.com/romkatv/powerlevel10k.git"


# ---- pure helpers ---------------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def parse_sha256(out: str) -> str:
    """First 64-hex token of `sha256sum`/`shasum -a 256` output, else ''."""
    import re
    m = re.search(r"\b([0-9a-f]{64})\b", out or "")
    return m.group(1) if m else ""


def known_hosts_in_sync(local_hash: str, remote_out: str) -> bool:
    return bool(local_hash) and parse_sha256(remote_out) == local_hash


def split_sections(out: str, n: int) -> list:
    """Split a multi-probe output on _SEP; always returns exactly n (stripped) parts."""
    parts = [p.strip() for p in (out or "").split(_SEP)]
    return (parts + [""] * n)[:n]


def directive_lines(text: str) -> list:
    """Non-blank, non-comment lines, whitespace-normalized."""
    return [" ".join(l.split()) for l in (text or "").splitlines() if l.strip() and not l.strip().startswith("#")]


def sshd_dropin_ok(text: str) -> bool:
    return directive_lines(text) == SSHD_DIRECTIVES


def dropin_sorts_first(names) -> bool:
    """sshd keeps the FIRST value it reads, so ours must sort before every other *.conf."""
    confs = sorted(n.strip() for n in names if n.strip().endswith(".conf"))
    return bool(confs) and confs[0] == SSHD_DROPIN_NAME


def sshd_effective_bad(sshd_t_out: str) -> list:
    """Keys from `sshd -T` that are not 'no' (missing keys count as bad)."""
    want = ("passwordauthentication", "kbdinteractiveauthentication", "permitrootlogin")
    seen = {}
    for l in (sshd_t_out or "").splitlines():
        p = l.split()
        if len(p) >= 2 and p[0].lower() in want:
            seen[p[0].lower()] = p[1].lower()
    return [f"{k}={seen.get(k, '?')}" for k in want if seen.get(k) != "no"]


def _snmp_view_oid(line: str):
    p = line.split()
    if len(p) == 4 and p[0] == "view" and p[1] == "systemonly" and p[2] == "included":
        return p[3]
    return None


def snmp_missing_views(text: str) -> list:
    have = {_snmp_view_oid(l) for l in (text or "").splitlines()}
    return [o for o in SNMP_VIEW_OIDS if o not in have]


def _is_community_line(line: str) -> bool:
    return " ".join(line.split()) == SNMP_COMMUNITY_LINE


def snmp_community_count(text: str) -> int:
    return sum(1 for l in (text or "").splitlines() if _is_community_line(l))


def snmp_fix_linux(text: str) -> str:
    """Return snmpd.conf with the missing systemonly views inserted (after the .25.1 view, else after
    the last systemonly view, else at the end) and exactly one LAN rocommunity line. Idempotent."""
    lines = (text or "").split("\n")
    trailing_nl = text.endswith("\n") if text else True
    if trailing_nl and lines and lines[-1] == "":
        lines = lines[:-1]
    out, seen_comm = [], False
    for l in lines:                                 # dedupe the community line (keep the first)
        if _is_community_line(l):
            if seen_comm:
                continue
            seen_comm = True
        out.append(l)
    missing = snmp_missing_views("\n".join(out))
    if missing:
        new = [f"view   systemonly  included   {o}" for o in missing]
        oids = [_snmp_view_oid(l) for l in out]
        if ".1.3.6.1.2.1.25.1" in oids:
            at = oids.index(".1.3.6.1.2.1.25.1") + 1
        else:
            idx = [i for i, o in enumerate(oids) if o]
            at = idx[-1] + 1 if idx else len(out)
        out[at:at] = new
    if not seen_comm:
        out.append(SNMP_COMMUNITY_LINE)
    return "\n".join(out) + "\n"


def zshrc_has_colors_line(text: str) -> bool:
    """True if a non-comment line sources ~/.nova-term-colors.zsh."""
    import re
    pat = re.compile(r"(^|[;&|\s])(source|\.)\s+(~|\$HOME|\$\{HOME\})/\.nova-term-colors\.zsh\b")
    return any(pat.search(l) for l in (text or "").splitlines() if not l.strip().startswith("#"))


def fstab_missing_mounts(text: str) -> list:
    """Required NAS mount points that have no active fstab line with the right type + source."""
    active = []
    for l in (text or "").splitlines():
        p = l.split()
        if len(p) >= 3 and not p[0].startswith("#"):
            active.append((p[0], p[1], p[2]))
    return [mp for mp, fs, src in FSTAB_REQUIRED
            if not any(a_mp == mp and a_fs == fs and a_src.startswith(src) for a_src, a_mp, a_fs in active)]


def symlink_state(probe: str, target: str) -> str:
    """probe = `readlink P || (test -e P && echo REALPATH || echo ABSENT)` output.
    -> 'ok' | 'fix' (absent, or a symlink pointing elsewhere) | 'blocked' (a real file/dir: never replaced)."""
    v = (probe or "").strip().splitlines()[-1:] or [""]
    v = v[0].strip()
    if v == target or v == target + "/":
        return "ok"
    if v == "REALPATH":
        return "blocked"
    return "fix"


def wazuh_address_ok(out: str) -> bool:
    import re
    addrs = re.findall(r"<address>\s*([^<\s]+)\s*</address>", out or "")
    return bool(addrs) and all(a == WAZUH_MANAGER for a in addrs)


def mac_snmpd_conf(local_host_name: str) -> str:
    return ("# Nova fleet SNMP baseline (2026-11-01) - read-only, LAN only, same as the Linux nodes\n"
            "agentaddress udp:161\n"
            "rocommunity public 192.168.1.0/24\n"
            "rocommunity public 127.0.0.1\n"
            f"syslocation Nova cluster - {local_host_name}\n"
            "syscontact Jordan Koch\n"
            "sysservices 72\n"
            "disk / 10000\n")


def missing_lines(text: str, required_text: str) -> list:
    have = set(directive_lines(text))
    return [l for l in directive_lines(required_text) if l not in have]


def firewall_blocks_app(globalstate: str, appblocked: str) -> bool:
    """True when the macOS app firewall is on and snmpd is not explicitly permitted."""
    import re
    on = bool(re.search(r"is enabled|State = [12]", globalstate or ""))
    return on and "permitted" not in (appblocked or "")


def detect_os(uname_out: str, default: str) -> str:
    v = (uname_out or "").strip().splitlines()[-1:] or [""]
    return {"Darwin": "macos", "Linux": "linux"}.get(v[0].strip(), default)


def backup_snippet(path: str, sudo: bool = True) -> str:
    """Shell: copy `path` to path.bak-YYYYMMDD once per day (never clobbers an earlier backup)."""
    s = "sudo -n " if sudo else ""
    b = f"{path}.bak-{datetime.now().strftime('%Y%m%d')}"
    return f"if [ -e {path} ] && [ ! -e {b} ]; then {s}cp -p {path} {b}; fi"


# ---- exec plumbing (local node runs the same recipes without SSHing into itself) -------------

_LOCAL_CACHE = {}


def _local(ip) -> bool:
    ip = str(ip)
    if ip not in _LOCAL_CACHE:
        _LOCAL_CACHE[ip] = is_local(ip)
    return _LOCAL_CACHE[ip]


def _run(user, ip, command, timeout=60):
    """ssh_exec, or a local bash when `ip` is this machine. Returns (rc, stdout, stderr)."""
    if not _local(ip):
        return ssh_exec(user, ip, command, timeout)
    try:
        r = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "local command timed out"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def _push(user, ip, data, command, timeout=60):
    """Run `command` on the node with `data` (bytes/str) on stdin — used with `sudo -n tee`."""
    if isinstance(data, str):
        data = data.encode()
    argv = ["/bin/bash", "-c", command] if _local(ip) else ["ssh"] + SSH_OPTS + [f"{user}@{ip}", command]
    try:
        r = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return -1, "", "push timed out"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def _read_source(path):
    try:
        return Path(path).read_bytes()
    except Exception:  # noqa: BLE001
        return None


# ---- checks (each returns a list of "[ok]/[drift]/[changed]/[failed] ..." lines) -------------

def _check_sshd_dropin(user, ip, os_family, dry_run):
    checks = []
    probe = (f"sudo -n cat {SSHD_DROPIN} 2>/dev/null; echo {_SEP}; ls -1 /etc/ssh/sshd_config.d/ 2>/dev/null; "
             f"echo {_SEP}; sudo -n /usr/sbin/sshd -T 2>/dev/null | "
             "grep -iE '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '")
    _, out, _ = _run(user, ip, probe)
    content, names, eff = split_sections(out, 3)
    if sshd_dropin_ok(content) and dropin_sorts_first(names.splitlines()):
        checks.append(f"[ok] sshd drop-in {SSHD_DROPIN_NAME}")
    elif not sshd_dropin_ok(content):
        checks.append(f"[drift] sshd drop-in {SSHD_DROPIN_NAME} missing or wrong")
        if not dry_run:
            grp = "wheel" if os_family == "macos" else "root"
            reload = ("true" if os_family == "macos"      # launchd starts sshd per connection
                      else "sudo -n systemctl reload ssh 2>/dev/null || sudo -n systemctl reload sshd")
            p = SSHD_DROPIN
            fix = (f"{backup_snippet(p)}; had=0; if [ -e {p} ]; then had=1; sudo -n cp -p {p} {p}.nova-prev; fi; "
                   f"sudo -n mkdir -p /etc/ssh/sshd_config.d && sudo -n tee {p} >/dev/null && "
                   f"sudo -n chown root:{grp} {p} && sudo -n chmod 644 {p} || {{ echo NOVA_WRITE_FAILED; exit 1; }}; "
                   f"if sudo -n /usr/sbin/sshd -t; then sudo -n rm -f {p}.nova-prev; {reload} && echo NOVA_OK; "
                   f"else if [ $had = 1 ]; then sudo -n mv {p}.nova-prev {p}; else sudo -n rm -f {p}; fi; "
                   "echo NOVA_SSHD_T_FAILED; fi")
            rc, o, e = _push(user, ip, SSHD_DROPIN_CONTENT, fix)
            if "NOVA_OK" in o:
                checks[-1] = f"[changed] sshd drop-in {SSHD_DROPIN_NAME} written, sshd -t ok, reloaded"
                _, out2, _ = _run(user, ip, probe)
                eff = split_sections(out2, 3)[2]
            else:
                checks[-1] = (f"[failed] sshd drop-in not applied (rolled back if sshd -t failed): "
                              f"{(o + e).strip()[-120:]}")
    else:
        checks.append(f"[drift] sshd drop-in present but another *.conf sorts before {SSHD_DROPIN_NAME} "
                      "(not renaming other files)")
    bad = sshd_effective_bad(eff)
    checks.append("[ok] sshd effective: password/kbdinteractive/root login all off" if not bad
                  else f"[drift] sshd effective config: {', '.join(bad)}")
    return checks


def _check_known_hosts(user, ip, os_family, dry_run):
    src = _read_source(KNOWN_HOSTS_SRC)
    if src is None:
        return [f"[skipped] ssh_known_hosts source {KNOWN_HOSTS_SRC} unreadable"]
    want = sha256_hex(src)
    _, out, _ = _run(user, ip, f"(sha256sum {KNOWN_HOSTS} 2>/dev/null || shasum -a 256 {KNOWN_HOSTS} 2>/dev/null)")
    if known_hosts_in_sync(want, out):
        return [f"[ok] {KNOWN_HOSTS} matches config/ssh_known_hosts"]
    if dry_run:
        return [f"[drift] {KNOWN_HOSTS} differs from config/ssh_known_hosts"]
    rc, o, e = _push(user, ip, src, f"{backup_snippet(KNOWN_HOSTS)}; sudo -n tee {KNOWN_HOSTS} >/dev/null && "
                                     f"sudo -n chmod 644 {KNOWN_HOSTS}")
    return [f"[changed] {KNOWN_HOSTS} pushed" if rc == 0 else f"[failed] {KNOWN_HOSTS} push: {e.strip()[:100]}"]


def _check_mac_syslog_forwarder(user, ip, dry_run):
    probe = (f"test -f {SYSLOG_FWD_PLIST} && echo HAVE_PLIST; test -f {SYSLOG_FWD_SCRIPT} && echo HAVE_SCRIPT; "
             f"sudo -n launchctl print system/{SYSLOG_FWD_LABEL} 2>/dev/null | grep -q 'state = running' && echo RUNNING")
    _, out, _ = _run(user, ip, probe)
    toks = set(out.split())
    if {"HAVE_PLIST", "HAVE_SCRIPT", "RUNNING"} <= toks:
        return ["[ok] syslog forwarder running"]
    what = [n for n, t in (("plist missing", "HAVE_PLIST"), ("script missing", "HAVE_SCRIPT"),
                           ("not running", "RUNNING")) if t not in toks]
    if dry_run:
        return [f"[drift] syslog forwarder: {', '.join(what)}"]
    for tok, name, dest, mode in (("HAVE_PLIST", f"{SYSLOG_FWD_LABEL}.plist", SYSLOG_FWD_PLIST, "644"),
                                  ("HAVE_SCRIPT", "nova_syslog_forwarder.sh", SYSLOG_FWD_SCRIPT, "755")):
        if tok in toks:
            continue
        data = _read_source(MACOS_CFG_DIR / name)
        if data is None:
            return [f"[failed] syslog forwarder: source config/macos/{name} unreadable"]
        rc, _, e = _push(user, ip, data, f"sudo -n mkdir -p {Path(dest).parent} && sudo -n tee {dest} >/dev/null && "
                                         f"sudo -n chown root:wheel {dest} && sudo -n chmod {mode} {dest}")
        if rc != 0:
            return [f"[failed] syslog forwarder push {name}: {e.strip()[:100]}"]
    rc, _, e = _run(user, ip, f"sudo -n launchctl bootstrap system {SYSLOG_FWD_PLIST} 2>/dev/null; "
                              f"sudo -n launchctl kickstart -k system/{SYSLOG_FWD_LABEL}")
    return [f"[changed] syslog forwarder ({', '.join(what)}) fixed" if rc == 0
            else f"[failed] syslog forwarder start: {e.strip()[:100]}"]


def _check_mac_snmpd(user, ip, dry_run):
    checks = []
    probe = (f"scutil --get LocalHostName 2>/dev/null; echo {_SEP}; sudo -n cat {MAC_SNMPD_CONF} 2>/dev/null; "
             f"echo {_SEP}; sudo -n launchctl print system/{MAC_SNMPD_LABEL} 2>/dev/null | grep -c 'state = running'; "
             f"echo {_SEP}; {SOCKETFILTERFW} --getglobalstate 2>/dev/null; echo {_SEP}; "
             f"{SOCKETFILTERFW} --getappblocked /usr/sbin/snmpd 2>/dev/null")
    _, out, _ = _run(user, ip, probe)
    lhn, conf, running, fw_state, fw_app = split_sections(out, 5)
    lhn = lhn.splitlines()[0].strip() if lhn else ""
    restart = False
    if not lhn:
        checks.append("[drift] snmpd.conf: LocalHostName unreadable, not checking")
    else:
        miss = missing_lines(conf, mac_snmpd_conf(lhn))
        if not miss:
            checks.append("[ok] snmpd.conf baseline")
        else:
            checks.append(f"[drift] snmpd.conf missing {len(miss)} baseline line(s): {'; '.join(miss)[:120]}")
            if not dry_run:
                rc, _, e = _push(user, ip, mac_snmpd_conf(lhn),
                                 f"{backup_snippet(MAC_SNMPD_CONF)}; sudo -n mkdir -p /etc/snmp && "
                                 f"sudo -n tee {MAC_SNMPD_CONF} >/dev/null && sudo -n chmod 600 {MAC_SNMPD_CONF}")
                checks[-1] = "[changed] snmpd.conf baseline written" if rc == 0 else f"[failed] snmpd.conf: {e[:100]}"
                restart = rc == 0
    if firewall_blocks_app(fw_state, fw_app):
        checks.append("[drift] app firewall on and snmpd not permitted")
        if not dry_run:
            rc, _, e = _run(user, ip, f"sudo -n {SOCKETFILTERFW} --add /usr/sbin/snmpd >/dev/null && "
                                      f"sudo -n {SOCKETFILTERFW} --unblockapp /usr/sbin/snmpd >/dev/null")
            checks[-1] = "[changed] snmpd permitted in app firewall" if rc == 0 else f"[failed] firewall: {e[:100]}"
    else:
        checks.append("[ok] app firewall not blocking snmpd")
    is_running = running.isdigit() and int(running) > 0
    if is_running and not restart:
        checks.append("[ok] snmpd (launchd) running")
    else:
        if not is_running:
            checks.append("[drift] snmpd (launchd) not running")
        if not dry_run:
            rc, _, e = _run(user, ip, f"sudo -n launchctl enable system/{MAC_SNMPD_LABEL}; "
                                      f"sudo -n launchctl bootstrap system {MAC_SNMPD_PLIST} 2>/dev/null; "
                                      f"sudo -n launchctl kickstart -k system/{MAC_SNMPD_LABEL}")
            line = "[changed] snmpd (launchd) (re)started" if rc == 0 else f"[failed] snmpd start: {e[:100]}"
            if is_running:
                checks.append(line)            # restart after a conf change
            else:
                checks[-1] = line
    return checks


def _check_linux_snmp(user, ip, dry_run):
    """views + exactly one LAN community line; one rewrite (with backup) fixes both."""
    rc, conf, _ = _run(user, ip, "sudo -n cat /etc/snmp/snmpd.conf 2>/dev/null")
    if rc != 0 or not conf.strip() or "agentaddress" not in conf.lower():
        return ["[drift] /etc/snmp/snmpd.conf unreadable or not a snmpd config (not rewriting)"]
    checks = []
    miss, n = snmp_missing_views(conf), snmp_community_count(conf)
    checks.append("[ok] snmpd systemonly views" if not miss else f"[drift] snmpd views missing: {', '.join(miss)}")
    checks.append("[ok] snmpd community configured (exactly once)" if n == 1
                  else f"[drift] snmpd community line present {n}x (want exactly 1)")
    if (miss or n != 1) and not dry_run:
        f = "/etc/snmp/snmpd.conf"
        rc, _, e = _push(user, ip, snmp_fix_linux(conf),
                         f"{backup_snippet(f)}; sudo -n tee {f} >/dev/null && sudo -n systemctl restart snmpd")
        for i, l in enumerate(checks):
            if l.startswith("[drift]"):
                checks[i] = l.replace("[drift]", "[changed]" if rc == 0 else "[failed]", 1) + \
                            (" -> fixed" if rc == 0 else f" ({e.strip()[:80]})")
    return checks


def _check_timezone(user, ip, dry_run):
    _, out, _ = _run(user, ip, "timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone")
    tz = out.strip().splitlines()[0].strip() if out.strip() else ""
    if tz == TIMEZONE:
        return [f"[ok] timezone {TIMEZONE}"]
    if dry_run:
        return [f"[drift] timezone is {tz or '?'} (want {TIMEZONE})"]
    rc, _, e = _run(user, ip, f"sudo -n timedatectl set-timezone {TIMEZONE}")
    return [f"[changed] timezone -> {TIMEZONE}" if rc == 0 else f"[failed] timezone: {e.strip()[:100]}"]


def _check_dns_search(user, ip, dry_run):
    probe = "getent hosts nova-core1 >/dev/null && echo ok || echo missing"
    _, out, _ = _run(user, ip, probe)
    if out.strip() == "ok":
        return ["[ok] DNS search domain (bare nova-core1 resolves)"]
    if dry_run:
        return ["[drift] bare 'nova-core1' does not resolve (search domain missing)"]
    r = "/etc/resolv.conf"
    fix = (f"sudo -n mkdir -p /etc/systemd/resolved.conf.d && {backup_snippet(RESOLVED_DROPIN)}; "
           f"printf '[Resolve]\\nDomains={SEARCH_DOMAIN}\\n' | sudo -n tee {RESOLVED_DROPIN} >/dev/null && "
           "sudo -n systemctl restart systemd-resolved; "
           f"if [ -f {r} ] && [ ! -L {r} ] && ! grep -qE '^search.*[[:space:]]{SEARCH_DOMAIN}([[:space:]]|$)' {r}; then "
           f"{backup_snippet(r)}; if grep -q '^search' {r}; then "
           f"sudo -n sed -i -E 's/^(search.*)$/\\1 {SEARCH_DOMAIN}/' {r}; "
           f"else echo 'search {SEARCH_DOMAIN}' | sudo -n tee -a {r} >/dev/null; fi; fi")
    _run(user, ip, fix)
    _, out, _ = _run(user, ip, probe)
    return ["[changed] DNS search domain set" if out.strip() == "ok"
            else "[failed] DNS search domain set but nova-core1 still does not resolve"]


def _check_nas(user, ip, dry_run):
    """CHECK ONLY for creds + fstab (never written). Symlinks fixed only when absent or already a symlink."""
    probe = (f"sudo -n stat -c '%a' /etc/cifs-unas.creds 2>/dev/null || echo absent; echo {_SEP}; cat /etc/fstab; "
             f"echo {_SEP}; readlink /opt/nova-shared || (test -e /opt/nova-shared && echo REALPATH || echo ABSENT); "
             f"echo {_SEP}; readlink ~/.openclaw/scripts || (test -e ~/.openclaw/scripts && echo REALPATH || echo ABSENT)")
    _, out, _ = _run(user, ip, probe)
    creds, fstab, shared, scripts = split_sections(out, 4)
    checks = ["[ok] /etc/cifs-unas.creds 0600" if creds == "600"
              else f"[drift] /etc/cifs-unas.creds {creds or '?'} (want present, 0600; check only — creds never written)"]
    miss = fstab_missing_mounts(fstab)
    checks.append("[ok] fstab NAS mounts" if not miss
                  else f"[drift] fstab missing active NAS line(s) for {', '.join(miss)} (check only)")
    for path, target, probe_out, cmd in (
            ("/opt/nova-shared", "/mnt/data", shared, "sudo -n ln -sfn /mnt/data /opt/nova-shared"),
            ("~/.openclaw/scripts", "/nova/scripts", scripts,
             "mkdir -p ~/.openclaw && ln -sfn /nova/scripts ~/.openclaw/scripts")):
        st = symlink_state(probe_out, target)
        if st == "ok":
            checks.append(f"[ok] {path} -> {target}")
        elif st == "blocked":
            checks.append(f"[drift] {path} is a real file/dir, not a symlink to {target} (not replacing)")
        else:
            checks.append(f"[drift] {path} -> {target} missing")
            if not dry_run:
                rc, _, e = _run(user, ip, cmd)
                checks[-1] = f"[changed] {path} -> {target}" if rc == 0 else f"[failed] {path}: {e.strip()[:80]}"
    return checks


def _check_wazuh(user, ip, os_family, dry_run, node_name=None):
    if os_family == "macos":
        return _check_wazuh_macos(user, ip, dry_run)
    checks = []
    probe = (f"systemctl is-active wazuh-agent 2>/dev/null; echo {_SEP}; "
             f"sudo -n grep -o '<address>[^<]*</address>' /var/ossec/etc/ossec.conf 2>/dev/null; echo {_SEP}; "
             f"sudo -n test -f /var/ossec/etc/ossec.conf && echo INSTALLED; echo {_SEP}; "   # /var/ossec is 0750
             "apt-mark showhold 2>/dev/null | grep -x wazuh-agent")
    _, out, _ = _run(user, ip, probe)
    active, addr, inst, hold = split_sections(out, 4)
    installed, addr_ok, is_active = inst == "INSTALLED", wazuh_address_ok(addr), active == "active"
    if installed and addr_ok and is_active:
        checks.append(f"[ok] wazuh-agent active, manager {WAZUH_MANAGER}")
    else:
        why = "not installed" if not installed else ", ".join(
            w for w, bad in (("manager address wrong", not addr_ok), ("not active", not is_active)) if bad)
        checks.append(f"[drift] wazuh-agent {why}")
        if not dry_run:
            name = node_name or "$(hostname -s)"
            if not installed:
                deb = f"/tmp/wazuh-agent_{WAZUH_VERSION}_amd64.deb"
                cmd = (f"[ \"$(dpkg --print-architecture)\" = amd64 ] || {{ echo 'not amd64'; exit 1; }}; "
                       f"curl -fsSL -o {deb} {WAZUH_DEB_URL} && "
                       f"sudo -n env WAZUH_MANAGER={WAZUH_MANAGER} WAZUH_AGENT_NAME={name} "
                       f"DEBIAN_FRONTEND=noninteractive apt-get install -y {deb} >/dev/null && "
                       "sudo -n systemctl daemon-reload && sudo -n systemctl enable --now wazuh-agent")
                timeout = 300
            else:
                f = "/var/ossec/etc/ossec.conf"
                cmd = ((f"{backup_snippet(f)}; sudo -n sed -i 's|<address>[^<]*</address>|"
                        f"<address>{WAZUH_MANAGER}</address>|' {f} && " if not addr_ok else "") +
                       "sudo -n systemctl enable wazuh-agent && sudo -n systemctl restart wazuh-agent")
                timeout = 120
            rc, o, e = _run(user, ip, cmd, timeout=timeout)
            checks[-1] = (f"[changed] wazuh-agent ({why}) fixed" if rc == 0
                          else f"[failed] wazuh-agent ({why}): {(o + e).strip()[-100:]}")
            if rc == 0:
                installed = True
    if hold == "wazuh-agent":
        checks.append("[ok] wazuh-agent apt-mark hold")
    elif installed:
        checks.append("[drift] wazuh-agent not held (apt-mark hold)")
        if not dry_run:
            rc, _, e = _run(user, ip, "sudo -n apt-mark hold wazuh-agent >/dev/null")
            checks[-1] = "[changed] wazuh-agent held" if rc == 0 else f"[failed] apt-mark hold: {e.strip()[:80]}"
    return checks


def _check_wazuh_macos(user, ip, dry_run):
    probe = (f"test -d /Library/Ossec && echo INSTALLED; echo {_SEP}; "
             f"sudo -n /Library/Ossec/bin/wazuh-control status 2>/dev/null | grep -c 'wazuh-agentd is running'; "
             f"echo {_SEP}; sudo -n grep -o '<address>[^<]*</address>' /Library/Ossec/etc/ossec.conf 2>/dev/null")
    _, out, _ = _run(user, ip, probe)
    inst, running, addr = split_sections(out, 3)
    installed, addr_ok = inst == "INSTALLED", wazuh_address_ok(addr)
    is_running = running.isdigit() and int(running) > 0
    if installed and addr_ok and is_running:
        return [f"[ok] wazuh-agentd running, manager {WAZUH_MANAGER}"]
    why = "not installed" if not installed else ", ".join(
        w for w, bad in (("manager address wrong", not addr_ok), ("agentd not running", not is_running)) if bad)
    name = WAZUH_MAC_AGENT_NAMES.get(str(ip))
    if dry_run:
        return [f"[drift] wazuh-agent {why}" + ("" if name else " (check-only node)")]
    if not name:
        return [f"[failed] wazuh-agent {why} — check-only node (enrolled by hand), not touching"]
    if not installed:
        pkg = f"/tmp/wazuh-agent-{WAZUH_VERSION}.arm64.pkg"
        cmd = (f"[ \"$(uname -m)\" = arm64 ] || {{ echo 'not arm64'; exit 1; }}; "
               f"curl -fsSL -o {pkg} {WAZUH_PKG_URL} && pkgutil --check-signature {pkg} >/dev/null && "
               f"echo \"WAZUH_MANAGER='{WAZUH_MANAGER}' && WAZUH_AGENT_NAME='{name}'\" > /tmp/wazuh_envs && "
               f"sudo -n installer -pkg {pkg} -target / >/dev/null && sudo -n /Library/Ossec/bin/wazuh-control start")
        timeout = 300
    else:
        f = "/Library/Ossec/etc/ossec.conf"
        cmd = ((f"{backup_snippet(f)}; sudo -n sed -i '' 's|<address>[^<]*</address>|"
                f"<address>{WAZUH_MANAGER}</address>|' {f} && " if not addr_ok else "") +
               "sudo -n /Library/Ossec/bin/wazuh-control restart")
        timeout = 120
    rc, o, e = _run(user, ip, cmd, timeout=timeout)
    return [f"[changed] wazuh-agent ({why}) fixed as {name}" if rc == 0
            else f"[failed] wazuh-agent ({why}): {(o + e).strip()[-100:]}"]


def _check_zsh(user, ip, os_family, dry_run):
    checks = []
    shell_q = ("dscl . -read /Users/$(id -un) UserShell 2>/dev/null | awk '{print $2}'" if os_family == "macos"
               else "getent passwd $(id -un) | cut -d: -f7")
    probe = (f"{shell_q}; echo {_SEP}; test -d ~/.oh-my-zsh && echo OMZ; "
             "test -d ~/.oh-my-zsh/custom/themes/powerlevel10k && echo P10K_THEME; test -f ~/.p10k.zsh && echo P10K; "
             f"test -f ~/.zshrc && echo ZSHRC; command -v zsh >/dev/null && echo ZSH_BIN; echo {_SEP}; "
             f"(sha256sum ~/{ZSH_COLORS_FILE} 2>/dev/null || shasum -a 256 ~/{ZSH_COLORS_FILE} 2>/dev/null); "
             f"echo {_SEP}; grep -F nova-term-colors ~/.zshrc 2>/dev/null")
    _, out, _ = _run(user, ip, probe)
    login, toks, colors_hash, rc_lines = split_sections(out, 4)
    toks = set(toks.split())
    linux = os_family == "linux"
    colors_src = _read_source(DOTFILES_SOURCE / ZSH_COLORS_FILE)

    def fix(desc, cmd, data=None, timeout=60):
        if dry_run:
            return
        rc, o, e = (_push(user, ip, data, cmd, timeout) if data is not None else _run(user, ip, cmd, timeout))
        checks[-1] = f"[changed] {desc}" if rc == 0 else f"[failed] {desc}: {(o + e).strip()[-100:]}"

    # zsh itself (Linux installs it; macOS ships it)
    if linux and "ZSH_BIN" not in toks:
        checks.append("[drift] zsh not installed")
        fix("zsh installed", "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y zsh git >/dev/null",
            timeout=180)
    # oh-my-zsh (+ powerlevel10k theme on Linux, where we install it ourselves)
    omz_ok = "OMZ" in toks and (not linux or "P10K_THEME" in toks)
    if omz_ok:
        checks.append("[ok] oh-my-zsh present")
    elif not linux:
        checks.append("[drift] ~/.oh-my-zsh missing (nova_dotfiles syncs it from mac-studio)")
    else:
        checks.append("[drift] oh-my-zsh / powerlevel10k missing")
        fix("oh-my-zsh + powerlevel10k cloned",
            f"(test -d ~/.oh-my-zsh || git clone -q --depth 1 {OMZ_URL} ~/.oh-my-zsh) && "
            f"(test -d ~/.oh-my-zsh/custom/themes/powerlevel10k || "
            f"git clone -q --depth 1 {P10K_URL} ~/.oh-my-zsh/custom/themes/powerlevel10k)", timeout=180)
    # ~/.p10k.zsh
    if "P10K" in toks:
        checks.append("[ok] ~/.p10k.zsh present")
    elif not linux:
        checks.append("[drift] ~/.p10k.zsh missing (nova_dotfiles syncs it)")
    else:
        checks.append("[drift] ~/.p10k.zsh missing")
        src = _read_source(DOTFILES_SOURCE / ".p10k.zsh") or _read_source(ZSH_COOKBOOK_FILES / "p10k.zsh")
        if src is None:
            checks[-1] += " (no source to push)"
        else:
            fix("~/.p10k.zsh pushed", "cat > ~/.p10k.zsh", data=src)
    # ~/.nova-term-colors.zsh
    want = sha256_hex(colors_src) if colors_src is not None else ""
    have = parse_sha256(colors_hash)
    if have and (not linux or not want or have == want):
        checks.append(f"[ok] ~/{ZSH_COLORS_FILE} present")
    elif not linux:
        checks.append(f"[drift] ~/{ZSH_COLORS_FILE} missing (nova_dotfiles syncs it)")
    else:
        checks.append(f"[drift] ~/{ZSH_COLORS_FILE} {'differs' if have else 'missing'}")
        if colors_src is None:
            checks[-1] += " (no source to push)"
        else:
            fix(f"~/{ZSH_COLORS_FILE} pushed",
                f"{backup_snippet('~/' + ZSH_COLORS_FILE, sudo=False)}; cat > ~/{ZSH_COLORS_FILE}", data=colors_src)
    # ~/.zshrc sources it (Linux: never overwrite an existing .zshrc, only append the one line)
    if zshrc_has_colors_line(rc_lines):
        checks.append("[ok] ~/.zshrc sources nova-term-colors")
    elif not linux:
        checks.append("[drift] ~/.zshrc does not source nova-term-colors (nova_dotfiles syncs .zshrc)")
    elif "ZSHRC" not in toks:
        checks.append("[drift] ~/.zshrc missing")
        src = _read_source(ZSH_COOKBOOK_FILES / "zshrc")
        if src is None:
            checks[-1] += " (no cookbook zshrc to push)"
        else:
            fix("~/.zshrc pushed from nova_zsh cookbook (+ colors line)", "cat > ~/.zshrc",
                data=src + (b"" if src.endswith(b"\n") else b"\n") +
                ((ZSH_COLORS_LINE + "\n").encode() if not zshrc_has_colors_line(src.decode(errors="replace")) else b""))
    else:
        checks.append("[drift] ~/.zshrc does not source nova-term-colors")
        fix("~/.zshrc: colors source line appended",
            f"{backup_snippet('~/.zshrc', sudo=False)}; grep -qF '.nova-term-colors.zsh ]] && source' ~/.zshrc "
            "|| cat >> ~/.zshrc", data="\n" + ZSH_COLORS_LINE + "\n")
    # login shell
    if login.strip().endswith("/zsh"):
        checks.append("[ok] login shell zsh")
    else:
        checks.append(f"[drift] login shell is {login.strip() or '?'} (want zsh)")
        fix("login shell -> zsh", 'sudo -n chsh -s "$(command -v zsh)" "$(id -un)"' if linux
            else 'sudo -n chsh -s /bin/zsh "$(id -un)" 2>/dev/null')
    return checks


def _recipe_nova_wazuh(user, ip, os_family, dry_run, node_name=None):
    """nova_wazuh: Wazuh agent installed, running, enrolled to the manager on nova-core (.2)."""
    return 0, "\n".join(_check_wazuh(user, ip, os_family, dry_run, node_name)), ""


def _recipe_nova_zsh(user, ip, os_family, dry_run):
    """nova_zsh: zsh login shell + oh-my-zsh + p10k + nova-term-colors sourced from ~/.zshrc."""
    return 0, "\n".join(_check_zsh(user, ip, os_family, dry_run)), ""


def _recipe_nova_macos(user, ip, os_family, dry_run, run_list=()):
    """nova_macos: macOS-only baseline items whose home recipe is not in this node's run_list
    (itunes has no nova_monitoring; no macOS run_list has nova_wazuh)."""
    if os_family != "macos":
        return 0, "nova_macos: [skipped] not macOS", ""
    checks = []
    if "nova_monitoring" not in run_list:
        checks += _check_mac_snmpd(user, ip, dry_run)
    if "nova_wazuh" not in run_list:
        checks += _check_wazuh_macos(user, ip, dry_run)
    return 0, "\n".join(checks) or "[ok] nothing extra (covered by nova_monitoring / nova_wazuh)", ""


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
