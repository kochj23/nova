#!/usr/bin/env python3
"""
nova_bulk_ingest.py — Bulk ingest ALL video files from TVShows + Ripped Movies.

Pipeline per file: video → ffmpeg (extract 16kHz mono WAV) → mlx_whisper (transcribe) → vector memory
Parallelism: 3 concurrent whisper workers (balanced for M3 Ultra 512GB + large-v3-turbo model)
Status: Posts to #nova-notifications every 5 minutes.
Resilience: nohup-safe, crash-recoverable via .ingested marker files.

Usage:
  nohup python3 nova_bulk_ingest.py &

Written by Jordan Koch.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from multiprocessing import Manager
from pathlib import Path
from threading import Thread, Event

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

MEDIA_DIRS = [
    Path("/Volumes/external/videos/TVShows"),
    Path("/Volumes/external/videos/Ripped Movies"),
]
WORK_DIR = Path("/Volumes/Data/nova-bulk-ingest")
AUDIO_DIR = WORK_DIR / "audio"
TRANSCRIPT_DIR = WORK_DIR / "transcripts"
STATE_DIR = WORK_DIR / "state"

MEMORY_URL = "http://127.0.0.1:18790/remember?async=1"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
STATUS_INTERVAL = 300  # 5 minutes
MAX_WORKERS = 3  # Parallel transcription workers
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".ts", ".flv", ".wmv"}

LOG_FILE = WORK_DIR / "bulk-ingest.log"

# ── Globals ───────────────────────────────────────────────────────────────────

shutdown_requested = False


def signal_handler(sig, frame):
    global shutdown_requested
    shutdown_requested = True
    log("Shutdown requested — finishing active workers...")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[bulk-ingest {ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def post_status(stats):
    now = time.time()
    elapsed = now - stats["start_time"]
    mins = int(elapsed // 60)
    hours = mins // 60
    remaining_mins = mins % 60

    total = stats["total_files"]
    done = stats["completed"]
    errors = stats["errors"]
    skipped = stats["skipped"]
    pct = (done / total * 100) if total > 0 else 0

    # ETA calculation
    if done > 0:
        avg_per_file = elapsed / done
        remaining = (total - done - skipped) * avg_per_file
        eta_mins = int(remaining // 60)
        eta_str = f"{eta_mins // 60}h {eta_mins % 60}m" if eta_mins > 60 else f"{eta_mins}m"
    else:
        eta_str = "calculating..."

    current_workers = stats.get("active_workers", "?")
    current_file = stats.get("current_file", "")

    msg = (
        f":brain: *Bulk Video Ingest — Status Update*\n"
        f"• Progress: {done}/{total} files ({pct:.1f}%)\n"
        f"• Skipped (already done): {skipped}\n"
        f"• Errors: {errors}\n"
        f"• Memories stored: {stats['memories_stored']}\n"
        f"• Active workers: {current_workers}/{MAX_WORKERS}\n"
        f"• Elapsed: {hours}h {remaining_mins}m | ETA: {eta_str}\n"
        f"• Last completed: `{current_file[-60:]}`"
    )
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception as e:
        log(f"Status post failed: {e}")


# ── File Discovery ───────────────────────────────────────────────────────────

def find_all_videos() -> list[Path]:
    """Find all video files across both media directories."""
    videos = []
    for media_dir in MEDIA_DIRS:
        if not media_dir.exists():
            log(f"WARNING: {media_dir} not found — skipping")
            continue
        for f in sorted(media_dir.rglob("*")):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                # Skip sample files, extras, trailers
                name_lower = f.name.lower()
                if any(skip in name_lower for skip in ["sample", "trailer", ".ds_store"]):
                    continue
                videos.append(f)
    return videos


def is_already_ingested(video_path: Path) -> bool:
    """Check if this file has already been transcribed and ingested."""
    marker = STATE_DIR / f"{video_path.stem}.ingested"
    return marker.exists()


def mark_ingested(video_path: Path):
    """Mark a file as ingested."""
    marker = STATE_DIR / f"{video_path.stem}.ingested"
    marker.write_text(datetime.now().isoformat())


# ── Processing Pipeline ──────────────────────────────────────────────────────

def classify_source(video_path: Path) -> tuple[str, str]:
    """Determine source tag and show name from file path."""
    path_str = str(video_path)

    if "/Ripped Movies/" in path_str:
        return "movie_transcript", video_path.stem

    # TV Shows — extract show name from parent directory structure
    if "/TVShows/" in path_str:
        parts = video_path.relative_to(Path("/Volumes/external/videos/TVShows")).parts
        show_name = parts[0] if parts else "unknown_show"
        return "tv_transcript", show_name

    return "video_transcript", video_path.stem


def extract_audio(video_path: Path) -> Path:
    """Extract audio to 16kHz mono WAV. Returns path to WAV file."""
    wav_path = AUDIO_DIR / f"{video_path.stem}.wav"

    if wav_path.exists() and wav_path.stat().st_size > 1000:
        return wav_path

    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(wav_path),
        ],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:300]}")
    return wav_path


def transcribe(wav_path: Path) -> str:
    """Transcribe WAV file using MLX Whisper. Returns transcript text."""
    txt_path = TRANSCRIPT_DIR / f"{wav_path.stem}.txt"

    if txt_path.exists():
        text = txt_path.read_text().strip()
        if text:
            return text

    # Use --output-name to force exact filename (avoids period-truncation bug)
    safe_name = wav_path.stem
    result = subprocess.run(
        [
            "mlx_whisper",
            "--model", WHISPER_MODEL,
            "--language", "en",
            "--output-format", "txt",
            "--output-dir", str(TRANSCRIPT_DIR),
            "--output-name", safe_name,
            str(wav_path),
        ],
        capture_output=True, text=True, timeout=3600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mlx_whisper failed: {result.stderr[:300]}")

    if txt_path.exists():
        return txt_path.read_text().strip()

    # Fallback: mlx_whisper may have truncated at a period — find closest match
    import glob
    prefix = wav_path.stem.split(".")[0]
    candidates = sorted(TRANSCRIPT_DIR.glob(f"{prefix}*.txt"), key=lambda p: len(p.name), reverse=True)
    for candidate in candidates:
        text = candidate.read_text().strip()
        if text:
            # Copy to expected path for future cache hits
            txt_path.write_text(text)
            return text

    raise FileNotFoundError(f"Transcription not found: {txt_path}")


def chunk_text(text: str, max_chars: int = 1800) -> list[str]:
    """Split text into chunks for vector memory storage."""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    sentences = text.replace(". ", ".\n").split("\n")
    current = ""
    for s in sentences:
        if len(current) + len(s) + 1 > max_chars:
            if current:
                chunks.append(current.strip())
            current = s
        else:
            current = current + " " + s if current else s
    if current.strip():
        chunks.append(current.strip())
    return chunks if chunks else [text[:max_chars]]


def ingest_to_memory(text: str, title: str, source: str, show: str, filename: str) -> int:
    """Store transcript chunks in vector memory. Returns number of chunks stored."""
    if not text or len(text.strip()) < 50:
        return 0

    chunks = chunk_text(text)
    stored = 0
    for i, chunk in enumerate(chunks):
        chunk_title = f"{title} (part {i+1}/{len(chunks)})" if len(chunks) > 1 else title
        payload = json.dumps({
            "text": f"{source} transcription: {chunk_title}\n\n{chunk}",
            "source": source,
            "metadata": {
                "privacy": "local-only",
                "origin": f"{source}-transcription",
                "title": chunk_title,
                "show": show,
                "file": filename,
                "ingested_at": datetime.now().isoformat(),
            },
        }).encode()
        try:
            req = urllib.request.Request(
                MEMORY_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=30)
            stored += 1
        except Exception:
            pass  # Non-fatal — memory server may be briefly busy
    return stored


def process_single_file(video_path: Path) -> dict:
    """Process one video file end-to-end. Returns result dict."""
    result = {
        "file": video_path.name,
        "success": False,
        "memories": 0,
        "words": 0,
        "error": None,
    }

    try:
        source, show = classify_source(video_path)
        title = re.sub(r'\s*\[[\w-]+\]$', '', video_path.stem)  # Strip YouTube IDs

        # Extract audio
        wav_path = extract_audio(video_path)

        # Transcribe
        transcript = transcribe(wav_path)
        result["words"] = len(transcript.split())

        # Ingest to vector memory
        stored = ingest_to_memory(transcript, title, source, show, video_path.name)
        result["memories"] = stored

        # Mark complete and clean up WAV
        mark_ingested(video_path)
        if wav_path.exists():
            wav_path.unlink()

        result["success"] = True

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


# ── Status Reporter Thread ───────────────────────────────────────────────────

def status_reporter(stats, stop_event):
    """Background thread that posts status every 5 minutes."""
    while not stop_event.is_set():
        stop_event.wait(STATUS_INTERVAL)
        if not stop_event.is_set():
            post_status(stats)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Setup directories
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(exist_ok=True)
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    STATE_DIR.mkdir(exist_ok=True)

    # Discover all video files
    log("Scanning for video files...")
    all_videos = find_all_videos()
    log(f"Found {len(all_videos)} total video files")

    # Filter out already-ingested
    to_process = []
    skipped = 0
    for v in all_videos:
        if is_already_ingested(v):
            skipped += 1
        else:
            to_process.append(v)

    log(f"To process: {len(to_process)} files ({skipped} already ingested, skipping)")

    if not to_process:
        log("Nothing to do — all files already ingested!")
        nova_config.post_both(
            ":white_check_mark: *Bulk Video Ingest* — Nothing to do, all files already ingested!",
            slack_channel=nova_config.SLACK_NOTIFY
        )
        return

    # Stats dict (shared across threads)
    stats = {
        "total_files": len(to_process) + skipped,
        "completed": 0,
        "skipped": skipped,
        "errors": 0,
        "memories_stored": 0,
        "words_transcribed": 0,
        "start_time": time.time(),
        "current_file": "",
        "active_workers": 0,
    }

    # Start status reporter thread
    stop_event = Event()
    reporter = Thread(target=status_reporter, args=(stats, stop_event), daemon=True)
    reporter.start()

    # Post initial status
    nova_config.post_both(
        f":rocket: *Bulk Video Ingest — Starting*\n"
        f"• Total files: {len(to_process)} to process ({skipped} already done)\n"
        f"• Sources: TVShows + Ripped Movies\n"
        f"• Workers: {MAX_WORKERS} parallel\n"
        f"• Model: {WHISPER_MODEL}\n"
        f"• Work dir: {WORK_DIR}",
        slack_channel=nova_config.SLACK_NOTIFY
    )

    # Process with parallel workers
    # Note: mlx_whisper is GPU-bound so we limit concurrency to avoid OOM
    # Using ProcessPoolExecutor for true parallelism
    completed_count = 0

    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all jobs
        future_to_path = {}
        for video_path in to_process:
            if shutdown_requested:
                break
            future = executor.submit(process_single_file, video_path)
            future_to_path[future] = video_path

        stats["active_workers"] = min(MAX_WORKERS, len(future_to_path))

        # Collect results as they complete
        for future in as_completed(future_to_path):
            if shutdown_requested:
                log("Shutdown — cancelling remaining jobs")
                executor.shutdown(wait=False, cancel_futures=True)
                break

            video_path = future_to_path[future]
            try:
                result = future.result(timeout=3700)
                if result["success"]:
                    stats["completed"] += 1
                    stats["memories_stored"] += result["memories"]
                    stats["words_transcribed"] += result["words"]
                    stats["current_file"] = result["file"]
                    log(f"  OK: {result['file'][:60]} — {result['words']} words, {result['memories']} chunks")
                else:
                    stats["errors"] += 1
                    log(f"  FAIL: {result['file'][:60]} — {result['error']}")
            except Exception as e:
                stats["errors"] += 1
                log(f"  EXCEPTION: {video_path.name[:60]} — {e}")

            completed_count += 1
            remaining = len(future_to_path) - completed_count
            stats["active_workers"] = min(MAX_WORKERS, remaining)

    # Stop status reporter
    stop_event.set()
    reporter.join(timeout=5)

    # Final stats
    elapsed = time.time() - stats["start_time"]
    hours = int(elapsed // 3600)
    mins = int((elapsed % 3600) // 60)

    final_msg = (
        f":white_check_mark: *Bulk Video Ingest — Complete*\n"
        f"• Files processed: {stats['completed']}/{len(to_process)}\n"
        f"• Memories stored: {stats['memories_stored']}\n"
        f"• Words transcribed: {stats['words_transcribed']:,}\n"
        f"• Errors: {stats['errors']}\n"
        f"• Total time: {hours}h {mins}m"
    )
    nova_config.post_both(final_msg, slack_channel=nova_config.SLACK_NOTIFY)
    log(f"\nDone! {stats['completed']} files, {stats['memories_stored']} memories in {hours}h {mins}m.")


if __name__ == "__main__":
    main()
