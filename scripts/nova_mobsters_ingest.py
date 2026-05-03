#!/usr/bin/env python3
"""
nova_mobsters_ingest.py — Extract audio, transcribe, and ingest "Mobsters (2007)"
episodes into Nova's vector memory.

Pipeline: video (.ts) → ffmpeg (extract WAV) → mlx_whisper (transcribe) → vector memory
Posts 5-minute status updates to #nova-notifications.

Written by Jordan Koch.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

MEDIA_PATH = Path("/Volumes/external/videos/TVShows/Mobsters (2007)")
WORK_DIR = Path("/Volumes/Data/nova-mobsters-ingest")
MEMORY_URL = "http://127.0.0.1:18790/remember?async=1"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
STATUS_INTERVAL = 300
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".ts"}
LOG_FILE = Path("/tmp/nova-mobsters-ingest.log")

shutdown_requested = False

stats = {
    "total_episodes": 0,
    "extracted": 0,
    "transcribed": 0,
    "ingested": 0,
    "errors": 0,
    "start_time": 0,
    "current_episode": "",
    "memories_stored": 0,
}
last_status_time = 0


def signal_handler(sig, frame):
    global shutdown_requested
    shutdown_requested = True
    log("Shutdown requested, finishing current episode...")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[mobsters {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def post_status(force=False):
    global last_status_time
    now = time.time()
    if not force and now - last_status_time < STATUS_INTERVAL:
        return
    last_status_time = now

    elapsed = now - stats["start_time"]
    mins = int(elapsed // 60)
    pct = (stats["ingested"] / stats["total_episodes"] * 100) if stats["total_episodes"] > 0 else 0

    msg = (
        f":gun: *Mobsters (2007) Ingest — Status*\n"
        f"• Progress: {stats['ingested']}/{stats['total_episodes']} episodes ({pct:.0f}%)\n"
        f"• Audio extracted: {stats['extracted']} | Transcribed: {stats['transcribed']}\n"
        f"• Memories stored: {stats['memories_stored']}\n"
        f"• Errors: {stats['errors']}\n"
        f"• Elapsed: {mins} min\n"
        f"• Current: `{stats['current_episode']}`"
    )
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)


def chunk_text(text, max_chars=1800):
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


def remember(text, title, episode_file):
    if not text or len(text.strip()) < 50:
        return 0

    chunks = chunk_text(text)
    stored = 0
    for i, chunk in enumerate(chunks):
        chunk_title = f"{title} (part {i+1}/{len(chunks)})" if len(chunks) > 1 else title
        payload = json.dumps({
            "text": f"Mobsters (2007) episode transcription: {chunk_title}\n\n{chunk}",
            "source": "local_knowledge",
            "metadata": {
                "privacy": "local-only",
                "origin": "mobsters-2007-transcription",
                "title": chunk_title,
                "show": "Mobsters (2007)",
                "file": episode_file,
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
        except Exception as e:
            log(f"  Memory store failed for chunk {i+1}: {e}")
            stats["errors"] += 1
    return stored


def extract_audio(video_path, output_dir):
    wav_path = output_dir / f"{video_path.stem}.wav"
    if wav_path.exists() and wav_path.stat().st_size > 1000:
        log(f"  Audio already extracted: {wav_path.name}")
        return wav_path

    log(f"  Extracting audio with ffmpeg...")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(wav_path),
        ],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")
    stats["extracted"] += 1
    return wav_path


def transcribe(wav_path, output_dir):
    txt_path = output_dir / f"{wav_path.stem}.txt"
    if txt_path.exists():
        text = txt_path.read_text().strip()
        if text:
            log(f"  Using cached transcription: {len(text)} chars")
            return text

    log(f"  Transcribing with MLX Whisper (large-v3-turbo)...")
    result = subprocess.run(
        [
            "mlx_whisper",
            "--model", WHISPER_MODEL,
            "--language", "en",
            "--output-format", "txt",
            "--output-dir", str(output_dir),
            str(wav_path),
        ],
        capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mlx_whisper failed: {result.stderr[:500]}")

    if txt_path.exists():
        text = txt_path.read_text().strip()
        stats["transcribed"] += 1
        return text
    raise FileNotFoundError(f"Transcription output not found: {txt_path}")


def parse_episode_title(filename):
    stem = Path(filename).stem
    stem = re.sub(r'\s*\[[\w-]+\]$', '', stem)
    return stem


def find_episodes():
    episodes = []
    for f in sorted(MEDIA_PATH.rglob("*")):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
            episodes.append(f)
    return sorted(episodes, key=lambda p: p.name)


def main():
    stats["start_time"] = time.time()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    audio_dir = WORK_DIR / "audio"
    transcript_dir = WORK_DIR / "transcripts"
    audio_dir.mkdir(exist_ok=True)
    transcript_dir.mkdir(exist_ok=True)

    episodes = find_episodes()
    stats["total_episodes"] = len(episodes)

    log(f"Found {len(episodes)} video files in '{MEDIA_PATH}'")
    log(f"Work directory: {WORK_DIR}")
    log(f"Model: {WHISPER_MODEL}")

    post_status(force=True)

    for ep in episodes:
        if shutdown_requested:
            log("Shutdown — stopping.")
            break

        title = parse_episode_title(ep.name)
        stats["current_episode"] = title[:60]
        log(f"\n{'='*60}")
        log(f"Processing: {title}")
        log(f"File: {ep.name} ({ep.stat().st_size / 1024 / 1024:.0f} MB)")

        done_marker = transcript_dir / f"{ep.stem}.ingested"
        if done_marker.exists():
            log(f"  SKIP (already ingested)")
            stats["ingested"] += 1
            post_status()
            continue

        try:
            wav_path = extract_audio(ep, audio_dir)
            transcript = transcribe(wav_path, transcript_dir)
            log(f"  Transcription: {len(transcript)} chars, {len(transcript.split())} words")

            stored = remember(transcript, title, ep.name)
            stats["memories_stored"] += stored
            stats["ingested"] += 1

            done_marker.write_text(datetime.now().isoformat())
            log(f"  Done — stored {stored} memory chunks")

            if wav_path.exists():
                wav_path.unlink()
                log(f"  Cleaned up WAV")

        except Exception as e:
            log(f"  ERROR: {e}")
            stats["errors"] += 1

        post_status()

    elapsed = time.time() - stats["start_time"]
    mins = int(elapsed // 60)
    final_msg = (
        f":white_check_mark: *Mobsters (2007) Ingest — Complete*\n"
        f"• Episodes processed: {stats['ingested']}/{stats['total_episodes']}\n"
        f"• Memories stored: {stats['memories_stored']}\n"
        f"• Errors: {stats['errors']}\n"
        f"• Total time: {mins} min"
    )
    nova_config.post_both(final_msg, slack_channel=nova_config.SLACK_NOTIFY)
    log(f"\nDone! {stats['ingested']} episodes ingested, {stats['memories_stored']} memories stored in {mins} min.")


if __name__ == "__main__":
    main()
