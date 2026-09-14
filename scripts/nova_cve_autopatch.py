#!/usr/bin/env python3
"""nova_cve_autopatch.py — weekly CVE auto-patch pass for the Linux fleet.

Wazuh already SCANS for CVEs and files "SECURITY: L13 alert on <host> — <CVE-ID>
affects <package>" tickets into claude_queue (that's where every CVE ticket this
session came from). What's missing is the PATCHING half — this closes that gap:

  1. Pull open L13 tickets, group by (host, package).
  2. For each: apt update, check if a fix is actually available yet (Wazuh flags
     the CVE the moment it's *known*, which can be before Ubuntu ships a fix —
     verified this the hard way tonight patching nova-core/nova-core3 by hand).
  3. Userspace packages: auto-upgrade (`apt-get install --only-upgrade -y`), verify,
     resolve the ticket(s).
  4. Kernel packages (linux-image/linux-generic/linux-headers): install the fix but
     do NOT auto-reboot unattended — a weekly cron job silently rebooting production
     hosts is a different risk profile than a supervised manual patch pass. Resolve
     the ticket with a clear "reboot pending" outcome instead.
  5. Post a summary report to Slack (#nova-info).

Scheduled weekly. Linux fleet only (nova-core through nova-core5) — that's where
Wazuh's L13 tickets have come from; macOS coverage can be added later if needed.
"""
import re
import subprocess
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

HOSTS = {
    "nova-core": "192.168.1.2", "nova-core2": "192.168.1.86",
    "nova-core3": "192.168.1.5", "nova-core4": "192.168.1.250",
    "nova-core5": "192.168.1.10",
}
KERNEL_PREFIXES = ("linux-image", "linux-generic", "linux-headers", "linux-modules")
TICKET_RE = re.compile(r"L13 alert on (\S+) — (CVE-\S+) affects (\S+)")


def log(m):
    print(f"[cve-autopatch] {m}", flush=True)


def ssh(host_ip, cmd, timeout=120):
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", f"kochj@{host_ip}", cmd],
        capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def main():
    conn = psycopg2.connect(DSN); conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT id, description FROM claude_queue "
                "WHERE status='queued' AND description LIKE 'SECURITY: L13 alert%'")
    rows = cur.fetchall()

    # Group ticket ids by (host, package)
    groups = {}
    for tid, desc in rows:
        m = TICKET_RE.search(desc)
        if not m:
            continue
        host, cve, pkg = m.groups()
        groups.setdefault((host, pkg), {"cves": [], "ticket_ids": []})
        groups[(host, pkg)]["cves"].append(cve)
        groups[(host, pkg)]["ticket_ids"].append(tid)

    if not groups:
        log("no open L13 tickets — nothing to do")
        cur.close(); conn.close()
        return

    patched, reboot_pending, no_fix_yet, failed = [], [], [], []

    for (host, pkg), info in groups.items():
        ip = HOSTS.get(host)
        if not ip:
            log(f"unknown host {host} — skipping ({pkg})")
            continue
        rc, _, err = ssh(ip, "sudo apt-get update -qq")
        if rc != 0:
            log(f"{host}: apt update failed: {err[:200]}")
            failed.append((host, pkg, "apt update failed"))
            continue

        rc, out, _ = ssh(ip, f"apt list --upgradable 2>/dev/null | grep -F '{pkg}/'")
        if not out.strip():
            log(f"{host}/{pkg}: no fix available yet from Ubuntu")
            no_fix_yet.append((host, pkg, info["cves"]))
            continue

        is_kernel = any(pkg.startswith(p) for p in KERNEL_PREFIXES)
        if is_kernel:
            rc, _, err = ssh(ip, f"sudo apt-get install --only-upgrade -y {pkg}", timeout=300)
            if rc == 0:
                log(f"{host}/{pkg}: kernel package installed, reboot pending")
                reboot_pending.append((host, pkg, info["cves"]))
                cur.execute(
                    "UPDATE claude_queue SET status='resolved', completed_at=now(), "
                    "outcome=%s WHERE id = ANY(%s)",
                    (f"Auto-patched {pkg} via nova_cve_autopatch.py — REBOOT PENDING to activate "
                     f"(kernel updates are never auto-rebooted unattended). CVEs: {', '.join(info['cves'])}",
                     info["ticket_ids"]))
            else:
                failed.append((host, pkg, err[:200]))
            continue

        rc, _, err = ssh(ip, f"sudo apt-get install --only-upgrade -y {pkg}", timeout=180)
        if rc == 0:
            log(f"{host}/{pkg}: patched")
            patched.append((host, pkg, info["cves"]))
            cur.execute(
                "UPDATE claude_queue SET status='resolved', completed_at=now(), "
                "outcome=%s WHERE id = ANY(%s)",
                (f"Auto-patched via nova_cve_autopatch.py. CVEs: {', '.join(info['cves'])}",
                 info["ticket_ids"]))
        else:
            failed.append((host, pkg, err[:200]))

    lines = [f":shield: *Weekly CVE auto-patch report* — {len(groups)} package(s) reviewed\n"]
    if patched:
        lines.append(f"*Patched* ({len(patched)}): " + ", ".join(f"{h}/{p}" for h, p, _ in patched))
    if reboot_pending:
        lines.append(f"*Kernel patched, REBOOT PENDING* ({len(reboot_pending)}): "
                      + ", ".join(f"{h}/{p}" for h, p, _ in reboot_pending))
    if no_fix_yet:
        lines.append(f"*No fix available yet from Ubuntu* ({len(no_fix_yet)}): "
                      + ", ".join(f"{h}/{p}" for h, p, _ in no_fix_yet))
    if failed:
        lines.append(f"*Failed* ({len(failed)}): " + ", ".join(f"{h}/{p}: {e}" for h, p, e in failed))

    nova_config.post_both("\n".join(lines), slack_channel=nova_config.SLACK_NOTIFY)
    log(f"done: {len(patched)} patched, {len(reboot_pending)} reboot-pending, "
        f"{len(no_fix_yet)} no-fix-yet, {len(failed)} failed")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
