#!/usr/bin/env python3
"""YouTube playlist ingestion — auto-classifies source based on transcript content."""
import json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config

MEMORY_URL = "http://192.168.1.6:18790/remember"
WORK_DIR = Path("/Volumes/Data/youtube-ingest/playlist4")
CHUNK_WORDS = 400
DELAY_BETWEEN_VIDEOS = 120

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

def classify_source(title, transcript_snippet):
    """Determine the best memory source based on content."""
    text = (title + " " + transcript_snippet[:500]).lower()
    if any(w in text for w in ["comic", "superhero", "marvel", "dc ", "hulk", "superman", "batman", "spider", "avenger", "x-men", "thanos", "villain"]):
        return "comic_books"
    if any(w in text for w in ["horror", "jason", "freddy", "michael myers", "pennywise", "ghost", "demon", "slasher"]):
        return "horror"
    if any(w in text for w in ["anime", "manga", "dragon ball", "naruto", "one piece", "goku"]):
        return "anime"
    if any(w in text for w in ["invincible", "omni-man", "thragg", "viltrumite", "mark grayson"]):
        return "comic_books"
    if any(w in text for w in ["who would win", "versus", "vs", "fight", "battle", "legendary fights"]):
        return "comic_books"
    return "video"

def remember(text, source, metadata):
    global count, failed
    payload = json.dumps({"text": nova_config.truncate_at_boundary(text), "source": source, "metadata": metadata}).encode()
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
        capture_output=True, text=True, timeout=120
    )
    videos = []
    seen_ids = set()
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and "[Private" not in parts[1] and "[Deleted" not in parts[1]:
            vid_id = parts[0]
            if vid_id not in seen_ids:
                seen_ids.add(vid_id)
                videos.append({"id": vid_id, "title": parts[1]})
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
        log(f"  Download failed: {result.stderr[:150]}")
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

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLB0AIbpVgTEGcuMq_QEwWu24y820x7k23"

log("Fetching playlist...")
WORK_DIR.mkdir(parents=True, exist_ok=True)
videos = get_playlist_videos(PLAYLIST_URL)
total_videos = len(videos)
log(f"Found {total_videos} unique accessible videos")

slack_post(
    f":movie_camera: *YouTube Playlist Ingestion Started*\n"
    f"  Videos: {total_videos}\n"
    f"  Delay: 2 min between downloads\n"
    f"  Content: LEGENDARY FIGHTS (comic/superhero battles)\n"
    f"  Auto-classifying source per video\n"
    f"  _Status every 5 min_"
)

last_status = time.time()
sources_used = {}

for i, video in enumerate(videos):
    vid_id = video["id"]
    title = video["title"]
    log(f"[{i+1}/{total_videos}] {title}")

    audio_path = download_audio(vid_id, WORK_DIR)
    if not audio_path:
        skipped_videos += 1
        completed_videos += 1
        if i < total_videos - 1:
            log(f"  Waiting 2 min...")
            time.sleep(DELAY_BETWEEN_VIDEOS)
        continue

    log(f"  Transcribing...")
    transcript = transcribe(audio_path)
    if not transcript:
        skipped_videos += 1
        completed_videos += 1
        audio_path.unlink(missing_ok=True)
        if i < total_videos - 1:
            time.sleep(DELAY_BETWEEN_VIDEOS)
        continue

    # Classify source
    source = classify_source(title, transcript)
    sources_used[source] = sources_used.get(source, 0) + 1
    log(f"  {len(transcript.split())} words → source: {source}")

    chunks = chunk_text(transcript)
    for j, chunk in enumerate(chunks):
        metadata = {
            "type": "youtube_transcript",
            "video_id": vid_id,
            "title": title,
            "chunk": j + 1,
            "total_chunks": len(chunks),
        }
        remember(f"[YouTube: {title}] {chunk}", source, metadata)

    completed_videos += 1
    log(f"  {len(chunks)} chunks ingested")
    audio_path.unlink(missing_ok=True)

    # Status every 5 min
    if time.time() - last_status > 300:
        elapsed = time.time() - start_time
        src_str = ", ".join(f"{k}:{v}" for k, v in sorted(sources_used.items()))
        slack_post(
            f":movie_camera: *YouTube Ingestion Progress*\n"
            f"  Videos: {completed_videos}/{total_videos} ({skipped_videos} skipped)\n"
            f"  Chunks: {count:,} | Failed: {failed}\n"
            f"  Sources: {src_str}\n"
            f"  Elapsed: {elapsed/60:.1f}m | ETA: {((elapsed/max(1,completed_videos))*(total_videos-completed_videos))/60:.0f}m"
        )
        last_status = time.time()

    if i < total_videos - 1:
        log(f"  Waiting 2 min...")
        time.sleep(DELAY_BETWEEN_VIDEOS)

elapsed = time.time() - start_time
src_str = ", ".join(f"{k}:{v}" for k, v in sorted(sources_used.items()))
slack_post(
    f":white_check_mark: *YouTube Playlist Complete (LEGENDARY FIGHTS)*\n"
    f"  Videos: {completed_videos}/{total_videos} (skipped: {skipped_videos})\n"
    f"  Chunks: {count:,}\n"
    f"  Sources: {src_str}\n"
    f"  Duration: {elapsed/60:.1f}m"
)
log(f"DONE: {count} chunks, {elapsed/60:.1f}m")
