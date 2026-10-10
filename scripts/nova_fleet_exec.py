#!/usr/bin/env python3
"""
nova_fleet_exec.py — platform-aware service restarts (six-month build #5, 2026-09-28).

Nova's first four autonomous acts (2026-09-16) failed identically: the actor ran on a
Linux box and reached for `launchctl`, the Mac's service manager. The follow-up fix
SSHed to the Mac instead — from a host that has no key there. This module is the one
place that knows which node speaks launchd and which speaks systemd, whether we are
standing on that node or not, and how to reach it if not.

    restart_service(node, svc) -> (ok: bool, detail: str)

node: a node_status.node_name ("mac-studio", "nova-core4"), a fleet alias ("nova-core8",
"Office-M4-2") or an IP. svc: the short service name ("nova-battery-monitor"); on macOS
it becomes the launchd label net.digitalnoise.<svc>, on Linux the systemd unit <svc>.

Remote macOS restarts go through a forced-command SSH key: the Mac's authorized_keys pins
the key to ~/bin/nova-restart-gate.sh, which allows only `restart <svc>` for allowlisted
labels. So even a fully compromised nova-core can, at most, kick a monitor job.
Linux remotes use the normal fleet key + `sudo -n systemctl restart`.
# ponytail: no docker dispatch yet — add a 'docker' os_family when a container joins SAFE_SERVICES
"""
import os
import shutil
import socket
import subprocess
import sys

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
RESTART_KEY = os.path.expanduser("~/.ssh/nova_restart")       # forced-command key (Linux -> Mac)
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]

# fallback when node_status is unreachable or the name is an alias it does not know
_STATIC = {
    "mac-studio": ("192.168.1.6", "macos"), "office-m4-2": ("192.168.1.6", "macos"),
    "nova-core8": ("192.168.1.6", "macos"), "192.168.1.6": ("192.168.1.6", "macos"),
    "tv-movies-mini": ("192.168.1.7", "macos"), "nova-core9": ("192.168.1.7", "macos"),
    "nova-core6": ("192.168.1.252", "macos"), "nova-core10": ("192.168.1.77", "macos"),
    "jordans-mac-mini": ("192.168.1.77", "macos"),
    "nova-core": ("192.168.1.2", "linux"), "nova-core1": ("192.168.1.2", "linux"),
    "nova-core2": ("192.168.1.86", "linux"), "nova-core3": ("192.168.1.5", "linux"),
    "nova-core4": ("192.168.1.250", "linux"), "nova-core5": ("192.168.1.10", "linux"),
    "nuk": ("192.168.1.10", "linux"), "nova-core7": ("192.168.1.125", "linux"),
}


def resolve(node):
    """-> (ip, os_family). node_status first, static map second, 'linux' if all else fails."""
    key = (node or "").strip().lower()
    try:
        import psycopg2
        with psycopg2.connect(OPS_DSN, connect_timeout=3) as c, c.cursor() as cur:
            cur.execute("SELECT host(node_ip), os_family FROM node_status "
                        "WHERE lower(node_name)=%s OR host(node_ip)=%s LIMIT 1", (key, key))
            row = cur.fetchone()
            if row and row[0] and row[1]:
                return row[0], row[1]
    except Exception:  # noqa: BLE001
        pass
    if key in _STATIC:
        return _STATIC[key]
    if key.count(".") == 3:
        return key, "macos" if key in {v[0] for v in _STATIC.values() if v[1] == "macos"} else "linux"
    return key, "linux"


def _local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0])
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
        ips.update(out.split())
    except Exception:  # noqa: BLE001
        pass
    try:  # macOS has no `hostname -I`
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        ips.update(w.split()[1] for w in out.splitlines() if w.strip().startswith("inet "))
    except Exception:  # noqa: BLE001
        pass
    return ips


def is_local(ip):
    return ip in _local_ips()


def plan(node, svc, local=None):
    """Pure: decide the argv for (node, svc). local=None probes the host; tests pass a bool."""
    ip, fam = resolve(node)
    here = is_local(ip) if local is None else local
    if fam == "macos":
        label = f"net.digitalnoise.{svc}"
        if here:
            return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]
        return ["ssh", "-i", RESTART_KEY, *SSH_OPTS, f"kochj@{ip}", f"restart {svc}"]
    if here:
        return ["sudo", "-n", "systemctl", "restart", svc]
    return ["ssh", *SSH_OPTS, f"kochj@{ip}", f"sudo -n systemctl restart {svc}"]


def restart_service(node, svc):
    """(ok, detail). Never raises."""
    if not svc or not all(c.isalnum() or c in "-_." for c in svc):
        return False, f"refusing malformed service name {svc!r}"
    try:
        argv = plan(node, svc)
        if argv[0] == "launchctl" and not shutil.which("launchctl"):
            return False, "planned a local launchctl on a host without launchd (node resolution bug)"
        r = subprocess.run(argv, capture_output=True, text=True, timeout=45)
        detail = (r.stderr or r.stdout or "").strip()[:200]
        return r.returncode == 0, detail or ("ok" if r.returncode == 0 else f"rc={r.returncode}")
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def demo():
    p = plan("mac-studio", "nova-battery-monitor", local=True)
    assert p[0] == "launchctl" and p[-1].endswith("net.digitalnoise.nova-battery-monitor"), p
    p = plan("mac-studio", "nova-battery-monitor", local=False)
    assert p[0] == "ssh" and "-i" in p and p[-1] == "restart nova-battery-monitor" and "kochj@192.168.1.6" in p, p
    p = plan("nova-core4", "nova-hue", local=False)
    assert p[0] == "ssh" and p[-1] == "sudo -n systemctl restart nova-hue" and "kochj@192.168.1.250" in p, p
    p = plan("192.168.1.250", "nova-hue", local=True)
    assert p[:3] == ["sudo", "-n", "systemctl"], p
    assert resolve("Office-M4-2")[1] == "macos" and resolve("nuk")[1] == "linux"
    ok, why = restart_service("mac-studio", "bad;name")
    assert not ok and "malformed" in why
    print("all fleet-exec assertions passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        demo()
    elif len(sys.argv) == 3:
        ok, detail = restart_service(sys.argv[1], sys.argv[2])
        print(("ok " if ok else "FAILED ") + detail); sys.exit(0 if ok else 1)
    else:
        print("usage: nova_fleet_exec.py <node> <service> | --selftest")
