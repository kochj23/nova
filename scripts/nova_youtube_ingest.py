#!/usr/bin/env python3
"""
nova_youtube_ingest.py — Download YouTube playlists, extract audio, transcribe with
MLX Whisper, and ingest transcriptions into Nova's local vector memory.

ALL processing is local (yt-dlp + ffmpeg + mlx_whisper + pgvector).
No data touches any cloud API. Every memory tagged privacy:local-only.

Posts status updates to Slack #nova-notifications every 5 minutes.

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
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

WORK_DIR = Path("/Volumes/Data/nova-youtube-ingest")
MEMORY_URL = "http://127.0.0.1:18790/remember?async=1"
STATUS_INTERVAL = 300  # 5 minutes

LOG_FILE = Path("/tmp/nova-youtube-ingest.log")
STATE_FILE = Path("/tmp/nova-youtube-ingest-state.json")

# ── Globals ───────────────────────────────────────────────────────────────────

stats = {
    "total_videos": 0,
    "downloaded": 0,
    "transcribed": 0,
    "ingested": 0,
    "errors": 0,
    "skipped_dupes": 0,
    "start_time": None,
    "current_video": "",
    "memories_stored": 0,
}
last_status_time = 0
slack_token = None
shutdown_requested = False


def signal_handler(sig, frame):
    global shutdown_requested
    shutdown_requested = True
    log("Shutdown requested, finishing current video...")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[yt-ingest {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_slack_token():
    global slack_token
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova", "-s", "nova-slack-bot-token", "-w"],
            capture_output=True, text=True, timeout=10
        )
        slack_token = result.stdout.strip()
    except Exception:
        pass


def post_slack(message):
    try:
        import nova_config
        nova_config.post_both(message, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception as e:
        log(f"Slack/Discord post failed: {e}")


def post_status():
    global last_status_time
    now = time.time()
    if now - last_status_time < STATUS_INTERVAL:
        return
    last_status_time = now

    elapsed = now - stats["start_time"] if stats["start_time"] else 0
    mins = int(elapsed // 60)
    pct = (stats["ingested"] / stats["total_videos"] * 100) if stats["total_videos"] > 0 else 0

    msg = (
        f":movie_camera: *YouTube Ingest — Status Update*\n"
        f"• Progress: {stats['ingested']}/{stats['total_videos']} videos ({pct:.0f}%)\n"
        f"• Downloaded: {stats['downloaded']} | Transcribed: {stats['transcribed']}\n"
        f"• Memories stored: {stats['memories_stored']}\n"
        f"• Errors: {stats['errors']} | Dupes skipped: {stats['skipped_dupes']}\n"
        f"• Elapsed: {mins} min\n"
        f"• Current: `{stats['current_video']}`"
    )
    post_slack(msg)


def remember(text, title, video_id, playlist_name):
    if not text or len(text.strip()) < 50:
        return 0

    chunks = chunk_text(text, max_chars=2000)
    stored = 0
    for i, chunk in enumerate(chunks):
        chunk_title = f"{title} (part {i+1}/{len(chunks)})" if len(chunks) > 1 else title
        payload = json.dumps({
            "text": chunk,
            "source": "youtube-ingest",
            "metadata": {
                "privacy": "local-only",
                "origin": "youtube-transcription",
                "video_id": video_id,
                "title": chunk_title,
                "playlist": playlist_name,
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
    return stored


def chunk_text(text, max_chars=2000):
    if len(text) <= max_chars:
        return [text]
    chunks = []
    sentences = re.split(r'(?<=[.!?])\s+', text)
    current = ""
    for sent in sentences:
        if len(current) + len(sent) + 1 > max_chars:
            if current:
                chunks.append(current.strip())
            current = sent
        else:
            current = current + " " + sent if current else sent
    if current.strip():
        chunks.append(current.strip())
    if not chunks:
        for i in range(0, len(text), max_chars):
            chunks.append(text[i:i + max_chars])
    return chunks


def get_playlist_videos(playlist_url):
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--print", "%(id)s\t%(title)s", playlist_url],
        capture_output=True, text=True, timeout=60
    )
    videos = []
    seen_ids = set()
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            vid_id, title = parts
            if vid_id not in seen_ids:
                seen_ids.add(vid_id)
                videos.append({"id": vid_id, "title": title})
    return videos


def download_audio(video_id, output_dir):
    audio_path = output_dir / f"{video_id}.m4a"
    if audio_path.exists():
        return audio_path

    result = subprocess.run(
        [
            "yt-dlp",
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "--extract-audio",
            "--audio-format", "m4a",
            "-o", str(output_dir / "%(id)s.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr[:500]}")

    for ext in [".m4a", ".webm", ".opus", ".mp3"]:
        p = output_dir / f"{video_id}{ext}"
        if p.exists():
            return p

    raise FileNotFoundError(f"No audio file found for {video_id}")


def convert_to_wav(audio_path, output_dir):
    wav_path = output_dir / f"{audio_path.stem}.wav"
    if wav_path.exists():
        return wav_path

    result = subprocess.run(
        ["ffmpeg", "-i", str(audio_path), "-ar", "16000", "-ac", "1", "-y", str(wav_path)],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")
    return wav_path


def transcribe(wav_path, output_dir):
    txt_path = output_dir / f"{wav_path.stem}.txt"
    if txt_path.exists():
        text = txt_path.read_text().strip()
        if text:
            return text

    result = subprocess.run(
        [
            "mlx_whisper",
            "--model", "mlx-community/whisper-large-v3-turbo",
            "--language", "en",
            "--output-format", "txt",
            "--output-dir", str(output_dir),
            str(wav_path),
        ],
        capture_output=True, text=True, timeout=1800  # 30 min max per video
    )
    if result.returncode != 0:
        raise RuntimeError(f"mlx_whisper failed: {result.stderr[:500]}")

    if txt_path.exists():
        return txt_path.read_text().strip()
    raise FileNotFoundError(f"Transcription output not found: {txt_path}")


def process_video(video, audio_dir, transcript_dir):
    vid_id = video["id"]
    title = video["title"]
    stats["current_video"] = title[:60]

    done_marker = transcript_dir / f"{vid_id}.ingested"
    if done_marker.exists():
        log(f"  SKIP (already ingested): {title[:60]}")
        stats["skipped_dupes"] += 1
        return

    log(f"  Downloading audio: {title[:60]}...")
    audio_path = download_audio(vid_id, audio_dir)
    stats["downloaded"] += 1

    log(f"  Converting to WAV...")
    wav_path = convert_to_wav(audio_path, audio_dir)

    log(f"  Transcribing with MLX Whisper...")
    transcript = transcribe(wav_path, transcript_dir)
    stats["transcribed"] += 1
    log(f"  Transcription: {len(transcript)} chars, {len(transcript.split())} words")

    stored = remember(
        f"YouTube lecture transcription: {title}\n\n{transcript}",
        title,
        vid_id,
        video.get("playlist", "unknown"),
    )
    stats["memories_stored"] += stored
    stats["ingested"] += 1

    done_marker.write_text(datetime.now().isoformat())

    # Clean up WAV (keep audio and transcript)
    wav_path.unlink(missing_ok=True)

    log(f"  DONE: {title[:60]} — {stored} memory chunks")


def main():
    global last_status_time
    stats["start_time"] = time.time()
    last_status_time = time.time()

    load_slack_token()

    playlists = sys.argv[1:]
    if not playlists:
        log("ERROR: No playlist URLs provided")
        print("Usage: nova_youtube_ingest.py <playlist_url> [playlist_url ...]")
        sys.exit(1)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    audio_dir = WORK_DIR / "audio"
    transcript_dir = WORK_DIR / "transcripts"
    audio_dir.mkdir(exist_ok=True)
    transcript_dir.mkdir(exist_ok=True)

    all_videos = []
    seen_ids = set()
    for url in playlists:
        log(f"Enumerating playlist: {url}")
        videos = get_playlist_videos(url)
        for v in videos:
            if v["id"] not in seen_ids:
                seen_ids.add(v["id"])
                v["playlist"] = url.split("list=")[-1][:20] if "list=" in url else "unknown"
                all_videos.append(v)
        log(f"  Found {len(videos)} videos ({len(all_videos)} unique total)")

    stats["total_videos"] = len(all_videos)
    log(f"Total unique videos to process: {len(all_videos)}")

    post_slack(
        f":movie_camera: *YouTube Ingest Started*\n"
        f"• Playlists: {len(playlists)}\n"
        f"• Unique videos: {len(all_videos)}\n"
        f"• Pipeline: download → extract audio → MLX Whisper → vector memory\n"
        f"• Privacy: `local-only` — zero cloud\n"
        f"• Status updates every 5 minutes"
    )

    for video in all_videos:
        if shutdown_requested:
            break
        try:
            process_video(video, audio_dir, transcript_dir)
        except Exception as e:
            stats["errors"] += 1
            log(f"  ERROR on {video['title'][:40]}: {e}")
            traceback.print_exc(file=sys.stdout)
        post_status()

        with open(STATE_FILE, "w") as f:
            json.dump(stats, f, indent=2, default=str)

    elapsed = time.time() - stats["start_time"]
    mins = int(elapsed // 60)

    summary = (
        f":white_check_mark: *YouTube Ingest Complete*\n"
        f"• Videos: {stats['ingested']}/{stats['total_videos']}\n"
        f"• Memories stored: {stats['memories_stored']}\n"
        f"• Errors: {stats['errors']} | Dupes skipped: {stats['skipped_dupes']}\n"
        f"• Time: {mins} min\n"
        f"• Transcripts saved: `{transcript_dir}`\n"
        f"• All data tagged `privacy:local-only`"
    )
    if shutdown_requested:
        summary = summary.replace("Complete", "INTERRUPTED — re-run to resume")

    log(summary.replace("*", "").replace(":white_check_mark:", "OK"))
    post_slack(summary)


if __name__ == "__main__":
    main()
