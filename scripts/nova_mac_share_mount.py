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
import json
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

# UNAS-primary cutover 2026-09-10: the UNAS (192.168.1.69) is now the read-write
# PRIMARY and the Synology (192.168.1.11) the read-only FALLBACK — inverted from the
# original Synology-primary design. Flip these two back to restore the old order.
PRIMARY, FALLBACK = UNAS, SYNOLOGY


def _share_for(host, spec):
    """Share name differs by box: Synology serves lowercase 'external', the UNAS
    serves capital 'External'. Pick the right one for whichever host we're mounting."""
    return spec["unas_share"] if host == UNAS else spec["syn_share"]


def _host_of(src):
    """Which known host serves this mount source. The mount table doesn't always
    show an IP: Finder/login items mount the Synology over AFP as
    //user@NAS._afpovertcp._tcp.local/<share>, which must still be recognized as
    the Synology or a healthy fallback mount gets misread as dead/foreign
    (post-reboot 2026-09-12: that misread caused a 2-min mount_smbfs EPERM loop)."""
    if not src:
        return None
    if SYNOLOGY in src or "NAS._afpovertcp" in src:
        return SYNOLOGY
    if UNAS in src:
        return UNAS
    return None


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
    host = _host_of(src)
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
    """Mount the PRIMARY share (UNAS since the 2026-09-10 cutover) read-write."""
    url = _smb_url(PRIMARY, _share_for(PRIMARY, spec))
    if not url:
        log(f"{spec['mount']}: no PRIMARY creds in Keychain — cannot mount")
        return False
    _ensure_mountpoint(spec["mount"])
    r = run(["mount_smbfs", url, spec["mount"]], timeout=30)
    # Success == the mount table now shows this point served by the PRIMARY. Do NOT
    # use ls here: it is TCC-blocked in the agent and would report a good mount failed
    # ("File exists" then a false negative). The mount table is the honest signal.
    if _mounted_from(spec["mount"], PRIMARY):
        return True
    log(f"{spec['mount']}: PRIMARY SMB mount failed: {(r.stderr or '').strip()[:150]}")
    return False


def mount_fallback_ro(spec):
    """Mount the FALLBACK replica READ-ONLY (Synology since the 2026-09-10 cutover;
    reads survive a UNAS outage; writes correctly fail rather than diverge and get
    clobbered by the reverse mirror on recovery)."""
    url = _smb_url(FALLBACK, _share_for(FALLBACK, spec))
    if not url:
        log(f"{spec['mount']}: no FALLBACK creds in Keychain — cannot fail over")
        return False
    _ensure_mountpoint(spec["mount"])
    r = run(["mount", "-t", "smbfs", "-o", "ro", url, spec["mount"]], timeout=30)
    if _mounted_from(spec["mount"], FALLBACK):
        return True
    log(f"{spec['mount']}: FALLBACK ro mount failed: {(r.stderr or '').strip()[:150]}")
    return False


def notify(msg, level="warning", dedup_key=None, meta=None):
    try:
        sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
        from nova_notify import notify as _n
        _n(f"Mac share mount: {msg}", level=level, category="storage",
           source="nova_mac_share_mount.py", dedup_key=dedup_key or f"mac-share-{level}",
           meta=meta)
    except Exception:
        pass


# Consecutive recovery-failure escalation (incident-2026-09-14-plex-mounts: the Linux
# twin failed its remount every 2 min for 31h with nobody told). Counter in the existing
# ~/.openclaw/state dir; alert at ESCALATE_AFTER, re-alert every 6h while stuck.
STATE_FILE = os.path.expanduser("~/.openclaw/state/mac_share_mount_fails.json")
ESCALATE_AFTER = 3
_STUCK_STATES = ("recover-failed", "failback-failed", "down")


def _load_fails():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def track_failure(mount, state, detail):
    fails = _load_fails()
    if state in _STUCK_STATES:
        n = int(fails.get(mount, 0)) + 1
        fails[mount] = n
        if n >= ESCALATE_AFTER:
            notify(f"{mount} recovery has failed {n} consecutive times ({state}: {detail}) — "
                   f"needs a human (Keychain creds / NAS reachability / stale mount)",
                   "warning", dedup_key=f"mac-share-recover-stuck-{mount}",
                   meta={"dedup_window_s": 6 * 3600, "consecutive_failures": n})
    elif mount in fails:
        del fails[mount]
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(fails, f)
    except OSError:
        pass


def handle(spec, check_only):
    mount = spec["mount"]
    state, src = health(mount)
    on_fallback = _host_of(src) == FALLBACK
    primary_up = reachable(PRIMARY)

    # Healthy on the UNAS primary (the common case), or healthy on a host we
    # don't know: leave it alone. A Synology mount counts as fallback whatever
    # the protocol (login items remount it over AFP) — read-write on the replica
    # diverges, so it must fail back like any other fallback mount.
    if state == "healthy" and not on_fallback:
        return "ok", f"healthy ({src})"

    # Healthy on the Synology fallback — fail BACK when the UNAS primary returns.
    if state == "healthy" and on_fallback:
        if not primary_up:
            return "ok", "UNAS primary still down; serving read-only from Synology"
        if check_only:
            return "would-failback", "UNAS primary is back; would restore primary"
        _clear(mount)
        if mount_primary(spec):
            notify(f"{mount} failed BACK to UNAS primary (read-write)", "info")
            return "failback", "restored UNAS primary"
        # restore failed — get reads back on the Synology rather than leaving it dark
        mount_fallback_ro(spec)
        return "failback-failed", "restore failed; left Synology read-only fallback"

    # state is 'dead' (mounted but its server is unreachable) or 'unmounted'.
    # Recover: prefer the UNAS primary, else the Synology replica read-only.
    if primary_up:
        if check_only:
            return "would-recover", "UNAS primary up; would mount primary"
        _clear(mount)
        if mount_primary(spec):
            return "recovered", "mounted UNAS primary"
        return "recover-failed", "UNAS primary up but SMB mount failed"
    if check_only:
        return "would-failover", "UNAS down; would mount Synology read-only"
    _clear(mount)
    if mount_fallback_ro(spec):
        notify(f"{mount} FAILED OVER to Synology (READ-ONLY) — UNAS primary is down. Writes "
               f"will fail until it returns; that is intentional (no split-brain).", "warning")
        return "failover", "read-only on Synology"
    notify(f"{mount} DOWN — neither synology nor UNAS mountable", "critical")
    return "down", "no target mountable"


def main():
    check_only = "--check" in sys.argv
    rc = 0
    for spec in MOUNTS:
        state, detail = handle(spec, check_only)
        if state not in ("ok",):
            log(f"{spec['mount']}: {state} — {detail}")
        if not check_only:
            track_failure(spec["mount"], state, detail)
        if state in ("down", "recover-failed"):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
