#!/usr/bin/env python3
"""
nova_cinc_bootstrap.py — Bootstrap CINC Solo on local or remote nodes.

Installs cinc-client, pushes cookbooks via rsync, and runs the first converge.
All runs are recorded in nova_ops.deployment_runs for auditability.

Usage:
  nova_cinc_bootstrap.py localhost                   # bootstrap this machine
  nova_cinc_bootstrap.py 192.168.1.2                 # bootstrap remote Pi
  nova_cinc_bootstrap.py --converge localhost        # just re-converge (no install)
  nova_cinc_bootstrap.py --why-run localhost         # dry-run / drift check
  nova_cinc_bootstrap.py --list                      # list managed nodes from DB

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
SOLO_RB = CINC_BASE / "solo.rb"
NODES_DIR = CINC_BASE / "nodes"
DB_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
SSH_USER = "kochj"
CINC_INSTALL_URL = "https://omnitruck.cinc.sh/install.sh"

# Remote paths (where cookbooks/configs land on target)
REMOTE_CINC_DIR = "/opt/cinc-solo"
REMOTE_COOKBOOKS = f"{REMOTE_CINC_DIR}/cookbooks"
REMOTE_SOLO_RB = f"{REMOTE_CINC_DIR}/solo.rb"
REMOTE_NODE_JSON = f"{REMOTE_CINC_DIR}/node.json"


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[cinc-bootstrap {ts}] [{level}] {msg}", flush=True)


def notify(text):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception:
        pass


# ── Database ──────────────────────────────────────────────────────────────────

def db_record_run(run_id, node_name, node_ip, run_type, triggered_by="manual"):
    """Insert a deployment_runs record (start of run)."""
    try:
        subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_ops", "-c",
             f"INSERT INTO deployment_runs (run_id, node_name, node_ip, run_type, triggered_by) "
             f"VALUES ('{run_id}', '{node_name}', '{node_ip}', '{run_type}', '{triggered_by}')"],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        log(f"DB record failed: {e}", "WARN")


def db_complete_run(run_id, status, exit_code, resources_updated=0, error_output="", duration_s=0):
    """Update deployment_runs record (end of run)."""
    error_escaped = error_output.replace("'", "''")[:2000]
    try:
        subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_ops", "-c",
             f"UPDATE deployment_runs SET "
             f"ended_at = now(), status = '{status}', exit_code = {exit_code}, "
             f"resources_updated = {resources_updated}, "
             f"duration_s = {duration_s:.1f}, "
             f"error_output = '{error_escaped}' "
             f"WHERE run_id = '{run_id}'"],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        log(f"DB update failed: {e}", "WARN")


def db_upsert_node(node_name, node_ip, os_family, run_list, status):
    """Insert or update cinc_node_configs."""
    run_list_json = json.dumps(run_list)
    try:
        subprocess.run(
            ["psql", "-U", "kochj", "-d", "nova_ops", "-c",
             f"INSERT INTO cinc_node_configs (node_name, node_ip, os_family, run_list, last_converge, last_status) "
             f"VALUES ('{node_name}', '{node_ip}', '{os_family}', '{run_list_json}', now(), '{status}') "
             f"ON CONFLICT (node_name) DO UPDATE SET "
             f"last_converge = now(), last_status = '{status}', updated_at = now()"],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        log(f"DB node upsert failed: {e}", "WARN")


# ── OS Detection ──────────────────────────────────────────────────────────────

def detect_os(target):
    """Detect OS family of target host."""
    if target in ("localhost", "127.0.0.1", "192.168.1.6"):
        r = subprocess.run(["uname", "-s"], capture_output=True, text=True)
        return "macos" if "Darwin" in r.stdout else "linux"
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             f"{SSH_USER}@{target}", "uname -s"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return "macos" if "Darwin" in r.stdout else "linux"
    except Exception:
        pass
    return "unknown"


# ── CINC Installation ─────────────────────────────────────────────────────────

def check_cinc_installed(target):
    """Check if cinc-client is already installed."""
    if target in ("localhost", "127.0.0.1", "192.168.1.6"):
        r = subprocess.run(["which", "cinc-client"], capture_output=True, text=True)
        return r.returncode == 0
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", f"{SSH_USER}@{target}", "which cinc-client"],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def install_cinc(target, os_family):
    """Install cinc-client on target."""
    log(f"Installing CINC client on {target} ({os_family})...")

    if target in ("localhost", "127.0.0.1", "192.168.1.6"):
        # Local install
        r = subprocess.run(
            ["bash", "-c", f"curl -fsSL {CINC_INSTALL_URL} | sudo bash -s -- -P cinc"],
            capture_output=True, text=True, timeout=300,
        )
    else:
        # Remote install via SSH
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", f"{SSH_USER}@{target}",
             f"curl -fsSL {CINC_INSTALL_URL} | sudo bash -s -- -P cinc"],
            capture_output=True, text=True, timeout=300,
        )

    if r.returncode == 0:
        log("CINC client installed successfully")
        return True
    else:
        log(f"CINC install failed: {r.stderr[:500]}", "ERROR")
        return False


# ── Cookbook Push ──────────────────────────────────────────────────────────────

def push_cookbooks(target, node_json_path):
    """Push cookbooks and config to target via rsync."""
    if target in ("localhost", "127.0.0.1", "192.168.1.6"):
        # Local — cookbooks already in place, just validate paths
        log("Local target — cookbooks already available")
        return True

    log(f"Pushing cookbooks to {target}...")
    # Ensure remote dir exists
    subprocess.run(
        ["ssh", f"{SSH_USER}@{target}", f"sudo mkdir -p {REMOTE_CINC_DIR}"],
        capture_output=True, timeout=10,
    )

    # rsync cookbooks
    r = subprocess.run(
        ["rsync", "-avz", "--delete",
         f"{COOKBOOKS}/", f"{SSH_USER}@{target}:{REMOTE_COOKBOOKS}/"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        log(f"rsync failed: {r.stderr[:200]}", "ERROR")
        return False

    # Push solo.rb
    subprocess.run(
        ["scp", str(SOLO_RB), f"{SSH_USER}@{target}:{REMOTE_SOLO_RB}"],
        capture_output=True, timeout=10,
    )

    # Push node JSON
    subprocess.run(
        ["scp", str(node_json_path), f"{SSH_USER}@{target}:{REMOTE_NODE_JSON}"],
        capture_output=True, timeout=10,
    )

    log("Cookbooks pushed successfully")
    return True


# ── Converge ──────────────────────────────────────────────────────────────────

def run_converge(target, node_name, node_json_path, why_run=False):
    """Run cinc-client converge on target."""
    mode = "--why-run" if why_run else ""
    mode_label = "drift-check" if why_run else "converge"
    log(f"Running {mode_label} on {target}...")

    if target in ("localhost", "127.0.0.1", "192.168.1.6"):
        cmd = (
            f"cinc-client --local-mode "
            f"-c {SOLO_RB} "
            f"-j {node_json_path} "
            f"--chef-zero-port 18901 "
            f"{mode}"
        ).strip()
        r = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=300,
        )
    else:
        cmd = (
            f"sudo cinc-client --local-mode "
            f"-c {REMOTE_SOLO_RB} "
            f"-j {REMOTE_NODE_JSON} "
            f"--chef-zero-port 18901 "
            f"{mode}"
        ).strip()
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", f"{SSH_USER}@{target}", cmd],
            capture_output=True, text=True, timeout=300,
        )

    # Parse output for resource counts
    resources_updated = 0
    for line in r.stdout.split("\n"):
        if "resources updated" in line.lower():
            try:
                resources_updated = int(line.split()[0])
            except (ValueError, IndexError):
                pass

    return r.returncode, r.stdout, r.stderr, resources_updated


# ── List Nodes ────────────────────────────────────────────────────────────────

def list_nodes():
    """List all managed nodes from database."""
    r = subprocess.run(
        ["psql", "-U", "kochj", "-d", "nova_ops", "-c",
         "SELECT node_name, node_ip, os_family, last_status, last_converge "
         "FROM cinc_node_configs ORDER BY node_name"],
        capture_output=True, text=True, timeout=10,
    )
    print(r.stdout)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bootstrap CINC Solo on nodes")
    parser.add_argument("target", nargs="?", help="Target IP or 'localhost'")
    parser.add_argument("--converge", action="store_true", help="Skip install, just converge")
    parser.add_argument("--why-run", action="store_true", help="Dry-run drift check")
    parser.add_argument("--list", action="store_true", help="List managed nodes")
    parser.add_argument("--node-json", help="Path to node JSON file")
    parser.add_argument("--triggered-by", default="manual", help="Who triggered this run")
    args = parser.parse_args()

    if args.list:
        list_nodes()
        return

    if not args.target:
        parser.print_help()
        return

    target = args.target
    run_id = str(uuid.uuid4())[:12]
    run_type = "drift_check" if args.why_run else ("converge" if args.converge else "bootstrap")
    start_time = time.time()

    # Resolve node name and JSON
    if target in ("localhost", "127.0.0.1", "192.168.1.6"):
        node_name = "mac-studio"
        node_ip = "192.168.1.6"
    else:
        node_name = target.replace(".", "-")
        node_ip = target

    node_json = Path(args.node_json) if args.node_json else (NODES_DIR / f"{node_name}.json")
    if not node_json.exists():
        log(f"Node JSON not found: {node_json}", "ERROR")
        log("Create it first or specify --node-json")
        return

    log(f"Target: {target} ({node_name}), run_type: {run_type}")
    db_record_run(run_id, node_name, node_ip, run_type, args.triggered_by)

    # Detect OS
    os_family = detect_os(target)
    log(f"OS: {os_family}")
    if os_family == "unknown":
        log("Cannot detect OS — aborting", "ERROR")
        db_complete_run(run_id, "failure", 1, error_output="OS detection failed")
        return

    # Install CINC if bootstrap
    if not args.converge and not args.why_run:
        if not check_cinc_installed(target):
            if not install_cinc(target, os_family):
                db_complete_run(run_id, "failure", 1, error_output="CINC install failed")
                return
        else:
            log("CINC client already installed — skipping install")

    # Push cookbooks (remote targets only)
    if not push_cookbooks(target, node_json):
        db_complete_run(run_id, "failure", 1, error_output="Cookbook push failed")
        return

    # Run converge
    exit_code, stdout, stderr, resources = run_converge(
        target, node_name, node_json, why_run=args.why_run
    )

    duration = time.time() - start_time
    status = "success" if exit_code == 0 else "failure"

    log(f"Converge {status}: exit_code={exit_code}, resources_updated={resources}, duration={duration:.1f}s")

    if stdout:
        # Show last 20 lines of output
        lines = stdout.strip().split("\n")
        for line in lines[-20:]:
            log(f"  {line}")

    # Record results
    error_out = stderr if exit_code != 0 else ""
    db_complete_run(run_id, status, exit_code, resources, error_out, duration)
    db_upsert_node(node_name, node_ip, os_family, ["recipe[nova_base]"], status)

    # Notify on failure
    if exit_code != 0:
        notify(
            f":x: *CINC Converge Failed* — {node_name} ({node_ip})\n"
            f"  Exit code: {exit_code}\n"
            f"  Error: {stderr[:200]}"
        )
    else:
        action = "Drift check" if args.why_run else "Converge"
        notify(
            f":white_check_mark: *CINC {action}* — {node_name} ({node_ip})\n"
            f"  Resources updated: {resources}\n"
            f"  Duration: {duration:.1f}s"
        )


if __name__ == "__main__":
    main()
