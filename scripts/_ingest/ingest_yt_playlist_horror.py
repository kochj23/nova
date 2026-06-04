#!/usr/bin/env python3
"""YouTube playlist ingestion with 2-min delay between videos."""
import json, os, subprocess, sys, time, urllib.request
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config

MEMORY_URL = "http://192.168.1.6:18790/remember"
WORK_DIR = Path("/Volumes/Data/youtube-ingest/playlist3")
CHUNK_WORDS = 400
DELAY_BETWEEN_VIDEOS = 120  # 2 minutes

count = 0
failed = 0
total_videos = 0
completed_videos = 0
skipped_videos = 0
start_time = time.time()

def log(msg):
    print(f"[yt-ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)

def remember(text, metadata):
    global count, failed
    payload = json.dumps({"text": nova_config.truncate_at_boundary(text), "source": "youtube_transcript", "metadata": metadata}).encode()
    req = urllib.request.Request(MEMORY_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15):
            count += 1
            return True
    except:
        failed += 1
        return False

def chunk_text(text):
    words = text.split()
    chunks = []
    for i in range(0, len(words), CHUNK_WORDS):
        chunk = " ".join(words[i:i + CHUNK_WORDS])
        if len(chunk.strip()) > 50:
            chunks.append(chunk)
    return chunks

def get_playlist_videos(url):
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--print", "%(id)s\t%(title)s", url],
        capture_output=True, text=True, timeout=60
    )
    videos = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and "[Private" not in parts[1] and "[Deleted" not in parts[1]:
            videos.append({"id": parts[0], "title": parts[1]})
    return videos

def download_audio(video_id, output_dir):
    output_path = output_dir / f"{video_id}.wav"
    if output_path.exists():
        return output_path
    result = subprocess.run([
        "yt-dlp", "-x", "--audio-format", "wav", "--audio-quality", "0",
        "-o", str(output_dir / f"{video_id}.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}"
    ], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        log(f"  Download failed: {result.stderr[:200]}")
        return None
    if output_path.exists():
        return output_path
    for ext in ["m4a", "opus", "webm", "mp3"]:
        alt = output_dir / f"{video_id}.{ext}"
        if alt.exists():
            subprocess.run(["ffmpeg", "-i", str(alt), "-ar", "16000", "-ac", "1", str(output_path), "-y"],
                          capture_output=True, timeout=300)
            alt.unlink()
            if output_path.exists():
                return output_path
    return None

def transcribe(audio_path):
    try:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
            language="en",
        )
        return result.get("text", "")
    except Exception as e:
        log(f"  Transcription error: {e}")
        return ""

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLt-rS-1ZDnoWnuzMlAW5ChLO0UMTJ8-JL"

log("Fetching playlist...")
WORK_DIR.mkdir(parents=True, exist_ok=True)
videos = get_playlist_videos(PLAYLIST_URL)
total_videos = len(videos)
log(f"Found {total_videos} accessible videos (private videos skipped)")

slack_post(
    f":movie_camera: *YouTube Playlist Ingestion Started*\n"
    f"  Videos: {total_videos} (private videos skipped)\n"
    f"  Delay: 2 min between downloads\n"
    f"  Content: Horror crossover battles\n"
    f"  _Status updates every 5 min_"
)

last_status = time.time()

for i, video in enumerate(videos):
    vid_id = video["id"]
    title = video["title"]
    log(f"[{i+1}/{total_videos}] Processing: {title}")

    # Download
    log(f"  Downloading audio...")
    audio_path = download_audio(vid_id, WORK_DIR)
    if not audio_path:
        log(f"  SKIPPED (download failed)")
        skipped_videos += 1
        completed_videos += 1
        if i < total_videos - 1:
            log(f"  Waiting 2 min before next video...")
            time.sleep(DELAY_BETWEEN_VIDEOS)
        continue

    # Transcribe
    log(f"  Transcribing with MLX Whisper...")
    transcript = transcribe(audio_path)
    if not transcript:
        log(f"  SKIPPED (transcription failed)")
        skipped_videos += 1
        completed_videos += 1
        audio_path.unlink(missing_ok=True)
        if i < total_videos - 1:
            log(f"  Waiting 2 min before next video...")
            time.sleep(DELAY_BETWEEN_VIDEOS)
        continue

    log(f"  Transcript: {len(transcript.split())} words")

    # Chunk and ingest
    chunks = chunk_text(transcript)
    for j, chunk in enumerate(chunks):
        metadata = {
            "type": "youtube_transcript",
            "video_id": vid_id,
            "title": title,
            "chunk": j + 1,
            "total_chunks": len(chunks),
            "playlist": PLAYLIST_URL,
        }
        remember(f"[YouTube: {title}] {chunk}", metadata)

    completed_videos += 1
    log(f"  Done: {len(chunks)} chunks ingested")
    audio_path.unlink(missing_ok=True)

    # Status every 5 min
    if time.time() - last_status > 300:
        elapsed = time.time() - start_time
        slack_post(
            f":movie_camera: *YouTube Ingestion Progress*\n"
            f"  Videos: {completed_videos}/{total_videos}\n"
            f"  Chunks: {count:,} | Skipped: {skipped_videos}\n"
            f"  Elapsed: {elapsed/60:.1f}m"
        )
        last_status = time.time()

    # Wait 2 minutes between videos
    if i < total_videos - 1:
        log(f"  Waiting 2 min before next video...")
        time.sleep(DELAY_BETWEEN_VIDEOS)

# Final
elapsed = time.time() - start_time
slack_post(
    f":white_check_mark: *YouTube Playlist Ingestion Complete*\n"
    f"  Videos: {completed_videos}/{total_videos} (skipped: {skipped_videos})\n"
    f"  Chunks: {count:,}\n"
    f"  Duration: {elapsed/60:.1f}m"
)
log(f"DONE: {count} chunks from {completed_videos} videos in {elapsed/60:.1f}m")
