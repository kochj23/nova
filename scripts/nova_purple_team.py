#!/usr/bin/env python3
"""
nova_purple_team.py — detection-validation harness ("does the SIEM actually catch it?").

Jordan's steer (2026-07-30): more scanners is not the gap — validating that our
detections FIRE is. This fires a small catalog of known attacker signatures at the
Nova syslog server and then checks telemetry.events to confirm the matching
detection landed within a window, scoring caught/missed into a coverage scorecard.

WHAT IT TESTS. The Nova detection pipeline: syslog line -> nova_syslog_server
detect_threat() -> notify(category=<threat>) -> telemetry.events. Each technique
sends realistic RFC3164 syslog matching a real detector (auth brute force,
sensitive-path access, suspicious-TLD DNS) from an obviously-synthetic source, then
scores whether the expected category appears in telemetry.events after the fire.

WHAT IT DOES NOT TEST. It validates DETECTION (parse -> alert -> record), not full
end-to-end attack execution (it does not actually brute-force SSH). That is the
honest scope: it answers "if this signature appears in the logs, does the SIEM
catch it?" — which is exactly the coverage question. Fired traffic uses TEST-NET
source IPs (RFC 5737) and the synthetic hostname below so nothing implicates a real
host and every test event is attributable.

SAFETY. Runs inside a nova_maintenance window so the (correctly-firing) detections
don't page during the test — the event rows still land in telemetry.events, which
is what we score. Read-only against real infra otherwise.

    nova_purple_team.py --list
    nova_purple_team.py --run [--technique auth_brute_force] [--target 127.0.0.1]
    nova_purple_team.py --run --no-maintenance     # let it page (not recommended)

Written by Jordan Koch (via Claude).
"""
import argparse
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
LOG_FILE = Path.home() / ".openclaw/logs/purple_team.log"

DEFAULT_TARGET = "127.0.0.1"   # the nova_syslog_server UDP listener (.6)
SYSLOG_PORT = 1514
SIM_HOST = "PURPLE-TEAM-SIM"   # synthetic hostname stamped into every fired log line
TESTNET_IP = "203.0.113.66"    # RFC 5737 TEST-NET-3 — never a real host


def log(msg):
    line = f"[purple {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _syslog(target, msg, facility_severity=38, tag="sshd", pid=6613):
    """Send one RFC3164 syslog line to the detector."""
    ts = datetime.now().strftime("%b %e %H:%M:%S")
    line = f"<{facility_severity}>{ts} {SIM_HOST} {tag}[{pid}]: {msg}"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(line.encode(), (target, SYSLOG_PORT))
    finally:
        s.close()


# ── Technique catalog ────────────────────────────────────────────────────────
# Each fire() sends signatures that a real detector in nova_syslog_server keys on.

def _fire_auth_brute_force(target):
    # detect_threat AUTH_PATTERNS + brute threshold (>=5 in 300s from one src).
    for i in range(7):
        _syslog(target, f"Failed password for root from {TESTNET_IP} port 22 ssh2",
                tag="sshd", pid=6613 + i)
        time.sleep(0.15)


def _fire_sensitive_path(target):
    # SENSITIVE_PATHS_RE + threshold (>=3 in 300s per host).
    for path in ("/etc/shadow", "/etc/shadow", ".ssh/id_rsa", "/private/etc/sudoers"):
        _syslog(target, f"audit: process cat attempted read of {path}",
                tag="kernel", pid=0)
        time.sleep(0.15)


def _fire_suspicious_dns(target):
    # "query"/"dns" in msg + a SUSPICIOUS_TLD (.xyz).
    _syslog(target, "query: beacon-c2-check.xyz IN A + (203.0.113.66)",
            tag="named", pid=910, facility_severity=53)


def _fire_off_hours_auth(target):
    # Only fires when the detector's local clock is 01:00-05:00.
    _syslog(target, f"Failed password for admin from {TESTNET_IP} port 22 ssh2",
            tag="sshd", pid=7001)


CATALOG = [
    {
        "id": "auth_brute_force", "attack": "T1110 Brute Force",
        "desc": "7 failed SSH logins from one source (>threshold) — expect brute-force detection",
        "fire": _fire_auth_brute_force, "category": "auth_failure", "window_s": 90,
    },
    {
        "id": "sensitive_path", "attack": "T1552.001 Credentials in Files",
        "desc": "Repeated reads of /etc/shadow, id_rsa, sudoers — expect sensitive-path detection",
        "fire": _fire_sensitive_path, "category": "sensitive_access", "window_s": 90,
    },
    {
        "id": "suspicious_dns", "attack": "T1071.004 DNS / C2",
        "desc": "DNS query to a suspicious-TLD C2 domain (.xyz) — expect suspicious-DNS detection",
        "fire": _fire_suspicious_dns, "category": "suspicious_dns", "window_s": 90,
    },
    {
        "id": "off_hours_auth", "attack": "T1078 Valid Accounts (off-hours)",
        "desc": "Auth activity — detector only fires 01:00-05:00 local (time-gated)",
        "fire": _fire_off_hours_auth, "category": "off_hours_auth", "window_s": 90,
        "time_gated": lambda: 1 <= datetime.now().hour <= 5,
    },
]


def _conn():
    return psycopg2.connect(DSN, connect_timeout=5,
                            cursor_factory=psycopg2.extras.RealDictCursor)


def _detected_since(conn, category, since_epoch):
    """Return the first matching detection event after `since_epoch`, or None.
    Matches the SIM host so we never credit a real, unrelated detection."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, ts, title, meta FROM telemetry.events "
            "WHERE category = %s AND ts > to_timestamp(%s) "
            "AND (meta::text ILIKE %s OR title ILIKE %s OR body ILIKE %s) "
            "ORDER BY ts ASC LIMIT 1",
            (category, since_epoch, f"%{SIM_HOST}%", f"%{SIM_HOST}%", f"%{TESTNET_IP}%"))
        return cur.fetchone()


def run(techniques, target, use_maintenance=True):
    try:
        import nova_maintenance
    except Exception:
        nova_maintenance = None

    if use_maintenance and nova_maintenance:
        nova_maintenance.start(10, "purple-team detection validation")
        log("maintenance window opened (detections recorded, Slack muted)")

    results = []
    conn = _conn()
    try:
        for t in techniques:
            gate = t.get("time_gated")
            if gate and not gate():
                results.append({**t, "outcome": "skipped",
                                "detail": "detector is time-gated and not in its active window now"})
                log(f"SKIP {t['id']}: time-gated")
                continue
            t0 = time.time()
            log(f"FIRE {t['id']} ({t['attack']}) -> expect category '{t['category']}'")
            try:
                t["fire"](target)
            except Exception as e:
                results.append({**t, "outcome": "error", "detail": f"fire failed: {e}"})
                continue
            hit, deadline = None, t0 + t["window_s"]
            while time.time() < deadline:
                hit = _detected_since(conn, t["category"], t0)
                if hit:
                    break
                time.sleep(3)
            if hit:
                latency = (hit["ts"].timestamp() - t0)
                results.append({**t, "outcome": "CAUGHT", "latency_s": round(latency, 1),
                                "detail": f"event #{hit['id']} '{hit['title'][:60]}'"})
                log(f"  CAUGHT in {latency:.1f}s (event #{hit['id']})")
            else:
                results.append({**t, "outcome": "MISSED",
                                "detail": f"no '{t['category']}' event within {t['window_s']}s"})
                log(f"  MISSED (no detection within {t['window_s']}s)")
    finally:
        conn.close()
        if use_maintenance and nova_maintenance:
            nova_maintenance.stop()
            log("maintenance window closed")

    _scorecard(results)
    return results


def _scorecard(results):
    scored = [r for r in results if r["outcome"] in ("CAUGHT", "MISSED")]
    caught = [r for r in scored if r["outcome"] == "CAUGHT"]
    lines = [f"Detection coverage: {len(caught)}/{len(scored)} techniques caught"
             + (f", {len(results) - len(scored)} skipped/errored" if len(results) != len(scored) else "")]
    for r in results:
        mark = {"CAUGHT": "✅", "MISSED": "❌", "skipped": "⏭️", "error": "⚠️"}.get(r["outcome"], "?")
        lat = f" ({r['latency_s']}s)" if r.get("latency_s") is not None else ""
        lines.append(f"  {mark} {r['id']:16} [{r['attack']}]{lat} — {r.get('detail','')}")
    body = "\n".join(lines)
    print("\n" + body)
    # A MISS is the actionable signal — page it; an all-caught run is FYI.
    level = "warning" if len(caught) < len(scored) else "info"
    try:
        notify("Purple-Team Detection Scorecard",
               body=body, level=level, category="security",
               source="nova_purple_team.py", dedup_key="purple-team-scorecard",
               meta={"caught": len(caught), "scored": len(scored)})
    except Exception as e:
        log(f"notify failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="show the technique catalog")
    ap.add_argument("--run", action="store_true", help="fire techniques and score detections")
    ap.add_argument("--technique", help="run only this technique id")
    ap.add_argument("--target", default=DEFAULT_TARGET, help="syslog server IP (default 127.0.0.1)")
    ap.add_argument("--no-maintenance", action="store_true",
                    help="do NOT open a maintenance window (detections will page)")
    a = ap.parse_args()

    if a.list or not (a.run):
        print("Purple-team technique catalog:")
        for t in CATALOG:
            print(f"  {t['id']:16} {t['attack']:34} -> expect '{t['category']}'")
            print(f"      {t['desc']}")
        return 0

    techs = CATALOG
    if a.technique:
        techs = [t for t in CATALOG if t["id"] == a.technique]
        if not techs:
            print(f"unknown technique: {a.technique}", file=sys.stderr)
            return 2
    run(techs, a.target, use_maintenance=not a.no_maintenance)
    return 0


if __name__ == "__main__":
    sys.exit(main())
