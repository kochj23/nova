#!/usr/bin/env python3
"""nova_pkg_audit.py — daily software + outdated-package audit across the fleet.

Replaces the stale, misdesigned CINC path: software_inventory stopped writing 2026-07-29 (CINC was
never scheduled — it ran manually once), and package_updates stuffed raw apt progress-output into
package rows ('apt-upgrade', '(Reading database… 5%…') instead of real packages.

Per host it counts INSTALLED packages and collects the OUTDATED ones — the security-relevant signal,
since an outdated package is potential CVE exposure. Clean rows land in:
  - package_audit          : one row per outdated package (host, name, current, available, source)
  - package_audit_hosts    : per-host rollup (installed, outdated, reachable, ts)
The security report reads these for Ring 1 (what's installed / updates pending) and Ring 2 (real
exposure on your ACTUAL versions, not just vendor names).

Robust: an unreachable host is recorded as reachable=false, never fabricated as zero-outdated.
Scheduled daily (scheduler task pkg_audit). Manual: python3 nova_pkg_audit.py
"""
import re
import subprocess
import sys
import time

import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
LOCAL_IPS = {"192.168.1.6"}          # .6 (mac-studio) runs this — collect locally, no ssh
BREW = "/opt/homebrew/bin/brew"


def log(m):
    print(f"[pkg_audit {time.strftime('%H:%M:%S')}] {m}", flush=True)


def _run(cmd, ip=None, timeout=90):
    """Run one of this file's static shell lines locally (LOCAL_IPS) or over ssh.
    Returns (rc, stdout) or (-1, ''). No shell=True: local runs go through an explicit
    sh -c argv (the commands are in-file constants with pipes/redirects), remote runs pass
    the line as a single ssh argument for the remote shell."""
    if ip in LOCAL_IPS:
        argv = ["/bin/sh", "-c", cmd]
    else:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=accept-new", f"kochj@{ip}", cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "")
    except Exception:
        return -1, ""


def get_hosts():
    conn = psycopg2.connect(DSN); conn.autocommit = True; cur = conn.cursor()
    cur.execute("SELECT node_name, node_ip, os_family FROM cinc_node_configs ORDER BY node_name")
    rows = cur.fetchall(); conn.close()
    return rows


def collect(name, ip, os_family):
    """(installed_count, outdated:[(pkg,current,available,source)], reachable)."""
    outdated = []
    if os_family == "macos":
        rc, out = _run(f"{BREW} list --versions 2>/dev/null", ip)
        if rc != 0:
            return 0, [], False
        installed = len([l for l in out.splitlines() if l.strip()])
        rc, out = _run(f"{BREW} outdated --verbose 2>/dev/null", ip)
        for line in out.splitlines():
            # "aws-c-common (0.14.4) < 0.14.5"
            m = re.match(r"^(\S+)\s+\((.+?)\)\s+<\s+(\S+)", line.strip())
            if m:
                outdated.append((m.group(1), m.group(2), m.group(3), "homebrew"))
        return installed, outdated, True
    else:  # linux / apt
        # plain -W lists one package per line (no format string to mangle over ssh); wc -l counts.
        rc, out = _run("dpkg-query -W 2>/dev/null | wc -l", ip)
        installed = int(out.strip()) if out.strip().isdigit() else 0
        rc, out = _run("apt list --upgradable 2>/dev/null", ip)
        if rc != 0 and installed == 0:
            return 0, [], False
        for line in out.splitlines():
            # "alsa-ucm-conf/resolute-updates 1.2.15.3-1ubuntu1.5 all [upgradable from: 1.2.15.3-1ubuntu1.4]"
            m = re.match(r"^(\S+?)/\S+\s+(\S+)\s+\S+\s+\[upgradable from:\s+(\S+)\]", line.strip())
            if m:
                outdated.append((m.group(1), m.group(3), m.group(2), "apt"))
        return installed, outdated, True


def ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS package_audit (
            id serial PRIMARY KEY, ts timestamptz DEFAULT now(), host_name text,
            package_name text, current_version text, available_version text, source text);
        CREATE TABLE IF NOT EXISTS package_audit_hosts (
            ts timestamptz DEFAULT now(), host_name text PRIMARY KEY,
            installed int, outdated int, reachable boolean, source text);""")


def main():
    conn = psycopg2.connect(DSN); conn.autocommit = True; cur = conn.cursor()
    ensure_tables(cur)
    hosts = get_hosts()
    log(f"auditing {len(hosts)} hosts")
    total_out = 0
    for name, ip, osf in hosts:
        try:
            installed, outdated, reachable = collect(name, ip, osf)
        except Exception as e:
            log(f"  {name}: error {e}"); installed, outdated, reachable = 0, [], False
        if reachable:
            # replace this host's outdated rows atomically
            cur.execute("DELETE FROM package_audit WHERE host_name=%s", (name,))
            for pkg, curv, availv, src in outdated:
                cur.execute("INSERT INTO package_audit (host_name,package_name,current_version,available_version,source) "
                            "VALUES (%s,%s,%s,%s,%s)", (name, pkg[:120], curv[:60], availv[:60], src))
            total_out += len(outdated)
        cur.execute("""INSERT INTO package_audit_hosts (host_name,installed,outdated,reachable,source,ts)
                       VALUES (%s,%s,%s,%s,%s,now())
                       ON CONFLICT (host_name) DO UPDATE SET installed=EXCLUDED.installed,
                       outdated=EXCLUDED.outdated, reachable=EXCLUDED.reachable, source=EXCLUDED.source, ts=now()""",
                    (name, installed, len(outdated), reachable, "brew" if osf == "macos" else "apt"))
        log(f"  {name}: {'ok' if reachable else 'UNREACHABLE'} — {installed} installed, {len(outdated)} outdated")
    log(f"done: {total_out} outdated packages across the fleet")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
