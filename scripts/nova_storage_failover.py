#!/usr/bin/env python3
"""nova_storage_failover.py — keep /nova alive, on whichever storage is actually up.

Written 2026-07-27, the day the Synology died mid-morning and the only symptom
anyone noticed was a missing local newspaper article.

WHAT IT DOES, in Jordan's words:
  "If the Synology is not mounted, mount it. If the Synology is down, fail over
   until it comes back. When you fail over, get the latest scripts from GitHub."

WHY IT IS NOT JUST `mount -a`:
  The old watchdog asked "is /nova in the mount table?" and got back YES while the
  mount was a corpse — the CIFS connection was dead and every read returned
  EHOSTDOWN. A mount table entry is the filesystem reporting on itself. So this
  checks the only thing that matters: can I READ A REAL FILE. (Minimum grain —
  see runbook-witness-proven-red.)

DRIFT:
  The share is a distribution artifact, not a source of truth. GitHub is the source
  of truth. So after any failover the scripts are refreshed FROM GIT rather than
  from whatever stale copy the failover target happened to have — which is how the
  UNAS ended up ten days behind without anyone noticing.

Runs from a systemd timer on .2/.10/.86. Idempotent; a no-op when healthy.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

MOUNT = "/nova"
CANARY = "scripts"                       # must exist and be non-empty on a good mount
# SSH + a per-node read-only DEPLOY KEY (fleet rule: SSH URLs, never tokens in files).
# The "github-nova" alias is an ~/.ssh/config Host entry owned by GIT_USER — root has
# no GitHub key and must not have one, so all git runs drop to that user.
REPO = "github-nova:kochj23/nova.git"
GIT_USER = "kochj"
GIT_CACHE = Path("/home/kochj/.cache/nova-storage-repo")
REFRESH_MARKER = Path(MOUNT) / ".last-git-refresh"
REFRESH_MIN_AGE = 900                    # don't let N nodes all refresh at once

# Preference order. First reachable wins; the script fails BACK automatically.
# UNAS-primary cutover 2026-09-10: UNAS is now the primary store for /nova, served
# from //192.168.1.69/nas/nova-fs (a dedicated faithful replica of the Synology's
# /volume1/nova — the old //.69/nas/nova path held unrelated data). Synology is the
# read-back-first fallback. Flip the two list entries to restore Synology-primary.
TARGETS = [
    {"name": "unas", "host": "192.168.1.69", "unc": "//192.168.1.69/nas/nova-fs",
     "creds": "/etc/cifs-unas.creds", "primary": True},
    {"name": "synology", "host": "192.168.1.11", "unc": "//192.168.1.11/nova",
     "creds": "/etc/cifs-nas.creds", "primary": False},
]
MOUNT_OPTS = "uid=kochj,gid=kochj,iocharset=utf8,vers=3.0,_netdev"


def log(msg):
    print(f"[storage-failover] {msg}", flush=True)


def _asuser(cmd):
    """Run as GIT_USER when we are root — the deploy key lives in their ~/.ssh."""
    if os.geteuid() == 0:
        return ["sudo", "-n", "-u", GIT_USER, "-H"] + cmd
    return cmd


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def reachable(host, port=445, timeout=4):
    """Is the SMB server answering? Cheap TCP probe, short timeout."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def mount_healthy():
    """MINIMUM GRAIN: not 'is it mounted' but 'can I read real content'.

    A stale CIFS mount stays in the mount table and fails every read with
    EHOSTDOWN — that is the exact failure that took the fleet down today, and
    the exact failure the mount table cannot see.
    """
    probe = Path(MOUNT) / CANARY
    try:
        return bool(os.listdir(probe))
    except OSError:
        return False


def current_source():
    """Which UNC currently backs /nova, per the kernel."""
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) > 1 and parts[1] == MOUNT:
                return parts[0]
    except OSError:
        pass
    return None


def clear_mount():
    """A dead CIFS mount will not umount cleanly — lazy is the only thing that works."""
    run(["sudo", "-n", "umount", "-f", MOUNT], timeout=15)
    run(["sudo", "-n", "umount", "-l", MOUNT], timeout=15)


def do_mount(target):
    run(["sudo", "-n", "mkdir", "-p", MOUNT], timeout=10)
    r = run(["sudo", "-n", "mount", "-t", "cifs", target["unc"], MOUNT,
             "-o", f"credentials={target['creds']},{MOUNT_OPTS}"], timeout=45)
    if r.returncode != 0:
        log(f"mount {target['name']} FAILED: {(r.stderr or '').strip()[:160]}")
        return False
    return mount_healthy()


def refresh_scripts_from_git():
    """Pull the scripts from GitHub onto whatever we just mounted.

    This is the anti-drift step. The failover target is NOT trusted to hold a
    current copy — the UNAS was ten days stale when we needed it. Git is the
    source of truth; the share is just where nodes read it from.
    """
    # Don't stampede: several nodes run this timer.
    try:
        if REFRESH_MARKER.exists() and (time.time() - REFRESH_MARKER.stat().st_mtime) < REFRESH_MIN_AGE:
            log("scripts refreshed recently by another node — skipping")
            return True
    except OSError:
        pass

    GIT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if (GIT_CACHE / ".git").exists():
        r = run(_asuser(["git", "-C", str(GIT_CACHE), "fetch", "--depth", "1", "origin", "main"]), timeout=180)
        if r.returncode == 0:
            r = run(_asuser(["git", "-C", str(GIT_CACHE), "reset", "--hard", "origin/main"]), timeout=60)
    else:
        r = run(_asuser(["git", "clone", "--depth", "1", REPO, str(GIT_CACHE)]), timeout=300)
    if r.returncode != 0:
        log(f"git refresh FAILED: {(r.stderr or '')[:160]} — leaving existing scripts in place")
        return False

    src = GIT_CACHE / "scripts"
    if not src.is_dir() or not any(src.iterdir()):
        log("git checkout has no scripts/ — refusing to sync an empty tree over a live share")
        return False

    # -L dereferences symlinks into real files: SMB/CIFS cannot create symlinks at all
    # (rsync dies with "Operation not supported (95)" partway through, leaving a PARTIALLY
    # SYNCED share — which is worse than not trying). The share is a distribution artifact,
    # so consumers want the file, not the link.
    r = run(_asuser(["rsync", "-a", "-L", "--delete", "--exclude=__pycache__", "--exclude=*.pyc",
                     f"{src}/", f"{MOUNT}/scripts/"]), timeout=300)
    if r.returncode != 0:
        log(f"rsync to share FAILED: {(r.stderr or '')[:160]}")
        return False
    try:
        REFRESH_MARKER.touch()
    except OSError:
        pass
    n = len(list((Path(MOUNT) / "scripts").glob("*.py")))
    log(f"scripts refreshed from GitHub -> {MOUNT}/scripts ({n} py files)")
    return True


def notify(msg, level="warning"):
    try:
        sys.path.insert(0, "/nova/scripts")
        from nova_notify import notify as _n
        _n(f"Storage failover: {msg}", level=level, category="infra")
    except Exception:
        pass


def main():
    primary = TARGETS[0]
    source = current_source()
    healthy = mount_healthy()
    on_primary = bool(source and primary["unc"] in source)

    # ── Healthy on the primary: nothing to do. The common case.
    if healthy and on_primary:
        return 0

    # ── Healthy on a fallback: try to fail BACK when the primary returns.
    if healthy and not on_primary:
        if reachable(primary["host"]):
            log(f"primary {primary['name']} is back — failing back from {source}")
            clear_mount()
            if do_mount(primary):
                refresh_scripts_from_git()
                log("failed back to primary")
                notify(f"{MOUNT} failed BACK to {primary['name']} — primary is healthy again", "info")
                return 0
            log("fail-back failed — restoring fallback")
            clear_mount()
            for t in TARGETS[1:]:
                if reachable(t["host"]) and do_mount(t):
                    return 0
            return 1
        return 0  # primary still down, fallback is serving fine

    # ── Not healthy: mount table may claim otherwise. Clear it and rebuild.
    log(f"{MOUNT} unreadable (source={source or 'none'}) — attempting recovery")
    clear_mount()
    for t in TARGETS:
        if not reachable(t["host"]):
            log(f"{t['name']} ({t['host']}) unreachable — skipping")
            continue
        if do_mount(t):
            log(f"mounted {MOUNT} from {t['name']}")
            refresh_scripts_from_git()
            if not t["primary"]:
                notify(f"{MOUNT} FAILED OVER to {t['name']} — {primary['name']} is down. "
                       f"Scripts refreshed from GitHub. Will fail back automatically.", "critical")
            return 0

    log("NO storage target reachable — /nova is down")
    notify(f"{MOUNT} DOWN — no storage target reachable (tried: "
           f"{', '.join(t['name'] for t in TARGETS)})", "critical")
    return 1


if __name__ == "__main__":
    sys.exit(main())
