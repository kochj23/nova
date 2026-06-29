#!/usr/bin/env python3
"""nova_ssh_rsync_watch.py — watch the direct Synology→UNAS SSH rsync repair and
report to #nova-info + record telemetry.backup_runs when it finishes.

The CIFS path mangled leading-space/special filenames so ~1.26M files never
landed; this watches the ext4→ext4 SSH rsync that fixes them. Completion is
detected via `kill -0 <pid>` on the Synology (busybox has no pgrep).

Usage: nova_ssh_rsync_watch.py <synology_pid>
Written by Jordan Koch (via Claude).
"""
import re
import subprocess
import sys
import time

import psycopg2

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import nova_config

SYN = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "kochj@192.168.1.11"]
UNAS = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "root@192.168.1.69"]
LOG = "/volume1/homes/kochj/nova_direct_rsync.log"
UPATH = "/volume/b37f2e84-517c-4a4f-92f0-4d642527ba17/.srv/.unifi-drive/nas"
DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
HEARTBEAT_S = 1800
POLL_S = 120
MAX_S = 14 * 3600


def slack(m):
    try:
        nova_config.post_both(m, slack_channel=nova_config.SLACK_INFO, discord_channel=None)
    except Exception as e:
        print("slack:", e, flush=True)


def syn(cmd):
    try:
        return subprocess.run(SYN + [cmd], capture_output=True, text=True, timeout=40).stdout
    except Exception:
        return ""


def unas_files():
    try:
        out = subprocess.run(UNAS + [f"find {UPATH} -type f 2>/dev/null | wc -l"],
                             capture_output=True, text=True, timeout=300).stdout
        return int(re.search(r"\d+", out).group()) if re.search(r"\d+", out) else None
    except Exception:
        return None


def record(ok, files):
    try:
        c = psycopg2.connect(DSN); c.autocommit = True
        with c.cursor() as cur:
            cur.execute("INSERT INTO telemetry.backup_runs (ts,job,rc,elapsed_s,files,bytes,errors,ok) "
                        "VALUES (now(),'nova-backup:nas:ssh-repair',%s,0,%s,0,%s,%s)",
                        (0 if ok else 23, files, 0 if ok else 1, ok))
        c.close()
    except Exception as e:
        print("telemetry:", e, flush=True)


def main():
    pid = sys.argv[1]
    slack(":satellite: *Direct SSH backup repair started* (Synology→UNAS, ext4→ext4) — re-sending the "
          "~1.26M files the CIFS path couldn't land (leading-space/SMB names). Will report when done.")
    t0 = time.time(); last_hb = t0
    while time.time() - t0 < MAX_S:
        time.sleep(POLL_S)
        alive = "ALIVE" in syn(f"kill -0 {pid} 2>/dev/null && echo ALIVE || echo DEAD")
        if not alive:
            log = syn(f"tail -25 {LOG}")
            xfer = re.search(r"Number of regular files transferred:\s*([\d,]+)", log)
            size = re.search(r"Total transferred file size:\s*([\d,]+)", log)
            err = re.search(r"rsync error:.*?\(code (\d+)\)", log)
            nfiles = int(xfer.group(1).replace(",", "")) if xfer else 0
            uf = unas_files()
            ok = bool(xfer) and (not err or err.group(1) == "23")
            record(ok, nfiles)
            head = ":white_check_mark: *SSH backup repair complete*" if ok else ":warning: *SSH backup repair finished with errors*"
            gb = (int(size.group(1).replace(",", "")) / 1e9) if size else 0
            extra = f" (rsync code {err.group(1)})" if err else ""
            slack(f"{head}{extra}\n• files transferred: {nfiles:,} ({gb:.1f} GB)\n• UNAS nas now holds {uf:,} files" if uf else
                  f"{head}{extra}\n• files transferred: {nfiles:,} ({gb:.1f} GB)")
            return
        if time.time() - last_hb >= HEARTBEAT_S:
            last_hb = time.time()
            uf = unas_files()
            slack(f":hourglass_flowing_sand: SSH backup repair running ({(time.time()-t0)/3600:.1f}h). "
                  f"UNAS nas file count: {uf:,}" if uf else
                  f":hourglass_flowing_sand: SSH backup repair running ({(time.time()-t0)/3600:.1f}h).")
    slack(":x: SSH backup-repair watcher hit its 14h limit — check nova_direct_rsync.log on the Synology.")


if __name__ == "__main__":
    main()
