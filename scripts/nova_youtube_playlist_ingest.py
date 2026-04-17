#!/usr/bin/env python3
"""
nova_youtube_playlist_ingest.py — Download, transcribe, and ingest YouTube playlists.

Downloads audio-only via yt-dlp, transcribes with MLX Whisper, and stores
chunked transcripts in Nova's vector memory.

100% local — no cloud APIs. 5-min Slack status updates.

Usage:
  python3 nova_youtube_playlist_ingest.py PLAYLIST_URL --source occult
  python3 nova_youtube_playlist_ingest.py PLAYLIST_URL --source religion --tag "Kabbalah"

Written by Jordan Koch.
"""

import json
import os
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

VECTOR_URL = "http://127.0.0.1:18790/remember"
SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_NOTIFY
YT_DLP = "/opt/homebrew/bin/yt-dlp"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
STATUS_INTERVAL = 300

shutdown = Event()
stats = {
    "total": 0, "processed": 0, "transcribed": 0, "chunks_stored": 0,
    "errors": 0, "current": "", "start_time": 0, "total_chars": 0,
}
SOURCE = "occult"
TAG = ""


def log(msg):
    print(f"[yt_ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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
    payload = json.dumps({"text": text, "source": SOURCE, "metadata": metadata}).encode()
    try:
        req = urllib.request.Request(VECTOR_URL, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log(f"  Memory write failed: {e}")
        return False


def get_playlist_info(playlist_url):
    """Get video metadata from playlist via yt-dlp."""
    log(f"Fetching playlist metadata...")
    result = subprocess.run(
        [YT_DLP, "--flat-playlist", "--dump-json", playlist_url],
        capture_output=True, text=True, timeout=120
    )
    videos = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            videos.append({
                "id": d.get("id", ""),
                "title": d.get("title", "Unknown"),
                "duration": d.get("duration", 0),
                "url": d.get("url", d.get("webpage_url", f"https://www.youtube.com/watch?v={d.get('id', '')}")),
            })
        except json.JSONDecodeError:
            continue
    return videos


def download_audio(video_id, title, tmpdir):
    """Download audio-only via yt-dlp, convert to 16kHz WAV."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    audio_path = os.path.join(tmpdir, f"{video_id}.wav")

    try:
        # Download best audio, convert to 16kHz mono WAV
        subprocess.run([
            YT_DLP, "-x", "--audio-format", "wav",
            "--postprocessor-args", "ffmpeg:-ar 16000 -ac 1",
            "-o", os.path.join(tmpdir, f"{video_id}.%(ext)s"),
            "--no-playlist", url
        ], capture_output=True, timeout=300)

        # Find the output file (yt-dlp may name it differently)
        for f in os.listdir(tmpdir):
            if f.startswith(video_id) and f.endswith(".wav"):
                return os.path.join(tmpdir, f)

        # Fallback: try converting whatever was downloaded
        for f in os.listdir(tmpdir):
            if f.startswith(video_id):
                src = os.path.join(tmpdir, f)
                subprocess.run([
                    FFMPEG, "-i", src, "-ar", "16000", "-ac", "1", "-y", audio_path
                ], capture_output=True, timeout=120)
                if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
                    return audio_path
    except Exception as e:
        log(f"  Download error: {e}")
    return None


def transcribe(audio_path):
    """Transcribe audio with MLX Whisper."""
    try:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
        )
        return result.get("text", "").strip()
    except ImportError:
        log("ERROR: mlx-whisper not installed")
        return ""
    except Exception as e:
        log(f"  Transcription error: {e}")
        return ""


def status_reporter():
    while not shutdown.is_set():
        shutdown.wait(STATUS_INTERVAL)
        if shutdown.is_set():
            break
        elapsed = time.time() - stats["start_time"]
        pct = (stats["processed"] / stats["total"] * 100) if stats["total"] else 0
        remaining = stats["total"] - stats["processed"]
        if stats["processed"] > 0:
            eta = str(timedelta(seconds=int(remaining * elapsed / stats["processed"])))
        else:
            eta = "calculating..."
        slack_post(
            f":scroll: *YouTube Playlist Ingest* (source: `{SOURCE}`)\n"
            f"  Processed: {stats['processed']}/{stats['total']} ({pct:.0f}%)\n"
            f"  Transcribed: {stats['transcribed']}, {stats['total_chars']:,} chars\n"
            f"  Chunks: {stats['chunks_stored']}\n"
            f"  Current: {stats['current']}\n"
            f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}, ETA: {eta}"
        )


def ingest_video(video, tmpdir):
    """Download, transcribe, and ingest one video."""
    vid = video["id"]
    title = video["title"]
    duration = video.get("duration", 0)
    dur_str = str(timedelta(seconds=int(duration))) if duration else "?"

    log(f"Processing: {title} ({dur_str})")
    stats["current"] = title[:60]

    # Download audio
    audio_path = download_audio(vid, title, tmpdir)
    if not audio_path:
        log(f"  No audio — skipping")
        stats["errors"] += 1
        return None

    # Transcribe
    transcript = transcribe(audio_path)

    # Clean up audio immediately
    try:
        os.unlink(audio_path)
    except Exception:
        pass

    if not transcript or len(transcript) < 50:
        log(f"  Transcript too short ({len(transcript)} chars) — skipping")
        stats["errors"] += 1
        return None

    stats["transcribed"] += 1
    stats["total_chars"] += len(transcript)
    log(f"  Transcribed: {len(transcript):,} characters")

    # Store metadata
    meta_text = f"YouTube video: {title}. Duration: {dur_str}. Video ID: {vid}."
    if TAG:
        meta_text += f" Topic: {TAG}."
    vector_remember(meta_text, {
        "type": "youtube_metadata",
        "title": title,
        "video_id": vid,
        "duration": duration,
        "tag": TAG,
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
        chunk_text = f"{title} (part {i+1}/{len(chunks)}):\n{chunk}"
        ok = vector_remember(chunk_text, {
            "type": "youtube_transcript",
            "title": title,
            "video_id": vid,
            "part": i + 1,
            "total_parts": len(chunks),
            "tag": TAG,
        })
        if ok:
            stats["chunks_stored"] += 1

    log(f"  Stored: {len(chunks)} chunks")
    return {"title": title, "transcript_length": len(transcript), "chunks": len(chunks), "duration": dur_str}


def main(playlist_url):
    videos = get_playlist_info(playlist_url)
    if not videos:
        log("No videos found in playlist")
        return

    stats["total"] = len(videos)
    stats["start_time"] = time.time()

    log(f"Playlist: {len(videos)} videos")
    total_duration = sum(v.get("duration", 0) for v in videos)
    log(f"Total duration: {str(timedelta(seconds=int(total_duration)))}")

    slack_post(
        f":scroll: *YouTube Playlist Ingest Starting*\n"
        f"  Videos: {len(videos)}\n"
        f"  Duration: {str(timedelta(seconds=int(total_duration)))}\n"
        f"  Source: `{SOURCE}`{f' | Tag: {TAG}' if TAG else ''}\n"
        f"  Pipeline: yt-dlp audio → MLX Whisper → vector memory\n"
        f"  100% local, status every 5 min"
    )

    reporter = Thread(target=status_reporter, daemon=True)
    reporter.start()

    results = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for video in videos:
            if shutdown.is_set():
                break
            try:
                result = ingest_video(video, tmpdir)
                if result:
                    results.append(result)
            except Exception as e:
                log(f"  ERROR: {e}")
                stats["errors"] += 1
            stats["processed"] += 1

    shutdown.set()
    elapsed = time.time() - stats["start_time"]

    if results:
        slack_post(
            f":white_check_mark: *YouTube Playlist Ingest Complete*\n"
            f"  Videos: {len(results)}/{len(videos)}\n"
            f"  Transcript: {stats['total_chars']:,} chars\n"
            f"  Chunks: {stats['chunks_stored']}\n"
            f"  Source: `{SOURCE}`\n"
            f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}"
            + (f"\n  Errors: {stats['errors']}" if stats["errors"] else "")
        )

    log(f"Done. {len(results)} videos, {stats['chunks_stored']} chunks, {str(timedelta(seconds=int(elapsed)))}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="YouTube Playlist Ingest")
    parser.add_argument("url", help="YouTube playlist URL")
    parser.add_argument("--source", default="occult", help="Memory source tag")
    parser.add_argument("--tag", default="", help="Additional topic tag")
    args = parser.parse_args()

    SOURCE = args.source
    TAG = args.tag
    main(args.url)
