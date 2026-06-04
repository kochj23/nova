#!/usr/bin/env python3
"""
nova_yale_backfill.py — One-time backfill of all Yale Courses YouTube playlists.

Downloads every video from every Yale Courses playlist into:
  /Volumes/external/videos/TVShows/Yale Courses/Season XX/Yale Courses - SxxExxxx - Title.mp4

Each season = one playlist. Episode numbers are per-season.
Delay between videos: π minutes (188.495 seconds) exactly.

State is checkpointed after every video — safe to kill and resume.
Per-video #nova-notifications on download + transcription.

Written by Jordan Koch.
"""

import json
import math
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
import nova_media_registry as registry

# ── Config ────────────────────────────────────────────────────────────────────

CHANNEL_URL   = "https://www.youtube.com/@yalecourses"
SHOW_NAME     = "Yale Courses"
BASE_DIR      = Path("/Volumes/external/videos/TVShows/Yale Courses")
STATE_FILE    = Path.home() / ".openclaw/workspace/state/yale_backfill_state.json"
WORK_DIR      = Path("/Volumes/Data/nova-livetv/yale-backfill")
LOG_FILE      = Path.home() / ".openclaw/logs/nova_yale_backfill.log"

YT_DLP        = "/opt/homebrew/bin/yt-dlp"
WHISPER_BIN   = "/opt/homebrew/bin/mlx_whisper"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
FFMPEG_BIN    = "/opt/homebrew/bin/ffmpeg"

MEMORY_URL    = "http://192.168.1.6:18790/remember"
RECALL_URL    = "http://192.168.1.6:18790/recall"
SLACK         = "#nova-notifications"

DELAY_BETWEEN = math.pi * 60          # π minutes = 188.495... seconds
CHUNK_WORDS   = 400
MIN_CHUNK_WORDS = 10
TRASH_RATIO   = 0.7
MAX_AUDIO_SECS = 7200
MAX_RESOLUTION = "720"
SOURCE        = "education"
TODAY         = datetime.now().strftime("%Y-%m-%d")

# ── Trash detection ───────────────────────────────────────────────────────────

_TRASH_PATTERNS = [
    re.compile(r"[♪♫♬♩]"),
    re.compile(r"\b(\w+)\s+\1\s+\1\s+\1", re.IGNORECASE),
    re.compile(r"^[A-Z\s\W]{20,}$"),
    re.compile(r"^[^aeiouAEIOU\s]{8,}$"),
    re.compile(r"^[\W\d\s]+$"),
    re.compile(r"subtitles?\s+by|transcribed\s+by|closed\s+caption", re.IGNORECASE),
    re.compile(r"^\[?\s*(silence|music|applause|laughter|cheering|crowd|♪)\s*\]?$", re.IGNORECASE),
    re.compile(r"(.{5,}?)(\s+\1){4,}"),
]
_MUSIC_PHRASES = ["♪", "♫", "la la la", "da da da", "na na na", "hmm hmm"]


def is_trash_chunk(text: str) -> bool:
    stripped = text.strip()
    if len(stripped.split()) < MIN_CHUNK_WORDS:
        return True
    for pat in _TRASH_PATTERNS:
        if pat.search(stripped):
            return True
    lower = stripped.lower()
    for phrase in _MUSIC_PHRASES:
        if lower.count(phrase) >= 3:
            return True
    alpha = sum(c.isalpha() for c in stripped)
    return alpha / max(len(stripped), 1) < 0.5


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[yale_backfill {ts}] {msg}"
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


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {
        "done_playlists": [],       # playlist URLs fully completed
        "done_videos": [],          # individual video IDs completed
        "current_playlist": None,
        "total_downloaded": 0,
        "total_ingested": 0,
        "last_run": None,
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── yt-dlp helpers ────────────────────────────────────────────────────────────

def get_playlists() -> list[dict]:
    r = subprocess.run(
        [YT_DLP, "--cookies-from-browser", "safari",
         "--flat-playlist", "--print", "%(url)s\t%(title)s",
         f"{CHANNEL_URL}/playlists"],
        capture_output=True, text=True, timeout=120,
    )
    playlists = []
    for line in r.stdout.strip().splitlines():
        if "\t" in line:
            url, title = line.split("\t", 1)
            playlists.append({"url": url, "title": title.strip()})
    return playlists


def get_playlist_videos(playlist_url: str) -> list[dict]:
    r = subprocess.run(
        [YT_DLP, "--cookies-from-browser", "safari",
         "--flat-playlist",
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


def sanitize(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:120]


def download_video(vid_id: str, out_path: Path) -> str:
    if out_path.exists():
        return "skip"
    cmd = [
        YT_DLP,
        "--cookies-from-browser", "safari",
        "-f", "bestvideo[height=720]+bestaudio/bestvideo[height=540]+bestaudio/bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        "--no-overwrites",
        "--no-playlist",
        f"https://www.youtube.com/watch?v={vid_id}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        combined = r.stdout + r.stderr
        if "already been downloaded" in combined:
            return "skip"
        return f"error: {r.stderr[-300:]}"
    return "ok"


# ── Audio / Transcription ─────────────────────────────────────────────────────

def extract_audio(video: Path, wav: Path) -> bool:
    cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000",
        "-acodec", "pcm_s16le",
        "-t", str(MAX_AUDIO_SECS),
        str(wav),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=MAX_AUDIO_SECS + 60)
        return wav.exists() and wav.stat().st_size > 1000
    except Exception as exc:
        log(f"  ffmpeg error: {exc}")
        return False


def transcribe(wav: Path, stem: str) -> str | None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        WHISPER_BIN, str(wav),
        "--model", WHISPER_MODEL,
        "--output-format", "txt",
        "--output-dir", str(WORK_DIR),
        "--output-name", stem,
        "--language", "en",
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_AUDIO_SECS * 2)
        txt = WORK_DIR / f"{stem}.txt"
        if txt.exists():
            text = txt.read_text(encoding="utf-8", errors="ignore").strip()
            txt.unlink(missing_ok=True)
            return text if len(text) > 20 else None
    except subprocess.TimeoutExpired:
        log("  Whisper timeout")
    except Exception as exc:
        log(f"  Whisper error: {exc}")
    return None


def chunk_text(text: str) -> list[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), CHUNK_WORDS):
        chunk = " ".join(words[i:i + CHUNK_WORDS])
        if not is_trash_chunk(chunk):
            chunks.append(chunk)
    return chunks


def remember(text: str, metadata: dict) -> bool:
    payload = json.dumps({
        "text": nova_config.truncate_at_boundary(text),
        "source": SOURCE,
        "tier": "long_term",
        "privacy": "local-only",
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception:
        return False


# ── Season / episode numbering ────────────────────────────────────────────────

def get_season_dir(playlist_title: str, season_num: int) -> tuple[Path, Path]:
    """Return (season_dir, info_file) for a playlist, creating if needed."""
    season_dir = BASE_DIR / f"Season {season_num:02d}"
    season_dir.mkdir(parents=True, exist_ok=True)
    info_file = season_dir / ".season_info.json"
    if not info_file.exists():
        info_file.write_text(json.dumps({
            "playlist_title": playlist_title,
            "season_number": season_num,
            "channel": SHOW_NAME,
        }, indent=2))
    return season_dir, info_file


def last_episode_in_season(season_dir: Path) -> int:
    max_ep = 0
    for f in season_dir.rglob("*"):
        if f.suffix.lower() not in {".mp4", ".mkv"}:
            continue
        m = re.search(r"E(\d+)", f.stem, re.IGNORECASE)
        if m:
            max_ep = max(max_ep, int(m.group(1)))
    return max_ep


def season_num_for_playlist(playlist_title: str) -> int:
    """Find existing season number for this playlist, or assign next available."""
    for sd in sorted(BASE_DIR.iterdir()):
        if not sd.is_dir():
            continue
        info = sd / ".season_info.json"
        if info.exists():
            try:
                d = json.loads(info.read_text())
                if d.get("playlist_title", "").strip() == playlist_title.strip():
                    return d["season_number"]
            except Exception:
                pass
    existing_seasons = [
        d for d in BASE_DIR.iterdir()
        if d.is_dir() and d.name.lower().startswith("season")
    ]
    return len(existing_seasons) + 1


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log(f"=== Yale Courses backfill started — {TODAY} ===")
    log(f"Delay between videos: π minutes ({DELAY_BETWEEN:.3f}s)")

    state = load_state()
    done_playlists = set(state.get("done_playlists", []))
    done_videos    = set(state.get("done_videos", []))

    log("Fetching playlist list from YouTube...")
    playlists = get_playlists()
    log(f"Found {len(playlists)} playlists")

    pending_playlists = [p for p in playlists if p["url"] not in done_playlists]
    log(f"Playlists remaining: {len(pending_playlists)}")

    notify(
        f":mortar_board: *Yale Courses Backfill Started*\n"
        f"  {len(playlists)} total playlists · {len(pending_playlists)} remaining\n"
        f"  Delay: π min ({DELAY_BETWEEN:.1f}s) between videos\n"
        f"  Downloads → #nova-notifications per file"
    )

    for pl in pending_playlists:
        pl_title = pl["title"]
        pl_url   = pl["url"]
        state["current_playlist"] = pl_url
        save_state(state)

        log(f"\n── Playlist: {pl_title} ──")
        videos = get_playlist_videos(pl_url)
        if not videos:
            log(f"  No videos found, skipping")
            done_playlists.add(pl_url)
            state["done_playlists"] = list(done_playlists)
            save_state(state)
            continue

        sn = season_num_for_playlist(pl_title)
        season_dir, _ = get_season_dir(pl_title, sn)
        log(f"  Season {sn:02d} | {len(videos)} videos")

        pending_videos = [v for v in videos if v["id"] not in done_videos]
        log(f"  Pending: {len(pending_videos)}")

        for i, video in enumerate(pending_videos):
            vid_id = video["id"]
            title  = video["title"]

            # Skip if already in registry
            ep = last_episode_in_season(season_dir) + 1
            filename = f"Yale Courses - S{sn:02d}E{ep:04d} - {sanitize(title)}.mp4"
            out_path = season_dir / filename
            path_key = str(out_path)

            if registry.is_done(path_key):
                done_videos.add(vid_id)
                continue

            log(f"  [{i+1}/{len(pending_videos)}] S{sn:02d}E{ep:04d} — {title[:70]}")
            registry.register_file(path_key, SHOW_NAME, title, SOURCE, "nova_yale_backfill.py")

            # Download
            dl_result = download_video(vid_id, out_path)
            if dl_result == "skip":
                log(f"    already on disk")
                registry.mark_status(path_key, "downloaded")
            elif dl_result != "ok":
                log(f"    download error: {dl_result[:80]}")
                registry.mark_status(path_key, "error", error_msg=dl_result)
                done_videos.add(vid_id)
                state["done_videos"] = list(done_videos)
                save_state(state)
                continue
            else:
                log(f"    downloaded ✓")
                registry.mark_status(path_key, "downloaded")
                state["total_downloaded"] = state.get("total_downloaded", 0) + 1

            # Transcribe
            wav_stem = f"yale_{abs(hash(path_key)) % 1_000_000:06d}"
            wav = WORK_DIR / f"{wav_stem}.wav"
            WORK_DIR.mkdir(parents=True, exist_ok=True)

            ingested = 0
            word_count = 0

            if extract_audio(out_path, wav):
                transcript = transcribe(wav, wav_stem)
                wav.unlink(missing_ok=True)

                if transcript:
                    word_count = len(transcript.split())
                    chunks = chunk_text(transcript)
                    total_raw = max(1, word_count // CHUNK_WORDS)
                    trash_ratio = 1 - (len(chunks) / total_raw)

                    if trash_ratio <= TRASH_RATIO and chunks:
                        for j, chunk in enumerate(chunks):
                            ok = remember(f"[{SHOW_NAME}] {chunk}", {
                                "type": "course_lecture",
                                "show": SHOW_NAME,
                                "playlist": pl_title,
                                "title": title,
                                "season": sn,
                                "episode": ep,
                                "chunk": j + 1,
                                "total_chunks": len(chunks),
                                "source_file": path_key,
                                "ingested_date": TODAY,
                            })
                            if ok:
                                ingested += 1
                        registry.mark_ingested(path_key, ingested, SOURCE)
                        state["total_ingested"] = state.get("total_ingested", 0) + ingested
                        log(f"    ingested {ingested} chunks · {word_count:,} words")
                    else:
                        registry.mark_status(path_key, "trash")
                        log(f"    trash ({trash_ratio:.0%} garbage)")
                else:
                    registry.mark_status(path_key, "no_transcript")
                    log(f"    no transcript")
            else:
                registry.mark_status(path_key, "audio_failed")
                log(f"    audio extraction failed")

            # Notify #nova-notifications only if ingested
            if ingested > 0:
                notify(
                    f":mortar_board: *Yale Courses* — S{sn:02d}E{ep:04d}\n"
                    f"  :clapper: _{title[:80]}_\n"
                    f"  :books: Playlist: {pl_title[:60]}\n"
                    f"  :brain: {ingested} memories · {word_count:,} words"
                )

            done_videos.add(vid_id)
            state["done_videos"] = list(done_videos)
            state["last_run"] = datetime.now().isoformat()
            save_state(state)

            # π-minute delay (skip after last video of last playlist)
            is_last = (i == len(pending_videos) - 1) and (pl == pending_playlists[-1])
            if not is_last:
                log(f"    sleeping π min ({DELAY_BETWEEN:.3f}s)…")
                time.sleep(DELAY_BETWEEN)

        # Playlist complete
        done_playlists.add(pl_url)
        state["done_playlists"] = list(done_playlists)
        save_state(state)
        log(f"  ✓ Playlist complete: {pl_title}")

    total_dl = state.get("total_downloaded", 0)
    total_ing = state.get("total_ingested", 0)
    log(f"=== Yale Courses backfill complete — {total_dl} downloaded, {total_ing} chunks ingested ===")
    notify(
        f":white_check_mark: *Yale Courses Backfill Complete*\n"
        f"  :arrow_down: {total_dl} videos downloaded\n"
        f"  :brain: {total_ing:,} total memory chunks ingested"
    )


if __name__ == "__main__":
    main()
