#!/usr/bin/env python3
"""
nova_nas_rsync.py — Synology → UNAS rsync replication.

Syncs the Synology's mounted copies of UNAS shares back to the UNAS.
Direction: Synology (source of truth for backups) → UNAS local mounts.

The Synology mounts UNAS shares via SMB at:
  \\192.168.1.69\nas      → /volume1/docker/nas
  \\192.168.1.69\external → /volume1/docker/external

This script pulls FROM Synology TO local UNAS mounts:
  Synology:/volume1/docker/nas/      → /Volumes/nas-1/
  Synology:/volume1/docker/external/ → /Volumes/external/

Runs nightly. Never deletes (additive only for safety).
Logs progress to PG + shared_observations.

Written by Jordan Koch.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
SYNOLOGY_IP = "192.168.1.11"
SYNOLOGY_USER = "kochj"
SSH_OPTS = "-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new"

SHARES = [
    {
        "name": "nas",
        "source": f"{SYNOLOGY_USER}@{SYNOLOGY_IP}:/volume1/docker/nas/",
        "dest": "/Volumes/nas-1/",
    },
    {
        "name": "external",
        "source": f"{SYNOLOGY_USER}@{SYNOLOGY_IP}:/volume1/docker/external/",
        "dest": "/Volumes/external/",
    },
]


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[nas_rsync {ts}] {msg}", flush=True)


def preflight_checks():
    """Run all safety checks before syncing."""
    errors = []

    # Check local mounts are accessible
    for share in SHARES:
        dest = Path(share["dest"])
        if not dest.exists():
            errors.append(f"{share['name']}: destination not mounted ({share['dest']})")
        elif not dest.is_dir():
            errors.append(f"{share['name']}: destination is not a directory")
        elif not os.access(share["dest"], os.W_OK):
            errors.append(f"{share['name']}: destination not writable")

    # Check SSH to Synology
    try:
        r = subprocess.run(
            f"ssh {SSH_OPTS} {SYNOLOGY_USER}@{SYNOLOGY_IP} 'echo OK'",
            shell=True, capture_output=True, text=True, timeout=15)
        if "OK" not in r.stdout:
            errors.append(f"SSH to Synology ({SYNOLOGY_IP}) failed: {r.stderr[:100]}")
    except subprocess.TimeoutExpired:
        errors.append(f"SSH to Synology ({SYNOLOGY_IP}) timed out")
    except Exception as e:
        errors.append(f"SSH to Synology ({SYNOLOGY_IP}) error: {e}")

    # Check remote source dirs exist
    for share in SHARES:
        remote_path = share["source"].split(":", 1)[1]
        try:
            r = subprocess.run(
                f"ssh {SSH_OPTS} {SYNOLOGY_USER}@{SYNOLOGY_IP} 'test -d \"{remote_path}\" && echo EXISTS'",
                shell=True, capture_output=True, text=True, timeout=15)
            if "EXISTS" not in r.stdout:
                errors.append(f"{share['name']}: remote source not found ({remote_path})")
        except Exception as e:
            errors.append(f"{share['name']}: remote check failed ({e})")

    # Check disk space on destination (don't sync if < 5% free)
    for share in SHARES:
        try:
            r = subprocess.run(f"df -P '{share['dest']}'", shell=True, capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    capacity = int(parts[4].rstrip('%'))
                    if capacity > 95:
                        errors.append(f"{share['name']}: destination disk {capacity}% full (< 5% free)")
        except Exception:
            pass

    # Check rsync binary exists
    rsync_path = "/opt/homebrew/bin/rsync" if Path("/opt/homebrew/bin/rsync").exists() else "/usr/bin/rsync"
    try:
        r = subprocess.run([rsync_path, "--version"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            errors.append("rsync binary not functional")
    except Exception:
        errors.append("rsync binary not found")

    return errors


def rsync_share(share):
    """Rsync one share from Synology to local UNAS mount."""
    name = share["name"]
    source = share["source"]
    dest = share["dest"]

    if not Path(dest).exists():
        return {"name": name, "status": "error", "error": f"Destination not mounted: {dest}"}

    log(f"Syncing {name}: {source} → {dest}")
    start = time.time()

    cmd = (
        f"rsync -av --progress --stats "
        f"--exclude='.DS_Store' "
        f"--exclude='@eaDir/' "
        f"--exclude='.Spotlight-V100/' "
        f"--exclude='.Trashes/' "
        f"-e 'ssh {SSH_OPTS}' "
        f"'{source}' '{dest}' 2>&1"
    )

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=14400)
        output = result.stdout + result.stderr
        duration = time.time() - start
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "error", "error": "rsync timed out (4h)", "duration_s": 14400}
    except Exception as e:
        return {"name": name, "status": "error", "error": str(e)}

    # Parse stats
    transferred = 0
    total_size = 0
    for line in output.splitlines():
        line = line.strip()
        if "Number of regular files transferred:" in line:
            try:
                transferred = int(line.split(":")[1].strip().replace(",", ""))
            except (ValueError, IndexError):
                pass
        elif line.startswith("Total transferred file size:"):
            try:
                total_size = int(line.split(":")[1].strip().split()[0].replace(",", ""))
            except (ValueError, IndexError):
                pass

    status = "ok" if result.returncode == 0 else "warning"

    return {
        "name": name,
        "status": status,
        "files_transferred": transferred,
        "bytes_transferred": total_size,
        "duration_s": round(duration, 1),
        "exit_code": result.returncode,
        "completed_at": datetime.now().isoformat(),
    }


def run_sync():
    """Sync all shares and record results."""
    log("Starting NAS rsync: Synology → UNAS")

    # Preflight
    errors = preflight_checks()
    if errors:
        for e in errors:
            log(f"  PREFLIGHT FAIL: {e}")
        try:
            import nova_config
            nova_config.notify_local("NAS Rsync FAILED", f"Preflight: {errors[0]}", critical=True)
            nova_config.post_both(f":x: *NAS Rsync preflight failed:*\n" + "\n".join(f"• {e}" for e in errors),
                                 nova_config.SLACK_NOTIFY)
        except Exception:
            pass
        return [{"name": "preflight", "status": "error", "errors": errors}]

    log("Preflight OK — all checks passed")
    results = []

    for share in SHARES:
        result = rsync_share(share)
        results.append(result)
        if result.get("status") == "ok":
            log(f"  {result['name']}: {result['files_transferred']} files, "
                f"{result['bytes_transferred'] / 1e9:.2f} GB in {result['duration_s']}s")
        else:
            log(f"  {result['name']}: {result.get('status')} — {result.get('error', 'unknown')}")

    # Record to shared_observations
    total_files = sum(r.get("files_transferred", 0) for r in results)
    total_bytes = sum(r.get("bytes_transferred", 0) for r in results)
    total_duration = sum(r.get("duration_s", 0) for r in results)

    try:
        import psycopg2
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES ('nova', 'storage', 'nas-rsync', %s, 'info', %s)
        """, (
            f"NAS rsync complete: {total_files} files, {total_bytes / 1e9:.2f} GB transferred in {total_duration:.0f}s",
            json.dumps({"shares": results, "total_files": total_files, "total_bytes": total_bytes}),
        ))
        cur.close()
        conn.close()
    except Exception as e:
        log(f"DB write failed: {e}")

    # Notify — always post summary to notifications channel
    try:
        import nova_config
        all_ok = all(r.get("status") == "ok" for r in results)
        status_icon = "✅" if all_ok else "⚠️"
        shares_detail = " | ".join(
            f"{r['name']}: {r.get('files_transferred', 0)} files ({r.get('bytes_transferred', 0) / 1e9:.2f} GB)"
            for r in results
        )
        msg = (
            f"{status_icon} *NAS Rsync — Daily Sync*\n"
            f"Synology → UNAS | {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"{shares_detail}\n"
            f"Total: {total_files} files, {total_bytes / 1e9:.2f} GB in {total_duration:.0f}s"
        )
        nova_config.post_both(msg, nova_config.SLACK_NOTIFY)
        if total_files > 0:
            nova_config.notify_local("NAS Rsync Complete",
                                     f"{total_files} files synced ({total_bytes / 1e9:.1f} GB)")
    except Exception as e:
        log(f"Notification failed: {e}")

    # Save state for dashboard
    state_file = Path.home() / ".openclaw/workspace/state/nova_nas_rsync.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "last_sync": datetime.now().isoformat(),
        "shares": results,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "total_duration_s": total_duration,
    }, indent=2))

    log(f"Complete: {total_files} files, {total_bytes / 1e9:.2f} GB")
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synced without transferring")
    parser.add_argument("--share", choices=["nas", "external"], help="Sync only one share")
    args = parser.parse_args()

    if args.dry_run:
        for share in SHARES:
            if args.share and share["name"] != args.share:
                continue
            log(f"DRY RUN: {share['source']} → {share['dest']}")
            cmd = (
                f"rsync -an --stats "
                f"--exclude='.DS_Store' --exclude='@eaDir/' "
                f"-e 'ssh {SSH_OPTS}' "
                f"'{share['source']}' '{share['dest']}' 2>&1"
            )
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
            print(result.stdout[-500:] if result.stdout else "No output")
    else:
        if args.share:
            SHARES[:] = [s for s in SHARES if s["name"] == args.share]
        run_sync()
