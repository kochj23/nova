#!/usr/bin/env python3
"""
nova_comedy_ingest.py — Ingest comedy specials into Nova's vector memory.

Extracts audio from comedy video files, transcribes via MLX Whisper,
and stores chunked transcripts with comedian/show metadata.

All processing is 100% local — MLX Whisper on Apple Silicon, no cloud.
Posts 5-minute status updates to #nova-notifications.

Usage:
  python3 nova_comedy_ingest.py /Volumes/external/videos/Comedy/

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
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
STATUS_INTERVAL = 300  # 5 minutes

shutdown = Event()

# ── Stats ────────────────────────────────────────────────────────────────────
stats = {
    "total_files": 0,
    "processed": 0,
    "transcribed": 0,
    "chunks_stored": 0,
    "errors": 0,
    "skipped": 0,
    "current_file": "",
    "start_time": 0,
    "total_transcript_chars": 0,
}


def log(msg):
    print(f"[comedy_ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def vector_remember(text, metadata):
    payload = json.dumps({
        "text": text,
        "source": "comedy",
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


# ── Filename parsing ─────────────────────────────────────────────────────────

def parse_comedian_show(filename):
    """Extract comedian name and show title from filename.

    Examples:
      'Dave Chappelle_ For What It's Worth.m4v' → ('Dave Chappelle', "For What It's Worth")
      'Louis CK_ HBO Special.m4v' → ('Louis CK', 'HBO Special')
      'Eddie Izzard - Dress to Kill.m4v' → ('Eddie Izzard', 'Dress to Kill')
      'LOUIS_CK_CHEWED_UP-2.m4v' → ('Louis CK', 'Chewed Up (Part 2)')
      'Lewis Black Unleashed-3.m4v' → ('Lewis Black', 'Unleashed (Part 3)')
      'Patton Oswalt Outtakes 1_ No Reason to Complain.m4v' → ('Patton Oswalt', 'No Reason to Complain (Outtakes 1)')
    """
    stem = Path(filename).stem

    # Detect part numbers at end: -1, -2, etc.
    part_match = re.search(r'-(\d+)$', stem)
    part_num = int(part_match.group(1)) if part_match else None
    if part_num:
        stem = stem[:part_match.start()]

    # Replace underscores with spaces
    stem = stem.replace('_', ' ')

    # Try splitting on common delimiters
    # "Comedian_ Show" or "Comedian: Show"
    for delim in ['_ ', ': ', ' - ']:
        if delim in stem:
            parts = stem.split(delim, 1)
            comedian = parts[0].strip()
            show = parts[1].strip()
            # Handle "Outtakes N" prefix in show name
            outtakes = re.match(r'Outtakes\s+(\d+)\s*[_:]\s*(.*)', show)
            if outtakes:
                show = f"{outtakes.group(2)} (Outtakes {outtakes.group(1)})"
            if part_num:
                show = f"{show} (Part {part_num})"
            return _normalize_comedian(comedian), show

    # Fallback: try to detect known comedian names
    known = [
        "Dave Chappelle", "Eddie Izzard", "Louis CK", "Louis C.K.",
        "Lewis Black", "Patton Oswalt", "Katt Williams", "Kat Williams",
        "Kevin Smith", "Bill Cosby", "John Waters", "John Watters",
    ]
    # Special case: "Norman Rockwell is Bleeding" is Lewis Black
    if "norman rockwell" in stem.lower():
        return "Lewis Black", "Norman Rockwell is Bleeding"
    # Special case: "An Evening With Kevin Smith"
    if "evening with kevin smith" in stem.lower():
        return "Kevin Smith", "An Evening With Kevin Smith"
    for name in known:
        if name.lower() in stem.lower():
            show = stem[len(name):].strip(' -_:')
            if not show:
                show = stem
            if part_num:
                show = f"{show} (Part {part_num})"
            return _normalize_comedian(name), show

    # Last resort: whole filename is the title
    show = stem
    if part_num:
        show = f"{show} (Part {part_num})"
    return "Unknown", show


def _normalize_comedian(name):
    """Normalize comedian name variations."""
    n = name.strip()
    # Louis CK variations
    if re.match(r'louis\s*c\.?k\.?', n, re.IGNORECASE):
        return "Louis C.K."
    if re.match(r'katt?\s*williams', n, re.IGNORECASE):
        return "Katt Williams"
    if re.match(r'john\s*wat[t]?ers', n, re.IGNORECASE):
        return "John Waters"
    return n


# ── Audio + Transcription ────────────────────────────────────────────────────

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


# ── Status reporter ──────────────────────────────────────────────────────────

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
        eta_s = remaining * avg_per
        eta = str(timedelta(seconds=int(eta_s)))
    else:
        eta = "calculating..."

    msg = (
        f":performing_arts: *Comedy Ingest Status*\n"
        f"  Processed: {stats['processed']}/{stats['total_files']} ({pct:.0f}%)\n"
        f"  Transcribed: {stats['transcribed']} specials, {stats['total_transcript_chars']:,} chars\n"
        f"  Memory chunks stored: {stats['chunks_stored']}\n"
        f"  Current: {stats['current_file']}\n"
        f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}\n"
        f"  ETA: {eta}"
    )
    if stats["errors"]:
        msg += f"\n  Errors: {stats['errors']}"
    if stats["skipped"]:
        msg += f"\n  Skipped (dup): {stats['skipped']}"
    slack_post(msg)
    log(f"Status: {stats['processed']}/{stats['total_files']} ({pct:.0f}%), ETA {eta}")


# ── Main pipeline ────────────────────────────────────────────────────────────

def ingest_one(video_path):
    video_path = Path(video_path)
    comedian, show = parse_comedian_show(video_path.name)
    duration = get_duration(video_path)
    dur_str = str(timedelta(seconds=int(duration))) if duration else "?"

    log(f"Processing: {comedian} — {show} ({dur_str})")
    stats["current_file"] = f"{comedian} — {show}"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Extract audio
        audio_path = extract_audio(video_path, tmpdir)
        if not audio_path:
            log(f"  No audio extracted — skipping")
            stats["errors"] += 1
            return None

        # Transcribe
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
        f"Comedy special: {comedian} — {show}. "
        f"Duration: {dur_str}. "
        f"File: {video_path.name}."
    )
    vector_remember(meta_text, {
        "type": "comedy_metadata",
        "comedian": comedian,
        "show": show,
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
            f"{comedian} — {show} (transcript part {i+1}/{len(chunks)}):\n{chunk}"
        )
        ok = vector_remember(chunk_text, {
            "type": "comedy_transcript",
            "comedian": comedian,
            "show": show,
            "part": i + 1,
            "total_parts": len(chunks),
        })
        if ok:
            stats["chunks_stored"] += 1

    log(f"  Stored: {len(chunks)} transcript chunks")
    return {
        "comedian": comedian,
        "show": show,
        "transcript_length": len(transcript),
        "chunks": len(chunks),
        "duration": dur_str,
    }


def ingest_folder(folder_path):
    folder = Path(folder_path)
    videos = sorted([f for f in folder.iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS])

    if not videos:
        log(f"No video files found in {folder}")
        return

    stats["total_files"] = len(videos)
    stats["start_time"] = time.time()

    log(f"Found {len(videos)} comedy specials in {folder}")
    log(f"Comedians detected:")

    # Preview what we'll process
    comedians = {}
    for v in videos:
        comedian, show = parse_comedian_show(v.name)
        comedians.setdefault(comedian, []).append(show)
    for c, shows in sorted(comedians.items()):
        log(f"  {c}: {len(shows)} special(s)")

    # Notify start
    comedian_list = ", ".join(f"{c} ({len(s)})" for c, s in sorted(comedians.items()))
    slack_post(
        f":performing_arts: *Comedy Ingest Starting*\n"
        f"  Specials: {len(videos)}\n"
        f"  Comedians: {comedian_list}\n"
        f"  Pipeline: ffmpeg audio → MLX Whisper → vector memory\n"
        f"  Source: `comedy` (100% local, no cloud)\n"
        f"  Status updates every 5 minutes"
    )

    # Start status reporter
    reporter = Thread(target=status_reporter, daemon=True)
    reporter.start()

    results = []
    for video in videos:
        if shutdown.is_set():
            break
        try:
            result = ingest_one(video)
            if result:
                results.append(result)
        except Exception as e:
            log(f"  ERROR processing {video.name}: {e}")
            stats["errors"] += 1
        stats["processed"] += 1

    shutdown.set()
    elapsed = time.time() - stats["start_time"]

    # Final summary
    if results:
        lines = [f":performing_arts: *Comedy Ingest Complete*"]
        lines.append(f"  Specials processed: {len(results)}/{len(videos)}")
        lines.append(f"  Total transcript: {stats['total_transcript_chars']:,} characters")
        lines.append(f"  Memory chunks: {stats['chunks_stored']}")
        lines.append(f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}")
        lines.append("")
        for r in results:
            lines.append(f"  :microphone: {r['comedian']} — {r['show']}: {r['transcript_length']:,} chars ({r['duration']})")
        if stats["errors"]:
            lines.append(f"\n  Errors: {stats['errors']}")
        slack_post("\n".join(lines))
    else:
        slack_post(":warning: *Comedy Ingest* — No specials transcribed successfully.")

    log(f"Done. {len(results)} specials, {stats['chunks_stored']} chunks, {str(timedelta(seconds=int(elapsed)))}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 nova_comedy_ingest.py /path/to/comedy/folder")
        sys.exit(1)

    path = Path(sys.argv[1])
    if path.is_dir():
        ingest_folder(path)
    elif path.is_file():
        stats["total_files"] = 1
        stats["start_time"] = time.time()
        ingest_one(path)
    else:
        print(f"Path not found: {path}")
        sys.exit(1)
