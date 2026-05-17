#!/usr/bin/env python3
"""
nova_nightly_media.py — Unified nightly media pipeline.

Runs nightly at 10pm. Two phases:

  Phase 1 — YT channel sweep
    Checks all 61 configured YouTube channels for new videos since last run,
    downloads them with yt-dlp (Safari cookies, 720p/540p), transcribes each
    inline with MLX Whisper, chunks + vectorizes into nova_memories, and posts
    a per-video notification to #nova-notifications.  Random 10–45s delay between
    videos; random 30–90s delay between channels.  Channels are shuffled nightly.
    YT blocking (403/429) is handled gracefully: the run position is saved and
    the channel is skipped with an alert to #nova-chat.

  Phase 2 — Full sweep of /Volumes/external/videos
    Walks all of VIDEO_ROOT (except excluded/music dirs), and for every video
    not already in the media registry: extract audio → transcribe → chunk →
    vectorize.  Music channels are skipped for transcription (no Whisper pass).
    Progress is checkpointed every 10 files into pipeline_runs so the run can be
    resumed the next night from where it left off.

DB state is kept in the nova_media PostgreSQL database.  Two tables are
created on first run if they don't exist: yt_channels and pipeline_runs.

Resume logic: on startup, if a run from the last 48 hours is found with status
'running', 'paused_yt_blocked', or 'paused_error', the pipeline resumes from
where it left off.  nova_media_registry.is_done() handles per-video dedup
across runs automatically.

nohup-safe: all output goes to LOG_FILE, no interactive prompts.
SIGTERM-safe: catches SIGTERM, saves current position, exits 0.

Written by Jordan Koch.
"""

from __future__ import annotations

import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Scripts path resolution ───────────────────────────────────────────────────

SCRIPTS = str(Path(__file__).parent)
sys.path.insert(0, SCRIPTS)

import nova_config
import nova_media_registry as registry

# Import CHANNELS dict directly — do not duplicate
from nova_yt_new_episodes import (
    CHANNELS,
    disk_titles,
    is_on_disk,
    get_recent_videos,
    get_playlists,
    get_playlist_videos,
    next_episode_single,
    next_episode_year,
    next_episode_playlist,
    season_for_year,
    sanitize,
    normalize,
)

# ── Config constants ──────────────────────────────────────────────────────────

BASE_DIR        = Path("/Volumes/external/videos/TVShows")
VIDEO_ROOT      = Path("/Volumes/external/videos")
EXCLUDED_DIRS   = {"other", "Other"}
MUSIC_DIRS      = {"Youtube Music Videos"}
MUSIC_CHANNELS  = {
    "kexp", "hör berlin", "hor berlin", "ukf drum", "boiler room",
    "dnb portal", "dj mag", "skratch bastid", "laufey", "tracklib",
    "relevant dnb", "dnb allstars", "drum bass arena", "drum&bass arena",
}

YT_DLP          = "/opt/homebrew/bin/yt-dlp"
WHISPER_BIN     = "/opt/homebrew/bin/mlx_whisper"
WHISPER_MODEL   = "mlx-community/whisper-large-v3-turbo"
FFMPEG_BIN      = "/opt/homebrew/bin/ffmpeg"
WORK_DIR        = Path("/Volumes/Data/nova-nightly-media")
LOG_FILE        = Path.home() / ".openclaw/logs/nova_nightly_media.log"
SLACK_NOTIFY    = nova_config.SLACK_NOTIFY
SLACK_CHAT      = nova_config.SLACK_CHAN
RECALL_URL      = "http://192.168.1.6:18790/recall"

CHUNK_WORDS         = 400
MIN_CHUNK_WORDS     = 10
TRASH_RATIO         = 0.7
MAX_AUDIO_SECS      = 7200
MAX_RESOLUTION      = "720"
RECENT_VIDEOS_CHECK  = 15
MAX_NEW_PER_CHANNEL  = 5   # cap per channel per night — prevents a new 1000-ep channel blocking all others

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".wmv", ".flv"}

# DB constants
DSN = "dbname=nova_media"

# ── Graceful SIGTERM handling ─────────────────────────────────────────────────

_sigterm_received = False
_current_run_id: int | None = None
_current_channel: str | None = None
_current_video_url: str | None = None
_current_video_title: str | None = None
_current_phase: str = "yt_sweep"


def _sigterm_handler(signum, frame):
    global _sigterm_received
    _sigterm_received = True
    log("SIGTERM received — saving position and exiting cleanly...")
    _save_run_position(status="paused_error", error_msg="SIGTERM received")
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[nightly_media {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(text: str, channel: str = SLACK_NOTIFY) -> None:
    """Post to the given Slack channel (and its Discord mirror)."""
    try:
        nova_config.post_both(text, slack_channel=channel)
    except Exception as exc:
        log(f"Slack notify error: {exc}")


# ── DB setup ──────────────────────────────────────────────────────────────────

def _db_connect():
    import psycopg2
    return psycopg2.connect(DSN)


def ensure_tables() -> None:
    """Create yt_channels and pipeline_runs tables if they don't exist."""
    import psycopg2
    sql_yt_channels = """
        CREATE TABLE IF NOT EXISTS yt_channels (
            channel_key         TEXT PRIMARY KEY,
            channel_name        TEXT,
            channel_url         TEXT,
            last_checked_at     TIMESTAMPTZ,
            last_video_downloaded TEXT,
            total_downloaded    INTEGER DEFAULT 0,
            updated_at          TIMESTAMPTZ DEFAULT NOW()
        )
    """
    sql_pipeline_runs = """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id                  SERIAL PRIMARY KEY,
            started_at          TIMESTAMPTZ DEFAULT NOW(),
            phase               TEXT,
            current_channel     TEXT,
            current_video_url   TEXT,
            current_video_title TEXT,
            status              TEXT DEFAULT 'running',
            videos_downloaded   INTEGER DEFAULT 0,
            videos_ingested     INTEGER DEFAULT 0,
            error_msg           TEXT,
            completed_at        TIMESTAMPTZ,
            resumed_from_run_id INTEGER
        )
    """
    con = _db_connect()
    try:
        cur = con.cursor()
        cur.execute(sql_yt_channels)
        cur.execute(sql_pipeline_runs)
        con.commit()
    finally:
        con.close()


# ── Pipeline run tracking ─────────────────────────────────────────────────────

def _start_run(resumed_from: int | None = None) -> int:
    """Insert a new pipeline_runs row and return its id."""
    global _current_run_id
    import psycopg2
    con = _db_connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO pipeline_runs (phase, status, resumed_from_run_id)
            VALUES (%s, 'running', %s)
            RETURNING id
            """,
            ("yt_sweep", resumed_from),
        )
        row = cur.fetchone()
        run_id = row[0]
        con.commit()
        _current_run_id = run_id
        return run_id
    finally:
        con.close()


def _save_run_position(
    *,
    status: str = "running",
    phase: str | None = None,
    channel: str | None = None,
    video_url: str | None = None,
    video_title: str | None = None,
    videos_downloaded: int | None = None,
    videos_ingested: int | None = None,
    error_msg: str | None = None,
    completed: bool = False,
) -> None:
    """Update the current pipeline_runs row with current position."""
    if _current_run_id is None:
        return
    import psycopg2
    set_parts = ["status = %s", "updated_at = NOW()"]
    params: list = [status]

    if phase is not None:
        set_parts.append("phase = %s")
        params.append(phase)
    if channel is not None:
        set_parts.append("current_channel = %s")
        params.append(channel)
    if video_url is not None:
        set_parts.append("current_video_url = %s")
        params.append(video_url)
    if video_title is not None:
        set_parts.append("current_video_title = %s")
        params.append(video_title)
    if videos_downloaded is not None:
        set_parts.append("videos_downloaded = %s")
        params.append(videos_downloaded)
    if videos_ingested is not None:
        set_parts.append("videos_ingested = %s")
        params.append(videos_ingested)
    if error_msg is not None:
        set_parts.append("error_msg = %s")
        params.append(error_msg)
    if completed:
        set_parts.append("completed_at = NOW()")

    # Add the WHERE id param last
    params.append(_current_run_id)

    sql = f"UPDATE pipeline_runs SET {', '.join(set_parts)} WHERE id = %s"
    con = _db_connect()
    try:
        cur = con.cursor()
        cur.execute(sql, params)
        con.commit()
    except Exception as exc:
        log(f"[db] _save_run_position error: {exc}")
    finally:
        con.close()


def _find_resumable_run() -> dict | None:
    """
    Look for a pipeline_runs row from the last 48 hours with a resumable status.
    Returns the row as a dict, or None.
    """
    import psycopg2, psycopg2.extras
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    con = _db_connect()
    try:
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT * FROM pipeline_runs
            WHERE status IN ('running', 'paused_yt_blocked', 'paused_error')
              AND started_at >= %s
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (cutoff,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        con.close()


# ── YT channel DB helpers ─────────────────────────────────────────────────────

def _get_channel_last_checked(channel_key: str) -> str | None:
    """Return last_checked_at as YYYYMMDD string, or None."""
    import psycopg2
    con = _db_connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT last_checked_at FROM yt_channels WHERE channel_key = %s",
            (channel_key,),
        )
        row = cur.fetchone()
        if row and row[0]:
            return row[0].strftime("%Y%m%d")
        return None
    finally:
        con.close()


def _update_yt_channel(
    channel_key: str,
    channel_name: str,
    channel_url: str,
    last_video: str | None = None,
    increment_downloaded: int = 0,
) -> None:
    """Upsert a yt_channels row after processing a channel."""
    import psycopg2
    con = _db_connect()
    try:
        cur = con.cursor()
        if last_video is not None:
            cur.execute(
                """
                INSERT INTO yt_channels
                    (channel_key, channel_name, channel_url,
                     last_checked_at, last_video_downloaded, total_downloaded, updated_at)
                VALUES (%s, %s, %s, NOW(), %s, %s, NOW())
                ON CONFLICT (channel_key) DO UPDATE SET
                    last_checked_at      = NOW(),
                    last_video_downloaded = EXCLUDED.last_video_downloaded,
                    total_downloaded     = yt_channels.total_downloaded + EXCLUDED.total_downloaded,
                    updated_at           = NOW()
                """,
                (channel_key, channel_name, channel_url, last_video, increment_downloaded),
            )
        else:
            cur.execute(
                """
                INSERT INTO yt_channels
                    (channel_key, channel_name, channel_url, last_checked_at,
                     total_downloaded, updated_at)
                VALUES (%s, %s, %s, NOW(), %s, NOW())
                ON CONFLICT (channel_key) DO UPDATE SET
                    last_checked_at  = NOW(),
                    total_downloaded = yt_channels.total_downloaded + EXCLUDED.total_downloaded,
                    updated_at       = NOW()
                """,
                (channel_key, channel_name, channel_url, increment_downloaded),
            )
        con.commit()
    finally:
        con.close()


# ── YT blocking detection ─────────────────────────────────────────────────────

_YT_HARD_BLOCK_STRINGS = (
    "Sign in",
    "HTTP Error 403",
    "HTTP Error 429",
)
_YT_SOFT_ERROR_STRINGS = (
    "age-restricted",
    "This video is not available",
)


def _detect_yt_block(stderr: str) -> str:
    """
    Returns 'hard' for 403/429/Sign-in errors (alert + skip channel),
    'soft' for age-restricted/unavailable (silent skip),
    or 'none'.
    """
    for s in _YT_HARD_BLOCK_STRINGS:
        if s in stderr:
            return "hard"
    for s in _YT_SOFT_ERROR_STRINGS:
        if s in stderr:
            return "soft"
    return "none"


# ── Audio / transcription (adapted from nova_tv_ingest.py) ───────────────────

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
_MUSIC_PHRASES = ["♪", "♫", "la la la", "da da da", "na na na", "hmm hmm", "mmm mmm", "woo woo"]


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
    if alpha / max(len(stripped), 1) < 0.5:
        return True
    return False


def extract_audio(video: Path, out_wav: Path) -> bool:
    cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000",
        "-acodec", "pcm_s16le",
        "-t", str(MAX_AUDIO_SECS),
        str(out_wav),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=MAX_AUDIO_SECS + 60)
        return out_wav.exists() and out_wav.stat().st_size > 1000
    except Exception as exc:
        log(f"  ffmpeg error: {exc}")
        return False


def transcribe(wav: Path, out_dir: Path, stem: str) -> str | None:
    cmd = [
        WHISPER_BIN, str(wav),
        "--model", WHISPER_MODEL,
        "--output-format", "txt",
        "--output-dir", str(out_dir),
        "--output-name", stem,
        "--language", "en",
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_AUDIO_SECS * 2)
        txt_path = out_dir / f"{stem}.txt"
        if txt_path.exists():
            text = txt_path.read_text(encoding="utf-8", errors="ignore").strip()
            txt_path.unlink(missing_ok=True)
            return text if len(text) > 20 else None
    except subprocess.TimeoutExpired:
        log("  Whisper timeout — skipping")
    except Exception as exc:
        log(f"  Whisper error: {exc}")
    return None


def chunk_text(text: str) -> list[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), CHUNK_WORDS):
        chunk = " ".join(words[i : i + CHUNK_WORDS])
        if not is_trash_chunk(chunk):
            chunks.append(chunk)
    return chunks


def remember(text: str, source: str, metadata: dict) -> bool:
    """POST a memory chunk to the Nova vector memory endpoint."""
    payload = json.dumps({
        "text": text[:2000],
        "source": source,
        "tier": "long_term",
        "privacy": "local-only",
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        nova_config.VECTOR_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception:
        return False


def classify_source(show_name: str, title: str, snippet: str) -> str:
    """Lightweight source classifier. Show-name matches are authoritative (no content override)."""
    show = show_name.lower()

    # ── Authoritative show-name matches (never overridden by content keywords) ──

    if any(w in show for w in ["thesmokingtire", "smoking tire", "smokingtire",
                                "brian scotto", "vinwiki", "vin wiki",
                                "vin_tra", "vin tra", "rob dahm", "jason cammisa",
                                "jay leno", "jasoncommisa", "wheeler dealer",
                                "fourwheeler", "four wheeler", "mighty car mod",
                                "rev explained"]):
        return "automotive"

    if any(w in show for w in ["meat church", "arnitex", "arnie tex",
                                "good eats", "binging with babish", "babish",
                                "ethan chlebowski", "food wishes"]):
        return "cooking"

    if any(w in show for w in ["forgotten weapon", "forbidden weapon"]):
        return "military_history"

    if any(w in show for w in ["wipeout", "jeopardy", "wheel of fortune",
                                "game show", "price is right", "joeschmo",
                                "joe schmo"]):
        return "game_show"

    if any(w in show for w in ["history of christianity", "history of religion",
                                "bible", "gospel", "theology"]):
        return "religion"

    if any(w in show for w in ["red letter media", "redlettermedia", "half in the bag",
                                "best of the worst", "re:view"]):
        return "film_criticism"

    if any(w in show for w in ["crash course", "crashcourse"]):
        return "education"

    if any(w in show for w in ["wristwatch revival", "wristwatch"]):
        return "horology"

    if any(w in show for w in ["documentary", "biography", "civilizations", "connections",
                                "frontline", "american experience"]):
        return "documentary"

    if any(w in show for w in ["car", "auto", "garage", "engine", "motor", "mustang",
                                "corvette", "racing", "drift", "truck", "wheels", "horsepower",
                                "finnegan", "car wizard", "chasing classic", "dream car",
                                "build or bust", "car craft"]):
        return "automotive"

    if any(w in show for w in ["combat", "war", "battle", "military", "bonanza", "western",
                                "cannon", "batman", "21 jump"]):
        return "crime_drama"

    if any(w in show for w in ["cooking", "pepin", "kitchen", "chef", "recipe", "food"]):
        return "cooking"

    if any(w in show for w in ["louis ck", "comedy", "standup", "stand-up", "chug"]):
        return "comedy"

    # ── Content-based fallback (only if no show-name match above) ──

    text = (title + " " + snippet[:400]).lower()

    if any(w in text for w in ["firearm", "rifle", "pistol", "shotgun", "cartridge",
                                "caliber", "ammunition", "magazine", "barrel", "trigger"]):
        return "military_history"
    if any(w in text for w in ["horsepower", "torque", "carburetor", "engine", "transmission",
                                "differential", "chassis", "dyno", "lap time", "drag strip"]):
        return "automotive"
    if any(w in text for w in ["history", "war", "battle", "ancient", "civilization", "empire",
                                "century", "dynasty", "revolution"]):
        return "documentary"
    if any(w in text for w in ["joke", "laugh", "funny", "comedian", "crowd", "audience"]):
        return "comedy"
    return "television"


def show_name_from_path(video: Path) -> str:
    parts = video.parts
    for i, part in enumerate(parts):
        if part.lower().startswith("season") or re.match(r"^s\d{2}$", part.lower()):
            if i - 1 >= 0:
                return parts[i - 1]
    return video.parent.name


# ── Random memory recall for notifications ───────────────────────────────────

def recall_memory_for_show(show_name: str, chunks: list[str] | None = None) -> str | None:
    """Pull a random existing memory for this show from the vector DB."""
    try:
        payload = json.dumps({
            "query": f"{show_name} television episode",
            "limit": 20,
        }).encode()
        req = urllib.request.Request(
            RECALL_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            memories = data.get("memories", data.get("results", []))
            if memories:
                m = random.choice(memories)
                text = re.sub(r"^\[.*?\]\s*", "", m.get("text", ""))
                return text[:200].strip()
    except Exception:
        pass
    # Fallback: pick a random chunk from the current transcript
    if chunks:
        return re.sub(r"^\[.*?\]\s*", "", random.choice(chunks))[:200].strip()
    return None


# ── Ingest a single video file ────────────────────────────────────────────────

def ingest_video(video: Path, show_name: str, title: str, source_hint: str | None = None) -> dict:
    """
    Full pipeline for one local video file.
    Returns dict with keys: status, chunks, words.
    """
    path_key = str(video)
    wav_stem = f"nightly_{abs(hash(path_key)) % 1_000_000}"
    wav = WORK_DIR / f"{wav_stem}.wav"

    log(f"  [ingest] {show_name} — {title[:70]}")

    if not extract_audio(video, wav):
        log(f"    audio_failed: {title[:50]}")
        registry.mark_status(path_key, "audio_failed")
        return {"status": "audio_failed", "chunks": 0, "words": 0}

    transcript = transcribe(wav, WORK_DIR, wav_stem)
    wav.unlink(missing_ok=True)

    if not transcript:
        log(f"    no_transcript: {title[:50]}")
        registry.mark_status(path_key, "no_transcript")
        return {"status": "no_transcript", "chunks": 0, "words": 0}

    word_count = len(transcript.split())
    chunks = chunk_text(transcript)
    total_raw = max(1, word_count // CHUNK_WORDS)
    trash_ratio_val = 1.0 - (len(chunks) / total_raw)

    if trash_ratio_val > TRASH_RATIO or len(chunks) == 0:
        log(f"    trash ({trash_ratio_val:.0%}): {title[:50]}")
        registry.mark_status(path_key, "trash")
        return {"status": "trash", "chunks": 0, "words": word_count}

    source = source_hint or classify_source(show_name, title, transcript[:500])
    today = datetime.now().strftime("%Y-%m-%d")
    ingested = 0
    for i, chunk in enumerate(chunks):
        ok = remember(
            f"[{show_name}] {chunk}",
            source,
            {
                "type": "tv_transcript",
                "show": show_name,
                "title": title,
                "chunk": i + 1,
                "total_chunks": len(chunks),
                "ingested_date": today,
                "source_file": path_key,
            },
        )
        if ok:
            ingested += 1

    if ingested > 0:
        registry.mark_ingested(path_key, ingested, source)
        log(f"    ingested {ingested} chunks [{source}] — {title[:50]}")
    else:
        registry.mark_status(path_key, "no_transcript", notes="all chunks rejected by memory endpoint")
        log(f"    0 chunks stored — {title[:50]}")

    return {
        "status": "ingested" if ingested > 0 else "no_transcript",
        "chunks": ingested,
        "words": word_count,
        "chunks_text": chunks,   # raw chunk list for memory snippet fallback
    }


# ── Phase 1: YT download + inline ingest ─────────────────────────────────────

def _yt_download_video(vid_id: str, output_path: Path, dateafter: str | None = None) -> tuple[str, str]:
    """
    Download one YouTube video.
    Returns (result, stderr) where result is 'ok', 'skip', or 'error:<msg>'.
    """
    cmd = [
        YT_DLP,
        "--cookies-from-browser", "safari",
        "-f",
        f"bestvideo[height={MAX_RESOLUTION}]+bestaudio"
        f"/bestvideo[height=540]+bestaudio"
        f"/bestvideo[height<={MAX_RESOLUTION}]+bestaudio"
        f"/best[height<={MAX_RESOLUTION}]",
        "--merge-output-format", "mp4",
        "-o", str(output_path),
        "--no-overwrites",
        "--no-playlist",
    ]
    if dateafter:
        cmd += ["--dateafter", dateafter]
    cmd.append(f"https://www.youtube.com/watch?v={vid_id}")

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        if "already been downloaded" in r.stdout or "has already been recorded" in r.stdout:
            return "skip", r.stderr
        return f"error: {r.stderr[-400:]}", r.stderr
    return "ok", r.stderr


def _get_recent_videos_with_dateafter(channel_url: str, dateafter: str | None) -> list[dict]:
    """
    Fetch recent videos, optionally filtering with --dateafter YYYYMMDD.
    Falls back to RECENT_VIDEOS_CHECK if no dateafter.
    """
    cmd = [YT_DLP, "--flat-playlist",
           "--print", "%(id)s\t%(title)s\t%(upload_date)s"]
    if dateafter:
        cmd += ["--dateafter", dateafter]
    else:
        cmd += ["--playlist-end", str(RECENT_VIDEOS_CHECK)]
    cmd.append(f"{channel_url}/videos")

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
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


def _process_yt_channel(
    key: str,
    cfg: dict,
    run_counters: dict,
    resume_from_video: str | None = None,
) -> bool:
    """
    Process one YT channel: check for new videos, download + ingest each.

    Returns True if successful, False if blocked (hard YT error).
    resume_from_video: if set, skip videos until we reach this URL, then proceed.
    """
    global _current_channel, _current_video_url, _current_video_title

    name     = cfg["name"]
    mode     = cfg["mode"]
    url      = cfg["url"]
    show_dir = BASE_DIR / name
    show_dir.mkdir(parents=True, exist_ok=True)

    _current_channel = key
    _save_run_position(phase="yt_sweep", channel=key)

    # Determine dateafter from yt_channels table
    last_checked = _get_channel_last_checked(key)
    if last_checked:
        dateafter = last_checked
    else:
        # Default: 7 days ago
        dateafter = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

    log(f"[YT] {name} — dateafter={dateafter}")
    on_disk = disk_titles(show_dir)

    # Gather videos to download
    if mode == "playlists":
        try:
            playlists = get_playlists(url)
        except Exception as exc:
            log(f"[YT] {name} — get_playlists failed: {exc}")
            _update_yt_channel(key, name, url)
            return True

        to_download = []
        for pl in playlists:
            try:
                pl_videos = get_playlist_videos(pl["url"])
            except Exception:
                continue
            for v in pl_videos[-RECENT_VIDEOS_CHECK:]:
                if not is_on_disk(v["title"], on_disk):
                    to_download.append({**v, "playlist_title": pl["title"],
                                        "playlist_url": pl["url"]})
    else:
        try:
            videos = _get_recent_videos_with_dateafter(url, dateafter)
        except Exception as exc:
            log(f"[YT] {name} — video list failed: {exc}")
            _update_yt_channel(key, name, url)
            return True

        to_download = [v for v in videos if not is_on_disk(v["title"], on_disk)]

    if not to_download:
        log(f"[YT] {name} — up to date")
        _update_yt_channel(key, name, url)
        return True

    log(f"[YT] {name} — {len(to_download)} new video(s) (cap: {MAX_NEW_PER_CHANNEL}/night)")
    to_download = to_download[:MAX_NEW_PER_CHANNEL]

    # If resuming, skip ahead
    resuming = resume_from_video is not None
    last_downloaded_title = None
    new_downloads = 0

    for v in to_download:
        if _sigterm_received:
            return True

        vid_url = f"https://www.youtube.com/watch?v={v['id']}"

        if resuming:
            if vid_url == resume_from_video or v["id"] == resume_from_video:
                resuming = False  # Found our resume point; start processing this one
            else:
                # Skip — already done in a previous run; registry.is_done() handles dedup
                continue

        # Episode numbering
        if mode == "playlists":
            sn, ep = next_episode_playlist(show_dir, v.get("playlist_title", ""))
            season_dir = show_dir / f"Season {sn:02d}"
            season_dir.mkdir(parents=True, exist_ok=True)
            import json as _json
            info_file = season_dir / ".season_info.json"
            if not info_file.exists():
                info_file.write_text(_json.dumps({
                    "playlist_title": v.get("playlist_title", ""),
                    "playlist_url": v.get("playlist_url", ""),
                    "season_number": sn,
                }, indent=2))
        elif mode == "year":
            year = v.get("upload_date", "")[:4] or datetime.now().strftime("%Y")
            sn, ep = next_episode_year(show_dir, year)
            season_dir = show_dir / f"Season {sn:02d}"
            season_dir.mkdir(parents=True, exist_ok=True)
            import json as _json
            info_file = season_dir / ".season_info.json"
            if not info_file.exists():
                info_file.write_text(_json.dumps({
                    "year": year, "season_number": sn,
                }, indent=2))
        else:  # single
            sn, ep = next_episode_single(show_dir)
            season_dir = show_dir / f"Season {sn:02d}"
            season_dir.mkdir(parents=True, exist_ok=True)

        title_safe = sanitize(v["title"])
        filename   = f"{name} - S{sn:02d}E{ep:04d} - {title_safe}.mp4"
        out_path   = season_dir / filename
        path_key   = str(out_path)

        # Save current position to DB for resume support
        _current_video_url = vid_url
        _current_video_title = v["title"]
        _save_run_position(
            video_url=vid_url,
            video_title=v["title"],
            videos_downloaded=run_counters["downloaded"],
            videos_ingested=run_counters["ingested"],
        )

        # Skip if already fully processed
        if registry.is_done(path_key):
            log(f"  [skip] already done: {v['title'][:60]}")
            continue

        log(f"  [dl] S{sn:02d}E{ep:04d} — {v['title'][:70]}")
        result, stderr = _yt_download_video(v["id"], out_path, dateafter=None)

        if result == "skip":
            log(f"  [skip] already on disk: {v['title'][:60]}")
            registry.register_file(path_key, show_name=name, title=v["title"],
                                   ingest_script="nova_nightly_media.py")
            registry.mark_status(path_key, "skipped")
            continue

        if result.startswith("error:"):
            block_type = _detect_yt_block(stderr)
            if block_type == "hard":
                log(f"  [YT BLOCKED] hard error on {name}: {result[:120]}")
                notify(
                    f":warning: *YT Auth Error* — nightly media pipeline blocked at "
                    f"*{name}* / _{v['title'][:60]}_. "
                    f"Fix Safari session and I'll resume automatically tomorrow, or restart manually.",
                    channel=SLACK_CHAT,
                )
                # Save paused position
                _save_run_position(
                    status="paused_yt_blocked",
                    channel=key,
                    video_url=vid_url,
                    video_title=v["title"],
                    error_msg=result[:400],
                )
                # Continue to next channel (don't abort entire run)
                return False
            else:
                # Soft error or unavailable — register and continue silently
                log(f"  [soft error] {v['title'][:60]}: {result[:80]}")
                registry.register_file(path_key, show_name=name, title=v["title"],
                                       ingest_script="nova_nightly_media.py")
                registry.mark_status(path_key, "error", error_msg=result[:400])
                continue

        # Download succeeded — register and ingest
        run_counters["downloaded"] += 1
        new_downloads += 1
        last_downloaded_title = v["title"]

        registry.register_file(path_key, show_name=name, title=v["title"],
                               ingest_script="nova_nightly_media.py")
        registry.mark_status(path_key, "downloaded")

        # Transcribe inline
        ingest_result = ingest_video(out_path, name, v["title"])
        chunks_added = ingest_result["chunks"]
        word_count = ingest_result["words"]

        if chunks_added > 0:
            run_counters["ingested"] += 1
            notify(
                f":arrow_down: *{name}* — _{v['title'][:80]}_\n"
                f":brain: {chunks_added} memories · {word_count:,} words",
                channel=SLACK_NOTIFY,
            )

        # Random delay between videos (10–45s)
        sleep_secs = random.randint(10, 45)
        log(f"  [sleep] {sleep_secs}s before next video")
        time.sleep(sleep_secs)

    # Update yt_channels table
    _update_yt_channel(
        key, name, url,
        last_video=last_downloaded_title,
        increment_downloaded=new_downloads,
    )
    return True


def run_phase1(resume_channel: str | None = None, resume_video: str | None = None) -> None:
    """Phase 1: iterate all 61 channels, shuffled, downloading + ingesting new videos."""
    log(f"=== Phase 1: YT channel sweep ({len(CHANNELS)} channels) ===")

    channel_keys = list(CHANNELS.keys())
    random.shuffle(channel_keys)

    # If resuming, find the resume channel and put it first
    if resume_channel and resume_channel in channel_keys:
        channel_keys.remove(resume_channel)
        channel_keys.insert(0, resume_channel)

    run_counters = {"downloaded": 0, "ingested": 0}
    resuming_channel = resume_channel is not None
    first_channel = True

    for key in channel_keys:
        if _sigterm_received:
            break

        cfg = CHANNELS[key]
        resume_vid = None
        if resuming_channel and key == resume_channel:
            resume_vid = resume_video
            resuming_channel = False  # Only apply resume logic once

        try:
            ok = _process_yt_channel(key, cfg, run_counters, resume_from_video=resume_vid)
        except Exception as exc:
            log(f"[YT] {cfg['name']} — EXCEPTION: {exc}")
            ok = True  # Don't abort entire phase for one channel failure

        # Random inter-channel delay (30–90s), skip for very first channel
        if not first_channel and not _sigterm_received:
            sleep_secs = random.randint(30, 90)
            log(f"[YT] sleeping {sleep_secs}s before next channel...")
            time.sleep(sleep_secs)
        first_channel = False

    log(f"=== Phase 1 complete: {run_counters['downloaded']} downloaded, "
        f"{run_counters['ingested']} ingested ===")
    _save_run_position(
        phase="sweep",
        status="running",
        videos_downloaded=run_counters["downloaded"],
        videos_ingested=run_counters["ingested"],
    )


# ── Music path detection ──────────────────────────────────────────────────────

def _is_music_path(path: Path) -> bool:
    """
    Return True if this video is in a music-only directory or channel
    and should be skipped for transcription.
    """
    path_lower = str(path).lower()
    # Check for music directory by name
    for part in path.parts:
        if part in MUSIC_DIRS:
            return True
    # Check channel name substrings
    for ch in MUSIC_CHANNELS:
        if ch in path_lower:
            return True
    return False


# ── Phase 2: Full video root sweep ───────────────────────────────────────────

def run_phase2(resume_path: str | None = None) -> None:
    """Phase 2: walk all of VIDEO_ROOT, ingest every unprocessed non-music video."""
    log(f"=== Phase 2: Full sweep of {VIDEO_ROOT} ===")
    _save_run_position(phase="sweep", status="running")

    # Collect all video files
    all_videos: list[Path] = []
    for root, dirs, files in os.walk(VIDEO_ROOT):
        # Prune excluded dirs in-place
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() in VIDEO_EXTS:
                all_videos.append(p)

    log(f"  Found {len(all_videos):,} video files in {VIDEO_ROOT}")

    # If resuming, fast-forward to where we left off
    resuming = resume_path is not None
    processed = 0
    ingested_count = 0
    checkpoint_counter = 0

    for idx, video in enumerate(all_videos):
        if _sigterm_received:
            break

        path_key = str(video)

        if resuming:
            if path_key == resume_path:
                resuming = False
            else:
                resuming = False if path_key == resume_path else resuming
                if resuming:
                    continue

        # Skip if already fully processed
        if registry.is_done(path_key):
            continue

        # Skip music paths entirely
        if _is_music_path(video):
            registry.register_file(path_key,
                                   show_name=video.parent.name,
                                   title=video.stem,
                                   ingest_script="nova_nightly_media.py")
            registry.mark_status(path_key, "skipped", notes="music channel — transcription skipped")
            continue

        show_name = show_name_from_path(video)
        title = video.stem

        # Peek at the next non-music, non-done video for the "Next:" line
        next_title = None
        for nxt in all_videos[idx + 1:idx + 20]:
            if not registry.is_done(str(nxt)) and not _is_music_path(nxt):
                next_title = nxt.stem
                break

        registry.register_file(path_key,
                               show_name=show_name,
                               title=title,
                               ingest_script="nova_nightly_media.py")

        result = ingest_video(video, show_name, title)
        processed += 1
        checkpoint_counter += 1

        if result["chunks"] > 0:
            ingested_count += 1
            memory_snippet = recall_memory_for_show(show_name, result.get("chunks_text"))
            notif_lines = [
                f":clapper: *{show_name}* — _{title[:80]}_",
                f":brain: {result['chunks']} memories · {result['words']:,} words",
            ]
            if memory_snippet:
                notif_lines.append(f":thought_balloon: _\"{memory_snippet[:180]}…\"_")
            if next_title:
                notif_lines.append(f":arrow_forward: *Next:* _{next_title[:80]}_")
            notify("\n".join(notif_lines), channel=SLACK_NOTIFY)

        # Checkpoint every 10 files
        if checkpoint_counter >= 10:
            checkpoint_counter = 0
            _save_run_position(
                phase="sweep",
                video_url=path_key,
                video_title=title,
                videos_ingested=ingested_count,
            )
            log(f"  [checkpoint] {processed} processed, {ingested_count} ingested so far")

    log(f"=== Phase 2 complete: {processed} processed, {ingested_count} ingested ===")
    _save_run_position(
        phase="sweep",
        status="running",
        videos_ingested=ingested_count,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def main() -> None:
    now = datetime.now()
    log(f"=== nova_nightly_media started — {now.strftime('%Y-%m-%d %H:%M')} ===")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ensure_tables()

    # --- Resume detection ---
    resumable = _find_resumable_run()
    resume_channel = None
    resume_video = None
    resume_sweep_path = None
    skip_phase1 = False
    resumed_from_id = None

    if resumable:
        prev_phase   = resumable.get("phase", "yt_sweep")
        prev_channel = resumable.get("current_channel")
        prev_video   = resumable.get("current_video_url")
        prev_id      = resumable["id"]
        resumed_from_id = prev_id
        log(f"  Resuming from run id={prev_id} phase={prev_phase} channel={prev_channel}")

        if prev_phase == "sweep":
            # Skip Phase 1 entirely, resume Phase 2 from last checkpoint
            skip_phase1 = True
            resume_sweep_path = prev_video
        else:
            # Resume Phase 1 from the channel that was in progress
            resume_channel = prev_channel
            resume_video   = prev_video

    # Start a new run row (linked to the previous one if resuming)
    _start_run(resumed_from=resumed_from_id)

    # --- Phase 1 ---
    if not skip_phase1:
        run_phase1(resume_channel=resume_channel, resume_video=resume_video)
    else:
        log("  Skipping Phase 1 (resuming in Phase 2 sweep)")

    if _sigterm_received:
        log("=== Aborted by SIGTERM ===")
        return

    # --- Phase 2 ---
    run_phase2(resume_path=resume_sweep_path)

    # --- Mark complete ---
    _save_run_position(status="completed", completed=True)
    log(f"=== nova_nightly_media complete — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")


if __name__ == "__main__":
    main()
