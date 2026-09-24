#!/usr/bin/env python3
"""nova_datashare_failover.py — read-only failover for the bulk NAS data shares.

Companion to nova_storage_failover.py (which handles /nova). That one is special:
/nova's source of truth is GitHub, so it can fail over READ-WRITE and re-seed from
git. The bulk data shares (media, backups, general storage) have no such external
truth — their only backup is the UNAS replica. So failing them over read-WRITE would
risk split-brain: writes during a Synology outage would diverge from the master and
the Synology->UNAS mirror would clobber them on recovery.

So this daemon fails the data shares over READ-ONLY to the UNAS replica. Reads
(Plex, media, browsing) keep working through a Synology outage; writes correctly
fail rather than diverge, and resume when the Synology returns.

DESIGN (inherited from nova_storage_failover, same hard-won lessons):
  * HEALTH = "can I read a real file", not "is it in the mount table". A dead CIFS
    mount stays listed and fails every read with EHOSTDOWN — the mount table lies.
  * NEVER unmount something we can still read (the 2026-07-27 incident: a false
    "not mounted" unmounted a live /Volumes/external and the remount failed).
  * Fail BACK to the Synology primary automatically when it returns.

It does NOT create the steady-state primary mount — fstab does that at boot,
read-write, to the Synology. This daemon only intervenes on failure (mount the
UNAS replica read-only) and on recovery (restore the Synology primary).

Host-aware: only acts on mounts that actually exist on the host it runs on.
Runs from a systemd timer on the nova-cores (.2/.10/.86), same as the /nova one.

    nova_datashare_failover.py            # act
    nova_datashare_failover.py --check    # report what it WOULD do, change nothing
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SYNOLOGY = "192.168.1.11"
UNAS = "192.168.1.69"
UNAS_CREDS = "/etc/cifs-unas.creds"

# Every bulk data share we know how to fail over. `mount` points differ per host
# (.2/.86 use /mnt/nas, .10 uses /nas) — we skip any whose mount point isn't
# present on this host. The UNAS secondary UNCs were verified 2026-07-30 to mount
# and hold the mirrored data (//69/nas and //69/External).
MANAGED = [
    {"mount": "/mnt/nas",  "primary_unc": "//192.168.1.69/nas",
     "secondary_unc": "//192.168.1.11/nas"},
    {"mount": "/nas",      "primary_unc": "//192.168.1.69/nas",
     "secondary_unc": "//192.168.1.11/nas"},
    {"mount": "/external", "primary_unc": "//192.168.1.69/External",
     "secondary_unc": "//192.168.1.11/external"},
    # NOTE: the old NFS mount /mnt/nas-external (192.168.1.11:/volume1/external) was
    # retired 2026-07-30 — it exposed the same /volume1/external data as /external
    # (CIFS) above, had zero consumers (no repo/cron/systemd refs, no open handles),
    # and both fail over to the same UNAS 'External' share. Collapsed to just /external.
]
RO_OPTS = "credentials=%s,ro,uid=kochj,gid=kochj,iocharset=utf8,vers=3.0,_netdev" % UNAS_CREDS


def log(msg):
    print(f"[datashare-failover] {msg}", flush=True)


def run(cmd, timeout=45):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def reachable(host, port=445, timeout=4):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def readable(mount, timeout=8):
    """Minimum grain: can we actually read the directory (not just is-it-mounted)?

    Two subtleties learned the hard way:
      * The daemon runs as ROOT, but these shares are owned by kochj (CIFS
        forceuid / NFS root_squash), so root's own listdir gets EACCES and would
        falsely read as 'dead'. Probe AS kochj.
      * A slow-but-alive mount (e.g. the NAS mid-RAID-resync) must NOT read as
        dead — that would cause needless remount churn. Use a bounded timeout and
        treat 'still readable within N seconds' as healthy.
    """
    ls = ["timeout", str(timeout), "ls", "-1", mount]
    if os.geteuid() == 0:
        ls = ["sudo", "-n", "-u", "kochj"] + ls
    r = run(ls, timeout=timeout + 3)
    return r.returncode == 0


def current_source(mount):
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            p = line.split()
            if len(p) > 1 and p[1] == mount:
                return p[0]
    except OSError:
        pass
    return None


def on_secondary(mount, spec):
    src = current_source(mount) or ""
    return UNAS in src


def clear_mount(mount):
    run(["sudo", "-n", "umount", "-f", mount], timeout=15)
    run(["sudo", "-n", "umount", "-l", mount], timeout=15)


def mount_secondary_ro(spec):
    """Mount the UNAS replica READ-ONLY at the share's mount point."""
    run(["sudo", "-n", "mkdir", "-p", spec["mount"]], timeout=10)
    r = run(["sudo", "-n", "mount", "-t", "cifs", spec["secondary_unc"], spec["mount"],
             "-o", RO_OPTS], timeout=45)
    if r.returncode != 0:
        log(f"{spec['mount']}: UNAS ro mount FAILED: {(r.stderr or '').strip()[:150]}")
        return False
    return readable(spec["mount"])


def restore_primary(spec):
    """Fail back: drop the UNAS fallback and let fstab remount the Synology primary."""
    clear_mount(spec["mount"])
    # `mount <mountpoint>` uses the fstab entry (Synology, read-write) if present.
    r = run(["sudo", "-n", "mount", spec["mount"]], timeout=45)
    return r.returncode == 0 and readable(spec["mount"])


def notify(msg, level="warning", dedup_key=None, meta=None):
    try:
        sys.path.insert(0, "/nova/scripts")
        from nova_notify import notify as _n
        _n(f"Data-share failover: {msg}", level=level, category="storage",
           source="nova_datashare_failover.py", dedup_key=dedup_key or f"datashare-{level}",
           meta=meta)
    except Exception:
        pass


# Consecutive recovery-failure escalation (incident-2026-09-14-plex-mounts: this daemon
# failed the remount every 2 min for 31 HOURS and only journald knew). The counter lives
# in a tiny JSON file (this script has no PG/state store); root-owned like the daemon.
STATE_FILE = Path("/var/tmp/nova_datashare_failover_fails.json")
ESCALATE_AFTER = 3
_STUCK_STATES = ("recover-failed", "failback-failed", "down")


def _load_fails():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def track_failure(mount, state, detail):
    """Count consecutive recovery failures per mount; alert once at ESCALATE_AFTER and
    re-alert every 6h while still stuck (dedup at the notifier). Reset on any success."""
    fails = _load_fails()
    if state in _STUCK_STATES:
        n = int(fails.get(mount, 0)) + 1
        fails[mount] = n
        if n >= ESCALATE_AFTER:
            notify(f"{mount} recovery has failed {n} consecutive times ({state}: {detail}) — "
                   f"needs a human (check fstab units / systemctl daemon-reload / NAS reachability)",
                   "warning", dedup_key=f"datashare-recover-stuck-{mount}",
                   meta={"dedup_window_s": 6 * 3600, "consecutive_failures": n})
    elif mount in fails:
        del fails[mount]
    try:
        STATE_FILE.write_text(json.dumps(fails))
    except OSError:
        pass


def applies_here(spec):
    """Only manage a share whose mount point exists on this host (in fstab or mounted)."""
    if current_source(spec["mount"]):
        return True
    # mount point present as a directory that fstab would populate
    return Path(spec["mount"]).is_dir() and _in_fstab(spec["mount"])


def _in_fstab(mount):
    try:
        return any(len(l.split()) > 1 and l.split()[1] == mount and not l.strip().startswith("#")
                   for l in Path("/etc/fstab").read_text().splitlines())
    except OSError:
        return False


def handle(spec, check_only):
    mount = spec["mount"]
    if not applies_here(spec):
        return "skip", "not on this host"
    src = current_source(mount)
    healthy = readable(mount)
    # ROOT-CAUSE FIX (2026-09-06): a CONFIGURED share whose mount fully dropped leaves
    # an empty placeholder dir. readable() runs `ls` on it, gets rc=0 on the empty dir,
    # and reports "healthy" — so the healer concluded "healthy on primary" and never
    # remounted. That is exactly what left /mnt/nas (Plex) unmounted for ~40h across the
    # 2026-09-05/06 Synology outage despite this running every 2 min. If NOTHING is
    # mounted at the mountpoint (current_source is None; autofs shows 'systemd-1', not
    # None, so this won't misfire), it is NOT healthy — force recovery.
    if not src:
        healthy = False
    syn_up = reachable(SYNOLOGY)
    # "On the fallback" is the only positively-identifiable state (our ro UNAS mount
    # shows the UNAS UNC). Everything else that's readable — a direct-CIFS Synology
    # mount, an NFS mount, or an autofs placeholder ('systemd-1') — is the primary.
    # Keying on NOT-fallback avoids the autofs trap where /proc/mounts hides the real
    # source behind 'systemd-1'.
    fallback = bool(src and UNAS in src)

    # Healthy on the Synology primary (the common case) — nothing to do.
    if healthy and not fallback:
        return "ok", f"healthy on primary ({src})"

    # Healthy on the UNAS fallback — fail BACK when the Synology returns.
    if healthy and fallback:
        if syn_up:
            if check_only:
                return "would-failback", "synology is back; would restore primary"
            if restore_primary(spec):
                notify(f"{mount} failed BACK to synology (read-write)", "info")
                return "failback", "restored synology primary"
            return "failback-failed", "restore failed; leaving UNAS ro fallback"
        return "ok", "synology still down; serving read-only from UNAS"

    # Unreadable (mount table may lie). Recover: prefer Synology, else UNAS read-only.
    if syn_up:
        if check_only:
            return "would-recover", "synology up; would restore primary"
        clear_mount(mount)
        if restore_primary(spec):
            return "recovered", "restored synology primary"
        return "recover-failed", "synology up but primary mount failed"
    # Synology down — fail over to UNAS read-only so reads keep working.
    if check_only:
        return "would-failover", f"synology down; would mount {spec['secondary_unc']} read-only"
    clear_mount(mount)
    if mount_secondary_ro(spec):
        notify(f"{mount} FAILED OVER to UNAS (READ-ONLY) — synology is down. "
               f"Writes will fail until it returns; that is intentional (no split-brain).", "warning")
        return "failover", f"read-only on UNAS ({spec['secondary_unc']})"
    notify(f"{mount} DOWN — neither synology nor UNAS mountable", "critical")
    return "down", "no target mountable"


def main():
    check_only = "--check" in sys.argv
    rc = 0
    for spec in MANAGED:
        state, detail = handle(spec, check_only)
        if state not in ("ok", "skip"):
            log(f"{spec['mount']}: {state} — {detail}")
        if not check_only and state != "skip":
            track_failure(spec["mount"], state, detail)
        if state in ("down", "recover-failed", "failover-failed"):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
