#!/usr/bin/env python3
"""nova_yt_backfill.py [N] — capture the most recent N (default 10) videos from each
fishbowl YouTube channel, SEQUENTIALLY (one mlx_whisper at a time — GPU-friendly).

Reuses the watcher's channel list + capture worker. Already-ingested videos are
skipped. Live ones are recorded from start; the rest are downloaded as VODs. Posts
progress to #nova-info. Run once (nohup); going-forward is handled by the watcher.
"""
import subprocess
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_yt_ingest_watch import CHANNELS, recent_ids, vid_live_status, PY, CAPTURE

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
ONLY = next((a for a in sys.argv[1:] if not a.isdigit()), None)  # optional channel-key filter


def log(m):
    print(f"[yt-backfill] {m}", flush=True)


def slack(m):
    try:
        nova_config.post_both(m, slack_channel=nova_config.SLACK_FEED, discord_channel=None)
    except Exception as e:
        log(f"slack: {e}")


def main():
    conn = psycopg2.connect(DSN); conn.autocommit = True
    cur = conn.cursor()
    slack(f":rewind: *Fishbowl YT backfill started* — capturing the most recent {N} videos "
          f"from each of the {len(CHANNELS)} channels (sequential, transcribe + chat). Reporting here.")
    done = failed = skipped = 0
    for ch in CHANNELS:
        if ONLY and ch["key"] != ONLY:
            continue
        vids = recent_ids(ch["url"])[:N]
        log(f"{ch['key']}: {len(vids)} candidates")
        for vid, _lsflat, title in vids:
            cur.execute("SELECT status FROM yt_ingest_seen WHERE channel=%s AND video_id=%s",
                        (ch["key"], vid))
            row = cur.fetchone()
            if row and row[0] in ("ingested", "capturing", "recording"):
                skipped += 1
                continue
            ls = vid_live_status(vid)
            if ls == "is_upcoming":
                continue
            live = (ls == "is_live")
            cur.execute("INSERT INTO yt_ingest_seen (channel,video_id,title,status) "
                        "VALUES (%s,%s,%s,'capturing') ON CONFLICT (channel,video_id) "
                        "DO UPDATE SET status='capturing'", (ch["key"], vid, title[:200]))
            slack(f":arrow_down: backfill: {ch['key']} — {title[:60]}")
            log(f"capturing {vid} ({'live' if live else 'vod'}) {title[:50]}")
            args = [PY, CAPTURE, vid, ch["vector"]] + (["--live"] if live else [])
            try:
                subprocess.run(args, timeout=12 * 3600)
                cur.execute("SELECT status FROM yt_ingest_seen WHERE channel=%s AND video_id=%s",
                            (ch["key"], vid))
                st = (cur.fetchone() or [""])[0]
                if st == "ingested":
                    done += 1
                else:
                    failed += 1
            except Exception as e:
                log(f"capture failed {vid}: {e}"); failed += 1
                cur.execute("UPDATE yt_ingest_seen SET status='failed' WHERE channel=%s AND video_id=%s",
                            (ch["key"], vid))
    slack(f":checkered_flag: *Fishbowl YT backfill complete* — {done} captured, {failed} failed/empty, "
          f"{skipped} already had. (Deleted/unavailable streams are expected to fail.)")
    conn.close()


if __name__ == "__main__":
    main()
