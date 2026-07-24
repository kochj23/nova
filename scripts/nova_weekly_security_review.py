#!/usr/bin/env python3
"""nova_weekly_security_review.py — Sunday 08:00 fleet security posture review.

Bundles the actionable half of Nova's 2026-07-14 security-review suggestions:
  * listening-TCP-port audit across the fleet, DIFFED week-over-week (new/removed
    ports are the signal; a snapshot per host lands in security_snapshots),
  * promiscuous-mode check (#5 — flags any NIC in PROMISC),
  * pending OS security updates per Linux host,
  * a Big Brother "close the loop" tally — the noisiest alert types over 7 days, so
    the chronic-but-ignored ones can be pruned (that's the real alert-fatigue fix).
Posts one concise digest to Slack #nova-notifications.

Nova's #3 (thresholds) and #4 (severity routing) already live in nova_bb_escalator,
so this does not re-implement them. Runs on .6 (scheduler host); SSHes to the fleet.
Written by Jordan Koch (via Claude).
"""
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_config  # noqa: E402  (Keychain/env token, portable)

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
SLACK_CHANNEL = "C0ATAF7NZG9"  # #nova-notifications
LOG_DIR = Path.home() / ".openclaw/logs"
BB_LOGS = ["nova-service-monitor.err.log", "nova-system-monitor.err.log", "big-brother.err.log"]

# host -> (label, is_linux). .6 is local (this box).
HOSTS = [
    ("192.168.1.6",   "mac-studio(.6)",  False),
    ("192.168.1.2",   "nova-core(.2)",   True),
    ("192.168.1.86",  "nova-core2(.86)", True),
    ("192.168.1.5",   "nova-core3(.5)",  True),
    ("192.168.1.10",  "nuk(.10)",        True),
]


def run_on(host, cmd, timeout=25):
    """Run cmd on host (local for .6, else ssh). Returns stdout or '' on failure."""
    if host in ("192.168.1.6", "localhost"):
        full = ["bash", "-lc", cmd]
    else:
        full = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", f"kochj@{host}", cmd]
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def listening_ports(host, is_linux):
    """Set of listening TCP ports on host."""
    if is_linux:
        out = run_on(host, "ss -tlnH 2>/dev/null || ss -tln 2>/dev/null")
        ports = set(re.findall(r":(\d+)\s", out))
    else:  # macOS
        out = run_on(host, "lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $9}'")
        ports = set(re.findall(r":(\d+)$", out, re.M))
    return {int(p) for p in ports if p.isdigit()}


def promisc(host, is_linux):
    if is_linux:
        out = run_on(host, "ip -o link show 2>/dev/null | grep -i PROMISC")
        return [l.split(":")[1].strip() for l in out.splitlines() if l.strip()]
    out = run_on(host, "ifconfig 2>/dev/null | grep -iB3 PROMISC | grep flags | cut -d: -f1")
    return [l.strip() for l in out.splitlines() if l.strip()]


def pending_updates(host, is_linux):
    if not is_linux:
        return None
    out = run_on(host, "apt-get -s dist-upgrade 2>/dev/null | grep -c '^Inst'")
    try:
        return int(out.strip() or "0")
    except ValueError:
        return None


def prev_ports(cur, host):
    cur.execute("SELECT ports FROM security_snapshots WHERE host=%s "
                "ORDER BY ts DESC LIMIT 1", (host,))
    row = cur.fetchone()
    return set(row[0]) if row else None


def alert_tally():
    """Top Big Brother alert types in the current (rotated ~weekly) logs — the noise to prune."""
    c = Counter()
    for name in BB_LOGS:
        p = LOG_DIR / name
        if not p.exists():
            continue
        for line in p.read_text(errors="ignore").splitlines():
            if not re.search(r"\bWARN|CRIT|alert|escalat|down|stale\b", line, re.I):
                continue
            # normalize: drop timestamps/pids/numbers so like-alerts group
            key = re.sub(r"\d{4}-\d\d-\d\d[ T][\d:.,]+", "", line)
            key = re.sub(r"\[\w+\]|\bpid \d+|\d+", "", key)
            key = re.sub(r"\s+", " ", key).strip()[:80]
            if key:
                c[key] += 1
    return c.most_common(8)


def post_slack(text):
    token = nova_config.slack_bot_token() if hasattr(nova_config, "slack_bot_token") else nova_config._keychain("nova-slack-bot-token")
    import urllib.request
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": SLACK_CHANNEL, "text": text, "mrkdwn": True}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read()).get("ok", False)
    except Exception as e:
        print("slack post failed:", e); return False


def main():
    dry = "--dry" in sys.argv
    c = psycopg2.connect(OPS_DSN); c.autocommit = True; cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS security_snapshots (
                     host text NOT NULL, ts timestamptz NOT NULL DEFAULT now(),
                     ports jsonb NOT NULL)""")

    port_lines, promisc_hits, update_lines = [], [], []
    for host, label, is_linux in HOSTS:
        cur_ports = listening_ports(host, is_linux)
        if not cur_ports:
            port_lines.append(f"• {label}: _unreachable_")
            continue
        old = prev_ports(cur, host)
        if old is None:
            port_lines.append(f"• {label}: {len(cur_ports)} ports (baseline)")
        else:
            new, gone = sorted(cur_ports - old), sorted(old - cur_ports)
            if new or gone:
                bits = []
                if new:  bits.append("＋" + ",".join(map(str, new)))
                if gone: bits.append("－" + ",".join(map(str, gone)))
                port_lines.append(f"• {label}: {' '.join(bits)}  ⚠️" if new else f"• {label}: {' '.join(bits)}")
            else:
                port_lines.append(f"• {label}: no change ({len(cur_ports)})")
        if not dry:
            cur.execute("INSERT INTO security_snapshots(host, ports) VALUES(%s,%s)",
                        (host, json.dumps(sorted(cur_ports))))
        p = promisc(host, is_linux)
        if p:
            promisc_hits.append(f"• {label}: {', '.join(p)}")
        u = pending_updates(host, is_linux)
        if u:
            update_lines.append(f"• {label}: {u} pending ⚠️" if u else "")

    tally = alert_tally()
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    lines = [f"🔐 *Weekly Security Review — {today}*", "", "*Listening ports (week-over-week):*", *port_lines]
    lines += ["", "*Promiscuous mode:*", *( promisc_hits or ["• none ✅"])]
    lines += ["", "*Pending OS security updates:*", *([l for l in update_lines if l] or ["• all current ✅"])]
    lines += ["", "*Noisiest Big Brother alerts (recent) — prune candidates:*"]
    lines += [f"• `{k}` ×{n}" for k, n in tally] or ["• (no alerts logged)"]
    msg = "\n".join(lines)

    if dry:
        print(msg); return 0
    ok = post_slack(msg)
    print(f"[weekly-security-review] posted={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
