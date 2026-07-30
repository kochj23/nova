#!/usr/bin/env python3
"""nova_nas_mount_watchdog.py — keep /Volumes/nas (Synology SMB share) mounted.

The mount was previously ad-hoc (whatever established it once, no remount on drop)
and silently degraded every consumer (nova_pg_backup.sh, nova_nas_rsync.py, Plex
ingest, ...) to local-only/skip whenever it dropped — caught for real on 2026-07-19
when the nightly PG backup fell back to local-only because the mount was down at
2am. This just keeps it up: check, and if missing, remount with Keychain creds.

Scheduled every 5 min via scheduler.yaml. Idempotent — a no-op when already mounted.
"""
import os
import subprocess
import sys
import urllib.parse

MOUNT_POINT = "/Volumes/nas"
# 2026-07-26: /Volumes/external silently dropped mid-session and broke the video
# ingest (PermissionError on a path that simply was not there). The watchdog only
# ever watched /Volumes/nas, so nothing noticed. Watch every share we depend on.
MOUNT_POINTS = {"/Volumes/nas": "nas", "/Volumes/external": "external"}
NAS_IP = "192.168.1.11"
SHARE = "nas"


def log(m):
    print(f"[nas-mount-watchdog] {m}", flush=True)


def is_mounted(path):
    r = subprocess.run(["mount"], capture_output=True, text=True)
    return f" on {path} " in r.stdout


def keychain(service):
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", service, "-w"],
        capture_output=True, text=True, timeout=10)
    if r.stdout.strip():
        return r.stdout.strip()
    # Keychain is unreadable from the scheduler's daemon context — fall back to
    # the fleet secret store (PG pgcrypto) so the watchdog works unattended.
    try:
        import nova_secrets
        return nova_secrets.get_secret(service) or ""
    except Exception:
        return ""


def _nas_reachable():
    """Does the NAS answer ICMP at all? Distinguishes a fixable mount problem from
    a NAS that is entirely down."""
    r = subprocess.run(["ping", "-c", "1", "-t", "2", NAS_IP], capture_output=True)
    return r.returncode == 0


def main():
    # If the NAS itself is unreachable, no remount can succeed — this is a NAS-down
    # condition, not a mount the watchdog can fix. Alert ONCE (deduped, wide window)
    # and exit 0, so a multi-hour hard outage doesn't emit a scheduler failure every
    # run (that was the 64-consecutive-failure alert storm on 2026-07-30, while the
    # Synology was hung link-up but IP-dead — recoverable only by a power-cycle).
    if not _nas_reachable():
        log(f"NAS {NAS_IP} unreachable (ICMP) — nothing to remount; alerting (deduped)")
        try:
            from nova_notify import notify
            notify(f"NAS {NAS_IP} unreachable — SMB mounts can't recover until it's back",
                   body="ICMP down: the box is not answering at all (may be hung link-up / needs a "
                        "power-cycle). Mounts will auto-recover when it returns.",
                   level="warning", category="storage", source="nova_nas_mount_watchdog.py",
                   dedup_key="nas-unreachable",
                   meta={"host": "synology-nas", "dedup_window_s": 21600})
        except Exception:
            pass
        return 0
    rc = 0
    for mp, share in MOUNT_POINTS.items():
        rc |= _ensure(mp, share)
    return rc


def _ensure(MOUNT_POINT, SHARE):
    # MINIMUM GRAIN, and the reason matters: on 2026-07-27 this watchdog UNMOUNTED a
    # perfectly healthy /Volumes/external. is_mounted() gave a false negative, the recovery
    # path ran `umount -f` on a live mount, macOS then removed the /Volumes entry, and the
    # remount failed with "could not find mount point". A watchdog that can destroy what it
    # watches is worse than no watchdog. So: trust a successful READ over the mount table,
    # and never unmount something we can still read.
    try:
        if os.listdir(MOUNT_POINT):
            return 0
    except OSError:
        pass
    if is_mounted(MOUNT_POINT):
        return 0

    log(f"{MOUNT_POINT} not mounted — attempting remount")
    # /Volumes is root-owned — mkdir needs sudo (passwordless, matches this fleet's
    # convention), then hand ownership to the mounting user or mount_smbfs itself
    # fails with "Operation not permitted".
    subprocess.run(["sudo", "-n", "mkdir", "-p", MOUNT_POINT], capture_output=True)
    subprocess.run(["sudo", "-n", "chown", f"{subprocess.run(['whoami'], capture_output=True, text=True).stdout.strip()}:staff", MOUNT_POINT], capture_output=True)
    # Clear a stale/broken mountpoint entry before remounting
    subprocess.run(["umount", "-f", MOUNT_POINT], capture_output=True)

    user = keychain("nova-synology-username")
    pw = keychain("nova-synology-password")
    if not user or not pw:
        log("no Synology credentials in Keychain — cannot remount")
        return 1

    enc_pw = urllib.parse.quote(pw, safe="")
    r = subprocess.run(
        ["mount_smbfs", f"//{user}:{enc_pw}@{NAS_IP}/{SHARE}", MOUNT_POINT],
        capture_output=True, text=True, timeout=30)
    if r.returncode == 0 and is_mounted(MOUNT_POINT):
        log("remounted successfully")
        return 0
    log(f"remount FAILED: {r.stderr.strip()[:200]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
