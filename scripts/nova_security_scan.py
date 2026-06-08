#!/usr/bin/env python3
"""
nova_security_scan.py — Security Scan Orchestrator Daemon (#security)

Runs daily fleet-wide security scans (rkhunter, chkrootkit, aide) at 03:00,
stores results in PostgreSQL, and exposes an HTTP API for status/triggering.

HTTP API (port 37474):
    GET  /health   — liveness check
    GET  /status   — fleet security posture summary
    GET  /results  — recent scan results (optional ?host=&limit=)
    POST /scan     — trigger immediate scan (optional JSON body: {"host": "name"})

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras

# ── Configuration ─────────────────────────────────────────────────────────────

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
HTTP_PORT = 37474
SCAN_HOUR = 3  # 3am local time

FLEET = [
    {"name": "mac-studio", "ip": "127.0.0.1", "os": "macos", "local": True},
    {"name": "lts01", "ip": "192.168.1.2", "os": "linux", "user": "kochj"},
    {"name": "nuk", "ip": "192.168.1.10", "os": "linux", "user": "kochj"},
    {"name": "mac-mini", "ip": "192.168.1.190", "os": "macos", "user": "kochj"},
    {"name": "itunes", "ip": "192.168.1.7", "os": "macos", "user": "kochj"},
]

# Tools per OS
TOOLS_MACOS = ["rkhunter"]
TOOLS_LINUX = ["rkhunter", "chkrootkit", "aide"]

# ── State ─────────────────────────────────────────────────────────────────────

_last_scan_time = None
_scan_running = False
_start_time = datetime.now(timezone.utc)


# ── Database ──────────────────────────────────────────────────────────────────

def _conn():
    c = psycopg2.connect(DB_DSN)
    c.autocommit = True
    return c


def store_result(scan_id, host, ip, scan_type, status, findings, raw_output, duration_ms):
    """Store a scan result in PostgreSQL."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO security_scan_results
            (scan_id, host_name, host_ip, scan_type, status, findings, raw_output, duration_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (scan_id, host, ip, scan_type, status,
          json.dumps(findings), raw_output, duration_ms))
    cur.close()
    conn.close()


def post_observation(subject, observation, severity="warning", metadata=None):
    """Post to shared_observations table."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, ("nova", "security", subject, observation, severity,
          json.dumps(metadata or {})))
    cur.close()
    conn.close()


def slack_alert(msg):
    """Send Slack notification + local macOS alert for critical findings."""
    try:
        import nova_config
        nova_config.post_both(msg, nova_config.SLACK_NOTIFY)
        nova_config.notify_local("Security Alert", msg[:200], critical=True)
    except Exception as e:
        print(f"[security_scan] Slack alert failed: {e}", file=sys.stderr)


# ── Command Execution ─────────────────────────────────────────────────────────

def run_local(cmd, timeout=300):
    """Run a command locally (for mac-studio)."""
    try:
        result = subprocess.run(
            cmd, shell=True,
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return f"TIMEOUT: command exceeded {timeout}s", -1
    except Exception as e:
        return f"ERROR: {e}", -1


def run_remote(host, cmd, timeout=300):
    """Run a command on a remote host via SSH."""
    user = host.get("user", "kochj")
    ip = host["ip"]
    ssh_cmd = (
        f"ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "
        f"{user}@{ip} '{cmd}'"
    )
    try:
        result = subprocess.run(
            ssh_cmd, shell=True,
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return f"TIMEOUT: SSH command exceeded {timeout}s", -1
    except Exception as e:
        return f"ERROR: {e}", -1


def run_on_host(host, cmd, timeout=300):
    """Run command on the appropriate host (local or remote)."""
    if host.get("local"):
        return run_local(cmd, timeout)
    else:
        return run_remote(host, cmd, timeout)


# ── Whitelist (known-good warnings to suppress) ──────────────────────────────

RKHUNTER_WHITELIST = [
    "file properties have changed",
    "The command '/usr/bin",
    "Found preloaded shared library",
    "hidden directory found",
    "/dev/.lxc",
    "/dev/.udev",
    "inetd",
    "xinetd",
    "The file of stored",
    "Could not find file",
    "Update check skipped",
    "Checking if SSH",
    "Filesystem checks",
    "Application version",
]

CHKROOTKIT_FALSE_POSITIVES = [
    "not infected",
    "not found",
    "nothing found",
    "not tested",
    "PACKET_SNIFFER",
]


def _is_whitelisted_rkhunter(warning_line):
    """Check if an rkhunter warning is a known false positive."""
    for pattern in RKHUNTER_WHITELIST:
        if pattern.lower() in warning_line.lower():
            return True
    return False


def _is_false_positive_chkrootkit(line):
    """Filter chkrootkit lines that look like INFECTED but aren't real."""
    for fp in CHKROOTKIT_FALSE_POSITIVES:
        if fp.lower() in line.lower():
            return True
    return False


def _get_baseline_warnings(host_name, tool):
    """Get previously accepted warnings from the baseline."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT findings FROM security_scan_results
            WHERE host_name = %s AND scan_type = %s AND baseline_current = true
            ORDER BY scan_time DESC LIMIT 1
        """, (host_name, tool))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            return {f.get("detail", "") for f in data}
    except Exception:
        pass
    return set()


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_rkhunter(output, host_name="unknown"):
    """Parse rkhunter output — binary yes/no verdicts only."""
    all_warnings = []
    real_findings = []
    baseline = _get_baseline_warnings(host_name, "rkhunter")

    for line in output.splitlines():
        stripped = line.strip()
        if "Warning:" in stripped:
            all_warnings.append(stripped)
            if _is_whitelisted_rkhunter(stripped):
                continue
            if stripped in baseline:
                continue
            real_findings.append({"type": "critical", "detail": stripped, "verdict": "FAIL"})

    if not real_findings:
        return "clean", []
    else:
        return "critical", real_findings


def parse_chkrootkit(output, host_name="unknown"):
    """Parse chkrootkit — only flag definitive INFECTED results."""
    findings = []
    for line in output.splitlines():
        if "INFECTED" in line and not _is_false_positive_chkrootkit(line):
            findings.append({"type": "rootkit", "detail": line.strip(), "verdict": "FAIL"})

    if not findings:
        return "clean", []
    else:
        return "critical", findings


def parse_aide(output, host_name="unknown"):
    """Parse aide — only flag changes NOT caused by recent deploys."""
    added = removed = changed = 0
    changed_files = []

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Added:") or stripped.startswith("added:"):
            try:
                added += int(stripped.split(":")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif stripped.startswith("Removed:") or stripped.startswith("removed:"):
            try:
                removed += int(stripped.split(":")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif stripped.startswith("Changed:") or stripped.startswith("changed:"):
            try:
                changed += int(stripped.split(":")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif stripped.startswith("/") and (":" in stripped or " " in stripped):
            changed_files.append(stripped.split()[0] if " " in stripped else stripped.split(":")[0])

    # Check if recent deploy explains the changes
    recent_deploy = _had_recent_deploy(host_name)
    total = added + removed + changed

    if total == 0:
        return "clean", []
    elif recent_deploy:
        return "clean", [{"type": "expected_changes", "detail": f"{total} file changes (post-deploy, expected)", "verdict": "PASS"}]
    else:
        return "critical", [{"type": "unexpected_changes", "added": added, "removed": removed,
                            "changed": changed, "files": changed_files[:20], "verdict": "FAIL"}]


def _had_recent_deploy(host_name):
    """Check if there was a deploy in the last 2 hours that explains file changes."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM deploy_requests
            WHERE status = 'success' AND completed_at > now() - interval '2 hours'
        """)
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count > 0
    except Exception:
        return False


PARSERS = {
    "rkhunter": lambda output, host: parse_rkhunter(output, host),
    "chkrootkit": lambda output, host: parse_chkrootkit(output, host),
    "aide": lambda output, host: parse_aide(output, host),
}

COMMANDS = {
    "rkhunter": "sudo rkhunter --check --skip-keypress --no-colors 2>&1",
    "chkrootkit": "sudo chkrootkit 2>&1",
    "aide": "sudo aide --check --config=/etc/aide/aide.conf 2>&1",
}

TIMEOUTS = {
    "rkhunter": 600,
    "chkrootkit": 300,
    "aide": 600,
}


# ── Scan Logic ────────────────────────────────────────────────────────────────

def scan_host(host, scan_id, tools=None):
    """Run all applicable security tools on a single host."""
    global _scan_running
    results = []

    if host["os"] == "macos":
        applicable = TOOLS_MACOS
    else:
        applicable = TOOLS_LINUX

    if tools:
        applicable = [t for t in applicable if t in tools]

    for tool in applicable:
        cmd = COMMANDS[tool]
        timeout = TIMEOUTS.get(tool, 300)
        start = time.time()
        output, returncode = run_on_host(host, cmd, timeout)
        duration_ms = int((time.time() - start) * 1000)

        # Parse results
        parser = PARSERS[tool]
        if "TIMEOUT" in output or "ERROR" in output:
            status = "error"
            findings = [{"type": "error", "detail": output[:500]}]
        else:
            status, findings = parser(output, host["name"])

        # Store
        store_result(
            scan_id=scan_id,
            host=host["name"],
            ip=host["ip"],
            scan_type=tool,
            status=status,
            findings=findings,
            raw_output=output[:50000],  # cap raw output
            duration_ms=duration_ms,
        )

        # Alert on critical or warning
        if status == "critical":
            msg = f":rotating_light: *CRITICAL* security finding on `{host['name']}` ({tool}): {len(findings)} issue(s)"
            slack_alert(msg)
            post_observation(
                subject=f"security-{host['name']}",
                observation=f"Critical {tool} finding: {json.dumps(findings[:3])}",
                severity="critical",
                metadata={"host": host["name"], "tool": tool, "scan_id": scan_id}
            )
        elif status == "warning":
            post_observation(
                subject=f"security-{host['name']}",
                observation=f"Warning from {tool}: {len(findings)} finding(s)",
                severity="warning",
                metadata={"host": host["name"], "tool": tool, "scan_id": scan_id}
            )

        results.append({
            "host": host["name"],
            "tool": tool,
            "status": status,
            "findings_count": len(findings),
            "duration_ms": duration_ms,
        })

    return results


def run_full_scan(target_host=None):
    """Run scans across the fleet (or a single host)."""
    global _last_scan_time, _scan_running

    if _scan_running:
        return {"error": "Scan already in progress"}

    _scan_running = True
    scan_id = str(uuid.uuid4())[:12]
    all_results = []

    try:
        hosts = FLEET
        if target_host:
            hosts = [h for h in FLEET if h["name"] == target_host]
            if not hosts:
                return {"error": f"Unknown host: {target_host}"}

        print(f"[security_scan] Starting scan {scan_id} for {len(hosts)} host(s)", flush=True)
        for host in hosts:
            try:
                results = scan_host(host, scan_id)
                all_results.extend(results)
            except Exception as e:
                print(f"[security_scan] Error scanning {host['name']}: {e}", file=sys.stderr, flush=True)
                all_results.append({
                    "host": host["name"],
                    "tool": "all",
                    "status": "error",
                    "error": str(e),
                })

        _last_scan_time = datetime.now(timezone.utc)
        print(f"[security_scan] Scan {scan_id} complete: {len(all_results)} results", flush=True)
    finally:
        _scan_running = False

    return {"scan_id": scan_id, "results": all_results}


# ── HTTP API ──────────────────────────────────────────────────────────────────

class ScanHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress default logging
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/health":
            self._json({
                "status": "ok",
                "service": "nova-security-scan",
                "uptime_s": int((datetime.now(timezone.utc) - _start_time).total_seconds()),
                "last_scan": _last_scan_time,
                "scan_running": _scan_running,
            })

        elif path == "/status":
            self._json(get_fleet_status())

        elif path == "/results":
            host = params.get("host", [None])[0]
            limit = int(params.get("limit", ["50"])[0])
            self._json(get_recent_results(host=host, limit=limit))

        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/scan":
            # Read body
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b""
            target_host = None
            if body:
                try:
                    data = json.loads(body)
                    target_host = data.get("host")
                except json.JSONDecodeError:
                    pass

            # Run scan in background thread
            def _scan():
                run_full_scan(target_host=target_host)

            thread = threading.Thread(target=_scan, daemon=True)
            thread.start()
            self._json({"status": "scan_started", "target": target_host or "all"})
        else:
            self._json({"error": "Not found"}, 404)


def get_fleet_status():
    """Get a summary of fleet security posture."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Latest scan per host+tool
    cur.execute("""
        SELECT DISTINCT ON (host_name, scan_type)
            host_name, scan_type, status, scan_time, findings
        FROM security_scan_results
        ORDER BY host_name, scan_type, scan_time DESC
    """)
    latest = cur.fetchall()

    # Summary counts
    cur.execute("""
        SELECT status, COUNT(*) as cnt
        FROM (
            SELECT DISTINCT ON (host_name, scan_type)
                host_name, scan_type, status
            FROM security_scan_results
            ORDER BY host_name, scan_type, scan_time DESC
        ) sub
        GROUP BY status
    """)
    summary = {row["status"]: row["cnt"] for row in cur.fetchall()}

    cur.close()
    conn.close()

    return {
        "summary": summary,
        "last_scan": _last_scan_time,
        "scan_running": _scan_running,
        "fleet_size": len(FLEET),
        "latest_per_host": [dict(r) for r in latest],
    }


def get_recent_results(host=None, limit=50):
    """Get recent scan results."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if host:
        cur.execute("""
            SELECT id, scan_id, host_name, host_ip, scan_type, scan_time, status, findings, duration_ms
            FROM security_scan_results
            WHERE host_name = %s
            ORDER BY scan_time DESC
            LIMIT %s
        """, (host, limit))
    else:
        cur.execute("""
            SELECT id, scan_id, host_name, host_ip, scan_type, scan_time, status, findings, duration_ms
            FROM security_scan_results
            ORDER BY scan_time DESC
            LIMIT %s
        """, (limit,))

    results = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"results": results, "count": len(results)}


# ── Scheduler ─────────────────────────────────────────────────────────────────

def scheduler_loop():
    """Main loop: check if it's 3am and run daily scan."""
    global _last_scan_time
    last_scan_date = None

    while True:
        now = datetime.now()
        today = now.date()

        # Run at SCAN_HOUR if not already run today
        if now.hour == SCAN_HOUR and last_scan_date != today and not _scan_running:
            print(f"[security_scan] Triggering scheduled daily scan at {now}", flush=True)
            run_full_scan()
            last_scan_date = today

        time.sleep(60)  # Check every minute


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[security_scan] Starting Nova Security Scan Orchestrator on port {HTTP_PORT}", flush=True)
    print(f"[security_scan] Fleet: {[h['name'] for h in FLEET]}", flush=True)
    print(f"[security_scan] Scheduled daily scan at {SCAN_HOUR:02d}:00", flush=True)

    # Start scheduler in background
    sched_thread = threading.Thread(target=scheduler_loop, daemon=True)
    sched_thread.start()

    # Start HTTP server (blocks)
    server = HTTPServer(("127.0.0.1", HTTP_PORT), ScanHandler)
    print(f"[security_scan] HTTP API listening on http://127.0.0.1:{HTTP_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[security_scan] Shutting down.", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
