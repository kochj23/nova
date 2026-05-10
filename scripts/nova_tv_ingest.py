#!/usr/bin/env python3
"""
nova_tv_ingest.py — Nightly TV show ingest pipeline.

Scans /Volumes/external/videos/ (all subdirs except other/Other),
finds video files modified in the last 3 days that haven't been ingested,
extracts audio with ffmpeg, transcribes with MLX Whisper large-v3-turbo,
filters garbage transcriptions (music, noise, silence), chunks and stores
into Nova's vector memory, then posts a summary to #nova-notifications.

State tracking: ~/.openclaw/workspace/state/tv_ingest_state.json
  - Tracks every file that has been processed (path → metadata)
  - Any file older than 3 days at first-run is marked done without processing

Scheduler: cron 0 23 * * * (11pm daily)

PRIVACY: All TV transcript data is local-only. Never cloud-routed.

Written by Jordan Koch.
"""

import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

VIDEO_ROOT      = Path("/Volumes/external/videos")
EXCLUDED_DIRS   = {"other", "Other"}
VIDEO_EXTS      = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".wmv", ".flv"}
STATE_FILE      = Path.home() / ".openclaw/workspace/state/tv_ingest_state.json"
WORK_DIR        = Path("/Volumes/Data/nova-livetv/tv-ingest")
LOG_FILE        = Path.home() / ".openclaw/logs/nova_tv_ingest.log"
MEMORY_URL      = nova_config.VECTOR_URL + "/remember"
SLACK_CHANNEL   = nova_config.SLACK_NOTIFY

WHISPER_BIN     = "/opt/homebrew/bin/mlx_whisper"
WHISPER_MODEL   = "mlx-community/whisper-large-v3-turbo"
FFMPEG_BIN      = "/opt/homebrew/bin/ffmpeg"

CHUNK_WORDS     = 400           # words per memory chunk
MIN_CHUNK_WORDS = 30            # discard chunks shorter than this
TRASH_RATIO     = 0.6           # if >60% of chunks are garbage → skip whole video
RECENT_DAYS     = 3             # only ingest files modified within this window
MAX_AUDIO_SECS  = 7200          # cap at 2h to avoid runaway jobs (most episodes ≤ 1h)

NOW             = datetime.now()
TODAY           = NOW.strftime("%Y-%m-%d")

# ── Garbage detection patterns ────────────────────────────────────────────────

# Patterns that indicate a chunk is mostly music/noise/non-speech content.
# Used per-chunk; if a chunk matches, it's dropped before ingestion.
_TRASH_PATTERNS = [
    # Music notation artifacts from Whisper
    re.compile(r"[♪♫♬♩]"),
    # Repeated filler (Whisper hallucination on silence/music)
    re.compile(r"\b(\w+)\s+\1\s+\1\s+\1", re.IGNORECASE),
    # All-caps shouting noise
    re.compile(r"^[A-Z\s\W]{20,}$"),
    # Very short with no vowels (transcription noise)
    re.compile(r"^[^aeiouAEIOU\s]{8,}$"),
    # Pure symbol noise
    re.compile(r"^[\W\d\s]+$"),
    # Subtitle/credits artifacts
    re.compile(r"subtitles?\s+by|transcribed\s+by|closed\s+caption", re.IGNORECASE),
    # Silence markers
    re.compile(r"^\[?\s*(silence|music|applause|laughter|cheering|crowd|♪)\s*\]?$", re.IGNORECASE),
    # Extremely repetitive (same phrase ≥ 5 times)
    re.compile(r"(.{5,}?)(\s+\1){4,}"),
]

_MUSIC_PHRASES = [
    "♪", "♫", "la la la", "da da da", "na na na",
    "hmm hmm", "mmm mmm", "woo woo",
]


def is_trash_chunk(text: str) -> bool:
    """Return True if this chunk is likely music, noise, or hallucination."""
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
    # Ratio of alphabetic chars — noise transcriptions are symbol-heavy
    alpha = sum(c.isalpha() for c in stripped)
    if alpha / max(len(stripped), 1) < 0.5:
        return True
    return False


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = NOW.strftime("%H:%M:%S")
    line = f"[tv_ingest {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"done": {}, "last_run": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def mark_done(state: dict, path: str, metadata: dict):
    state["done"][path] = {**metadata, "marked_at": NOW.isoformat()}


# ── Source classification ─────────────────────────────────────────────────────

def classify_source(show_name: str, title: str, snippet: str) -> str:
    """Map show name + transcript content to a Nova memory source tag."""
    text = (show_name + " " + title + " " + snippet[:400]).lower()

    # Show-name based — most reliable
    show = show_name.lower()
    if any(w in show for w in ["forgotten weapon", "forbidden weapon"]):
        return "military_history"
    if any(w in show for w in ["jeopardy", "wheel of fortune", "game show", "price is right"]):
        return "game_show"
    if any(w in show for w in ["documentary", "biography", "civilizations", "connections",
                                 "crash course", "nova ", "frontline", "american experience"]):
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
        return "education"
    if any(w in show for w in ["louis ck", "comedy", "standup", "stand-up", "chug"]):
        return "comedy"
    if any(w in show for w in ["arnie", "gunsmith", "bladesmiths", "how it's made", "build"]):
        return "education"

    # Content fallback
    if any(w in text for w in ["firearm", "rifle", "pistol", "shotgun", "cartridge",
                                 "caliber", "ammunition", "magazine", "barrel", "trigger"]):
        return "military_history"
    if any(w in text for w in ["horsepower", "torque", "carburetor", "engine", "transmission",
                                 "differential", "chassis", "dyno", "lap time", "drag strip"]):
        return "automotive"
    if any(w in text for w in ["history", "war", "battle", "ancient", "civilization", "empire",
                                 "century", "dynasty", "revolution"]):
        return "documentary"
    if any(w in text for w in ["joke", "laugh", "funny", "comedian", "crowd", "audience",
                                 "bit", "stand up"]):
        return "comedy"

    return "television"


# ── Video discovery ───────────────────────────────────────────────────────────

def find_videos(cutoff: datetime) -> list[Path]:
    """Find all video files modified after cutoff, excluding other/Other dirs."""
    results = []
    for root, dirs, files in os.walk(VIDEO_ROOT):
        # Prune excluded dirs in-place
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                if mtime >= cutoff:
                    results.append(p)
            except OSError:
                pass
    return sorted(results, key=lambda p: p.stat().st_mtime)


def show_name_from_path(video: Path) -> str:
    """Extract show name from directory structure."""
    parts = video.parts
    # Look for the directory just above the season or just above the file
    idx = None
    for i, part in enumerate(parts):
        if part.lower().startswith("season") or re.match(r"^s\d{2}$", part.lower()):
            idx = i - 1
            break
    if idx is not None and idx >= 0:
        return parts[idx]
    # Fallback: parent directory name
    return video.parent.name


# ── Audio extraction ──────────────────────────────────────────────────────────

def extract_audio(video: Path, out_wav: Path) -> bool:
    """Extract mono 16kHz WAV from video using ffmpeg. Returns True on success."""
    cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(video),
        "-vn",                           # strip video
        "-ac", "1",                      # mono
        "-ar", "16000",                  # 16kHz (Whisper native)
        "-acodec", "pcm_s16le",          # WAV PCM
        "-t", str(MAX_AUDIO_SECS),       # cap length
        str(out_wav),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=MAX_AUDIO_SECS + 60
        )
        return out_wav.exists() and out_wav.stat().st_size > 1000
    except Exception as exc:
        log(f"  ffmpeg error: {exc}")
        return False


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe(wav: Path, out_dir: Path, stem: str) -> str | None:
    """Transcribe WAV with MLX Whisper. Returns transcript text or None."""
    cmd = [
        WHISPER_BIN,
        str(wav),
        "--model", WHISPER_MODEL,
        "--output-format", "txt",
        "--output-dir", str(out_dir),
        "--output-name", stem,
        "--language", "en",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=MAX_AUDIO_SECS * 2
        )
        txt_path = out_dir / f"{stem}.txt"
        if txt_path.exists():
            text = txt_path.read_text(encoding="utf-8", errors="ignore").strip()
            txt_path.unlink(missing_ok=True)  # clean up
            return text if len(text) > 20 else None
    except subprocess.TimeoutExpired:
        log("  Whisper timeout — skipping")
    except Exception as exc:
        log(f"  Whisper error: {exc}")
    return None


# ── Memory ingestion ──────────────────────────────────────────────────────────

def remember(text: str, source: str, metadata: dict) -> bool:
    payload = json.dumps({
        "text": text[:2000],
        "source": source,
        "tier": "long_term",
        "privacy": "local-only",
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception:
        return False


def chunk_text(text: str) -> list[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), CHUNK_WORDS):
        chunk = " ".join(words[i:i + CHUNK_WORDS])
        if not is_trash_chunk(chunk):
            chunks.append(chunk)
    return chunks


def random_memory_for_show(show_name: str) -> str | None:
    """Pull a random existing memory that matches this show to include in notification."""
    query_payload = json.dumps({
        "query": f"{show_name} television episode",
        "limit": 20,
    }).encode()
    req = urllib.request.Request(
        nova_config.VECTOR_URL + "/recall",
        data=query_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            memories = data.get("memories", data.get("results", []))
            if memories:
                m = random.choice(memories)
                text = m.get("text", "")
                # Strip the show prefix if present
                text = re.sub(r"^\[.*?\]\s*", "", text)
                return text[:200].strip()
    except Exception:
        pass
    return None


# ── Slack notification ────────────────────────────────────────────────────────

def post_slack(msg: str):
    try:
        nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)
    except Exception as exc:
        log(f"Slack error: {exc}")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_video(video: Path, state: dict, work_dir: Path) -> dict | None:
    """
    Full pipeline for one video file.
    Returns result dict on success, None on skip/failure.
    """
    path_key = str(video)

    # Already done?
    if path_key in state["done"]:
        return None

    show_name = show_name_from_path(video)
    title = video.stem
    log(f"Processing: {show_name} — {title}")

    # Extract audio
    wav = work_dir / f"{video.stem[:80]}.wav"
    if not extract_audio(video, wav):
        log(f"  Audio extraction failed — skipping")
        mark_done(state, path_key, {
            "show": show_name, "title": title,
            "status": "audio_failed", "chunks": 0
        })
        return None

    audio_size_mb = round(wav.stat().st_size / 1e6, 1)
    log(f"  Audio: {audio_size_mb} MB")

    # Transcribe
    transcript = transcribe(wav, work_dir, video.stem[:80])
    wav.unlink(missing_ok=True)  # always clean up WAV

    if not transcript:
        log("  No transcript — skipping")
        mark_done(state, path_key, {
            "show": show_name, "title": title,
            "status": "no_transcript", "chunks": 0
        })
        return None

    word_count = len(transcript.split())
    log(f"  Transcript: {word_count} words")

    # Chunk + filter
    chunks = chunk_text(transcript)
    total_raw = max(1, word_count // CHUNK_WORDS)
    trash_ratio = 1 - (len(chunks) / total_raw)

    if trash_ratio > TRASH_RATIO or len(chunks) == 0:
        log(f"  Trash ratio {trash_ratio:.0%} — skipping (likely music/noise)")
        mark_done(state, path_key, {
            "show": show_name, "title": title,
            "status": "trash", "chunks": 0, "trash_ratio": round(trash_ratio, 2)
        })
        return None

    # Determine source
    source = classify_source(show_name, title, transcript[:500])
    log(f"  Source: {source} | {len(chunks)} clean chunks")

    # Ingest chunks
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
                "ingested_date": TODAY,
                "source_file": path_key,
            },
        )
        if ok:
            ingested += 1

    log(f"  Ingested {ingested}/{len(chunks)} chunks")
    mark_done(state, path_key, {
        "show": show_name, "title": title,
        "status": "ingested", "chunks": ingested,
        "source": source, "words": word_count,
    })

    # Per-episode notification to #nova-notifications
    # Try to pull an existing memory for this show; fall back to one of the
    # just-ingested chunks so there's always something to show.
    memory_snippet = random_memory_for_show(show_name)
    if not memory_snippet and chunks:
        snippet_chunk = random.choice(chunks)
        memory_snippet = re.sub(r"^\[.*?\]\s*", "", snippet_chunk)[:200]

    notif_lines = [
        f":clapper: *{show_name}* — _{title[:80]}_",
        f":brain: {ingested} memories stored `[{source}]` · {word_count} words transcribed",
    ]
    if memory_snippet:
        notif_lines.append(f":thought_balloon: _\"{memory_snippet[:180]}…\"_")
    post_slack("\n".join(notif_lines))

    return {
        "show": show_name,
        "title": title,
        "source": source,
        "chunks": ingested,
        "words": word_count,
    }


def main():
    log(f"=== TV Ingest started — {NOW.strftime('%Y-%m-%d %H:%M')} ===")

    # Setup
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    cutoff = NOW - timedelta(days=RECENT_DAYS)

    # Discover videos
    all_videos = find_videos(cutoff)
    new_videos = [v for v in all_videos if str(v) not in state["done"]]
    log(f"Found {len(all_videos)} recent videos, {len(new_videos)} not yet ingested")

    if not new_videos:
        log("Nothing new to ingest.")
        state["last_run"] = NOW.isoformat()
        save_state(state)
        post_slack(
            f":tv: *TV Ingest — {TODAY}*\n"
            f"No new videos to ingest. All caught up."
        )
        return

    # Pre-mark all videos older than 3 days as done (first-run backfill guard)
    backfill_count = 0
    all_existing = find_videos(datetime(2000, 1, 1))
    for v in all_existing:
        path_key = str(v)
        if path_key not in state["done"]:
            mtime = datetime.fromtimestamp(v.stat().st_mtime)
            if mtime < cutoff:
                state["done"][path_key] = {
                    "show": show_name_from_path(v),
                    "title": v.stem,
                    "status": "backfilled",
                    "marked_at": NOW.isoformat(),
                }
                backfill_count += 1
    if backfill_count:
        log(f"Backfilled {backfill_count} older files as done")
        save_state(state)

    # Process new videos
    results_by_show: dict[str, list[dict]] = {}
    total_chunks = 0
    skipped = 0
    failed = 0

    for i, video in enumerate(new_videos):
        log(f"\n[{i+1}/{len(new_videos)}] {video.name}")
        try:
            result = process_video(video, state, WORK_DIR)
            if result:
                show = result["show"]
                results_by_show.setdefault(show, []).append(result)
                total_chunks += result["chunks"]
            else:
                skipped += 1
        except Exception as exc:
            log(f"  ERROR: {exc}")
            failed += 1
            mark_done(state, str(video), {
                "show": show_name_from_path(video),
                "title": video.stem,
                "status": "error",
                "error": str(exc),
            })
        # Save state after each video
        save_state(state)

    state["last_run"] = NOW.isoformat()
    save_state(state)

    # Build Slack notification
    ingested_count = sum(len(eps) for eps in results_by_show.values())

    lines = [
        f":tv: *TV Ingest Complete — {TODAY}*",
        f":white_check_mark: *{ingested_count} episodes ingested* | "
        f":bar_chart: {total_chunks} memory chunks | "
        f":fast_forward: {skipped} skipped | "
        f":x: {failed} errors",
        "",
    ]

    for show, episodes in sorted(results_by_show.items()):
        ep_count = len(episodes)
        chunk_count = sum(e["chunks"] for e in episodes)
        source_tag = episodes[0]["source"]
        lines.append(f"*{show}* — {ep_count} ep{'s' if ep_count > 1 else ''}, "
                     f"{chunk_count} chunks `[{source_tag}]`")

        # Recent episode titles (up to 3)
        for ep in episodes[-3:]:
            lines.append(f"  • _{ep['title'][:70]}_")

        # Pull a random existing memory for this show
        memory_snippet = random_memory_for_show(show)
        if memory_snippet:
            lines.append(f"  :thought_balloon: _{memory_snippet[:180]}…_")

        lines.append("")

    post_slack("\n".join(lines))
    log(f"=== Complete. {ingested_count} ingested, {skipped} skipped, {failed} errors ===")


if __name__ == "__main__":
    main()
