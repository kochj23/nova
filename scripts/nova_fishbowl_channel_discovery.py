#!/usr/bin/env python3
"""nova_fishbowl_channel_discovery.py — surface candidate NEW Fishbowl channels from
who's actually showing up in the chat/superchats of channels we already track.

nova_yt_capture.py tallies every commenter's channel_id (not just their display name —
"searching for Uzi is fruitless on YT" is exactly the problem a channel_id avoids) into
nova_ops.fishbowl_commenters. This script:
  1. Resolves the tracked CHANNELS list (nova_yt_ingest_watch.py) to their real channel IDs
     (cached so we only hit yt-dlp once per tracked channel).
  2. Finds commenters NOT in that tracked set, active enough to be worth a look
     (>=5 messages or >=1 superchat), not yet reviewed.
  3. Resolves each candidate's channel via yt-dlp for a real name + recent upload titles,
     posts a digest to nova-info, and marks them reviewed so they don't repeat daily.

Scheduled daily. Never auto-adds a channel — this surfaces candidates for a human/Nova
call, per Jordan's ask ("find their YT channel... send it to nova-info").
"""
import re
import subprocess
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_yt_ingest_watch import CHANNELS

DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
YTDLP = "/opt/homebrew/bin/yt-dlp"
MIN_MESSAGES = 5
MIN_SUPERCHATS = 1
MAX_CANDIDATES_PER_RUN = 15

# Loose keyword signal for "does this channel look watch/fishbowl-relevant" — not a
# verdict, just context to help the human call surfaced alongside the raw stats.
RELEVANCE_KEYWORDS = (
    "watch", "rolex", "ad ", "grey market", "gray market", "horology", "timepiece",
    "submariner", "daytona", "patek", "audemars", "vintage watch", "dealer",
)


def log(m):
    print(f"[fishbowl-discovery] {m}", flush=True)


def _db():
    c = psycopg2.connect(DSN); c.autocommit = True
    return c


def ensure_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS fishbowl_tracked_channel_ids (
        handle_key text PRIMARY KEY, channel_id text, resolved_at timestamptz DEFAULT now())""")


def resolve_channel_id(url_or_id):
    """yt-dlp -> the channel's canonical UC... id. Works for both handle URLs and IDs."""
    try:
        r = subprocess.run(
            [YTDLP, "--flat-playlist", "--no-warnings", "-I", "1:1",
             "--print", "%(channel_id)s", url_or_id],
            capture_output=True, text=True, timeout=60)
        out = r.stdout.strip().splitlines()
        return out[0].strip() if out else None
    except Exception as e:
        log(f"resolve failed for {url_or_id}: {e}")
        return None


def tracked_channel_ids(cur):
    """The channel IDs we already watch, resolving+caching any tracked handle not yet seen."""
    cur.execute("SELECT handle_key FROM fishbowl_tracked_channel_ids")
    cached = {r[0] for r in cur.fetchall()}
    for ch in CHANNELS:
        if ch["key"] in cached:
            continue
        cid = resolve_channel_id(ch["url"])
        if cid:
            cur.execute(
                "INSERT INTO fishbowl_tracked_channel_ids (handle_key, channel_id) VALUES (%s,%s) "
                "ON CONFLICT (handle_key) DO UPDATE SET channel_id=EXCLUDED.channel_id, resolved_at=now()",
                (ch["key"], cid))
            log(f"resolved tracked channel {ch['key']} -> {cid}")
    cur.execute("SELECT channel_id FROM fishbowl_tracked_channel_ids WHERE channel_id IS NOT NULL")
    return {r[0] for r in cur.fetchall()}


def channel_info(channel_id):
    """Real channel name + a few recent upload titles, for the relevance heuristic."""
    url = f"https://www.youtube.com/channel/{channel_id}"
    try:
        r = subprocess.run(
            [YTDLP, "--flat-playlist", "--no-warnings", "-I", "1:5",
             "--print", "%(channel)s\t%(title)s", f"{url}/videos"],
            capture_output=True, text=True, timeout=60)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        if not lines:
            return None, []
        name = lines[0].split("\t", 1)[0].strip()
        titles = [ln.split("\t", 1)[1].strip() for ln in lines if "\t" in ln]
        return name, titles
    except Exception as e:
        log(f"channel_info failed for {channel_id}: {e}")
        return None, []


def looks_relevant(titles):
    blob = " ".join(titles).lower()
    return any(kw in blob for kw in RELEVANCE_KEYWORDS)


def main():
    conn = _db(); cur = conn.cursor()
    ensure_tables(cur)
    tracked = tracked_channel_ids(cur)
    log(f"{len(tracked)} tracked channel IDs resolved")

    cur.execute("""
        SELECT channel_id, display_name, message_count, superchat_count, superchat_total, source_channels
        FROM fishbowl_commenters
        WHERE NOT reviewed AND channel_id != ALL(%s)
          AND (message_count >= %s OR superchat_count >= %s)
        ORDER BY (message_count + superchat_count * 10) DESC
        LIMIT %s
    """, (list(tracked), MIN_MESSAGES, MIN_SUPERCHATS, MAX_CANDIDATES_PER_RUN))
    candidates = cur.fetchall()

    if not candidates:
        log("no new candidates this run")
        cur.close(); conn.close()
        return

    lines = [f":mag: *Fishbowl channel discovery* — {len(candidates)} new candidate(s) from stream chat/superchats:\n"]
    reviewed_ids = []
    for cid, name, msgs, sc_count, sc_total, sources in candidates:
        real_name, titles = channel_info(cid)
        display = real_name or name or "?"
        relevant = looks_relevant(titles)
        url = f"https://www.youtube.com/channel/{cid}"
        stat = f"{msgs} msg" + (f", {sc_count} superchat(s) (${sc_total:.0f})" if sc_count else "")
        flag = ":large_green_circle: looks relevant" if relevant else ":white_circle: unclear — check manually"
        lines.append(f"• <{url}|{display}> — {stat} — seen on {', '.join(sources)} — {flag}")
        reviewed_ids.append(cid)

    nova_config.post_both("\n".join(lines), slack_channel=nova_config.SLACK_INFO)
    cur.execute("UPDATE fishbowl_commenters SET reviewed = true WHERE channel_id = ANY(%s)", (reviewed_ids,))
    log(f"posted {len(candidates)} candidates to nova-info, marked reviewed")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
