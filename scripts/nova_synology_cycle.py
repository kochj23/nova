#!/usr/bin/env python3
"""nova_synology_cycle.py — daily wake → replicate → off-site → sleep for the Synology cold replica.

Jordan's flow (2026-11-16): Synology is off -> Nova wakes it -> backup job runs -> when done, wait
for Cloud Sync (it only watches one folder, /volume1/nas/GoogleDriveBackups: that IS the off-site
backup) to finish -> Nova turns the Synology off.

Steps
  1. WAKE     magic packet to every Synology NIC MAC; wait (≤ WAKE_TIMEOUT) until SSH answers and
              /volume1 is mounted. Clears the expected_off flag (nova_synology_power) so monitors
              watch it again while it is up.
  2. BACKUP   nova_nas_localdiff_reverse.py --apply (full diff, UNAS -> Synology) for each share.
              Same per-share lock as before. Soft deletes when service_config
              synology_cycle.soft_deletes = true; churn_prefixes ({share: [prefix]}) are exempt
              from the 1% cap AND purged at once (disposable); real data keeps the 30-day hold.
  3. CLOUD    Cloud Sync health (connection_table.error != 0 -> warning: off-site is broken).
              Wait at least CLOUD_MIN_WAIT, then until its history log has been quiet for
              CLOUD_QUIET; give up waiting at CLOUD_MAX_WAIT (power off anyway, warn).
  4. SLEEP    Guard: no rsync running on the Synology and no md rebuild/scrub in /proc/mdstat
              (a running scrub keeps it ON — never interrupt it). Then set expected_off, `poweroff`,
              and confirm it stops answering. A failed step never powers it off.

  nova_synology_cycle.py              # run the cycle (scheduler, daily)
  nova_synology_cycle.py --dry-run    # print what would happen, touch nothing
  nova_synology_cycle.py --no-sleep   # wake + backup + cloud wait, leave it on
  nova_synology_cycle.py --wake       # just wake it (and clear expected_off)
  nova_synology_cycle.py --status     # is it up, flag, Cloud Sync state
"""
import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_synology_power as power  # noqa: E402

HOST = "192.168.1.11"
SSH_TARGET = f"kochj@{HOST}"
# NIC MACs live in PG: service_config synology_cycle.wol_macs (eth4 = the cabled 10GbE add-in card,
# plus onboard eth0-3). WoL from soft-off is only guaranteed on onboard ports; DSM's power schedule
# is the fallback if the add-in NIC can't wake it.
SHARES = ["nas", "external"]
SCRIPTS = Path(__file__).resolve().parent
CLOUD_DB = "/volume1/@cloudsync/db"

WAKE_TIMEOUT = int(os.environ.get("SYN_WAKE_TIMEOUT", 900))
BACKUP_TIMEOUT = int(os.environ.get("SYN_BACKUP_TIMEOUT", 4 * 3600))
CLOUD_MIN_WAIT = int(os.environ.get("SYN_CLOUD_MIN_WAIT", 3600))
CLOUD_QUIET = int(os.environ.get("SYN_CLOUD_QUIET", 600))
CLOUD_MAX_WAIT = int(os.environ.get("SYN_CLOUD_MAX_WAIT", 4 * 3600))
OFF_TIMEOUT = int(os.environ.get("SYN_OFF_TIMEOUT", 600))
POLL = 30

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR"]


def log(m):
    print(f"[synology-cycle {time.strftime('%H:%M:%S')}] {m}", flush=True)


def notify(title, body, level="info"):
    try:
        import nova_notify
        nova_notify.notify(title, body, level=level, category="backup", source="nova_synology_cycle")
    except Exception as e:
        log(f"notify failed: {e}")


def ssh(cmd, timeout=60):
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, SSH_TARGET, cmd], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        return -1, ""


def is_up():
    rc, out = ssh("test -d /volume1/nas/frigate && echo up", timeout=20)  # local replica volume
    return rc == 0 and "up" in out


def wol_macs():
    try:
        import psycopg2
        with psycopg2.connect(power.DSN, connect_timeout=5) as c, c.cursor() as cur:
            cur.execute("SELECT value FROM service_config WHERE service='synology_cycle' AND key='wol_macs'")
            row = cur.fetchone()
            return list(row[0]) if row else []
    except Exception:
        return []


def magic_packet(mac):
    raw = bytes.fromhex(mac.replace(":", ""))
    return b"\xff" * 6 + raw * 16


def wake(dry):
    if is_up():
        log("already up")
        return True
    macs = wol_macs()
    if not macs:
        log("no WoL MACs in service_config synology_cycle.wol_macs")
        return False
    log(f"sending Wake-on-LAN to {len(macs)} MACs")
    if dry:
        return True
    deadline = time.time() + WAKE_TIMEOUT
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for mac in macs:
                for port in (9, 7):
                    s.sendto(magic_packet(mac), ("192.168.1.255", port))
        time.sleep(POLL)
        if is_up():
            log(f"up after {int(WAKE_TIMEOUT - (deadline - time.time()))}s")
            return True
    return False


def cfg(key, default=None):
    """service_config(service='synology_cycle', key) -> jsonb value, or default."""
    try:
        import psycopg2
        with psycopg2.connect(power.DSN, connect_timeout=5) as c, c.cursor() as cur:
            cur.execute("SELECT value FROM service_config WHERE service='synology_cycle' AND key=%s", (key,))
            row = cur.fetchone()
            return row[0] if row else default
    except Exception:
        return default


def soft_deletes_enabled():
    return cfg("soft_deletes") is True


def purge_churn_now(share, prefixes, dry):
    """Churn (camera segments, pg WAL/dumps) is disposable by design: drop today's soft-deleted
    copies at once instead of holding them 30 days. At ~330 GB/day of Frigate churn a 30-day hold
    would pin ~10 TB. Real data keeps the 30-day hold in #nova_deleted/<date>/."""
    day = time.strftime("%Y-%m-%d")
    for p in prefixes:
        p = p.strip("/")
        if not p or ".." in p.split("/"):
            continue
        target = f"/volume1/{share}/#nova_deleted/{day}/{p}"
        log(f"purge churn now: {target}")
        if not dry:
            ssh(f"sudo -n rm -rf -- '{target}'", timeout=3600)


def backup(dry):
    ok = True
    soft = soft_deletes_enabled()
    churn = cfg("churn_prefixes", {}) or {}
    for share in SHARES:
        prefixes = churn.get(share, []) if soft else []
        extra = (["--soft-deletes"] + [x for p in prefixes for x in ("--churn-prefix", p)]) if soft else []
        cmd = [sys.executable, str(SCRIPTS / "nova_nas_localdiff_reverse.py"), "--apply", *extra, share]
        log("run: " + " ".join(cmd[1:]))
        if dry:
            purge_churn_now(share, prefixes, dry)
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=BACKUP_TIMEOUT)
            tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
            log(f"{share}: rc={r.returncode} " + " | ".join(tail))
            if r.returncode not in (0, 3):      # 3 = another layer holds the lock (not a failure)
                ok = False
            elif prefixes:
                purge_churn_now(share, prefixes, dry)
        except subprocess.TimeoutExpired:
            log(f"{share}: timed out after {BACKUP_TIMEOUT}s")
            ok = False
    return ok


def cloud_state():
    """(error_code, seconds_since_last_activity) from Cloud Sync's own DBs; (None, None) if unknown."""
    q = (f"sudo -n sqlite3 {CLOUD_DB}/config.sqlite 'SELECT error FROM connection_table LIMIT 1'; "
         f"sudo -n sqlite3 {CLOUD_DB}/history.sqlite 'SELECT MAX(time) FROM history_table'; date +%s")
    rc, out = ssh(q, timeout=30)
    vals = [v.strip() for v in out.splitlines() if v.strip()]
    if rc != 0 or len(vals) < 3:
        return None, None
    try:
        err, last, now = int(vals[0]), int(vals[1] or 0), int(vals[2])
    except ValueError:
        return None, None
    return err, now - last


def wait_cloud(dry):
    err, idle = cloud_state()
    if err not in (0, None) and not dry:
        notify("Off-site backup broken: Synology Cloud Sync",
               f"Cloud Sync's Google Drive connection reports error {err}; GoogleDriveBackups is not "
               "reaching the cloud. Re-link the Google Drive task in DSM > Cloud Sync.", "warning")
    log(f"cloud sync: error={err} idle={idle}s; waiting ≥{CLOUD_MIN_WAIT}s then {CLOUD_QUIET}s quiet")
    if dry:
        return True
    start = time.time()
    while True:
        waited = time.time() - start
        _, idle = cloud_state()
        if waited >= CLOUD_MIN_WAIT and idle is not None and idle >= CLOUD_QUIET:
            log(f"cloud sync quiet for {idle}s after {int(waited)}s")
            return True
        if waited >= CLOUD_MAX_WAIT:
            notify("Synology cycle: Cloud Sync still busy at cut-off",
                   f"Still uploading after {int(waited / 60)} min; powering off anyway. It resumes "
                   "at the next wake.", "warning")
            return True
        time.sleep(POLL * 2)


def safe_to_sleep():
    rc, out = ssh("pgrep -x rsync >/dev/null && echo RSYNC; cat /proc/mdstat", timeout=30)
    if rc not in (0, 1):
        return False, "cannot reach it to check"
    if "RSYNC" in out:
        return False, "rsync still running"
    if re.search(r"(resync|recovery|reshape|check)\s*=", out):
        return False, "RAID scrub/rebuild in progress — leaving it on (never interrupt it)"
    return True, ""


def sleep_now(dry):
    ok, why = safe_to_sleep()
    if not ok:
        log(f"not powering off: {why}")
        notify("Synology left ON", why, "info")
        return False
    log("powering off")
    if dry:
        return True
    power.set_expected_off(True)
    ssh("sudo -n poweroff", timeout=30)
    deadline = time.time() + OFF_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL)
        if subprocess.run(["ping", "-c1", "-t2", HOST], capture_output=True).returncode != 0:
            log("confirmed off")
            return True
    power.set_expected_off(False)              # it never went down: monitor it normally
    notify("Synology did not power off", f"Still answering ping {OFF_TIMEOUT}s after poweroff.", "warning")
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-sleep", action="store_true")
    ap.add_argument("--wake", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)

    if a.status:
        up = is_up()
        err, idle = cloud_state() if up else (None, None)
        print(f"up={up} expected_off={power.expected_off()} cloud_error={err} cloud_idle_s={idle} "
              f"soft_deletes={soft_deletes_enabled()}")
        return 0

    if not wake(a.dry_run):
        notify("Synology did not wake", f"No SSH {WAKE_TIMEOUT}s after Wake-on-LAN. Check power / "
               "DSM power schedule.", "critical")
        return 1
    if not a.dry_run:
        power.set_expected_off(False)
    if a.wake:
        return 0

    if not backup(a.dry_run):
        notify("Synology cycle: replication failed", "Leaving the Synology ON for inspection.", "warning")
        return 1
    wait_cloud(a.dry_run)
    if a.no_sleep:
        return 0
    sleep_now(a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
