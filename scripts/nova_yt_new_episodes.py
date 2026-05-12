#!/usr/bin/env python3
"""
nova_yt_new_episodes.py — Weekly new-episode checker for YouTube channels.

Checks all configured YouTube channels for videos not yet on disk,
downloads them into the correct TVShows season directory with proper
SxxExx numbering, and posts a per-file notification to #nova-notifications.

Runs every Monday at 10:15am via scheduler.
66-second delay between downloads.

Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
import nova_media_registry as registry

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR       = Path("/Volumes/external/videos/TVShows")
VIDEO_ROOT     = Path("/Volumes/external/videos")   # full root for non-YT scan
YT_DLP         = "/opt/homebrew/bin/yt-dlp"
CHANNELS_CACHE = Path.home() / ".openclaw/cache/yt_channels.json"
LOG_FILE  = Path.home() / ".openclaw/logs/nova_yt_new_episodes.log"
SLACK     = "#nova-notifications"
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".wmv", ".flv"}

MAX_RESOLUTION      = "720"
DELAY_BETWEEN       = 66   # seconds between downloads
RECENT_VIDEOS_CHECK = 15   # how many recent YT videos to check per channel

# ── Channel registry ──────────────────────────────────────────────────────────
# mode:
#   "single"    — all videos in Season 01, episode numbers are global count
#   "year"      — season = upload year (S01=oldest year, S02=next, etc.)
#   "playlists" — season = playlist (checked separately, not used here for new eps)

CHANNELS = {
    "arnietex": {
        "name": "ArnieTex",
        "url": "https://www.youtube.com/@ArnieTex",
        "mode": "single",
    },
    "meatchurch": {
        "name": "Meat Church BBQ",
        "url": "https://www.youtube.com/@MeatChurchBBQ",
        "mode": "single",
    },
    "robdahm": {
        "name": "Rob Dahm",
        "url": "https://www.youtube.com/@RobDahm",
        "mode": "single",
    },
    "carwizard": {
        "name": "Car Wizard",
        "url": "https://www.youtube.com/@TheCarWizard_",
        "mode": "single",
    },
    "redlettermedia": {
        "name": "Red Letter Media",
        "url": "https://www.youtube.com/@RedLetterMedia",
        "mode": "year",
    },
    "finnegans": {
        "name": "Finnegans Garage",
        "url": "https://www.youtube.com/@FinnegansGarage",
        "mode": "single",
    },
    "freiburger": {
        "name": "David Freiburger",
        "url": "https://www.youtube.com/@DavidFreiburger",
        "mode": "year",
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
    "crashcourse": {
        "name": "CrashCourse",
        "url": "https://www.youtube.com/@crashcourse",
        "mode": "playlists",
    },
    "jakkuh": {
        "name": "jakkuh",
        "url": "https://www.youtube.com/@jakkuh",
        "mode": "single",
    },
    "vintra": {
        "name": "Vin_tra",
        "url": "https://www.youtube.com/@Vin_tra",
        "mode": "single",
    },
    "redlinerebuilds": {
        "name": "Redline Rebuilds",
        "url": "https://www.youtube.com/@RedlineRebuilds",
        "mode": "year",
    },
    "forgottenweapons": {
        "name": "Forgotten Weapons",
        "url": "https://www.youtube.com/@ForgottenWeapons",
        "mode": "single",
    },
    "piccolino": {
        "name": "Peter Piccolino",
        "url": "https://www.youtube.com/@PeterPiccolino",
        "mode": "single",
    },

    # ── Automotive ────────────────────────────────────────────────────────────
    "wheelerdealers": {
        "name": "Wheeler Dealers",
        "url": "https://www.youtube.com/channel/UCOCElZGcmHm3dcNLq_bIZSQ",
        "mode": "single",
    },
    "roadkill": {
        "name": "Roadkill",
        "url": "https://www.youtube.com/@RoadkillShow",
        "mode": "single",
    },
    "hotrodgarage": {
        "name": "Hot Rod Garage",
        "url": "https://www.youtube.com/@HotRodGarage",
        "mode": "single",
    },
    "enginemasters": {
        "name": "Engine Masters",
        "url": "https://www.youtube.com/@EngineMasters",
        "mode": "single",
    },
    "richrebuilds": {
        "name": "Rich Rebuilds",
        "url": "https://www.youtube.com/@RichRebuilds",
        "mode": "single",
    },
    "tavarish": {
        "name": "Tavarish",
        "url": "https://www.youtube.com/@Tavarish",
        "mode": "single",
    },
    "vinwiki": {
        "name": "VINwiki",
        "url": "https://www.youtube.com/@VINwiki",
        "mode": "single",
    },
    "mightycarmods": {
        "name": "Mighty Car Mods",
        "url": "https://www.youtube.com/@MightyCarMods",
        "mode": "single",
    },
    "thesmokingtire": {
        "name": "TheSmokingTire",
        "url": "https://www.youtube.com/@TheSmokingTire",
        "mode": "single",
    },
    "bisforbuild": {
        "name": "B is for Build",
        "url": "https://www.youtube.com/@BisforBuild",
        "mode": "single",
    },
    "cleetusmcfarland": {
        "name": "Cleetus McFarland",
        "url": "https://www.youtube.com/@CleetusMcFarland",
        "mode": "single",
    },
    "hagerty": {
        "name": "Hagerty",
        "url": "https://www.youtube.com/@HagertyDriversFoundation",
        "mode": "single",
    },

    # ── Science / Tech / Engineering ──────────────────────────────────────────
    "tested": {
        "name": "Adam Savages Tested",
        "url": "https://www.youtube.com/@tested",
        "mode": "single",
    },
    "markrober": {
        "name": "Mark Rober",
        "url": "https://www.youtube.com/@markrober",
        "mode": "single",
    },
    "linustechtips": {
        "name": "Linus Tech Tips",
        "url": "https://www.youtube.com/@LinusTechTips",
        "mode": "single",
    },
    "mkbhd": {
        "name": "MKBHD",
        "url": "https://www.youtube.com/@mkbhd",
        "mode": "single",
    },
    "stuffmadehere": {
        "name": "Stuff Made Here",
        "url": "https://www.youtube.com/@StuffMadeHere",
        "mode": "single",
    },
    "scishow": {
        "name": "SciShow",
        "url": "https://www.youtube.com/@SciShow",
        "mode": "single",
    },
    "pbsspacetime": {
        "name": "PBS Space Time",
        "url": "https://www.youtube.com/@pbsspacetime",
        "mode": "single",
    },
    "joescott": {
        "name": "Joe Scott",
        "url": "https://www.youtube.com/@joescott",
        "mode": "single",
    },
    "welchlabs": {
        "name": "Welch Labs",
        "url": "https://www.youtube.com/@WelchLabsVideo",
        "mode": "single",
    },
    "asianometry": {
        "name": "Asianometry",
        "url": "https://www.youtube.com/@Asianometry",
        "mode": "single",
    },

    # ── Military / History ────────────────────────────────────────────────────
    "lazerpig": {
        "name": "LazerPig",
        "url": "https://www.youtube.com/@LazerPig",
        "mode": "single",
    },
    "wardcarroll": {
        "name": "Ward Carroll",
        "url": "https://www.youtube.com/@wardcarroll",
        "mode": "single",
    },
    "militaryaviationhistory": {
        "name": "Military Aviation History",
        "url": "https://www.youtube.com/@MilitaryAviationHistory",
        "mode": "single",
    },
    "ww2stories": {
        "name": "WW2 Stories",
        "url": "https://www.youtube.com/@WW2Stories",
        "mode": "single",
    },
    "historybuffs": {
        "name": "History Buffs",
        "url": "https://www.youtube.com/@HistoryBuffs",
        "mode": "single",
    },
    "biographics": {
        "name": "Biographics",
        "url": "https://www.youtube.com/@Biographics",
        "mode": "single",
    },
    "oversimplified": {
        "name": "OverSimplified",
        "url": "https://www.youtube.com/@OverSimplified",
        "mode": "single",
    },
    "reallifelore": {
        "name": "RealLifeLore",
        "url": "https://www.youtube.com/@RealLifeLore",
        "mode": "single",
    },

    # ── Education / Courses ───────────────────────────────────────────────────
    # Yale Courses excluded — handled by nova_yale_backfill.py (one-time backfill)
    # to prevent the 1000+ episode queue from blocking the nightly job for other channels.

    # ── News / Commentary / Politics ──────────────────────────────────────────
    "lastweektonight": {
        "name": "Last Week Tonight",
        "url": "https://www.youtube.com/@LastWeekTonight",
        "mode": "single",
    },
    "thedailyshow": {
        "name": "The Daily Show",
        "url": "https://www.youtube.com/@thedailyshow",
        "mode": "single",
    },
    "jimmykimmellive": {
        "name": "Jimmy Kimmel Live",
        "url": "https://www.youtube.com/@JimmyKimmelLive",
        "mode": "single",
    },
    "theweeklyshow": {
        "name": "The Weekly Show",
        "url": "https://www.youtube.com/@TheWeeklyShow",
        "mode": "single",
    },
    "podsaveamerica": {
        "name": "Pod Save America",
        "url": "https://www.youtube.com/@PodSaveAmerica",
        "mode": "single",
    },
    "jomboymedia": {
        "name": "Jomboy Media",
        "url": "https://www.youtube.com/@JomboyMedia",
        "mode": "single",
    },

    # ── Style / Lifestyle ─────────────────────────────────────────────────────
    "realmensrealstyle": {
        "name": "Real Men Real Style",
        "url": "https://www.youtube.com/@RealMenRealStyle",
        "mode": "single",
    },

    # ── Food / BBQ / Cooking ──────────────────────────────────────────────────
    "madscientistbbq": {
        "name": "Mad Scientist BBQ",
        "url": "https://www.youtube.com/@MadScientistBBQ",
        "mode": "single",
    },
    "samthecookingguy": {
        "name": "Sam The Cooking Guy",
        "url": "https://www.youtube.com/@SamTheCookingGuy",
        "mode": "single",
    },
    "americastestkitchen": {
        "name": "Americas Test Kitchen",
        "url": "https://www.youtube.com/@AmericasTestKitchen",
        "mode": "single",
    },
    "mattymatheson": {
        "name": "Matty Matheson",
        "url": "https://www.youtube.com/@MattMatheson",
        "mode": "single",
    },
    "gordonramsay": {
        "name": "Gordon Ramsay",
        "url": "https://www.youtube.com/@GordonRamsay",
        "mode": "single",
    },

    # ── Home / DIY ────────────────────────────────────────────────────────────
    "thisoldhouse": {
        "name": "This Old House",
        "url": "https://www.youtube.com/@ThisOldHouse",
        "mode": "single",
    },

    # ── Law ───────────────────────────────────────────────────────────────────
    "legaleagle": {
        "name": "LegalEagle",
        "url": "https://www.youtube.com/@LegalEagle",
        "mode": "single",
    },
}

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[yt-new {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def notify(text: str):
    try:
        nova_config.post_both(text, slack_channel=SLACK)
    except Exception as e:
        log(f"Slack error: {e}")

# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:120]

def normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def disk_titles(show_dir: Path) -> set:
    """Return normalized titles of all video files in a show dir."""
    titles = set()
    for f in show_dir.rglob("*"):
        if f.suffix.lower() not in {".mp4", ".mkv", ".avi"}:
            continue
        stem = f.stem
        stem = re.sub(r"^.*?S\d+E\d+\s*[-_ ]+", "", stem, flags=re.IGNORECASE)
        titles.add(normalize(stem))
    return titles

def is_on_disk(title: str, on_disk: set) -> bool:
    """Fuzzy title match: >=60% of meaningful words present on disk."""
    norm = normalize(title)
    words = [w for w in norm.split() if len(w) > 3]
    if not words:
        return False
    hit = sum(1 for w in words if any(w in dt for dt in on_disk))
    return (hit / len(words)) >= 0.6

def get_recent_videos(channel_url: str, count: int = RECENT_VIDEOS_CHECK) -> list:
    """Fetch most recent N videos from a channel."""
    r = subprocess.run(
        [YT_DLP, "--flat-playlist", "--playlist-end", str(count),
         "--print", "%(id)s\t%(title)s\t%(upload_date)s",
         f"{channel_url}/videos"],
        capture_output=True, text=True, timeout=60,
    )
    videos = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2:
            videos.append({
                "id": parts[0],
                "title": parts[1],
                "upload_date": parts[2] if len(parts) > 2 and parts[2] != "NA" else "",
            })
    return videos

def get_playlists(channel_url: str) -> list:
    r = subprocess.run(
        [YT_DLP, "--flat-playlist", "--print", "%(url)s\t%(title)s",
         f"{channel_url}/playlists"],
        capture_output=True, text=True, timeout=120,
    )
    playlists = []
    for line in r.stdout.strip().splitlines():
        if "\t" in line:
            url, title = line.split("\t", 1)
            playlists.append({"url": url, "title": title})
    return playlists

def get_playlist_videos(playlist_url: str) -> list:
    r = subprocess.run(
        [YT_DLP, "--flat-playlist",
         "--print", "%(id)s\t%(title)s\t%(upload_date)s",
         playlist_url],
        capture_output=True, text=True, timeout=120,
    )
    videos = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2:
            videos.append({
                "id": parts[0],
                "title": parts[1],
                "upload_date": parts[2] if len(parts) > 2 and parts[2] != "NA" else "",
            })
    return videos

# ── Episode numbering ─────────────────────────────────────────────────────────

def next_episode_single(show_dir: Path) -> tuple[int, int]:
    """Return (season=1, next_ep_num) for single-season channels."""
    max_ep = 0
    for f in show_dir.rglob("*"):
        if f.suffix.lower() not in {".mp4", ".mkv"}:
            continue
        m = re.search(r"S(\d+)E(\d+)", f.stem, re.IGNORECASE)
        if m and int(m.group(1)) == 1:
            max_ep = max(max_ep, int(m.group(2)))
    return 1, max_ep + 1

def season_for_year(show_dir: Path, year: str) -> int:
    """Map an upload year to a season number, consistent with existing files."""
    years_seen = set()
    for f in show_dir.rglob("*"):
        if f.suffix.lower() not in {".mp4", ".mkv"}:
            continue
        # Look for .season_info.json sidecar
        info = f.parent / ".season_info.json"
        if info.exists():
            try:
                d = json.loads(info.read_text())
                if d.get("year"):
                    years_seen.add(d["year"])
            except Exception:
                pass
    # Also infer years from upload dates if we have them in filenames (we don't,
    # so fall back to counting distinct season dirs)
    season_dirs = sorted([d for d in show_dir.iterdir() if d.is_dir() and d.name.lower().startswith("season")])
    # Build year→season map from existing .season_info.json files
    year_to_season = {}
    for sd in season_dirs:
        info = sd / ".season_info.json"
        if info.exists():
            try:
                d = json.loads(info.read_text())
                y = d.get("year", "")
                sn = d.get("season_number", 0)
                if y and sn:
                    year_to_season[y] = sn
            except Exception:
                pass
    if year in year_to_season:
        return year_to_season[year]
    # New year → new season
    return len(season_dirs) + 1

def next_episode_year(show_dir: Path, year: str) -> tuple[int, int]:
    """Return (season_num, next_ep_num) for a year-based channel."""
    season_num = season_for_year(show_dir, year)
    max_ep = 0
    for f in show_dir.rglob("*"):
        if f.suffix.lower() not in {".mp4", ".mkv"}:
            continue
        m = re.search(r"S(\d+)E(\d+)", f.stem, re.IGNORECASE)
        if m and int(m.group(1)) == season_num:
            max_ep = max(max_ep, int(m.group(2)))
    return season_num, max_ep + 1

def next_episode_playlist(show_dir: Path, playlist_title: str) -> tuple[int, int]:
    """Return (season_num, next_ep_num) for a playlist-based channel."""
    # Find which season dir matches this playlist
    for sd in show_dir.iterdir():
        if not sd.is_dir():
            continue
        info = sd / ".season_info.json"
        if info.exists():
            try:
                d = json.loads(info.read_text())
                if normalize(d.get("playlist_title", "")) == normalize(playlist_title):
                    m_s = re.search(r"Season (\d+)", sd.name, re.IGNORECASE)
                    if m_s:
                        sn = int(m_s.group(1))
                        max_ep = max(
                            (int(re.search(r"E(\d+)", f.stem, re.IGNORECASE).group(1))
                             for f in sd.rglob("*")
                             if f.suffix.lower() in {".mp4", ".mkv"}
                             and re.search(r"E(\d+)", f.stem, re.IGNORECASE)),
                            default=0
                        )
                        return sn, max_ep + 1
            except Exception:
                pass
    # New playlist → new season
    season_dirs = [d for d in show_dir.iterdir() if d.is_dir() and d.name.lower().startswith("season")]
    return len(season_dirs) + 1, 1

# ── Download ──────────────────────────────────────────────────────────────────

def download_video(vid_id: str, output_path: Path) -> str:
    cmd = [
        YT_DLP,
        "--cookies-from-browser", "safari",
        # 720p preferred; fall back to 540p, then best available ≤720p
        "-f", "bestvideo[height=720]+bestaudio/bestvideo[height=540]+bestaudio/bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "-o", str(output_path),
        "--no-overwrites",
        "--no-playlist",
        f"https://www.youtube.com/watch?v={vid_id}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        if "already been downloaded" in r.stdout or "has already been recorded" in r.stdout:
            return "skip"
        return f"error: {r.stderr[-300:]}"
    return "ok"

# ── Per-channel logic ─────────────────────────────────────────────────────────

def process_channel(key: str, cfg: dict, results: list):
    name    = cfg["name"]
    mode    = cfg["mode"]
    url     = cfg["url"]
    show_dir = BASE_DIR / name
    show_dir.mkdir(parents=True, exist_ok=True)

    on_disk = disk_titles(show_dir)
    log(f"[{name}] Checking for new episodes...")

    if mode == "playlists":
        # For playlist channels: check each playlist's most recent videos
        playlists = get_playlists(url)
        to_download = []
        for pl in playlists:
            pl_videos = get_playlist_videos(pl["url"])
            for v in pl_videos[-RECENT_VIDEOS_CHECK:]:
                if not is_on_disk(v["title"], on_disk):
                    to_download.append({**v, "playlist_title": pl["title"], "playlist_url": pl["url"]})
        if not to_download:
            log(f"[{name}] Up to date")
            return
        log(f"[{name}] {len(to_download)} new episode(s)")
        for v in to_download:
            sn, ep = next_episode_playlist(show_dir, v["playlist_title"])
            season_dir = show_dir / f"Season {sn:02d}"
            season_dir.mkdir(parents=True, exist_ok=True)
            # Write season info if missing
            info_file = season_dir / ".season_info.json"
            if not info_file.exists():
                info_file.write_text(json.dumps({
                    "playlist_title": v["playlist_title"],
                    "playlist_url": v["playlist_url"],
                    "season_number": sn,
                }, indent=2))
            _download_one(v, name, sn, ep, season_dir, results)

    elif mode == "year":
        videos = get_recent_videos(url)
        to_download = [v for v in videos if not is_on_disk(v["title"], on_disk)]
        if not to_download:
            log(f"[{name}] Up to date")
            return
        log(f"[{name}] {len(to_download)} new episode(s)")
        # Download oldest-first so episode numbering is correct
        for v in reversed(to_download):
            year = v.get("upload_date", "")[:4] or datetime.now().strftime("%Y")
            sn, ep = next_episode_year(show_dir, year)
            season_dir = show_dir / f"Season {sn:02d}"
            season_dir.mkdir(parents=True, exist_ok=True)
            info_file = season_dir / ".season_info.json"
            if not info_file.exists():
                info_file.write_text(json.dumps({
                    "year": year,
                    "season_number": sn,
                }, indent=2))
            _download_one(v, name, sn, ep, season_dir, results)

    else:  # single
        videos = get_recent_videos(url)
        to_download = [v for v in videos if not is_on_disk(v["title"], on_disk)]
        if not to_download:
            log(f"[{name}] Up to date")
            return
        log(f"[{name}] {len(to_download)} new episode(s)")
        for v in reversed(to_download):
            sn, ep = next_episode_single(show_dir)
            season_dir = show_dir / f"Season {sn:02d}"
            season_dir.mkdir(parents=True, exist_ok=True)
            _download_one(v, name, sn, ep, season_dir, results)


def _download_one(video: dict, show_name: str, sn: int, ep: int,
                  season_dir: Path, results: list):
    title_safe = sanitize(video["title"])
    filename   = f"{show_name} - S{sn:02d}E{ep:04d} - {title_safe}.mp4"
    out_path   = season_dir / filename

    log(f"  ↓ S{sn:02d}E{ep:04d} — {video['title'][:70]}")
    result = download_video(video["id"], out_path)

    if result == "ok":
        log(f"  ✓ {filename[:80]}")
        notify(
            f":arrow_down: *{show_name}* — new episode downloaded\n"
            f"  :clapper: S{sn:02d}E{ep:04d} — _{video['title'][:80]}_\n"
            f"  :file_folder: `{season_dir.relative_to(BASE_DIR)}/{filename}`"
        )
        results.append({"show": show_name, "title": video["title"], "status": "ok"})
        # Register the downloaded file so tv_ingest can pick it up; status='downloaded'
        # means "on disk, not yet transcribed" — tv_ingest will upgrade to 'ingested'.
        registry.register_file(str(out_path), show_name=show_name,
                               title=video["title"], ingest_script="nova_yt_new_episodes.py")
        registry.mark_status(str(out_path), "downloaded")
        time.sleep(DELAY_BETWEEN)
    elif result == "skip":
        log(f"  ~ already downloaded: {video['title'][:60]}")
        results.append({"show": show_name, "title": video["title"], "status": "skip"})
    else:
        log(f"  ✗ {result[:120]}")
        notify(
            f":x: *{show_name}* — download failed\n"
            f"  S{sn:02d}E{ep:04d} — _{video['title'][:80]}_\n"
            f"  `{result[:200]}`"
        )
        results.append({"show": show_name, "title": video["title"], "status": "error"})
        # Register the (non-existent) path so the error is tracked in the DB.
        registry.register_file(str(out_path), show_name=show_name,
                               title=video["title"], ingest_script="nova_yt_new_episodes.py")
        registry.mark_status(str(out_path), "error", error_msg=result)

# ── Main ──────────────────────────────────────────────────────────────────────

def scan_new_recordings(since_days: int = 8) -> list[dict]:
    """Scan /Volumes/external/videos/ for non-YouTube video files added in the last N days.

    Catches Plex recordings, manual downloads, or anything else dropped in
    the video root that isn't managed by nova_yt_new_episodes itself.
    Returns a list of dicts with path, name, size_mb, modified_at.
    """
    if not VIDEO_ROOT.exists():
        log("VIDEO_ROOT not mounted — skipping non-YT scan")
        return []

    cutoff = datetime.now() - timedelta(days=since_days)
    new_files = []

    for f in VIDEO_ROOT.rglob("*"):
        if f.suffix.lower() not in VIDEO_EXTS:
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
        except OSError:
            continue
        if mtime < cutoff:
            continue

        # Skip files that live in the YT-managed TVShows tree — those are
        # reported by the YT download loop above.
        if BASE_DIR in f.parents:
            continue

        # Skip system/temp files
        if f.name.startswith(".") or "~" in f.name:
            continue

        size_mb = f.stat().st_size / (1024 * 1024)
        # Skip tiny files (<5 MB) — likely partial downloads or thumbnails
        if size_mb < 5:
            continue

        new_files.append({
            "path": str(f),
            "name": f.name,
            "relative": str(f.relative_to(VIDEO_ROOT)),
            "size_mb": round(size_mb, 1),
            "modified_at": mtime.strftime("%Y-%m-%d %H:%M"),
            "parent": f.parent.name,
        })

    new_files.sort(key=lambda x: x["modified_at"], reverse=True)
    return new_files


def sync_subscriptions() -> dict:
    """Pull current YouTube subscriptions and merge with hardcoded CHANNELS.

    Returns a merged channel dict:
    - Hardcoded channels keep their mode/url config
    - Subscribed channels not in CHANNELS are added with mode='single'
    - Channels in CHANNELS but no longer subscribed are kept but flagged inactive
      (we keep downloading until you explicitly remove them, since unsub ≠ done)

    Cache written to CHANNELS_CACHE so the run logs show what changed.
    """
    log("Syncing YouTube subscriptions from Safari cookies...")
    try:
        r = subprocess.run(
            [YT_DLP, "--cookies-from-browser", "safari",
             "--flat-playlist", "--print", "%(id)s\t%(uploader_id)s\t%(uploader)s",
             "https://www.youtube.com/feed/channels"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0 or not r.stdout.strip():
            log(f"  Subscription fetch failed (exit {r.returncode}) — using hardcoded CHANNELS")
            return CHANNELS

        # Parse subscriptions: channel_id → {uploader_id, name}
        subscribed: dict[str, dict] = {}
        for line in r.stdout.strip().splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            ch_id, uploader_id, name = parts
            if ch_id == "NA" or not ch_id:
                continue
            subscribed[ch_id] = {"uploader_id": uploader_id.lstrip("@"), "name": name}

        log(f"  Found {len(subscribed)} subscribed channels")

        # Build a lookup of existing channel URLs → key for dedup
        url_to_key: dict[str, str] = {}
        for k, v in CHANNELS.items():
            url_to_key[v["url"].rstrip("/").lower()] = k

        merged = dict(CHANNELS)  # start from hardcoded config
        added, already_present = [], []

        for ch_id, info in subscribed.items():
            uid  = info["uploader_id"].lower()
            name = info["name"]

            # Check if this channel is already in CHANNELS by uploader_id or channel_id
            found_key = None
            for k, v in CHANNELS.items():
                existing_url = v["url"].rstrip("/").lower()
                if uid and (f"@{uid}" in existing_url or ch_id in existing_url):
                    found_key = k
                    break

            if found_key:
                already_present.append(found_key)
            else:
                # New subscription — add with sensible defaults
                # Use the @handle URL if uploader_id looks like a handle
                if uid and re.match(r'^[a-zA-Z0-9_\-\.]+$', uid):
                    channel_url = f"https://www.youtube.com/@{info['uploader_id'].lstrip('@')}"
                else:
                    channel_url = f"https://www.youtube.com/channel/{ch_id}"

                safe_key = re.sub(r'[^a-z0-9]', '', uid.lower()) or ch_id[:12]
                if safe_key not in merged:
                    merged[safe_key] = {
                        "name": name,
                        "url":  channel_url,
                        "mode": "single",   # default — change manually for year/playlists
                        "_auto_added": True,
                    }
                    added.append(name)

        # Persist cache so changes are visible in logs
        CHANNELS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        cache_data = {
            "updated_at": datetime.now().isoformat(),
            "subscribed_count": len(subscribed),
            "total_channels": len(merged),
            "added_this_run": added,
            "channels": {k: {"name": v["name"], "url": v["url"], "mode": v.get("mode","single"),
                              "auto": v.get("_auto_added", False)}
                         for k, v in merged.items()},
        }
        CHANNELS_CACHE.write_text(json.dumps(cache_data, indent=2))

        if added:
            log(f"  Added {len(added)} new subscriptions: {', '.join(added[:10])}")
            notify(
                f":new: *YouTube Subscription Sync* — {len(added)} new channel(s) added:\n"
                + "\n".join(f"  • {n}" for n in added[:15])
            )
        else:
            log(f"  No new subscriptions (all {len(subscribed)} already tracked)")

        return merged

    except Exception as e:
        log(f"  Subscription sync error: {e} — using hardcoded CHANNELS")
        return CHANNELS


def main():
    log(f"=== YouTube new-episode check started — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    # Sync subscriptions first so newly subscribed channels are included this run
    active_channels = sync_subscriptions()

    notify(
        f":mag: *YouTube Weekly Episode Check* starting\n"
        f"  Checking {len(active_channels)} channels for new episodes…"
    )

    results = []
    for key, cfg in active_channels.items():
        try:
            process_channel(key, cfg, results)
        except Exception as e:
            log(f"[{cfg['name']}] FATAL: {e}")
            notify(f":x: *{cfg['name']}* — check failed: `{e}`")

    downloaded = [r for r in results if r["status"] == "ok"]
    errors     = [r for r in results if r["status"] == "error"]

    # ── Scan for non-YouTube recordings (Plex, manual drops, etc.) ──────────
    log("Scanning for non-YouTube new recordings in /Volumes/external/videos/...")
    new_recordings = scan_new_recordings(since_days=8)
    if new_recordings:
        log(f"Found {len(new_recordings)} new non-YT recording(s)")
        rec_lines = [f":video_camera: *New Recordings in /external/videos* — {len(new_recordings)} file(s) added this week:"]
        for r in new_recordings[:20]:
            rec_lines.append(f"  • `{r['relative']}` ({r['size_mb']} MB, {r['modified_at']})")
        if len(new_recordings) > 20:
            rec_lines.append(f"  _...and {len(new_recordings) - 20} more_")
        rec_lines.append(f"\n  :information_source: `nova_tv_ingest.py` will transcribe these tonight at 11pm")
        notify("\n".join(rec_lines))
    else:
        log("No new non-YT recordings found")

    # ── Final summary ─────────────────────────────────────────────────────────
    if downloaded:
        summary_lines = [f":white_check_mark: *Weekly Episode Check Complete* — {len(downloaded)} new episode(s) downloaded ({len(active_channels)} channels checked)"]
        for r in downloaded:
            summary_lines.append(f"  • *{r['show']}* — {r['title'][:70]}")
        if errors:
            summary_lines.append(f"\n  :warning: {len(errors)} error(s) — check log")
        if new_recordings:
            summary_lines.append(f"\n  :video_camera: {len(new_recordings)} new non-YT recording(s) found")
        notify("\n".join(summary_lines))
    else:
        notify(
            f":white_check_mark: *Weekly Episode Check Complete* — all {len(active_channels)} channels up to date"
            + (f"\n  :video_camera: {len(new_recordings)} new Plex/manual recording(s) found" if new_recordings else "")
        )

    log(f"=== Done. {len(downloaded)} downloaded, {len(errors)} errors, {len(active_channels)} channels checked, {len(new_recordings)} non-YT recordings ===")


if __name__ == "__main__":
    main()
