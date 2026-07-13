#!/usr/bin/env python3
"""
nova_youtube_download.py — Download YouTube channels into TVShows structure.

Downloads videos from YouTube channels/playlists into:
  /Volumes/external/videos/TVShows/<ChannelName>/Season XX/<ChannelName> - SXXEXX - Title.mp4

For channels with playlists (CrashCourse): each playlist = one season.
For channels without (Leno, Vintage Space, Cammisa): season by upload year.

Usage:
  python3 nova_youtube_download.py           # Run all channels
  python3 nova_youtube_download.py --channel crashcourse
  python3 nova_youtube_download.py --channel leno
  python3 nova_youtube_download.py --channel vintagespace
  python3 nova_youtube_download.py --channel cammisa

Written by Jordan Koch.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    import nova_config
    HAS_NOVA_CONFIG = True
except ImportError:
    HAS_NOVA_CONFIG = False

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path("/Volumes/external/videos/TVShows")
YT_DLP = "/opt/homebrew/bin/yt-dlp"
DELAY_BETWEEN_VIDEOS = 32
MAX_RESOLUTION = "720"
STATUS_INTERVAL = 300

CHANNELS = {
    "crashcourse": {
        "name": "CrashCourse",
        "url": "https://www.youtube.com/@crashcourse",
        "mode": "playlists",
    },
    "leno": {
        "name": "Jay Leno's Garage",
        "url": "https://www.youtube.com/@jaylenosgarage",
        "mode": "year",
    },
    "vintagespace": {
        "name": "The Vintage Space",
        "url": "https://www.youtube.com/@AmyShiraTeitel",
        "mode": "year",
    },
    "cammisa": {
        "name": "Jason Cammisa",
        "url": "https://www.youtube.com/@jasoncammisa",
        "mode": "year",
    },
}

shutdown = False
stats = {}


def signal_handler(sig, frame):
    global shutdown
    shutdown = True
    log("Shutdown requested, finishing current downloads...")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[yt-dl {ts}] {msg}", flush=True)


def notify(text):
    if HAS_NOVA_CONFIG:
        try:
            nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
        except Exception:
            pass


def sanitize_filename(s):
    s = re.sub(r'[<>:"/\\|?*]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:120]


def get_playlists(channel_url):
    result = subprocess.run(
        [YT_DLP, "--flat-playlist", "--print", "%(url)s\t%(title)s",
         f"{channel_url}/playlists"],
        capture_output=True, text=True, timeout=120,
    )
    playlists = []
    for line in result.stdout.strip().split("\n"):
        if "\t" in line:
            url, title = line.split("\t", 1)
            if "extra curricular" not in title.lower() and "best of" not in title.lower():
                playlists.append({"url": url, "title": title})
    return playlists


def get_playlist_videos(playlist_url):
    result = subprocess.run(
        [YT_DLP, "--flat-playlist", "--print", "%(id)s\t%(title)s",
         playlist_url],
        capture_output=True, text=True, timeout=120,
    )
    videos = []
    for line in result.stdout.strip().split("\n"):
        if "\t" in line:
            vid_id, title = line.split("\t", 1)
            videos.append({"id": vid_id, "title": title})
    return videos


def get_channel_videos(channel_url):
    result = subprocess.run(
        [YT_DLP, "--flat-playlist", "--print", "%(id)s\t%(title)s\t%(upload_date)s",
         f"{channel_url}/videos"],
        capture_output=True, text=True, timeout=300,
    )
    videos = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) >= 2:
            vid_id = parts[0]
            title = parts[1]
            upload_date = parts[2] if len(parts) > 2 and parts[2] != "NA" else ""
            videos.append({"id": vid_id, "title": title, "upload_date": upload_date})
    return videos


def download_video(vid_id, output_path):
    cmd = [
        YT_DLP,
        "-f", f"bestvideo[height<={MAX_RESOLUTION}]+bestaudio/best[height<={MAX_RESOLUTION}]",
        "--merge-output-format", "mp4",
        "-o", str(output_path),
        "--no-overwrites",
        "--no-playlist",
        f"https://www.youtube.com/watch?v={vid_id}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        if "already been downloaded" in result.stdout or "has already been recorded" in result.stdout:
            return "skip"
        return f"error: {result.stderr[-200:]}"
    return "ok"


def process_channel_playlists(channel_key, channel_cfg):
    channel_name = channel_cfg["name"]
    channel_dir = BASE_DIR / channel_name
    channel_dir.mkdir(parents=True, exist_ok=True)

    log(f"[{channel_name}] Fetching playlists...")
    playlists = get_playlists(channel_cfg["url"])
    log(f"[{channel_name}] Found {len(playlists)} playlists")

    stats[channel_key] = {"total": 0, "downloaded": 0, "skipped": 0, "errors": 0, "name": channel_name}

    for season_num, playlist in enumerate(playlists, 1):
        if shutdown:
            break

        season_name = f"Season {season_num:02d}"
        season_dir = channel_dir / season_name
        season_dir.mkdir(parents=True, exist_ok=True)

        # Write a season metadata file
        meta_file = season_dir / ".season_info.json"
        if not meta_file.exists():
            meta_file.write_text(json.dumps({
                "playlist_title": playlist["title"],
                "playlist_url": playlist["url"],
                "season_number": season_num,
            }, indent=2))

        videos = get_playlist_videos(playlist["url"])
        log(f"[{channel_name}] Season {season_num:02d} ({playlist['title']}): {len(videos)} videos")
        stats[channel_key]["total"] += len(videos)

        for ep_num, video in enumerate(videos, 1):
            if shutdown:
                break

            title_safe = sanitize_filename(video["title"])
            filename = f"{channel_name} - S{season_num:02d}E{ep_num:02d} - {title_safe}.mp4"
            output_path = season_dir / filename

            stats[channel_key]["current_season"] = f"S{season_num:02d} — {playlist['title']}"
            stats[channel_key]["current_video"] = f"S{season_num:02d}E{ep_num:02d} — {video['title'][:60]}"
            next_ep = videos[ep_num] if ep_num < len(videos) else None
            stats[channel_key]["next_video"] = f"S{season_num:02d}E{ep_num+1:02d} — {next_ep['title'][:60]}" if next_ep else ""

            if output_path.exists():
                stats[channel_key]["skipped"] += 1
                continue

            result = download_video(video["id"], output_path)
            if result == "ok":
                stats[channel_key]["downloaded"] += 1
                log(f"[{channel_name}] S{season_num:02d}E{ep_num:02d} {title_safe[:50]}")
            elif result == "skip":
                stats[channel_key]["skipped"] += 1
            else:
                stats[channel_key]["errors"] += 1
                log(f"[{channel_name}] ERROR S{season_num:02d}E{ep_num:02d}: {result[:80]}")

            time.sleep(DELAY_BETWEEN_VIDEOS)


def process_channel_by_year(channel_key, channel_cfg):
    channel_name = channel_cfg["name"]
    channel_dir = BASE_DIR / channel_name
    channel_dir.mkdir(parents=True, exist_ok=True)

    log(f"[{channel_name}] Fetching all videos...")
    videos = get_channel_videos(channel_cfg["url"])
    log(f"[{channel_name}] Found {len(videos)} videos")

    stats[channel_key] = {"total": len(videos), "downloaded": 0, "skipped": 0, "errors": 0, "name": channel_name}

    # Group by year, assign season numbers
    years = sorted(set(v["upload_date"][:4] for v in videos if v.get("upload_date") and len(v["upload_date"]) >= 4))
    if not years:
        years = ["01"]

    year_to_season = {year: idx + 1 for idx, year in enumerate(years)}

    # Track episode numbers per season
    season_episode_count = {}

    for vid_idx, video in enumerate(videos):
        if shutdown:
            break

        upload_year = video.get("upload_date", "")[:4]
        if upload_year and upload_year in year_to_season:
            season_num = year_to_season[upload_year]
        else:
            season_num = len(year_to_season) + 1

        season_name = f"Season {season_num:02d}"
        season_dir = channel_dir / season_name
        season_dir.mkdir(parents=True, exist_ok=True)

        # Write year metadata
        meta_file = season_dir / ".season_info.json"
        if not meta_file.exists():
            meta_file.write_text(json.dumps({
                "year": upload_year or "unknown",
                "season_number": season_num,
            }, indent=2))

        season_episode_count.setdefault(season_num, 0)
        season_episode_count[season_num] += 1
        ep_num = season_episode_count[season_num]

        title_safe = sanitize_filename(video["title"])
        filename = f"{channel_name} - S{season_num:02d}E{ep_num:02d} - {title_safe}.mp4"
        output_path = season_dir / filename

        stats[channel_key]["current_season"] = f"S{season_num:02d} — {upload_year or 'unknown'}"
        stats[channel_key]["current_video"] = f"S{season_num:02d}E{ep_num:02d} — {video['title'][:60]}"
        next_vid = videos[vid_idx + 1] if vid_idx + 1 < len(videos) else None
        stats[channel_key]["next_video"] = f"{next_vid['title'][:60]}" if next_vid else ""

        if output_path.exists():
            stats[channel_key]["skipped"] += 1
            continue

        result = download_video(video["id"], output_path)
        if result == "ok":
            stats[channel_key]["downloaded"] += 1
            log(f"[{channel_name}] S{season_num:02d}E{ep_num:02d} {title_safe[:50]}")
        elif result == "skip":
            stats[channel_key]["skipped"] += 1
        else:
            stats[channel_key]["errors"] += 1
            log(f"[{channel_name}] ERROR S{season_num:02d}E{ep_num:02d}: {result[:80]}")

        time.sleep(DELAY_BETWEEN_VIDEOS)


def process_channel(channel_key, channel_cfg):
    try:
        if channel_cfg["mode"] == "playlists":
            process_channel_playlists(channel_key, channel_cfg)
        else:
            process_channel_by_year(channel_key, channel_cfg)
    except Exception as e:
        log(f"[{channel_cfg['name']}] FATAL: {e}")
        stats.setdefault(channel_key, {})["error_msg"] = str(e)


def status_reporter():
    start_time = time.time()
    while not shutdown:
        time.sleep(STATUS_INTERVAL)
        if shutdown:
            break
        elapsed = time.time() - start_time
        elapsed_str = f"{int(elapsed//3600)}h {int((elapsed%3600)//60)}m"
        lines = [f":arrow_down: *YouTube TVShows Download* — {elapsed_str} elapsed"]
        for key, s in stats.items():
            name = s.get("name", key)
            total = s.get("total", 0)
            dl = s.get("downloaded", 0)
            skip = s.get("skipped", 0)
            err = s.get("errors", 0)
            done = dl + skip
            pct = (done / total * 100) if total > 0 else 0
            remaining = total - done
            current = s.get("current_video", "")
            next_vid = s.get("next_video", "")
            season = s.get("current_season", "")
            lines.append(f"\n:gear: *{name}* — {pct:.1f}% ({done}/{total})")
            lines.append(f"  :white_check_mark: Downloaded: {dl} | Skipped: {skip} | Errors: {err}")
            lines.append(f"  :hourglass: Remaining: {remaining}")
            if season:
                lines.append(f"  :file_folder: Season: {season}")
            if current:
                lines.append(f"  :clapper: Just finished: {current}")
            if next_vid:
                lines.append(f"  :arrow_right: Up next: {next_vid}")
        notify("\n".join(lines))


def main():
    import argparse
    import threading

    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=list(CHANNELS.keys()), help="Only download this channel")
    args = parser.parse_args()

    channels_to_process = {args.channel: CHANNELS[args.channel]} if args.channel else CHANNELS

    log(f"Starting download of {len(channels_to_process)} channel(s) in parallel")
    log(f"Target: {BASE_DIR}")
    log(f"Resolution: {MAX_RESOLUTION}p, Delay: {DELAY_BETWEEN_VIDEOS}s between videos")

    notify(
        f":arrow_down: YouTube TVShows Download Starting\n"
        f"• Channels: {', '.join(c['name'] for c in channels_to_process.values())}\n"
        f"• Target: /Volumes/external/videos/TVShows/\n"
        f"• Format: 720p mp4\n"
        f"• Delay: {DELAY_BETWEEN_VIDEOS}s between videos\n"
        f"• Status updates every 5 min"
    )

    # Start status reporter thread
    reporter = threading.Thread(target=status_reporter, daemon=True)
    reporter.start()

    # Run channels in parallel
    with ThreadPoolExecutor(max_workers=len(channels_to_process)) as executor:
        futures = {
            executor.submit(process_channel, key, cfg): key
            for key, cfg in channels_to_process.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                future.result()
                log(f"[{CHANNELS[key]['name']}] Complete")
            except Exception as e:
                log(f"[{CHANNELS[key]['name']}] Failed: {e}")

    # Final report
    lines = [":checkered_flag: YouTube Download Complete"]
    for key, s in stats.items():
        name = s.get("name", key)
        lines.append(f"  • {name}: {s.get('downloaded',0)} new, {s.get('skipped',0)} skipped, {s.get('errors',0)} errors")
    notify("\n".join(lines))
    log("All done.")


if __name__ == "__main__":
    main()
