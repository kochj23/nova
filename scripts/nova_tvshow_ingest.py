#!/usr/bin/env python3
"""
nova_tvshow_ingest.py — Ingest TV show episodes into Nova's vector memory.

Extracts audio, transcribes via MLX Whisper, stores chunked transcripts
with show/season/episode metadata.

100% local — MLX Whisper on Apple Silicon. 5-min Slack status updates.

Usage:
  python3 nova_tvshow_ingest.py /path/to/show/folder [--source vehicles]
  python3 nova_tvshow_ingest.py /path/to/folder1 /path/to/folder2 ...

Written by Jordan Koch.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread, Event

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
VECTOR_URL = "http://127.0.0.1:18790/remember"
SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_NOTIFY
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
STATUS_INTERVAL = 300

shutdown = Event()

stats = {
    "total_files": 0,
    "processed": 0,
    "transcribed": 0,
    "chunks_stored": 0,
    "errors": 0,
    "current_file": "",
    "start_time": 0,
    "total_transcript_chars": 0,
}

SOURCE = "vehicles"  # Default source, can be overridden via --source


def log(msg):
    print(f"[tvshow_ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text):
    if not SLACK_TOKEN:
        return
    try:
        payload = json.dumps({"channel": SLACK_CHAN, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {SLACK_TOKEN}"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def vector_remember(text, metadata):
    payload = json.dumps({
        "text": text,
        "source": SOURCE,
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log(f"  Memory write failed: {e}")
        return False


def parse_episode(filename, parent_dir=""):
    """Extract show name, season, episode from filename.

    Examples:
      'A Car Is Born - S01E02.m4v' → ('A Car Is Born', 1, 2)
      'A Racing car is born - S01E05.m4v' → ('A Racing Car Is Born', 1, 5)
    """
    stem = Path(filename).stem

    # Try S01E02 pattern
    match = re.search(r'[- ]+S(\d+)E(\d+)', stem, re.IGNORECASE)
    if match:
        show = stem[:match.start()].strip(' -')
        season = int(match.group(1))
        episode = int(match.group(2))
        return _normalize_show(show, parent_dir), season, episode

    # Try "1x02" pattern
    match = re.search(r'[- ]+(\d+)x(\d+)', stem, re.IGNORECASE)
    if match:
        show = stem[:match.start()].strip(' -')
        season = int(match.group(1))
        episode = int(match.group(2))
        return _normalize_show(show, parent_dir), season, episode

    # Fallback: use parent directory as show name
    show = parent_dir or stem
    return _normalize_show(show, ""), 1, 0


def _normalize_show(show, parent_dir):
    """Title-case and clean up show name."""
    if not show or len(show) < 3:
        show = parent_dir
    return show.strip().title()


def get_duration(video_path):
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=30
        )
        return float(result.stdout.strip())
    except Exception:
        return 0


def extract_audio(video_path, output_dir):
    audio_path = Path(output_dir) / "audio.wav"
    try:
        subprocess.run([
            FFMPEG, "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            "-y", str(audio_path)
        ], capture_output=True, timeout=600)
        if audio_path.exists() and audio_path.stat().st_size > 1000:
            return str(audio_path)
    except Exception as e:
        log(f"  Audio extraction error: {e}")
    return None


def transcribe(audio_path):
    try:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
        )
        return result.get("text", "").strip()
    except ImportError:
        log("ERROR: mlx-whisper not installed. Run: pip3 install mlx-whisper")
        return ""
    except Exception as e:
        log(f"  Transcription error: {e}")
        return ""


def status_reporter():
    while not shutdown.is_set():
        shutdown.wait(STATUS_INTERVAL)
        if shutdown.is_set():
            break
        post_status()


def post_status():
    elapsed = time.time() - stats["start_time"]
    pct = (stats["processed"] / stats["total_files"] * 100) if stats["total_files"] else 0
    remaining = stats["total_files"] - stats["processed"]

    if stats["processed"] > 0:
        avg_per = elapsed / stats["processed"]
        eta = str(timedelta(seconds=int(remaining * avg_per)))
    else:
        eta = "calculating..."

    msg = (
        f":wrench: *TV Show Ingest Status* (source: `{SOURCE}`)\n"
        f"  Processed: {stats['processed']}/{stats['total_files']} ({pct:.0f}%)\n"
        f"  Transcribed: {stats['transcribed']} episodes, {stats['total_transcript_chars']:,} chars\n"
        f"  Memory chunks: {stats['chunks_stored']}\n"
        f"  Current: {stats['current_file']}\n"
        f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}\n"
        f"  ETA: {eta}"
    )
    if stats["errors"]:
        msg += f"\n  Errors: {stats['errors']}"
    slack_post(msg)
    log(f"Status: {stats['processed']}/{stats['total_files']} ({pct:.0f}%), ETA {eta}")


def ingest_one(video_path, parent_dir=""):
    video_path = Path(video_path)
    show, season, episode = parse_episode(video_path.name, parent_dir)
    duration = get_duration(video_path)
    dur_str = str(timedelta(seconds=int(duration))) if duration else "?"
    ep_label = f"S{season:02d}E{episode:02d}" if episode else f"S{season:02d}"

    log(f"Processing: {show} {ep_label} ({dur_str})")
    stats["current_file"] = f"{show} {ep_label}"

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = extract_audio(video_path, tmpdir)
        if not audio_path:
            log(f"  No audio — skipping")
            stats["errors"] += 1
            return None

        transcript = transcribe(audio_path)
        if not transcript or len(transcript) < 50:
            log(f"  Transcript too short ({len(transcript)} chars) — skipping")
            stats["errors"] += 1
            return None

    stats["transcribed"] += 1
    stats["total_transcript_chars"] += len(transcript)
    log(f"  Transcribed: {len(transcript):,} characters")

    # Store metadata
    meta_text = (
        f"TV Show: {show}, {ep_label}. "
        f"Duration: {dur_str}. File: {video_path.name}."
    )
    vector_remember(meta_text, {
        "type": "tvshow_metadata",
        "show": show,
        "season": season,
        "episode": episode,
        "filename": video_path.name,
        "duration": duration,
    })
    stats["chunks_stored"] += 1

    # Chunk and store transcript
    words = transcript.split()
    chunks = []
    current = []
    current_len = 0
    for word in words:
        current.append(word)
        current_len += len(word) + 1
        if current_len >= 800:
            chunks.append(" ".join(current))
            current = []
            current_len = 0
    if current:
        chunks.append(" ".join(current))

    for i, chunk in enumerate(chunks):
        chunk_text = (
            f"{show} {ep_label} (transcript part {i+1}/{len(chunks)}):\n{chunk}"
        )
        ok = vector_remember(chunk_text, {
            "type": "tvshow_transcript",
            "show": show,
            "season": season,
            "episode": episode,
            "part": i + 1,
            "total_parts": len(chunks),
        })
        if ok:
            stats["chunks_stored"] += 1

    log(f"  Stored: {len(chunks)} transcript chunks")
    return {
        "show": show,
        "episode": ep_label,
        "transcript_length": len(transcript),
        "chunks": len(chunks),
        "duration": dur_str,
    }


def ingest_folders(folder_paths):
    all_videos = []
    for fp in folder_paths:
        folder = Path(fp)
        if not folder.is_dir():
            log(f"Not a directory: {folder}")
            continue
        parent_name = folder.name
        # Recursive search to handle Season subdirectories
        videos = sorted([
            (f, parent_name) for f in folder.rglob("*")
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
        ])
        all_videos.extend(videos)
        log(f"  {parent_name}: {len(videos)} episodes")

    if not all_videos:
        log("No video files found")
        return

    stats["total_files"] = len(all_videos)
    stats["start_time"] = time.time()

    log(f"Total: {len(all_videos)} episodes across {len(folder_paths)} show(s)")

    # Show summary
    shows = {}
    for v, parent in all_videos:
        show, _, _ = parse_episode(v.name, parent)
        shows[show] = shows.get(show, 0) + 1
    show_list = ", ".join(f"{s} ({c})" for s, c in sorted(shows.items()))

    slack_post(
        f":wrench: *TV Show Ingest Starting* (source: `{SOURCE}`)\n"
        f"  Episodes: {len(all_videos)}\n"
        f"  Shows: {show_list}\n"
        f"  Pipeline: ffmpeg audio → MLX Whisper → vector memory\n"
        f"  100% local, no cloud\n"
        f"  Status updates every 5 minutes"
    )

    reporter = Thread(target=status_reporter, daemon=True)
    reporter.start()

    results = []
    for video, parent in all_videos:
        if shutdown.is_set():
            break
        try:
            result = ingest_one(video, parent)
            if result:
                results.append(result)
        except Exception as e:
            log(f"  ERROR: {video.name}: {e}")
            stats["errors"] += 1
        stats["processed"] += 1

    shutdown.set()
    elapsed = time.time() - stats["start_time"]

    if results:
        lines = [f":wrench: *TV Show Ingest Complete* (source: `{SOURCE}`)"]
        lines.append(f"  Episodes: {len(results)}/{len(all_videos)}")
        lines.append(f"  Transcript: {stats['total_transcript_chars']:,} chars")
        lines.append(f"  Memory chunks: {stats['chunks_stored']}")
        lines.append(f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}")
        lines.append("")
        for show_name in sorted(shows.keys()):
            show_results = [r for r in results if r["show"] == show_name]
            if show_results:
                total_chars = sum(r["transcript_length"] for r in show_results)
                lines.append(f"  :tv: {show_name}: {len(show_results)} eps, {total_chars:,} chars")
        if stats["errors"]:
            lines.append(f"\n  Errors: {stats['errors']}")
        slack_post("\n".join(lines))

    log(f"Done. {len(results)} episodes, {stats['chunks_stored']} chunks, {str(timedelta(seconds=int(elapsed)))}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova TV Show Ingest")
    parser.add_argument("paths", nargs="+", help="Show folder(s)")
    parser.add_argument("--source", default="vehicles", help="Memory source tag (default: vehicles)")
    args = parser.parse_args()

    SOURCE = args.source
    ingest_folders(args.paths)
