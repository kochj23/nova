#!/usr/bin/env python3
"""
nova_deploy_agent.py — Continuous Deployment (#30)

Watches deploy_requests table and executes them:
  - restart/reload/stop/start services via launchctl
  - deploy_script: write new script content + restart
  - Health check after deploy
  - Automatic rollback on failure

Runs as a persistent daemon. Nova or Claude create deploy_requests rows;
this agent picks them up and executes.

Port: 37471 (health API)
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
POLL_INTERVAL = 5
PORT = 37471

SCRIPT_DIR = Path.home() / ".openclaw/scripts"
BACKUP_DIR = Path.home() / ".openclaw/backups/deploys"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def _conn():
    c = psycopg2.connect(DB_DSN)
    c.autocommit = True
    return c


def _annotate_grafana(text, tags):
    """Post annotation to Grafana for deploy/fix events."""
    import urllib.request
    try:
        payload = json.dumps({
            "text": text,
            "tags": tags,
            "time": int(time.time() * 1000),
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:3000/api/annotations",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic YWRtaW46YWRtaW4=",  # admin:admin base64
            },
            method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _trigger_security_scan(service):
    """Ask security scanner to check the host after a deploy."""
    import urllib.request
    try:
        payload = json.dumps({"host": "mac-studio", "reason": f"post-deploy:{service}"}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:37474/scan",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _update_security_baseline(service):
    """Update rkhunter props after a known-good deploy."""
    try:
        # Only update if rkhunter is installed locally
        subprocess.run(["/opt/homebrew/bin/rkhunter", "--propupd"],
                     capture_output=True, timeout=60)
        # Record in DB
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO security_baselines (host_name, tool, baseline_hash, created_by, notes)
            VALUES ('mac-studio', 'rkhunter', %s, 'deploy_agent', %s)
            ON CONFLICT (host_name, tool) DO UPDATE SET
                baseline_hash = EXCLUDED.baseline_hash,
                created_at = now(),
                created_by = EXCLUDED.created_by,
                notes = EXCLUDED.notes
        """, (datetime.now().isoformat(), f"Post-deploy update for {service}"))
        cur.close()
        conn.close()
    except Exception:
        pass


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[deploy {ts}] {msg}", flush=True)


def get_pending():
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM deploy_requests
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT 5
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def update_status(deploy_id, status, **kwargs):
    conn = _conn()
    cur = conn.cursor()
    sets = ["status = %s"]
    params = [status]
    if status == "in_progress":
        sets.append("started_at = now()")
    if status in ("success", "failed", "rolled_back"):
        sets.append("completed_at = now()")
    for k, v in kwargs.items():
        sets.append(f"{k} = %s")
        params.append(v)
    params.append(deploy_id)
    cur.execute(f"UPDATE deploy_requests SET {', '.join(sets)} WHERE id = %s", params)
    cur.close()
    conn.close()


def notify_slack(msg):
    try:
        import nova_config
        nova_config.slack(msg, nova_config.SLACK_NOTIFY)
    except Exception:
        pass


def execute_deploy(deploy):
    deploy_id = deploy["id"]
    service = deploy["target_service"]
    action = deploy["action"]
    payload = deploy.get("payload") or {}

    log(f"Executing deploy #{deploy_id}: {action} {service}")
    update_status(deploy_id, "in_progress")

    try:
        if action == "restart":
            _restart_service(service)
        elif action == "reload":
            _reload_service(service)
        elif action == "stop":
            _stop_service(service)
        elif action == "start":
            _start_service(service)
        elif action == "deploy_script":
            _deploy_script(service, payload)
        else:
            raise ValueError(f"Unknown action: {action}")

        # Health check
        health_url = deploy.get("health_check_url")
        if health_url:
            time.sleep(3)
            health_ok = _check_health(health_url)
            if not health_ok:
                raise RuntimeError(f"Health check failed: {health_url}")
            update_status(deploy_id, "success", health_check_status="healthy")
        else:
            time.sleep(2)
            update_status(deploy_id, "success", health_check_status="no_check")

        log(f"Deploy #{deploy_id} SUCCESS: {action} {service}")
        _annotate_grafana(f"Deploy: {action} {service}", ["deploy", service])
        _trigger_security_scan(service)
        if action == "deploy_script":
            _update_security_baseline(service)
        notify_slack(f":rocket: Deploy #{deploy_id} success: `{action}` on `{service}`")

        # Record observation
        _observe("nova", "runtime", service,
                 f"Deploy #{deploy_id} succeeded: {action}",
                 metadata={"deploy_id": deploy_id, "action": action})

    except Exception as e:
        error_msg = str(e)
        log(f"Deploy #{deploy_id} FAILED: {error_msg}")
        _annotate_grafana(f"Deploy FAILED: {action} {service}", ["deploy", "failed", service])

        # Attempt rollback
        rollback = deploy.get("rollback_action")
        if rollback:
            try:
                log(f"Rolling back: {rollback}")
                subprocess.run(rollback, shell=True, timeout=30,
                               capture_output=True, text=True)
                update_status(deploy_id, "rolled_back", error=error_msg)
                notify_slack(f":warning: Deploy #{deploy_id} failed + rolled back: `{service}` — {error_msg}")
                return
            except Exception as rb_err:
                error_msg += f" (rollback also failed: {rb_err})"

        update_status(deploy_id, "failed", error=error_msg)
        notify_slack(f":x: Deploy #{deploy_id} FAILED: `{service}` — {error_msg}")


def _restart_service(service):
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{service}"],
                   capture_output=True, text=True, timeout=30, check=True)


def _reload_service(service):
    _stop_service(service)
    time.sleep(1)
    _start_service(service)


def _stop_service(service):
    subprocess.run(["launchctl", "stop", service],
                   capture_output=True, text=True, timeout=15)


def _start_service(service):
    subprocess.run(["launchctl", "start", service],
                   capture_output=True, text=True, timeout=15)


def _deploy_script(service, payload):
    """Deploy a new version of a script and restart its service."""
    script_path = payload.get("script_path")
    script_content = payload.get("script_content")
    launchd_label = payload.get("launchd_label", service)

    if not script_path or not script_content:
        raise ValueError("deploy_script requires payload.script_path and payload.script_content")

    target = Path(script_path)
    if not target.exists():
        raise FileNotFoundError(f"Target script not found: {script_path}")

    # Backup current version
    backup_name = f"{target.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{target.suffix}"
    shutil.copy2(target, BACKUP_DIR / backup_name)
    log(f"Backed up {target.name} → {backup_name}")

    # Write new version
    target.write_text(script_content)
    if target.suffix in (".sh", ".py"):
        target.chmod(0o755)

    # Restart the service
    _restart_service(launchd_label)


def _check_health(url):
    """Check health endpoint returns 200."""
    import urllib.request
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def _observe(observer, category, subject, observation, metadata=None):
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, metadata)
            VALUES (%s, %s, %s, %s, %s)
        """, (observer, category, subject, observation, json.dumps(metadata or {})))
        cur.close()
        conn.close()
    except Exception:
        pass


# ── Main Loop ────────────────────────────────────────────────────────────────

def run_loop():
    log("Deploy agent started — watching deploy_requests table")
    while True:
        try:
            pending = get_pending()
            for deploy in pending:
                execute_deploy(deploy)
        except Exception as e:
            log(f"Poll error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "service": "deploy_agent"}).encode())
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, *args):
            pass

    # Health API in background thread
    server = HTTPServer(("127.0.0.1", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Health API on port {PORT}")

    run_loop()
