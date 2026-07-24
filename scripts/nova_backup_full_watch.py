#!/usr/bin/env python3
"""nova_backup_full_watch.py — watch a manually-triggered Synology FULL backup and
post periodic status to #nova-info, then a final summary.

The agent (nova_backup_agent.sh full) runs nas then external, writing one
telemetry.backup_runs row per job at completion. We poll those rows: heartbeat
hourly, and when the 'external:full' row (the last job) lands we post the final
result and exit. Pure DB polling — no fragile SSH progress parsing.

Usage: nova_backup_full_watch.py <start_epoch>
Written by Jordan Koch (via Claude).
"""
import sys
import time
from datetime import datetime, timezone

import psycopg2

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import nova_config

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
HEARTBEAT_S = 3600          # post a "still running" line hourly
POLL_S = 600                # check for completion every 10 min
MAX_RUNTIME_S = 20 * 3600   # safety: give up after 20h


def slack(msg):
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_INFO, discord_channel=None)
    except Exception as e:
        print(f"slack failed: {e}", flush=True)


def rows_since(start_dt):
    c = psycopg2.connect(DSN); c.autocommit = True
    with c.cursor() as cur:
        cur.execute(
            "SELECT job, rc, ok, files, bytes, elapsed_s FROM telemetry.backup_runs "
            "WHERE ts > %s AND job LIKE 'nova-backup:%%:full' ORDER BY ts", (start_dt,))
        r = cur.fetchall()
    c.close()
    return r


def fmt(r):
    job, rc, ok, files, by, el = r
    name = job.split(":")[1]
    gb = (by or 0) / 1e9
    return f"{name}: {'✅ ok' if ok else f'❌ rc={rc}'} ({files:,} files, {gb:.1f} GB, {el//60}m)"


def main():
    start_epoch = float(sys.argv[1])
    start_dt = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
    slack(":arrows_counterclockwise: *Full backup started* (Synology→UNAS, nas + external). "
          "Validating the new `*.app` exclude fix. I'll report progress here and a final summary.")
    last_hb = time.time()
    seen = set()
    while time.time() - start_epoch < MAX_RUNTIME_S:
        time.sleep(POLL_S)
        done = rows_since(start_dt)
        for r in done:
            if r[0] not in seen:
                seen.add(r[0])
                slack(f":package: Backup job finished — {fmt(r)}")
        # external is the last job; once it lands we're done
        if any(r[0].endswith("external:full") for r in done):
            ok_all = all(r[2] for r in done)
            hrs = (time.time() - start_epoch) / 3600
            head = ":white_check_mark: *Full backup complete*" if ok_all else ":warning: *Full backup finished with errors*"
            slack(f"{head} in {hrs:.1f}h\n" + "\n".join("• " + fmt(r) for r in done))
            return
        if time.time() - last_hb >= HEARTBEAT_S:
            last_hb = time.time()
            hrs = (time.time() - start_epoch) / 3600
            donen = ", ".join(r[0].split(":")[1] for r in done) or "none yet"
            slack(f":hourglass_flowing_sand: Full backup still running ({hrs:.1f}h elapsed). Jobs done so far: {donen}.")
    slack(":x: Full-backup watcher hit its 20h limit without seeing external:full complete — check the Synology.")


if __name__ == "__main__":
    main()
