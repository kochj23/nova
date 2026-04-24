#!/usr/bin/env python3
"""
nova_youtube_channel_ingest.py — Ingest an entire YouTube channel:
download audio, transcribe with MLX Whisper, store in Nova's vector memory.

Posts 10-minute status updates to Slack + Discord #nova-notifications
and Signal (Jordan's number).

Usage: nova_youtube_channel_ingest.py <channel_url> [--delay SECONDS] [--status-interval SECONDS]

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

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

WORK_DIR = Path("/Volumes/Data/nova-youtube-ingest")
MEMORY_URL = "http://127.0.0.1:18790/remember?async=1"
LOG_FILE = WORK_DIR / "channel-ingest.log"
STATE_FILE = WORK_DIR / "channel-ingest-state.json"
SIGNAL_CLI = "http://127.0.0.1:8080"
JORDAN_PHONE = "+13233645436"

DEFAULT_DELAY = 120
DEFAULT_STATUS_INTERVAL = 600

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
    "channel_name": "",
}
last_status_time = 0
shutdown_requested = False
video_delay = DEFAULT_DELAY
status_interval = DEFAULT_STATUS_INTERVAL


def signal_handler(sig, frame):
    global shutdown_requested
    shutdown_requested = True
    log("Shutdown requested, finishing current video...")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[yt-channel {ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def post_signal(message):
    plain = re.sub(r':[a-z_]+:', '', message)
    plain = re.sub(r'\*([^*]+)\*', r'\1', plain)
    plain = re.sub(r'`([^`]+)`', r'\1', plain)
    try:
        data = json.dumps({
            "jsonrpc": "2.0",
            "method": "send",
            "id": int(time.time()),
            "params": {
                "message": plain,
                "recipient": [JORDAN_PHONE],
            },
        }).encode()
        req = urllib.request.Request(
            f"{SIGNAL_CLI}/api/v1/rpc",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log(f"Signal post failed: {e}")


def notify(message):
    nova_config.post_both(message, slack_channel=nova_config.SLACK_NOTIFY)
    post_signal(message)


def post_status(force=False):
    global last_status_time
    now = time.time()
    if not force and now - last_status_time < status_interval:
        return
    last_status_time = now

    elapsed = now - stats["start_time"] if stats["start_time"] else 0
    mins = int(elapsed // 60)
    pct = (stats["ingested"] / stats["total_videos"] * 100) if stats["total_videos"] > 0 else 0

    msg = (
        f":movie_camera: *YouTube Channel Ingest — Status*\n"
        f"• Channel: `{stats['channel_name']}`\n"
        f"• Progress: {stats['ingested']}/{stats['total_videos']} videos ({pct:.0f}%)\n"
        f"• Downloaded: {stats['downloaded']} | Transcribed: {stats['transcribed']}\n"
        f"• Memories stored: {stats['memories_stored']}\n"
        f"• Errors: {stats['errors']} | Dupes skipped: {stats['skipped_dupes']}\n"
        f"• Elapsed: {mins} min\n"
        f"• Current: `{stats['current_video']}`"
    )
    notify(msg)


def remember(text, title, video_id, channel_name):
    if not text or len(text.strip()) < 50:
        return 0

    chunks = chunk_text(text, max_chars=2000)
    stored = 0
    for i, chunk in enumerate(chunks):
        chunk_title = f"{title} (part {i+1}/{len(chunks)})" if len(chunks) > 1 else title
        payload = json.dumps({
            "text": chunk,
            "source": "youtube-channel-ingest",
            "metadata": {
                "privacy": "local-only",
                "origin": "youtube-transcription",
                "video_id": video_id,
                "title": chunk_title,
                "channel": channel_name,
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


def get_channel_videos(channel_url):
    log(f"Enumerating channel: {channel_url}")
    result = subprocess.run(
        [
            "yt-dlp", "--flat-playlist",
            "--print", "%(id)s\t%(title)s\t%(upload_date)s\t%(duration)s",
            f"{channel_url}/videos",
        ],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        log(f"yt-dlp enumerate failed: {result.stderr[:300]}")
        return [], "unknown"

    channel_name = "unknown"
    name_result = subprocess.run(
        ["yt-dlp", "--print", "%(channel)s", "--playlist-items", "1", f"{channel_url}/videos"],
        capture_output=True, text=True, timeout=30
    )
    if name_result.returncode == 0 and name_result.stdout.strip():
        channel_name = name_result.stdout.strip().splitlines()[0]

    videos = []
    seen_ids = set()
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            vid_id = parts[0]
            title = parts[1]
            upload_date = parts[2] if len(parts) > 2 else ""
            duration = parts[3] if len(parts) > 3 else ""
            if vid_id and vid_id not in seen_ids:
                seen_ids.add(vid_id)
                videos.append({
                    "id": vid_id,
                    "title": title,
                    "upload_date": upload_date,
                    "duration": duration,
                })

    log(f"Found {len(videos)} videos on channel '{channel_name}'")
    return videos, channel_name


def download_audio(video_id, output_dir):
    for ext in [".m4a", ".webm", ".opus", ".mp3"]:
        p = output_dir / f"{video_id}{ext}"
        if p.exists():
            return p

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
        capture_output=True, text=True, timeout=1800
    )
    if result.returncode != 0:
        raise RuntimeError(f"mlx_whisper failed: {result.stderr[:500]}")

    if txt_path.exists():
        return txt_path.read_text().strip()
    raise FileNotFoundError(f"Transcription output not found: {txt_path}")


def process_video(video, audio_dir, transcript_dir, channel_name):
    vid_id = video["id"]
    title = video["title"]
    stats["current_video"] = title[:60]

    done_marker = transcript_dir / f"{vid_id}.ingested"
    if done_marker.exists():
        log(f"  SKIP (already ingested): {title[:60]}")
        stats["skipped_dupes"] += 1
        return

    log(f"  [{stats['ingested']+1}/{stats['total_videos']}] Downloading: {title[:60]}...")
    audio_path = download_audio(vid_id, audio_dir)
    stats["downloaded"] += 1

    log(f"  Converting to WAV...")
    wav_path = convert_to_wav(audio_path, audio_dir)

    log(f"  Transcribing with MLX Whisper...")
    transcript = transcribe(wav_path, transcript_dir)
    stats["transcribed"] += 1
    log(f"  Transcription: {len(transcript)} chars, {len(transcript.split())} words")

    stored = remember(
        f"YouTube video transcription — Channel: {channel_name} — Title: {title}\n\n{transcript}",
        title,
        vid_id,
        channel_name,
    )
    stats["memories_stored"] += stored
    stats["ingested"] += 1

    done_marker.write_text(datetime.now().isoformat())

    wav_path.unlink(missing_ok=True)
    audio_path.unlink(missing_ok=True)

    log(f"  DONE: {title[:60]} — {stored} memory chunks stored")


def main():
    global last_status_time, video_delay, status_interval

    args = sys.argv[1:]
    channel_url = None
    i = 0
    while i < len(args):
        if args[i] == "--delay" and i + 1 < len(args):
            video_delay = int(args[i + 1])
            i += 2
        elif args[i] == "--status-interval" and i + 1 < len(args):
            status_interval = int(args[i + 1])
            i += 2
        else:
            channel_url = args[i]
            i += 1

    if not channel_url:
        print("Usage: nova_youtube_channel_ingest.py <channel_url> [--delay 120] [--status-interval 600]")
        sys.exit(1)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    audio_dir = WORK_DIR / "audio"
    transcript_dir = WORK_DIR / "transcripts"
    audio_dir.mkdir(exist_ok=True)
    transcript_dir.mkdir(exist_ok=True)

    stats["start_time"] = time.time()
    last_status_time = time.time()

    videos, channel_name = get_channel_videos(channel_url)
    stats["channel_name"] = channel_name

    if not videos:
        log("No videos found. Check the URL.")
        sys.exit(1)

    already_done = sum(1 for v in videos if (transcript_dir / f"{v['id']}.ingested").exists())
    remaining = len(videos) - already_done
    stats["total_videos"] = len(videos)
    stats["skipped_dupes"] = already_done

    notify(
        f":movie_camera: *YouTube Channel Ingest Started*\n"
        f"• Channel: `{channel_name}`\n"
        f"• URL: {channel_url}\n"
        f"• Total videos: {len(videos)} ({already_done} already done, {remaining} remaining)\n"
        f"• Pipeline: download → extract audio → MLX Whisper → vector memory\n"
        f"• Delay between videos: {video_delay}s\n"
        f"• Privacy: `local-only` — zero cloud APIs\n"
        f"• Status updates every {status_interval // 60} minutes"
    )

    for idx, video in enumerate(videos):
        if shutdown_requested:
            break
        try:
            process_video(video, audio_dir, transcript_dir, channel_name)
        except Exception as e:
            stats["errors"] += 1
            log(f"  ERROR on {video['title'][:40]}: {e}")
            traceback.print_exc(file=sys.stdout)

        post_status()

        with open(STATE_FILE, "w") as f:
            json.dump(stats, f, indent=2, default=str)

        if idx < len(videos) - 1 and not shutdown_requested:
            done_marker = transcript_dir / f"{video['id']}.ingested"
            if not (transcript_dir / f"{video['id']}.ingested").exists() or stats["ingested"] > 0:
                log(f"  Waiting {video_delay}s before next video...")
                for _ in range(video_delay):
                    if shutdown_requested:
                        break
                    time.sleep(1)

    elapsed = time.time() - stats["start_time"]
    hours = int(elapsed // 3600)
    mins = int((elapsed % 3600) // 60)

    summary = (
        f":white_check_mark: *YouTube Channel Ingest Complete*\n"
        f"• Channel: `{channel_name}`\n"
        f"• Videos processed: {stats['ingested']}/{stats['total_videos']}\n"
        f"• Memories stored: {stats['memories_stored']}\n"
        f"• Errors: {stats['errors']} | Already done: {stats['skipped_dupes']}\n"
        f"• Time: {hours}h {mins}m\n"
        f"• Transcripts: `{transcript_dir}`\n"
        f"• All data tagged `privacy:local-only`"
    )
    if shutdown_requested:
        summary = summary.replace("Complete", "INTERRUPTED — re-run to resume")

    log(summary.replace("*", "").replace(":white_check_mark:", "OK"))
    notify(summary)


if __name__ == "__main__":
    main()
