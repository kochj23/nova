#!/usr/bin/env python3
"""nova_mac_share_mount.py — keep the NAS data shares mounted on macOS, with failover.

The macOS counterpart to nova_datashare_failover.py (which does this for the Linux
nova-cores). Same design, same hard-won lessons; the mechanics differ because macOS
uses mount_smbfs and Keychain rather than mount.cifs and /etc/cifs-*.creds.

WHAT IT DOES
  Keeps `nas` and `external` mounted over SMB from the Synology primary. If the
  Synology is down it fails them over READ-ONLY to the UNAS replica so reads (Plex,
  AppleTV/iTunes media, browsing) keep working; writes correctly fail rather than
  diverge (the Synology->UNAS mirror would clobber outage-writes on recovery — this
  is the same no-split-brain rule as the Linux data-share failover). It fails BACK
  to the Synology automatically when it returns.

WHY IT RUNS AS THE USER (LaunchAgent, not LaunchDaemon)
  mount_smbfs mounts as the invoking user, and macOS network mounts are consumed by
  GUI apps (AppleTV/iTunes/Plex) running as kochj. Mounting as kochj means those
  apps own the mount read-write. Running as the user also gives us an unlocked login
  Keychain (a root daemon over ssh cannot read it — "User interaction is not
  allowed"), which is where the SMB creds live. .7 and .251 cannot reach PG, so
  there is no nova_secrets fallback there; the login Keychain is the only store.

HARD-WON LESSONS (inherited from nova_nas_mount_watchdog / nova_storage_failover)
  * HEALTH = "can I actually read the directory", never "is it in the mount table".
    A dead SMB mount stays listed and every read fails — the mount table lies.
  * NEVER unmount something we can still read (the 2026-07-27 incident: a false
    "not mounted" unmounted a live /Volumes/external and the remount failed). If a
    mount is readable, it is healthy; leave it alone, whatever protocol it is.
  * The /nas path on .7 is a synthetic firmlink (/etc/synthetic.conf) pointing at
    /Volumes/nas, so keeping /Volumes/nas up is all that /nas/iTunes needs — this
    helper does not touch /nas itself.

    nova_mac_share_mount.py            # act
    nova_mac_share_mount.py --check    # report what it WOULD do, change nothing
"""
import os
import re
import socket
import subprocess
import sys
import urllib.parse

SYNOLOGY = "192.168.1.11"
UNAS = "192.168.1.69"

# Every share we keep mounted on a Mac. Share names differ by box: the Synology
# serves `nas` and `external` (lowercase); the UNAS replica serves `nas` and
# `External` (capital E) — verified 2026-07-30.
MOUNTS = [
    {"mount": "/Volumes/nas",      "syn_share": "nas",      "unas_share": "nas"},
    {"mount": "/Volumes/external", "syn_share": "external", "unas_share": "External"},
]


_CRED_URL = re.compile(r"(//[^:/\s]+:)[^@\s]*(@)")


def _safe(s):
    """Redact the password out of any //user:pass@host string before logging —
    mount_smbfs echoes the full URL (creds included) in its error messages."""
    return _CRED_URL.sub(r"\1***\2", s or "")


def log(m):
    print(f"[mac-share-mount] {_safe(str(m))}", flush=True)


def run(cmd, timeout=45):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def reachable(host, port=445, timeout=4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def keychain(service):
    """SMB creds live in kochj's login Keychain (unlocked in the GUI-session context
    a LaunchAgent runs in). If the Keychain is unreadable (e.g. a daemon/scheduler
    context on .6 where it can be locked), fall back to the fleet secret store (PG
    pgcrypto via nova_secrets) — this matches nova_nas_mount_watchdog. On .7/.251 PG
    is unreachable, so that fallback simply returns nothing and the GUI-session
    Keychain remains the source."""
    r = run(["security", "find-generic-password", "-a", "nova", "-s", service, "-w"],
            timeout=10)
    if r.stdout.strip():
        return r.stdout.strip()
    try:
        sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
        import nova_secrets
        return nova_secrets.get_secret(service) or ""
    except Exception:
        return ""


def can_ls(mount, timeout=8):
    """POSITIVE-ONLY fast path: if we can list the dir, it is definitely alive.
    A FALSE here is NOT proof of death — a background LaunchAgent's own process is
    blocked by macOS TCC from reading network volumes (the GUI apps that consume the
    mount are not), so its `ls` fails on a perfectly healthy share. Never key a
    tear-down decision on a failed ls alone; see health()."""
    r = run(["/bin/ls", "-1", mount], timeout=timeout)
    return r.returncode == 0


def mount_source(mount):
    """The device string macOS shows for `mount`, e.g. //kochj@192.168.1.11/nas .
    This is the mount table (no VFS access, so TCC-free and always observable)."""
    r = run(["/sbin/mount"], timeout=15)
    for line in r.stdout.splitlines():
        # format: <src> on <mountpoint> (fstype, opts)
        if f" on {mount} " in line:
            return line.split(" on ", 1)[0]
    return None


def health(mount):
    """Is this mount healthy, using signals a TCC-blocked agent CAN observe?

    Returns (state, src) where state is 'healthy' | 'dead' | 'unmounted'.
      * not in the mount table            -> unmounted
      * ls succeeds                       -> healthy (definitive positive)
      * ls fails but its server answers   -> healthy (the ls failure is TCC, not a
                                             dead mount — do NOT tear it down)
      * ls fails AND server unreachable   -> dead (genuinely gone; safe to recover)
    """
    src = mount_source(mount)
    if not src:
        return "unmounted", None
    if can_ls(mount):
        return "healthy", src
    host = SYNOLOGY if SYNOLOGY in src else (UNAS if UNAS in src else None)
    if host and reachable(host):
        return "healthy", src
    return "dead", src


def _mounted_from(mount, host):
    """Did a (re)mount actually land — is the mount now served by `host`? Uses the
    mount table, not ls, so it works in the TCC-blocked agent context too."""
    return host in (mount_source(mount) or "")


def _ensure_mountpoint(mount):
    """/Volumes is root-owned; make the dir and hand it to kochj or mount_smbfs fails
    with 'Operation not permitted' (matches nova_nas_mount_watchdog)."""
    run(["sudo", "-n", "mkdir", "-p", mount], timeout=10)
    user = run(["whoami"], timeout=5).stdout.strip()
    run(["sudo", "-n", "chown", f"{user}:staff", mount], timeout=10)


def _smb_url(host, share):
    user = keychain("nova-synology-username" if host == SYNOLOGY else "nova-unas-username")
    pw = keychain("nova-synology-password" if host == SYNOLOGY else "nova-unas-password")
    if not user or not pw:
        return None
    return f"//{user}:{urllib.parse.quote(pw, safe='')}@{host}/{share}"


def _clear(mount):
    run(["umount", "-f", mount], timeout=15)
    run(["sudo", "-n", "umount", "-f", mount], timeout=15)


def mount_primary(spec):
    """Mount the Synology share read-write at its mount point (steady state)."""
    url = _smb_url(SYNOLOGY, spec["syn_share"])
    if not url:
        log(f"{spec['mount']}: no Synology creds in Keychain — cannot mount")
        return False
    _ensure_mountpoint(spec["mount"])
    r = run(["mount_smbfs", url, spec["mount"]], timeout=30)
    # Success == the mount table now shows this point served by the Synology. Do NOT
    # use ls here: it is TCC-blocked in the agent and would report a good mount failed
    # ("File exists" then a false negative). The mount table is the honest signal.
    if _mounted_from(spec["mount"], SYNOLOGY):
        return True
    log(f"{spec['mount']}: Synology SMB mount failed: {(r.stderr or '').strip()[:150]}")
    return False


def mount_fallback_ro(spec):
    """Mount the UNAS replica READ-ONLY (reads survive a Synology outage; writes
    correctly fail rather than diverge and get clobbered by the mirror on recovery)."""
    url = _smb_url(UNAS, spec["unas_share"])
    if not url:
        log(f"{spec['mount']}: no UNAS creds in Keychain — cannot fail over")
        return False
    _ensure_mountpoint(spec["mount"])
    r = run(["mount", "-t", "smbfs", "-o", "ro", url, spec["mount"]], timeout=30)
    if _mounted_from(spec["mount"], UNAS):
        return True
    log(f"{spec['mount']}: UNAS ro mount failed: {(r.stderr or '').strip()[:150]}")
    return False


def notify(msg, level="warning"):
    try:
        sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
        from nova_notify import notify as _n
        _n(f"Mac share mount: {msg}", level=level, category="storage",
           source="nova_mac_share_mount.py", dedup_key=f"mac-share-{level}")
    except Exception:
        pass


def handle(spec, check_only):
    mount = spec["mount"]
    state, src = health(mount)
    on_fallback = bool(src and UNAS in src)
    syn_up = reachable(SYNOLOGY)

    # Healthy on the Synology primary (the common case), or healthy on any protocol
    # we didn't place (a pre-existing AFP mount): leave it alone.
    if state == "healthy" and not on_fallback:
        return "ok", f"healthy ({src})"

    # Healthy on the UNAS fallback — fail BACK when the Synology returns.
    if state == "healthy" and on_fallback:
        if not syn_up:
            return "ok", "synology still down; serving read-only from UNAS"
        if check_only:
            return "would-failback", "synology is back; would restore primary"
        _clear(mount)
        if mount_primary(spec):
            notify(f"{mount} failed BACK to synology (read-write)", "info")
            return "failback", "restored synology primary"
        # restore failed — get reads back on the UNAS rather than leaving it dark
        mount_fallback_ro(spec)
        return "failback-failed", "restore failed; left UNAS read-only fallback"

    # state is 'dead' (mounted but its server is unreachable) or 'unmounted'.
    # Recover: prefer the Synology primary, else the UNAS replica read-only.
    if syn_up:
        if check_only:
            return "would-recover", "synology up; would mount primary"
        _clear(mount)
        if mount_primary(spec):
            return "recovered", "mounted synology primary"
        return "recover-failed", "synology up but SMB mount failed"
    if check_only:
        return "would-failover", "synology down; would mount UNAS read-only"
    _clear(mount)
    if mount_fallback_ro(spec):
        notify(f"{mount} FAILED OVER to UNAS (READ-ONLY) — synology is down. Writes "
               f"will fail until it returns; that is intentional (no split-brain).", "warning")
        return "failover", "read-only on UNAS"
    notify(f"{mount} DOWN — neither synology nor UNAS mountable", "critical")
    return "down", "no target mountable"


def main():
    check_only = "--check" in sys.argv
    rc = 0
    for spec in MOUNTS:
        state, detail = handle(spec, check_only)
        if state not in ("ok",):
            log(f"{spec['mount']}: {state} — {detail}")
        if state in ("down", "recover-failed"):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
