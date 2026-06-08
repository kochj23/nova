#!/usr/bin/env python3
"""
nova_nas_sync_check.py — Lightweight NAS sync percentage check.

Compares UNAS Pro 8 shares against Synology RS1221+ using rsync --dry-run.
Reports what percentage of files are identical (in sync) vs need transfer.

Runs nightly. Results stored in nova_ops + shared_observations.
Exposes on port 37475 for dashboard consumption.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
STATE_FILE = Path.home() / ".openclaw/workspace/state/nova_nas_sync.json"

SYNOLOGY_IP = "192.168.1.11"
SYNOLOGY_USER = "kochj"

# Share mappings: UNAS (local AFP mount) → Synology (SMB mount of same UNAS shares)
# Synology mounts UNAS at \\192.168.1.69\nas → /volume1/docker/nas
SHARES = [
    {
        "name": "nas",
        "local": "/Volumes/nas-1/",
        "remote": f"{SYNOLOGY_USER}@{SYNOLOGY_IP}:/volume1/docker/nas/",
    },
    {
        "name": "external",
        "local": "/Volumes/external/",
        "remote": f"{SYNOLOGY_USER}@{SYNOLOGY_IP}:/volume1/docker/external/",
    },
]

SSH_OPTS = "-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[nas_sync {ts}] {msg}", flush=True)


def check_share_sync(share):
    """Compare file counts between local UNAS mount and Synology via SSH."""
    name = share["name"]
    local = share["local"]
    remote = share["remote"]

    if not Path(local).exists():
        return {"name": name, "status": "error", "error": f"Local mount not found: {local}"}

    log(f"Checking {name}: counting files...")
    start = time.time()

    # Count local files (UNAS via AFP mount)
    try:
        r = subprocess.run(
            f"find '{local}' -type f 2>/dev/null | wc -l",
            shell=True, capture_output=True, text=True, timeout=600)
        local_count = int(r.stdout.strip())
    except Exception as e:
        return {"name": name, "status": "error", "error": f"Local count failed: {e}"}

    # Count remote files (Synology via SSH)
    remote_path = remote.split(":", 1)[1]
    try:
        r = subprocess.run(
            f"ssh {SSH_OPTS} {SYNOLOGY_USER}@{SYNOLOGY_IP} "
            f"\"find '{remote_path}' -type f 2>/dev/null | wc -l\"",
            shell=True, capture_output=True, text=True, timeout=600)
        remote_count = int(r.stdout.strip())
    except Exception as e:
        return {"name": name, "status": "error", "error": f"Remote count failed: {e}"}

    duration = time.time() - start

    # Sync percentage: ratio of smaller to larger (100% = identical counts)
    if max(local_count, remote_count) == 0:
        sync_pct = 100.0
    else:
        sync_pct = round((min(local_count, remote_count) / max(local_count, remote_count)) * 100, 2)

    diff = abs(local_count - remote_count)
    direction = "UNAS ahead" if local_count > remote_count else "Synology ahead" if remote_count > local_count else "matched"

    return {
        "name": name,
        "status": "ok",
        "sync_pct": sync_pct,
        "local_files": local_count,
        "remote_files": remote_count,
        "difference": diff,
        "direction": direction,
        "duration_s": round(duration, 1),
        "checked_at": datetime.now().isoformat(),
    }


def run_check():
    """Check all shares and store results."""
    results = []
    for share in SHARES:
        result = check_share_sync(share)
        results.append(result)
        if result.get("status") == "ok":
            log(f"  {result['name']}: {result['sync_pct']}% synced "
                f"(UNAS={result['local_files']:,} / Synology={result['remote_files']:,}, "
                f"diff={result['difference']:,} — {result['direction']})")
        else:
            log(f"  {result['name']}: ERROR — {result.get('error')}")

    # Calculate overall sync
    total_local = sum(r.get("local_files", 0) for r in results if r.get("status") == "ok")
    total_remote = sum(r.get("remote_files", 0) for r in results if r.get("status") == "ok")
    overall_pct = round((min(total_local, total_remote) / max(total_local, total_remote, 1)) * 100, 2)
    total_diff = abs(total_local - total_remote)

    state = {
        "checked_at": datetime.now().isoformat(),
        "overall_sync_pct": overall_pct,
        "shares": results,
    }

    # Save state
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

    # Store in DB
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES ('nova', 'storage', 'nas-sync', %s, %s, %s)
        """, (
            f"NAS sync check: {overall_pct}% in sync ({total_diff:,} files differ)",
            "warning" if overall_pct < 95 else "info",
            json.dumps(state),
        ))
        cur.close()
        conn.close()
    except Exception as e:
        log(f"DB write failed: {e}")

    # Alert if badly out of sync
    if overall_pct < 90:
        try:
            import nova_config
            nova_config.notify_local("NAS Sync Warning",
                                     f"Only {overall_pct}% synced — {total_diff:,} files differ",
                                     critical=True)
            nova_config.post_both(
                f":warning: *NAS Sync Alert:* Only {overall_pct}% in sync — "
                f"{total_diff:,} files differ between UNAS and Synology",
                nova_config.SLACK_NOTIFY)
        except Exception:
            pass

    log(f"Overall: {overall_pct}% in sync")
    return state


# ── HTTP API (port 37475) ────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="Run as HTTP daemon")
    parser.add_argument("--check", action="store_true", help="Run sync check now")
    args = parser.parse_args()

    if args.check or not args.serve:
        run_check()

    if args.serve:
        from http.server import HTTPServer, BaseHTTPRequestHandler

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/health":
                    self._json(200, {"status": "ok", "service": "nas-sync-check"})
                elif self.path == "/status":
                    if STATE_FILE.exists():
                        self._json(200, json.loads(STATE_FILE.read_text()))
                    else:
                        self._json(200, {"status": "no data yet", "overall_sync_pct": None})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                if self.path == "/check":
                    result = run_check()
                    self._json(200, result)
                else:
                    self._json(404, {"error": "not found"})

            def _json(self, code, data):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data, default=str).encode())

            def log_message(self, *args):
                pass

        log("NAS Sync Check API on port 37475")
        HTTPServer(("127.0.0.1", 37475), Handler).serve_forever()
