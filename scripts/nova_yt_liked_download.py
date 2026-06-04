#!/usr/bin/env python3
"""
nova_yt_liked_download.py — Downloads YouTube liked videos to /Volumes/external/videos/Liked/

Runs every Saturday at 1am via scheduler. Downloads 0-4 videos at a time with
random 60-188.5s delays until all liked videos are downloaded or timeout.
Sends progress notifications every 5 minutes to #nova-notifications.
Skips videos already present in TVShows or Liked directories.

Written by Jordan Koch.
"""

import json
import math
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

LIKED_DIR       = Path("/Volumes/external/videos/Liked")
MUSIC_DIR       = Path("/Volumes/external/music/YouTube")
TVSHOWS_DIR     = Path("/Volumes/external/videos/TVShows")
YT_DLP          = "/opt/homebrew/bin/yt-dlp"
FFPROBE         = "/opt/homebrew/bin/ffprobe"
YT_COOKIES_FILE = Path.home() / ".openclaw/cache/yt_cookies.txt"
LOG_FILE        = Path.home() / ".openclaw/logs/nova_yt_liked_download.log"
STATE_FILE      = Path.home() / ".openclaw/cache/yt_liked_state.json"
SLACK           = "#nova-notifications"

DELAY_MIN       = 60             # 1 minute
DELAY_MAX       = math.pi * 60  # pi minutes (~188.5s)
BATCH_MIN       = 0
BATCH_MAX       = 4

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v"}
MUSIC_UPLOADERS = {"vevo", "- topic", "official", "records", "music"}



def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[yt-liked {ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(text: str):
    try:
        nova_config.post_both(text, slack_channel=SLACK)
    except Exception as e:
        log(f"Slack error: {e}")


def sanitize(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', '', s)
    s = s.encode('ascii', errors='ignore').decode('ascii')
    s = re.sub(r'[^\x20-\x7E]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    if not s or len(s) < 3:
        s = "untitled"
    return s[:150]


def _cookies_args() -> list:
    if YT_COOKIES_FILE.exists():
        age_hours = (time.time() - YT_COOKIES_FILE.stat().st_mtime) / 3600
        if age_hours < 6:
            return ["--cookies", str(YT_COOKIES_FILE)]
    return ["--cookies-from-browser", "chrome"]


def fetch_metadata(vid_id: str) -> dict:
    """Fetch full metadata for a video to detect music and get ID3 info."""
    try:
        r = subprocess.run(
            [YT_DLP, *_cookies_args(), "--dump-json", "--no-download",
             "--no-playlist", f"https://www.youtube.com/watch?v={vid_id}"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            return json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        log(f"  Metadata fetch failed: {e}")
    return {}


def is_music(meta: dict) -> bool:
    """Detect if a video is music based on category or uploader patterns."""
    categories = [c.lower() for c in (meta.get("categories") or [])]
    if "music" in categories:
        return True
    uploader = (meta.get("uploader") or "").lower()
    channel = (meta.get("channel") or "").lower()
    for pattern in MUSIC_UPLOADERS:
        if pattern in uploader or pattern in channel:
            return True
    return False


def download_music(vid_id: str, meta: dict) -> str:
    """Download audio only, convert to 256kbps MP3, apply ID3 tags."""
    artist = meta.get("artist") or meta.get("uploader") or "Unknown Artist"
    track = meta.get("track") or meta.get("title") or "Unknown Track"
    album = meta.get("album") or ""
    year = str(meta.get("release_year") or meta.get("upload_date", "")[:4] or "")

    safe_artist = sanitize(artist)
    safe_track = sanitize(track)
    out_path = MUSIC_DIR / f"{safe_artist} - {safe_track}.mp3"

    if out_path.exists():
        return "skip"

    MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        YT_DLP,
        *_cookies_args(),
        "--extractor-args", "youtube:player_client=web,default",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "256K",
        "--embed-thumbnail",
        "--embed-metadata",
        "--no-overwrites",
        "--no-playlist",
        "--windows-filenames",
        "-o", str(out_path),
        f"https://www.youtube.com/watch?v={vid_id}",
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        if "already been downloaded" in r.stdout:
            return "skip"
        if "members" in r.stderr.lower() or "join this channel" in r.stderr.lower():
            return "members-only"
        return f"error: {r.stderr[-300:]}"

    _apply_id3_tags(out_path, artist=artist, track=track, album=album, year=year)
    log(f"  ♪ {safe_artist} - {safe_track} (MP3 256kbps)")
    return "music"


def _apply_id3_tags(path: Path, artist: str, track: str, album: str, year: str):
    """Ensure ID3 tags are set correctly via mutagen (fills gaps yt-dlp missed)."""
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, TPE1, TIT2, TALB, TDRC, ID3NoHeaderError
    except ImportError:
        log("  mutagen not available — skipping ID3 tag pass")
        return

    try:
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()

        if artist and not tags.get("TPE1"):
            tags.add(TPE1(encoding=3, text=[artist]))
        if track and not tags.get("TIT2"):
            tags.add(TIT2(encoding=3, text=[track]))
        if album and not tags.get("TALB"):
            tags.add(TALB(encoding=3, text=[album]))
        if year and not tags.get("TDRC"):
            tags.add(TDRC(encoding=3, text=[year]))

        tags.save(str(path))
    except Exception as e:
        log(f"  ID3 tag error (non-fatal): {e}")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"downloaded": [], "skipped": [], "failed": []}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_liked_videos() -> list:
    """Fetch full liked videos playlist."""
    log("Fetching liked videos list...")
    r = subprocess.run(
        [YT_DLP, *_cookies_args(), "--flat-playlist",
         "--print", "%(id)s\t%(title)s\t%(uploader)s",
         "https://www.youtube.com/playlist?list=LL"],
        capture_output=True, text=True, timeout=120,
    )
    videos = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2:
            videos.append({
                "id": parts[0],
                "title": parts[1],
                "uploader": parts[2] if len(parts) > 2 else "Unknown",
            })
    log(f"Found {len(videos)} liked videos")
    return videos


_TVSHOWS_INDEX = None

def _build_tvshows_index() -> set:
    """Build a set of filenames in TVShows (one-time scan, cached)."""
    global _TVSHOWS_INDEX
    if _TVSHOWS_INDEX is not None:
        return _TVSHOWS_INDEX
    log("Building TVShows filename index (one-time)...")
    names = set()
    try:
        result = subprocess.run(
            ["find", str(TVSHOWS_DIR), "-type", "f", "-name", "*.mp4", "-o",
             "-name", "*.mkv", "-o", "-name", "*.avi"],
            capture_output=True, text=True, timeout=60
        )
        for line in result.stdout.splitlines():
            names.add(Path(line).name.lower())
    except Exception as e:
        log(f"TVShows index build failed: {e}")
    _TVSHOWS_INDEX = names
    log(f"TVShows index: {len(names)} files")
    return _TVSHOWS_INDEX


def is_already_downloaded(vid_id: str, title: str, state: dict) -> bool:
    """Check if video is already in Liked dir, TVShows, or state."""
    if vid_id in state["downloaded"] or vid_id in state["skipped"]:
        return True

    safe_title = sanitize(title).lower()

    # Check Liked directory (small, fast)
    for f in LIKED_DIR.iterdir():
        if f.suffix.lower() in VIDEO_EXTS:
            if vid_id in f.name or safe_title[:30] in f.stem.lower():
                return True

    # Check Music directory
    if MUSIC_DIR.exists():
        for f in MUSIC_DIR.iterdir():
            if f.suffix.lower() == ".mp3":
                if vid_id in f.name or safe_title[:30] in f.stem.lower():
                    return True

    # Check TVShows via cached index (filename match only)
    tvshows = _build_tvshows_index()
    for name in tvshows:
        if vid_id in name:
            return True

    return False


def download_video(vid_id: str, title: str) -> str:
    safe_title = sanitize(title)
    out_path = LIKED_DIR / f"{safe_title}.mp4"

    if out_path.exists():
        return "skip"

    cmd = [
        YT_DLP,
        *_cookies_args(),
        "--extractor-args", "youtube:player_client=web,default",
        "-f", "bestvideo[height<=540]+bestaudio/best[height<=540]",
        "--audio-quality", "0",
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        "--no-overwrites",
        "--no-playlist",
        "--windows-filenames",
        f"https://www.youtube.com/watch?v={vid_id}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        if "already been downloaded" in r.stdout:
            return "skip"
        if "members" in r.stderr.lower() or "join this channel" in r.stderr.lower():
            return "members-only"
        return f"error: {r.stderr[-300:]}"
    return "ok"


def main():
    log("=== YouTube Liked Videos downloader started ===")
    notify(":heart: *YouTube Liked Videos Download* starting — running continuously")

    LIKED_DIR.mkdir(parents=True, exist_ok=True)
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    # Stats for notifications
    session_downloaded = []
    session_skipped = 0
    session_errors = 0
    last_notify = time.time()
    last_fetch = 0

    liked_videos = []

    while True:
        # Re-fetch liked list every 30 minutes to catch new likes
        if time.time() - last_fetch > 1800 or not liked_videos:
            liked_videos = get_liked_videos()
            last_fetch = time.time()
            # Filter out already-done videos
            pending = [v for v in liked_videos if not is_already_downloaded(v["id"], v["title"], state)]
            log(f"{len(pending)} videos remaining to download")
            if not pending:
                log("All liked videos downloaded. Exiting.")
                notify(":white_check_mark: *Liked Videos* — all caught up! Run complete.")
                break

        # Pick a random batch size (0-4)
        batch_size = random.randint(BATCH_MIN, BATCH_MAX)
        if batch_size == 0:
            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            log(f"Batch size 0 — waiting {delay:.0f}s")
            time.sleep(delay)
            # Check if we should send a notification
            if time.time() - last_notify >= 300:
                _send_progress(session_downloaded, session_skipped, session_errors, pending)
                last_notify = time.time()
            continue

        # Grab batch_size videos from pending (random selection for variety)
        pending = [v for v in liked_videos if not is_already_downloaded(v["id"], v["title"], state)]
        if not pending:
            continue

        batch = random.sample(pending, min(batch_size, len(pending)))

        for video in batch:
            vid_id = video["id"]
            title = video["title"]
            uploader = video.get("uploader", "Unknown")

            log(f"  ↓ {title[:70]} ({uploader})")

            meta = fetch_metadata(vid_id)
            if meta and is_music(meta):
                result = download_music(vid_id, meta)
            else:
                result = download_video(vid_id, title)

            if result in ("ok", "music"):
                label = "♪" if result == "music" else "✓"
                log(f"  {label} {title[:60]}")
                state["downloaded"].append(vid_id)
                session_downloaded.append(f"{uploader} — {title[:50]}")
            elif result == "skip":
                state["skipped"].append(vid_id)
                session_skipped += 1
            elif result == "members-only":
                log(f"  ⊘ members-only — skipping permanently")
                state["skipped"].append(vid_id)
                session_skipped += 1
            else:
                log(f"  ✗ {result[:100]}")
                state["failed"].append(vid_id)
                session_errors += 1

            save_state(state)

            # Delay between individual downloads within a batch
            if video != batch[-1]:
                inner_delay = random.uniform(30, 90)
                time.sleep(inner_delay)

        # Progress notification every 5 minutes
        if time.time() - last_notify >= 300:
            pending = [v for v in liked_videos if not is_already_downloaded(v["id"], v["title"], state)]
            _send_progress(session_downloaded, session_skipped, session_errors, pending)
            last_notify = time.time()

        # Delay between batches
        delay = random.uniform(DELAY_MIN, DELAY_MAX)
        log(f"Batch complete — waiting {delay:.0f}s")
        time.sleep(delay)


def _send_progress(downloaded: list, skipped: int, errors: int, pending: list):
    lines = [f":heart: *Liked Videos Progress* — {len(downloaded)} downloaded, {len(pending)} remaining"]
    if downloaded:
        recent = downloaded[-5:]
        for d in recent:
            lines.append(f"  • {d}")
        if len(downloaded) > 5:
            lines.append(f"  _...and {len(downloaded) - 5} more this session_")
    if skipped:
        lines.append(f"  :fast_forward: {skipped} skipped (already downloaded or members-only)")
    if errors:
        lines.append(f"  :warning: {errors} errors")
    notify("\n".join(lines))


if __name__ == "__main__":
    main()
