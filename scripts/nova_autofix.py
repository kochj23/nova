#!/usr/bin/env python3
"""
nova_autofix.py — Auto-Fix Pipeline (#29)

Watches for known failure patterns and applies fixes automatically.
No human in the loop — Claude seeds fix patterns, Nova applies them.

Flow:
  1. Big Brother detects failure → scheduler_runs / service health
  2. Autofix matches against fix_patterns table
  3. If matched + confidence >= threshold → apply fix
  4. Verify via health check
  5. Record success/failure → adjusts confidence over time

Learns: successful fixes increase confidence, failures decrease it.
Disabled patterns (confidence < 0.3) stop being applied.

Port: 37472 (health + stats API)
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
POLL_INTERVAL = 30
CONFIDENCE_THRESHOLD = 0.5
PORT = 37472

_stats = {"checks": 0, "fixes_applied": 0, "fixes_succeeded": 0, "fixes_failed": 0}


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
            "http://192.168.1.6:3001/api/annotations",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Basic YWRtaW46Smtvb2dpZTAwMQ==",  # admin:admin base64
            },
            method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[autofix {ts}] {msg}", flush=True)


def notify_slack(msg):
    try:
        import nova_config
        nova_config.slack(msg, nova_config.SLACK_NOTIFY)
    except Exception:
        pass


def get_active_patterns():
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM fix_patterns
        WHERE enabled = true AND confidence >= %s
        ORDER BY confidence DESC
    """, (CONFIDENCE_THRESHOLD,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def check_trigger(pattern, system_state):
    """Check if a pattern's trigger condition matches current state."""
    trigger = pattern["trigger_condition"]

    # Match by service health failure
    if "service_down" in trigger:
        service = trigger["service_down"]
        port = trigger.get("port")
        host = trigger.get("host", "127.0.0.1")
        if port and not _port_alive(port, host):
            return True, f"{service} port {port} on {host} not responding"

    # Match by error pattern in recent scheduler runs
    if "error_pattern" in trigger:
        task_id = trigger.get("task_id")
        error_re = trigger["error_pattern"]
        recent_error = _get_recent_error(task_id)
        if recent_error and re.search(error_re, recent_error):
            return True, f"Error matched: {error_re[:50]}"

    # Match by high consecutive failures
    if "consecutive_failures" in trigger:
        task_id = trigger.get("task_id")
        threshold = trigger["consecutive_failures"]
        count = _get_consecutive_failures(task_id)
        if count >= threshold:
            return True, f"{task_id} has {count} consecutive failures"

    # Match by metric threshold
    if "metric_above" in trigger:
        metric = trigger["metric_above"]
        device = trigger.get("device", "mac-studio")
        threshold = trigger["threshold"]
        current = _get_latest_metric(device, metric)
        if current is not None and current > threshold:
            return True, f"{device}/{metric} = {current} > {threshold}"

    # Match by security scan critical finding
    if "security_critical" in trigger:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM security_scan_results
            WHERE status = 'critical' AND scan_time > now() - interval '1 hour'
            AND host_name = %s
        """, (trigger.get("host", "mac-studio"),))
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        if count > 0:
            return True, f"Critical security finding on {trigger.get('host')}"

    return False, None


def apply_fix(pattern, trigger_reason):
    """Apply a fix and record the attempt."""
    pattern_id = pattern["id"]
    fix_action = pattern["fix_action"]
    pattern_name = pattern["pattern_name"]

    log(f"Applying fix '{pattern_name}' — trigger: {trigger_reason}")

    conn = _conn()
    cur = conn.cursor()

    # Record attempt
    cur.execute("""
        INSERT INTO fix_attempts (pattern_id, trigger_event, service, action_taken, status)
        VALUES (%s, %s, %s, %s, 'applied')
        RETURNING id
    """, (pattern_id, trigger_reason, fix_action.get("service", "unknown"),
          json.dumps(fix_action)))
    attempt_id = cur.fetchone()[0]
    cur.close()
    conn.close()

    try:
        # Execute the fix
        if fix_action.get("type") == "restart":
            service = fix_action["service"]
            subprocess.run(["launchctl", "kickstart", "-k",
                          f"gui/{os.getuid()}/{service}"],
                         capture_output=True, text=True, timeout=30, check=True)

        elif fix_action.get("type") == "command":
            cmd = fix_action["command"]
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(f"Command failed (rc={r.returncode}): {r.stderr[:200]}")

        elif fix_action.get("type") == "deploy":
            # Create a deploy_request for the deploy agent
            conn = _conn()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO deploy_requests (requested_by, target_service, action, payload, health_check_url)
                VALUES ('autofix', %s, %s, %s, %s)
            """, (fix_action["service"], fix_action.get("action", "restart"),
                  json.dumps(fix_action.get("payload", {})),
                  fix_action.get("health_check_url")))
            cur.close()
            conn.close()

        # Verify
        time.sleep(5)
        health_url = fix_action.get("health_check_url")
        if health_url:
            if not _check_health(health_url):
                raise RuntimeError(f"Post-fix health check failed: {health_url}")

        # Success
        _update_attempt(attempt_id, "success")
        _adjust_confidence(pattern_id, success=True)
        _stats["fixes_succeeded"] += 1

        log(f"Fix '{pattern_name}' SUCCEEDED")
        _annotate_grafana(f"Auto-fix: {pattern_name}", ["autofix", fix_action.get("service", "unknown")])
        notify_slack(f":wrench: Auto-fix applied: *{pattern_name}* — {trigger_reason}")

        # Record shared observation
        _observe("nova", "runtime", fix_action.get("service", pattern_name),
                 f"Auto-fix applied successfully: {pattern_name}. Trigger: {trigger_reason}",
                 severity="info", metadata={"pattern_id": pattern_id, "attempt_id": attempt_id})

    except Exception as e:
        _update_attempt(attempt_id, "failed", str(e))
        _adjust_confidence(pattern_id, success=False)
        _stats["fixes_failed"] += 1

        log(f"Fix '{pattern_name}' FAILED: {e}")
        _annotate_grafana(f"Auto-fix FAILED: {pattern_name}", ["autofix", "failed"])
        notify_slack(f":x: Auto-fix failed: *{pattern_name}* — {e}")


def _update_attempt(attempt_id, status, result=None):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE fix_attempts SET status = %s, result = %s, verified_at = now()
        WHERE id = %s
    """, (status, result, attempt_id))
    cur.close()
    conn.close()


def _adjust_confidence(pattern_id, success):
    conn = _conn()
    cur = conn.cursor()
    if success:
        cur.execute("""
            UPDATE fix_patterns
            SET success_count = success_count + 1,
                confidence = LEAST(0.99, confidence + 0.05),
                last_applied = now()
            WHERE id = %s
        """, (pattern_id,))
    else:
        cur.execute("""
            UPDATE fix_patterns
            SET failure_count = failure_count + 1,
                confidence = GREATEST(0.1, confidence - 0.15),
                last_applied = now()
            WHERE id = %s
        """, (pattern_id,))
    cur.close()
    conn.close()


def _port_alive(port, host="127.0.0.1"):
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


def _check_health(url):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def _get_recent_error(task_id):
    if not task_id:
        return None
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT error_tail FROM scheduler_runs
        WHERE task_id = %s AND status = 'failed'
        ORDER BY ended_at DESC LIMIT 1
    """, (task_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def _get_consecutive_failures(task_id):
    if not task_id:
        return 0
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT status FROM scheduler_runs
        WHERE task_id = %s
        ORDER BY ended_at DESC LIMIT 10
    """, (task_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    count = 0
    for row in rows:
        if row[0] == "failed":
            count += 1
        else:
            break
    return count


def _get_latest_metric(device, metric):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT metric_value FROM snmp_metrics
        WHERE device_name = %s AND metric_name = %s
        ORDER BY timestamp DESC LIMIT 1
    """, (device, metric))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def _observe(observer, category, subject, observation, severity="info", metadata=None):
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (observer, category, subject, observation, severity, json.dumps(metadata or {})))
        cur.close()
        conn.close()
    except Exception:
        pass


# ── Seed Known Fix Patterns ──────────────────────────────────────────────────

def seed_patterns():
    """Seed the fix_patterns table with known fixes from operational history."""
    patterns = [
        {
            "pattern_name": "memory_server_restart",
            "trigger_condition": {"service_down": "memory-server", "port": 18790},
            "fix_action": {"type": "restart", "service": "net.digitalnoise.nova-memory-server",
                          "health_check_url": "http://192.168.1.6:18790/health"},
            "confidence": 0.9,
            "created_by": "claude",
        },
        {
            "pattern_name": "ollama_gpu_stuck",
            "trigger_condition": {"service_down": "ollama", "port": 11434},
            "fix_action": {"type": "command",
                          "command": "pkill ollama; sleep 3; open -a Ollama",
                          "service": "ollama",
                          "health_check_url": "http://127.0.0.1:11434/api/tags"},
            "confidence": 0.85,
            "created_by": "claude",
        },
        {
            "pattern_name": "novacontrol_restart",
            "trigger_condition": {"service_down": "novacontrol", "port": 37400},
            "fix_action": {"type": "restart", "service": "net.digitalnoise.NovaControl",
                          "health_check_url": "http://127.0.0.1:37400/"},
            "confidence": 0.9,
            "created_by": "claude",
        },
        {
            "pattern_name": "gateway_restart",
            "trigger_condition": {"service_down": "gateway", "port": 18789},
            "fix_action": {"type": "restart", "service": "net.digitalnoise.nova-gateway",
                          "health_check_url": "http://127.0.0.1:18789/health"},
            "confidence": 0.85,
            "created_by": "claude",
        },
        {
            "pattern_name": "snmp_poller_restart",
            "trigger_condition": {"service_down": "snmp-poller", "port": 37463},
            "fix_action": {"type": "restart", "service": "net.digitalnoise.nova-snmp-poller",
                          "health_check_url": "http://127.0.0.1:37463/health"},
            "confidence": 0.9,
            "created_by": "claude",
        },
    ]

    conn = _conn()
    cur = conn.cursor()
    for p in patterns:
        cur.execute("""
            INSERT INTO fix_patterns (pattern_name, trigger_condition, fix_action, confidence, created_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (pattern_name) DO UPDATE SET
                trigger_condition = EXCLUDED.trigger_condition,
                fix_action = EXCLUDED.fix_action,
                confidence = EXCLUDED.confidence
        """, (p["pattern_name"], json.dumps(p["trigger_condition"]),
              json.dumps(p["fix_action"]), p["confidence"], p["created_by"]))
    cur.close()
    conn.close()
    log(f"Seeded {len(patterns)} fix patterns")


# ── Main Loop ────────────────────────────────────────────────────────────────

def run_loop():
    log("Auto-fix engine started")
    seed_patterns()

    while True:
        try:
            _stats["checks"] += 1
            patterns = get_active_patterns()
            system_state = {}  # reserved for future state aggregation

            for pattern in patterns:
                triggered, reason = check_trigger(pattern, system_state)
                if triggered:
                    # Rate limit: don't apply same fix more than once per 10 minutes
                    if pattern.get("last_applied"):
                        elapsed = (datetime.now() - pattern["last_applied"]).total_seconds()
                        if elapsed < 600:
                            continue
                    apply_fix(pattern, reason)
                    _stats["fixes_applied"] += 1

        except Exception as e:
            log(f"Loop error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._json(200, {"status": "ok", "service": "autofix", **_stats})
            elif self.path == "/patterns":
                self._json(200, get_active_patterns())
            else:
                self._json(404, {"error": "not found"})

        def _json(self, code, data):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data, default=str).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Stats API on port {PORT}")

    run_loop()
