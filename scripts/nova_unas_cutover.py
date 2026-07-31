#!/usr/bin/env python3
"""nova_unas_cutover.py — staged, verified plan for the UNAS-primary cutover.

The migration end-state: UNAS (192.168.1.69) becomes PRIMARY storage, the Synology
(192.168.1.11) becomes the replica. This script exists so cutover day is "run the
tested thing and watch," not improvisation.

SAFE BY DESIGN:
  * DEFAULT IS A DRY-RUN PLAN. With no args it PRINTS the exact ordered steps and
    commands, checked against live state, and changes NOTHING.
  * It PREFLIGHTS the two hard gates and refuses to green-light until both pass:
      1. Synology RAID resync finished (don't cut over mid-rebuild).
      2. Parity proven — the UNAS holds everything the Synology does (nova_nas_localdiff
         reports 0 files differ), so nothing is lost when the Synology stops being source.
  * The actual flip steps are printed for you to execute (or paste), each reversible.
    There is intentionally NO blind --apply that rewrites fstab on three cores
    unattended; cutover is a supervised maintenance window.

    nova_unas_cutover.py            # preflight + print the full ordered plan
    nova_unas_cutover.py --check    # just the two go/no-go gates
"""
import subprocess
import sys

SYN = "192.168.1.11"
UNAS = "192.168.1.69"
CORES = ["192.168.1.2", "192.168.1.10", "192.168.1.86"]      # /nas + /external + fstab
MACS = ["192.168.1.6", "192.168.1.7", "192.168.1.251", "192.168.1.252"]  # nova_mac_share_mount


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def gate_resync():
    """Gate 1: the Synology RAID must NOT be resyncing."""
    r = run(["ssh", "-o", "BatchMode=yes", f"kochj@{SYN}", "cat /proc/mdstat"])
    import re
    m = re.search(r"(resync|recovery)\s*=\s*([0-9.]+)%", r.stdout)
    if m:
        return False, f"RAID still {m.group(1)} {m.group(2)}% — WAIT (cutting over mid-rebuild risks the source)"
    if "[U" in r.stdout:
        return True, "RAID healthy, no resync in progress"
    return False, "could not read /proc/mdstat — verify manually before proceeding"


def gate_parity():
    """Gate 2: UNAS must already hold everything the Synology does.
    We do NOT run a fresh scan here (that loads the array); we read the last
    nova_nas_localdiff result from telemetry.backup_runs. A clean recent reconcile
    (files=0) is the green light. Re-run localdiff manually right before cutover for
    the freshest confirmation."""
    sql = ("SELECT to_char(ts,'MM-DD HH24:MI'), job, files, ok FROM telemetry.backup_runs "
           "WHERE job LIKE 'nova-backup:%:localdiff' ORDER BY ts DESC LIMIT 4")
    r = run(["psql", "-h", "localhost", "-U", "kochj", "-d", "nova_ops", "-tA", "-F", " | ", "-c", sql])
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    if not lines:
        return False, "no localdiff runs recorded — run nova_nas_localdiff.py and confirm '0 files differ' first"
    fresh = "\n      ".join(lines)
    zero = all(l.rsplit(" | ", 2)[-2].strip() in ("0", "") for l in lines if " | " in l)
    return zero, f"last reconciles:\n      {fresh}"


PLAN = """
============================================================
  UNAS-PRIMARY CUTOVER — ordered plan (execute in a window)
============================================================
Each step is reversible; do them in order and verify before the next.

0) FINAL PARITY: run nova_nas_localdiff.py once more; confirm BOTH shares report
   "0 files differ". That clean run is the point of no regret.

1) QUIESCE the forward mirror (stop Synology->UNAS writes so nothing changes under you):
     - scheduler.yaml: set nas_localdiff enabled:false  (SIGHUP the scheduler)
     - Synology Task Scheduler: pause the 03:30 nova_nas_backup job
   Both are additive-only, so pausing is safe.

2) REVERSE the mirror (UNAS becomes source of truth going forward):
     nova_nas_localdiff_reverse.py            # DRY RUN first — expect ~0 delta at cutover
     nova_nas_localdiff_reverse.py --apply    # UNAS -> Synology, additive
   (Deletes stay OFF until you've watched a few clean rounds; --allow-deletes later.)

3) FLIP the mount primaries so reads/writes land on the UNAS:
   LINUX CORES (%(cores)s) — repoint the primary nas/external fstab entries from
     //%(syn)s/...  ->  //%(unas)s/...  (keep vers=3.0,nofail,x-systemd.automount),
     then: systemctl daemon-reload && umount -l /mnt/nas /external && ls /mnt/nas /external
   MACS (%(macs)s) — in nova_mac_share_mount.py swap SYNOLOGY<->UNAS so the UNAS is the
     rw primary and the Synology the ro fallback; redeploy; kickstart the LaunchAgent.
   Also flip nova_datashare_failover.py MANAGED primary_unc/secondary_unc (UNAS primary).

4) VERIFY every host: /nas + /external readable and now backed by //%(unas)s
   (the failover self-heal + `--check` on each helper should read clean).

5) REPOINT consumers (manual, app-level): Plex libraries, Time Machine target,
   iTunes/AppleTV (/nas/iTunes already resolves via the symlink — just confirm it's
   the UNAS mount underneath now).

6) The Synology is now the REPLICA. Leave it powered; the reverse mirror keeps it
   current. Keep it as warm rollback for a week before repurposing.

ROLLBACK at any point before step 5: re-point fstab/config back to //%(syn)s and
remount — the Synology is untouched and still complete.
============================================================
""" % {"cores": ", ".join(CORES), "macs": ", ".join(MACS), "syn": SYN, "unas": UNAS}


def main():
    check_only = "--check" in sys.argv
    print("\n=== CUTOVER PREFLIGHT ===")
    g1_ok, g1 = gate_resync()
    print(f"  [{'GO' if g1_ok else 'WAIT'}] Gate 1 — RAID resync: {g1}")
    g2_ok, g2 = gate_parity()
    print(f"  [{'GO' if g2_ok else 'WAIT'}] Gate 2 — UNAS parity: {g2}")
    ready = g1_ok and g2_ok
    print(f"\n  ==> {'READY to cut over.' if ready else 'NOT READY — clear the WAIT gates first.'}")
    if check_only:
        return 0 if ready else 1
    print(PLAN)
    if not ready:
        print("  (Plan shown for reference; do NOT execute until both gates read GO.)")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
