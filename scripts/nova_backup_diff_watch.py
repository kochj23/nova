#!/usr/bin/env python3
"""nova_backup_diff_watch.py — trigger (or attach to) the FAST diff-based
Synology→UNAS backup, report to #nova-info, and record results to
telemetry.backup_runs so the backup monitor reflects the reconcile.

Completion is detected from the diff LOG (busybox DSM has no pgrep): the script
writes "diff-backup done, overall rc=N" when finished. We snapshot the log
length at start and watch for a new "done" line, then parse the per-job result
lines that follow.

Usage: nova_backup_diff_watch.py [attach]   (attach = don't trigger; watch an
already-running diff)
Written by Jordan Koch (via Claude).
"""
import re
import subprocess
import sys
import time

import psycopg2

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import nova_config

SYNO = "kochj@192.168.1.11"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SYNO]
LOG = "/volume1/homes/kochj/nova_nas_diff.log"
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
POLL_S = 60
HEARTBEAT_S = 900
MAX_S = 4 * 3600


def slack(msg):
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_FEED, discord_channel=None)
    except Exception as e:
        print(f"slack failed: {e}", flush=True)


def ssh(cmd):
    try:
        return subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=60).stdout
    except Exception as e:
        print(f"ssh failed: {e}", flush=True)
        return ""


def log_lines():
    return ssh(f"cat {LOG} 2>/dev/null").splitlines()


def record(name, rc, files):
    ok = rc in (0, 24)
    try:
        c = psycopg2.connect(DSN); c.autocommit = True
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO telemetry.backup_runs (ts,job,rc,elapsed_s,files,bytes,errors,ok) "
                "VALUES (now(),%s,%s,0,%s,0,%s,%s)",
                (f"nova-backup:{name}:diff", rc, files, 0 if ok else 1, ok))
        c.close()
    except Exception as e:
        print(f"telemetry write failed: {e}", flush=True)


def report(new_lines):
    text = "\n".join(new_lines)
    synced = {m.group(1): int(m.group(2)) for m in re.finditer(r"(\w+): src=\d+ dst=\d+ to_sync=(\d+)", text)}
    rcs = {m.group(1): int(m.group(2)) for m in re.finditer(r"(\w+): rsync rc=(\d+)", text)}
    insync = set(re.findall(r"(\w+): already in sync", text))
    overall = re.search(r"diff-backup done, overall rc=(\d+)", text)
    lines = []
    for name in ("nas", "external"):
        if name in insync:
            lines.append(f"• {name}: ✅ already in sync (0 files)"); record(name, 0, 0)
        elif name in rcs:
            rc = rcs[name]; n = synced.get(name, 0); ok = rc in (0, 24)
            lines.append(f"• {name}: {'✅' if ok else '❌'} rc={rc}, {n:,} files synced"); record(name, rc, n)
    ok_all = lines and all("❌" not in l for l in lines)
    head = ":white_check_mark: *Diff backup complete*" if ok_all else ":warning: *Diff backup finished*"
    if overall:
        head += f" (overall rc={overall.group(1)})"
    slack(head + "\n" + ("\n".join(lines) if lines else "(no per-job lines parsed — see nova_nas_diff.log)"))


def main():
    attach = len(sys.argv) > 1 and sys.argv[1] == "attach"
    base = len(log_lines())
    if attach:
        slack(":mag: Attached to the in-progress fast diff backup (Synology→UNAS). Will report the result here.")
    else:
        slack(":arrows_counterclockwise: *Fast diff-based backup started* (Synology→UNAS) — find-both-sides + "
              "targeted rsync, not the 15h full.")
        ssh(f"nohup bash ~/nova_nas_diff_backup.sh >/volume1/homes/kochj/nova_nas_diff.run 2>&1 </dev/null & echo go")
    t0 = time.time(); last_hb = t0
    while time.time() - t0 < MAX_S:
        time.sleep(POLL_S)
        lines = log_lines()
        new = lines[base:]
        if any("diff-backup done" in l for l in new):
            report(new)
            return
        if time.time() - last_hb >= HEARTBEAT_S:
            last_hb = time.time()
            tail = " | ".join(l.split("] ", 1)[-1] for l in new[-2:]) or "scanning…"
            slack(f":hourglass_flowing_sand: Diff backup running ({(time.time()-t0)/60:.0f}m). Latest: {tail[:200]}")
    slack(":x: Diff-backup watcher hit its 4h limit — check nova_nas_diff.log on the Synology.")


if __name__ == "__main__":
    main()
