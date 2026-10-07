#!/usr/bin/env python3
"""nova_yt_ingest_watch.py — watch YouTube channels for NEW videos and ingest them
(transcribe -> Nova's vector memory) WITHOUT subscribing.

Uses yt-dlp --flat-playlist (no auth, no subscription) to list each channel's most
recent uploads/streams, tracks what's been seen in PG (nova_ops.yt_ingest_seen), and
runs nova_ingest.py video for anything new. On a channel's FIRST run it SEEDS the
current videos as "seen" without ingesting, so only videos that appear AFTER setup
get pulled in (this is a *watch*, not a backfill).

State in PostgreSQL per house rules. Scheduled via launchd (every 6h).
"""
import os
import subprocess
import sys
from pathlib import Path

import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
YTDLP = "/opt/homebrew/bin/yt-dlp"
PY = "/opt/homebrew/bin/python3"
INGEST = str(Path(__file__).parent / "nova_ingest.py")
RECENT = 12  # how many recent videos to check per channel per run

# Channels to watch. vector = nova_memories source. These are reality-TV / "fishbowl"
# livestream commentary channels (added 2026-06-30; Jordan does NOT want to subscribe).
CHANNELS = [
    {"key": "watchnicholas_live1", "url": "https://www.youtube.com/@WatchNicholasLivestream1/streams", "vector": "fishbowl"},
    {"key": "watchnicholas_streams", "url": "https://www.youtube.com/@watchnicholasstreams/streams", "vector": "fishbowl"},
    {"key": "thefranchiseclub", "url": "https://www.youtube.com/@TheFranchiseClub/streams", "vector": "fishbowl"},
    {"key": "archieluxury", "url": "https://www.youtube.com/@ARCHIELUXURY/streams", "vector": "fishbowl"},
    {"key": "marcelotime", "url": "https://www.youtube.com/@Marcelotime", "vector": "fishbowl"},
    {"key": "thefranchiseclubs", "url": "https://www.youtube.com/@theFranchiseClubs", "vector": "fishbowl"},
    {"key": "oisinomalley", "url": "https://www.youtube.com/@oisinomalley", "vector": "fishbowl"},
    {"key": "watchreporter", "url": "https://www.youtube.com/@WatchReporter", "vector": "fishbowl"},
    {"key": "watchtrapper", "url": "https://www.youtube.com/@Watchtrapper", "vector": "fishbowl"},
    {"key": "archieluxury_live", "url": "https://www.youtube.com/@ArchieLuxuryLivestream/streams", "vector": "fishbowl"},
    {"key": "blondeetime", "url": "https://www.youtube.com/@BlondeeTime/streams", "vector": "fishbowl"},
    {"key": "timwrite", "url": "https://www.youtube.com/@TimWrite", "vector": "fishbowl"},
    {"key": "watchhangout", "url": "https://www.youtube.com/@WatchHangout/streams", "vector": "horology"},
    {"key": "mookieverse", "url": "https://www.youtube.com/@themookieverse/streams", "vector": "fishbowl"},
    {"key": "escapementshow", "url": "https://www.youtube.com/@theescapementshow/streams", "vector": "horology"},
    {"key": "angelic_slayer", "url": "https://www.youtube.com/@angelic_slayer/streams", "vector": "fishbowl"},
    {"key": "wristchick", "url": "https://www.youtube.com/@TheWristChick/videos", "vector": "horology"},
    {"key": "redshovel", "url": "https://www.youtube.com/@redshovel/streams", "vector": "fishbowl"},
    {"key": "doxxreport", "url": "https://www.youtube.com/@doxxreport/streams", "vector": "fishbowl"},
    {"key": "oisinomalley_live", "url": "https://www.youtube.com/@oisinomalleylive/streams", "vector": "fishbowl"},
    {"key": "paulthorpe", "url": "https://www.youtube.com/@PaulThorpeOfficial/streams", "vector": "fishbowl"},
    {"key": "bearclooney", "url": "https://www.youtube.com/@bearclooneywatches4186/streams", "vector": "fishbowl"},
    {"key": "ac3dungeon", "url": "https://www.youtube.com/@AC3Dungeon/streams", "vector": "fishbowl"},
    {"key": "paulpluta", "url": "https://www.youtube.com/@PaulPlutaPrestige/streams", "vector": "horology"},
    {"key": "mortysdiner", "url": "https://www.youtube.com/@MortysDiner/streams", "vector": "fishbowl"},
    {"key": "theoriginaloc", "url": "https://www.youtube.com/@theoriginaloc/streams", "vector": "fishbowl"},
    {"key": "tpgentleman", "url": "https://www.youtube.com/@Thetimepiecegentleman/videos", "vector": "fishbowl"},
    {"key": "romansharf", "url": "https://www.youtube.com/@RomanSharf/streams", "vector": "horology"},
    {"key": "greymarketpod", "url": "https://www.youtube.com/@greymarketpod/videos", "vector": "horology"},
    {"key": "cramareels", "url": "https://www.youtube.com/@TheCramaReels/streams", "vector": "fishbowl"},
    # ---- Watches and Friends (Jordan 2026-10-06): watch-news channels -> horology; Nico -> fishbowl ----
    {"key": "peterpiccolino", "url": "https://www.youtube.com/@Peterpic/videos", "vector": "horology", "name": "Peter Piccolino"},
    {"key": "luxurybazaar", "url": "https://www.youtube.com/@LuxuryBazaar/videos", "vector": "horology", "name": "Luxury Bazaar"},
    {"key": "the1916company", "url": "https://www.youtube.com/@the1916company/videos", "vector": "horology", "name": "The 1916 Company"},
    {"key": "the1916reviews", "url": "https://www.youtube.com/@the1916companywatchreviews/videos", "vector": "horology", "name": "The 1916 Company Watch Reviews"},
    {"key": "teddyb", "url": "https://www.youtube.com/@TeddyBaldassarre/videos", "vector": "horology", "name": "Teddy Baldassarre"},
    {"key": "thiswatchthatwatch", "url": "https://www.youtube.com/@Mike.thiswatchthatwatch/videos", "vector": "horology", "name": "This Watch, That Watch"},
    {"key": "watchclyde", "url": "https://www.youtube.com/@WatchClyde/videos", "vector": "horology", "name": "Watch Clyde"},
    {"key": "watcheric", "url": "https://www.youtube.com/@WatchEric/videos", "vector": "horology", "name": "Watch Eric"},
    {"key": "andrewmorgan", "url": "https://www.youtube.com/@AndrewMorganWatches/videos", "vector": "horology", "name": "Andrew Morgan Watches"},
    {"key": "apodcastaboutwatches", "url": "https://www.youtube.com/@APodcastAboutWatches/videos", "vector": "horology", "name": "A Podcast About Watches"},
    {"key": "federico", "url": "https://www.youtube.com/@FedericoTalksWatches/videos", "vector": "horology", "name": "Federico Talks Watches"},
    {"key": "watchesofespionage", "url": "https://www.youtube.com/@WatchesofEspionage/videos", "vector": "horology", "name": "Watches of Espionage"},
    {"key": "watchfinder", "url": "https://www.youtube.com/@watchfinder/videos", "vector": "horology", "name": "Watchfinder & Co."},
    {"key": "bobswatches", "url": "https://www.youtube.com/@bobswatches/videos", "vector": "horology", "name": "Bob's Watches - Buy & Sell Rolex"},
    {"key": "chrono24", "url": "https://www.youtube.com/@Chrono24Official/videos", "vector": "horology", "name": "Chrono24"},
    {"key": "brittpearce", "url": "https://www.youtube.com/@BrittPearceWatches/videos", "vector": "horology", "name": "Britt Pearce"},
    {"key": "barkandjack", "url": "https://www.youtube.com/@BarkandJack/videos", "vector": "horology", "name": "Adrian Barker"},
    {"key": "jennielle", "url": "https://www.youtube.com/@JenniElle/videos", "vector": "horology", "name": "Jenni Elle"},
    {"key": "raimond", "url": "https://www.youtube.com/@Raimondirimescu/videos", "vector": "horology", "name": "Raimond Irimescu"},
    {"key": "talkingtimepieces", "url": "https://www.youtube.com/@talkingtimepieceswithtony/videos", "vector": "horology", "name": "Talking Timepieces With Tony"},
    {"key": "wristwatchrevival", "url": "https://www.youtube.com/@WristwatchRevival/videos", "vector": "horology", "name": "Wristwatch Revival"},
    {"key": "watchpro", "url": "https://www.youtube.com/@watchprolive/videos", "vector": "horology", "name": "WatchPro"},
    {"key": "menta", "url": "https://www.youtube.com/@mentawatches/videos", "vector": "horology", "name": "Menta Watches"},
    {"key": "officialwatches", "url": "https://www.youtube.com/@officialwatches1/videos", "vector": "horology", "name": "Official Watches"},
    {"key": "burdeens", "url": "https://www.youtube.com/@BurdeensJewelry/videos", "vector": "horology", "name": "Burdeens Jewelry"},
    {"key": "nicoleonard", "url": "https://www.youtube.com/@NicoLeonard/videos", "vector": "fishbowl", "name": "Nico Leonard"},
    {"key": "nicoleonard_mk2", "url": "https://www.youtube.com/@NicoLeonardMK2/videos", "vector": "fishbowl", "name": "Nico Leonard MK2"},
]


def log(m):
    print(f"[yt-watch] {m}", flush=True)


def _db():
    c = psycopg2.connect(DSN); c.autocommit = True
    return c


def ensure(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS yt_ingest_seen (
        channel text, video_id text, title text, status text DEFAULT 'ingested',
        seen_at timestamptz DEFAULT now(), PRIMARY KEY (channel, video_id))""")


def recent_ids(url):
    """List (video_id, live_status, title) for the most recent RECENT videos.
    live_status: is_live | is_upcoming | was_live | post_live | not_live | None.
    No auth, no subscription (yt-dlp --flat-playlist)."""
    try:
        r = subprocess.run(
            [YTDLP, "--flat-playlist", "--no-warnings", "-I", f"1:{RECENT}",
             "--print", "%(id)s\t%(live_status)s\t%(title)s", url],
            capture_output=True, text=True, timeout=180)
        out = []
        for line in r.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0].strip():
                out.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
        if not out and r.returncode != 0:
            log(f"list failed for {url}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.returncode}")
        return out
    except Exception as e:
        log(f"list error {url}: {e}")
        return []


CAPTURE = str(Path(__file__).parent / "nova_yt_capture.py")


def vid_live_status(vid):
    """Per-video live status (flat-playlist reports 'NA'): is_live | was_live |
    is_upcoming | post_live | not_live."""
    try:
        r = subprocess.run([YTDLP, "--no-warnings", "--print", "%(live_status)s",
                            f"https://www.youtube.com/watch?v={vid}"],
                           capture_output=True, text=True, timeout=90)
        return (r.stdout.strip().splitlines() or [""])[0].strip()
    except Exception:
        return ""


def dispatch(vid, vector, live):
    """Fire the capture worker DETACHED (live recordings run for hours; must not
    block the 15-min poll). nohup+setsid so it survives this process exiting."""
    args = [PY, CAPTURE, vid, vector] + (["--live"] if live else [])
    subprocess.Popen(["/usr/bin/nohup"] + args,
                     stdout=open(os.path.expanduser("~/.openclaw/logs/nova-yt-capture.log"), "a"),
                     stderr=subprocess.STDOUT, start_new_session=True)


def main():
    seed_only = "--seed" in sys.argv
    conn = _db(); cur = conn.cursor(); ensure(cur)
    for ch in CHANNELS:
        cur.execute("SELECT video_id FROM yt_ingest_seen WHERE channel=%s", (ch["key"],))
        seen = {r[0] for r in cur.fetchall()}
        vids = recent_ids(ch["url"])
        if not vids:
            log(f"{ch['key']}: no videos listed (skipping)")
            continue
        first_run = len(seen) == 0
        if first_run or seed_only:
            live_caught = 0
            for v, _ls, t in vids:
                # Seed the back-catalog WITHOUT capturing — EXCEPT anything currently LIVE.
                # These streams get deleted after they end, so "seed and skip" would lose a
                # live-right-now stream forever. Catch live ones even on the first run.
                live_now = (vid_live_status(v) == "is_live")
                status = "queued_live" if live_now else "seeded"
                cur.execute("INSERT INTO yt_ingest_seen (channel,video_id,title,status) "
                            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (ch["key"], v, t[:200], status))
                if live_now:
                    live_caught += 1
                    log(f"  {ch['key']}: seeding but CAPTURING live-now stream {v}  {t[:55]}")
            log(f"{ch['key']}: SEEDED {len(vids)} current videos"
                + (f" (+{live_caught} live-now queued for capture)" if live_caught else "")
                + " — will capture new ones going forward")
            continue
        new = [(v, ls, t) for v, ls, t in vids if v not in seen]
        log(f"{ch['key']}: {len(vids)} listed, {len(new)} new")
        for vid, _lsflat, title in new:
            ls = vid_live_status(vid)              # flat list reports NA; check per-video
            if ls == "is_upcoming":
                continue  # scheduled but not started — no content yet; pick it up once live
            live = (ls == "is_live")
            # enqueue — the launchd capture-runner drains this queue with FDA (so whisper
            # can read the model on /Volumes/Data). Claim now so we don't double-queue.
            cur.execute("INSERT INTO yt_ingest_seen (channel,video_id,title,status) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (channel,video_id) DO NOTHING",
                        (ch["key"], vid, title[:200], "queued_live" if live else "queued"))
            log(f"  queued {'LIVE ' if live else ''}capture {vid}  {title[:60]}")
    conn.close()


if __name__ == "__main__":
    main()
